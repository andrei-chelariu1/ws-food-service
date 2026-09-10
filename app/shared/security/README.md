# `app/shared/security/` — Authentication and Authorization

## Purpose

| File | Answers | Framework-free? |
|---|---|---|
| `principal.py` | *what* an authenticated caller is (`CurrentUser`, `UserAuthenticator`) | **yes** |
| `dependencies.py` | *who are you?* — `get_current_user`, `get_current_active_user` | no (FastAPI wiring) |
| `permissions.py` | *may you do this?* — `require_roles()`, `require_ownership()` | no |

Authentication and authorization are separate files because they have separate reasons to
change: swapping JWT for sessions touches only the first; adding a `MODERATOR` role touches
only the last. Single Responsibility at file granularity.

---

## The interesting problem: `shared/` cannot import `modules/`

Authentication is cross-cutting, so it belongs here. But verifying a token means loading a
user, and `User` lives in `app/modules/users/`. The naive implementation violates the
architecture's one hard rule.

### The solution: Dependency Inversion at the composition root

```
              principal.py  (CurrentUser + UserAuthenticator Protocol)
                    ▲                              ▲
     depends on ────┘                              └──── depends on
     the abstraction                                     the abstraction
                    │                              │
        dependencies.py                   modules/users/service.py
    (FastAPI wiring + a                (UserService satisfies the Protocol
     placeholder provider                STRUCTURALLY — never imports it)
     that RAISES)
                            ▲
                            │
                      app/main.py
   app.dependency_overrides[get_user_authenticator] = provide_user_authenticator
```

Neither side imports the other. `main.py` — the only file allowed to know about everything —
joins them with one line.

Verify:

```bash
grep -rn "app\.modules" app/shared/    # -> no matches
```

### Two details that make it actually work

**`principal.py` is separate from `dependencies.py`.** `UserService.authenticate_access_token`
returns a `CurrentUser`. If that type lived in the FastAPI-importing module, every service
touching authentication would transitively import FastAPI — and "services are
framework-independent" would be technically false. One small file buys a boundary that holds:

```bash
grep -rn "fastapi" app/modules/*/service.py app/shared/security/principal.py   # -> no matches
```

**The placeholder provider raises.** A silent permissive default that authenticated nobody —
or worse, everybody — would be a security hole hidden behind a convenience. If the wiring in
`create_app()` is ever removed, every protected endpoint fails loudly with a
`NotImplementedError` naming the fix.

---

## `CurrentUser` is not the `User` entity

Three reasons:

1. `shared/` cannot name that type without importing the module.
2. It carries **only** what authorization needs (id, email, role, active). `hashed_password`
   therefore cannot travel through the request pipeline — so it cannot end up in a log line,
   a traceback, or a debug response.
3. It is detached from the session, so passing it around can never trigger a lazy load after
   that session closed.

It is `frozen=True`: a handler cannot mutate its own identity part-way through a request
(say, flip `role`) and have a later check see the change.

`role` is a plain `str`, not the `Role` enum, for the boundary reason above. `Role` is a
`StrEnum`, so `require_roles(Role.ADMIN)` still reads naturally from the module side.

---

## Why `get_current_active_user` is separate from `get_current_user`

So deactivation takes effect **immediately** rather than at token expiry. A disabled account
with a still-valid 15-minute access token would otherwise keep working — which is not what
"disable this user" means to whoever clicked it.

It returns **403, not 401**. The credentials are genuine; telling the client to re-login
sends them round a loop they cannot win.

`CurrentUserDep` is the alias used in route signatures. If the default protection level ever
changes, it changes in one place rather than on forty endpoints.

---

## Two levels of authorization

| Level | Where | Example | Why there |
|---|---|---|---|
| Coarse (role) | route dependency | `require_roles(Role.ADMIN)` | needs only the token |
| Fine (ownership) | inside the service | `require_ownership(user, item.owner_id)` | needs the loaded row |

### `require_roles` is a factory

FastAPI dependencies take no arguments of their own, so `require_roles` must *return* a
dependency. The payoff is declarative, auditable authorization:

```python
@router.delete("/{user_id}", dependencies=[Depends(require_roles(Role.ADMIN))])
```

You can read the router and know who can call what, instead of hunting for an
`if user.role != ...` at line 40 of a service method. And it runs *before* the handler body,
so there is no path where the work happens and the check is skipped.

Calling it with no roles raises at **import** time — a dependency that allows nobody is
always a mistake, and one that allowed everybody would be a silent hole.

### `require_ownership` — the IDOR defence

The vulnerability:

```
PATCH /api/v1/food-items/{someone-elses-id}
```

The URL is well-formed, the token is valid, the row exists. Without an ownership check the
edit succeeds. Automated scanners will not flag it and happy-path tests will not reach it —
which is why it is one of the most common real-world API vulnerabilities, and why it gets its
own named, logged, tested helper rather than an inline `if`.

**It cannot be a route dependency.** `require_roles` only sees the token; "is this *your*
item?" needs the item. Trying to express object-level rules at the route is how you end up
loading the same row twice, or checking an id from the URL that the handler never uses.

Admins pass by override, and **the override is logged** — administrative access to someone
else's data is exactly the event an auditor asks about later.

`allow_admin=False` exists for actions that must stay personal even for administrators —
changing a password, for instance.

The error message says "not permitted", never "belongs to user X". Confirming that an id
exists and identifying its owner are both information leaks.

Tested at both layers:
`tests/modules/food_items/test_service.py::test_stranger_cannot_update_someone_elses_item`
and `tests/modules/users/test_api.py::test_user_cannot_read_another_users_profile`.

---

## When a module needs its own rule

`donations` has **two** parties with different rights (the owner accepts, the recipient
cancels). `require_ownership` models a single owner, so that module defines
`_assert_is_item_owner` and `NotDonationOwnerError` locally.

That is correct: a shared helper twisted to cover both would take a flag, and a flag that
switches authorization is how two permissions get entangled in one function.
