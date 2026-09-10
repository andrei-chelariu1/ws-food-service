"""Pagination request parameters.

WHY THIS IS A DEPENDENCY AND NOT TWO QUERY PARAMETERS PER ENDPOINT
------------------------------------------------------------------
Written inline, every list endpoint repeats:

    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)

...and then computes `offset = (page - 1) * size` by hand. Eight endpoints, eight
chances to write `page * size` (which silently skips the first page) or to forget
`le=100` (which lets a client request `size=1000000` and OOM the process).

As one injectable object the bounds are declared once, the offset is computed
once, and `Depends(PageParams)` is three words at each call site.

THE `MAX_PAGE_SIZE` CAP IS A SECURITY CONTROL
--------------------------------------------
Not an aesthetic limit. Unbounded `size` is a trivial denial-of-service: one
request that materialises a million ORM objects. The cap is enforced by Pydantic
at the edge, so it cannot be bypassed by any downstream code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Query

DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class PageParams:
    """Validated pagination input, exposed as `page`/`size` and consumed as
    `offset`/`limit`.

    The API speaks pages because that is what a UI paginator needs. The database
    speaks offset/limit. Translating in exactly one place means no repository
    ever sees a `page` number and no router ever computes an offset.

    A plain dataclass (not a Pydantic model) because FastAPI can already build it
    from the annotated `Query` defaults, and nothing here needs serialising.
    """

    page: Annotated[int, Query(ge=1, description="Page number, 1-based")] = 1
    size: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"Items per page (max {MAX_PAGE_SIZE})",
        ),
    ] = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        """Rows to skip. `(page - 1) * size` — the off-by-one lives here only."""
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


# NOTE ON SCALE: offset pagination degrades on large tables — the database must
# walk and discard `offset` rows, so page 10,000 is slow, and a concurrent insert
# shifts rows between pages (an item can be seen twice or missed). It is the
# right default because it is what UIs need and what clients expect.
#
# When a table outgrows it, switch that specific endpoint to keyset (cursor)
# pagination: `WHERE (created_at, id) < (:last_created_at, :last_id) ORDER BY
# created_at DESC, id DESC LIMIT :size`. Constant time, stable under concurrent
# writes, at the cost of losing random page access. Documented here rather than
# implemented, because building it before it is needed is the opposite of KISS.
