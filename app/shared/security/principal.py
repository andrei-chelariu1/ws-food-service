"""The authenticated principal and the authentication contract — framework-free.

WHY THIS IS A SEPARATE FILE FROM `dependencies.py`
--------------------------------------------------
`dependencies.py` is FastAPI wiring: it imports `Depends`, `HTTPBearer`,
`Request`. `UserService.authenticate_access_token` has to *return* a
`CurrentUser`, so if `CurrentUser` lived in that file, every service that touched
authentication would transitively import FastAPI — and the claim "services are
framework-independent" would be technically false.

Splitting the two costs one small file and buys a boundary that actually holds:

    app/modules/users/service.py     imports principal.py   (no FastAPI)
    app/shared/security/dependencies.py  imports principal.py + FastAPI

Verify it:
    grep -rn "fastapi" app/modules/*/service.py app/shared/security/principal.py
    -> no matches

This is a small deviation from the file list in ARCHITECTURE.md, made because
that list did not anticipate this constraint. The principle it protects — the
Dependency Inversion Principle, with the abstraction owned by neither side — is
worth more than the literal file layout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller, as the rest of the application sees them.

    NOT the `User` ORM entity. Three reasons:

    1. `shared/` may not import `app.modules`, so it could not name that type.
    2. It carries only what authorization needs. `hashed_password` therefore
       cannot travel through the request pipeline, so it cannot end up in a log
       line, a traceback, or a debug response.
    3. It is detached from the database session, so passing it around can never
       trigger a lazy load after that session has closed.

    Frozen, so a handler cannot mutate its own identity (say, flip `role`)
    part-way through a request and have a later check see the change.

    `role` is a plain `str`, not the `Role` enum, for the boundary reason above.
    `Role` is a `StrEnum`, so `require_roles(Role.ADMIN)` still reads naturally
    from the module side — the member degrades to its string value on the way in.
    """

    id: uuid.UUID
    email: str
    role: str
    is_active: bool

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    def is_self(self, user_id: uuid.UUID) -> bool:
        return self.id == user_id


@runtime_checkable
class UserAuthenticator(Protocol):
    """What `shared/security` needs from whichever module owns identity.

    One method. A narrow Protocol is a strong Protocol: this is the complete list
    of things the authentication layer may ask of the users module.

    `UserService` satisfies it **structurally** — it never declares that it
    implements this Protocol and never imports this file's consumer. Both sides
    depend on the abstraction; neither depends on the other.

    CONTRACT: implementations raise `AuthenticationError` (or a subclass) for an
    invalid, expired, revoked, or wrong-type token. They must never return
    `None` — a nullable return invites a caller to forget the check, whereas an
    exception cannot be ignored.
    """

    async def authenticate_access_token(self, token: str) -> CurrentUser: ...
