"""Redis connection pool, owned by the application lifespan.

WHY A SINGLE MODULE-LEVEL POOL
------------------------------
Opening a Redis connection per request would spend more time on TCP handshakes
than on the actual `GET`. `redis.asyncio.Redis.from_url` creates a *pool*; the
client is safe to share across tasks and multiplexes over pooled connections.

So: one pool, created in `lifespan` at startup, closed at shutdown. Consumers
receive the client through `get_redis()` as a dependency instead of importing
the global — which keeps them testable (override the dependency with a fake)
and keeps the lifetime in one place (Single Responsibility).

KEY NAMESPACES
--------------
One Redis instance serves three purposes, separated by key prefix rather than
by numbered DB — prefixes work on Redis Cluster, numbered DBs do not:

    LIMITS:LIMITER/rl:*   rate-limit counters   (app/core/rate_limit.py)
    cache:*               cache-aside cache     (app/shared/cache/cache.py)
    denylist:*            revoked JWT ids       (app/modules/users/service.py)

Note the rate-limit prefix: `KEY_PREFIX_RATE_LIMIT` below is `rl:`, but the
`limits` library that slowapi builds on prepends its own `LIMITS:LIMITER/`
namespace, so that is the shape you actually see in `redis-cli --scan`. Written
as observed rather than as intended — the distinction matters if you ever need
to inspect or clear counters by hand.

The separation is a security property, not tidiness: `RedisCache.clear()`
deletes only `cache:*`, so clearing the cache can never wipe rate-limit counters
(handing an attacker a clean slate) or the denylist (un-revoking every
logged-out token).
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

KEY_PREFIX_RATE_LIMIT = "rl:"
KEY_PREFIX_CACHE = "cache:"
KEY_PREFIX_DENYLIST = "denylist:"

# Module-level singleton. Only lifespan writes it; everything else reads it via
# get_redis(). Not a public import target — see the module docstring.
_redis: Redis | None = None


def create_redis(settings: Settings) -> Redis:
    """Build a Redis client with production-sane defaults."""
    return Redis.from_url(
        settings.redis_url_str,
        # We store and read text (JSON, counters), never binary blobs, so
        # decoding here saves a .decode() at every call site.
        decode_responses=True,
        # Without timeouts, a hung Redis turns into hung HTTP requests. Redis
        # is a *cache* and a *counter store* — never worth blocking a request.
        socket_connect_timeout=2,
        socket_timeout=2,
        # Detect connections dropped by the network or by Redis itself.
        health_check_interval=30,
        retry_on_timeout=True,
        max_connections=50,
    )


async def init_redis(settings: Settings) -> Redis:
    """Create the pool and verify connectivity. Called from lifespan."""
    global _redis
    _redis = create_redis(settings)
    await _redis.ping()
    log.info("redis_connected", url=_redis_safe_url(settings.redis_url_str))
    return _redis


async def close_redis() -> None:
    """Release the pool. Called from lifespan shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        log.info("redis_disconnected")


def get_redis() -> Redis:
    """FastAPI dependency returning the shared client.

    Raises if the pool was never initialised, which can only happen if a
    request is served outside the lifespan — a wiring bug worth surfacing
    loudly rather than degrading silently.
    """
    if _redis is None:
        raise RuntimeError(
            "Redis pool is not initialised. init_redis() runs in the app "
            "lifespan; if you see this in a test, override the get_redis "
            "dependency with a fake."
        )
    return _redis


async def redis_healthy() -> bool:
    """Used by /health/ready. Never raises — a health check must always answer."""
    try:
        return bool(await get_redis().ping())
    except Exception as exc:
        log.warning("redis_health_check_failed", error=str(exc))
        return False


def _redis_safe_url(url: str) -> str:
    """Strip credentials before logging a connection string."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"
