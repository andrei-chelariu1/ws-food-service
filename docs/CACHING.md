# Caching

## What is cached

**One endpoint: `GET /api/v1/food-items`.** Nothing else.

```
GET /food-items?page=1&size=20
      │
      ├─ cache HIT  ──► return the stored page          (~1ms)
      └─ cache MISS ──► SELECT rows + COUNT(*)          (~15ms)
                        └─► store, TTL 60s

any write to a food item ──► delete_prefix("food_items:available")
```

Key: `cache:food_items:available:page=1&size=20&search=bread`

---

## Why that endpoint and no other

| Property | Why it matters |
|---|---|
| **Unauthenticated** | the response is identical for every caller, so one cached page is correct for everyone |
| **Highest traffic** | the landing view of the application |
| **Two queries** | rows *plus* `COUNT(*)` — twice the saving |
| **Tolerates staleness** | a 60-second-old list of available food is still useful |

Contrast `/food-items/mine`: per-user, so caching multiplies keys by users for a fraction of the
traffic. Not cached.

Contrast `/notifications`: per-user *and* changes constantly. Caching it would be all
invalidation and no hits.

### The key deliberately excludes the user

Correct **because** the response has no per-user data. If a personalised field is ever added,
**the key must include the user id** — otherwise user A is served user B's view. That is the
classic cache-poisoning-by-omission bug, and the reason this endpoint stays anonymous-only. If
you ever add `CurrentUserDep` to it, revisit the key in the same commit.

---

## Cache-aside, and why not the alternatives

| Pattern | Rejected because |
|---|---|
| **Cache-aside** (used) | the cache is never authoritative; a miss or an outage just falls through to the database |
| Write-through | every write pays cache latency, and the cache becomes part of the write path |
| Write-behind | the cache becomes the source of truth — data loss on eviction |
| Read-through | needs the cache to know how to load, coupling it to the domain |

Cache-aside is the only one where **the cache can fail completely and the system stays
correct**.

---

## Invalidation

Every mutating method in `FoodItemService` calls `_invalidate_list_cache()`:
`create`, `update`, `reserve`, `release`, `mark_donated`, `cancel`, `soft_delete`,
`expire_stale_items`.

**It lives in the service, not the router.** In the router it would have to be remembered on
every write endpoint, and the first forgotten call serves stale data for a TTL. In the service it
sits next to the write it corresponds to.

**Coarse on purpose.** `delete_prefix` drops *every* cached page. Working out which page a
changed item appeared on is both hard and fragile; dropping a few dozen short-lived keys costs
almost nothing. Correct and simple beats clever and subtly wrong.

**Prefix-scoped, which is a security property.** `cache:*` only — so invalidating the cache can
never touch `LIMITS:LIMITER/rl:*` (handing an attacker a clean slate) or `denylist:*`
(un-revoking every logged-out token). `clear()` is emphatically **not** `FLUSHDB`.

---

## The staleness window is bounded and accepted

Writes invalidate, so the usual case is fresh. Two ways staleness can appear:

1. **A lost invalidation** — `delete_prefix` fails open on a Redis error. The 60-second TTL caps
   the damage. **The TTL is the backstop for exactly this case**, which is why it is short.
2. **A concurrent write** between a read and its cache write. Same bound.

Worst case: a browser shows a listing that was claimed a moment ago. The user clicks it, and
`FoodItemService.reserve()` rejects the request with a correct `409 FOOD_ITEM_NOT_AVAILABLE`.

**The cache can make the UI slightly stale. It cannot make the system incorrect.** That property
is what makes caching safe here, and it comes from the invariant living in `reserve()` rather
than in the read path.

---

## Fail-open

Every `RedisCache` method swallows Redis errors and logs them. `get` returns `None`, the caller
queries the database, the user gets a correct (slightly slower) response.

Propagating instead would make a cache outage a **total outage**. A cache exists to make things
faster; letting it make things *broken* inverts its purpose.

### Contrast with the token denylist

| Component | On Redis failure | Because |
|---|---|---|
| `RedisCache` | fails **open** | a miss costs a query — degraded performance |
| `RedisTokenDenylist` | fails **closed** | a missed revocation means a cancelled token still works — a security failure |
| Rate limiter | fails **open** | otherwise `/health/live` 500s and Kubernetes restarts every pod |

**The rule: availability controls fail open, security controls fail closed.**

Note the asymmetry *within* the cache: a failed `delete` is more dangerous than a failed `get`,
because it leaves stale data visible until the TTL expires.

---

## Implementation details

### JSON, never pickle

`pickle.loads` on data from a shared datastore is remote code execution if anything can write to
that Redis. JSON cannot execute.

Only JSON-serialisable values are cacheable — which is fine, because we cache **DTOs**
(`model_dump(mode="json")`), never ORM entities. Caching a live ORM object would be wrong
regardless: it is bound to a session about to close.

The router serialises *before* writing through, so what lands in Redis is exactly what the client
received — no risk of the cached shape drifting from the response shape.

### Always a TTL

Never an unbounded key. Unbounded keys grow until Redis evicts under memory pressure
(unpredictably), and the TTL bounds how long a missed invalidation can serve stale data.

### `SCAN`, never `KEYS`

`KEYS pattern` is O(N) over the whole keyspace and **blocks the Redis event loop** — on a large
instance, a production incident. `scan_iter(count=500)` is cursor-based and yields between
batches.

### Order-independent keys

```python
build_cache_key("food_items:available", page=1, size=20, search=None)
```

`sorted(parts)` matters: without it `?page=1&size=20` and `?size=20&page=1` produce two keys for
one identical query, **halving the hit rate for no reason and with no error to notice**. `None`
values are dropped so an absent filter and an explicit `None` share a key. Tested in
`tests/shared/test_pagination.py`.

### Corrupt entries are evicted, not fatal

A stale format left over from a previous deploy fails `json.loads`; the entry is treated as a
miss and deleted, rather than 500ing on that key forever.

---

## Known limitation: prefix scanning does not scale

`delete_prefix` is O(cached keys). At our scale (a few hundred pages) that is well within
budget. It does not scale to a large cached keyspace.

**The fix, when needed — a version counter:**

```python
version = await redis.get("food_items:version")            # or INCR on write
key = f"food_items:available:v{version}:page={page}&size={size}"
```

Invalidation becomes one O(1) `INCR`; old keys are orphaned and age out via TTL.

**Deliberately not implemented.** It adds a read to every cache lookup and a second key to
reason about, for a problem this application does not have. Building it before it is needed is
the opposite of KISS — but the migration path is written down so nobody has to rediscover it.

---

## Observability

```bash
docker compose exec redis redis-cli --scan --pattern 'cache:*'
docker compose exec redis redis-cli INFO stats | grep keyspace
docker compose logs api | grep -E "cache_hit|cache_miss|cache_invalidated"
```

Hits and misses log at **DEBUG**, so set `LOG_LEVEL=DEBUG` to see them. If you promote them to
INFO, sample them — one line per request is a lot of log volume for a ratio you should be
tracking as a metric anyway.

### Proving the cache actually serves reads

Asserting a hit is surprisingly easy to fake. The conclusive test:

```bash
curl -s localhost:8000/api/v1/food-items > /dev/null          # populate

# modify a row directly, bypassing the app so no invalidation fires
docker compose exec -T postgres psql -U app -d foodwaste \
  -c "update food_items set is_deleted=true where name='X'"

curl -s localhost:8000/api/v1/food-items | jq '.items[].name'  # still lists X -> cache served it

docker compose exec redis redis-cli --scan --pattern 'cache:*' \
  | xargs -r docker compose exec -T redis redis-cli DEL

curl -s localhost:8000/api/v1/food-items | jq '.items[].name'  # X gone -> the DB is the truth
```

---

## What is deliberately not cached

| Not cached | Why |
|---|---|
| Individual food items (`/food-items/{id}`) | already a primary-key lookup — the database is as fast as Redis |
| `/food-items/mine` | per-user, low traffic |
| `/notifications` | per-user and changes constantly |
| User lookups in `authenticate_access_token` | **on purpose** — the fresh read is what makes deactivation and role changes take effect immediately. Caching it would reintroduce the staleness that querying was chosen to avoid. |
| Anything authenticated | the key would need the user id, multiplying keys for little benefit |

That last row is the important one: it is a place where caching would be *easy* and would
quietly weaken a security property.
