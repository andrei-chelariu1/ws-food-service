# `app/shared/` — Reusable Plumbing

## Purpose

Generic, domain-agnostic building blocks that every feature module composes. The
equivalent of a `commons`/`core` module in a multi-module Maven or Gradle project.

| Directory | Provides |
|---|---|
| `db/` | Declarative base + naming convention, async session and Unit of Work, model mixins |
| `repository/` | `BaseRepository[Model, Id]` — generic async CRUD |
| `schemas/` | `ApiModel`, `Page[T]`, `ProblemDetail`, reusable OpenAPI error fragments |
| `security/` | `CurrentUser`, the auth dependencies, `require_roles()`, `require_ownership()` |
| `cache/` | `Cache` Protocol, `RedisCache`, `NullCache` |
| `utils/` | `PageParams`, `utcnow()` |

## The one hard rule

**Nothing under `shared/` may import from `app/modules/`.**

```bash
grep -rn "app\.modules" app/shared/    # -> no matches
```

Break this rule and `shared/` is no longer reusable plumbing — it is coupled to a feature,
and you cannot lift it into another project or reason about it independently.

The rule was genuinely hard to keep in exactly one place, and how that was solved is the
most instructive thing here — see below.

---

## The interesting problem: authentication

Authentication is cross-cutting, so it belongs in `shared/`. But verifying a token means
loading a user, and `User` lives in `app/modules/users/`. The naive implementation violates
the rule.

**The solution (Dependency Inversion at the composition root):**

```
shared/security/principal.py       declares CurrentUser + UserAuthenticator Protocol
                                   (framework-free — no FastAPI import)
        ▲                                          ▲
        │ depends on the abstraction               │ depends on the abstraction
        │                                          │
shared/security/dependencies.py            modules/users/service.py
  (FastAPI wiring, placeholder provider)     (UserService satisfies the Protocol
                                              STRUCTURALLY — never imports it)
                        ▲
                        │
                  app/main.py
   app.dependency_overrides[get_user_authenticator] = provide_user_authenticator
```

Neither side imports the other. `main.py` — the only file allowed to know about everything —
joins them with one line.

Two details worth noting:

* `principal.py` is separate from `dependencies.py` specifically so that a service returning
  a `CurrentUser` does not transitively import FastAPI. Without the split, "services are
  framework-independent" would be technically false.
* The placeholder provider **raises** rather than returning a permissive default. A silent
  fallback that authenticated nobody (or worse, everybody) would be a security hole hidden
  behind a convenience.

---

## Why each piece exists

### `db/base.py` — the naming convention

The highest-leverage seven lines of SQLAlchemy configuration there is. Without it, Postgres
invents constraint names and SQLAlchemy leaves some anonymous, so
`alembic revision --autogenerate` cannot emit a `DROP CONSTRAINT` for something whose name
it cannot predict — and names differ between your test database and production, so a
migration that passed CI fails on deploy.

### `db/session.py` — the Unit of Work

`get_db()` yields one session per request and commits on success / rolls back on error.
That single rule makes requests **atomic**: accepting a donation writes two tables and a
notification row, and a failure halfway cannot leave a reserved item with no donation.

The consequence, which is the rule most often broken: **services never call `commit()`.**

### `repository/base_repository.py` — DRY where forgetting has a cost

Seven identical CRUD methods for four entities. But the real payoff is the soft-delete
filter: every read goes through `_base_select()`, so no query can forget
`WHERE is_deleted = false`. Written by hand at each call site, the first omission silently
exposes deleted rows to users.

`tests/shared/test_base_repository.py::test_soft_deleted_rows_are_excluded_from_every_read`
is what proves that claim.

### `schemas/base_schema.py` — security by construction

`extra="forbid"` on `ApiModel` means an unknown request field is a 422, not a shrug. That
blocks both typos (`{"emial": ...}` creating a user with no email) and mass assignment
(smuggling `{"role": "ADMIN"}` into a schema that does not declare it).

Response DTOs are **allowlists**. `UserRead` has no `hashed_password` field, so the hash
cannot be serialised — not by this endpoint, not by a future one, and not after someone adds
a column to the model. Compare a denylist (`exclude={"hashed_password"}`), which protects
only the places somebody remembered to write it.

### `security/permissions.py` — two levels of authorization

| Level | Where | Example | Why there |
|---|---|---|---|
| Coarse (role) | Route dependency | `require_roles(Role.ADMIN)` | Needs only the token; visible in the router, runs before the handler |
| Fine (ownership) | Inside the service | `require_ownership(user, item.owner_id)` | Needs the loaded row |

Ownership **cannot** be a route dependency: `require_roles` only sees the token, and "is this
*your* item?" needs the item. This is the IDOR defence, and it is the most commonly missed
check in real APIs — valid token, well-formed id, existing row, and the edit succeeds.

### `cache/cache.py` — the clearest DIP example in the project

`FoodItemService` declares `cache: Cache` and never imports `redis`. Tests inject
`NullCache` and the service behaves identically — which is not a testing trick but the
*correctness property* that makes caching safe: a cache is by definition allowed to forget
everything. If a no-op implementation broke behaviour, the code would be relying on the
cache for correctness, which is a bug.

### `utils/datetime_utils.py` — why a one-line wrapper earns a module

1. **Testability.** "Expired food cannot be donated" is a rule *about time*. One seam means
   a test patches one function instead of monkeypatching a stdlib global or sleeping.
2. **Correctness.** `datetime.utcnow()` returns a *naive* datetime; comparing it with a
   timezone-aware value from the database raises `TypeError` in production on the one path
   nobody tested. One sanctioned source means every datetime in the system is aware.

Rule: no `datetime.now()` anywhere else in `app/`.

### `utils/pagination.py` — the cap is a security control

`MAX_PAGE_SIZE = 100` is not an aesthetic limit. Unbounded `size` is a trivial
denial-of-service: one request that materialises a million ORM objects. Enforced by Pydantic
at the edge, so no downstream code can bypass it.
