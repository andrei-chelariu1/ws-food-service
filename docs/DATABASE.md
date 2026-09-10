# Database — Schema, Migrations, Sessions

## The schema

```
users                       food_items                    donations
─────                       ──────────                    ─────────
id            UUID pk       id            UUID pk         id            UUID pk
email         unique        name                          food_item_id  fk RESTRICT
hashed_password             description                   recipient_id  fk RESTRICT
full_name                   quantity      NUMERIC(10,3)   status        PENDING|…
role          CHECK         unit          CHECK           note
is_active                   expires_at    tz-aware        decline_reason
created_at / updated_at     status        CHECK           created_at / updated_at
                            pickup_location
                            owner_id      fk RESTRICT   notifications
                            created_at / updated_at      ─────────────
                            is_deleted / deleted_at      id            UUID pk
                                                         user_id       fk CASCADE
                                                         type          CHECK
                                                         title / body
                                                         related_donation_id  (no FK)
                                                         read_at       nullable
                                                         created_at / updated_at
```

---

## Column-type decisions

| Choice | Instead of | Why |
|---|---|---|
| `UUID` pk | `BIGSERIAL` | no enumeration (IDOR), no growth-rate leak, id available before INSERT, merge-safe. Costs 16 bytes and index locality. |
| `NUMERIC(10,3)` for quantity | `DOUBLE PRECISION` | 0.1 is not representable in binary float; these values are **summed** for impact reporting |
| `TIMESTAMPTZ` everywhere | `TIMESTAMP` | naive timestamps mean "some timezone, ask the developer"; comparing naive with aware raises `TypeError` in production |
| `VARCHAR` + `CHECK` for enums | native `ENUM` | `ALTER TYPE … ADD VALUE` is awkward and effectively irreversible; widening a CHECK is a two-line reversible ALTER |
| `read_at` nullable timestamp | `is_read` boolean | strictly more information at the same cost; a boolean cannot be upgraded without a backfill that has no data |
| `VARCHAR(320)` for email | `VARCHAR(255)` | RFC 5321: 64-char local part + `@` + 255-char domain |

`type_annotation_map` in `shared/db/base.py` encodes the datetime and UUID choices **once**, so
no model can forget `timezone=True`.

### `enum_column()` — and the bug it prevents

`Mapped[Role] = mapped_column(String(16))` works and then lies: SQLAlchemy performs no
conversion, so a freshly constructed object holds a `Role` member while the *same row reloaded
from the database* holds a plain `str`. `user.role is Role.ADMIN` then passes on one path and
silently fails on the other. Caught by a test, not by review.

`enum_column()` uses `Enum(..., native_enum=False, create_constraint=False,
values_callable=...)`. `values_callable` is load-bearing: `FoodUnit.KILOGRAM` has the value
`"KG"`, and SQLAlchemy's default stores the *name* — putting `"KILOGRAM"` into a column whose
CHECK only permits `"KG"`.

---

## Foreign-key behaviour is not uniform, and that is deliberate

| FK | Action | Why |
|---|---|---|
| `food_items.owner_id → users` | **RESTRICT** | deleting a user must never silently erase food they donated; a `Donation` records a real handover |
| `donations.food_item_id → food_items` | **RESTRICT** | same |
| `donations.recipient_id → users` | **RESTRICT** | same |
| `notifications.user_id → users` | **CASCADE** | meaningless without its recipient, references no third party |
| `notifications.related_donation_id` | **no FK** | a historical record must survive the donation being purged; a real FK forces either a cascade (destroys history) or a RESTRICT (blocks cleanup) |

RESTRICT acts as a tripwire: an accidental hard delete fails loudly instead of cascading data
away. In practice users are deactivated, not deleted.

---

## Indexes, and the query each one serves

| Index | Serves |
|---|---|
| `ix_users_email` (unique) | login — the hottest authenticated lookup |
| `ix_users_role_active` (**partial**, `WHERE is_active`) | admin screens, which never list disabled accounts |
| `ix_food_items_owner_id_status` | "my listings, by status". `owner_id` **leftmost**, so it also serves the unfiltered case |
| `ix_food_items_available_expires` (**partial**) | `GET /food-items` — the hottest read in the app |
| `ix_donations_recipient_id_status` | the recipient's screen |
| `ix_donations_food_item_id_status` | the donor's screen — a different leading column, so it needs its own index |
| `ix_notifications_user_unread` (**partial**, `WHERE read_at IS NULL`) | the badge, polled constantly |

### Two ideas worth internalising

**Leftmost-prefix rule.** `(owner_id, status)` also serves a query filtering on `owner_id`
alone. `(status, owner_id)` would not. The column order is a decision, not an accident.

**Partial indexes stay small forever.** `ix_food_items_available_expires` covers only
`status = 'AVAILABLE' AND is_deleted = false` — exactly the rows the public endpoint can return.
DONATED items accumulate indefinitely and never enter the index. Same for the unread
notification index: its size tracks the *backlog*, not all of history. This is the highest-value
index in the schema.

---

## The naming convention

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Seven lines that make migrations work. Without them Postgres invents names and SQLAlchemy leaves
some anonymous, so `autogenerate` cannot emit a `DROP CONSTRAINT` for something whose name it
cannot predict — and names differ between your test SQLite and production Postgres, so a
migration that passed CI fails on deploy.

> **The gotcha.** Alembic applies this convention to **its own** operations. Because the `ck`
> pattern interpolates `%(constraint_name)s`, a migration must pass the **short** name:
>
> ```python
> sa.CheckConstraint("...", name="status_valid")   # -> ck_donations_status_valid   ✓
> sa.CheckConstraint("...", name="ck_donations_status_valid")
> #                                    -> ck_donations_ck_donations_status_valid    ✗
> ```
>
> PK/FK/index patterns do not interpolate it, so those *are* spelled out in full. Found by
> applying `0001` to real Postgres and reading `pg_constraint`.

---

## Migrations

See `migrations/README.md` for the full workflow. The four rules:

1. **Never edit an applied migration.** Alembic records the id, not the contents — databases
   diverge silently.
2. **Always read an autogenerated migration.** The checklist is in `script.py.mako`.
3. **Test the rollback:** `make migrate && make downgrade && make migrate`.
4. **Never use the ORM in a migration.** Migrations pin the schema as it was; models track the
   present.

Migrations run in `scripts/entrypoint.sh` before the port is bound — not in the app lifespan,
where N replicas would race on every deploy.

---

## Sessions and the Unit of Work

One session per request. `get_db()` commits on success, rolls back on any exception.

**That makes requests atomic.** Accepting a donation writes the donation status, the food-item
status, and a notification row. If a business rule fails halfway, you must not be left with a
reserved item and no donation. All three share one transaction and the exception propagates past
`get_db`, so **the rollback is automatic and nobody wrote it.**

Contrast `await session.commit()` inside every service method: each write is its own
transaction, partial failures leave inconsistent state, and correctness depends on forty authors
not forgetting a line. *A rule enforced by structure, not by discipline.*

**Consequence: services never call `commit()`.** They may `flush()` — for a generated id, or to
surface a constraint violation early where a service can still translate it into a domain error.

### Settings that matter

| Setting | Why |
|---|---|
| `expire_on_commit=False` | otherwise every attribute is expired after commit, and the next read lazy-loads *after* the session closed → `MissingGreenlet` while FastAPI serialises the response |
| `autoflush=False` | explicit `flush()` only — no surprise SQL mid-method |
| `pool_pre_ping=True` | prevents "the first request after an idle night fails" |
| `pool_recycle=1800` | stays under typical proxy idle timeouts |
| `lazy="raise"` on every relationship | see below |

### `lazy="raise"` is the most valuable line in the models

The default `lazy="select"` issues a hidden SELECT on first attribute access. In async
SQLAlchemy that raises the famously opaque `MissingGreenlet`; in sync code it silently produces
an N+1.

With `lazy="raise"`, forgetting to eager-load fails **immediately**, with a message naming the
attribute. **You cannot accidentally ship an N+1.** Loading becomes explicit:

```python
stmt = select(Donation).options(selectinload(Donation.food_item))
```

`DonationRepository.list_for_recipient_with_items` documents the arithmetic: 21 queries without
it, 2 with it, constant in page size.

### The engine is built lazily

`create_async_engine` imports the DBAPI eagerly, so a module-level call would make *importing*
anything that reaches `session.py` require `asyncpg` installed and `DATABASE_URL` valid — even
for a suite running on SQLite. Import-time side effects that touch the outside world turn an
import into a runtime dependency. (Found by running the tests: they failed with
`ModuleNotFoundError: asyncpg` before collecting a single test.)

---

## Concurrency: `SELECT ... FOR UPDATE`

Read-then-check-then-write is **not** atomic:

```
T1: SELECT ... status = 'AVAILABLE'   ✓
T2: SELECT ... status = 'AVAILABLE'   ✓   <- both passed the check
T1: UPDATE status = 'RESERVED'
T2: UPDATE status = 'RESERVED'            <- one loaf, two donations
```

Under Postgres's default READ COMMITTED isolation, T2's SELECT genuinely sees `AVAILABLE`, so an
application-level check cannot catch this.

`FoodItemRepository.get_for_update()` adds `.with_for_update()`, so T2 **blocks** until T1
commits, then re-reads `RESERVED` and is correctly rejected with a 409. The lock is released at
commit — which the request-scoped Unit of Work guarantees happens.

Used on exactly one path (`FoodItemService.reserve`), so ordinary reads stay lock-free.

**Alternatives considered:** SERIALIZABLE isolation (correct, but makes *every* transaction
retryable — a far larger change) and an optimistic version column (also correct, needs retry
logic at the API layer). Pessimistic locking on one narrow path is the KISS choice.

> **SQLite ignores row locks.** The default test suite proves the sequential rejection, not the
> concurrent one. Run against Postgres for the real behaviour.

---

## Bulk operations, and their honest cost

`mark_expired_batch` and `mark_all_read` issue a single `UPDATE ... WHERE` instead of a
load-modify-save loop: one statement instead of a thousand, locks held for milliseconds.

The trade-off: **bulk updates bypass the ORM**, so the Python-side `onupdate` on `updated_at`
does not fire (both set it explicitly) and the state-machine check in the service is skipped.

That is acceptable *only* because expiry and mark-all-read are **not transitions a user
requests** and their target states are terminal or idempotent. **Any transition a user can
trigger goes through the service.** Worth understanding before copying the pattern.

---

## Soft delete

Only `FoodItem` uses it, because a `Donation` references it and hard deletion would destroy the
record of a completed handover.

`User` does not: a soft-deleted account holding a unique email would block that address forever.
Deactivation covers the real case; genuine erasure goes through `hard_delete_by_id` (named to be
greppable during a data-retention audit).

`Notification` does not: a dismissed transient message should genuinely disappear.

**The tax:** every read must exclude deleted rows, and one forgotten filter leaks them to users.
`BaseRepository._base_select()` pays it once —
`test_base_repository.py::test_soft_deleted_rows_are_excluded_from_every_read` verifies `get`,
`get_by`, `list`, `count` *and* a module-specific query, none of which mentions `is_deleted`.

---

## Timestamps use two different clocks

`created_at` — **database** clock (`server_default=func.now()`): one source of truth, no replica
drift, works for rows written by a migration.

`updated_at` — **Python-side** `onupdate`. A server-side `onupdate` leaves SQLAlchemy not
knowing the new value, so it marks the attribute expired; the next read lazy-loads and raises
`MissingGreenlet` — typically mid-serialisation. Avoiding that would need
`await session.refresh(entity)` after **every** update, forever, to populate one timestamp.

Cost: a sub-second discrepancy no consumer cares about. Discovered by a test that reads an
attribute after an UPDATE.

---

## Operational notes

**Connection pooling.** `DB_POOL_SIZE=10` + `DB_MAX_OVERFLOW=5` per process. With 4 workers × 3
replicas that is up to 180 connections — Postgres's default `max_connections` is 100. **Size the
pool against replica count, or put PgBouncer in front.**

**Slow queries.** `DB_ECHO=true` logs every statement (dev only). For real analysis use
`pg_stat_statements` and `EXPLAIN (ANALYZE, BUFFERS)`.

**Backups.** Not configured here. `pg_dump` on a schedule plus WAL archiving for
point-in-time recovery is the minimum for real data.
