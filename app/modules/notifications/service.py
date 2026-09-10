"""Business logic for notifications.

TWO RESPONSIBILITIES, AND WHY THEY ARE SPLIT
-------------------------------------------
1. **The inbox** — list, count unread, mark read. Ordinary CRUD, authorized per
   user.
2. **Emitting notifications** — the `notify_*` methods that other modules call.

The `notify_*` methods are the module's *public contract to the rest of the app*.
`DonationService` calls `notify_donation_accepted(...)`, not
`create(user_id, type, title, body)`. That difference matters:

* **The message text lives here.** All notification copy is in one file, so
  changing wording (or adding translations) touches one module. If `DonationService`
  composed the strings, copy would be scattered across every module that ever
  notifies anyone.
* **The signature documents the requirement.** `notify_donation_accepted` needs a
  recipient, an item name and a pickup location. A generic `create()` would accept
  any four strings, and the caller would be free to omit the pickup location — the
  one piece of information the recipient actually needs.
* **The type/title/body triple cannot drift.** Each `notify_*` builds all three
  together, so a `DONATION_ACCEPTED` notification can never carry a declined body.

This is the Interface Segregation Principle read from the caller's side: expose the
narrow, meaningful operations rather than one wide, generic one.

WHAT IS TRANSACTIONAL AND WHAT IS NOT
-------------------------------------
The notification **row** is written inside the caller's transaction — so if the
donation is rolled back, so is its notification, and the two can never disagree.
The **email** is dispatched via `TaskDispatcher` and is best-effort.

The important half is durable; the unreliable half is optional. That split is the
design, not a compromise.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.logging import get_logger
from app.modules.notifications.exceptions import (
    NotificationAccessDeniedError,
    NotificationNotFoundError,
)
from app.modules.notifications.models import Notification, NotificationType
from app.modules.notifications.repository import NotificationRepository
from app.modules.notifications.tasks import TaskDispatcher
from app.shared.security.principal import CurrentUser
from app.shared.utils.datetime_utils import utcnow

log = get_logger(__name__)


class NotificationService:
    """Inbox management plus the `notify_*` emission API."""

    def __init__(
        self,
        repository: NotificationRepository,
        dispatcher: TaskDispatcher,
    ) -> None:
        self._repo = repository
        # The Protocol, not a concrete dispatcher — see tasks.py. Injecting
        # `ImmediateTaskDispatcher` in tests makes this class fully testable with no
        # request, no event loop scheduling and no FastAPI.
        self._dispatcher = dispatcher

    # -- Inbox -------------------------------------------------------------

    async def list_for_user(
        self,
        user: CurrentUser,
        *,
        offset: int,
        limit: int,
        unread_only: bool = False,
    ) -> tuple[Sequence[Notification], int]:
        """The caller's own notifications.

        Scoped to `user.id` from the token. There is no endpoint and no repository
        method that takes a `user_id` from the client, so one user's inbox is not
        reachable from another's session — the unsafe query does not exist.
        """
        items = await self._repo.list_for_user(
            user.id, offset=offset, limit=limit, unread_only=unread_only
        )
        total = await self._repo.count_for_user(user.id, unread_only=unread_only)
        return items, total

    async def count_unread(self, user: CurrentUser) -> int:
        return await self._repo.count_unread(user.id)

    async def mark_read(self, notification_id: uuid.UUID, user: CurrentUser) -> Notification:
        """Mark one notification read.

        THE IDOR CHECK, and this is the resource that most needs it: notifications
        are numerous, id-addressable, and their bodies reveal who is receiving food
        from whom. Without the ownership check, iterating ids would dump the
        application's most sensitive data.

        Idempotent: already-read notifications keep their original `read_at`, so a
        double-click cannot rewrite when the user first saw it.
        """
        notification = await self._repo.get(notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)

        if notification.user_id != user.id:
            log.warning(
                "notification_access_denied",
                actor_id=str(user.id),
                notification_id=str(notification_id),
            )
            # Same error for "not yours" as the client would get for a genuinely
            # absent id — so this endpoint cannot be used to test which ids exist.
            raise NotificationAccessDeniedError

        if notification.read_at is None:
            await self._repo.update(notification, read_at=utcnow())
        return notification

    async def mark_all_read(self, user: CurrentUser) -> int:
        """Mark the caller's whole inbox read. Returns how many changed."""
        count = await self._repo.mark_all_read(user.id, at=utcnow())
        log.info("notifications_marked_read", user_id=str(user.id), count=count)
        return count

    async def dismiss(self, notification_id: uuid.UUID, user: CurrentUser) -> None:
        """Hard-delete one notification.

        A genuine delete, unlike food items: `Notification` has no
        `SoftDeleteMixin`, so `BaseRepository.delete` removes the row. Correct here —
        a dismissed transient message has no audit value, and keeping tombstones
        would grow the fastest-growing table in the schema for nothing.
        """
        notification = await self._repo.get(notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        if notification.user_id != user.id:
            raise NotificationAccessDeniedError

        await self._repo.delete(notification)

    # -- Emission API (called by other modules' services) ------------------

    async def notify_donation_requested(
        self,
        *,
        donor_id: uuid.UUID,
        recipient_name: str,
        food_item_name: str,
        donation_id: uuid.UUID,
    ) -> Notification:
        """Tell a donor that someone requested their food."""
        return await self._create(
            user_id=donor_id,
            notification_type=NotificationType.DONATION_REQUESTED,
            title="New donation request",
            body=f"{recipient_name} would like to receive '{food_item_name}'.",
            related_donation_id=donation_id,
        )

    async def notify_donation_accepted(
        self,
        *,
        recipient_id: uuid.UUID,
        food_item_name: str,
        pickup_location: str,
        donation_id: uuid.UUID,
    ) -> Notification:
        """Tell a recipient their request was accepted.

        `pickup_location` is a required argument because a notification saying
        "accepted!" without telling the recipient where to go is useless. The
        signature enforces that; a generic `create(title, body)` could not.
        """
        return await self._create(
            user_id=recipient_id,
            notification_type=NotificationType.DONATION_ACCEPTED,
            title="Your request was accepted",
            body=f"'{food_item_name}' is reserved for you. Collect it at: {pickup_location}",
            related_donation_id=donation_id,
        )

    async def notify_donation_declined(
        self,
        *,
        recipient_id: uuid.UUID,
        food_item_name: str,
        reason: str | None,
        donation_id: uuid.UUID,
    ) -> Notification:
        suffix = f" Reason: {reason}" if reason else ""
        return await self._create(
            user_id=recipient_id,
            notification_type=NotificationType.DONATION_DECLINED,
            title="Your request was declined",
            body=f"Your request for '{food_item_name}' was declined.{suffix}",
            related_donation_id=donation_id,
        )

    async def notify_donation_completed(
        self,
        *,
        recipient_id: uuid.UUID,
        food_item_name: str,
        donation_id: uuid.UUID,
    ) -> Notification:
        return await self._create(
            user_id=recipient_id,
            notification_type=NotificationType.DONATION_COMPLETED,
            title="Donation completed",
            body=f"'{food_item_name}' has been handed over. Thank you for reducing food waste!",
            related_donation_id=donation_id,
        )

    async def notify_donation_cancelled(
        self,
        *,
        donor_id: uuid.UUID,
        food_item_name: str,
        donation_id: uuid.UUID,
    ) -> Notification:
        return await self._create(
            user_id=donor_id,
            notification_type=NotificationType.DONATION_CANCELLED,
            title="Request withdrawn",
            body=f"A request for '{food_item_name}' was withdrawn. It is available again.",
            related_donation_id=donation_id,
        )

    async def notify_food_expiring_soon(
        self,
        *,
        owner_id: uuid.UUID,
        food_item_name: str,
        hours_remaining: int,
    ) -> Notification:
        return await self._create(
            user_id=owner_id,
            notification_type=NotificationType.FOOD_EXPIRING_SOON,
            title="Food expiring soon",
            body=(
                f"'{food_item_name}' expires in about {hours_remaining} hours. "
                "Consider lowering the quantity or cancelling the listing."
            ),
        )

    # -- Internals ---------------------------------------------------------

    async def _create(
        self,
        *,
        user_id: uuid.UUID,
        notification_type: NotificationType,
        title: str,
        body: str,
        related_donation_id: uuid.UUID | None = None,
    ) -> Notification:
        """Persist a notification and dispatch its external delivery.

        Private. Every `notify_*` funnels through here, so the persist-then-dispatch
        ordering is written once and cannot be got wrong per event type.

        THE ORDER IS LOAD-BEARING: write the row, *then* dispatch. The row is part
        of the caller's transaction, so if that transaction rolls back the
        notification goes with it. Dispatching first would mean a possible email
        about a donation that never happened — an unrecallable side effect
        committed before the fact it describes.

        `dispatch` is not awaited because it only *queues*; the task itself runs
        after the response. That is also why the email task receives plain strings:
        by the time it executes, the session that produced them is closed.
        """
        notification = Notification(
            user_id=user_id,
            type=notification_type,
            title=title,
            body=body,
            related_donation_id=related_donation_id,
        )
        await self._repo.add(notification)

        log.info(
            "notification_created",
            notification_id=str(notification.id),
            user_id=str(user_id),
            type=str(notification_type),
        )

        # Best-effort external delivery. The in-app row above is the durable part.
        # NOTE: only the user id is available here, not the email address — fetching
        # it would mean this module querying the users table, i.e. reaching across a
        # module boundary through a repository. A real implementation resolves the
        # address inside the task via its own session, or the users module publishes
        # it. Left explicit rather than papered over.
        self._dispatcher.dispatch(
            _log_pending_delivery,
            str(notification.id),
            title,
        )
        return notification


async def _log_pending_delivery(notification_id: str, title: str) -> None:
    """Placeholder external-delivery task. See tasks.py for the real integration point."""
    log.info("notification_delivery_queued", notification_id=notification_id, title=title)
