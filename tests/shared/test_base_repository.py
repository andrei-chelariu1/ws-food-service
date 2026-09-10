"""Tests for `BaseRepository`.

WHY THE GENERIC BASE GETS ITS OWN TESTS
---------------------------------------
Four modules inherit these seven methods. A bug here is a bug in every module at
once — most dangerously the soft-delete filter, whose failure mode is *silently
returning deleted rows to users*. That is worth testing once, thoroughly, rather
than hoping four module test suites happen to cover it.

The tests use `FoodItem` (soft-deletable) and `User` (not), because the interesting
behaviour is precisely the difference between them.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.modules.food_items.models import FoodItemStatus
from app.modules.food_items.repository import FoodItemRepository
from app.modules.users.models import User
from app.modules.users.repository import UserRepository


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def test_get_returns_none_for_a_missing_row(db_session: Any) -> None:
    """Absence is `None`, not an exception.

    Whether "missing" is an error depends on the caller: a profile lookup wants a 404,
    the registration uniqueness check wants `None`. Only the caller knows, so the
    repository does not decide.
    """
    repo = UserRepository(db_session)
    assert await repo.get(uuid.uuid4()) is None


async def test_get_by_raises_when_multiple_rows_match(
    db_session: Any,
    make_user: Any,
) -> None:
    """`get_by` refuses to guess.

    Returning the first of several matches would hide a real problem — the caller
    assumed a uniqueness constraint that does not exist. Failing loudly surfaces the
    data-model bug instead of papering over it.
    """
    await make_user(full_name="Duplicate Name")
    await make_user(full_name="Duplicate Name")

    repo = UserRepository(db_session)
    with pytest.raises(Exception):  # noqa: B017 — SQLAlchemy's MultipleResultsFound
        await repo.get_by(full_name="Duplicate Name")


async def test_exists_is_true_only_when_a_row_matches(
    db_session: Any,
    make_user: Any,
) -> None:
    await make_user(email="present@example.com")
    repo = UserRepository(db_session)

    assert await repo.exists(email="present@example.com") is True
    assert await repo.exists(email="absent@example.com") is False


async def test_list_has_a_deterministic_default_order(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """Without an explicit `order_by`, results are still stable.

    Postgres may return rows in any order absent ORDER BY, which makes page 2 able to
    repeat or skip rows from page 1. `BaseRepository.list` falls back to ordering by
    primary key so pagination is never silently broken.
    """
    donor = await make_user()
    for index in range(3):
        await make_food_item(owner_id=donor.id, name=f"Item {index}")

    repo = FoodItemRepository(db_session)
    first = await repo.list(limit=10)
    second = await repo.list(limit=10)

    assert [item.id for item in first] == [item.id for item in second]


async def test_count_applies_the_same_filters_as_list(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """`count` counts over a subquery of `_base_select()`.

    So it applies exactly the filters `list` does — including soft delete. Duplicating
    the WHERE clause is how you get a `total` that disagrees with the items returned.
    """
    donor = await make_user()
    kept = await make_food_item(owner_id=donor.id, name="Kept")
    removed = await make_food_item(owner_id=donor.id, name="Removed")

    repo = FoodItemRepository(db_session)
    assert await repo.count() == 2

    await repo.delete(removed)

    assert await repo.count() == 1
    assert [item.id for item in await repo.list()] == [kept.id]


# --------------------------------------------------------------------------
# Soft delete — the highest-value behaviour in this class
# --------------------------------------------------------------------------
async def test_delete_is_soft_for_models_with_the_mixin(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """`FoodItem` is marked, not removed — and the timestamp is set with the flag."""
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    repo = FoodItemRepository(db_session)
    await repo.delete(item)

    assert item.is_deleted is True
    assert item.deleted_at is not None
    assert await repo.get(item.id) is None
    assert await repo.get(item.id, include_deleted=True) is not None


async def test_delete_is_hard_for_models_without_the_mixin(
    db_session: Any,
    make_user: Any,
) -> None:
    """`User` has no `SoftDeleteMixin`, so the row is genuinely removed.

    One `delete()` call, correct behaviour for both — decided by the model, not by the
    caller. That is why no service has to keep straight which entities are
    soft-deletable.
    """
    user = await make_user()
    user_id = user.id

    repo = UserRepository(db_session)
    await repo.delete(user)

    assert await repo.get(user_id) is None
    assert await repo.get(user_id, include_deleted=True) is None  # truly gone


async def test_soft_deleted_rows_are_excluded_from_every_read(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """THE CRITICAL TEST. No read path leaks a deleted row.

    The filter lives in `_base_select()`, so it applies to `get`, `get_by`, `list`,
    `count` and every module-specific query built on top of them — none of which
    contains an explicit `is_deleted` filter. This test is what proves that the
    "forgetting is impossible" claim in shared/repository/README.md is actually true.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id, name="Vanishing")

    repo = FoodItemRepository(db_session)
    await repo.delete(item)

    assert await repo.get(item.id) is None
    assert await repo.get_by(name="Vanishing") is None
    assert await repo.list() == []
    assert await repo.count() == 0
    # And the module-specific query, which never mentions is_deleted either:
    assert await repo.list_by_owner(donor.id) == []


async def test_restore_undoes_a_soft_delete(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """Recoverable by design — one of the reasons soft delete is worth its cost."""
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    repo = FoodItemRepository(db_session)
    await repo.delete(item)
    item.restore()
    await db_session.flush()

    assert await repo.get(item.id) is not None


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
async def test_add_flushes_so_the_id_is_available(
    db_session: Any,
    make_user: Any,
) -> None:
    """After `add`, server defaults and timestamps are populated.

    `add` flushes rather than commits: the INSERT is sent (so constraints fire here,
    where a service can translate the error) but the transaction boundary stays with
    the request.
    """
    repo = UserRepository(db_session)
    user = User(
        email="flushed@example.com",
        hashed_password="$2b$04$abcdefghijklmnopqrstuv",
        full_name="Flushed",
    )

    await repo.add(user)

    assert user.id is not None
    assert user.created_at is not None


async def test_update_rejects_an_unknown_field(
    db_session: Any,
    make_user: Any,
) -> None:
    """A typo'd field name raises instead of being silently ignored.

    "My update didn't save" is a miserable bug to chase. Failing loudly at the moment of
    the mistake is worth the strictness.
    """
    user = await make_user()
    repo = UserRepository(db_session)

    with pytest.raises(AttributeError):
        await repo.update(user, no_such_field="value")


async def test_update_only_changes_the_named_fields(
    db_session: Any,
    make_user: Any,
) -> None:
    user = await make_user(full_name="Original Name")
    original_email = user.email

    repo = UserRepository(db_session)
    await repo.update(user, full_name="New Name")

    assert user.full_name == "New Name"
    assert user.email == original_email


async def test_hard_delete_by_id_bypasses_soft_delete(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """The GDPR-erasure escape hatch, named so it cannot be used by accident.

    `grep hard_delete` finds every caller — which is exactly what a data-retention audit
    needs.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)
    item_id = item.id

    repo = FoodItemRepository(db_session)
    assert await repo.hard_delete_by_id(item_id) == 1
    assert await repo.get(item_id, include_deleted=True) is None


# --------------------------------------------------------------------------
# Module-specific queries built on the base
# --------------------------------------------------------------------------
async def test_list_by_owner_and_status_filters_both(
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
) -> None:
    donor = await make_user()
    other = await make_user()
    await make_food_item(owner_id=donor.id, name="Mine available")
    await make_food_item(owner_id=donor.id, name="Mine reserved", status=FoodItemStatus.RESERVED)
    await make_food_item(owner_id=other.id, name="Theirs")

    repo = FoodItemRepository(db_session)
    available = await repo.list_by_owner(donor.id, status=FoodItemStatus.AVAILABLE)

    assert [item.name for item in available] == ["Mine available"]


async def test_count_by_role_grouped_aggregates_in_sql(
    db_session: Any,
    make_user: Any,
) -> None:
    """The GROUP BY happens in the database, not in Python.

    Fetching every user to count them works at a hundred rows and fails at a million.
    """
    from app.modules.users.models import Role

    await make_user(role=Role.DONOR)
    await make_user(role=Role.DONOR)
    await make_user(role=Role.ADMIN)

    counts = await UserRepository(db_session).count_by_role_grouped()

    assert counts["DONOR"] == 2
    assert counts["ADMIN"] == 1
