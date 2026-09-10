"""Authentication dependencies: turn a bearer token into a `CurrentUser`.

THE INTERESTING PROBLEM THIS FILE SOLVES
----------------------------------------
Authentication is cross-cutting — every module needs it — so it belongs in
`shared/`. But verifying a token means loading a user, and `User` lives in
`app/modules/users/`. A naive implementation therefore has `shared/` importing
`modules/`, which is exactly the dependency direction the architecture forbids.
Allow that one import and `shared/` is no longer reusable plumbing; it is coupled
to a feature.

THE SOLUTION: DEPENDENCY INVERSION AT THE COMPOSITION ROOT
----------------------------------------------------------
`principal.py` (next to this file, deliberately framework-free) declares the
*contract*: the `CurrentUser` value object and the `UserAuthenticator` Protocol.
This module adds a placeholder provider that raises. `app/main.py` — the
composition root, the only place allowed to know about everything — binds the
real implementation:

    app.dependency_overrides[get_user_authenticator] = provide_user_authenticator

`UserService` satisfies `UserAuthenticator` **structurally**: it never declares
that it implements it and never imports this file. Both sides depend on the
abstract Protocol; neither depends on the other. That is the "D" in SOLID applied
to a real constraint rather than to a textbook example.

Verify the boundary holds:
    grep -rn "app.modules" app/shared/     -> no matches
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.logging import get_logger
from app.shared.security.principal import CurrentUser, UserAuthenticator

log = get_logger(__name__)

# `auto_error=False` so a missing header returns `None` here instead of
# Starlette's own 403. We raise our own `AuthenticationError` (401 + the correct
# `WWW-Authenticate` header) and it flows through the standard problem+json
# handler like every other error. Consistency of the error contract is worth
# these three extra lines.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Bearer",
    description="JWT access token from POST /api/v1/auth/login",
)


async def get_user_authenticator() -> UserAuthenticator:
    """Placeholder provider, replaced at the composition root.

    Raising rather than returning a default is deliberate: a silent fallback that
    authenticated nobody (or worse, everybody) would be a security hole hidden
    behind a convenience. If the wiring in `create_app()` is ever removed, every
    protected endpoint fails loudly with this message.
    """
    raise NotImplementedError(
        "No UserAuthenticator is bound. app/main.py:create_app() must override "
        "the get_user_authenticator dependency."
    )


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    authenticator: Annotated[UserAuthenticator, Depends(get_user_authenticator)],
) -> CurrentUser:
    """Require a valid access token; return the caller.

    Also stashes the user id on `request.state` so two things downstream can see
    it without re-running auth:
      * `rate_limit_key()` — per-user limits instead of per-IP
      * the access log — every request line attributed to an account
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    user = await authenticator.authenticate_access_token(credentials.credentials)

    request.state.user_id = str(user.id)
    request.state.user_role = user.role
    return user


async def get_current_active_user(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Require an *enabled* account.

    Separate from `get_current_user` so deactivation takes effect immediately
    rather than at token expiry. A disabled account with a still-valid 15-minute
    access token would otherwise keep working — which is not what "disable this
    user" means to whoever clicked it.

    403, not 401: the credentials are genuine. Telling the client to re-login
    would send them round a loop they cannot win.
    """
    if not user.is_active:
        log.warning("inactive_user_blocked", user_id=str(user.id))
        raise PermissionDeniedError("This account has been deactivated")
    return user


async def get_optional_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    authenticator: Annotated[UserAuthenticator, Depends(get_user_authenticator)],
) -> CurrentUser | None:
    """For endpoints that work anonymously but do more when signed in.

    A bad token is treated as anonymous rather than as an error: the endpoint is
    public, so the useful behaviour is to serve the public response. Logged at
    debug so a client with a genuinely broken token is still diagnosable.
    """
    if credentials is None or not credentials.credentials:
        return None
    try:
        user = await authenticator.authenticate_access_token(credentials.credentials)
    except AuthenticationError:
        log.debug("optional_auth_failed_treating_as_anonymous")
        return None
    request.state.user_id = str(user.id)
    request.state.user_role = user.role
    return user


# Readable aliases for route signatures. `user: CurrentUserDep` instead of
# `user: Annotated[CurrentUser, Depends(get_current_active_user)]` on every one
# of forty endpoints — and if the default protection level ever changes, it
# changes here, once.
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_active_user)]
OptionalUserDep = Annotated[CurrentUser | None, Depends(get_optional_current_user)]
