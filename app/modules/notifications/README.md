# `notifications` — In-App Messages

## Purpose

Two responsibilities, deliberately split:

1. **The inbox** — list, count unread, mark read, dismiss. Ordinary CRUD, scoped per user.
2. **Emission** — the `notify_*` methods other modules call when something happens.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/notifications` | Your inbox, newest first. `?unread_only=true`. |
| GET | `/notifications/unread-count` | Just the badge. Declared **before** `/{id}`. |
| POST | `/notifications/read-all` | One bulk UPDATE. |
| POST | `/notifications/{id}/read` | Idempotent — preserves the original `read_at`. |
| DELETE | `/notifications/{id}` | **Hard** delete. |

**There is no `POST /notifications`, and no `NotificationCreate` schema.** Notifications
are created by the system in response to domain events. A create endpoint would let any
user send any other user an arbitrary message — a spam and phishing vector, not a feature.
The absence is part of the security design.

---

## The `notify_*` API is the module's public contract

`DonationService` calls:

```python
await self._notifications.notify_donation_accepted(
    recipient_id=..., food_item_name=..., pickup_location=..., donation_id=...
)
```

not a generic `create(user_id, type, title, body)`. Three reasons:

**Message text lives in one file.** Changing wording — or adding translations — touches
this module only. If callers composed the strings, copy would be scattered across every
module that ever notifies anyone.

**The signature documents the requirement.** `notify_donation_accepted` *requires*
`pickup_location`, because a notification saying "accepted!" without telling the recipient
where to go is useless. A generic `create(title, body)` cannot enforce that.

**The `type`/`title`/`body` triple cannot drift.** Each `notify_*` builds all three
together, so a `DONATION_ACCEPTED` notification can never carry a declined body.

This is the Interface Segregation Principle read from the caller's side: expose the narrow,
meaningful operations rather than one wide generic one.

---

## What is transactional and what is not

```
notify_*()
  ├── INSERT notification row   ← inside the CALLER's transaction (durable)
  └── dispatcher.dispatch(...)  ← after the response (best-effort)
```

**The row is written inside the caller's transaction.** If the donation rolls back, so does
its notification — the two can never disagree. Tested by
`tests/modules/donations/test_service.py::test_notification_is_created_for_the_donor`.

**External delivery is dispatched, not awaited.** The request does not wait on SMTP.

**Order matters: persist, then dispatch.** Dispatching first would risk an email about a
donation that never happened — an unrecallable side effect committed before the fact it
describes.

The important half is durable; the unreliable half is optional. That split is the design,
not a compromise.

---

## Why notifications are persisted at all

A fire-and-forget push that fails is gone: the user is never told their donation was
accepted, and there is no record that we tried. Writing the row first makes the
notification durable, the in-app list always correct, external delivery a best-effort
*additional* channel, and retry possible because the intent still exists.

This is the transactional-outbox pattern in its simplest form. Full outbox semantics — a
`delivered_at` column, a worker polling undelivered rows, at-least-once delivery with
idempotency keys — is the natural next step. Not built, because there is no external
channel yet and building the machinery before the requirement is the opposite of KISS.
Where it would go is marked in `tasks.py`.

---

## `TaskDispatcher` — the Dependency Inversion lesson

ARCHITECTURE.md specifies arq. Per the project decision no arq worker is deployed
(`docs/adr/0004`), so side effects run in FastAPI's `BackgroundTasks`.

The naive version of that decision is `background_tasks.add_task(...)` sprinkled through
services. Then `NotificationService` imports FastAPI, becomes untestable without a request,
and the day arq *is* introduced every call site changes.

Instead the service depends on a Protocol with one method:

```python
class TaskDispatcher(Protocol):
    def dispatch(self, task: TaskCallable, *args: Any, **kwargs: Any) -> None: ...
```

| Implementation | Used by |
|---|---|
| `BackgroundTasksDispatcher` | production — the only place touching FastAPI's background machinery |
| `ImmediateTaskDispatcher` | tests — drops tasks, logs what would have run |
| *`ArqTaskDispatcher`* | ~15 lines, whenever a real queue is needed. **No service changes.** |

That the no-op implementation is *correct* is the proof the abstraction is honest:
dispatching is a side effect, not part of any business rule.

### The honest limitations of `BackgroundTasks`

* **No durability.** Process dies, task gone. Nothing retries.
* **No retries, no backoff, no dead-letter queue.**
* **Same process** — a slow task consumes a worker slot.
* **Must not use the request's session**, which is already committed and closed by the time
  it runs. Tasks therefore receive plain strings.

A task that needs its own database session is the signal it belongs in a real worker.

---

## Data-modelling decisions

**`read_at: datetime | None`, not `is_read: bool`.** A nullable timestamp carries strictly
more information at the same cost: it answers "is it read?" (`read_at IS NULL`) *and*
"when?". A boolean cannot be upgraded later without a backfill that has no data to fill
from. Prefer nullable timestamps over booleans for anything event-shaped.

**`related_donation_id` has no FK constraint.** A notification is a historical record and
must survive the donation it references being purged. A real FK would force either a cascade
(destroying history) or a RESTRICT (blocking cleanup).

**`ON DELETE CASCADE` on `user_id`** — unlike `food_items` and `donations`, which use
RESTRICT. A notification is meaningless without its recipient and references no third
party.

**No soft delete.** A dismissed transient message should genuinely disappear. Tombstones
would grow the fastest-growing table in the schema for nothing.

**The partial unread index is the highest-value index in the schema:**

```sql
CREATE INDEX ix_notifications_user_unread ON notifications (user_id) WHERE read_at IS NULL;
```

The badge is polled constantly while read notifications accumulate forever. Indexing only
unread rows keeps the index proportional to the *backlog*, not to all history.

**`delete_read_older_than`** exists because a retention policy is a design requirement, not
an afterthought — an unbounded append-only table is a slow-motion outage. It deletes only
*read* rows, so an inactive user's backlog is never silently discarded.

---

## Security

Every query in `NotificationRepository` filters on `user_id`, and no method accepts a
user id from a client. **The unsafe query does not exist**, which is stronger than
guarding its use.

`mark_read` and `dismiss` still need an ownership check (the id comes from the URL), and
they return the *same* error for "not yours" as for "does not exist" — so the endpoint
cannot be used to discover which ids are real. This is the most IDOR-exposed resource in
the application: ids are enumerable and bodies reveal who is receiving food from whom.
