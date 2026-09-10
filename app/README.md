# `app/` — The Application

## Purpose

Everything that is the running service. Three top-level concerns:

| Directory | Role | Knows about the domain? |
|---|---|---|
| `core/` | App-wide wiring: config, logging, security primitives, middleware, error handlers | **No** |
| `shared/` | Reusable, domain-agnostic plumbing: base repository, DB session, cache, auth guards | **No** |
| `modules/` | Feature slices: `users`, `food_items`, `donations`, `notifications` | **Yes** — this is where it lives |

Plus two files: `main.py` (the composition root) and `api_router.py` (route aggregation).

---

## The layers inside a module

```
HTTP request
    │
    ▼
api.py          ← routing, status codes, DTO mapping, auth dependencies
    │              (no business logic, no SQL)
    ▼
service.py      ← business rules, orchestration, transactions
    │              (no HTTP, no SQL)
    ▼
repository.py   ← queries
    │              (no business rules, no HTTP)
    ▼
models.py       ← SQLAlchemy entities
    │
    ▼
PostgreSQL
```

---

## The dependency rule

**Arrows point one way only.** Verify each of these; they are the architecture:

```bash
# 1. shared/ must never import a feature module
grep -rn "app\.modules" app/shared/                      # -> no matches

# 2. core/ must never import a feature module
grep -rn "app\.modules" app/core/                        # -> no matches

# 3. services must never import the web framework
grep -rn "^from fastapi\|^import fastapi" app/modules/*/service.py   # -> no matches

# 4. services must never touch SQLAlchemy directly
grep -rn "^from sqlalchemy\|session\." app/modules/*/service.py      # -> no matches

# 5. a module must never import another module's repository
grep -rn "modules\.[a-z_]*\.repository" app/modules/*/service.py     # -> no matches
```

Rule 5 has one legitimate exception, and it is worth understanding: `donations/repository.py`
imports the `FoodItem` **model** in order to `JOIN`. Sharing a *schema* is what having one
database means; sharing *behaviour* is what creates coupling. The rule is about behaviour.

### Why the rules matter, concretely

| Rule | What breaks without it |
|---|---|
| 1, 2 | `shared/` stops being reusable — it is coupled to one feature, so a new project cannot take it |
| 3 | Services become untestable without a request, and unusable from a CLI or worker |
| 4 | Services become untestable without a database; the repository abstraction is bypassed and cross-cutting query rules (soft delete) get skipped |
| 5 | Two modules write the same table, so invariants like "expired food cannot be donated" become *usually* true |

Rule 5 is the load-bearing one. See [`modules/README.md`](modules/README.md).

---

## Request lifecycle

```
TrustedHostMiddleware      reject forged Host headers
    ↓
CORSMiddleware             answer preflights early
    ↓
SecurityHeadersMiddleware  wraps everything, so even errors get headers
    ↓
GZipMiddleware
    ↓
RequestIdMiddleware        mint/adopt X-Request-ID -> ContextVar
    ↓
AccessLogMiddleware        start timer
    ↓
SlowAPIMiddleware          global rate limit
    ↓
route matching
    ↓
dependencies               get_db (opens transaction), get_current_user, PageParams
    ↓
@limiter.limit(...)        per-route rate limit (sees the authenticated user)
    ↓
handler -> service -> repository -> database
    ↓
get_db commits            (or rolls back if anything raised)
    ↓
response serialised through the DTO
    ↓
AccessLogMiddleware        log method, path, status, duration, user
    ↓
BackgroundTasks            notification delivery runs here, after the response
```

If anything raises, `core/exceptions.py` produces an `application/problem+json` response
carrying the same `request_id` — so a user's bug report is greppable in the logs.

---

## Where to start reading

1. `core/config.py` — every knob the system has
2. `core/exceptions.py` — the error contract (the controller-advice pattern)
3. `shared/repository/base_repository.py` — the DRY payoff
4. `modules/users/service.py` — a real service, and the auth design
5. `modules/donations/service.py` — the cross-module rule, and why it is not stylistic
6. `main.py` — how it is all wired together

---

## Adding a feature module

1. `mkdir app/modules/<name>` with the six files (`models`, `schemas`, `repository`, `service`, `api`, `exceptions`) plus a `README.md`
2. Add the model import to `migrations/env.py` — **required**, or autogenerate will emit `DROP TABLE` for it
3. Add `api_router.include_router(...)` in `app/api_router.py`
4. `make revision m="add <name>"`, then read the generated migration against the checklist in `migrations/script.py.mako`
5. Add `tests/modules/<name>/`

Note that steps 2–4 are the only places outside the new directory that change. That is the
property vertical slicing buys.
