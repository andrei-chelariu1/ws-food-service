# `app/core/` — Application-Wide Wiring

## Purpose

Cross-cutting concerns that every module needs and no module owns: configuration,
logging, security primitives, middleware, rate limiting, error handling, the Redis
pool.

## What belongs here / what does not

| Belongs | Does not belong |
|---|---|
| Reading configuration | Anything mentioning `User`, `FoodItem`, `Donation` |
| Logging setup | Business rules |
| Hashing a password, signing a JWT | Deciding *who* may do *what* (→ `shared/security/`) |
| HTTP middleware | Queries (→ `shared/repository/`, module repositories) |
| Exception classes and handlers | Reusable data-access helpers (→ `shared/`) |

**Hard rule: `core/` must never import `app.modules`.** Verify:

```bash
grep -rn "app\.modules" app/core/    # -> no matches
```

## Why it is separated from `shared/`

Both are domain-agnostic, so the distinction is worth stating: `core/` is about the
**application as a process** — it is configured once at startup and there is exactly one
of each thing (one settings object, one logging config, one Redis pool, one middleware
stack). `shared/` is about **reusable building blocks** that modules instantiate many
times (a repository per module, a cache per service, a session per request).

Practical test: if it is constructed in `create_app()` or in `lifespan`, it belongs in
`core/`. If a module constructs it, it belongs in `shared/`.

---

## Files

### `config.py` — Single Responsibility

The **only** place that reads environment variables. Everything else receives a
`Settings` object.

* **Fail fast.** Pydantic validates at startup. `DB_POOL_SIZE=ten` crashes on boot with
  a clear message instead of raising a `TypeError` under load three hours later.
* **Testable.** Tests construct `Settings(...)` directly. No monkeypatching `os.environ`.
* **`assert_production_safe()`** refuses to boot with the sample `SECRET_KEY`, `DEBUG=true`,
  `ALLOWED_HOSTS=["*"]`, or `BCRYPT_ROUNDS < 12` when `ENVIRONMENT=production` — the single
  most common real-world deployment mistake, made impossible.

If you are about to write `os.getenv(...)` somewhere else, add a field here instead.

### `logging.py` — structured, correlated

`log.info("donation_created", donation_id=...)` produces a queryable event, not a sentence.
The `request_id` ContextVar means every log line during a request carries its correlation
id without being threaded through twenty function signatures.

Third-party libraries (uvicorn, SQLAlchemy, Alembic) are routed through the same formatter,
so there is one log format in the stream rather than two.

### `security.py` — Dependency Inversion

`PasswordHasher` is a **Protocol**, not a class. `UserService` depends on the Protocol, so:

* tests inject a fake and run in milliseconds instead of ~250ms per bcrypt hash;
* migrating bcrypt → argon2 is a new 20-line adapter with zero service changes.

Every token carries `typ` (access | refresh) and `jti` (unique id):

* `typ` means a refresh token can never authenticate a request, and an access token can
  never be used at `/auth/refresh`. Without it, theft of a 7-day refresh token is
  equivalent to theft of a permanent access token.
* `jti` is what makes revocation possible at all.

Deviations from ARCHITECTURE.md (PyJWT over python-jose, bcrypt over passlib) are recorded
in [`docs/adr/`](../../docs/adr/).

### `exceptions.py` — the controller advice

**The most important file in `core/`.** Services raise `NotFoundError`,
`BusinessRuleViolation`, `PermissionDeniedError`. Handlers registered on the app translate
every one into RFC 9457 `application/problem+json`.

What this buys:

* Services import nothing from FastAPI, so they work from a CLI or a worker.
* One error shape for every failure mode — domain errors, validation, 404 on an unknown
  route, an unhandled 500. Clients parse one envelope.
* Adding an error type is one small class. The handlers never change (Open/Closed).
* A 500 logs the full traceback and returns only a generic message plus the `request_id`.
  Debuggable without leaking internals.

Clients must branch on `code`, never on the prose `detail`.

### `middleware.py` — the aspect-oriented seam

Correlation ids, access logs and security headers apply to every request, *including ones
that never reach a route* (404s, validation failures). A per-route decorator would have to
be remembered forty times and would still miss those.

**Registration order is reverse of execution order.** `register_middleware` documents the
resulting chain, because getting it wrong is a classic bug — request-id middleware after
logging middleware means every access log says `request_id="-"`.

### `rate_limit.py` — why Redis, not memory

slowapi's default storage is a process-local dict. Four uvicorn workers turn a "5 per
minute" login limit into 20 per minute; three replicas make it 60; a restart resets every
counter. A shared Redis counter is the entire point.

The key is **user, then IP**: keying only on IP punishes everyone behind one NAT; keying
only on user leaves `/auth/login` and `/auth/register` — the endpoints that actually need
protecting — unprotected.

See [`docs/RATE_LIMITING.md`](../../docs/RATE_LIMITING.md), including the deployment
requirement about `X-Forwarded-For`.

### `redis.py` — one pool, lifespan-managed

One connection per request would spend more time on TCP handshakes than on the `GET`. One
pool, created in `lifespan`, closed at shutdown, handed out via `get_redis()` as a
dependency (so it is overridable in tests).

One instance serves three purposes, separated by key **prefix** rather than by numbered DB —
prefixes work on Redis Cluster, numbered DBs do not:

```
rl:*         rate-limit counters
cache:*      read cache
denylist:*   revoked JWT ids
```

That separation is a security property: `RedisCache.clear()` deletes only `cache:*`, so
clearing the cache can never wipe rate-limit counters (handing an attacker a clean slate)
or the denylist (un-revoking every logged-out token).

---

## The fail-open / fail-closed split

The most important design decision in this directory, and it is deliberate that the two
differ:

| Component | On Redis failure | Why |
|---|---|---|
| `RedisCache` | **Fails open** — returns `None`, caller reads the database | A cache exists to make things faster. Letting it make things *broken* inverts its purpose. |
| `RedisTokenDenylist` | **Fails closed** — treats every token as revoked | A missed revocation means a token the user cancelled still works. That is a security failure, not a slowdown. |

Consequence: when Redis is down, the API rejects all authenticated requests. That is a hard
outage, and it is the correct trade for a security control — which is why Redis is a
`depends_on: service_healthy` dependency of the API container, not an optional extra.
