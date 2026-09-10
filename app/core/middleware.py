"""Cross-cutting HTTP middleware: request ids, access logs, security headers.

WHY MIDDLEWARE AND NOT A DECORATOR ON EVERY ROUTE
-------------------------------------------------
Correlation ids, timing and security headers apply to *every* request,
including ones that never reach a route (404s, validation failures). A
per-route decorator would have to be remembered forty times and would still
miss those cases. Middleware is the aspect-oriented seam: write it once, it
applies everywhere, and nobody can forget it. (DRY, and Single Responsibility —
route handlers stay about the domain.)

ORDERING MATTERS
----------------
Starlette runs middleware in **reverse** registration order on the way in.
`register_middleware` documents the resulting chain explicitly, because getting
this wrong is a classic bug: if the request-id middleware ran *after* the
logging middleware, every access log line would say `request_id="-"`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.logging import bind_request_id, get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
RESPONSE_TIME_HEADER = "X-Response-Time-ms"

CallNext = Callable[[Request], Awaitable[Response]]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns (or adopts) a correlation id for the request.

    If an upstream proxy or the client already sent `X-Request-ID` we reuse it,
    so a trace spans multiple services. Otherwise we mint one. Either way it is
    bound to a ContextVar (so every log line carries it) and echoed back in the
    response (so the caller can quote it in a bug report).
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        # Length-cap an adopted id: it lands in logs and headers, so an
        # attacker-controlled megabyte string is a log-injection vector.
        request_id = incoming[:64] if incoming else uuid.uuid4().hex

        bind_request_id(request_id)
        # Also on request.state so handlers/dependencies can read it without
        # importing the contextvar.
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured log line per request, with duration.

    Replaces `uvicorn.access` (silenced in core/logging.py) because that logger
    cannot see the request id, the authenticated user, or our own timing.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers produce the response; we only need to make
            # sure the request still appears in the access log, then re-raise.
            duration_ms = (time.perf_counter() - start) * 1000
            log.warning(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[RESPONSE_TIME_HEADER] = f"{duration_ms:.2f}"

        # Server errors are incidents; client errors are not. Splitting the
        # level here is what makes `level>=error` a usable alerting filter.
        level = "error" if response.status_code >= 500 else "info"
        getattr(log, level)(
            "request_completed",
            method=request.method,
            path=request.url.path,
            query=str(request.url.query) or None,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        return response


class SecurityHeadersMiddleware:
    """Adds defensive response headers.

    Written as raw ASGI rather than `BaseHTTPMiddleware` because it only needs
    to touch the response *start* message — no body buffering, so it costs
    essentially nothing per request.

    Each header, and why:
      X-Content-Type-Options   stop MIME sniffing (a JSON response being
                               executed as HTML is a stored-XSS vector)
      X-Frame-Options          clickjacking
      Referrer-Policy          don't leak our URLs (which contain ids) to
                               third parties
      Content-Security-Policy  minimal policy; this is a JSON API, so nothing
                               should ever be loaded from it. See the
                               `docs_paths` note below for the one exception.
      Permissions-Policy       deny sensor/device access outright
      HSTS                     force TLS. Production only — sending it over
                               plain HTTP in dev would pin localhost to https
                               in the developer's browser for a year.
    """

    # The API's own policy: this service returns JSON, so nothing it serves
    # should ever be loaded, framed, or executed by a browser.
    API_CSP = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

    # Swagger UI and ReDoc are real HTML pages that FastAPI builds for us, and
    # they cannot render under `default-src 'none'`:
    #
    #   * the page bootstraps itself with an INLINE <script> ->  'unsafe-inline'
    #   * the bundle and stylesheet come from jsdelivr        ->  cdn.jsdelivr.net
    #   * the favicon comes from fastapi.tiangolo.com         ->  img-src that host
    #   * ReDoc builds its fonts/icons as blob:/data: URLs    ->  blob: data:
    #
    # Observed as three console errors ("violates the following Content Security
    # Policy directive: default-src 'none'") and a blank /docs page. The strict
    # policy was not wrong — it was applied to a page it was never written for.
    #
    # Why relaxing it here is acceptable, and not a hole:
    #
    #   1. It applies ONLY to the two documentation paths. Every API response,
    #      including error bodies, still gets `API_CSP`.
    #   2. `docs_url`/`redoc_url` are `None` in production (see config.py), so
    #      `docs_paths` is EMPTY there and this branch is unreachable. The
    #      relaxation is structurally a development-only concession.
    #   3. Nothing user-controlled is rendered on these pages, so the XSS sink
    #      that 'unsafe-inline' normally guards against does not exist here.
    #
    # The alternative is vendoring swagger-ui-dist and serving it from
    # StaticFiles, which removes the CDN dependency and lets the page run
    # without 'unsafe-inline'. That is the right move if you ever want /docs
    # in production or must work offline; it is more machinery than a
    # development aid needs here.
    DOCS_CSP = (
        b"default-src 'none'; "
        b"script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        b"img-src 'self' data: https://fastapi.tiangolo.com; "
        b"font-src 'self' data: https://cdn.jsdelivr.net; "
        b"worker-src 'self' blob:; "
        b"connect-src 'self'; "
        b"frame-ancestors 'none'; base-uri 'self'"
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        enable_hsts: bool,
        docs_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self._docs_paths = docs_paths
        base: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if enable_hsts:
            base.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        # Precomputed so the hot path is a set lookup plus a list extend, with
        # no per-request header construction.
        self._headers = [*base, (b"content-security-policy", self.API_CSP)]
        self._docs_headers = [*base, (b"content-security-policy", self.DOCS_CSP)]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Raw ASGI, using Starlette's own `Scope`/`Receive`/`Send` aliases.

        Spelling these properly rather than `dict`/`Callable` is what lets mypy verify
        the `send` wrapper below actually matches the signature Starlette will call it
        with — the earlier `dict` annotation forced a `type: ignore` that hid a genuine
        mismatch (`dict` is not `MutableMapping[str, Any]`).
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = self._docs_headers if scope.get("path") in self._docs_paths else self._headers

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].extend(headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def get_client_ip(request: Request) -> str:
    """Best-effort client IP, proxy-aware.

    SECURITY NOTE: `X-Forwarded-For` is client-controlled unless a trusted
    proxy overwrites it. We take the *first* entry (the original client) which
    is correct behind a well-configured ingress, and is what the rate limiter
    keys on. If you deploy without a proxy that rewrites this header, an
    attacker can rotate the value to evade IP-based limits — see
    docs/RATE_LIMITING.md for the deployment requirement.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Install the middleware stack.

    Registration is bottom-up: the LAST one added is the OUTERMOST, i.e. the
    first to see an incoming request. The resulting inbound order is:

        TrustedHost      -> reject forged Host headers before anything else
        CORS             -> answer OPTIONS preflights early, cheaply
        SecurityHeaders  -> wraps everything, so even error responses get headers
        RequestId        -> must precede AccessLog, or logs lose the id
        AccessLog        -> innermost, so its timing excludes middleware overhead
                            it cannot influence and includes the real handler
    """
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    # `docs_url`/`redoc_url` are None in production, so this set is empty there
    # and every response — without exception — gets the strict API policy.
    docs_paths = frozenset(path for path in (settings.docs_url, settings.redoc_url) if path)
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.is_production,
        docs_paths=docs_paths,
    )

    # Compress JSON list responses; 1 KiB floor avoids wasting CPU on small ones.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            # An explicit allowlist. `allow_origins=["*"]` together with
            # `allow_credentials=True` is rejected by browsers anyway, and is a
            # CSRF footgun besides.
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER, RESPONSE_TIME_HEADER, "Retry-After"],
            max_age=600,
        )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
