"""Data access for `Notification`."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.modules.notifications.models import Notification
from app.shared.repository.base_repository import BaseRepository


class NotificationRepository(BaseRepository[Notification, uuid.UUID]):
    """`BaseRepository[Notification, UUID]` plus inbox queries."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Notification, session)

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
        unread_only: bool = False,
    ) -> Sequence[Notification]:
        """The user's inbox, newest first.

        Every query in this class filters on `user_id`, and that is the module's
        core security property: there is no method that can return another user's
        notifications, so an endpoint physically cannot leak one. Scoping at the
        repository is stronger than checking in the service, because it removes the
        unsafe query from existence rather than guarding its use.
        """
        filters: list[ColumnElement[bool]] = [Notification.user_id == user_id]
        if unread_only:
            filters.append(Notification.read_at.is_(None))

        return await self.list(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by=[Notification.created_at.desc()],
        )

    async def count_for_user(self, user_id: uuid.UUID, *, unread_only: bool = False) -> int:
        filters: list[ColumnElement[bool]] = [Notification.user_id == user_id]
        if unread_only:
            filters.append(Notification.read_at.is_(None))
        return await self.count(filters=filters)

    async def count_unread(self, user_id: uuid.UUID) -> int:
        """The badge count. Served by the partial index
        `ix_notifications_user_unread`, so it stays fast no matter how much read
        history accumulates."""
        stmt = select(func.count()).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def mark_all_read(self, user_id: uuid.UUID, *, at: datetime) -> int:
        """Mark every unread notification read. Returns how many changed.

        A single bulk UPDATE rather than a load-modify-save loop: "mark all read"
        on a 500-item backlog is one statement instead of a thousand.

        `read_at IS NULL` in the WHERE clause is not just an optimisation — it makes
        the operation idempotent and preserves the original read time of anything
        already read, so a double-click cannot rewrite history.

        `at` is passed in rather than read from the clock here, keeping the
        repository free of hidden time dependencies (see
        shared/utils/datetime_utils.py).
        """
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=at, updated_at=at)
        )
        # `execute()` is typed as returning `Result`, but a DML statement always yields a
        # `CursorResult` — the class that actually carries `rowcount`. Narrowing is more
        # honest than a blanket `type: ignore`.
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        await self.session.flush()
        return result.rowcount

    async def delete_read_older_than(self, cutoff: datetime) -> int:
        """Retention cleanup: hard-delete read notifications older than `cutoff`.

        Notifications are the fastest-growing table in an app like this and the
        least valuable to keep. A retention policy is a design requirement, not an
        afterthought — an unbounded append-only table is a slow-motion outage.

        Deletes only *read* rows, so an inactive user's backlog is never silently
        discarded.
        """
        from sqlalchemy import delete

        stmt = delete(Notification).where(
            Notification.read_at.is_not(None),
            Notification.created_at < cutoff,
        )
        # `execute()` is typed as returning `Result`, but a DML statement always yields a
        # `CursorResult` — the class that actually carries `rowcount`. Narrowing is more
        # honest than a blanket `type: ignore`.
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return result.rowcount
