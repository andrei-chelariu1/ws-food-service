"""Application entrypoint and composition root.

WHAT A COMPOSITION ROOT IS, AND WHY IT MATTERS
----------------------------------------------
This is the one file allowed to know about everything: configuration, logging,
Redis, the database, every module's router, and the binding of abstractions to
implementations. Every *other* file depends only on what it directly needs.

Concentrating the wiring here is what keeps the rest of the codebase decoupled.
The clearest example is three lines down in `create_app()`:

    app.dependency_overrides[get_user_authenticator] = provide_user_authenticator

`shared/security` needs to authenticate users. `modules/users` knows how. Neither
imports the other — this line joins them. That is the Dependency Inversion
Principle with a visible seam, rather than an import that quietly couples shared
plumbing to one feature.

WHY `create_app()` IS A FACTORY AND NOT A MODULE-LEVEL `app = FastAPI()`
-----------------------------------------------------------------------
A factory can be called more than once, with different settings. That is what lets
the test suite build an app with an in-memory cache and a test database without
mutating global state, and it is what makes each test independent. A module-level
singleton would force tests to monkeypatch whatever it captured at import time.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api_router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware
from app.core.rate_limit import limiter, register_rate_limiting
from app.core.redis import close_redis, init_redis, redis_healthy
from app.modules.users.api import provide_user_authenticator
from app.shared.db.session import database_healthy, dispose_engine
from app.shared.security.dependencies import get_user_authenticator

log = get_logger(__name__)

DESCRIPTION = """
Backend for a food-waste reduction platform: donors list surplus food, recipients
request it.

**Reading this API**
* Every error response is RFC 9457 `application/problem+json`. Branch on the
  `code` field, never on the prose `detail`.
* Every response carries `X-Request-ID`. Quote it when reporting a problem — it
  appears in the server logs alongside the full traceback.
* `POST /api/v1/auth/login`, then paste the `access_token` into **Authorize**.
* Refresh tokens are single-use: each refresh returns a new one and invalidates
  the old.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown, in one place.

    WHY LIFESPAN AND NOT `@app.on_event("startup")`: the deprecated decorators
    cannot guarantee that a resource opened at startup is the same one closed at
    shutdown, and they run in a less predictable order. A context manager makes the
    pairing structural — whatever is set up before `yield` is torn down after, on
    every path including a failed startup.

    WHY CONNECT TO REDIS EAGERLY: `init_redis` pings. If Redis is unreachable, the
    process fails to start and the orchestrator does not route traffic to it. The
    alternative — connect lazily on first use — means the container reports healthy
    and then 500s on the first real request. **Fail at boot, not in front of a user.**
    """
    settings: Settings = app.state.settings

    configure_logging(settings)
    # Checked here rather than in a validator: tests legitimately construct
    # `Settings` with development defaults, and this must only gate real startup.
    settings.assert_production_safe()

    log.info(
        "application_starting",
        environment=settings.ENVIRONMENT,
        debug=settings.DEBUG,
        version=app.version,
    )

    await init_redis(settings)

    # NOTE: migrations are NOT run here. `scripts/entrypoint.sh` runs
    # `alembic upgrade head` before the server starts. Running them in lifespan
    # would mean N replicas racing to migrate the same database on every deploy —
    # see migrations/README.md.

    yield

    log.info("application_stopping")
    await close_redis()
    await dispose_engine()
    log.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    The order of registration below is deliberate and documented inline; several of
    these steps depend on earlier ones.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Disabled in production: interactive docs are API surface, and a schema
        # dump is free reconnaissance. Controlled by computed properties on
        # `Settings`, so the rule is expressed once (see core/config.py).
        docs_url=settings.docs_url,
        redoc_url=settings.redoc_url,
        openapi_url=settings.openapi_url,
        # Trailing-slash redirects turn a POST into a GET on some clients and leak
        # the Authorization header to the redirect target on others. Off.
        redirect_slashes=False,
    )

    # Settings on app.state so `lifespan` (which receives only the app) can reach
    # them without importing the cached global — the same mechanism tests use to
    # inject a different configuration.
    app.state.settings = settings

    # 1. Exception handlers first, so a failure during any later setup step is
    #    already rendered in the standard problem+json envelope.
    register_exception_handlers(app)

    # 2. Rate limiting before general middleware: it registers its own exception
    #    handler and needs `app.state.limiter` in place before routes are added.
    register_rate_limiting(app, settings)

    # 3. Middleware. `register_middleware` documents the resulting inbound order —
    #    getting it wrong is how access logs end up without request ids.
    register_middleware(app, settings)

    # 4. THE DEPENDENCY INVERSION SEAM. `shared/security` declares that it needs a
    #    `UserAuthenticator`; the users module provides one. This line is the only
    #    connection between them, and without it every protected endpoint fails
    #    loudly with a NotImplementedError rather than silently authenticating
    #    nobody. See app/shared/security/dependencies.py.
    app.dependency_overrides[get_user_authenticator] = provide_user_authenticator

    # 5. Routes.
    register_root_route(app)
    register_health_routes(app)
    app.include_router(api_router)

    log.info("application_configured", routes=len(app.routes))
    return app


def register_root_route(app: FastAPI) -> None:
    """A service banner at `/`.

    Registered as a function rather than written inline in `create_app` for the same
    reason as `register_health_routes`: `create_app` stays a readable list of numbered
    steps, and each route group keeps its rationale next to itself.

    WHY IT RETURNS LINKS AND NOT `{"message": "Hello World"}`
    ---------------------------------------------------------
    `/` is the URL a human types first and the one an uptime monitor is most likely to
    be pointed at. Both want the same thing: *what is this, and where do I go next?*
    A greeting answers neither.

    So it names the service, its version, and the two links a caller actually needs —
    `docs_url` is `None` in production, so the key is simply absent there rather than
    advertising a 404.

    NOT A HEALTH CHECK. It touches nothing external, so a `200` here says only that the
    process is running. Point orchestrators at `/health/live` and `/health/ready`, which
    make that distinction explicitly (see `register_health_routes`).

    `include_in_schema=False` because a banner is not part of the API contract; the
    contract lives under `/api/v1`. It is deliberately NOT `@limiter.exempt` — unlike a
    probe, nothing needs to poll this endpoint every second, so the global limit applies.

    It takes no `Request`: the settings come from the closure, which is why the handler
    has no parameters at all. Declaring `request: Request` on a route that does not need
    it is not free here — `tests/shared/test_rate_limit_key.py` uses that parameter as
    the marker for "rate-limited endpoint" and would then also demand a `Response`.
    """
    settings: Settings = app.state.settings

    @app.get("/", include_in_schema=True, tags=["meta"])
    async def root() -> dict[str, str]:
        """Service banner: what this is and where to go next."""
        payload = {
            "service": settings.APP_NAME,
            "version": app.version,
            "environment": settings.ENVIRONMENT,
            "api": settings.API_V1_PREFIX,
            "health": "/health/ready",
        }
        if settings.docs_url:
            payload["docs"] = settings.docs_url
        return payload


def register_health_routes(app: FastAPI) -> None:
    """Liveness and readiness probes.

    THE DISTINCTION IS NOT PEDANTRY — orchestrators act differently on each:

    * **`/health/live`** — "is this process alive?" Answers from memory, touching
      nothing external. A failure means *restart the container*.
      It must NOT check the database: a brief database outage would otherwise make
      Kubernetes kill every replica, turning a recoverable blip into a full outage
      with a thundering-herd restart on the way back.

    * **`/health/ready`** — "can this process serve traffic?" Checks Postgres and
      Redis. A failure means *stop sending requests here*, without restarting.
      The container recovers on its own when its dependencies do.

    Both are excluded from the OpenAPI schema (`include_in_schema=False`): they are
    infrastructure endpoints, not part of the public contract.

    BOTH ARE ALSO EXEMPT FROM RATE LIMITING (`@limiter.exempt`). Two reasons, and the
    second one is the important one:

    * Probes fire every few seconds from every node. They would consume the caller's
      budget and pollute the counters for no purpose.
    * More seriously: the global limiter runs as middleware, so an *unexempted* health
      endpoint depends on Redis being reachable. When Redis is down that turns
      `/health/live` into a 500, Kubernetes concludes the container is broken, and it
      restarts every replica — amplifying a recoverable dependency blip into a full
      outage. `swallow_errors=True` on the limiter is the primary defence
      (see core/rate_limit.py); this exemption is the belt to that braces.
    """

    @app.get("/health/live", include_in_schema=False, tags=["health"])
    # slowapi ships no type annotations for `exempt`, so mypy --strict treats the
    # decorated function as untyped. The decorator is a pass-through marker.
    @limiter.exempt  # type: ignore[untyped-decorator]
    async def liveness() -> dict[str, str]:
        """Is the process alive? Answers from memory, touching nothing external."""
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False, tags=["health"])
    @limiter.exempt  # type: ignore[untyped-decorator]
    async def readiness() -> JSONResponse:
        checks: dict[str, Any] = {
            "database": "ok" if await database_healthy() else "unavailable",
            "redis": "ok" if await redis_healthy() else "unavailable",
        }
        all_ok = all(value == "ok" for value in checks.values())
        # 503, not 200-with-a-body: orchestrators and load balancers read the status
        # code. A 200 saying `{"database": "unavailable"}` is reported as healthy by
        # every probe that does not parse JSON — which is most of them.
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content={"status": "ready" if all_ok else "not_ready", **checks},
        )


# The ASGI entrypoint: `uvicorn app.main:app`.
app = create_app()
