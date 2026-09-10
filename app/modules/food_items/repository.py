"""Data access for `FoodItem`.

Every method here is a query with no decisions in it. The interesting part is
`get_for_update`, which is where a real concurrency bug is prevented — see its
docstring.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.modules.food_items.models import FoodItem, FoodItemStatus
from app.shared.repository.base_repository import BaseRepository


class FoodItemRepository(BaseRepository[FoodItem, uuid.UUID]):
    """`BaseRepository[FoodItem, UUID]` plus listing-specific queries.

    Inherited from the base: `get`, `list`, `count`, `add`, `update`, `delete`
    (soft, automatically, because `FoodItem` carries `SoftDeleteMixin`) — and every
    read here excludes soft-deleted rows without a single explicit filter, because
    `_base_select()` applies it. That is the DRY payoff described in
    shared/repository/README.md.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(FoodItem, session)

    async def get_for_update(self, item_id: uuid.UUID) -> FoodItem | None:
        """Load an item and hold a row lock until the transaction ends.

        WHY THIS EXISTS — A REAL RACE CONDITION
        Two recipients request the same item at the same instant:

            T1: SELECT ... status = 'AVAILABLE'   ✓
            T2: SELECT ... status = 'AVAILABLE'   ✓   <- both passed the check
            T1: UPDATE status = 'RESERVED'
            T2: UPDATE status = 'RESERVED'            <- one loaf, two donations

        Read-then-write with a check in between is not atomic by default. Under
        Postgres's default READ COMMITTED isolation, T2's SELECT genuinely sees
        AVAILABLE, so the application-level check cannot catch this.

        `SELECT ... FOR UPDATE` makes T2's read *block* until T1 commits, after
        which T2 re-reads and sees RESERVED, and the business rule correctly
        rejects it. The lock is released at commit — which the request-scoped Unit
        of Work guarantees happens (shared/db/session.py).

        `FoodItemService.reserve` therefore uses this, not `get`. That is why
        reservation is safe and why the ordinary read path stays lock-free.

        Alternatives considered: SERIALIZABLE isolation (correct, but makes every
        transaction retryable, which is a much larger change) and an optimistic
        version column (also correct, needs retry logic at the API layer).
        Pessimistic locking on one narrow path is the KISS choice here.
        """
        stmt = (
            self._base_select()
            .where(FoodItem.id == item_id)
            # `with_for_update()` -> `SELECT ... FOR UPDATE`
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_available(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        now: datetime,
        search: str | None = None,
    ) -> Sequence[FoodItem]:
        """The public browse query: available, unexpired listings.

        `now` is a parameter rather than read from the clock here. A repository
        that called `utcnow()` would be untestable without freezing global time,
        and a single request could read the clock twice and page inconsistently.

        Ordered by `expires_at` ascending — the food that needs rescuing soonest
        appears first. That is a product decision, and it is also exactly what the
        partial index `ix_food_items_available_expires` is built to serve, so the
        hottest read in the application is an index scan.
        """
        filters = self._available_filters(now)
        if search:
            filters.append(FoodItem.name.ilike(f"%{self._escape_like(search)}%"))

        return await self.list(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by=[FoodItem.expires_at.asc()],
        )

    async def count_available(self, *, now: datetime, search: str | None = None) -> int:
        filters = self._available_filters(now)
        if search:
            filters.append(FoodItem.name.ilike(f"%{self._escape_like(search)}%"))
        return await self.count(filters=filters)

    async def list_by_owner(
        self,
        owner_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
        status: FoodItemStatus | None = None,
    ) -> Sequence[FoodItem]:
        """ "My listings", optionally filtered by status.

        Served by the composite index `(owner_id, status)`. Because `owner_id` is
        leftmost, the same index also serves the unfiltered case — which is why the
        column order in models.py is not arbitrary.
        """
        filters: list[ColumnElement[bool]] = [FoodItem.owner_id == owner_id]
        if status is not None:
            filters.append(FoodItem.status == status)
        return await self.list(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by=[FoodItem.created_at.desc()],
        )

    async def count_by_owner(
        self,
        owner_id: uuid.UUID,
        *,
        status: FoodItemStatus | None = None,
    ) -> int:
        filters: list[ColumnElement[bool]] = [FoodItem.owner_id == owner_id]
        if status is not None:
            filters.append(FoodItem.status == status)
        return await self.count(filters=filters)

    async def list_expiring_before(
        self,
        moment: datetime,
        *,
        limit: int = 500,
    ) -> Sequence[FoodItem]:
        """Items still AVAILABLE but expiring before `moment`.

        Drives the expiry-reminder notification. `limit` is not optional: a sweep
        that loaded an unbounded result set would be a self-inflicted outage the
        first time the table got large.
        """
        return await self.list(
            offset=0,
            limit=limit,
            filters=[
                FoodItem.status == FoodItemStatus.AVAILABLE,
                FoodItem.expires_at <= moment,
            ],
            order_by=[FoodItem.expires_at.asc()],
        )

    async def mark_expired_batch(self, now: datetime) -> int:
        """Flip every past-date AVAILABLE/RESERVED item to EXPIRED. Returns the count.

        A single bulk `UPDATE ... WHERE`, not a load-modify-save loop. At a
        thousand stale rows that is one statement instead of two thousand, and it
        holds locks for milliseconds rather than seconds.

        THE TRADE-OFF, STATED HONESTLY: bulk update bypasses the ORM, so the
        `onupdate=func.now()` on `updated_at` does not fire — which is why it is
        set explicitly below. It also bypasses the state-machine check in the
        service. That is acceptable *only* because expiry is time-driven rather
        than actor-driven: it is not a transition anyone requests, and the target
        state is terminal. Any transition a user can trigger goes through the
        service. This asymmetry is intentional and worth understanding before
        copying the pattern.
        """
        from sqlalchemy import update

        stmt = (
            update(FoodItem)
            .where(
                FoodItem.expires_at <= now,
                FoodItem.status.in_([FoodItemStatus.AVAILABLE, FoodItemStatus.RESERVED]),
                FoodItem.is_deleted.is_(False),
            )
            .values(status=FoodItemStatus.EXPIRED, updated_at=now)
        )
        # `execute()` is typed as returning `Result`, but a DML statement always yields a
        # `CursorResult` — the class that actually carries `rowcount`. Narrowing is more
        # honest than a blanket `type: ignore`.
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return result.rowcount

    async def sum_donated_quantity(self, unit: str) -> float:
        """Total quantity donated, per unit — the headline impact metric.

        Aggregated by the database. Fetching every donated row to sum in Python
        works at a hundred items and falls over at a million; the SQL version is
        constant memory either way.

        `coalesce(..., 0)` because `SUM` over zero rows returns NULL, and a metrics
        endpoint returning `null` instead of `0` breaks dashboards.
        """
        stmt = select(func.coalesce(func.sum(FoodItem.quantity), 0)).where(
            FoodItem.status == FoodItemStatus.DONATED,
            FoodItem.unit == unit,
            FoodItem.is_deleted.is_(False),
        )
        return float((await self.session.execute(stmt)).scalar_one())

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _available_filters(now: datetime) -> list[ColumnElement[bool]]:
        """The definition of "available", in one place.

        Used by both `list_available` and `count_available`. If the two built their
        filters independently, the `total` in a paginated response would eventually
        disagree with the items in it — a bug that looks like a pagination fault
        and is actually a copy-paste fault.
        """
        return [
            FoodItem.status == FoodItemStatus.AVAILABLE,
            FoodItem.expires_at > now,
        ]

    @staticmethod
    def _escape_like(term: str) -> str:
        """Escape LIKE wildcards in user input.

        Not an SQL-injection defence — SQLAlchemy parameterises the value, so
        injection is already impossible. This is a *correctness and performance*
        fix: a search for `%` would otherwise match every row, and `_` would match
        any single character, so a user searching for "100_g" gets nonsense
        results and the database does needless work.
        """
        return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
