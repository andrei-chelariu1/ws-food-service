# Security — Threat by Threat

What this application defends against, how, and — equally important — **what it deliberately
does not do**.

---

## 1. Credential theft and brute force

| Control | Where | Detail |
|---|---|---|
| bcrypt, cost 12 | `core/security.py` | ~250ms/hash. Refuses to boot below 12 in production. |
| 5/minute login limit | `modules/users/api.py` | Turns an offline-speed guessing attack into an 87-year one. |
| Redis-backed counters | `core/rate_limit.py` | Per-process counting would multiply the limit by worker × replica. |
| Transparent rehash | `UserService.authenticate` | `needs_rehash()` upgrades existing users when the cost factor is raised — the only way an increase ever reaches existing accounts. |
| 72-byte rejection | `BcryptPasswordHasher._encode` | bcrypt silently truncates at 72 bytes; two different long passwords would share a hash. |
| 12-char minimum | `modules/users/schemas.py` | Length is the dominant factor. NIST SP 800-63B advises *against* elaborate composition rules — they produce "Password1!" and reuse. |

**Not done:** breached-password checking (Have I Been Pwned k-anonymity API), MFA, account
lockout. Lockout is a deliberate omission — it converts a brute-force attempt into a
denial-of-service against the victim. Rate limiting is the better trade.

---

## 2. User enumeration

`POST /auth/login` returns the identical `401 "Incorrect email or password"` for an unknown
address, a wrong password, and a disabled account.

**That alone is insufficient.** Without a second measure the unknown-email path returns in
~2ms (one indexed SELECT) while the wrong-password path takes ~250ms (a bcrypt verify) — a
100× difference, trivially measurable, leaking exactly what the identical message hid.

So `UserService.authenticate` runs a **dummy hash comparison** against a fixed hash when the
email is unknown. *A vague message with a loud timing signal is not vague.*

Tested: `test_service.py::test_login_failures_are_indistinguishable` (parametrised over both
failure modes, asserting the same class **and** the same message).

**Registration makes the opposite trade, on purpose.** `EmailAlreadyRegisteredError` confirms
an address has an account, because silent-success registration is badly confusing. The 3/hour
limit stops that oracle being used at scale. This is a *product* judgement, recorded in
`modules/users/exceptions.py` so it can be revisited — for a medical or legal service, invert
it.

---

## 3. Token theft

```
access token    15 min   typ=access   jti   role
refresh token    7 days  typ=refresh  jti
```

**`typ` claim.** Without it a refresh token authenticates ordinary requests — so stealing a
7-day credential equals stealing a permanent one, and the short access lifetime is pointless.

**`jti` + Redis denylist.** JWTs are self-contained, so the server cannot "delete the session".
`jti` is what makes revocation possible at all.

**Refresh rotation (single-use).** Each refresh token works exactly once:

* a stolen token is useful only until the real client next refreshes;
* if the *thief* refreshes first, the real client's next attempt fails — **surfacing the
  compromise instead of hiding it**;
* `token_reuse_detected` is a high-signal alert, because a correct client never replays.

**Revoke before issuing.** Reversed, a crash in between leaves the old token valid — the exact
failure rotation prevents.

**Logout revokes both.** Revoking only the access token leaves the refresh token able to mint a
replacement — a "logout" that logs nobody out.

**The denylist fails CLOSED.** `is_revoked()` returns `True` when Redis is unreachable. During a
Redis outage every authenticated request is rejected. That is a hard availability cost, and it
is the correct trade for a security control — which is why Redis is `depends_on:
service_healthy`, not an optional extra.

**Not done — and this is the most significant gap:** changing a password does **not** revoke
the user's other sessions. Doing it properly needs a per-user token *generation counter* (bump
it, and every earlier token is invalid) rather than a per-`jti` denylist, because we cannot
enumerate a user's outstanding tokens. Noted in `UserService.change_password`.

**Refresh tokens are returned in the response body**, not as an HttpOnly cookie. For a browser
SPA the cookie is stronger (JavaScript cannot read it, so XSS cannot steal it) at the cost of
needing CSRF protection. Body delivery is right for mobile and service clients and keeps the
flow inspectable in Swagger. Revisit if a browser SPA becomes the primary consumer.

---

## 4. Privilege escalation

| Attempt | Result | Why |
|---|---|---|
| `POST /auth/register {"role": "ADMIN"}` | **422** | `UserCreate` has no `role`; `extra="forbid"` |
| `PATCH /users/me {"role": "ADMIN"}` | **422** | `UserUpdate` has no `role` |
| `PATCH /users/{id}/role` as a DONOR | **403** | `require_roles(Role.ADMIN)` |
| `POST /food-items {"owner_id": other}` | **422** | not in the schema; taken from the token |
| `POST /food-items {"status": "DONATED"}` | **422** | not in the schema; state machine only |

The pattern: **narrow input schemas + `extra="forbid"` makes escalation unexpressible**, rather
than something we validate against. Role changes live on a separate endpoint precisely so they
can carry a stricter guard that is visible in the router.

---

## 5. IDOR (Insecure Direct Object Reference)

**The most common real-world API vulnerability**, and the one automated scanners and happy-path
tests both miss:

```
PATCH /api/v1/food-items/{someone-elses-id}
```

Well-formed URL, valid token, existing row. Without a check, the edit succeeds.

| Resource | Guard | Test |
|---|---|---|
| `/users/{id}` | `require_ownership` | `test_api.py::test_user_cannot_read_another_users_profile` |
| `/food-items/{id}` | `require_ownership` in the service | `test_api.py::test_stranger_cannot_edit_another_users_item` |
| `/donations/{id}` | two-party participant check | `test_service.py::test_stranger_cannot_view_donation` |
| `/notifications/{id}` | `user_id` check in the service | (repository cannot express a cross-user query) |

**Structural defences, not just checks:**

* **UUID primary keys.** `GET /users/1`, `/2`, `/3` walks a table; UUIDs do not.
* **`/me` endpoints.** The id comes from the token, so there is no URL parameter to tamper
  with — an entire class of bug does not exist on those routes.
* **Repository-level scoping.** Every `NotificationRepository` query filters on `user_id` and
  none accepts a client-supplied id. **The unsafe query does not exist**, which is stronger
  than guarding its use.
* **Ownership checked before fetch** on `/users/{id}`, so response timing and a 404-vs-403
  difference cannot reveal whether an id exists.
* **Same error for "not yours" and "does not exist"** — the endpoint cannot be used to
  enumerate valid ids.

---

## 6. Injection

**SQL injection is structurally impossible:** every query goes through SQLAlchemy Core
expressions, which parameterise values. `BaseRepository.list()` accepts
`Sequence[ColumnElement[bool]]`, so passing a raw string is a *type* error — there is no route
from user input to SQL text. `grep -rn "text(" app/modules/` finds nothing.

**Path parameters are typed `uuid.UUID`**, so anything resembling a payload is rejected with a
422 before application code runs.

**LIKE wildcards are escaped** in `FoodItemRepository._escape_like`. Not an injection defence
(the value is parameterised) but a correctness and performance one: an unescaped `%` matches
every row, so a user searching "100_g" gets nonsense and the database does needless work.

**Log injection** is mitigated by capping an adopted `X-Request-ID` at 64 characters — it lands
in logs and response headers, so an attacker-controlled megabyte string is a real vector.

---

## 7. Data exposure

| Risk | Defence |
|---|---|
| Password hash in a response | `UserRead` has no such field — an **allowlist**, so a new column is invisible until deliberately added |
| Hash in a log line | `Base.__repr__` prints only the primary key |
| Hash in the request pipeline | `CurrentUser` carries only id/email/role/active |
| Stack traces to clients | catch-all handler logs the traceback, returns a generic message plus `request_id` |
| DB internals in a 409 | `integrity_error_handler` logs `exc.orig`, returns "conflicts with existing data" — constraint names are free reconnaissance |
| Swagger in production | `docs_url`/`openapi_url` are `None` when `ENVIRONMENT=production` |
| Soft-delete internals | `FoodItemRead` omits `is_deleted`/`deleted_at` |
| Credentials in logs | `SecretStr`; `_redis_safe_url()` strips them from connection strings |

The `request_id` is the bridge: a user quotes it in a ticket, an engineer greps the logs for
the full traceback. **Debuggability without leaking.**

---

## 8. Transport and browser-facing headers

```
X-Content-Type-Options: nosniff                 JSON executed as HTML is a stored-XSS vector
X-Frame-Options: DENY                           clickjacking
Referrer-Policy: strict-origin-when-cross-origin  our URLs contain ids
Content-Security-Policy: default-src 'none'; …  a JSON API should load nothing
Permissions-Policy: geolocation=(), …
Cross-Origin-Opener-Policy: same-origin
Strict-Transport-Security: max-age=31536000     PRODUCTION ONLY
```

HSTS is production-only deliberately: sending it over plain HTTP in dev pins `localhost` to
https in the developer's browser for a year.

### The one CSP exception: `/docs` and `/redoc`

`default-src 'none'` is right for a JSON API and **breaks Swagger UI**, which is a real HTML
page: it bootstraps itself with an inline `<script>`, loads its bundle and stylesheet from
`cdn.jsdelivr.net`, and takes its favicon from `fastapi.tiangolo.com`. Under the strict policy
the browser blocks all three and `/docs` renders blank.

Found the way these things usually are — three console errors reading *"violates the following
Content Security Policy directive: default-src 'none'"*. The policy was not wrong; it was
applied to a page it was never written for.

So `SecurityHeadersMiddleware` carries **two** precomputed policies and picks by path:

| Path | Policy |
|---|---|
| `/docs`, `/redoc` | `DOCS_CSP` — adds `'unsafe-inline'` for script/style, allowlists jsdelivr and the favicon host, `worker-src blob:` for ReDoc |
| **everything else**, including `/openapi.json` and every error body | `API_CSP` — unchanged `default-src 'none'` |

Why this is a scoped exception rather than a downgrade:

1. **Two paths only.** `/openapi.json` is *fetched by* the docs page but is not itself a
   document, so it keeps the strict policy — CSP governs the page doing the loading, and
   `DOCS_CSP` permits `connect-src 'self'`.
2. **It cannot reach production.** `docs_paths` is built from `settings.docs_url` and
   `settings.redoc_url`, both `None` when `ENVIRONMENT=production`. The set is empty, the branch
   is unreachable. This is not a flag someone can forget to turn off — it is derived from whether
   the pages exist at all.
3. **No XSS sink.** Nothing user-controlled is rendered on these pages, which is the thing
   `'unsafe-inline'` normally protects.

Pinned by `tests/shared/test_security_headers.py`, including
`test_docs_relaxation_does_not_leak_to_the_api` and a check that the CDN hosts named in the
policy are still the ones the served HTML actually references — so if FastAPI changes its CDN,
the test tells you to narrow the policy instead of leaving a stale allowance behind.

**If you ever want `/docs` exposed in production, vendor `swagger-ui-dist` and serve it from
`StaticFiles`.** That drops the CDN dependency and the `'unsafe-inline'` allowance entirely. It
is more machinery than a development aid needs, which is why it is written down rather than
built.

**CORS** uses an explicit allowlist. `allow_origins=["*"]` with `allow_credentials=True` is
rejected by browsers anyway and is a CSRF footgun. `assert_production_safe()` refuses `*` in
production.

**TrustedHostMiddleware** blocks Host-header attacks (cache poisoning, password-reset link
injection). `*` is refused in production.

**`redirect_slashes=False`** — trailing-slash redirects turn a POST into a GET on some clients
and leak the `Authorization` header to the redirect target on others.

---

## 9. Denial of service

| Vector | Control |
|---|---|
| Unbounded page size | `MAX_PAGE_SIZE = 100`, enforced by Pydantic at the edge |
| Unbounded search pattern | `max_length=100` on the query parameter |
| Request flooding | 200/min default, tighter per-endpoint tiers |
| Unbounded expiry sweep | `list_expiring_before(limit=500)` — not optional |
| Redis `KEYS` blocking the server | `scan_iter`, never `KEYS` |
| Slow queries | targeted composite and partial indexes |
| Connection exhaustion | pool size + `pool_timeout` + `pool_pre_ping` |
| Unbounded table growth | `delete_read_older_than` retention on notifications |

**The rate limiter fails OPEN** (`swallow_errors=True`) — the opposite of the denylist. Found
the hard way: without it, a Redis outage made the limiter raise on *every* request including
`/health/live`, so Kubernetes would restart every replica and amplify a recoverable blip into a
full outage. During a Redis outage rate limiting degrades to none; that is accepted because
total unavailability is worse and the denylist rejects authenticated traffic anyway.

**The rule: availability controls fail open, security controls fail closed.**

---

## 10. Container and configuration

* **Non-root user** (`appuser`, uid 1001) — code execution lands unprivileged.
* **`no-new-privileges:true`** — blocks setuid escalation.
* **Multi-stage build** — no compilers or `uv` in the runtime image. Smaller attack surface,
  not just a smaller pull.
* **`.dockerignore` excludes `.env`** — secrets are never baked into a layer.
* **`scram-sha-256`** for Postgres, not the deprecated md5.
* **Redis AOF persistence** — the denylist must survive a restart, or logout silently
  un-revokes.
* **`assert_production_safe()`** refuses to boot in production with the sample `SECRET_KEY`,
  `DEBUG=true`, `ALLOWED_HOSTS=["*"]`, `CORS_ORIGINS=["*"]`, `BCRYPT_ROUNDS < 12`, or the sample
  seed password. Fail at boot, so the orchestrator never routes traffic to it. Every branch is
  tested.

**Not done:** `read_only: true` on the container. It needs tmpfs mounts for `/tmp` and Python's
bytecode paths; left off rather than added half-configured, because a broken hardening flag
teaches the wrong lesson.

---

## Deployment requirements — the API cannot enforce these

**`X-Forwarded-For` must be *overwritten* by your ingress, not appended to.** It is
client-controlled otherwise, so an attacker can rotate the value and evade every IP-based rate
limit. Pinned by a test that documents the behaviour
(`test_rate_limit_key.py::test_forwarded_header_values_are_passed_through_verbatim`).

**Terminate TLS at the load balancer.** The app speaks HTTP; HSTS only means something behind
real TLS.

**Rotate `SECRET_KEY` with a grace period.** Rotating it invalidates every outstanding token at
once. Support both keys for one refresh-token lifetime.

**Run migrations as a pre-deploy job** rather than in the entrypoint, once you have more than a
couple of replicas. See `migrations/README.md`.

---

## Summary of deliberate omissions

Listed together, because a security document that only lists wins is not useful:

1. **No MFA.**
2. **No breached-password check.**
3. **No password-change session revocation** (needs a generation counter).
4. **No refresh-token family tracking** — rotation detects reuse of a *specific* token, not a
   whole stolen chain.
5. **No audit-log table** — privilege changes and admin overrides are logged as structured
   events, not persisted rows.
6. **No field-level encryption** — nothing here warrants it yet.
7. **No `read_only` container filesystem.**
8. **Rate limiting is bypassable if `X-Forwarded-For` is not sanitised at the edge.**
9. **Registration confirms account existence** — a conscious usability trade.
10. **No CSRF protection** — correct for a bearer-token API, and would become necessary the
    moment cookie auth is introduced.
