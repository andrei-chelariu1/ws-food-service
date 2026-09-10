"""Generic async CRUD repository — the `JpaRepository<T, ID>` equivalent.

WHY HAVE A REPOSITORY AT ALL, GIVEN SQLALCHEMY IS ALREADY AN ABSTRACTION?
------------------------------------------------------------------------
Two concrete reasons, not dogma:

1. **It is the only place that knows SQL exists.** A service reads
   `await self._repo.get_by_email(email)`, not
   `await session.execute(select(User).where(User.email == email))`. So the
   service is testable with a fake repository — no database, no event loop
   fixture, milliseconds per test. Look at `tests/modules/donations/
   test_service.py`: it tests the "expired food cannot be donated" rule with no
   database at all. That is only possible because of this seam.

2. **Cross-cutting query rules are applied once.** Soft delete is the example:
   every read must exclude `is_deleted = true`. Written by hand at each call
   site, the first forgotten filter silently exposes deleted rows to users.
   Here, `_base_select()` applies it automatically for any model carrying
   `SoftDeleteMixin` — DRY where forgetting has a real cost.

WHY GENERICS
------------
`get`, `list`, `create`, `update`, `delete`, `count`, `exists` are identical for
every entity apart from the type. `BaseRepository[User, UUID]` gives each module
those seven methods with full type safety and zero copy-paste. Each module's
repository then adds only what is genuinely specific — `get_by_email`,
`list_available`. That is the DRY payoff: ~40 lines of generic code instead of
~40 lines per module.

WHAT THIS CLASS DELIBERATELY DOES NOT DO
----------------------------------------
* **No `commit()`.** The transaction boundary is the request (see
  shared/db/session.py). A repository that commits makes atomic multi-entity
  operations impossible.
* **No business rules.** `delete()` will happily soft-delete a reserved item.
  Deciding whether that is *allowed* is the service's job. Mixing the two is
  how you end up with business logic that can only run against Postgres.
* **No HTTP.** It returns `None` for a missing row rather than raising 404.
  Translating "absent" into "404" is a web concern; a background job might
  legitimately treat absence as normal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, Select, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.shared.db.base import Base
from app.shared.db.mixins import SoftDeleteMixin
from app.shared.utils.datetime_utils import utcnow

# `bound=Base` means only mapped entities are accepted — a typo like
# `BaseRepository[UserRead, UUID]` (a Pydantic schema) fails type-checking.
ModelT = TypeVar("ModelT", bound=Base)
IdT = TypeVar("IdT")


class BaseRepository(Generic[ModelT, IdT]):
    """Async CRUD over a single entity type.

    Subclass per module:

        class UserRepository(BaseRepository[User, UUID]):
            def __init__(self, session: AsyncSession) -> None:
                super().__init__(User, session)
    """

    def __init__(self, model: type[ModelT], session: AsyncSession) -> None:
        self._model = model
        self._session = session
        # Computed once, not per query.
        self._soft_deletable = issubclass(model, SoftDeleteMixin)

    # -- Query construction ------------------------------------------------

    def _base_select(self, *, include_deleted: bool = False) -> Select[tuple[ModelT]]:
        """Every read starts here, which is what makes the soft-delete filter
        impossible to forget. `include_deleted=True` is the explicit escape
        hatch for admin/audit reads — visible in the call, as it should be.
        """
        stmt = select(self._model)
        if self._soft_deletable and not include_deleted:
            stmt = stmt.where(self._model.is_deleted.is_(False))  # type: ignore[attr-defined]
        return stmt

    # -- Read --------------------------------------------------------------

    async def get(self, entity_id: IdT, *, include_deleted: bool = False) -> ModelT | None:
        """Fetch by primary key. Returns `None` when absent — see the module
        docstring on why this is not a 404.

        Deliberately not `session.get()`: that consults the identity map and
        cannot apply the soft-delete filter, so a previously-loaded deleted row
        would come back.
        """
        stmt = self._base_select(include_deleted=include_deleted).where(
            self._model.id == entity_id  # type: ignore[attr-defined]
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by(self, **filters: Any) -> ModelT | None:
        """Fetch one row by equality filters: `get_by(email=..., is_active=True)`.

        Raises if more than one row matches — that means the caller assumed a
        uniqueness constraint that does not exist, and silently returning the
        first row would hide a real data-model bug.
        """
        stmt = self._base_select().filter_by(**filters)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        filters: Sequence[ColumnElement[bool]] | None = None,
        order_by: Sequence[Any] | None = None,
        include_deleted: bool = False,
    ) -> Sequence[ModelT]:
        """Paginated list.

        `filters` takes SQLAlchemy expressions rather than kwargs so callers can
        express ranges (`FoodItem.expires_at > now`) — kwargs can only do
        equality. It is typed `ColumnElement[bool]`, so passing a raw string is
        a type error: there is no route from user input to SQL text here.

        `limit` defaults to 50 and is capped by `PageParams` at the API edge, so
        no client can request the whole table (accidentally or otherwise).
        """
        stmt = self._base_select(include_deleted=include_deleted)
        if filters:
            stmt = stmt.where(*filters)
        # Deterministic default when the caller gives no ordering. Without ORDER BY,
        # Postgres may return rows in any order — so page 2 can repeat or skip rows
        # from page 1.
        stmt = stmt.order_by(*(order_by or [self._model.id]))  # type: ignore[attr-defined]
        stmt = stmt.offset(offset).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def count(
        self,
        *,
        filters: Sequence[ColumnElement[bool]] | None = None,
        include_deleted: bool = False,
    ) -> int:
        """Total matching rows, for pagination metadata.

        Counts over a subquery of `_base_select()` so it applies exactly the
        same filters as `list()`. Duplicating the WHERE clause here is how you
        get a `total` that disagrees with the returned items.
        """
        inner = self._base_select(include_deleted=include_deleted)
        if filters:
            inner = inner.where(*filters)
        stmt = select(func.count()).select_from(inner.subquery())
        return (await self._session.execute(stmt)).scalar_one()

    async def exists(self, **filters: Any) -> bool:
        """Cheaper than `get_by(...) is not None`: selects a literal, not the row.

        Matters for the uniqueness check on registration, which runs on every
        signup and does not need the user object.
        """
        stmt = select(func.count()).select_from(
            self._base_select().filter_by(**filters).limit(1).subquery()
        )
        return bool((await self._session.execute(stmt)).scalar_one())

    # -- Write -------------------------------------------------------------

    async def add(self, entity: ModelT) -> ModelT:
        """Stage an INSERT and flush it.

        `flush()` — not `commit()`. It sends the INSERT so that server defaults
        are populated and constraint violations surface *here* (as an
        `IntegrityError` the service can translate into a domain `ConflictError`)
        rather than at request commit, where the traceback no longer says which
        operation caused it.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        self._session.add_all(list(entities))
        await self._session.flush()
        return entities

    async def update(self, entity: ModelT, **values: Any) -> ModelT:
        """Apply attribute updates to a loaded entity.

        Takes the *entity*, not an id, so the caller has necessarily already
        loaded it — which means authorization and business rules have had a
        chance to run against real state. A `update_by_id(id, **values)` helper
        would quietly invite skipping that.

        `setattr` in a loop lets SQLAlchemy's change tracking emit an UPDATE
        containing only the modified columns.
        """
        for field, value in values.items():
            if not hasattr(entity, field):
                # Fail loudly: a typo'd field name would otherwise be a silent
                # no-op, and "my update didn't save" is a miserable bug to chase.
                raise AttributeError(f"{type(entity).__name__} has no attribute {field!r}")
            setattr(entity, field, value)
        await self._session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        """Soft-delete when the model supports it, hard-delete otherwise.

        One method, correct behaviour for both — so services say `delete()` and
        do not have to know (or keep straight) which entities are soft-deletable.
        """
        # `self._soft_deletable` was computed once in __init__ from the model class.
        # Branching on it rather than `isinstance(entity, SoftDeleteMixin)` because
        # `ModelT` is bound to `Base`, so a static checker proves the isinstance branch
        # dead — it cannot know that *some* models mix in SoftDeleteMixin. Same runtime
        # behaviour, and the type checker stays useful instead of being silenced.
        if self._soft_deletable:
            cast("SoftDeleteMixin", entity).mark_deleted(utcnow())
        else:
            await self._session.delete(entity)
        await self._session.flush()

    async def hard_delete_by_id(self, entity_id: IdT) -> int:
        """Unconditional DELETE, bypassing soft delete. Returns rows affected.

        Exists for GDPR erasure and test cleanup. Named `hard_` so it can never
        be invoked by accident, and so `grep hard_delete` finds every caller
        during a data-retention audit.
        """
        stmt = sql_delete(self._model).where(self._model.id == entity_id)  # type: ignore[attr-defined]
        # `execute()` is typed as returning `Result`, but a DML statement always
        # yields a `CursorResult`, which is the class that carries `rowcount`.
        # Narrowing here is more honest than a blanket `type: ignore`.
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount

    # -- Escape hatch ------------------------------------------------------

    @property
    def session(self) -> AsyncSession:
        """Raw session access for genuinely complex queries.

        Provided rather than pretending it is never needed — a repository that
        blocks a legitimate window function or CTE just gets bypassed. The rule
        is that this stays *inside* a repository subclass: it must never be
        reached from a service, which is the boundary that keeps services
        database-free.
        """
        return self._session
