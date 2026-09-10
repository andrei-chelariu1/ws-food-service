"""Async engine, session factory, and the request-scoped Unit of Work.

THE UNIT OF WORK, AND WHY `get_db` IS SHAPED LIKE THIS
------------------------------------------------------
`get_db()` yields one session per HTTP request and then:

    * commits if the handler returned normally, or
    * rolls back if anything was raised.

That single rule gives you **atomic requests**. Consider the donation flow: it
inserts a `Donation` row, flips a `FoodItem` to RESERVED, and inserts a
`Notification`. If the business rule check fails halfway, you must not be left
with a reserved item and no donation. Because all three writes share one
transaction and the exception propagates past this dependency, the rollback is
automatic and nobody had to remember it.

Compare the alternative — `await session.commit()` sprinkled through every
service method. Then each write is its own transaction, partial failures leave
inconsistent state, and the correctness of the system depends on forty
individual authors not forgetting a line. This is the difference between a rule
enforced by structure and a rule enforced by discipline.

CONSEQUENCE FOR SERVICES: **services never call `commit()`.** They may call
`flush()` (to get a generated id, or to trigger a constraint check early), but
the commit boundary belongs to the request. This is stated again in
app/modules/README.md because it is the rule most often broken.

WHY `expire_on_commit=False`
----------------------------
By default SQLAlchemy expires every attribute after commit, so the next
attribute read issues a fresh SELECT. In async code that lazy load happens
*after* the session closed, raising `MissingGreenlet`. Since `get_db` commits
at the very end of the request, FastAPI then serialises the returned ORM object
— and would blow up. Disabling expiry is the correct setting for this pattern.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    Pooling notes:
      * `pool_pre_ping` sends a cheap `SELECT 1` before handing out a connection.
        Costs a microsecond; prevents the classic "first request after an idle
        night fails" caused by the DB or a proxy silently dropping connections.
      * `pool_recycle` closes connections older than 30 min, staying under
        typical proxy/idle timeouts.
      * `NullPool` under pytest: each test gets its own event loop, and a pooled
        asyncpg connection bound to a dead loop raises confusing errors. Not
        pooling in tests trades a little speed for reliability.
    """
    is_test = settings.ENVIRONMENT == "development" and "test" in settings.database_url_str

    return create_async_engine(
        settings.database_url_str,
        echo=settings.DB_ECHO,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        **(
            {"poolclass": NullPool}
            if is_test
            else {
                "pool_size": settings.DB_POOL_SIZE,
                "max_overflow": settings.DB_MAX_OVERFLOW,
                "pool_recycle": 1800,
                "pool_timeout": 30,
            }
        ),
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,  # see module docstring
        autoflush=False,  # explicit flush() only — no surprise SQL mid-method
        autocommit=False,
    )


# --------------------------------------------------------------------------
# The process-wide engine, created LAZILY.
#
# One engine — and therefore one connection pool — per process. Creating an engine
# per request would create a pool per request, defeating the purpose of pooling.
#
# WHY LAZY AND NOT `engine = create_engine(get_settings())` AT IMPORT TIME:
# `create_async_engine` imports the DBAPI driver eagerly. With a module-level call,
# merely importing *any* module that transitively reaches this file would require
# `asyncpg` to be installed and `DATABASE_URL` to be a valid Postgres URL — even for
# a test suite that runs entirely on SQLite, and even for `--help` on a CLI.
#
# Import-time side effects that touch the outside world are a design smell for
# exactly this reason: they turn an import into a runtime dependency. Deferring the
# construction to first use keeps the singleton benefit and removes the coupling.
# --------------------------------------------------------------------------
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """The shared engine, built on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings())
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The shared session factory, built on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session with commit-on-success / rollback-on-error.

    Used as `session: AsyncSession = Depends(get_db)`. Tests override this
    dependency to bind a session to an outer transaction that is rolled back
    afterwards, which is what makes the suite isolated without truncating
    tables (see tests/conftest.py).
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            # Re-raise: translating the exception is the job of the handlers in
            # core/exceptions.py. This dependency's single responsibility is the
            # transaction boundary.
            raise
        else:
            await session.commit()
        # `async with` closes the session and returns the connection to the pool
        # on every path, including cancellation.


async def database_healthy() -> bool:
    """Used by /health/ready. Never raises — a health check must always answer."""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("database_health_check_failed", error=str(exc))
        return False


async def dispose_engine() -> None:
    """Close all pooled connections. Called from lifespan shutdown so the
    database does not sit on sockets from a dead process.

    No-op if the engine was never built — which is the case for a process that only
    imported the module (a CLI, a test run with `get_db` overridden).
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.info("database_engine_disposed")
