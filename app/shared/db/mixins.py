"""Reusable column groups for entities.

WHY MIXINS
----------
Four entities all need `id`, `created_at`, `updated_at`. Writing those columns
four times means four chances to define them slightly differently — and the
first time someone writes `DateTime()` instead of `DateTime(timezone=True)`,
you have a real bug in production data.

Mixins are composition, not inheritance-for-reuse: `class User(Base, UUIDMixin,
TimestampMixin)` reads as a *declaration of properties*. Pick only what an
entity needs — `Notification` gets timestamps but no soft delete, because a
read notification should genuinely disappear.

WHY `mapped_column` LIVES IN A `declared_attr`-FREE MIXIN
--------------------------------------------------------
SQLAlchemy 2.0 supports plain annotated attributes on mixins directly, as long
as the mixin has no table of its own. Simpler than the old `@declared_attr`
dance — KISS.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.utils.datetime_utils import utcnow as _utcnow


class UUIDMixin:
    """A UUID v4 primary key.

    WHY UUID INSTEAD OF AUTO-INCREMENT INTEGER:

    * **No enumeration.** `GET /users/1`, `/2`, `/3` walks your entire user
      table. `GET /users/9f8c...` does not. This is an IDOR mitigation you get
      for free.
    * **No information leak.** Sequential ids tell competitors your growth rate.
    * **Client-side generation.** The id exists before the INSERT, so a service
      can build related objects in one flush without a round-trip.
    * **Merge-friendly.** Two databases (or a shard split) never collide.

    Cost: 16 bytes vs 8, and index locality is worse than a sequence. For an app
    of this shape that trade is obviously worth it. If you were writing a
    high-throughput append-only event table, prefer UUIDv7 or a bigint.

    `default=uuid.uuid4` is a *Python-side* default, so `entity.id` is populated
    immediately on construction. `server_default` is also set so raw SQL inserts
    (and data migrations) get an id too — belt and braces.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
        sort_order=-100,  # keep `id` the first column in CREATE TABLE
    )


class TimestampMixin:
    """`created_at` / `updated_at`.

    THE TWO COLUMNS USE DIFFERENT CLOCKS, AND THAT IS DELIBERATE.

    `created_at` — `server_default=func.now()`, i.e. **the database clock**:
      * one source of truth. Application servers drift, and with several replicas you
        get rows whose `created_at` ordering contradicts their real insert order;
      * it still works for rows written by a migration or by hand in psql.
      On INSERT, SQLAlchemy fetches server defaults eagerly (via RETURNING), so the
      value is available immediately with no extra query.

    `updated_at` — `server_default` for the initial value, but a **Python-side
    `onupdate`** for subsequent writes. The reason is specific to async SQLAlchemy:

      A server-side `onupdate=func.now()` means SQLAlchemy does not know the new value
      after an UPDATE, so it marks the attribute *expired*. The next attribute read
      then issues a lazy SELECT — and in async code that raises
      `MissingGreenlet: greenlet_spawn has not been called`, because the read happens
      outside an await-able context (typically while Pydantic is serialising the
      response). Avoiding it would require `await session.refresh(entity)` after every
      single update: an extra round-trip per write, forever, to populate one timestamp.

      A Python-side callable puts the value directly into the UPDATE statement, so
      SQLAlchemy knows it and nothing expires. The cost is that `updated_at` uses the
      application clock while `created_at` uses the database clock — a sub-second
      discrepancy that no consumer of this data cares about.

      This is a real trade, not an oversight, and it is the kind of thing that is only
      discovered by a test that reads an attribute after an update. See
      `tests/modules/users/test_api.py::test_admin_can_change_a_role`.

    NOTE: `onupdate` is ORM-level, so a bulk `update()` that bypasses the ORM skips it.
    `NotificationRepository.mark_all_read` and `FoodItemRepository.mark_expired_batch`
    therefore set `updated_at` explicitly. A database trigger would be airtight; it is
    not worth the operational complexity here (KISS).
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        sort_order=100,  # audit columns last
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        # Python-side, for the async reason explained above. `_utcnow` rather than
        # `datetime.now` so there is still exactly one source of "now" in the codebase.
        onupdate=lambda: _utcnow(),
        nullable=False,
        sort_order=101,
    )


class SoftDeleteMixin:
    """Logical deletion: mark the row, keep the data.

    WHY SOFT DELETE FOR FOOD ITEMS:
    A donated food item is referenced by a `Donation` row. Hard-deleting it
    would either break the FK or cascade the donation away, destroying the
    audit trail of a completed handover. Soft delete keeps history intact and
    makes "undo" trivial.

    WHY NOT EVERYWHERE:
    Soft delete is a tax — *every* query must remember `.where(is_deleted ==
    False)`, and one forgotten filter leaks deleted rows to users. We pay it
    only where history matters. `Notification` is hard-deleted.

    `BaseRepository` applies the filter automatically for models that have this
    mixin, which is what keeps the tax from being paid by hand at every call
    site (see shared/repository/base_repository.py).

    `deleted_at` alongside the boolean is not redundant: the flag is what indexes
    and filters cheaply, the timestamp is what answers "when, and by which
    release?" during an incident.
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
        index=True,
        sort_order=102,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        sort_order=103,
    )

    def mark_deleted(self, at: datetime) -> None:
        """Set both fields together so they cannot drift apart.

        Takes `at` as a parameter rather than calling `utcnow()` itself: an
        entity that reads the clock is untestable without freezing time. The
        caller (a service) supplies it.
        """
        self.is_deleted = True
        self.deleted_at = at

    def restore(self) -> None:
        self.is_deleted = False
        self.deleted_at = None
