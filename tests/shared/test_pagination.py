"""Tests for pagination and the shared response envelopes.

Small, pure, fast — no database, no app. These are the arithmetic that every list
endpoint depends on, and an off-by-one here is a bug in all of them at once.
"""

from __future__ import annotations

import pytest

from app.shared.schemas.base_schema import Page, PageMeta
from app.shared.utils.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageParams


# --------------------------------------------------------------------------
# PageParams
# --------------------------------------------------------------------------
def test_offset_is_zero_on_the_first_page() -> None:
    """The off-by-one that matters.

    `(page - 1) * size`, not `page * size`. The naive version silently skips the entire
    first page — a bug that looks like missing data and is trivially easy to write.
    """
    assert PageParams(page=1, size=20).offset == 0


@pytest.mark.parametrize(
    ("page", "size", "expected_offset"),
    [
        (1, 20, 0),
        (2, 20, 20),
        (3, 10, 20),
        (5, 100, 400),
    ],
)
def test_offset_arithmetic(page: int, size: int, expected_offset: int) -> None:
    assert PageParams(page=page, size=size).offset == expected_offset


def test_limit_mirrors_size() -> None:
    """The API speaks pages; the database speaks offset/limit. Translated once, here —
    so no repository ever sees a page number and no router computes an offset."""
    params = PageParams(page=3, size=25)
    assert params.limit == params.size == 25


def test_defaults_are_sane() -> None:
    params = PageParams()
    assert params.page == 1
    assert params.size == DEFAULT_PAGE_SIZE
    assert params.offset == 0


def test_max_page_size_is_bounded() -> None:
    """The cap is a DoS control.

    An unbounded `size` lets one request materialise a million ORM objects. Pydantic
    enforces `le=MAX_PAGE_SIZE` at the edge, so no downstream code can bypass it —
    this test pins the constant so it cannot be quietly raised.
    """
    assert MAX_PAGE_SIZE == 100
    assert DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE


# --------------------------------------------------------------------------
# PageMeta
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("total", "size", "expected_pages"),
    [
        (0, 20, 0),  # empty result set: zero pages, not one
        (1, 20, 1),
        (20, 20, 1),  # exactly one full page
        (21, 20, 2),  # the ceiling, not truncation
        (100, 7, 15),  # 14.28... -> 15
    ],
)
def test_page_count_rounds_up(total: int, size: int, expected_pages: int) -> None:
    """`ceil`, not integer division.

    With truncation, 21 items at 20 per page would report 1 page and the last item
    would be unreachable through the UI.
    """
    assert PageMeta(total=total, page=1, size=size).pages == expected_pages


def test_has_next_and_has_previous_at_the_boundaries() -> None:
    """Computed from `total`/`page`/`size`, so they cannot contradict them.

    Storing these as fields is how a response ends up saying `has_next: true` on the
    last page.
    """
    first = PageMeta(total=50, page=1, size=20)
    assert first.has_next is True
    assert first.has_previous is False

    middle = PageMeta(total=50, page=2, size=20)
    assert middle.has_next is True
    assert middle.has_previous is True

    last = PageMeta(total=50, page=3, size=20)
    assert last.has_next is False
    assert last.has_previous is True


def test_empty_page_reports_no_next() -> None:
    meta = PageMeta(total=0, page=1, size=20)
    assert meta.pages == 0
    assert meta.has_next is False
    assert meta.has_previous is False


# --------------------------------------------------------------------------
# Page envelope
# --------------------------------------------------------------------------
def test_page_create_assembles_the_envelope() -> None:
    """The named constructor exists so no endpoint hand-builds the dict.

    Building `{"items": ..., "meta": {...}}` inline in eight routers is how `page` and
    `size` end up swapped in one of them.
    """
    page: Page[str] = Page.create(["a", "b"], total=5, page=1, size=2)

    assert page.items == ["a", "b"]
    assert page.meta.total == 5
    assert page.meta.pages == 3
    assert page.meta.has_next is True


def test_page_is_generic_over_its_item_type() -> None:
    """`Page[T]` is defined once and reused with precise types.

    FastAPI resolves the parameter into the OpenAPI schema, so clients see
    `items: FoodItemRead[]` rather than `items: object[]`.
    """
    page: Page[int] = Page.create([1, 2, 3], total=3, page=1, size=10)
    assert page.items == [1, 2, 3]
    assert page.meta.pages == 1


# --------------------------------------------------------------------------
# Cache keys — small, but a wrong key silently halves the hit rate
# --------------------------------------------------------------------------
def test_cache_key_is_order_independent() -> None:
    """`?page=1&size=20` and `?size=20&page=1` must produce ONE key.

    Without sorting, the same logical query gets two cache entries — halving the hit
    rate for no reason, with no error to notice.
    """
    from app.shared.cache.cache import build_cache_key

    first = build_cache_key("ns", page=1, size=20, search=None)
    second = build_cache_key("ns", size=20, search=None, page=1)
    assert first == second


def test_cache_key_drops_none_values() -> None:
    """An absent filter and an explicit `None` share a key."""
    from app.shared.cache.cache import build_cache_key

    assert build_cache_key("ns", page=1, search=None) == build_cache_key("ns", page=1)


def test_cache_key_distinguishes_different_queries() -> None:
    from app.shared.cache.cache import build_cache_key

    assert build_cache_key("ns", page=1) != build_cache_key("ns", page=2)
