# `app/modules/` — Feature Modules (Vertical Slices)

## Purpose

Each subdirectory is a **self-contained feature**, holding every layer it needs:

```
users/
├── models.py       SQLAlchemy entity
├── schemas.py      Pydantic DTOs
├── repository.py   queries
├── service.py      business rules
├── api.py          HTTP routes + DI wiring
└── exceptions.py   domain errors
```

## Why package-by-feature and not package-by-layer

The alternative is `all_models/`, `all_services/`, `all_routers/`. Compare what it takes
to add a field to a food item:

| | package-by-layer | package-by-feature |
|---|---|---|
| Files touched | 5, in 5 distant directories | 4, in one directory |
| Reviewer's job | reconstruct the feature from fragments | read one folder |
| Deleting the feature | hunt through every layer, miss something | `rm -rf food_items/` + 3 known lines |
| Merge conflicts | everyone edits `all_services/` | teams work in separate folders |
| Extracting a service later | untangle it from four shared files | lift the directory |

Layering still exists — it is *inside* each module. Same discipline, better locality.

---

## The layer rules

| File | May do | May **not** do |
|---|---|---|
| `api.py` | routing, status codes, DTO mapping, auth dependencies | business logic, SQL |
| `service.py` | business rules, orchestration | HTTP, SQLAlchemy, `commit()` |
| `repository.py` | queries | business rules, HTTP, `commit()` |
| `models.py` | columns, relationships, constraints, pure derived properties | queries, hashing, rules |
| `schemas.py` | validation, serialisation | database access |
| `exceptions.py` | domain error definitions | anything else |

### Three rules that are easy to break and expensive to fix

**1. Services never call `commit()`.** The transaction boundary is the request
(`shared/db/session.py`). This is what makes "accept a donation" — two tables plus a
notification row — atomic. A service that commits makes partial failure possible and
nobody notices until production data is inconsistent.

**2. Services never raise `HTTPException`.** They raise domain errors;
`core/exceptions.py` translates. Verify:

```bash
grep -rn "^from fastapi\|HTTPException" app/modules/*/service.py    # -> no matches
```

**3. Route handlers are one or two lines.** If one grows, the logic belongs in the
service. That is the review heuristic, and it is easy to check.

---

## Cross-module communication — the rule that carries real weight

> A module may import another module's **service**. Never its **repository**.

`app/modules/donations/service.py`:

```python
def __init__(self, repository, food_item_service, notification_service):
                          ▲                ▲
                          │                └── another module's SERVICE
                          └── this module's own repository
```

### Why this is not stylistic

`DonationService` must reserve a food item. Going through `FoodItemService.reserve()`
means it necessarily passes:

* the expiry check (`expires_at` in the past → `FoodItemExpiredError`);
* the state-machine check (`AVAILABLE` → `RESERVED` only);
* the row lock (`SELECT ... FOR UPDATE`) that makes it safe under concurrency.

With a `FoodItemRepository` it could write `status = RESERVED` directly and skip all
three. **The rule is what turns "expired food cannot be donated" from usually-true into
guaranteed.**

Secondary benefits: rules stay owned by the module that defines them, and
`grep -rn "food_item_service" app/modules/donations/` lists every cross-module
interaction — so extracting `food_items` into its own service means replacing one
injected object with an HTTP client and changing nothing else.

### The one legitimate exception

`donations/repository.py` imports the `FoodItem` **model** to `JOIN`. Sharing a *schema*
is what one database means; sharing *behaviour* is what creates coupling. The rule is
about behaviour.

---

## The modules

| Module | Owns | Notable |
|---|---|---|
| `users` | identity, authentication, roles | JWT with rotation + denylist; satisfies the `UserAuthenticator` Protocol structurally |
| `food_items` | listings and their lifecycle | **owns the expiry invariant**; state machine as data; cache-aside on the public list |
| `donations` | the handover between two parties | depends on two sibling services; two-party authorization (donor vs recipient) |
| `notifications` | in-app messages | `notify_*` emission API; `TaskDispatcher` Protocol for background delivery |

Read them in that order — each builds on the previous one.

---

## Adding a module

1. Create the six files plus a `README.md`.
2. **Add the model import to `migrations/env.py`.** Non-optional: without it,
   `alembic revision --autogenerate` will emit `DROP TABLE` for your new table.
3. `api_router.include_router(...)` in `app/api_router.py`.
4. `make revision m="add <name>"`, then read the output against the checklist in
   `migrations/script.py.mako`.
5. `tests/modules/<name>/` — a `test_service.py` (fakes, no database) and a
   `test_api.py` (real app).

Steps 2–4 are the only edits outside the new directory.
