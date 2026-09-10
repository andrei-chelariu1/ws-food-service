"""Authorization: role checks and ownership guards.

AUTHENTICATION vs AUTHORIZATION — WHY THEY ARE SEPARATE FILES
-------------------------------------------------------------
`dependencies.py` answers *"who are you?"*. This file answers *"may you do
this?"*. Different questions, different reasons to change: swapping JWT for
sessions touches only the former; adding a `MODERATOR` role touches only the
latter. Single Responsibility at file granularity.

WHY `require_roles` IS A FACTORY
--------------------------------
FastAPI dependencies take no arguments of their own — you cannot write
`Depends(require_roles(Role.ADMIN))` unless `require_roles` *returns* a
dependency. So it is a closure factory: call it with the allowed roles, get back
a dependency function that closes over them.

The payoff is that authorization becomes declarative and visible in the route
signature:

    @router.delete("/{user_id}", dependencies=[Depends(require_roles(Role.ADMIN))])

You can audit who can call what by reading the router, instead of hunting for an
`if user.role != ...` buried at line 40 of a service method. And the check runs
*before* the handler body, so there is no path where the work happens first and
the check is skipped.

THE TWO-LEVEL MODEL, AND WHY OWNERSHIP CANNOT LIVE HERE
------------------------------------------------------
* **Coarse (this file, route level):** "only admins reach this endpoint." Needs
  nothing but the token.
* **Fine (services, object level):** "only the *owner* may edit *this* food
  item." Needs the row, so the check must happen after loading it — inside the
  service.

`require_ownership` below is the shared *predicate* for the second kind, so the
403 message and the admin-override rule are written once rather than in every
service that needs them. The decision of *when* to call it stays with the service
that owns the object. Trying to express object-level rules as a route dependency
is how you end up loading the same row twice, or checking against an id from the
URL that the handler never actually uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from app.core.exceptions import PermissionDeniedError
from app.core.logging import get_logger
from app.shared.security.dependencies import get_current_active_user
from app.shared.security.principal import CurrentUser

log = get_logger(__name__)

# Role names are strings here, not an enum, to keep the shared/modules boundary
# intact (see dependencies.py). `Role` is a StrEnum in app/modules/users/models.py,
# so `require_roles(Role.ADMIN)` passes its string value transparently.
ADMIN_ROLE = "ADMIN"


def require_roles(*allowed_roles: str) -> Callable[[CurrentUser], Awaitable[CurrentUser]]:
    """Build a dependency that admits only the listed roles.

        @router.get("/admin/stats", dependencies=[Depends(require_roles(Role.ADMIN))])

    Returns the user, so it can equally be used as a value dependency when the
    handler needs the caller too:

        user: CurrentUser = Depends(require_roles(Role.DONOR, Role.ADMIN))
    """
    if not allowed_roles:
        # A dependency that allows nobody is always a mistake, and one that
        # allowed *everybody* would be a silent security hole. Fail at import
        # time — when the route is being defined — not at request time.
        raise ValueError("require_roles() needs at least one role")

    permitted = frozenset(allowed_roles)

    async def dependency(
        user: Annotated[CurrentUser, Depends(get_current_active_user)],
    ) -> CurrentUser:
        if user.role not in permitted:
            # Logged at warning: a legitimate client does not probe endpoints it
            # cannot use, so a burst of these is a signal worth alerting on.
            log.warning(
                "authorization_denied",
                user_id=str(user.id),
                user_role=user.role,
                required_roles=sorted(permitted),
            )
            # The message names the requirement but not the caller's own role —
            # no need to help an attacker map the role model.
            raise PermissionDeniedError(
                f"This action requires one of: {', '.join(sorted(permitted))}"
            )
        return user

    # Makes the generated OpenAPI description and any error trace readable
    # instead of showing forty identical `dependency` entries.
    dependency.__name__ = f"require_roles_{'_'.join(sorted(permitted)).lower()}"
    return dependency


def require_ownership(
    user: CurrentUser,
    owner_id: uuid.UUID,
    *,
    resource: str = "resource",
    allow_admin: bool = True,
) -> None:
    """Assert the caller owns the object (or is an admin). Raises, or returns None.

    A plain function, not a dependency, because it needs `owner_id` — which is
    only known after the row has been loaded, i.e. inside a service.

    THIS IS THE IDOR DEFENCE. The vulnerability it prevents:

        PATCH /api/v1/food-items/{someone-elses-id}

    The URL is well-formed, the token is valid, the row exists. Without an
    ownership check the edit succeeds. Automated scanners will not flag it and
    integration tests written by the happy path will not catch it — which is why
    it is one of the most common real-world API vulnerabilities, and why this
    check gets its own named, logged, tested helper rather than an inline `if`.

    `allow_admin=False` exists for actions that must remain personal even for
    administrators — changing a password, for instance. An admin should reset via
    a dedicated flow, not by editing someone's credentials directly.
    """
    if user.id == owner_id:
        return
    if allow_admin and user.role == ADMIN_ROLE:
        # Logged: administrative access to someone else's data is exactly the
        # event an auditor will ask about later.
        log.info(
            "admin_override_ownership",
            admin_id=str(user.id),
            owner_id=str(owner_id),
            resource=resource,
        )
        return

    log.warning(
        "ownership_check_failed",
        user_id=str(user.id),
        owner_id=str(owner_id),
        resource=resource,
    )
    # Deliberately says "not permitted", never "belongs to user X". Confirming
    # that the id exists and identifying its owner are both information leaks.
    raise PermissionDeniedError(f"You do not have permission to modify this {resource}")


def is_admin(user: CurrentUser) -> bool:
    """Readable predicate for the "admins see everything" branch in list queries."""
    return user.role == ADMIN_ROLE
