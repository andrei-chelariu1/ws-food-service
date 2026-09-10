"""HTTP layer for food items.

Note the authorization pattern across these routes — three different levels, each
expressed in the place that can actually enforce it:

* `GET /food-items` — no dependency at all. Public: browsing surplus food should
  not require an account.
* `POST /food-items` — `CurrentUserDep`. Any authenticated user may list food.
* `PATCH /food-items/{id}` — authenticated *plus* an ownership check inside the
  service, because the rule depends on the row.

That last one cannot be a route dependency, and the reason is worth internalising:
`require_roles` can only see the token. "Is this *your* item?" needs the item.
See app/shared/security/permissions.py.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.rate_limit import limiter
from app.core.redis import get_redis
from app.modules.food_items.models import FoodItemStatus
from app.modules.food_items.repository import FoodItemRepository
from app.modules.food_items.schemas import FoodItemCreate, FoodItemRead, FoodItemUpdate
from app.modules.food_items.service import FoodItemService
from app.shared.cache.cache import RedisCache
from app.shared.db.session import get_db
from app.shared.schemas.base_schema import (
    ERROR_RESPONSES_READ,
    ERROR_RESPONSES_WRITE,
    MessageResponse,
    Page,
)
from app.shared.security.dependencies import CurrentUserDep
from app.shared.utils.pagination import PageParams

router = APIRouter(prefix="/food-items", tags=["food-items"])


def get_food_item_service(
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FoodItemService:
    """Assemble a `FoodItemService`.

    `RedisCache` is constructed here and injected as a `Cache`. The service is
    annotated against the Protocol, so this is the only line that would change to
    swap the backend — and `tests/conftest.py` overrides this provider with
    `NullCache` so the test suite needs no Redis at all.
    """
    return FoodItemService(
        repository=FoodItemRepository(session),
        cache=RedisCache(redis, default_ttl=settings.CACHE_DEFAULT_TTL_SECONDS),
    )


FoodItemServiceDep = Annotated[FoodItemService, Depends(get_food_item_service)]


@router.get(
    "",
    response_model=Page[FoodItemRead],
    summary="Browse available food (public)",
)
async def list_available_food(
    service: FoodItemServiceDep,
    page_params: Annotated[PageParams, Depends()],
    search: Annotated[
        str | None,
        Query(max_length=100, description="Case-insensitive name match"),
    ] = None,
) -> Page[FoodItemRead]:
    """List available, unexpired food.

    Deliberately public — no `CurrentUserDep`. Someone in need should be able to
    see what is available before creating an account. It is also therefore the one
    endpoint safe to cache globally: no per-user data means one cached page is
    correct for every caller.

    On a cache miss the service returns ORM entities; on a hit, already-serialised
    dicts. `FoodItemRead.model_validate` accepts both (`from_attributes=True` plus
    Pydantic's native dict handling), so one line covers the two paths without a
    branch.

    `search` is capped at 100 characters: an unbounded `ILIKE '%...%'` pattern is a
    cheap way to make the database work hard.
    """
    items, total, from_cache = await service.list_available(
        offset=page_params.offset,
        limit=page_params.limit,
        page=page_params.page,
        size=page_params.size,
        search=search,
    )

    read_items = [FoodItemRead.model_validate(item) for item in items]

    if not from_cache:
        # Write through *after* serialising, so what lands in Redis is exactly what
        # the client received — no ORM objects (bound to a session that is about to
        # close) and no risk of the cached shape drifting from the response shape.
        await service.cache_available_page(
            page=page_params.page,
            size=page_params.size,
            search=search,
            serialised_items=[item.model_dump(mode="json") for item in read_items],
            total=total,
        )

    return Page.create(read_items, total=total, page=page_params.page, size=page_params.size)


@router.get(
    "/mine",
    response_model=Page[FoodItemRead],
    summary="List your own listings",
    responses=ERROR_RESPONSES_READ,
)
async def list_my_food(
    current_user: CurrentUserDep,
    service: FoodItemServiceDep,
    page_params: Annotated[PageParams, Depends()],
    item_status: Annotated[FoodItemStatus | None, Query(alias="status")] = None,
) -> Page[FoodItemRead]:
    """Your listings, in any status.

    Declared *before* `/{item_id}` on purpose. FastAPI matches routes in
    registration order, so with the dynamic route first, `/mine` would be captured
    as `item_id="mine"` and fail with a 422 UUID parse error. A classic and
    confusing bug; the fix is simply ordering.

    The parameter is named `item_status` in Python (`status` is the imported
    `fastapi.status` module) but aliased back to `status` for the client — the API
    contract is unaffected by our local naming.
    """
    items, total = await service.list_mine(
        current_user,
        offset=page_params.offset,
        limit=page_params.limit,
        status=item_status,
    )
    return Page.create(
        [FoodItemRead.model_validate(i) for i in items],
        total=total,
        page=page_params.page,
        size=page_params.size,
    )


@router.get(
    "/{item_id}",
    response_model=FoodItemRead,
    summary="Get one listing (public)",
    responses=ERROR_RESPONSES_READ,
)
async def get_food_item(item_id: uuid.UUID, service: FoodItemServiceDep) -> FoodItemRead:
    """Fetch a single listing. Public, like the browse endpoint.

    `uuid.UUID` in the signature means a malformed id is rejected with a 422 by
    FastAPI before any application code runs.
    """
    item = await service.get_by_id(item_id)
    return FoodItemRead.model_validate(item)


@router.post(
    "",
    response_model=FoodItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="List surplus food",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def create_food_item(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: FoodItemCreate,
    current_user: CurrentUserDep,
    service: FoodItemServiceDep,
) -> FoodItemRead:
    """Create a listing owned by the caller.

    201 with the created resource in the body, so the client gets the
    server-assigned `id`, `status` and timestamps without a follow-up GET.
    """
    item = await service.create(payload, current_user)
    return FoodItemRead.model_validate(item)


@router.patch(
    "/{item_id}",
    response_model=FoodItemRead,
    summary="Edit your listing",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def update_food_item(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    item_id: uuid.UUID,
    payload: FoodItemUpdate,
    current_user: CurrentUserDep,
    service: FoodItemServiceDep,
) -> FoodItemRead:
    """Partially update a listing you own.

    PATCH, not PUT: only the fields present in the body are applied
    (`exclude_unset=True` in the service). A PUT here would silently blank every
    omitted field.

    The ownership check is inside `service.update` — this is the IDOR-prone shape
    of route (`{item_id}` from the URL, identity from the token), and the guard is
    what stops one user editing another's listing.
    """
    item = await service.update(item_id, payload, current_user)
    return FoodItemRead.model_validate(item)


@router.post(
    "/{item_id}/cancel",
    response_model=FoodItemRead,
    summary="Withdraw your listing",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def cancel_food_item(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    item_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: FoodItemServiceDep,
) -> FoodItemRead:
    """Cancel a listing.

    A named action endpoint rather than `PATCH {"status": "CANCELLED"}`. Two
    reasons:

    * A generic status field would let a client attempt *any* transition, turning
      the state machine into a suggestion. There is no `status` field in
      `FoodItemUpdate` at all, so the only way to change state is through an
      endpoint that models a specific, authorized action.
    * `POST /{id}/cancel` documents intent. `PATCH` with a magic string does not.

    Fails with 409 if the item is RESERVED — decline the donation request first, so
    the recipient is notified rather than left waiting.
    """
    item = await service.cancel(item_id, current_user)
    return FoodItemRead.model_validate(item)


@router.delete(
    "/{item_id}",
    response_model=MessageResponse,
    summary="Delete your listing",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def delete_food_item(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    item_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: FoodItemServiceDep,
) -> MessageResponse:
    """Soft-delete a listing.

    The row survives (see `SoftDeleteMixin`) because a `Donation` may reference it
    and the record of a completed handover must not vanish. Clients see it gone;
    the audit trail stays intact.
    """
    await service.soft_delete(item_id, current_user)
    return MessageResponse(message="Food item deleted")
