"""FoodItem entity: a listing of surplus food.

THE STATUS FIELD IS A STATE MACHINE, AND THAT MATTERS
----------------------------------------------------
`AVAILABLE -> RESERVED -> DONATED` (with `CANCELLED`/`EXPIRED` as terminal exits).
The *allowed transitions* are declared here, next to the states, as
`ALLOWED_TRANSITIONS`. The service enforces them.

Why declare them as data rather than writing `if current == ... and new == ...`
in the service:

* The whole lifecycle is legible in eight lines. A reader does not have to
  reconstruct it from scattered conditionals.
* Adding a state is one dict entry, not an audit of every branch (Open/Closed).
* The service's `_assert_transition` is four lines and cannot disagree with the
  table, because the table is the only definition.

WHY `EXPIRED` IS A STATUS AND NOT JUST `expires_at < now()`
----------------------------------------------------------
Both exist, deliberately. `expires_at` is the truth — it is what the business
rule checks, so an item that passed its date one second ago cannot be donated even
though no job has run. `EXPIRED` is a materialised marker for cheap filtering and
reporting. The service treats `expires_at` as authoritative, so a stale `status`
can never let expired food through.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import Base, enum_column
from app.shared.db.mixins import SoftDeleteMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.modules.donations.models import Donation
    from app.modules.users.models import User


class FoodItemStatus(StrEnum):
    AVAILABLE = "AVAILABLE"  # listed, can be requested
    RESERVED = "RESERVED"  # a donation request is pending
    DONATED = "DONATED"  # handed over — terminal
    CANCELLED = "CANCELLED"  # withdrawn by the owner — terminal
    EXPIRED = "EXPIRED"  # past its date — terminal


# The state machine, as data. Empty frozenset = terminal state.
ALLOWED_TRANSITIONS: Final[dict[FoodItemStatus, frozenset[FoodItemStatus]]] = {
    FoodItemStatus.AVAILABLE: frozenset(
        {FoodItemStatus.RESERVED, FoodItemStatus.CANCELLED, FoodItemStatus.EXPIRED}
    ),
    # RESERVED can return to AVAILABLE: that is what happens when a recipient
    # cancels their request, and the food must become offerable again.
    FoodItemStatus.RESERVED: frozenset(
        {FoodItemStatus.DONATED, FoodItemStatus.AVAILABLE, FoodItemStatus.EXPIRED}
    ),
    FoodItemStatus.DONATED: frozenset(),
    FoodItemStatus.CANCELLED: frozenset(),
    FoodItemStatus.EXPIRED: frozenset(),
}


class FoodUnit(StrEnum):
    """Units of measure. A closed set, not free text.

    Free-text units produce "kg", "Kg", "kilos", "kilogram" in one column, and
    then no aggregation ("how much food did we save?") is possible without a
    cleanup script. Constraining the input is cheaper than cleaning the output.
    """

    KILOGRAM = "KG"
    GRAM = "G"
    LITRE = "L"
    PIECE = "PIECE"
    PORTION = "PORTION"


class FoodItem(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """A donatable food listing.

    Uses `SoftDeleteMixin` (unlike `User`): a `Donation` row references this item,
    and hard-deleting it would either violate the foreign key or cascade away the
    record of a completed handover. Soft delete keeps the audit trail intact.
    """

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(
        # Explicit `String(1000)` overrides the project-wide 255 default from
        # `type_annotation_map` (shared/db/base.py).
        String(1000),
        nullable=True,
    )

    quantity: Mapped[float] = mapped_column(
        # NUMERIC, never FLOAT. Binary floating point cannot represent 0.1
        # exactly, so summing float quantities accumulates error — and "total food
        # rescued" is a number this project reports. NUMERIC is exact decimal
        # arithmetic; that it is slower is irrelevant at this scale.
        Numeric(10, 3),
        nullable=False,
    )

    unit: Mapped[FoodUnit] = mapped_column(enum_column(FoodUnit, length=16), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        nullable=False,
        index=True,  # supports the expiry sweep and "expiring soon" queries
        comment="Timezone-aware. The authoritative expiry check; status is a cache of it.",
    )

    status: Mapped[FoodItemStatus] = mapped_column(
        enum_column(FoodItemStatus, length=16),
        nullable=False,
        default=FoodItemStatus.AVAILABLE,
        server_default=text(f"'{FoodItemStatus.AVAILABLE.value}'"),
    )

    pickup_location: Mapped[str] = mapped_column(String(500), nullable=False)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        # RESTRICT, not CASCADE: deleting a user must not silently erase the food
        # they donated. In practice users are deactivated rather than deleted, so
        # this constraint is a tripwire that catches an accidental hard delete.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # --- Relationships ----------------------------------------------------
    # `lazy="raise"` on both: forgetting to eager-load fails loudly instead of
    # emitting a hidden N+1 query (or `MissingGreenlet` in async). See
    # app/modules/users/models.py for the full reasoning.
    owner: Mapped[User] = relationship(back_populates="food_items", lazy="raise")
    donations: Mapped[list[Donation]] = relationship(
        back_populates="food_item",
        lazy="raise",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s.value) for s in FoodItemStatus)})",
            name="status_valid",
        ),
        CheckConstraint(
            f"unit IN ({', '.join(repr(u.value) for u in FoodUnit)})",
            name="unit_valid",
        ),
        # Composite, and column order is the point: (owner_id, status) also serves
        # a query filtering on owner_id alone (leftmost-prefix rule), whereas
        # (status, owner_id) would not. "My items, by status" is the most common
        # query in the application, so the index is built for it.
        Index("ix_food_items_owner_id_status", "owner_id", "status"),
        # Partial index for the public browse endpoint — the hottest read path.
        # It indexes only the rows that endpoint can return, so it stays small
        # even as DONATED items accumulate indefinitely.
        Index(
            "ix_food_items_available_expires",
            "expires_at",
            postgresql_where=text(
                f"status = '{FoodItemStatus.AVAILABLE.value}' AND is_deleted = false"
            ),
        ),
    )

    def can_transition_to(self, new_status: FoodItemStatus) -> bool:
        """Consult the transition table. Pure — no I/O, no clock, so it is safe
        on the entity and trivially testable."""
        return new_status in ALLOWED_TRANSITIONS[self.status]

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self.status]
