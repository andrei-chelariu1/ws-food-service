# `tests/` — Test Suite

**158 tests. `pytest` needs no Docker, no Redis, and no `.env`.**

```
tests/
├── conftest.py       fixtures: rollback-scoped session, app, fakes, data factories
├── shared/           base repository, pagination, config, rate-limit logic
└── modules/
    ├── users/        test_service.py (unit) + test_api.py (integration)
    ├── food_items/   test_service.py + test_api.py
    └── donations/    test_service.py  <- THE most important file
```

```bash
make test                                                    # in the container, with coverage
pytest -q                                                    # on the host
pytest tests/modules/donations -v                            # one module
TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/testdb pytest   # against Postgres
```

---

## The two-tier strategy

### Tier 1 — unit tests (`test_service.py`)

Construct a service directly with fakes. No HTTP, no auth, no event loop fixture. Milliseconds
each.

The showcase is `tests/modules/donations/test_service.py::test_expired_food_cannot_be_donated`
— the rule the whole application exists to protect, tested with **no HTTP client, no
authentication, no mocking of `datetime.now`, and no Redis**. Just three objects and an item
whose `expires_at` is in the past.

That is only possible because services receive collaborators through their constructor and
raise domain exceptions rather than HTTP ones. If a service imported `HTTPException`, this test
would have to assert on a status code — testing the transport instead of the rule.

### Tier 2 — integration tests (`test_api.py`)

Drive the real ASGI app: real routers, real middleware, real exception handlers, real dependency
graph. These verify what a unit test structurally cannot — status codes, the problem+json
envelope, authorization wiring, response headers.

**Most rules are cheaper and clearer at tier 1. Tier 2 exists to prove the wiring, not to
re-test every branch.**

---

## How isolation works

Each test runs inside a transaction that is **rolled back** afterwards:

```python
connection = await db_engine.connect()
transaction = await connection.begin()
session = async_sessionmaker(bind=connection)()  # bound to the CONNECTION
yield session
await transaction.rollback()  # discards everything
```

The app's own `commit()` then commits a *nested* transaction, which the outer rollback still
discards.

Versus the alternatives: `TRUNCATE` per test is slow; drop-and-recreate-schema is slower;
both need a cleanup step that a failing test can skip. Rollback is one command, works on every
path, and makes tests order-independent.

---

## What is overridden, and what is deliberately not

| Dependency | Override | Why |
|---|---|---|
| `get_db` | rollback-scoped session | isolation |
| `get_redis` | `FakeRedis` | no server needed |
| `get_food_item_service` | injects `NullCache` | the `Cache` Protocol paying off |
| **everything else** | **not overridden** | routers, middleware, handlers, services are all under test |

`auth_headers` mints a **real** token with the real token service, so signature, `typ`, `jti`,
the denylist check and the database lookup are exercised on every authenticated request in the
suite. Faking authentication would be faster and would test considerably less.

`BCRYPT_ROUNDS=4` in the test settings is the single most valuable line in `conftest.py`: at the
production cost of 12 every login pays ~250ms; at 4 it is ~2ms. Safe only because the cost
factor is *configuration* — and `assert_production_safe()` refuses to boot with rounds < 12 in
production.

---

## Factories, not fixed fixtures

```python
donor = await make_user(role=Role.DONOR)
item = await make_food_item(owner_id=donor.id, expires_in_hours=-1)  # already expired
```

A fixed `user` fixture forces every test to share one user, so the moment a test needs two — an
owner and a stranger, i.e. **every authorization test** — you need `user`, `other_user`,
`admin_user`... A factory handles all of it, and each test states the data it actually needs.

`expires_in_hours` accepts negatives, which is how the central time-based rule is tested with
**no clock manipulation at all** — just data.

---

## Known gaps, stated rather than hidden

A test suite that quietly does not cover something is worse than one that says so.

### 1. SQLite is the default backend

Zero setup, so `pytest` works on a laptop. But SQLite:

* **ignores `SELECT ... FOR UPDATE`** — so the *concurrent* double-reservation case is not
  proven, only the sequential one;
* enforces CHECK constraints differently;
* lacks real partial-index and NUMERIC semantics.

**Mitigation:** the same suite runs against Postgres via `TEST_DATABASE_URL`. Both are verified
green. CI should run both.

### 2. Migrations are not exercised

`db_engine` builds the schema with `Base.metadata.create_all`, not `alembic upgrade head`. Fast,
and it works on SQLite — but **a migration that disagrees with the models would not be caught
here.**

**Mitigation:** a CI job must run `alembic upgrade head` against Postgres. That is exactly how
the doubled-constraint-name bug in `0001` was found. Treat it as required, not optional.

### 3. Rate limiting is disabled in the suite

The `Limiter` is a module-level singleton whose storage and enabled flag are fixed at import,
so driving a real 429 in-process needs a live Redis.

**Mitigation, in two parts:**

* the *decision logic* (`rate_limit_key`, `get_client_ip`) is unit-tested in
  `tests/shared/test_rate_limit_key.py`;
* a **structural guard** in the same file walks the real route table and asserts every
  rate-limited endpoint declares `request: Request` *and* `response: Response`. That closes a
  gap that had already shipped a 500 on `POST /auth/register` — the decorator is a no-op when
  the limiter is disabled, so the missing parameter was invisible to every other test.
* the end-to-end 429 is verified by curl against the running stack (step 7 of the README
  checklist).

Note `test_the_structural_guard_is_not_vacuous`: a structural test that silently matches zero
routes passes forever while checking nothing. The first version of that guard did exactly that —
twice, for two different reasons (string annotations under `from __future__ import annotations`,
then lazy `_IncludedRouter` route objects). **If you write a reflective test, assert that it
found something.**

### 4. `BackgroundTasks` delivery is not asserted

`ImmediateTaskDispatcher` drops tasks. Tests assert the notification **row** — the durable half
— rather than the dropped side effect. Testing the drop would test nothing.

---

## Tests that exist because a bug shipped

Worth reading, because each documents a failure mode that reasoning alone did not catch:

| Test | The bug it now prevents |
|---|---|
| `test_rate_limit_key.py::test_every_rate_limited_route_declares_request_and_response` | 500 on every rate-limited endpoint once the limiter was enabled |
| `test_rate_limit_key.py::test_the_structural_guard_is_not_vacuous` | the guard above silently checking nothing |
| `test_config.py::test_seed_admin_email_is_accepted_by_the_login_schema` | the seeded admin being unable to log in (`.local` TLD rejected by `EmailStr`) |
| `test_api.py::test_admin_can_change_a_role` | `MissingGreenlet` when reading `updated_at` after an UPDATE |
| `test_service.py::test_notification_is_created_for_the_donor` | `Mapped[Enum]` on a `String` column returning `str` after a reload |
| `test_base_repository.py::test_soft_deleted_rows_are_excluded_from_every_read` | a forgotten filter leaking deleted rows |
