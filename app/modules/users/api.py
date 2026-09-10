"""HTTP layer for the users module: `/auth/*` and `/users/*`.

WHAT A CONTROLLER IS ALLOWED TO DO
----------------------------------
1. Declare the route, status code, and OpenAPI documentation.
2. Declare authentication/authorization dependencies.
3. Call exactly one service method.
4. Map the result to a response DTO.

WHAT IT MUST NOT DO
-------------------
No `select()`, no `session.` anything, no business rules, no `try/except` around
domain errors. Notice there is not a single `try` block in this file: services
raise domain exceptions and the handlers in `core/exceptions.py` translate them.
Adding `except UserNotFoundError: raise HTTPException(404)` here would duplicate,
in every endpoint, a mapping that is already declared once on the exception class.

Every handler body below is one or two lines. If one grows, the logic belongs in
the service — that is the review heuristic.

WHERE DEPENDENCY INJECTION HAPPENS
----------------------------------
The `get_user_service` provider at the top of this file is the *only* place in the
module that mentions FastAPI's `Depends`. It assembles
`session -> repository -> service` and hands back a ready object. Everything below
it is framework-agnostic by construction.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.rate_limit import limiter
from app.core.redis import get_redis
from app.core.security import get_password_hasher, get_token_service
from app.modules.users.models import Role
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import (
    LoginRequest,
    PasswordChange,
    RefreshRequest,
    RegisterResponse,
    TokenPair,
    UserCreate,
    UserRead,
    UserRoleUpdate,
    UserUpdate,
)
from app.modules.users.service import RedisTokenDenylist, UserService
from app.shared.db.session import get_db
from app.shared.schemas.base_schema import (
    ERROR_RESPONSES_AUTH,
    ERROR_RESPONSES_READ,
    ERROR_RESPONSES_WRITE,
    MessageResponse,
    Page,
)
from app.shared.security.dependencies import CurrentUserDep, bearer_scheme
from app.shared.security.permissions import require_ownership, require_roles
from app.shared.utils.pagination import PageParams


# --------------------------------------------------------------------------
# Dependency wiring — the composition root for this module
# --------------------------------------------------------------------------
def get_user_service(
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserService:
    """Assemble a `UserService` for this request.

    The whole object graph is built here, explicitly:

        AsyncSession -> UserRepository ─┐
        Settings     -> PasswordHasher ─┼-> UserService
        Settings     -> JwtTokenService ─┤
        Redis        -> RedisTokenDenylist ┘

    WHY A PLAIN FUNCTION AND NOT A DI CONTAINER
    ARCHITECTURE.md lists `dependency-injector` as optional. It is not used, and
    this is why: FastAPI's `Depends` already resolves the graph, scopes it to the
    request, and — crucially — makes it overridable in tests
    (`app.dependency_overrides[get_user_service] = ...`). A container would add a
    second, parallel wiring mechanism with its own lifecycle rules. KISS: one
    mechanism, and it is the framework's own.

    The service is cheap to construct (four attribute assignments), so building it
    per request costs nothing measurable and gains request-scoped isolation.
    """
    return UserService(
        repository=UserRepository(session),
        password_hasher=get_password_hasher(settings),
        token_service=get_token_service(settings),
        denylist=RedisTokenDenylist(redis),
        access_token_ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


async def provide_user_authenticator(service: UserServiceDep) -> UserService:
    """Binds `UserService` to the `UserAuthenticator` Protocol that `shared/`
    depends on. Registered in `create_app()`; see
    app/shared/security/dependencies.py for why the indirection exists."""
    return service


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and sign in",
    responses=ERROR_RESPONSES_WRITE,
)
# 3/hour: registration is expensive (a bcrypt hash) and mass account creation is
# the abuse case. Stricter than any other endpoint, and low enough to make the
# email-enumeration oracle in EmailAlreadyRegisteredError impractical to exploit.
@limiter.limit(lambda: get_settings().RATE_LIMIT_REGISTER)
async def register(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: UserCreate,
    service: UserServiceDep,
) -> RegisterResponse:
    """Register a new account.

    New accounts are always `DONOR` and always active. Neither is client-settable:
    `UserCreate` has no such fields and `extra="forbid"` rejects them (422).
    """
    user, tokens = await service.register(payload)
    return RegisterResponse(user=UserRead.model_validate(user), tokens=tokens)


@auth_router.post(
    "/login",
    response_model=TokenPair,
    summary="Exchange credentials for tokens",
    responses=ERROR_RESPONSES_WRITE,
)
# 5/minute is the single most important rate limit in the application: it is what
# turns an offline-speed password guessing attack into an 87-year one.
@limiter.limit(lambda: get_settings().RATE_LIMIT_LOGIN)
async def login(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: LoginRequest,
    service: UserServiceDep,
) -> TokenPair:
    """Sign in.

    Returns 401 with an identical message for an unknown email, a wrong password,
    and a disabled account — see `UserService.authenticate` for why.
    """
    _user, tokens = await service.authenticate(payload)
    return tokens


@auth_router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate a refresh token for a new pair",
    responses=ERROR_RESPONSES_AUTH,
)
async def refresh_tokens(
    payload: RefreshRequest,
    service: UserServiceDep,
) -> TokenPair:
    """Exchange a refresh token for a new access/refresh pair.

    The presented token is single-use: it is denylisted here. Replaying it returns
    401 `TOKEN_REVOKED`, which is how refresh-token theft becomes detectable.

    Not rate-limited beyond the global default: legitimate clients refresh
    regularly, and a limit tight enough to matter would break them. The denylist
    is the control that matters here, not the limit.
    """
    return await service.refresh(payload.refresh_token)


@auth_router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoke the current tokens",
    responses=ERROR_RESPONSES_AUTH,
)
async def logout(
    payload: RefreshRequest,
    service: UserServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    _user: CurrentUserDep,
) -> MessageResponse:
    """Revoke both tokens.

    The refresh token is required in the body: revoking only the access token
    would leave the refresh token able to mint a replacement, which is not a
    logout. `_user` is depended upon so an unauthenticated caller cannot use this
    endpoint to probe token validity.

    Note this handler needs the *raw* access token, not just the decoded
    `CurrentUser` — revocation is by `jti`, which only the token itself carries.
    That is why `bearer_scheme` appears here as well.
    """
    access_token = getattr(credentials, "credentials", "")
    await service.logout(access_token=access_token, refresh_token=payload.refresh_token)
    return MessageResponse(message="Logged out")


# --------------------------------------------------------------------------
# User routes
# --------------------------------------------------------------------------
users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get(
    "/me",
    response_model=UserRead,
    summary="Get the current user's profile",
    responses=ERROR_RESPONSES_AUTH,
)
async def get_me(current_user: CurrentUserDep, service: UserServiceDep) -> UserRead:
    """The caller's own profile.

    `/me` rather than `/users/{my_id}` on purpose: the id comes from the token, so
    there is no id in the URL for a client to tamper with. An entire class of
    authorization bug simply does not exist on this route.
    """
    user = await service.get_by_id(current_user.id)
    return UserRead.model_validate(user)


@users_router.patch(
    "/me",
    response_model=UserRead,
    summary="Update the current user's profile",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_WRITE)
async def update_me(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: UserUpdate,
    current_user: CurrentUserDep,
    service: UserServiceDep,
) -> UserRead:
    """Update your own profile. Only `full_name` is editable — see `UserUpdate`."""
    user = await service.update_profile(current_user.id, payload)
    return UserRead.model_validate(user)


@users_router.post(
    "/me/password",
    response_model=MessageResponse,
    summary="Change the current user's password",
    responses=ERROR_RESPONSES_WRITE,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_LOGIN)
async def change_password(
    request: Request,
    response: Response,  # slowapi writes X-RateLimit-* here; see core/rate_limit.py
    payload: PasswordChange,
    current_user: CurrentUserDep,
    service: UserServiceDep,
) -> MessageResponse:
    """Change your password, re-proving the current one.

    Rate-limited at the login tier, not the write tier: this endpoint verifies a
    password, so it is a password-guessing surface just like `/auth/login`.
    """
    await service.change_password(current_user.id, payload)
    return MessageResponse(message="Password changed")


@users_router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Get a user by id (self or admin)",
    responses=ERROR_RESPONSES_READ,
)
async def get_user(
    user_id: uuid.UUID,
    current_user: CurrentUserDep,
    service: UserServiceDep,
) -> UserRead:
    """Fetch a user by id.

    THE IDOR CHECK. Any authenticated caller can reach this route with any id, so
    `require_ownership` is what stops user A reading user B's profile. It could not
    be a route dependency: `require_roles` alone would either lock everyone out or
    let everyone in, because the rule depends on the *id in the path* matching the
    *id in the token*.

    Admins pass by override, and that override is logged.

    Note the ordering: the ownership check runs *before* the fetch. A caller
    therefore cannot use response timing or a 404-vs-403 difference to discover
    whether an id exists.

    `uuid.UUID` in the signature is itself a control: a non-UUID path segment is
    rejected with a 422 by FastAPI, so nothing resembling an injection payload
    reaches the service.
    """
    require_ownership(current_user, user_id, resource="user profile")
    user = await service.get_by_id(user_id)
    return UserRead.model_validate(user)


@users_router.get(
    "",
    response_model=Page[UserRead],
    summary="List users (admin only)",
    dependencies=[Depends(require_roles(Role.ADMIN))],
    responses=ERROR_RESPONSES_READ,
)
async def list_users(
    service: UserServiceDep,
    page_params: Annotated[PageParams, Depends()],
    role: Role | None = None,
) -> Page[UserRead]:
    """Paginated user list, optionally filtered by role.

    Authorization is declared in the decorator, so it is visible when reading the
    route rather than buried in the body — and it runs before the handler, so
    there is no path where the query executes and the check is skipped.
    """
    users, total = await service.list_users(
        offset=page_params.offset,
        limit=page_params.limit,
        role=role,
    )
    return Page.create(
        [UserRead.model_validate(u) for u in users],
        total=total,
        page=page_params.page,
        size=page_params.size,
    )


@users_router.patch(
    "/{user_id}/role",
    response_model=UserRead,
    summary="Change a user's role (admin only)",
    dependencies=[Depends(require_roles(Role.ADMIN))],
    responses=ERROR_RESPONSES_WRITE,
)
async def change_user_role(
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    service: UserServiceDep,
) -> UserRead:
    """Promote or demote a user.

    A separate endpoint from `PATCH /users/me` precisely so it can carry a
    separate, stricter authorization dependency. Merging the two would push the
    admin check down into the service, where it is invisible to anyone auditing
    the routes.
    """
    user = await service.change_role(user_id, payload.role)
    return UserRead.model_validate(user)


@users_router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    summary="Deactivate a user (admin only)",
    dependencies=[Depends(require_roles(Role.ADMIN))],
    responses=ERROR_RESPONSES_WRITE,
)
async def deactivate_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
) -> MessageResponse:
    """Deactivate an account.

    `DELETE` that deactivates rather than erases. The verb matches the client's
    intent ("remove this user") while the implementation preserves referential
    integrity — a deleted donor's completed donations must remain in the record.
    Genuine erasure is a GDPR workflow, not an HTTP verb; see
    `BaseRepository.hard_delete_by_id`.

    Takes effect immediately: `authenticate_access_token` re-reads `is_active` on
    every request, so the user's still-valid access token stops working now rather
    than in fifteen minutes.
    """
    await service.set_active(user_id, is_active=False)
    return MessageResponse(message="User deactivated")
