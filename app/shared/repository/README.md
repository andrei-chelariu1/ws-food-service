# `app/shared/repository/` — Generic Data Access

## Purpose

`BaseRepository[ModelT, IdT]` — seven async CRUD methods, typed, shared by every module.

```python
class UserRepository(BaseRepository[User, uuid.UUID]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> User | None:
        return await self.get_by(email=email)
```

Each module gets `get`, `get_by`, `list`, `count`, `exists`, `add`, `update`, `delete` for
free, and adds only what is genuinely specific.

---

## Why have a repository at all, given SQLAlchemy is already an abstraction?

Two concrete reasons, not dogma.

### 1. It is the only place that knows SQL exists

A service reads `await self._repo.get_by_email(email)`, not
`await session.execute(select(User).where(...))`. So a service is testable with a fake
repository — no database, no event loop fixture, milliseconds per test.

Look at `tests/modules/donations/test_service.py`: it tests "expired food cannot be
donated" with **no HTTP, no auth, no clock mocking**. That is only possible because of this
seam.

### 2. Cross-cutting query rules are applied once

Soft delete is the example. *Every* read must exclude `is_deleted = true`, and one forgotten
filter silently exposes deleted rows to users.

`_base_select()` applies it automatically for any model carrying `SoftDeleteMixin`:

```python
def _base_select(self, *, include_deleted: bool = False) -> Select[tuple[ModelT]]:
    stmt = select(self._model)
    if self._soft_deletable and not include_deleted:
        stmt = stmt.where(self._model.is_deleted.is_(False))
    return stmt
```

Every read — including every module-specific query built on `list()` — inherits it. Not one
of them contains an explicit `is_deleted` filter.
`tests/shared/test_base_repository.py::test_soft_deleted_rows_are_excluded_from_every_read`
is what proves that claim, checking `get`, `get_by`, `list`, `count` **and** a module
query.

`include_deleted=True` is the explicit escape hatch for admin and audit reads — visible in
the call, as it should be.

### The DRY arithmetic

~40 lines of generic code instead of ~40 lines **per module**. Four modules today. And when
a bug is found in `list()`, it is fixed once.

---

## What this class deliberately does not do

| Not done | Why |
|---|---|
| `commit()` | The transaction boundary is the request (`shared/db/session.py`). A repository that commits makes atomic multi-entity operations impossible. |
| Business rules | `delete()` will happily soft-delete a reserved item. Deciding whether that is *allowed* is the service's job. |
| HTTP | Returns `None` for a missing row rather than raising 404. Whether absence is exceptional depends on the caller — a profile lookup wants 404, the registration uniqueness check wants `None`. |

---

## Design details worth knowing

**`ModelT` is bound to `Base`**, so `BaseRepository[UserRead, UUID]` (a Pydantic schema)
fails type-checking.

**`get()` does not use `session.get()`.** That consults the identity map and cannot apply
the soft-delete filter, so a previously-loaded deleted row would come back.

**`get_by()` raises on multiple matches.** Returning the first would hide a real
data-model bug: the caller assumed a uniqueness constraint that does not exist.

**`list()` takes SQLAlchemy expressions, not kwargs.** Kwargs can only express equality;
`FoodItem.expires_at > now` needs an expression. Typed `ColumnElement[bool]`, so passing a
raw string is a type error — there is no route from user input to SQL text.

**`list()` always has an ORDER BY.** Without one, Postgres may return rows in any order, so
page 2 can repeat or skip rows from page 1. Falls back to the primary key.

**`count()` counts over a subquery of `_base_select()`.** Duplicating the WHERE clause is
how you get a `total` that disagrees with the items returned — a bug that looks like a
pagination fault and is actually a copy-paste fault.

**`exists()` selects a literal, not the row.** It runs on every signup and does not need
the user object.

**`add()` flushes, not commits.** The INSERT is sent so server defaults populate and
constraint violations surface *there* — as an `IntegrityError` the service can translate
into a domain `ConflictError` — rather than at request commit, where the traceback no longer
says which operation caused it.

**`update()` takes the entity, not an id.** The caller has therefore already loaded it,
which means authorization and business rules have had a chance to run against real state. An
`update_by_id(id, **values)` helper would quietly invite skipping that. It also raises
`AttributeError` on an unknown field name — a typo would otherwise be a silent no-op, and
"my update didn't save" is a miserable bug to chase.

**`delete()` is soft or hard depending on the model.** One method, correct behaviour for
both, so services do not have to keep straight which entities are soft-deletable — and
cannot get it wrong.

**`hard_delete_by_id()` is named to be greppable.** It bypasses soft delete for GDPR
erasure. `grep hard_delete` finds every caller, which is exactly what a data-retention audit
needs.

**`session` is an intentional escape hatch.** A repository that blocks a legitimate window
function or CTE just gets bypassed. The rule is that it stays *inside* a repository
subclass — that boundary is what keeps services database-free.
