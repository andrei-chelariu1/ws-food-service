# `app/shared/cache/` — Caching Abstraction

## Purpose

A `Cache` Protocol, a Redis implementation, and a no-op. Five methods total.

```python
class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def clear(self) -> None: ...
```

---

## The clearest Dependency Inversion example in the project

`FoodItemService` declares `cache: Cache`. It never imports `redis`. Therefore:

**Tests need no Redis.** `tests/conftest.py` injects `NullCache`, and the service's behaviour
is unchanged.

That is not a testing trick — it is the *correctness property* that makes caching safe. A
cache is by definition allowed to forget everything. **That `NullCache` is a fully correct
implementation is the proof the abstraction is honest:** if a no-op broke behaviour, the code
would be relying on the cache for correctness, which is a bug.

**Swapping the backend is one class.** In-process LRU for a single node, Memcached, a
two-tier local+Redis cache — none of it touches a service.

**The Protocol is the documentation.** A reader knows the entire surface the domain relies on
without reading the Redis client's API. Note what is *absent*: no `pipeline`, no `incr`, no
Redis-specific anything. Narrow on purpose — every method added here is one every future
implementation must provide.

---

## Fail-open is the central design rule

Every method swallows Redis exceptions and logs them. If Redis is down, `get` returns `None`,
the caller falls through to the database, and the user gets a correct (slightly slower)
response.

The alternative — propagating the error — makes a cache outage a **total outage**. A cache
exists to make things faster; letting it make things *broken* inverts its purpose.

### Note the asymmetry with the token denylist

| Component | On Redis failure | Because |
|---|---|---|
| `RedisCache` | fails **open** | a miss costs a query — degraded performance |
| `RedisTokenDenylist` | fails **closed** | a missed revocation means a cancelled token still works — a security failure |

**The rule: availability controls fail open, security controls fail closed.** The rate limiter
follows the cache (`swallow_errors=True`), for reasons documented in `docs/RATE_LIMITING.md` —
and that decision was forced by observing `/health/live` return 500 during a Redis outage.

A failed `delete` is more dangerous than a failed `get`, because it leaves stale data visible.
That is why TTLs are short (60s): **the TTL is the backstop for exactly that case.**

---

## Implementation details

### JSON, never pickle

`pickle.loads` on data from a shared datastore is remote code execution if anything can write
to that Redis. JSON cannot execute.

The cost is that only JSON-serialisable values are cacheable — which is fine, because we
cache **DTOs** (`model_dump(mode="json")` output), never ORM entities. Caching a live ORM
object would be wrong regardless: it is bound to a session that is about to close.

### Every key gets a TTL

Never an unbounded key. Two reasons: unbounded keys grow until Redis evicts under memory
pressure (unpredictably), and the TTL bounds how long a missed invalidation can serve stale
data.

### Every key is namespaced `cache:`

So `delete_prefix` can never touch rate-limit counters or denylist entries in the same Redis
instance. `clear()` is emphatically **not** `FLUSHDB` — that would also wipe rate-limit
counters (handing an attacker a clean slate) and the JWT denylist (un-revoking every
logged-out token). **Prefix scoping is a security property here, not tidiness.**

### `delete_prefix` uses `SCAN`, not `KEYS`

`KEYS pattern` is O(N) over the whole keyspace and **blocks the Redis event loop** for the
duration — on a large instance that is a production incident. `SCAN` is cursor-based and
yields between batches (`count=500`, because the default of 10 means many round-trips).

**Honest limitation:** this is still O(keys) work. At our scale (a few hundred cached list
pages) it is well within budget. If the cached keyspace grows large, replace prefix scanning
with a **version counter**: keep `food_items:version` in Redis, embed it in every cache key,
and `INCR` it on write. Invalidation becomes one O(1) command and old keys age out via TTL.
Deliberately not implemented yet — see `docs/CACHING.md`.

### A corrupt entry is evicted, not fatal

If `json.loads` fails (a stale format left over from a previous deploy), the entry is treated
as a miss and deleted. The alternative is 500ing on that key forever.

### `build_cache_key` sorts its parts

```python
build_cache_key("food_items:available", page=1, size=20, search=None)
# -> "food_items:available:page=1&size=20"
```

`sorted(parts)` matters: without it `?page=1&size=20` and `?size=20&page=1` produce two keys
for one identical query, halving the hit rate for no reason and with no error to notice.
`None` values are dropped so an absent filter and an explicit `None` share a key. Tested in
`tests/shared/test_pagination.py`.
