"""Data access for `User`.

This class is short, and that is the point: `BaseRepository` already provides
`get`, `list`, `count`, `exists`, `add`, `update`, `delete`. Only genuinely
user-specific queries live here.

Every method here is a *query*. No decisions. `get_by_email` returns `None` for
an unknown address rather than raising — whether that is an error depends on the
caller (registration wants `None`, login treats it as a failed attempt), and only
the caller can know. A repository that raised would force `try/except` on the
happy path.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import Role, User
from app.shared.repository.base_repository import BaseRepository


class UserRepository(BaseRepository[User, uuid.UUID]):
    """`BaseRepository[User, UUID]` plus user-specific lookups."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> User | None:
        """Look up by email address.

        Compares against the stored value directly, with no `func.lower()`.
        That is only safe because `UserCreate`/`LoginRequest` normalise to
        lowercase at the edge (see schemas.py) — and it matters: wrapping the
        column in a function makes `ix_users_email` unusable, turning every login
        into a sequential scan. The normalisation and this query are two halves of
        one decision.
        """
        return await self.get_by(email=email)

    async def email_exists(self, email: str) -> bool:
        """Uniqueness pre-check for registration.

        Runs on every signup and does not need the row, so `exists()` selects a
        literal instead of the full user.

        NOTE — this is a pre-check, not the guarantee. Two concurrent signups for
        the same address can both pass it before either commits; the unique
        constraint on `users.email` is what actually prevents the duplicate. The
        service catches the resulting `IntegrityError`. Doing both is not
        redundant: the check gives a clean error message in the common case, the
        constraint gives correctness in the racing case.
        """
        return await self.exists(email=email)

    async def list_by_role(
        self,
        role: Role,
        *,
        offset: int = 0,
        limit: int = 50,
        active_only: bool = True,
    ) -> Sequence[User]:
        """Admin listing, filtered by role.

        `active_only=True` by default, matching the partial index
        `ix_users_role_active` (see models.py) — the common query and the index
        that serves it were designed together.
        """
        filters = [User.role == role]
        if active_only:
            filters.append(User.is_active.is_(True))
        return await self.list(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by=[User.created_at.desc()],
        )

    async def count_by_role(self, role: Role, *, active_only: bool = True) -> int:
        filters = [User.role == role]
        if active_only:
            filters.append(User.is_active.is_(True))
        return await self.count(filters=filters)

    async def count_by_role_grouped(self) -> dict[str, int]:
        """`{"DONOR": 42, "RECIPIENT": 17, "ADMIN": 2}` in one query.

        A GROUP BY that `BaseRepository` cannot express generically — exactly the
        kind of thing a module repository is for. Note that the aggregation is
        done by the *database*: fetching every user and counting in Python would
        work at 100 users and fall over at 100,000.

        Uses `self.session` (the documented escape hatch) because this returns
        rows, not entities. It stays inside the repository, which is the rule
        that keeps services database-free.
        """
        stmt = select(User.role, func.count()).group_by(User.role)
        rows = (await self.session.execute(stmt)).all()
        return {str(role): count for role, count in rows}
