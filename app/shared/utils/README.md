# `app/shared/utils/` — Small Helpers

## Purpose

Two files, both tiny, both preventing a specific class of bug.

| File | Provides |
|---|---|
| `datetime_utils.py` | `utcnow()`, `is_expired()`, `seconds_until()` |
| `pagination.py` | `PageParams` — the injectable page/size dependency |

## What belongs here

Genuinely generic, dependency-free helpers used by more than one module. **Not** a dumping
ground: a `utils/` that accumulates everything nobody could place becomes the file nobody
can reason about. If a helper is used by one module, it belongs in that module.

---

## Why a one-line `utcnow()` wrapper earns a module

### 1. Testability

"An expired food item cannot be donated" is a rule **about time**. If services call
`datetime.now(UTC)` directly, testing that rule means either monkeypatching a stdlib function
globally (fragile, leaks between tests) or `sleep()`ing in a test (slow, flaky).

With one wrapper there is one seam.

### 2. Timezone correctness by construction

`datetime.utcnow()` is deprecated in 3.12 **and returns a naive datetime** — one that looks
like UTC but carries no tzinfo. Compare it with a timezone-aware value from the database and
you get:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

in production, on the one code path nobody tested. Making `utcnow()` the only sanctioned
source means every datetime in the system is aware.

**Rule: no `datetime.now()` anywhere else in `app/`.** Grep enforces it.

### `is_expired(expires_at, *, now=None)`

`now` is injectable so a caller making several time-based decisions can pass one consistent
instant. Otherwise a single request could read the clock twice, straddle a boundary, and
reach two contradictory conclusions.

`FoodItemRepository.list_available` takes `now` as a **required** parameter for the same
reason: it is what keeps the paginated rows and the `COUNT(*)` agreeing about "now".

### `seconds_until(moment)`

Used for Redis TTLs. A revoked token's denylist entry should expire exactly when the token
does, and `EXPIRE` with a negative TTL **deletes the key immediately** — which would silently
un-revoke it. Clamped to zero.

### `_as_aware()`

Defensive coercion of a naive datetime to UTC. Values read back from SQLite (the test suite)
can lose tzinfo even when the column is declared `timezone=True`. Assuming UTC is right here
because `utcnow()` is the only writer.

---

## `PageParams` — why a dependency, not two query parameters

Written inline, every list endpoint repeats:

```python
page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)
```

...and then computes `offset = (page - 1) * size` by hand. Eight endpoints, eight chances to
write `page * size` (which silently skips the entire first page) or to forget `le=100`.

As one injectable object the bounds are declared once, the offset is computed once, and
`Depends(PageParams)` is three words at each call site.

```python
page_params: Annotated[PageParams, Depends()]
...
service.list_mine(user, offset=page_params.offset, limit=page_params.limit)
```

### The API speaks pages; the database speaks offset/limit

Translated in exactly one place, so no repository ever sees a `page` number and no router
ever computes an offset. The off-by-one lives in a single property with a test
(`tests/shared/test_pagination.py::test_offset_is_zero_on_the_first_page`).

### `MAX_PAGE_SIZE = 100` is a security control

Not an aesthetic limit. Unbounded `size` is a trivial denial-of-service: one request that
materialises a million ORM objects. Enforced by Pydantic at the edge, so no downstream code
can bypass it. The constant is pinned by a test so it cannot be quietly raised.

### Scale note, documented rather than pre-solved

Offset pagination degrades on large tables — the database must walk and discard `offset`
rows, so page 10,000 is slow, and a concurrent insert shifts rows between pages (an item can
be seen twice or missed).

It is the right default because it is what UIs need and what clients expect. When a table
outgrows it, switch **that endpoint** to keyset pagination:

```sql
WHERE (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT :size
```

Constant time, stable under concurrent writes, at the cost of losing random page access.
Documented rather than implemented, because building it before it is needed is the opposite
of KISS.
