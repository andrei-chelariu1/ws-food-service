# Testing

```bash
pytest -q                        # 166 tests, no containers needed
pytest -q --cov=app              # with coverage
TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/testdb pytest -q
```

The default suite needs **no Postgres, no Redis, no `.env`**. That is a design property, not a
convenience: a suite with setup requirements is a suite people skip.

---

## The two tiers

| | Unit (`test_service.py`) | Integration (`test_api.py`) |
|---|---|---|
| Under test | one service's business rules | the whole request path |
| Built by | calling the constructor directly | `httpx.AsyncClient` over the real ASGI app |
| Goes through | nothing else | middleware, dependencies, exception handlers, DB |
| Cost | milliseconds | tens of milliseconds |
| Catches | wrong rule, wrong ordering, wrong error | wrong status code, missing dependency, **authorization gap** |

**Most rules are tested at tier 1. Tier 2 exists to prove the wiring, not to re-test every
branch.** Re-testing every rule through HTTP is how suites get slow and vague — a 400 tells you
much less than an assertion on the exception type.

### Tier 1 is only possible because of the architecture

```python
service = DonationService(
    repository=DonationRepository(db_session),
    food_item_service=food_item_service,
    notification_service=notification_service,
)
with pytest.raises(BusinessRuleViolation):
    await service.request_donation(item_id=expired.id, user=recipient)
```

No app, no client, no token, no clock mocking. That works because services take their
collaborators as constructor arguments and import no FastAPI. If they called `Depends()`
internally or reached for a global session, **every one of these tests would need an HTTP
request**. See `docs/SOLID.md` § D.

### Counts, as they actually stand

| File | Tests | What it is really for |
|---|---|---|
| `modules/users/test_service.py` | 21 | registration, authentication, refresh rotation, replay |
| `modules/users/test_api.py` | 24 | status codes, RBAC, the problem+json envelope |
| `modules/food_items/test_service.py` | 19 | the state machine and the expiry invariant |
| `modules/food_items/test_api.py` | 19 | pagination, ownership, cache-aside wiring |
| `modules/donations/test_service.py` | 14 | **the cross-module rule** — the acceptance gate |
| `shared/test_base_repository.py` | 15 | soft delete, `get`-returns-`None`, the generic CRUD contract |
| `shared/test_pagination.py` | 20 | `PageParams` bounds, `build_cache_key` determinism |
| `shared/test_config.py` | 13 | production safety assertions, list parsing |
| `shared/test_rate_limit_key.py` | 13 | key selection + the structural route guard |
| `shared/test_security_headers.py` | 8 | strict API CSP vs. the scoped `/docs` exception |

---

## Isolation: a transaction that is rolled back

```python
async with db_engine.connect() as connection:
    transaction = await connection.begin()
    session = async_sessionmaker(bind=connection, ...)()
    yield session
    await transaction.rollback()     # <- the whole cleanup story
```

| Alternative | Why not |
|---|---|
| `TRUNCATE` between tests | more commands, and it must be maintained as tables are added |
| drop/recreate the schema per test | slow, and it invalidates connections |
| a cleanup fixture | **skipped when a test fails mid-way** — exactly when you need it |

Rollback has none of those failure modes: there is no cleanup step to forget, order does not
matter, and a test that raises halfway leaves nothing behind.

**The subtlety worth understanding:** the app under test *does* call `commit()` — that is the
unit of work doing its job, and we want it exercised. Binding the session to an outer
connection-level transaction turns that commit into a nested one, which the outer rollback still
discards. So the code runs its real commit path and the database still ends the test clean.

`get_db` is overridden to hand the app this session. Without that override the app would open its
own connection, and nothing the test set up would be visible to it.

---

## SQLite by default, Postgres on demand

The default is `sqlite+aiosqlite:///:memory:` with `StaticPool` (so every connection sees the
same in-memory database).

**What SQLite cannot tell you — stated plainly:**

| Not covered | Consequence |
|---|---|
| `SELECT ... FOR UPDATE` | **ignored** — the row-lock in `reserve()` is untested here, so the double-reservation race is not proven |
| Partial indexes | `WHERE is_deleted = false` uniqueness is not enforced |
| `NUMERIC` semantics | quantity arithmetic differs |
| Some CHECK constraints | enum violations may pass that Postgres rejects |
| `TIMESTAMPTZ` | offset handling is looser |

So the same suite runs against Postgres by setting `TEST_DATABASE_URL` — same tests, same
assertions, real engine. **CI should run both.** The SQLite pass is the fast feedback loop; the
Postgres pass is the one that can actually fail on a constraint.

The one thing neither pass proves is genuine concurrency: both run the suite serially, so the
`FOR UPDATE` lock is exercised for syntax but never contended. Proving it needs two concurrent
transactions — the manual recipe is in `docs/DATABASE.md`.

Both were run for this repo: **166 passed on SQLite and on Postgres.**

---

## The fake pyramid — and why each fake is legitimate

| Fake | Replaces | Why it is not a stub |
|---|---|---|
| `NullCache` | `RedisCache` | a real `Cache` implementation. **If the suite behaved differently with it, the code would be depending on the cache for correctness — a bug.** |
| `NullTokenDenylist` | `RedisTokenDenylist` | revoking nothing is a valid contract; rotation tests that need revocation use `FakeRedis` |
| `ImmediateTaskDispatcher` | `BackgroundTasksDispatcher` | runs the task inline, so a test can assert the notification exists |
| `FakeRedis` | Redis | an in-memory dict with TTL and `scan_iter`, for the denylist tests that need real state |

**`NullCache` is a substitutability test that doubles as a design check** — see `docs/SOLID.md`
§ L.

`BCRYPT_ROUNDS=4` in the test environment. Cost 12 is ~250ms per hash; the suite hashes
hundreds of times. Four rounds is cryptographically useless and functionally identical — the
tests are asserting that hashing and verification agree, not that bcrypt is slow. Production
cost lives in `.env`, and `test_config.py` asserts production refuses a weak value.

---

## The fixtures

```
settings          (session)   one Settings, BCRYPT_ROUNDS=4
  db_engine       (function)  in-memory engine + create_all
    db_session    (function)  the rollback-scoped session
      app         (function)  create_app() with get_db and get_redis overridden
        client    (function)  AsyncClient over ASGITransport — no network
      *_service   (function)  services wired to db_session
      make_user / make_food_item          data builders
      as_current_user / auth_headers      identity builders
```

`db_engine` is **function-scoped**, which looks wasteful and is not: in-memory `create_all` is
milliseconds, and the session-scoped version needs a session-scoped event loop, which is how you
get `ScopeMismatch` errors. (That was not theoretical — the first version raised exactly that.)
**Correct and simple beat a saving that does not matter.**

`client` uses `ASGITransport`, so requests go straight into the app in-process. No port, no
sockets, nothing to conflict with a running dev server.

### Factories are fixtures, not a `factories.py`

The plan for this repo listed a `tests/factories.py`. It does not exist, deliberately: every
builder needs the `db_session` to persist into, so a plain module would have to take it as an
argument at every call site. As fixtures, `make_user`/`make_food_item` already have it.

```python
async def test_owner_can_cancel(make_user, make_food_item, food_item_service):
    owner = await make_user(role=Role.DONOR)
    item = await make_food_item(owner_id=owner.id, status=FoodItemStatus.AVAILABLE)
```

Every field defaults to something valid, so a test names **only what it is about**. That is what
makes the tests readable: a test that sets `expires_at` in the past is visibly a test about
expiry.

`as_current_user` builds a `CurrentUser` for unit tests; `auth_headers` mints a real signed token
for integration tests. Two identity paths because the tiers need different things — the unit tier
should not have to know JWTs exist.

---

## Tests that guard the architecture, not the behaviour

Two in `test_rate_limit_key.py` are unusual and worth copying:

```python
def test_every_rate_limited_route_declares_request_and_response() -> None:
    """slowapi requires BOTH parameters. Omitting `response` 500s the endpoint."""

def test_the_structural_guard_is_not_vacuous() -> None:
    """...because the first two versions of the guard matched zero routes."""
```

The first walks the real route table and enforces a rule the type checker cannot express. It
exists because that bug **shipped**: `RATE_LIMIT_ENABLED=false` makes the decorator a no-op, so
the suite was structurally blind to it.

The second is the lesson. The guard was silently vacuous twice — first because
`from __future__ import annotations` makes annotations strings, then because this FastAPI version
stores `include_router` results as lazy `_IncludedRouter` objects. Both times it passed while
checking nothing.

> **If you write a reflective test, assert that it found something.** A green test that inspects
> an empty list is worse than no test, because it buys confidence it has not earned.

`test_security_headers.py` does the same thing for the CSP: it asserts the CDN hosts named in the
policy are still the ones the served `/docs` HTML references, so a FastAPI change surfaces as
"narrow this policy" rather than a stale allowance nobody revisits.

---

## Coverage, and what it does not tell you

`--cov=app` is available and no threshold is enforced. Deliberate: a coverage gate mostly
teaches people to write tests that execute lines.

The tests that matter here are the ones from the plan's acceptance gate — refresh-token replay,
the rate-limit 429, and the expired-item donation rollback. **All three are behaviours that look
fine while being broken**, which is a property coverage cannot measure.

---

## Known gaps

1. **No true concurrency test.** `FOR UPDATE` is exercised, never contended.
2. **No load or soak testing.** Nothing establishes the connection-pool sizing.
3. **`notifications` has no dedicated test file.** It is covered transitively through the
   donation flow, which is where it is actually used.
4. **The Postgres pass is manual** — one environment variable, but nobody is forced to run it.
   That belongs in CI.
5. **No migration test.** `alembic upgrade head` runs in the container entrypoint and is verified
   by hand; nothing asserts `downgrade` then `upgrade` round-trips.
