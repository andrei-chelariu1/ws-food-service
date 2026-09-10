# ADR 0004 — FastAPI `BackgroundTasks` behind a `TaskDispatcher` Protocol

**Status:** accepted · **Revisit when:** notifications must reach an external channel

## Context

Accepting a donation should notify the recipient. The notification row is written
transactionally, but *delivering* it (email, push, SMS) is slow and can fail — it must not sit
inside the request that accepts the donation.

The production answer is a real queue: arq, Celery, RQ, or a broker. The user explicitly declined
adding a worker to this project.

## Decision

Dispatch through a **`TaskDispatcher` Protocol**, implemented today by
`BackgroundTasksDispatcher` (FastAPI's `BackgroundTasks`) and `ImmediateTaskDispatcher` (inline,
for tests).

```python
class TaskDispatcher(Protocol):
    def dispatch(self, task: TaskCallable, *args: object, **kwargs: object) -> None: ...
```

## Why

**`BackgroundTasks` is honestly sufficient right now.** There is no external channel configured —
`send_notification_email` is a logging stub. Adding a fourth container, a broker connection, a
worker image and a second deployment unit to run a `log.info()` is not engineering, it is
ceremony. That is KISS applied at the level where it matters: the architecture, not the syntax.

**The Protocol is what keeps that decision cheap.** `NotificationService` names an interface with
one method. Swapping in arq is one new class:

```python
class ArqTaskDispatcher:
    def __init__(self, pool: ArqRedis) -> None: ...
    def dispatch(self, task, *args, **kwargs) -> None:
        self._pool.enqueue_job(task.__name__, *args, **kwargs)
```

plus one line at the composition root. **Zero service changes, zero test changes.** A sketch is
left in `notifications/tasks.py` so the shape is not rediscovered.

This is the Dependency Inversion lesson in its most concrete form — and unlike most textbook
examples, the second implementation is genuinely expected rather than hypothetical.

## Consequences — the limitations, stated plainly

`BackgroundTasks` runs **in the same process, after the response is sent**. Therefore:

| Limitation | Effect |
|---|---|
| **Not durable** | a process restart between response and task loses the task silently |
| **No retries** | a transient SMTP failure is a permanently lost notification |
| **No visibility** | nothing to inspect, no dead-letter queue, no metrics |
| **Shares the event loop** | a slow task consumes capacity the API needs |
| **No scheduling** | no delays, no cron, no rate-limited outbound sending |

**The mitigation that makes this acceptable:** the `Notification` row is committed in the request
transaction, *before* dispatch. So the notification always exists and is always visible via
`GET /notifications` — only the out-of-band delivery attempt can be lost. The persisted row is
also the natural basis for a proper outbox later.

**Do not ship this to production with a real email channel.** A dropped password-reset or
donation-confirmation email is a user-visible failure with no trace. The row-first design plus the
Protocol are what make the upgrade a small, well-understood change instead of a rewrite.

## Alternatives

| Option | Rejected because |
|---|---|
| arq worker now | declined; also a fourth container for a stub |
| Celery | heavier still, and its async support is weaker |
| `asyncio.create_task` directly | same durability limits with *no* seam, and unhandled exceptions vanish |
| Postgres outbox + poller | the right long-term answer; more machinery than a stub delivery path justifies. The committed row is deliberately the first half of it. |
