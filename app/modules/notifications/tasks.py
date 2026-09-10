"""Background work: the `TaskDispatcher` abstraction.

WHY THIS FILE EXISTS AT ALL
---------------------------
ARCHITECTURE.md specifies arq for background jobs. Per the project decision, no
arq worker is deployed (see docs/adr/0004-backgroundtasks-behind-protocol.md), so
side effects run in FastAPI's `BackgroundTasks` — after the response is sent, in
the same process.

The naive version of that decision is to sprinkle `background_tasks.add_task(...)`
through the services. Then `NotificationService` imports FastAPI, becomes
untestable without a request, and the day arq *is* introduced every call site
changes.

Instead, services depend on the `TaskDispatcher` **Protocol** — one method,
`dispatch(fn, *args)`. Three implementations exist below. Adding an arq adapter
later is a fourth, roughly fifteen lines, and **no service changes**. That is the
Dependency Inversion Principle earning its keep on a decision that was made *for
us*, which is exactly when an abstraction pays for itself.

THE HONEST LIMITATIONS OF `BackgroundTasks`
-------------------------------------------
Worth stating plainly, because "background" sounds safer than it is:

* **No durability.** The process dies, the task is gone. Nothing retries it.
* **No retries, no backoff, no dead-letter queue.**
* **Same process.** A slow task consumes a worker slot that could serve requests.
* **Runs after the response but inside the request's lifetime**, so it must not
  use the request's database session — that session is already committed and
  closed.

That last point is why the notification *row* is written inside the request
transaction (durable, transactional) and only the *external delivery* is
dispatched. The design keeps the important half safe and the unreliable half
optional. If email or push is added, `ArqTaskDispatcher` becomes necessary — the
Protocol is the seam that makes it a small change rather than a refactor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger

log = get_logger(__name__)

# A task is any async callable. Deliberately not narrower: constraining the
# signature would force every future task into one shape for no benefit.
TaskCallable = Callable[..., Awaitable[None]]


@runtime_checkable
class TaskDispatcher(Protocol):
    """Fire-and-forget side-effect scheduling.

    One method. Services depend on this and nothing else, so they neither know nor
    care whether the work runs in-process, in a worker, or not at all.
    """

    def dispatch(self, task: TaskCallable, *args: Any, **kwargs: Any) -> None: ...


class BackgroundTasksDispatcher:
    """Runs tasks in FastAPI's `BackgroundTasks` — after the response is sent.

    The one place in the codebase that touches FastAPI's background machinery. It is
    constructed per request in `api.py` (that is where `BackgroundTasks` is
    injectable) and passed to the service as a `TaskDispatcher`.

    Note it is not `async`: `add_task` only *queues*. The response is not delayed.
    """

    def __init__(self, background_tasks: Any) -> None:
        # Typed `Any` rather than `fastapi.BackgroundTasks` so this module has no
        # FastAPI import at all. The duck-typed surface is a single method
        # (`add_task`), which keeps `tasks.py` importable from anywhere — including
        # a CLI or a worker entrypoint that never loads a web framework.
        self._background_tasks = background_tasks

    def dispatch(self, task: TaskCallable, *args: Any, **kwargs: Any) -> None:
        self._background_tasks.add_task(task, *args, **kwargs)
        log.debug("task_dispatched_inprocess", task=getattr(task, "__name__", repr(task)))


class ImmediateTaskDispatcher:
    """Drops tasks, logging what would have run.

    For tests and for CLI/migration contexts where there is no request and no event
    loop worth scheduling on. `NotificationService` behaves identically with this
    injected — which is the proof that dispatching is genuinely a side effect and
    not part of any business rule.
    """

    def dispatch(self, task: TaskCallable, *args: Any, **kwargs: Any) -> None:
        log.debug("task_skipped", task=getattr(task, "__name__", repr(task)))


# --------------------------------------------------------------------------
# Task implementations
# --------------------------------------------------------------------------
async def send_notification_email(
    email: str,
    subject: str,
    body: str,
) -> None:
    """Deliver a notification by email.

    A stub: it logs instead of sending, because no SMTP provider is configured in a
    sample project. Deliberately kept as a real function rather than deleted, so
    the dispatch path is exercised end-to-end and the integration point is obvious.

    NOTE WHAT IT MUST NOT DO: touch the database. By the time this runs, the
    request's session is committed and closed. A task that needs data must either
    receive it as arguments (as here) or open its own session — and a task opening
    its own session is the signal that it belongs in a real worker with a real
    queue, not in `BackgroundTasks`.

    Exceptions raised here are swallowed by Starlette and logged; nothing retries.
    That is acceptable only because the notification row is already persisted, so
    the user still sees it in-app. See the module docstring.
    """
    log.info(
        "notification_email_would_be_sent",
        to=email,
        subject=subject,
        body_preview=body[:80],
    )


# WHERE AN ARQ ADAPTER WOULD GO
#
#   class ArqTaskDispatcher:
#       def __init__(self, redis_pool: ArqRedis) -> None:
#           self._pool = redis_pool
#
#       def dispatch(self, task, *args, **kwargs) -> None:
#           # enqueue_job is async; in a sync dispatch, schedule it:
#           asyncio.create_task(self._pool.enqueue_job(task.__name__, *args, **kwargs))
#
# Plus a worker entrypoint (`WorkerSettings` listing the task functions) and a
# fourth container in docker-compose.yml. The service layer does not change — which
# is the entire point of the Protocol above.
