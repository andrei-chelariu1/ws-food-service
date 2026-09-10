# `donations` — The Handover

## Purpose

Connects a food item to a recipient, and coordinates the two-party workflow between them.
This is the module where the **cross-module rule** and **transactional atomicity** both do
visible work.

## Endpoints

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/donations` | any (not the owner) | Reserves the item. 4 possible 409s. |
| GET | `/donations/mine` | recipient | Your requests, items eager-loaded. |
| GET | `/donations/received` | donor | Requests for food you listed. Needs a JOIN. |
| GET | `/donations/{id}` | **participants only** | Private, unlike a food listing. |
| POST | `/donations/{id}/accept` | **item owner** | → ACCEPTED. |
| POST | `/donations/{id}/decline` | **item owner** | → CANCELLED, **releases the item**. |
| POST | `/donations/{id}/complete` | **item owner** | → COMPLETED, item → DONATED. |
| POST | `/donations/{id}/cancel` | **recipient** | → CANCELLED, **releases the item**. |

Action endpoints, not `PATCH {"status": "..."}`. Each action has a different authorized
party and different side effects; one generic status field would collapse four distinct
permissions into one and make the state machine bypassable.

---

## The state machine

```
PENDING ──accept──► ACCEPTED ──complete──► COMPLETED (terminal)
   │                    │
   └──decline/cancel────┴──► CANCELLED (terminal)
```

Two state machines are in play — `Donation.status` records what the *parties agreed*,
`FoodItem.status` records where the *food is*. They are coupled but not merged, which is
why accepting must be transactional.

---

## The cross-module boundary — read the constructor

```python
class DonationService:
    def __init__(self, repository, food_item_service, notification_service):
                              ▲                ▲
                              │                └── another module's SERVICE
                              └── this module's own repository
```

`food_item_service`, never `FoodItemRepository`. What that buys:

**The expiry invariant cannot be bypassed.** To reserve an item this service must call
`FoodItemService.reserve()`, which checks `expires_at`, checks the state machine, and holds
a row lock. With a repository it could write `status = RESERVED` directly and skip all
three. The rule is what makes "expired food cannot be donated" *guaranteed* rather than
*usually true*.

**Rules stay where they are owned.** The food-item lifecycle lives in `food_items`. A change
there propagates here for free. Two modules writing one table is how invariants rot.

**The seam is visible.** `grep -rn "food_item_service" app/modules/donations/` lists every
cross-module interaction. Extracting `food_items` into its own service later means swapping
one injected object for an HTTP client — and nothing else.

Note that `repository.py` here *does* import the `FoodItem` model, to `JOIN`. Sharing a
schema is what one database means; sharing behaviour is what creates coupling.

---

## Two-party authorization

A donation has **two** legitimate parties with **different** rights, so
`require_ownership` (which models a single owner) is not enough:

| Action | Permitted | Why not the other party |
|---|---|---|
| accept / decline / complete | the **item owner** | the recipient must not self-approve |
| cancel | the **recipient** | withdrawing your own request |
| view | **either**, or an admin | it reveals who is receiving food |

Hence `_assert_is_item_owner` and `NotDonationOwnerError` in this module. The permitted
party is determined by the *food item's* owner — two joins from the token — so it cannot be
a route dependency; `require_roles` only sees the token.

Note the asymmetry with `food_items`: a food listing is **public**, a donation is
**private**. That is deliberate — a donation reveals who is receiving food, which is
sensitive.

---

## Transactional atomicity, concretely

`decline()` performs three writes:

```
1. donation.status  -> CANCELLED  (+ decline_reason)
2. food_item.status -> AVAILABLE (or EXPIRED, if it went off while reserved)
3. INSERT notification for the recipient
```

All three share one transaction, because the commit boundary is the request
(`shared/db/session.py`). If any step raises, all of it rolls back — there is no state
where the request is declined but the food stays stuck in `RESERVED`. **Nobody wrote a
rollback.**

**Releasing the item is the point of `decline`.** Without it, a donor saying "no" would
strand perfectly good food in `RESERVED` forever. Tested:
`test_service.py::test_declining_releases_the_food_item`.

Note that `release()` decides whether the item returns to `AVAILABLE` or goes to `EXPIRED`
— this module does not know that rule and deliberately does not need to. Re-listing food
that went off while reserved would put the invariant's failure mode back on the table.

---

## Ordering inside `request_donation`

```python
item = await self._food_items.get_by_id(payload.food_item_id)  # 1. exists? (404)
if item.owner_id == recipient.id: raise CannotDonateToSelfError # 2. cheap check
if await self._repo.has_open_request(...): raise Duplicate...   # 3. indexed check
await self._food_items.reserve(payload.food_item_id)            # 4. THE INVARIANT
await self._repo.add(donation)                                  # 5. persist last
```

Step 5 last is the deliberate bit: doing the work that *can fail* before the work that
*must persist* keeps the failing path cheap — no pointless write lock on `donations` for a
request that was going to be rejected. Verified by
`test_service.py::test_expired_food_rule_is_not_bypassed_by_the_donations_module`, which
asserts zero donation rows after a rejection.

---

## Why `cancel` and `decline` are separate methods

Same state change, opposite party, opposite notification. Merging them behind a
`by_recipient: bool` flag would entangle two authorization rules and two message templates
in one function — the classic boolean-parameter smell. Duplicating four lines is cheaper
than the wrong abstraction.

The same reasoning applies to `_transition` being duplicated in `FoodItemService` and here:
each module owns its own state machine, and a shared `StateMachineMixin` would couple two
lifecycles that have no reason to change together. See `docs/SOLID.md` on where DRY stops
applying.

---

## No unique constraint on `(food_item_id) WHERE status = 'PENDING'`

It looks like the obvious way to stop double-reservation. It is not:

* it produces an `IntegrityError` at commit, far from the code that caused it, with a
  message clients cannot act on;
* it would also block a legitimate second request after the first was declined, which is
  correct behaviour — so it cannot be the guard anyway.

`SELECT ... FOR UPDATE` in `FoodItemService.reserve` is the real protection, and it yields
a clean 409.
