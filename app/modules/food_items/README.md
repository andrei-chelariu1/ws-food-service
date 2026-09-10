# `food_items` — Surplus Food Listings

## Purpose

Owns food listings and their lifecycle. **This module owns the invariant the whole
application exists to protect:**

> Expired or unavailable food cannot be reserved for donation.

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/food-items` | **none** | Public browse. Cached. Available + unexpired only. |
| GET | `/food-items/{id}` | **none** | Public. |
| GET | `/food-items/mine` | any | Your listings, every status. Declared **before** `/{id}`. |
| POST | `/food-items` | any | `owner_id` from the token, never the body. |
| PATCH | `/food-items/{id}` | owner/admin | Ownership check in the service. AVAILABLE only. |
| POST | `/food-items/{id}/cancel` | owner/admin | Named action, not `PATCH {"status": ...}`. |
| DELETE | `/food-items/{id}` | owner/admin | **Soft** delete. |

Note `/mine` before `/{item_id}`: FastAPI matches in registration order, so the reverse
order makes `/mine` parse as a UUID and return 422. Regression-tested.

---

## The state machine

```
                 ┌──────────► CANCELLED (terminal)
                 │
   AVAILABLE ────┼──reserve──► RESERVED ──mark_donated──► DONATED (terminal)
        │        │                 │
        │        └───────────────► EXPIRED (terminal) ◄───┘
        │                             ▲
        └─────────────────────────────┘
                       release ──► AVAILABLE (if still fresh)
                                   EXPIRED   (if it went off while reserved)
```

Declared as **data** in `models.py`:

```python
ALLOWED_TRANSITIONS = {
    FoodItemStatus.AVAILABLE: frozenset({RESERVED, CANCELLED, EXPIRED}),
    FoodItemStatus.RESERVED:  frozenset({DONATED, AVAILABLE, EXPIRED}),
    ...
}
```

Why data rather than conditionals:

* the whole lifecycle is legible in eight lines;
* adding a state is one dict entry, not an audit of every branch (**Open/Closed**);
* `FoodItemService._transition` is four lines and cannot disagree with the table, because
  the table is the only definition.

Note what is **absent**: `RESERVED → CANCELLED`. An owner may not withdraw food someone is
counting on; they must decline the donation request, which notifies the recipient. The rule
is expressed by an omission in the table rather than by an `if` in a service.

---

## The invariant, and why it cannot be bypassed

`FoodItemService.reserve()` is the **only** way an item becomes `RESERVED`. The donations
module holds a `FoodItemService`, not a `FoodItemRepository`, so there is no code path
from a donation request to the `food_items` table that skips this method.

Inside it, three things happen in this order:

```python
item = await self._repo.get_for_update(item_id)  # 1. row lock
if is_expired(item.expires_at):  # 2. expiry — checks the TIMESTAMP
    raise FoodItemExpiredError(item.name)
if item.status is not FoodItemStatus.AVAILABLE:  # 3. state machine
    raise FoodItemNotAvailableError(str(item.status))
```

**Why `expires_at` and not `status`.** An item whose date passed one second ago is expired
even though no sweep has marked it `EXPIRED`. Checking `status` would let that food
through — and it is the *common* case, because nothing runs continuously. The timestamp is
the authority; `status` is a materialised cache of it for cheap filtering.

**Why expiry is checked before status.** An expired-and-reserved item reports EXPIRED. It
is the more fundamental failure and the more useful message; "not available" for food that
has gone off is actively misleading. Pinned by
`test_service.py::test_expiry_is_checked_before_status`.

**Why `get_for_update` and not `get`.** Read-then-check-then-write is not atomic:

```
T1: SELECT ... status = 'AVAILABLE'   ✓
T2: SELECT ... status = 'AVAILABLE'   ✓   <- both passed the check
T1: UPDATE status = 'RESERVED'
T2: UPDATE status = 'RESERVED'            <- one loaf, two donations
```

Under Postgres's default READ COMMITTED isolation, T2's SELECT genuinely sees `AVAILABLE`,
so an application-level check cannot catch this. `SELECT ... FOR UPDATE` makes T2 *block*
until T1 commits, then re-read `RESERVED` and be correctly rejected.

> **Test-suite caveat:** SQLite ignores row locks, so the default suite proves the
> *sequential* rejection, not the concurrent one. Run against Postgres
> (`TEST_DATABASE_URL=postgresql+asyncpg://...`) to exercise the real behaviour.

---

## Data-modelling decisions worth copying

**`quantity` is `NUMERIC(10,3)`, not `FLOAT`.** Binary floating point cannot represent 0.1
exactly, so summing float quantities accumulates error — and "total food rescued" is a
number this project reports. The DTO uses `Decimal` to match, so precision is not lost at
the JSON boundary either.

**`unit` is a closed enum.** Free text produces "kg", "Kg", "kilos", "kilogram" in one
column, after which no aggregation is possible without a cleanup script. Constraining
input is cheaper than cleaning output.

**Soft delete** (unlike `User`). A `Donation` row references an item, and hard-deleting it
would either violate the FK or cascade away the record of a completed handover.

**Two indexes with a purpose:**

* `(owner_id, status)` — `owner_id` leftmost, so the same index also serves the unfiltered
  "my listings" query (leftmost-prefix rule). The column order is not arbitrary.
* `ix_food_items_available_expires` — **partial**, on `status = 'AVAILABLE' AND
  is_deleted = false`. It indexes only the rows the public browse endpoint can return, so
  it stays small forever even as DONATED items accumulate. It is also exactly what
  `ORDER BY expires_at ASC` needs, so the hottest read in the application is an index scan.

---

## Caching

Cache-aside on `GET /food-items` only. Why that endpoint and no other:

* unauthenticated, so the response is identical for every caller — one cached page is
  correct for everyone;
* the most-requested read in the application;
* backed by two queries (rows + `COUNT(*)`).

Contrast `/food-items/mine`: per-user, so caching would multiply keys by users for a
fraction of the traffic.

**The key deliberately excludes the user.** If a personalised field were ever added, the
key must include the user id — otherwise user A is served user B's view. That is the
classic cache-poisoning-by-omission bug, and the reason this endpoint stays anonymous-only.

**Staleness is bounded and accepted.** Writes invalidate by prefix, so the usual case is
fresh. If an invalidation is lost (Redis blip — `delete_prefix` fails open), the 60-second
TTL caps the damage. Worst case a browser sees a listing that was just claimed, and
`reserve()` then rejects the request with a correct 409. **The cache can make the UI
slightly stale; it cannot make the system incorrect.** See `docs/CACHING.md`.

---

## The expiry sweep is not the safety mechanism

`expire_stale_items()` flips past-date items to `EXPIRED`. It exists so listings *look*
right and reporting is cheap. It is **not** required for correctness — `reserve()` checks
`expires_at` directly, so an unswept item still cannot be donated.

Worth being clear about which of the two is load-bearing: the check, not the sweep.

The sweep is idempotent (its WHERE clause excludes already-EXPIRED rows), so it is safe on
a schedule, twice concurrently, or by hand during an incident. It uses a single bulk
`UPDATE`, which bypasses the ORM — acceptable *only* because expiry is time-driven rather
than actor-driven and the target state is terminal. Any transition a user can trigger goes
through the service.
