# `users` — Identity, Authentication, Roles

## Purpose

Owns accounts and the entire token lifecycle. Every other module depends on this one for
"who is calling?", and it is the module that satisfies the `UserAuthenticator` contract
that `app/shared/security/` depends on.

## Endpoints

| Method | Path | Auth | Rate limit | Notes |
|---|---|---|---|---|
| POST | `/auth/register` | — | **3/hour** | Always creates a `DONOR`. Returns user + tokens. |
| POST | `/auth/login` | — | **5/minute** | One error for every failure mode. |
| POST | `/auth/refresh` | — | default | **Single-use** — rotates and denylists the old token. |
| POST | `/auth/logout` | access token | default | Revokes both tokens. |
| GET | `/users/me` | any | default | No id in the URL, so nothing to tamper with. |
| PATCH | `/users/me` | any | 30/min | `full_name` only. |
| POST | `/users/me/password` | any | **5/minute** | Requires the current password. |
| GET | `/users/{id}` | self or admin | default | `require_ownership` — the IDOR check. |
| GET | `/users` | **ADMIN** | default | Paginated, filterable by role. |
| PATCH | `/users/{id}/role` | **ADMIN** | default | Separate endpoint so it can carry a stricter guard. |
| DELETE | `/users/{id}` | **ADMIN** | default | Deactivates; does not erase. |

---

## The token design

```
POST /auth/login
    ├── access token   15 min   typ=access   jti=<uuid>   role=<role>
    └── refresh token   7 days  typ=refresh  jti=<uuid>

POST /auth/refresh (with refresh token)
    1. verify signature, expiry, typ == "refresh"
    2. is jti on the denylist?  -> 401 TOKEN_REVOKED   (replay detected)
    3. re-read the user         -> 401 if gone, 403 if deactivated
    4. denylist the old jti     <-- BEFORE issuing the new pair
    5. issue a fresh pair
```

### Why each piece exists

**`typ` claim.** Without it, a refresh token authenticates ordinary requests — so
stealing a 7-day credential is equivalent to stealing a permanent one, and the short
access-token lifetime is pointless.

**`jti` claim.** JWTs are self-contained, so the server cannot "delete the session".
`jti` is what makes revocation possible at all.

**Refresh rotation.** Each refresh token works exactly once:

* a stolen token is useful only until the real client next refreshes;
* if the *thief* refreshes first, the real client's next attempt fails — surfacing the
  compromise instead of hiding it;
* `token_reuse_detected` in the logs is a high-signal alert, because a correct client
  never presents the same refresh token twice.

**Revoke before issuing.** If it happened the other way round, a crash in between would
leave the old token valid — the exact failure rotation exists to prevent.

**The denylist fails CLOSED.** `is_revoked()` returns `True` when Redis is unreachable.
That is the opposite of the cache, deliberately: a missed cache read costs a query, a
missed revocation means a token the user cancelled still works.

---

## Anti-enumeration, and why it takes two measures

`/auth/login` returns the identical `401 "Incorrect email or password"` for an unknown
address, a wrong password, and a disabled account.

That alone is not enough. Without a second measure the unknown-email path returns in
~2ms (one indexed SELECT) while the wrong-password path takes ~250ms (a bcrypt verify).
A 100× timing difference leaks exactly what the identical message was hiding.

So `UserService.authenticate` runs a **dummy hash comparison** when the email is unknown.
A vague message with a loud timing signal is not vague.

Registration makes the opposite trade: `EmailAlreadyRegisteredError` **does** confirm that
an address has an account, because silent-success registration is badly confusing for
users. The 3/hour limit is what stops that oracle being used at scale. Written down in
`exceptions.py` as a product judgement, so it can be revisited for a system where account
existence is genuinely sensitive.

---

## Why `authenticate_access_token` queries the database

The token already carries `sub` and `role`, so this could be zero queries. It is not,
because a token is a snapshot up to 15 minutes old:

* an account deactivated a minute ago must stop working **now**;
* a role demoted from ADMIN must take effect immediately;
* a deleted user must not keep a valid identity.

One indexed primary-key lookup per request buys revocation that actually works. If it
ever becomes a measured bottleneck, cache the user row for a few seconds — but measure
first.

---

## Security invariants (each has a test)

| Invariant | Enforced by | Test |
|---|---|---|
| Password hashes never leave the server | `UserRead` has no such field | `test_api.py::test_register_returns_201_with_user_and_tokens` |
| A client cannot choose its own role | `UserCreate` has no `role` + `extra="forbid"` | `test_api.py::test_register_rejects_client_supplied_role` |
| Login failures are indistinguishable | one error class + dummy verify | `test_service.py::test_login_failures_are_indistinguishable` |
| A refresh token works once | rotation + denylist | `test_service.py::test_refresh_rotates_and_invalidates_the_old_token` |
| Logout revokes **both** tokens | `logout()` takes both | `test_service.py::test_logout_revokes_both_tokens` |
| An access token cannot refresh | `typ` claim | `test_service.py::test_refresh_token_is_rejected_at_the_access_endpoint` |
| Deactivation is immediate | DB read per request | `test_service.py::test_deactivated_user_is_rejected_immediately` |
| User A cannot read user B | `require_ownership` | `test_api.py::test_user_cannot_read_another_users_profile` |
| Redis down ⇒ tokens rejected | denylist fails closed | `test_service.py::test_denylist_fails_closed_when_redis_is_unavailable` |

---

## Known limitation, stated rather than hidden

**Changing a password does not revoke the user's other sessions.** Doing that properly
needs a per-user token *generation counter* (bump it, and every token issued earlier
becomes invalid) rather than a per-`jti` denylist — we cannot enumerate a user's
outstanding tokens. Left out to keep the sample focused; noted in
`service.py::change_password` and in `docs/SECURITY.md`.
