# `tests/shared/` — tests for the plumbing

## Purpose

Tests for `app/shared/` and `app/core/` — the code every module depends on. A bug here is a bug in
four modules at once, which is why this directory is worth its own attention.

## What belongs here

| ✅ | ❌ |
|---|---|
| The generic repository contract | anything about food items or donations |
| Config parsing and production safety | endpoint behaviour — that is `tests/modules/` |
| Cross-cutting HTTP concerns (headers, rate-limit keys) | |

## The files

| File | Tests | The interesting part |
|---|---|---|
| `test_base_repository.py` | 15 | **soft delete is applied centrally** — a deleted row is invisible to `get`, `list`, `count` and `exists`. Tested once here so no module has to. Also: `get()` returns `None`, never raises. |
| `test_pagination.py` | 20 | `PageParams` bounds (`size ≤ 100`), and `build_cache_key` determinism — key order must not matter, or the hit rate silently halves |
| `test_config.py` | 13 | `assert_production_safe()` refuses insecure production config, and list parsing accepts both JSON and comma-separated forms |
| `test_rate_limit_key.py` | 13 | user-then-IP key selection, **plus the two structural guards** |
| `test_security_headers.py` | 8 | the strict API CSP vs. the scoped `/docs` exception, and that the relaxation cannot leak |

## Two tests here are unusual, and are the point

`test_rate_limit_key.py` contains a test that walks the **real route table** and asserts every
rate-limited endpoint declares both `request: Request` and `response: Response` — a rule slowapi
enforces at runtime and no type checker can express. It exists because omitting `response` shipped
once: `RATE_LIMIT_ENABLED=false` makes the decorator a no-op, so the ordinary suite was
structurally blind to it.

Next to it:

```python
def test_the_structural_guard_is_not_vacuous() -> None:
    ...
    assert len(rate_limited) >= 13
```

Because the guard silently matched **zero** routes, twice — first because
`from __future__ import annotations` turns annotations into strings, then because this FastAPI
version stores `include_router` results as lazy `_IncludedRouter` objects. Both times it passed
while checking nothing.

> **If you write a reflective test, assert that it found something.** A green test inspecting an
> empty list is worse than no test: it buys confidence it has not earned.

`test_security_headers.py` applies the same idea differently — it asserts the CDN hosts named in
the `/docs` CSP are still the hosts the served HTML actually references, so a FastAPI change
surfaces as "narrow this policy" instead of a stale allowance nobody revisits.

## Regression tests live where the bug was

`test_config.py::test_seed_admin_email_is_accepted_by_the_login_schema` looks trivial and is not:
the seeded admin was originally `admin@foodwaste.local`, which `EmailStr` rejects (`.local` is a
special-use TLD). The user was created by the migration and **could never log in** — a bug with no
error message anywhere. The test pins the fix at the boundary where the two rules disagreed.

See [`../README.md`](../README.md) for fixtures and
[`../../docs/TESTING.md`](../../docs/TESTING.md) for the strategy.
