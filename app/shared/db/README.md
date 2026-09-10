# `app/shared/db/` — Database Infrastructure

## Purpose

The declarative base, the naming convention, the async engine and session lifecycle, and
reusable column groups. No domain knowledge — no `User`, no `FoodItem`.

| File | Provides |
|---|---|
| `base.py` | `Base`, `NAMING_CONVENTION`, `type_annotation_map`, `enum_column()` |
| `session.py` | engine, session factory, `get_db()` (the Unit of Work), health check |
| `mixins.py` | `UUIDMixin`, `TimestampMixin`, `SoftDeleteMixin` |

---

## `base.py` — the naming convention is the point

Seven lines, and the highest-leverage SQLAlchemy configuration there is.

Without it, PostgreSQL invents constraint names (`food_items_owner_id_fkey`) and SQLAlchemy
leaves some anonymous. Then:

```python
op.drop_constraint("???", "food_items")  # what is it called?
```

`alembic revision --autogenerate` cannot emit a `DROP CONSTRAINT` for something whose name
it cannot predict — and names differ between your SQLite test database and production
Postgres, so a migration that passed CI fails on deploy.

Declaring the convention once makes every name deterministic and identical everywhere:

```
ix_food_items_expires_at              ck_food_items_quantity_positive
uq_users_email                        fk_donations_food_item_id_food_items
pk_users
```

> **Gotcha that bit us.** The `ck` pattern is `ck_%(table_name)s_%(constraint_name)s`, and
> Alembic applies the convention to *its own* operations too. So a migration must pass the
> **short** name (`name="status_valid"`), not the full one — otherwise you get
> `ck_donations_ck_donations_status_valid`. PK/FK/index patterns do not reference
> `%(constraint_name)s`, so those *are* spelled out in full. Documented in
> `migrations/versions/0001_initial_schema.py`; found by reading `pg_constraint` on a real
> database, not by reasoning about it.

### `type_annotation_map`

Maps Python types to column types once, project-wide:

```
datetime   -> DateTime(timezone=True)   # always aware; naive timestamps are a bug factory
uuid.UUID  -> PgUUID(as_uuid=True)      # 16 bytes, not a 36-char string
str        -> String(255)               # a bare Mapped[str] would be unbounded TEXT
```

Without it, every model repeats `DateTime(timezone=True)` and the day someone forgets
`timezone=True` you have a naive timestamp in an aware table.

### `enum_column()`

The canonical way to map a `StrEnum`. **It exists because of a real bug.** The tempting
shortcut:

```python
role: Mapped[Role] = mapped_column(String(16))  # WRONG — the annotation lies
```

SQLAlchemy performs no conversion, so a freshly constructed object holds a `Role` member
while the *same row reloaded from the database* holds a plain `str`. `user.role is
Role.ADMIN` then passes on one code path and silently fails on the other. Caught by
`tests/modules/donations/test_service.py::test_notification_is_created_for_the_donor`.

Three keyword choices, each deliberate:

| Keyword | Why |
|---|---|
| `native_enum=False` | VARCHAR + CHECK instead of a native ENUM: `ALTER TYPE ... ADD VALUE` is awkward and effectively irreversible; widening a CHECK is a two-line reversible ALTER |
| `create_constraint=False` | models declare their own named CHECKs; two constraints for one rule under different names is exactly the drift we avoid |
| `values_callable=...` | store the **value**, not the name. Load-bearing: `FoodUnit.KILOGRAM` is `"KG"`, and the default would store `"KILOGRAM"` — violating the CHECK immediately |

---

## `session.py` — the Unit of Work

```python
async def get_db():
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
```

One session per request; commit on success, rollback on error. That single rule makes
requests **atomic**.

Accepting a donation writes the donation status, the food-item status, and a notification
row. If the business-rule check fails halfway, you must not be left with a reserved item
and no donation. All three share one transaction and the exception propagates past this
dependency, so **the rollback is automatic and nobody wrote it.**

Compare the alternative — `await session.commit()` sprinkled through every service method.
Each write becomes its own transaction, partial failures leave inconsistent state, and
correctness depends on forty authors not forgetting a line. That is the difference between a
rule enforced by *structure* and a rule enforced by *discipline*.

**Consequence: services never call `commit()`.** They may `flush()` — for a generated id, or
to surface a constraint violation early where a service can still translate it.

### `expire_on_commit=False`

By default SQLAlchemy expires every attribute after commit, so the next attribute read
issues a fresh SELECT. In async code that lazy load happens *after* the session closed and
raises `MissingGreenlet`. Since `get_db` commits at the very end of the request, FastAPI
then serialises the returned ORM object — and would blow up.

### The engine is built lazily

`create_async_engine` imports the DBAPI driver eagerly. With a module-level call, merely
*importing* anything that reaches this file would require `asyncpg` installed and
`DATABASE_URL` valid — even for a test suite running entirely on SQLite. Import-time side
effects that touch the outside world turn an import into a runtime dependency. (Found by
running the suite: it failed with `ModuleNotFoundError: asyncpg` before collecting a single
test.)

---

## `mixins.py` — composition, not inheritance-for-reuse

`class User(Base, UUIDMixin, TimestampMixin)` reads as a *declaration of properties*. Pick
only what an entity needs.

### `UUIDMixin` — why UUID over auto-increment

* **No enumeration.** `GET /users/1`, `/2`, `/3` walks your user table. A UUID does not —
  an IDOR mitigation you get for free.
* **No information leak.** Sequential ids tell competitors your growth rate.
* **Client-side generation.** The id exists before the INSERT.
* **Merge-friendly.** Two databases never collide.

Cost: 16 bytes vs 8, and worse index locality. Obviously worth it here; for a
high-throughput append-only event table, prefer UUIDv7 or a bigint.

### `TimestampMixin` — the two columns use different clocks

`created_at` uses the **database** clock (`server_default=func.now()`): one source of truth,
no replica drift, and it works for rows written by a migration or by hand. On INSERT,
SQLAlchemy fetches server defaults eagerly, so the value is available with no extra query.

`updated_at` uses a **Python-side** `onupdate`. The reason is specific to async SQLAlchemy:
a server-side `onupdate` leaves SQLAlchemy not knowing the new value, so it marks the
attribute *expired*; the next read triggers a lazy SELECT and raises `MissingGreenlet` —
typically while Pydantic serialises the response. Avoiding that would need
`await session.refresh(entity)` after **every** update, forever, to populate one timestamp.

The cost is a sub-second discrepancy between the two clocks that no consumer cares about. A
real trade, and one only discovered by a test that reads an attribute after an update
(`tests/modules/users/test_api.py::test_admin_can_change_a_role`).

> `onupdate` is ORM-level, so a bulk `update()` skips it. `mark_all_read` and
> `mark_expired_batch` therefore set `updated_at` explicitly.

### `SoftDeleteMixin` — and why not everywhere

Used for `FoodItem`, because a `Donation` references it and hard-deleting would destroy the
record of a completed handover.

**Not** used for `User` (a soft-deleted account holding a unique email would block that
address forever; deactivation covers the real case) or `Notification` (a dismissed message
should genuinely disappear).

Soft delete is a tax: *every* read must remember `WHERE is_deleted = false`, and one
forgotten filter leaks deleted rows to users. `BaseRepository._base_select()` pays that tax
once, which is what keeps it from being paid by hand at forty call sites.

`deleted_at` alongside the boolean is not redundant: the flag indexes and filters cheaply,
the timestamp answers "when, and in which release?" during an incident.
