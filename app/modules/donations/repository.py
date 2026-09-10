"""Data access for `Donation`.

The interesting method is `list_for_recipient_with_items`, which is where the N+1
query problem is solved explicitly rather than accidentally.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.modules.donations.models import Donation, DonationStatus
from app.modules.food_items.models import FoodItem
from app.shared.repository.base_repository import BaseRepository

# Statuses that mean "this request is still live". Defined once because three
# different queries need the same definition, and a duplicate that drifted would
# make the duplicate-request check disagree with the listing.
OPEN_STATUSES = (DonationStatus.PENDING, DonationStatus.ACCEPTED)


class DonationRepository(BaseRepository[Donation, uuid.UUID]):
    """`BaseRepository[Donation, UUID]` plus donation-specific queries.

    NOTE ON MODULE BOUNDARIES: this file imports `FoodItem` — a *model* from
    another module — in order to JOIN. That is allowed and necessary; a relational
    query across two tables cannot avoid naming both.

    What is forbidden is importing another module's **repository or service** here.
    The rule is about *behaviour*, not about tables: sharing a schema is what a
    single database means, while sharing logic is what creates coupling. See
    app/modules/README.md.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Donation, session)

    async def get_with_item(self, donation_id: uuid.UUID) -> Donation | None:
        """Load a donation with its food item eagerly.

        `selectinload` is required, not optional: `Donation.food_item` is declared
        `lazy="raise"`, so touching it without this raises immediately. That is the
        design working as intended — the alternative default (`lazy="select"`) would
        emit a hidden query, which in async SQLAlchemy means a `MissingGreenlet`
        error at serialisation time and in sync code means a silent N+1.
        """
        stmt = (
            self._base_select()
            .where(Donation.id == donation_id)
            .options(selectinload(Donation.food_item))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_recipient_with_items(
        self,
        recipient_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
        status: DonationStatus | None = None,
    ) -> Sequence[Donation]:
        """ "My requests", each with its food item, in **two** queries total.

        THE N+1 PROBLEM, CONCRETELY. Without eager loading, rendering 20 donations
        with their item names costs:

            1 query   SELECT * FROM donations WHERE recipient_id = ...
            20 queries SELECT * FROM food_items WHERE id = ...   (one per row)

        21 round-trips, growing linearly with page size. `selectinload` issues
        instead:

            1 query   SELECT * FROM donations WHERE recipient_id = ...
            1 query   SELECT * FROM food_items WHERE id IN (...)

        Two queries, constant in page size.

        WHY `selectinload` AND NOT `joinedload`: `joinedload` emits a single LEFT
        JOIN, which duplicates the parent row for every child. For a many-to-one
        like this it is roughly equivalent, but `selectinload` keeps the pattern
        uniform with collection relationships (where the JOIN's row multiplication
        breaks `LIMIT`) — so the same idiom is always correct here.
        """
        filters: list[ColumnElement[bool]] = [Donation.recipient_id == recipient_id]
        if status is not None:
            filters.append(Donation.status == status)

        stmt = (
            self._base_select()
            .where(*filters)
            .options(selectinload(Donation.food_item))
            .order_by(Donation.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_for_recipient(
        self,
        recipient_id: uuid.UUID,
        *,
        status: DonationStatus | None = None,
    ) -> int:
        filters: list[ColumnElement[bool]] = [Donation.recipient_id == recipient_id]
        if status is not None:
            filters.append(Donation.status == status)
        return await self.count(filters=filters)

    async def list_for_donor(
        self,
        donor_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
        status: DonationStatus | None = None,
    ) -> Sequence[Donation]:
        """Requests for food *I* listed — the donor's inbox.

        Requires a JOIN: "who owns the item" lives on `food_items`, not on
        `donations`. Denormalising `owner_id` onto `donations` would avoid the join
        at the cost of a value that can drift out of sync with the item it copies.
        A join on an indexed foreign key is cheap; a second source of truth is not.
        """
        filters: list[ColumnElement[bool]] = [FoodItem.owner_id == donor_id]
        if status is not None:
            filters.append(Donation.status == status)

        stmt = (
            select(Donation)
            .join(FoodItem, Donation.food_item_id == FoodItem.id)
            .where(*filters)
            .options(selectinload(Donation.food_item))
            .order_by(Donation.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_for_donor(
        self,
        donor_id: uuid.UUID,
        *,
        status: DonationStatus | None = None,
    ) -> int:
        from sqlalchemy import func

        filters: list[ColumnElement[bool]] = [FoodItem.owner_id == donor_id]
        if status is not None:
            filters.append(Donation.status == status)

        stmt = (
            select(func.count())
            .select_from(Donation)
            .join(FoodItem, Donation.food_item_id == FoodItem.id)
            .where(*filters)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def has_open_request(self, recipient_id: uuid.UUID, food_item_id: uuid.UUID) -> bool:
        """True if this recipient already has a live request for this item.

        Backs `DuplicateDonationRequestError`. Uses `OPEN_STATUSES`, so a
        *cancelled* request does not block a genuine second attempt — a recipient
        who withdrew by mistake must be able to ask again.
        """
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(Donation)
            .where(
                Donation.recipient_id == recipient_id,
                Donation.food_item_id == food_item_id,
                Donation.status.in_(OPEN_STATUSES),
            )
            .limit(1)
        )
        return bool((await self.session.execute(stmt)).scalar_one())
