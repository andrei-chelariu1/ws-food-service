"""Donation entity: the link between a food item and its recipient.

A second state machine, same pattern as `FoodItem` — transitions declared as data,
enforced in one service method.

    PENDING ──accept──> ACCEPTED ──complete──> COMPLETED
       │                    │
       └────decline/cancel──┴──> CANCELLED

The two state machines are coupled but not merged, and that separation is the
point: `Donation.status` records what the *parties* agreed, `FoodItem.status`
records where the *food* is. Accepting a donation advances both, which is why the
operation must be transactional (one request, one commit — see
shared/db/session.py).

WHY THERE IS NO UNIQUE CONSTRAINT ON (food_item_id, status='PENDING')
--------------------------------------------------------------------
It looks like the obvious way to stop two people reserving the same loaf. It is
not: a partial unique index would produce an `IntegrityError` at commit, far from
the code that caused it, with a message clients cannot act on. The real protection
is `SELECT ... FOR UPDATE` in `FoodItemService.reserve`, which serialises the two
requests and lets the second receive a proper 409. The index would also permit a
second PENDING donation after the first is declined, which is correct behaviour —
so it cannot be the guard anyway.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import Base, enum_column
from app.shared.db.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.modules.food_items.models import FoodItem
    from app.modules.users.models import User


class DonationStatus(StrEnum):
    PENDING = "PENDING"  # recipient asked, owner has not answered
    ACCEPTED = "ACCEPTED"  # owner agreed, handover not yet done
    COMPLETED = "COMPLETED"  # food handed over — terminal
    CANCELLED = "CANCELLED"  # declined by owner or withdrawn by recipient — terminal


ALLOWED_TRANSITIONS: Final[dict[DonationStatus, frozenset[DonationStatus]]] = {
    DonationStatus.PENDING: frozenset({DonationStatus.ACCEPTED, DonationStatus.CANCELLED}),
    DonationStatus.ACCEPTED: frozenset({DonationStatus.COMPLETED, DonationStatus.CANCELLED}),
    DonationStatus.COMPLETED: frozenset(),
    DonationStatus.CANCELLED: frozenset(),
}


class Donation(Base, UUIDMixin, TimestampMixin):
    """A request to receive a specific food item.

    No `SoftDeleteMixin`: a donation is an immutable record of what happened
    between two people. It is cancelled, never deleted — and `CANCELLED` carries
    more information than a `deleted_at` timestamp ever could.
    """

    food_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("food_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[DonationStatus] = mapped_column(
        enum_column(DonationStatus, length=16),
        nullable=False,
        default=DonationStatus.PENDING,
        server_default=text(f"'{DonationStatus.PENDING.value}'"),
    )

    note: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Message from the recipient to the donor",
    )

    decline_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Set when the owner declines; shown to the recipient",
    )

    # --- Relationships ----------------------------------------------------
    # `lazy="raise"` again. `DonationService` must therefore eager-load explicitly
    # when it needs the item — which is honest, because it is the difference
    # between one query and one per row.
    food_item: Mapped[FoodItem] = relationship(back_populates="donations", lazy="raise")
    recipient: Mapped[User] = relationship(back_populates="donations", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s.value) for s in DonationStatus)})",
            name="status_valid",
        ),
        # Serves "my requests, filtered by status" — the recipient's main screen.
        # `recipient_id` leftmost so the same index also covers the unfiltered case.
        Index("ix_donations_recipient_id_status", "recipient_id", "status"),
        # Serves "pending requests for my food" — the donor's main screen. Different
        # access path, so it needs its own index; one composite index cannot serve
        # both because neither column can be leftmost in both queries.
        Index("ix_donations_food_item_id_status", "food_item_id", "status"),
    )

    def can_transition_to(self, new_status: DonationStatus) -> bool:
        return new_status in ALLOWED_TRANSITIONS[self.status]

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self.status]
