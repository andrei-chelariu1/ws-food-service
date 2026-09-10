# Food Waste App — a production-shaped FastAPI reference

A small backend where **donors** list surplus food and **recipients** request it. The domain is
deliberately minimal; the *structure* is the deliverable.

Built from the design in [`ARCHITECTURE.md`](ARCHITECTURE.md). Every directory has a `README.md`
explaining what belongs there **and why it is separated that way** — the rationale is the point.

```bash
make up          # postgres + redis + api, migrations applied on boot
open http://localhost:8000/docs
```

---

## Quickstart

**Requires:** Docker (with Compose v2). Nothing else — no local Python needed.

```bash
cp .env.example .env      # or `make env`
make up                   # build + start, follows logs
make ps                   # all three services healthy
make smoke                # curl the health endpoints
```

Then:

| | |
|---|---|
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| Liveness | http://localhost:8000/health/live |
| Readiness | http://localhost:8000/health/ready |
| Seeded admin | `admin@foodwaste.example` / `ChangeMe123!` (from `.env`) |

Run `make help` for every target. Tests and linters run locally too:

```bash
uv sync && uv run pytest -q     # 166 tests, no containers required
make lint                       # ruff check + ruff format --check + mypy strict
```

> **The seeded admin password is in `.env.example`.** It is a development convenience. Rotate it
> before anything real, and note that `assert_production_safe()` refuses to boot production with
> the sample `SECRET_KEY`.

---

## Try it in one paste

```bash
API=http://localhost:8000/api/v1

# 1. register a donor and a recipient
curl -s -X POST $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"donor@example.com","password":"Str0ngPass!","full_name":"D","role":"DONOR"}'
curl -s -X POST $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"recip@example.com","password":"Str0ngPass!","full_name":"R","role":"RECIPIENT"}'

# 2. log in
DONOR=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"donor@example.com","password":"Str0ngPass!"}' | jq -r .access_token)
RECIP=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"recip@example.com","password":"Str0ngPass!"}' | jq -r .access_token)

# 3. the donor lists surplus food
ITEM=$(curl -s -X POST $API/food-items -H "Authorization: Bearer $DONOR" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Bread","quantity":"5","unit":"loaves","expires_at":"2030-01-01T00:00:00Z",
       "pickup_location":"Main St 1"}' | jq -r .id)

# 4. the recipient requests it -> the item is reserved in the same transaction
DON=$(curl -s -X POST $API/donations -H "Authorization: Bearer $RECIP" \
  -H 'Content-Type: application/json' \
  -d "{\"food_item_id\":\"$ITEM\",\"note\":\"I can collect today\"}" | jq -r .id)

# 5. the donor accepts -> the recipient gets a notification
curl -s -X POST $API/donations/$DON/accept -H "Authorization: Bearer $DONOR" | jq .status
curl -s $API/notifications -H "Authorization: Bearer $RECIP" | jq '.items[].title'
```

---

## What is in here

**Runtime:** FastAPI · SQLAlchemy 2.0 async · PostgreSQL 17 · Redis 7 · Alembic · uv

| Feature | Where |
|---|---|
| Feature modules (vertical slices) | `app/modules/{users,food_items,donations,notifications}/` |
| Controller advice → RFC 9457 `problem+json` | `app/core/exceptions.py` |
| JWT access + refresh **with rotation and replay detection** | `app/core/security.py`, `app/modules/users/service.py` |
| RBAC (`DONOR` / `RECIPIENT` / `ADMIN`) | `app/shared/security/permissions.py` |
| Redis-backed tiered rate limiting | `app/core/rate_limit.py` |
| Cache-aside read caching | `app/shared/cache/cache.py` |
| Unit of work, one transaction per request | `app/shared/db/session.py` |
| Generic repository with centralised soft delete | `app/shared/repository/base_repository.py` |
| Async Alembic migrations + idempotent seed | `migrations/` |
| Multi-stage Docker, non-root, healthchecks | `Dockerfile`, `docker-compose.yml` |
| 166 tests, ruff + mypy strict clean | `tests/` |

Redis does three jobs: **rate-limit counters**, **read cache**, and the **JWT denylist**. They use
separate key prefixes, which is why invalidating the cache can never clear rate-limit counters or
un-revoke a logged-out token.

### Endpoints

<details>
<summary>31 routes under <code>/api/v1</code></summary>

```
POST   /auth/register            POST   /auth/login
POST   /auth/refresh             POST   /auth/logout

GET    /users/me                 PATCH  /users/me
POST   /users/me/password        GET    /users/{id}
GET    /users                    (admin)  PATCH /users/{id}/role  (admin)
DELETE /users/{id}               (admin)

GET    /food-items               POST   /food-items
GET    /food-items/mine          GET    /food-items/{id}
PATCH  /food-items/{id}          DELETE /food-items/{id}
POST   /food-items/{id}/cancel

POST   /donations                GET    /donations/mine
GET    /donations/received       GET    /donations/{id}
POST   /donations/{id}/accept    POST   /donations/{id}/decline
POST   /donations/{id}/complete  POST   /donations/{id}/cancel

GET    /notifications            GET    /notifications/unread-count
POST   /notifications/read-all   POST   /notifications/{id}/read
DELETE /notifications/{id}
```

</details>

---

## The request lifecycle

Worth reading once — it is the map for every other file.

```
                        HTTP request
                             │
   ┌─────────────────────────▼─────────────────────────────────────┐
   │ TrustedHost      reject forged Host headers first             │
   │ CORS             answer preflights early and cheaply          │
   │ SecurityHeaders  CSP/HSTS/nosniff — wraps even error bodies   │
   │ GZip             ≥1 KiB responses only                        │
   │ SlowAPI          Redis counter; fails OPEN                    │
   │ RequestId        adopt or mint X-Request-ID -> contextvar      │
   │ AccessLog        one structured line, innermost so timing is  │
   │                  the handler's                                │
   └─────────────────────────┬─────────────────────────────────────┘
                             │
      api.py  ── Depends(get_db) ──► session + transaction OPENED
        │       Depends(get_current_user) ──► verify JWT, check denylist,
        │                                     load the user FRESH
        │       Pydantic validates the body ──► 422 on failure
        ▼
     service.py   business rules. No HTTP. No SQL. Raises AppError.
        │              │
        │              └─► another module's SERVICE (never its repository)
        ▼
   repository.py  queries. Soft-delete filter applied centrally.
        │
        ▼
     PostgreSQL
                             │
            ┌────────────────┴─────────────────┐
        success                             AppError
            │                                  │
   get_db COMMITs                    get_db ROLLS BACK
            │                                  │
            ▼                                  ▼
     response model            controller advice ──► problem+json
                                                     + request_id
```

Three properties fall out of that diagram, and they are the reason for the whole structure:

1. **A raised exception rolls the transaction back.** The exception handler never has to remember
   to clean up — a partially applied write is not reachable.
2. **Services are plain Python objects.** No FastAPI import, so the central business rule is
   tested by calling a constructor. See [`docs/TESTING.md`](docs/TESTING.md).
3. **Every error leaves in the same envelope**, with a `request_id` that matches the
   `X-Request-ID` header and appears in the logs.

---

## Why the structure is what it is

### Feature modules, not layer packages

`app/modules/food_items/` holds the model, schemas, repository, service and router for food
items. The alternative — `models/`, `schemas/`, `services/` — means one feature change touches
five directories, and nothing stops `UserService` from importing `DonationRepository`.

**One hard rule, and it is checkable:**

```bash
grep -rn "^from app\.modules" app/shared/ app/core/    # -> no matches
```

`shared/` and `core/` never import a feature module. Authentication needs `User`, which would
break that — solved with a Protocol plus one line at the composition root, not by bending the
rule. See [`docs/SOLID.md`](docs/SOLID.md) § D.

### Services depend on services, never on another module's repository

`DonationService` calls `food_item_service.reserve(...)`, not `FoodItemRepository`. That single
choice is what makes *"expired food cannot be donated"* **structurally unbypassable** rather than
a convention: the invariant lives in `reserve()`, and there is no path to the rows that skips it.

Adding a fifth caller does not add a fifth place to re-check expiry.

### Errors are raised, not returned

Services raise `NotFoundError` / `ConflictError` / `BusinessRuleViolation` and never import
`HTTPException`. Handlers registered once translate them to `problem+json`. Adding an error type
is a three-line class; **no handler changes**.

### Everything swappable is a `Protocol`

`PasswordHasher`, `Cache`, `TaskDispatcher`, `TokenDenylist`, `UserAuthenticator` — each tiny,
each with a real second implementation used by the tests. `NullCache` is the proof that caching is
an optimisation here and not a correctness dependency: **if a no-op cache changed behaviour, that
would be a bug.**

### The fail-open / fail-closed rule

| Component | On Redis failure | Why |
|---|---|---|
| Cache | **open** | a miss costs a query |
| Rate limiter | **open** | otherwise `/health/live` 500s and Kubernetes restarts every pod — observed, not theorised |
| JWT denylist | **closed** | a missed revocation means a logged-out token still works |

**Availability controls fail open, security controls fail closed.** One sentence; it decides three
implementations.

---

## Where the trade-offs are written down

Every doc has a "known limitations" section. Read those before deploying anything.

| Doc | |
|---|---|
| [`docs/README.md`](docs/README.md) | index and reading order |
| [`docs/SOLID.md`](docs/SOLID.md) | each principle → a real file here, plus where DRY is deliberately **not** applied |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat by threat, including what is **not** defended |
| [`docs/DATABASE.md`](docs/DATABASE.md) | migrations, naming conventions, the unit of work |
| [`docs/CACHING.md`](docs/CACHING.md) | key design and invalidation |
| [`docs/RATE_LIMITING.md`](docs/RATE_LIMITING.md) | tiers, and the required endpoint signature |
| [`docs/TESTING.md`](docs/TESTING.md) | the two tiers and the rollback isolation |
| [`docs/adr/`](docs/adr/) | 4 decisions, 3 of which **deviate from `ARCHITECTURE.md`** |

`ARCHITECTURE.md` is the design brief and is left untouched. Where the implementation departs from
it, the departure is argued in an ADR rather than made silently.

### ⚠ Two things to know before deploying

1. **Put a proxy in front that overwrites `X-Forwarded-For`** (`proxy_set_header
   X-Forwarded-For $remote_addr` — *set*, not append). Exposed directly, IP-based rate limits are
   bypassable. The application cannot enforce this;
   [`docs/RATE_LIMITING.md`](docs/RATE_LIMITING.md) has the per-ingress detail.
2. **Notification delivery is not durable.** `BackgroundTasks` has no retries and no persistence;
   a restart loses the delivery attempt. The notification *row* is always committed, so nothing is
   invisible to the user — but do not wire a real email channel without reading
   [ADR 0004](docs/adr/0004-backgroundtasks-behind-protocol.md).

---

## Verification checklist

Each of these was run against a live stack. Steps **5, 7 and 9 are the acceptance gate** — they
are the ones that look fine while being broken.

| # | Check | Expected |
|---|---|---|
| 1 | `make up` | all three services healthy |
| 2 | `make current` | revision `0002`; `make psql` → `\dt` lists 4 tables + `alembic_version` |
| 3 | `curl /health/ready` | `200`. Then `docker compose stop redis` → `503`, while `/health/live` stays `200` |
| 4 | open `/docs` | every router present, "Authorize" works |
| 5 | **auth flow** | register → login → `/users/me` `200`; refresh returns a new pair; **replaying the old refresh token → `401`**; after logout the access token → `401` |
| 6 | RBAC | a `DONOR` calling `GET /users` → `403` in `problem+json` |
| 7 | **rate limit** | 8 bad logins → `401 ×5` then `429` with `Retry-After`; counters visible via `redis-cli --scan --pattern '*rl:*'` |
| 8 | controller advice | `GET /food-items/00000000-…` → `404` whose `request_id` matches `X-Request-ID` and appears in `make logs`; an invalid body → `422` in the same envelope |
| 9 | **business rule** | request a donation for an expired item → `409`, and **no donation row exists** (check via `make psql`) — that proves the unit-of-work rollback |
| 10 | cache | `GET /food-items` twice → a cache hit at `LOG_LEVEL=DEBUG`; create an item → it appears immediately, proving invalidation |
| 11 | tests | `make test` → 166 passed. Also green against Postgres via `TEST_DATABASE_URL` |
| 12 | static checks | `make lint` → ruff clean, `ruff format --check` clean, mypy strict clean on 59 files |
| 13 | docs | every directory has a `README.md`; the verification commands in `docs/SOLID.md` print what it says they print |

Step 7's `429` and step 9's rollback are worth doing by hand at least once. Step 9 in particular:
a `409` with the row still committed would pass a casual look and be a genuine data-integrity bug.

---

## Layout

```
app/
├── core/       app-wide wiring: config, security primitives, logging,
│               exceptions, middleware, rate limiting, Redis.
│               Knows nothing about the domain.
├── shared/     reusable plumbing: db, repository, schemas, cache,
│               security dependencies, utils.  MUST NOT import modules.
└── modules/    the features. Each owns its model → schema → repository
                → service → router.  Talks to peers via their SERVICES.

migrations/  async Alembic; handwritten initial schema + idempotent seed
scripts/     entrypoint: wait for postgres -> upgrade head -> uvicorn
tests/       unit (no DB) + integration (real app), rollback-isolated
docs/        the cross-cutting rationale and the ADRs
```

Start with [`app/README.md`](app/README.md) for the dependency rule, then
[`app/modules/README.md`](app/modules/README.md) for the module contract.
