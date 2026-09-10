"""HTTP layer for notifications.

Every route here is scoped to the authenticated caller. There is no
`GET /notifications/{user_id}` and no `POST /notifications` — see schemas.py on why
the absence of a create endpoint is a security decision rather than an omission.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.repository import NotificationRepository
from app.modules.notifications.schemas import NotificationRead, UnreadCountResponse
from app.modules.notifications.service import NotificationService
from app.modules.notifications.tasks import BackgroundTasksDispatcher
from app.shared.db.session import get_db
from app.shared.schemas.base_schema import (
    ERROR_RESPONSES_READ,
    ERROR_RESPONSES_WRITE,
    MessageResponse,
    Page,
)
from app.shared.security.dependencies import CurrentUserDep
from app.shared.utils.pagination import PageParams

router = APIRouter(prefix="/notifications", tags=["notifications"])


def get_notification_service(
    session: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> NotificationService:
    """Assemble a `NotificationService`.

    `BackgroundTasks` is only injectable inside a request, which is precisely why
    `BackgroundTasksDispatcher` is constructed *here* and passed to the service as a
    `TaskDispatcher`. The service never sees FastAPI; this function is the entire
    extent of the framework's involvement in background work.

    Note this provider is also imported by `donations/api.py`: `DonationService`
    needs a `NotificationService` to emit events, and reusing this provider means
    both share one session and one dispatcher within a request — so a donation and
    its notification commit or roll back together.
    """
    return NotificationService(
        repository=NotificationRepository(session),
        dispatcher=BackgroundTasksDispatcher(background_tasks),
    )


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]


@router.get(
    "",
    response_model=Page[NotificationRead],
    summary="List your notifications",
    responses=ERROR_RESPONSES_READ,
)
async def list_notifications(
    current_user: CurrentUserDep,
    service: NotificationServiceDep,
    page_params: Annotated[PageParams, Depends()],
    unread_only: Annotated[bool, Query(description="Return only unread notifications")] = False,
) -> Page[NotificationRead]:
    """Your inbox, newest first."""
    items, total = await service.list_for_user(
        current_user,
        offset=page_params.offset,
        limit=page_params.limit,
        unread_only=unread_only,
    )
    return Page.create(
        [NotificationRead.model_validate(n) for n in items],
        total=total,
        page=page_params.page,
        size=page_params.size,
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="Unread notification count",
    responses=ERROR_RESPONSES_READ,
)
async def unread_count(
    current_user: CurrentUserDep,
    service: NotificationServiceDep,
) -> UnreadCountResponse:
    """The badge number.

    Declared before `/{notification_id}` — otherwise FastAPI would match this path
    against the dynamic route and fail trying to parse "unread-count" as a UUID.
    Route order is significant; see the same note in food_items/api.py.

    A dedicated endpoint because clients poll it frequently: one indexed `COUNT(*)`
    against the partial unread index, instead of transferring a page of rows to
    render a single integer.
    """
    return UnreadCountResponse(unread=await service.count_unread(current_user))


@router.post(
    "/read-all",
    response_model=MessageResponse,
    summary="Mark all notifications read",
    responses=ERROR_RESPONSES_WRITE,
)
async def mark_all_read(
    current_user: CurrentUserDep,
    service: NotificationServiceDep,
) -> MessageResponse:
    """Clear the whole inbox in one bulk UPDATE."""
    count = await service.mark_all_read(current_user)
    return MessageResponse(message=f"Marked {count} notification(s) as read")


@router.post(
    "/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark one notification read",
    responses=ERROR_RESPONSES_WRITE,
)
async def mark_read(
    notification_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: NotificationServiceDep,
) -> NotificationRead:
    """Mark a single notification read.

    The ownership check is in the service, because it needs the row. This is the
    most IDOR-exposed route in the application — enumerable ids, sensitive bodies —
    and it returns the same error for "not yours" as for "does not exist", so it
    cannot be used to discover which ids are real.
    """
    notification = await service.mark_read(notification_id, current_user)
    return NotificationRead.model_validate(notification)


@router.delete(
    "/{notification_id}",
    response_model=MessageResponse,
    summary="Dismiss a notification",
    responses=ERROR_RESPONSES_WRITE,
)
async def dismiss_notification(
    notification_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: NotificationServiceDep,
) -> MessageResponse:
    """Delete a notification permanently.

    A genuine hard delete — `Notification` has no `SoftDeleteMixin`, so
    `BaseRepository.delete` removes the row. Contrast `DELETE /food-items/{id}`,
    which soft-deletes because a donation references it. The same method call
    produces the correct behaviour for both, decided by the model rather than by the
    caller.
    """
    await service.dismiss(notification_id, current_user)
    return MessageResponse(message="Notification dismissed")
