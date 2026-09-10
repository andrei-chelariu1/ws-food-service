"""Structured logging with per-request correlation.

WHY STRUCTLOG AND NOT `logging.info(f"...")`
--------------------------------------------
An f-string log line is a *sentence*. You cannot filter, aggregate, or alert on
a sentence without regexes that break the first time someone rewords the
message. structlog emits **events with fields**:

    log.info("donation_created", donation_id=..., food_item_id=...)
    -> {"event":"donation_created","donation_id":"...","request_id":"..."}

Now `donation_id` is a queryable column in your log platform.

THE `request_id` CONTEXTVAR
---------------------------
The hard part of debugging a concurrent async service is telling whose log line
is whose. A `ContextVar` is per-task, so every log emitted while handling a
request automatically carries that request's id — no threading it through
twenty function signatures. `RequestIdMiddleware` sets it; this module's
processor copies it onto every event.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.config import Settings

# Set by RequestIdMiddleware, read by the processor below. Default "-" so logs
# emitted outside a request (startup, background tasks) are still well-formed.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def bind_request_id(request_id: str) -> None:
    request_id_ctx.set(request_id)


def get_request_id() -> str:
    return request_id_ctx.get()


def _add_request_id(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Copy the ambient request id onto every log event."""
    event_dict["request_id"] = request_id_ctx.get()
    return event_dict


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """uvicorn duplicates its message under `color_message`; drop the noise."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route stdlib logging through it.

    Third-party libraries (uvicorn, sqlalchemy, alembic) use stdlib `logging`.
    Rather than have two log formats in one stream, we install a structlog
    `ProcessorFormatter` on the root handler so *everything* comes out in the
    same shape. One format to parse — DRY at the observability layer.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_request_id,
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Formats exc_info into a readable traceback field.
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append: uvicorn installs its own handler, and leaving
    # it in place would print every line twice.
    root.handlers = [handler]
    root.setLevel(settings.LOG_LEVEL)

    # uvicorn.access is redundant — AccessLogMiddleware logs the same request
    # with more context (request_id, duration, user). Silence the duplicate.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = False

    for name in ("uvicorn", "uvicorn.error", "alembic"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    # SQLAlchemy at INFO logs every statement; DB_ECHO controls that instead.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Module-level logger factory: `log = get_logger(__name__)`."""
    return structlog.stdlib.get_logger(name)
