"""Notification entity.

WHY NOTIFICATIONS ARE PERSISTED AND NOT JUST PUSHED
---------------------------------------------------
A fire-and-forget push (email, websocket, mobile push) that fails is gone. The
user is never told their donation was accepted, and there is no record that we
tried. Writing the row first makes the notification durable: the in-app list is
always correct, external delivery becomes a best-effort *additional* channel, and
a failed send can be retried because the intent still exists.

This is the same reasoning as the transactional-outbox pattern, in its simplest
form. Full outbox semantics (a `delivered_at` column, a worker that polls
undelivered rows, at-least-once delivery with idempotency keys) is the natural
next step; not built here because there is no external channel yet, and building
the machinery before the requirement is the opposite of KISS. Where it would go is
noted in `tasks.py`.

WHY `read_at: datetime | None` AND NOT `is_read: bool`
-----------------------------------------------------
A nullable timestamp carries strictly more information at the same storage cost:
it answers both "is it read?" (`read_at IS NULL`) and "when?". The boolean can
never be upgraded without a migration and a backfill that has no data to fill from.
Prefer nullable timestamps over booleans for anything event-shaped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import Base, enum_column
from app.shared.db.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.modules.users.models import User


class NotificationType(StrEnum):
    """What happened.

    A closed set rather than free text, so clients can switch on the type to pick
    an icon, a deep link, or a translation — none of which is possible if `type` is
    an arbitrary string. It also keeps analytics ("which notifications get read?")
    answerable with a GROUP BY.
    """

    DONATION_REQUESTED = "DONATION_REQUESTED"
    DONATION_ACCEPTED = "DONATION_ACCEPTED"
    DONATION_DECLINED = "DONATION_DECLINED"
    DONATION_COMPLETED = "DONATION_COMPLETED"
    DONATION_CANCELLED = "DONATION_CANCELLED"
    FOOD_EXPIRING_SOON = "FOOD_EXPIRING_SOON"


class Notification(Base, UUIDMixin, TimestampMixin):
    """An in-app message for one user.

    No `SoftDeleteMixin`: a dismissed notification should genuinely disappear.
    Keeping tombstones for transient messages would grow the table without ever
    answering a question anyone asks.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        # CASCADE here, unlike `food_items` and `donations` (which use RESTRICT).
        # A notification is meaningless without its recipient and references no
        # third party, so deleting the user should take it. Matched by
        # `cascade="all, delete-orphan"` on the ORM side in users/models.py, so
        # both the database and the ORM agree.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    type: Mapped[NotificationType] = mapped_column(
        enum_column(NotificationType, length=32), nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)

    # Nullable FK: `FOOD_EXPIRING_SOON` has no donation. Kept as a plain column
    # with no ForeignKey constraint on purpose — see below.
    related_donation_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        comment=(
            "Deep-link target. Intentionally NOT a foreign key: a notification is a "
            "historical record, and it must survive the donation it refers to being "
            "purged. A real FK would force either a cascade (destroying history) or a "
            "RESTRICT (blocking cleanup)."
        ),
    )

    read_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="NULL = unread. See the module docstring on why this is not a boolean.",
    )

    user: Mapped[User] = relationship(back_populates="notifications", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            f"type IN ({', '.join(repr(t.value) for t in NotificationType)})",
            name="type_valid",
        ),
        # Partial index on unread rows only. The unread badge is polled constantly
        # while read notifications accumulate forever, so indexing only the unread
        # ones keeps the index permanently small — it is proportional to the backlog,
        # not to history. This is the highest-value index in the schema.
        Index(
            "ix_notifications_user_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        # Serves the paginated notification list.
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
    )

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
