"""Exception hierarchy + global handlers — the `@ControllerAdvice` equivalent.

THE PROBLEM THIS SOLVES
-----------------------
The naive way to return a 404 from a service is:

    raise HTTPException(status_code=404, detail="Food item not found")

Do that and your *business logic* now imports your *web framework*. Consequences:

* You cannot reuse `FoodItemService` from a CLI command, an arq worker, or a
  gRPC handler without dragging FastAPI along.
* You cannot unit-test the rule "expired items cannot be donated" without
  asserting on an HTTP status code, which is a category error.
* Error response shapes drift: one endpoint returns `{"detail": ...}`, another
  `{"error": ...}`, a third `{"message": ...}`. Clients suffer.

THE FIX
-------
Services raise **domain** exceptions (`NotFoundError`, `BusinessRuleViolation`).
Each carries an HTTP status and a stable machine-readable `code`, but knows
nothing about requests or responses. This module registers handlers on the app
that translate every one of them into a single response shape.

Result:
* Services import nothing from FastAPI. Verify with:
  `grep -r "fastapi" app/modules/*/service.py` -> no matches.
* Every error — domain, validation, 500, or a raw `HTTPException` from a
  dependency — comes out as RFC 9457 `application/problem+json`. One shape,
  documented once, for every failure mode. That is DRY applied to error
  contracts.
* Adding a new error type is one small class (Open/Closed): the handlers below
  never change, because they dispatch on the `AppError` base.

WHY RFC 9457 (`application/problem+json`)
-----------------------------------------
It is the standard for HTTP error bodies, so clients and gateways already
understand `type`/`title`/`status`/`detail`. We add `code`, `request_id` and
optional `errors`. `code` is what clients should branch on — never the prose
`detail`, which is free to be reworded or translated.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_request_id

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


# --------------------------------------------------------------------------
# Error codes — the stable contract clients branch on.
# --------------------------------------------------------------------------
class ErrorCode:
    """String constants, not an Enum, so subclasses in modules can add their own.

    Kept in one class so the full vocabulary is discoverable in one place and
    two modules cannot accidentally invent the same code with different meaning.
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# --------------------------------------------------------------------------
# Domain exception hierarchy
# --------------------------------------------------------------------------
class AppError(Exception):
    """Base for every expected, domain-meaningful failure.

    Subclasses set `status_code` and `code` as class attributes, so raising one
    is a one-liner and the HTTP mapping lives with the error definition rather
    than in a giant `if isinstance(...)` ladder in the handler.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        # Field-level details (validation) and response headers (Retry-After,
        # WWW-Authenticate) — optional, so simple raises stay simple.
        self.errors = errors or []
        self.headers = headers or {}
        super().__init__(self.message)

    def to_problem(self, request_id: str, instance: str) -> dict[str, Any]:
        title = HTTPStatus(self.status_code).phrase
        problem: dict[str, Any] = {
            # A URI reference identifying the problem *kind*. Dereferenceable
            # docs are nice-to-have; stability is the requirement.
            "type": f"/problems/{self.code.lower().replace('_', '-')}",
            "title": title,
            "status": self.status_code,
            "detail": self.message,
            "instance": instance,
            "code": self.code,
            "request_id": request_id,
        }
        if self.errors:
            problem["errors"] = self.errors
        return problem


class ValidationError(AppError):
    """Semantic validation that Pydantic cannot express (e.g. cross-field rules)."""

    # The literal, not `status.HTTP_422_UNPROCESSABLE_ENTITY`: Starlette renamed that
    # constant to `HTTP_422_UNPROCESSABLE_CONTENT` and deprecated the old name, so
    # either spelling breaks on some supported version. The number is stable forever.
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "Request validation failed"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND
    message = "Resource not found"

    def __init__(self, resource: str = "Resource", identifier: object = None) -> None:
        detail = (
            f"{resource} not found" if identifier is None else f"{resource} {identifier} not found"
        )
        super().__init__(detail)


class ConflictError(AppError):
    """The request contradicts current state (duplicate key, stale version)."""

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT
    message = "Resource conflict"


# N818 wants an `Error` suffix. Deliberately not renamed: `BusinessRuleViolation` is
# the name domain experts use for this concept, and a service reading
# `raise BusinessRuleViolation("expired food cannot be donated")` says exactly what
# happened. Ubiquitous language beats a linter's naming convention here.
class BusinessRuleViolation(AppError):  # noqa: N818
    """A domain invariant was violated — e.g. "expired food cannot be donated".

    409 rather than 400: the *request* was well-formed; the current state of the
    world makes it impossible. Clients cannot fix it by editing the payload.
    """

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.BUSINESS_RULE_VIOLATION
    message = "Operation violates a business rule"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.AUTHENTICATION_FAILED
    message = "Authentication failed"

    def __init__(self, message: str | None = None) -> None:
        # RFC 6750 requires this header on a 401 from a Bearer-protected resource.
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    """Authenticated but not allowed. 403, never 401 — the difference matters:
    401 tells the client "retry with credentials", 403 tells it "don't bother"."""

    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.PERMISSION_DENIED
    message = "You do not have permission to perform this action"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.SERVICE_UNAVAILABLE
    message = "Service temporarily unavailable"


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def _problem_response(
    request: Request,
    error: AppError,
) -> JSONResponse:
    """Single place that builds an error response. Every handler funnels here."""
    request_id = get_request_id()
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_problem(request_id=request_id, instance=request.url.path),
        media_type=PROBLEM_JSON,
        headers=error.headers or None,
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handles every `AppError` subclass — present and future (Open/Closed)."""
    assert isinstance(exc, AppError)
    # 4xx is client behaviour, not an incident: log at INFO/WARNING, not ERROR,
    # so real problems remain visible in the noise.
    level = "warning" if exc.status_code >= 500 else "info"
    getattr(log, level)(
        "app_error",
        code=exc.code,
        status_code=exc.status_code,
        detail=exc.message,
        path=request.url.path,
        method=request.method,
    )
    return _problem_response(request, exc)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI/Pydantic request validation -> the same problem+json envelope.

    Without this, 422s would be the *only* error shape a client has to special
    case. Normalising it is the whole point of having one envelope.
    """
    assert isinstance(exc, RequestValidationError)
    errors = [
        {
            # Drop the leading "body"/"query" segment: clients care about the
            # field path, not FastAPI's internal location tuple.
            "field": ".".join(str(part) for part in err["loc"][1:]) or str(err["loc"][0]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    log.info("request_validation_failed", path=request.url.path, errors=errors)
    return _problem_response(
        request,
        ValidationError("One or more fields are invalid", errors=errors),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches `HTTPException` raised by FastAPI internals and third parties.

    We do not raise these ourselves, but the framework does (404 for an unknown
    route, 405 for a wrong method). Re-wrapping keeps *those* consistent too —
    otherwise a typo'd URL returns a different shape than every real error.
    """
    assert isinstance(exc, StarletteHTTPException)
    code = {
        status.HTTP_401_UNAUTHORIZED: ErrorCode.AUTHENTICATION_FAILED,
        status.HTTP_403_FORBIDDEN: ErrorCode.PERMISSION_DENIED,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    }.get(exc.status_code, ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else "HTTP_ERROR")

    return _problem_response(
        request,
        AppError(
            str(exc.detail),
            code=code,
            status_code=exc.status_code,
            headers=dict(exc.headers or {}),
        ),
    )


async def integrity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Unique/FK constraint violations -> 409, with the DB message withheld.

    A `UniqueViolation` body would leak table and constraint names, which is
    free reconnaissance for an attacker. Services should catch the common cases
    first and raise a specific `ConflictError`; this is the safety net.
    """
    assert isinstance(exc, IntegrityError)
    log.warning(
        "database_integrity_error",
        path=request.url.path,
        error=str(exc.orig),  # full detail to logs, never to the client
    )
    return _problem_response(
        request,
        ConflictError("The request conflicts with existing data"),
    )


async def sqlalchemy_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Any other DB failure -> 503, not 500: it is usually transient (pool
    exhausted, connection dropped), and 503 tells clients retrying is sensible."""
    assert isinstance(exc, SQLAlchemyError)
    log.error("database_error", path=request.url.path, error=str(exc), exc_info=True)
    return _problem_response(
        request,
        ServiceUnavailableError("A database error occurred, please retry"),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Logs everything, reveals nothing.

    The client gets a generic message plus the `request_id`. That id is the
    bridge: the user quotes it in a support ticket and an engineer greps the
    logs for the full traceback. This is how you get debuggability *without*
    leaking stack traces to the internet.
    """
    log.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        error=str(exc),
        exc_info=True,
    )
    return _problem_response(
        request,
        AppError(
            "An internal error occurred. Quote the request_id when reporting this.",
            code=ErrorCode.INTERNAL_ERROR,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler. Called once from `create_app()`.

    Order does not matter — Starlette dispatches on the most specific
    registered class — but the list reads as a checklist of "every way a
    request can fail", which is exactly what you want to be able to audit.
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
