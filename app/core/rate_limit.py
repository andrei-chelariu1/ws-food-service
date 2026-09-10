"""Rate limiting via slowapi, backed by Redis.

WHY REDIS AND NOT IN-MEMORY
---------------------------
slowapi's default storage is a process-local dict. That is worse than useless in
production:

* Run 4 uvicorn workers and a "5 per minute" login limit becomes 20 per minute,
  because each worker counts separately.
* Scale to 3 replicas and it becomes 60.
* Restart a pod and every counter resets, so an attacker just waits for a deploy.

A shared Redis counter is the whole point: one limit, correctly enforced, no
matter how many processes serve traffic.

WHY THE KEY IS "USER, THEN IP"
------------------------------
Keying purely on IP punishes everyone behind one corporate NAT or mobile
carrier gateway. Keying purely on user id leaves unauthenticated endpoints
(login, register — the ones that actually need protecting) unprotected.

So: if the request carries an authenticated user, key on the user id; otherwise
key on the client IP. Authenticated abuse is attributable to an account;
anonymous abuse is attributable to an address. See docs/RATE_LIMITING.md.

TIERS
-----
Different endpoints have different abuse profiles, so one global number cannot
be right for all of them:

    default   200/min   generous — a limit, not a quota
    login       5/min   password brute force
    register    3/hour  signup spam / mass account creation
    writes     30/min   protects the database from a runaway client
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger, get_request_id
from app.core.middleware import get_client_ip
from app.core.redis import KEY_PREFIX_RATE_LIMIT

log = get_logger(__name__)


class RateLimitExceededError(AppError):
    """Our own 429, so it flows through the same problem+json handler as
    everything else instead of slowapi's bespoke plain-text response."""

    status_code = 429
    code = ErrorCode.RATE_LIMIT_EXCEEDED
    message = "Rate limit exceeded"


def rate_limit_key(request: Request) -> str:
    """Identify the caller: authenticated user if known, else client IP.

    Reads `request.state.user_id`, which `shared/security/dependencies.py` sets
    once the bearer token has been validated.

    ORDERING CAVEAT: `SlowAPIMiddleware` runs *before* route dependencies, so
    for the global default limit `state.user_id` is not yet set and this falls
    back to IP. Per-route `@limiter.limit(...)` decorators run *after*
    dependency resolution and do see the user. That is the desired behaviour —
    the global limit is a coarse anti-flood net, per-route limits are precise —
    but it is worth knowing rather than discovering.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_client_ip(request)}"


def create_limiter(settings: Settings) -> Limiter:
    return Limiter(
        key_func=rate_limit_key,
        # A shared store is what makes the limit real across processes.
        storage_uri=settings.redis_url_str,
        key_prefix=KEY_PREFIX_RATE_LIMIT,
        default_limits=[settings.RATE_LIMIT_DEFAULT],
        headers_enabled=True,  # emit X-RateLimit-* so clients can self-throttle
        enabled=settings.RATE_LIMIT_ENABLED,
        # Sliding window: fairer than a fixed window, which lets a client send
        # 2x the limit by straddling a window boundary.
        strategy="moving-window",
        # ────────────────────────────────────────────────────────────────────
        # THE RATE LIMITER MUST FAIL **OPEN**. This one line is load-bearing.
        #
        # Without it, a Redis outage makes the limiter raise on *every* request —
        # and because `SlowAPIMiddleware` wraps everything, that includes
        # `/health/live`. A liveness probe returning 500 tells Kubernetes the
        # container is broken, so it restarts every replica: a recoverable Redis
        # blip becomes a full outage with a thundering-herd restart on recovery.
        #
        # (Observed, not theorised: `docker compose stop redis` produced
        # `500 INTERNAL_ERROR` on `/health/live` before this was added.)
        #
        # THE TRADE-OFF, STATED PLAINLY: while Redis is unavailable, rate
        # limiting degrades to *no* limiting, so login brute-force protection is
        # temporarily absent. That is accepted because:
        #   * the alternative is total unavailability, which is strictly worse;
        #   * the JWT denylist fails CLOSED, so authenticated traffic is rejected
        #     anyway during the same outage;
        #   * the window is short — Redis is a `depends_on: service_healthy`
        #     dependency and the readiness probe pulls the instance out of
        #     rotation.
        #
        # Note this is the *opposite* choice from `RedisTokenDenylist`, which
        # fails closed. The rule: availability controls fail open, security
        # controls fail closed. See docs/RATE_LIMITING.md.
        # ────────────────────────────────────────────────────────────────────
        swallow_errors=True,
    )


# Module-level singleton because slowapi's `@limiter.limit(...)` decorator is
# applied at import time, when route functions are defined — there is no
# request context to inject into yet. `get_settings()` is cached, so this reads
# configuration exactly once.
#
# ────────────────────────────────────────────────────────────────────────────
# REQUIRED ENDPOINT SIGNATURE FOR `@limiter.limit(...)`
#
# Every decorated route MUST declare both:
#
#     async def handler(request: Request, response: Response, ...)
#
# `request`  — slowapi reads it to compute the rate-limit key.
# `response` — with `headers_enabled=True` (below), slowapi writes the
#              `X-RateLimit-*` headers onto it. Omit it and *every call to that
#              endpoint* fails with:
#
#     Exception: parameter `response` must be an instance of
#                starlette.responses.Response
#
# Both parameters are unused by the handler body; each carries a short comment at
# its declaration site pointing back here.
#
# THIS BIT US, AND IT IS WORTH KNOWING WHY THE TESTS DID NOT CATCH IT: the suite
# sets `RATE_LIMIT_ENABLED=false`, which makes the decorator a no-op — so a
# missing `response` parameter is invisible until the limiter is actually
# enabled, i.e. in the deployed container. The gap is now closed by a structural
# test, `tests/shared/test_rate_limit_key.py::test_every_rate_limited_route_
# declares_request_and_response`, which inspects the route signatures directly
# and needs neither Redis nor a live limiter.
# ────────────────────────────────────────────────────────────────────────────
limiter: Limiter = create_limiter(get_settings())


async def rate_limit_handler(request: Request, exc: Exception) -> Response:
    """Translate slowapi's exception into our standard error envelope."""
    assert isinstance(exc, RateLimitExceeded)

    # `exc.limit` is Optional in slowapi's own types, so it is read defensively —
    # an error handler that raises an AttributeError while reporting an error is the
    # worst possible failure mode.
    limit_description = str(exc.limit.limit) if exc.limit is not None else "the configured limit"

    # Visibility matters here: a spike in this log line is either an attack or a
    # misbehaving client, and both are things you want to see.
    log.warning(
        "rate_limit_exceeded",
        key=rate_limit_key(request),
        path=request.url.path,
        method=request.method,
        limit=limit_description,
    )

    retry_after = str(getattr(exc, "retry_after", None) or 60)
    error = RateLimitExceededError(
        f"Rate limit exceeded: {limit_description}. Retry after {retry_after}s.",
        # Retry-After is not decoration — a well-behaved client honours it, and
        # without it clients hammer you harder while limited.
        headers={"Retry-After": retry_after},
    )

    from fastapi.responses import JSONResponse  # local import: avoids a cycle

    return JSONResponse(
        status_code=error.status_code,
        content=error.to_problem(request_id=get_request_id(), instance=request.url.path),
        media_type="application/problem+json",
        headers=error.headers,
    )


def register_rate_limiting(app: FastAPI, settings: Settings) -> None:
    """Attach the limiter to the app. Called once from `create_app()`."""
    app.state.limiter = limiter  # slowapi's decorator looks it up here
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    if settings.RATE_LIMIT_ENABLED:
        # Applies `default_limits` to every route without per-route decoration.
        app.add_middleware(SlowAPIMiddleware)
        log.info("rate_limiting_enabled", default=settings.RATE_LIMIT_DEFAULT)
    else:
        log.warning("rate_limiting_disabled")
