"""Cache abstraction: a Protocol, a Redis implementation, and a no-op.

THIS FILE IS THE CLEAREST DEPENDENCY-INVERSION EXAMPLE IN THE PROJECT
--------------------------------------------------------------------
`FoodItemService` declares `cache: Cache`. It never imports `redis`. Therefore:

* **Tests need no Redis.** `tests/conftest.py` injects `NullCache`, and the
  service's behaviour is unchanged — because a cache is by definition allowed to
  forget everything. That is not a testing trick; it is the correctness property
  that makes caching safe.
* **Swapping the backend is one class.** In-process LRU for a single-node
  deployment, Memcached, a two-tier local+Redis cache — none of it touches a
  service.
* **The Protocol is the documentation.** Five methods. A reader knows the entire
  surface the domain relies on without reading the Redis client's API.

FAIL-OPEN IS THE CENTRAL DESIGN RULE
------------------------------------
Every method here swallows Redis exceptions and logs them. If Redis is down,
`get` returns `None`, the caller falls through to the database, and the user gets
a correct (slightly slower) response.

The alternative — propagating the error — means a cache outage becomes a **total
outage**. A cache exists to make things faster; letting it make things *broken*
inverts its purpose. The one thing we must never do is fail *closed* on a read.

Note the asymmetry: a failed `delete` is more dangerous than a failed `get`,
because it leaves stale data visible until the TTL expires. That is why TTLs are
short (60s default) — the TTL is the backstop for exactly this case.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.core.redis import KEY_PREFIX_CACHE

log = get_logger(__name__)

DEFAULT_TTL_SECONDS = 60


@runtime_checkable
class Cache(Protocol):
    """What the domain is allowed to assume about caching.

    Note what is absent: no `pipeline`, no `incr`, no Redis-specific anything.
    The Protocol is narrow on purpose — every method added here is a method every
    future implementation must provide.
    """

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def clear(self) -> None: ...


class RedisCache:
    """JSON-serialising cache over Redis.

    WHY JSON AND NOT PICKLE:
    `pickle.loads` on data from a shared datastore is remote code execution if
    anything can write to that Redis. JSON cannot execute. The cost is that only
    JSON-serialisable values are cacheable — which is fine, because we cache
    *DTOs* (`model_dump()` output), never ORM entities. Caching a live ORM object
    would be wrong regardless: it is bound to a closed session.
    """

    def __init__(self, redis: Redis, *, default_ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    def _key(self, key: str) -> str:
        """Namespace every key, so `delete_prefix` can never touch rate-limit
        counters or denylist entries in the same Redis instance."""
        return f"{KEY_PREFIX_CACHE}{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(self._key(key))
        except RedisError as exc:
            log.warning("cache_get_failed", key=key, error=str(exc))
            return None  # fail open -> caller reads the database

        if raw is None:
            log.debug("cache_miss", key=key)
            return None

        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            # Corrupt or stale-format entry (e.g. left over from a previous
            # deploy). Treat as a miss and evict, rather than 500ing forever.
            log.warning("cache_decode_failed", key=key)
            await self.delete(key)
            return None

        log.debug("cache_hit", key=key)
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store with a TTL.

        A TTL is always set — never an unbounded key. Two reasons: unbounded keys
        grow until Redis evicts under memory pressure (unpredictably), and the TTL
        is the safety net that bounds how long a missed invalidation can serve
        stale data.
        """
        try:
            payload = json.dumps(value, default=str)  # `default=str` handles UUID/datetime
        except (TypeError, ValueError) as exc:
            log.warning("cache_encode_failed", key=key, error=str(exc))
            return

        try:
            await self._redis.set(self._key(key), payload, ex=ttl_seconds or self._default_ttl)
        except RedisError as exc:
            log.warning("cache_set_failed", key=key, error=str(exc))

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*(self._key(k) for k in keys))
        except RedisError as exc:
            log.warning("cache_delete_failed", keys=keys, error=str(exc))

    async def delete_prefix(self, prefix: str) -> int:
        """Invalidate every key under a prefix. Returns how many were removed.

        WHY `scan_iter` AND NOT `KEYS`:
        `KEYS pattern` is O(N) over the whole keyspace and blocks the Redis event
        loop for the duration — on a large instance that is a production incident.
        `SCAN` is cursor-based and yields between batches.

        HONEST LIMITATION: this is still O(keys) work. At our scale (a few hundred
        cached list pages) it is well within budget. If the cached keyspace grows
        large, replace prefix scanning with a **version counter**: keep
        `food_items:version` in Redis, embed it in every cache key, and `INCR` it
        on write. Invalidation then becomes one O(1) command and the old keys age
        out via TTL. Deliberately not implemented yet — see docs/CACHING.md.
        """
        namespaced = self._key(prefix)
        deleted = 0
        try:
            # `count=500` batches the scan; the default of 10 means many
            # round-trips.
            async for key in self._redis.scan_iter(match=f"{namespaced}*", count=500):
                await self._redis.delete(key)
                deleted += 1
        except RedisError as exc:
            log.warning("cache_delete_prefix_failed", prefix=prefix, error=str(exc))
            return deleted

        if deleted:
            log.debug("cache_invalidated", prefix=prefix, keys_deleted=deleted)
        return deleted

    async def clear(self) -> None:
        """Drop every cache key — never the whole Redis database.

        Emphatically not `FLUSHDB`: that would also wipe rate-limit counters
        (handing an attacker a clean slate) and the JWT denylist (un-revoking
        every logged-out token). Prefix scoping is a security property here, not
        tidiness.
        """
        await self.delete_prefix("")


class NullCache:
    """A cache that stores nothing. Satisfies `Cache` completely.

    Not a stub or a placeholder — a legitimate implementation of the Null Object
    pattern, and the reason services need no `if cache_enabled:` branches
    anywhere. Used in tests, and available as a kill switch if caching ever needs
    to be disabled in an incident without a code change.

    That this class is *correct* is the proof that the `Cache` abstraction is
    honest: if a no-op implementation broke behaviour, the code would be relying
    on the cache for correctness rather than for speed — which is a bug.
    """

    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def delete_prefix(self, prefix: str) -> int:
        return 0

    async def clear(self) -> None:
        return None


def build_cache_key(namespace: str, **parts: Any) -> str:
    """Deterministic cache keys: `food_items:list:page=1&size=20&status=AVAILABLE`.

    `sorted(parts)` matters — without it `?page=1&size=20` and `?size=20&page=1`
    would produce two different keys for one identical query, halving the hit
    rate for no reason.

    `None` values are dropped so an absent filter and an explicit `filter=None`
    share a key.
    """
    suffix = "&".join(f"{k}={v}" for k, v in sorted(parts.items()) if v is not None)
    return f"{namespace}:{suffix}" if suffix else namespace
