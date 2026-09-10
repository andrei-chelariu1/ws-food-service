# Rate Limiting

## The tiers

| Tier | Limit | Applies to | Abuse profile |
|---|---|---|---|
| default | `200/minute` | every route (middleware) | coarse anti-flood net |
| login | `5/minute` | `POST /auth/login`, `POST /users/me/password` | **password brute force** |
| register | `3/hour` | `POST /auth/register` | signup spam, mass account creation |
| write | `30/minute` | POST/PATCH/DELETE | runaway clients |

One global number cannot be right for all of them: 200/min is generous for browsing and
uselessly permissive for a login endpoint.

**`5/minute` on login is the single most important limit in the application.** It converts an
offline-speed guessing attack into an 87-year one. `/users/me/password` gets the *login* tier,
not the write tier, because it also verifies a password — so it is a guessing surface too.

Configurable via `RATE_LIMIT_*` in `.env`.

---

## Why Redis, not in-memory

slowapi's default storage is a process-local dict:

| Deployment | A "5/minute" limit actually allows |
|---|---|
| 1 process | 5/min ✓ |
| 4 uvicorn workers | **20/min** |
| 4 workers × 3 replicas | **60/min** |
| after a deploy | every counter resets to zero |

A shared Redis counter is the entire point: **one limit, correctly enforced, regardless of how
many processes serve traffic.**

Verify:

```bash
docker compose exec redis redis-cli --scan --pattern '*rl:*'
# LIMITS:LIMITER/rl:/ip:172.19.0.1//api/v1/auth/login/5/1/minute
```

> Note the actual prefix: our `key_prefix` is `rl:`, but the `limits` library prepends its own
> `LIMITS:LIMITER/`. Written as observed — it matters if you ever inspect or clear counters by
> hand.

---

## The key: user, then IP

```python
def rate_limit_key(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_client_ip(request)}"
```

| Keying only on… | Breaks because |
|---|---|
| IP | one corporate NAT or mobile carrier gateway = hundreds of users sharing a bucket |
| user id | `/auth/login` and `/auth/register` have no user — **the endpoints that most need protecting are unprotected** |

So: authenticated abuse is attributable to an **account**, anonymous abuse to an **address**. The
`user:`/`ip:` prefixes keep the namespaces from colliding.

### An ordering caveat worth knowing

`SlowAPIMiddleware` runs **before** route dependencies, so for the *global default* limit
`request.state.user_id` is not yet set and the key falls back to IP. Per-route
`@limiter.limit(...)` decorators run **after** dependency resolution and do see the user.

That is the desired behaviour — the global limit is a coarse net, per-route limits are precise —
but it is worth knowing rather than discovering.

---

## The required endpoint signature

Every `@limiter.limit(...)` route **must** declare both parameters:

```python
@router.post("/login")
@limiter.limit(lambda: get_settings().RATE_LIMIT_LOGIN)
async def login(
    request: Request,   # slowapi reads it to compute the key
    response: Response, # slowapi writes X-RateLimit-* onto it
    payload: LoginRequest,
    service: UserServiceDep,
) -> TokenPair: ...
```

Omit `response` and **every call to that endpoint** fails:

```
Exception: parameter `response` must be an instance of starlette.responses.Response
```

### This shipped once, and the tests could not see it

`RATE_LIMIT_ENABLED=false` in the suite makes the decorator a no-op, so a missing `response` is
invisible until the limiter is enabled — i.e. in the deployed container, on
`POST /auth/register`, on the happy path.

**The gap is now closed by a structural test** that walks the real route table and asserts the
pairing:
`tests/shared/test_rate_limit_key.py::test_every_rate_limited_route_declares_request_and_response`.
No Redis, no live limiter.

There is also a `test_the_structural_guard_is_not_vacuous`, because the first two versions of
that guard silently matched **zero** routes — first because `from __future__ import annotations`
makes annotations strings, then because this FastAPI version stores `include_router` results as
lazy `_IncludedRouter` objects. *If you write a reflective test, assert that it found something.*

---

## The limiter fails OPEN — the opposite of the denylist

```python
Limiter(..., swallow_errors=True)
```

**Without it, a Redis outage makes the limiter raise on every request** — and because
`SlowAPIMiddleware` wraps everything, that includes `/health/live`. A liveness probe returning
500 tells Kubernetes the container is broken, so it restarts **every replica**: a recoverable
Redis blip becomes a full outage with a thundering-herd restart on recovery.

Observed, not theorised. `docker compose stop redis` produced:

```
GET /health/live  ->  500 INTERNAL_ERROR      (before)
GET /health/live  ->  200 {"status":"alive"}  (after)
GET /health/ready ->  503 {"redis":"unavailable"}   ← correct: stop routing, do not restart
```

### The trade-off, stated plainly

While Redis is unavailable, rate limiting degrades to **no limiting**, so login brute-force
protection is temporarily absent. Accepted because:

* total unavailability is strictly worse;
* the JWT denylist fails **closed**, so authenticated traffic is rejected during the same
  outage anyway;
* the window is short — Redis is a `depends_on: service_healthy` dependency, and the readiness
  probe pulls the instance out of rotation.

**The rule: availability controls fail open, security controls fail closed.**

Health endpoints are additionally `@limiter.exempt` — belt to that braces, and probes should not
consume anyone's budget.

---

## The 429 response

Flows through the same problem+json handler as every other error, rather than slowapi's bespoke
plain-text body:

```json
{
  "type": "/problems/rate-limit-exceeded",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Rate limit exceeded: 5 per 1 minute. Retry after 60s.",
  "instance": "/api/v1/auth/login",
  "code": "RATE_LIMIT_EXCEEDED",
  "request_id": "a59cbcd3…"
}
```

with `Retry-After: 60`.

`Retry-After` is not decoration: a well-behaved client honours it, and **without it clients
hammer you harder while limited**. `X-RateLimit-Limit`/`-Remaining`/`-Reset` let a client
self-throttle before being cut off. All are in CORS `expose_headers`, or a browser client cannot
read them.

`rate_limit_exceeded` logs at **warning** with the key and path — a spike is either an attack or
a misbehaving client, and both are worth seeing.

---

## `moving-window`, not `fixed-window`

A fixed window lets a client send **2× the limit** by straddling a boundary: 5 requests at
:59.9 and 5 more at :00.1. A sliding window counts the trailing 60 seconds and cannot be gamed
that way. Slightly more expensive in Redis; correct.

---

## ⚠ Deployment requirement — the API cannot enforce this

**`X-Forwarded-For` is client-controlled unless your ingress overwrites it.**

`get_client_ip` takes the *first* entry (`client, proxy1, proxy2`), which is correct behind a
well-configured proxy. But an attacker who can set the header freely rotates the value and
**evades every IP-based limit**.

| Deployment | Action needed |
|---|---|
| nginx | `proxy_set_header X-Forwarded-For $remote_addr;` (**set**, not append) |
| Traefik / most cloud LBs | overwrites by default — verify |
| **Directly exposed, no proxy** | IP limits are **bypassable**. Put a proxy in front. |

Pinned by a test that documents the behaviour rather than pretending it is validated:
`test_rate_limit_key.py::test_forwarded_header_values_are_passed_through_verbatim`.

`get_client_ip` returns `"unknown"` when `request.client` is `None` (true for some ASGI
transports) rather than raising — the limiter must never be the thing that breaks the
application.

---

## Verifying it end to end

```bash
for i in $(seq 1 8); do
  curl -s -o /dev/null -w "%{http_code} " -X POST localhost:8000/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"x@example.com","password":"WrongPassw0rd"}'
done; echo
# 401 401 401 401 401 429 429 429

curl -sD - -o /dev/null -X POST localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"x@example.com","password":"y"}' \
  | grep -i 'ratelimit\|retry-after'
```

Counters live in Redis, so restarting the API does **not** reset them — which is the whole point.

---

## Known limitations

1. **Bypassable without a sanitising proxy** — see above.
2. **No per-user tier differentiation.** A paid tier would key on a plan attribute; the
   `key_func` is the hook.
3. **No global cost budget.** Limits are per-endpoint counts, not weighted by how expensive a
   request is.
4. **`enabled` and the storage backend are fixed at import**, because the `Limiter` must be a
   module-level singleton — `@limiter.limit(...)` decorates route functions at definition time,
   when there is no request to inject settings into. Consequence: toggling rate limiting needs a
   restart, and the test suite must set the environment *before* importing `app.*` (see the
   header of `tests/conftest.py`).
5. **No distributed-attack protection.** A botnet with thousands of IPs defeats per-IP limits.
   That is a WAF / upstream problem, not an application one.
