"""HTTP layer for donations.

The provider below is the clearest illustration of the layering in the project: it
assembles a service that depends on **two other modules' services**, each built by
its own module's provider. Composition happens here, at the edge; the services
themselves know only the abstractions they were handed.

Also note the action-endpoint style: `/accept`, `/decline`, `/complete`, `/cancel`
rather than `PATCH {"status": "..."}`. Each action has a different authorized party
(donor vs recipient) and different side effects, so each gets its own route. A
single generic status field would collapse four distinct permissions into one and
make the state machine bypassable.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.modules.donations.models import DonationStatus
from app.modules.donations.repository import DonationRepository
from app.modules.donations.schemas import (
    DonationCreate,
    DonationDecline,
    DonationRead,
    DonationWithItemRead,
)
from app.modules.donations.service import DonationService
from app.modules.food_items.api import get_food_item_service
from app.modules.food_items.service import FoodItemService
from app.modules.notifications.api import get_notification_service
from app.modules.notifications.service import NotificationService
from app.shared.db.session import get_db
from app.shared.schemas.base_schema import ERROR_RESPONSES_READ, ERROR_RESPONSES_WRITE, Page
from app.shared.security.dependencies import CurrentUserDep
from app.shared.utils.pagination import PageParams

router = APIRouter(prefix="/donations", tags=["donations"])


def get_donation_service(
    session: Annotated[AsyncSession, Depends(get_db)],
    food_item_service: Annotated[FoodItemService, Depends(get_food_item_service)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
) -> DonationService:
    """Assemble a `DonationService` from its own repository and two sibling services.

    REUSING THE OTHER MODULES' PROVIDERS IS THE IMPORTANT DETAIL. This function does
    not construct `FoodItemService(FoodItemRepository(session), ...)` itself. Two
    consequences:

    * **One session per request, shared.** FastAPI caches dependency results within
      a request, so `get_db` resolves once and all three services use the same
      `AsyncSession` — which is what makes "accept a donation" (two tables plus a
      notification row) a single atomic transaction.
    * **No duplicated wiring.** When `FoodItemService` gains a dependency, only
      `food_items/api.py` changes. If this file rebuilt it by hand, every such change
      would need a matching edit here — and the day someone forgot, two code paths
      would construct differently-configured services.
    """
    return DonationService(
        repository=DonationRepository(session),
        food_item_service=food_item_service,
        notification_service=notification_service,
    )


DonationServiceDep = Annotated[DonationService, Depends(get_donation_service)]


@router.post(
    "",
    response_model=DonationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Request a food item",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def request_donation(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: DonationCreate,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationRead:
    """Request a food item.

    Possible 409s, all of which are business rules rather than client mistakes:
      * `FOOD_ITEM_EXPIRED` — the central invariant of the application;
      * `FOOD_ITEM_NOT_AVAILABLE` — someone else claimed it first;
      * `CANNOT_DONATE_TO_SELF`;
      * `DUPLICATE_DONATION_REQUEST`.

    None of them is raised in this function. The service raises domain errors and
    `core/exceptions.py` maps them — which is why there is no `try` block here.

    `recipient_id` comes from the token, so a client cannot request food on someone
    else's behalf.
    """
    donation = await service.request_donation(payload, current_user)
    return DonationRead.model_validate(donation)


@router.get(
    "/mine",
    response_model=Page[DonationWithItemRead],
    summary="Requests you made",
    responses=ERROR_RESPONSES_READ,
)
async def list_my_requests(
    current_user: CurrentUserDep,
    service: DonationServiceDep,
    page_params: Annotated[PageParams, Depends()],
    donation_status: Annotated[DonationStatus | None, Query(alias="status")] = None,
) -> Page[DonationWithItemRead]:
    """Your outgoing requests, each with its food item.

    Returns the nested `DonationWithItemRead`: a list of requests without item names
    would be unreadable. The items are eager-loaded in two queries total, not N+1 —
    see `DonationRepository.list_for_recipient_with_items`.

    Declared before `/{donation_id}` so "mine" is not parsed as a UUID.
    """
    items, total = await service.list_my_requests(
        current_user,
        offset=page_params.offset,
        limit=page_params.limit,
        status=donation_status,
    )
    return Page.create(
        [DonationWithItemRead.model_validate(d) for d in items],
        total=total,
        page=page_params.page,
        size=page_params.size,
    )


@router.get(
    "/received",
    response_model=Page[DonationWithItemRead],
    summary="Requests for food you listed",
    responses=ERROR_RESPONSES_READ,
)
async def list_requests_for_my_food(
    current_user: CurrentUserDep,
    service: DonationServiceDep,
    page_params: Annotated[PageParams, Depends()],
    donation_status: Annotated[DonationStatus | None, Query(alias="status")] = None,
) -> Page[DonationWithItemRead]:
    """The donor's inbox. Filter with `?status=PENDING` for the actionable ones."""
    items, total = await service.list_requests_for_my_food(
        current_user,
        offset=page_params.offset,
        limit=page_params.limit,
        status=donation_status,
    )
    return Page.create(
        [DonationWithItemRead.model_validate(d) for d in items],
        total=total,
        page=page_params.page,
        size=page_params.size,
    )


@router.get(
    "/{donation_id}",
    response_model=DonationWithItemRead,
    summary="Get a donation (participants only)",
    responses=ERROR_RESPONSES_READ,
)
async def get_donation(
    donation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationWithItemRead:
    """Fetch one donation.

    Visible only to its two participants (or an admin) — unlike a food listing,
    which is public. A donation reveals who is *receiving* food, which is sensitive.
    That asymmetry between the two modules is deliberate.
    """
    donation = await service.get_by_id(donation_id, current_user)
    return DonationWithItemRead.model_validate(donation)


@router.post(
    "/{donation_id}/accept",
    response_model=DonationRead,
    summary="Accept a request (item owner only)",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def accept_donation(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    donation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationRead:
    """Accept a request. Only the food's owner may do this.

    Authorization is enforced in the service (`_assert_is_item_owner`) rather than
    as a route dependency, because the permitted party is determined by the *food
    item's* owner — two joins away from the token. `require_roles` cannot see that.
    """
    donation = await service.accept(donation_id, current_user)
    return DonationRead.model_validate(donation)


@router.post(
    "/{donation_id}/decline",
    response_model=DonationRead,
    summary="Decline a request (item owner only)",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def decline_donation(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    donation_id: uuid.UUID,
    payload: DonationDecline,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationRead:
    """Decline a request and release the food back to AVAILABLE.

    Two tables change plus a notification row, all in one transaction. If any step
    fails the whole thing rolls back — there is no state where the request is
    declined but the food stays stuck in RESERVED.
    """
    donation = await service.decline(donation_id, payload, current_user)
    return DonationRead.model_validate(donation)


@router.post(
    "/{donation_id}/complete",
    response_model=DonationRead,
    summary="Confirm handover (item owner only)",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def complete_donation(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    donation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationRead:
    """Mark the handover done. Both the donation and the item reach terminal states."""
    donation = await service.complete(donation_id, current_user)
    return DonationRead.model_validate(donation)


@router.post(
    "/{donation_id}/cancel",
    response_model=DonationRead,
    summary="Withdraw your request (recipient only)",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def cancel_donation(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    donation_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: DonationServiceDep,
) -> DonationRead:
    """Withdraw your own request, releasing the food for someone else.

    The recipient's counterpart to `/decline`. Separate endpoints because the
    authorized party and the notification differ — merging them behind a flag would
    entangle two permissions in one handler.
    """
    donation = await service.cancel(donation_id, current_user)
    return DonationRead.model_validate(donation)
