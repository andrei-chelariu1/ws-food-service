"""Shared pytest fixtures.

THE TWO-TIER TEST STRATEGY
--------------------------
1. **Unit tests** (`test_service.py`) — construct a service directly with fakes.
   No database, no event loop fixture, no HTTP. Milliseconds each. This is only
   possible because services depend on injected abstractions rather than on a global
   session (see app/modules/README.md).

2. **Integration tests** (`test_api.py`) — drive the real ASGI app with `httpx`,
   against a real database, through the real middleware and exception handlers.
   These are the ones that catch a wrong status code, a missing dependency, or an
   authorization gap.

Most rules are cheaper and clearer to test at tier 1. Tier 2 exists to prove the
wiring, not to re-test every branch.

THE ISOLATION MECHANISM (`db_session`)
--------------------------------------
Each test runs inside a transaction that is **rolled back** afterwards. Not
`TRUNCATE`, not "drop and recreate the schema per test":

* rollback is one command, so the suite stays fast;
* nothing leaks between tests, so they can run in any order;
* it works even if a test fails mid-way — there is no cleanup step to skip.

The trick is binding the session to an outer connection-level transaction and
overriding `get_db` to hand that session to the app. The app's own `commit()` then
commits a nested transaction, which the outer rollback still discards.

WHY SQLITE FOR THE DEFAULT SUITE
--------------------------------
Zero setup: `pytest` works on a laptop with no containers running. Stated
limitation, honestly: SQLite does not enforce every CHECK constraint identically,
ignores `SELECT ... FOR UPDATE`, and lacks partial-index and NUMERIC semantics.
So the concurrency behaviour of `reserve()` cannot be proven here. Set
`TEST_DATABASE_URL` to a Postgres URL to run the same suite against the real
engine, and do that in CI — see docs/TESTING.md.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# THIS BLOCK MUST RUN BEFORE ANY `app.*` IMPORT BELOW.
#
# `app/core/rate_limit.py` builds its `Limiter` at *module import* time — it has to,
# because `@limiter.limit(...)` decorates route functions as they are defined, when
# there is no request to inject settings into. That limiter therefore reads
# `get_settings()` (cached) during import.
#
# Consequence for tests: if `RATE_LIMIT_ENABLED` is not already false when the import
# happens, every decorated route would try to reach a real Redis for its counter.
# Setting the environment here — before the imports — is what makes the suite
# runnable with no Redis and no .env file.
#
# It is also the honest cost of the module-level singleton, written down rather than
# discovered. See docs/RATE_LIMITING.md for why it is a singleton at all.
# ---------------------------------------------------------------------------
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-used-anywhere-real-0123456789")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.redis import get_redis
from app.core.security import BcryptPasswordHasher, JwtTokenService
from app.main import create_app
from app.modules.food_items.api import get_food_item_service
from app.modules.food_items.repository import FoodItemRepository
from app.modules.food_items.service import FoodItemService
from app.modules.notifications.repository import NotificationRepository
from app.modules.notifications.service import NotificationService
from app.modules.notifications.tasks import ImmediateTaskDispatcher
from app.modules.users.models import Role, User
from app.modules.users.repository import UserRepository
from app.modules.users.service import NullTokenDenylist, UserService
from app.shared.cache.cache import NullCache
from app.shared.db.base import Base
from app.shared.db.session import get_db
from app.shared.security.principal import CurrentUser
from app.shared.utils.datetime_utils import utcnow

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test configuration.

    `BCRYPT_ROUNDS=4` is the single most valuable line in this file. At the
    production cost of 12, every test that registers or logs in a user pays ~250ms
    per hash; a suite with fifty such tests spends most of its runtime on bcrypt. At
    4 it is ~2ms.

    This is safe *only* because the cost factor is configuration rather than a
    constant — and `assert_production_safe()` refuses to boot with rounds < 12 in
    production. Making a security parameter tunable is what allows a fast test suite
    without weakening the real system.
    """
    return Settings(
        ENVIRONMENT="development",
        DEBUG=True,
        SECRET_KEY="test-secret-key-not-used-anywhere-real-0123456789",
        BCRYPT_ROUNDS=4,
        RATE_LIMIT_ENABLED=False,  # see the note in the `app` fixture
        DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test",
        LOG_JSON=False,
        LOG_LEVEL="WARNING",  # keep test output readable
    )


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
@pytest.fixture
async def db_engine() -> AsyncGenerator[Any, None]:
    """A fresh in-memory engine per test, with the schema created on it.

    FUNCTION-SCOPED, not session-scoped, and deliberately so. A session-scoped async
    fixture needs a session-scoped event loop, which then has to be threaded through
    every other async fixture — and the moment one of them is function-scoped, pytest
    raises `ScopeMismatch`. Creating an in-memory SQLite schema takes single-digit
    milliseconds, so paying it per test buys a much simpler fixture graph.

    KISS: the cheap-and-obviously-correct option beats the fast-and-fragile one until
    the suite is large enough for the difference to show up in wall-clock time.

    `StaticPool` plus a single shared connection is required for SQLite in-memory:
    each new connection would otherwise get its own empty database, so a table
    created in one would not exist in the next.

    The schema is built with `create_all`, NOT by running Alembic. That is a
    deliberate trade — it is fast and it works on SQLite — but it means **the
    migrations themselves are not exercised here**. A migration that disagrees with
    the models would not be caught by this suite. `docs/TESTING.md` describes the CI
    job that runs `alembic upgrade head` against Postgres to close that gap; treat it
    as required, not optional.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool if "sqlite" in TEST_DATABASE_URL else None,
        connect_args={"check_same_thread": False} if "sqlite" in TEST_DATABASE_URL else {},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: Any) -> AsyncGenerator[AsyncSession, None]:
    """A session inside a transaction that is always rolled back.

    The mechanism, step by step:
      1. open a connection and begin a transaction on it;
      2. bind a session to that *connection* (not the engine);
      3. hand the session to the test;
      4. roll the outer transaction back, discarding everything — including anything
         the application committed, because those commits land in a nested
         transaction.

    Net effect: perfect isolation, one rollback, no truncation, and no ordering
    dependencies between tests.
    """
    connection = await db_engine.connect()
    transaction = await connection.begin()

    session_factory = async_sessionmaker(bind=connection, expire_on_commit=False)
    session = session_factory()

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------
# Fakes for external services
# --------------------------------------------------------------------------
class FakeRedis:
    """Minimal in-memory stand-in for the Redis client.

    Implements only what the code under test calls. A full fake would be a
    maintenance burden and would tempt tests to assert on Redis internals rather than
    on behaviour.

    Note that `NullCache` covers the caching path, so this exists mainly to satisfy
    the `get_redis` dependency and for the denylist tests.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = sum(1 for key in keys if self._store.pop(key, None) is not None)
        return removed

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def aclose(self) -> None:
        self._store.clear()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
@pytest.fixture
async def app(settings: Settings, db_session: AsyncSession, fake_redis: FakeRedis) -> Any:
    """The real app, with external dependencies overridden.

    `dependency_overrides` is the mechanism, and note that it is the *same* mechanism
    `create_app()` uses to bind the `UserAuthenticator` — so the production wiring and
    the test wiring use one seam rather than two. Overriding a provider replaces one
    node of the graph and leaves the rest genuinely under test: real routers, real
    middleware, real exception handlers, real services.

    What is overridden, and why:
      * `get_db` — the rollback-scoped session, for isolation.
      * `get_redis` — `FakeRedis`, so no server is needed.
      * `get_food_item_service` — injects `NullCache`. This is the payoff of the
        `Cache` Protocol: the service behaves identically with a cache that stores
        nothing, which is also the proof that caching is an optimisation and not a
        correctness dependency.

    `RATE_LIMIT_ENABLED=False` in the test settings (and in the environment block at
    the top of this file), because a shared limiter would otherwise leak counter state
    between tests and make them order-dependent — and because the module-level limiter
    would need a live Redis.

    The limiter's *decision logic* is unit-tested in
    `tests/shared/test_rate_limit_key.py`; the end-to-end 429 is verified by curl
    against the running stack (step 7 of the README checklist). Stated here rather than
    left as a silent gap.
    """
    application = create_app(settings)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    def override_get_redis() -> Any:
        return fake_redis

    def override_food_item_service() -> FoodItemService:
        return FoodItemService(
            repository=FoodItemRepository(db_session),
            cache=NullCache(),
        )

    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_redis] = override_get_redis
    application.dependency_overrides[get_food_item_service] = override_food_item_service

    return application


@pytest.fixture
async def client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """An HTTP client that talks to the app in-process.

    `ASGITransport` calls the app directly — no socket, no port, no server process.
    Fast, and it means a test failure produces the real traceback rather than a
    connection error.

    IMPORTANT: this does NOT run the lifespan, so `init_redis` never fires. That is
    intentional (the `get_redis` override supplies the fake), but it also means
    startup logic is not covered here. The `/health/ready` integration test exercises
    that path separately.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as async_client:
        yield async_client


# --------------------------------------------------------------------------
# Services, wired for unit tests
# --------------------------------------------------------------------------
@pytest.fixture
def password_hasher(settings: Settings) -> BcryptPasswordHasher:
    return BcryptPasswordHasher(rounds=settings.BCRYPT_ROUNDS)


@pytest.fixture
def token_service(settings: Settings) -> JwtTokenService:
    return JwtTokenService(settings)


@pytest.fixture
def user_service(
    db_session: AsyncSession,
    password_hasher: BcryptPasswordHasher,
    token_service: JwtTokenService,
    settings: Settings,
) -> UserService:
    """`UserService` with a null denylist.

    `NullTokenDenylist` for tests that are not about revocation — it keeps them from
    depending on Redis behaviour they do not care about. Revocation tests construct
    the service themselves with a real `RedisTokenDenylist` over `FakeRedis`.
    """
    return UserService(
        repository=UserRepository(db_session),
        password_hasher=password_hasher,
        token_service=token_service,
        denylist=NullTokenDenylist(),
        access_token_ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@pytest.fixture
def food_item_service(db_session: AsyncSession) -> FoodItemService:
    """`FoodItemService` with `NullCache` — see the `app` fixture."""
    return FoodItemService(repository=FoodItemRepository(db_session), cache=NullCache())


@pytest.fixture
def notification_service(db_session: AsyncSession) -> NotificationService:
    """`NotificationService` with a dispatcher that drops tasks.

    Proof that `TaskDispatcher` is a real abstraction: the service is fully testable
    with an implementation that does nothing, because dispatching is a side effect and
    not part of any business rule.
    """
    return NotificationService(
        repository=NotificationRepository(db_session),
        dispatcher=ImmediateTaskDispatcher(),
    )


# --------------------------------------------------------------------------
# Data builders
# --------------------------------------------------------------------------
@pytest.fixture
def make_user(db_session: AsyncSession, password_hasher: BcryptPasswordHasher) -> Any:
    """Factory fixture: `await make_user(role=Role.ADMIN)`.

    A factory, not a fixed `user` fixture. A fixed fixture forces every test to use
    the same user, so the moment a test needs two (an owner and a stranger — i.e. every
    authorization test) you need `user`, `other_user`, `admin_user`... A factory
    handles all of it, and each test states the data it actually needs.
    """

    async def _make(
        *,
        email: str | None = None,
        password: str = "TestPassw0rd!",  # noqa: S107
        role: Role = Role.DONOR,
        is_active: bool = True,
        full_name: str = "Test User",
    ) -> User:
        user = User(
            # Unique by default so two calls in one test cannot collide on the unique
            # constraint — a failure that looks like a bug in the code under test.
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=password_hasher.hash(password),
            full_name=full_name,
            role=role,
            is_active=is_active,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make


@pytest.fixture
def make_food_item(db_session: AsyncSession) -> Any:
    """Factory for food items.

    `expires_at` defaults to 48 hours ahead, and `expires_in_hours` may be
    **negative** to build an already-expired item. That is how the central business
    rule is tested without any clock manipulation — just data.
    """

    async def _make(
        *,
        owner_id: uuid.UUID,
        name: str = "Test bread",
        quantity: float = 2.5,
        expires_in_hours: float = 48,
        status: Any = None,
    ) -> Any:
        from decimal import Decimal

        from app.modules.food_items.models import FoodItem, FoodItemStatus, FoodUnit

        item = FoodItem(
            name=name,
            description="Test item",
            quantity=Decimal(str(quantity)),
            unit=FoodUnit.KILOGRAM,
            expires_at=utcnow() + timedelta(hours=expires_in_hours),
            status=status or FoodItemStatus.AVAILABLE,
            pickup_location="Test Street 1",
            owner_id=owner_id,
        )
        db_session.add(item)
        await db_session.flush()
        return item

    return _make


@pytest.fixture
def as_current_user() -> Any:
    """Build a `CurrentUser` from a `User` row, without going through HTTP.

    Lets unit tests call `service.update(item_id, payload, as_current_user(user))`
    directly. Services take `CurrentUser`, never `User`, so this is the seam that
    keeps service tests free of tokens and requests.
    """

    def _make(user: User) -> CurrentUser:
        return CurrentUser(
            id=user.id,
            email=user.email,
            role=str(user.role),
            is_active=user.is_active,
        )

    return _make


@pytest.fixture
def auth_headers(token_service: JwtTokenService) -> Any:
    """Build an `Authorization` header for a user, for integration tests.

    Mints a real token with the real token service rather than stubbing
    authentication. The whole chain — signature, `typ`, `jti`, the denylist check, the
    database lookup in `authenticate_access_token` — is therefore exercised on every
    authenticated request in the suite. Faking it here would be faster and would test
    considerably less.
    """

    def _make(user: User) -> dict[str, str]:
        issued = token_service.create_access_token(str(user.id), role=str(user.role))
        return {"Authorization": f"Bearer {issued.token}"}

    return _make
