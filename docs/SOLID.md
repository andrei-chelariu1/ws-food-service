# SOLID, DRY and KISS — Where They Live in This Repo

Principles are cheap to recite and hard to point at. Every claim below names a real file and
what would break without it.

---

## S — Single Responsibility

*A class or module should have one reason to change.*

| Component | Its one responsibility | What it explicitly does not do |
|---|---|---|
| `core/config.py` | where configuration comes from | anything about the domain |
| `core/security.py` | hash a string, sign a dict | know what a `User` is, or who may do what |
| `shared/db/session.py` `get_db()` | the transaction boundary | translate exceptions |
| `shared/repository/base_repository.py` | run queries | decide whether an operation is allowed |
| `modules/*/service.py` | business rules | HTTP, SQL, committing |
| `modules/*/api.py` | HTTP concerns | business rules, SQL |

**The clearest test.** `core/security.py` can hash a password and sign a token, but it cannot
tell you whether a login should succeed. That decision needs a database lookup, an
active-account check, and an anti-enumeration strategy — all in `modules/users/service.py`.
Merging them would mean the hashing code needed a database, so testing "does bcrypt work"
would need one too.

**`dependencies.py` vs `permissions.py`** — split for this reason. Swapping JWT for sessions
touches only the first; adding a `MODERATOR` role touches only the second. Two files, two
reasons to change.

---

## O — Open/Closed

*Open for extension, closed for modification.*

### The exception hierarchy

Adding an error type is one small class:

```python
class FoodItemExpiredError(BusinessRuleViolation):
    code = "FOOD_ITEM_EXPIRED"
```

The handlers in `core/exceptions.py` **never change**, because they dispatch on the `AppError`
base. Ten modules could add fifty error types and the translation layer stays as it is.

### The state machines as data

`modules/food_items/models.py`:

```python
ALLOWED_TRANSITIONS = {
    FoodItemStatus.AVAILABLE: frozenset({RESERVED, CANCELLED, EXPIRED}),
    FoodItemStatus.RESERVED:  frozenset({DONATED, AVAILABLE, EXPIRED}),
    FoodItemStatus.DONATED:   frozenset(),
    ...
}
```

`FoodItemService._transition` is four lines and never changes. Adding a state is one dict
entry — not an audit of every `if status == ...` in the module.

Note the rule expressed by an **omission**: `RESERVED → CANCELLED` is absent, so an owner
cannot withdraw food a recipient is counting on. Encoded as data, not as a conditional.

### `Cache` and `TaskDispatcher`

New backend = new class implementing a Protocol. Nothing that *uses* them changes.

---

## L — Liskov Substitution

*A subtype must be usable wherever its base type is expected.*

### `NullCache` is the proof

`NullCache` satisfies `Cache` completely, and `FoodItemService` behaves **identically** with
it. That is not laziness — it is the Null Object pattern, and it demonstrates something
important: caching here is an *optimisation*, not a correctness dependency.

**If a no-op implementation broke behaviour, the code would be relying on the cache for
correctness — which is a bug.** So `NullCache` is a substitutability test that doubles as a
design check.

Same for `ImmediateTaskDispatcher` (drops tasks) and `NullTokenDenylist` (revokes nothing).
Each is a legitimate implementation of its contract, not a stub.

### Every `BaseRepository` subclass honours the base contract

`UserRepository.get()` returns `None` for a missing row, exactly as the base promises. It does
not raise. A subclass that raised would break every caller written against the base.

### Where substitutability is *deliberately* narrow

`BaseRepository.delete()` soft-deletes for `FoodItem` and hard-deletes for `User`. That is not
an LSP violation, because the contract is "make this row invisible to ordinary reads" — both
satisfy it. The contract was written at the level the callers actually depend on.

---

## I — Interface Segregation

*No client should be forced to depend on methods it does not use.*

### The Protocols are deliberately tiny

```python
class UserAuthenticator(Protocol):
    async def authenticate_access_token(self, token: str) -> CurrentUser: ...


class TaskDispatcher(Protocol):
    def dispatch(self, task: TaskCallable, *args, **kwargs) -> None: ...


class PasswordHasher(Protocol):
    def hash(self, plain: str) -> str: ...
    def verify(self, plain: str, hashed: str) -> bool: ...
    def needs_rehash(self, hashed: str) -> bool: ...
```

`shared/security` needs one thing from the users module. It asks for exactly one method — not
a `UserService` interface with twenty. A narrow Protocol is a strong Protocol: it is the
complete list of what a consumer may assume.

`Cache` has five methods and no `pipeline`, no `incr`, no Redis-specific anything.

### Read from the caller's side too

`notifications` exposes `notify_donation_accepted(recipient_id, food_item_name,
pickup_location, donation_id)` rather than a generic `create(user_id, type, title, body)`.

The narrow, meaningful signature **enforces** that a caller supplies the pickup location — a
notification saying "accepted!" without telling the recipient where to go is useless. A generic
four-string method cannot express that requirement.

---

## D — Dependency Inversion

*Depend on abstractions, not concretions.* The most heavily used principle here.

### 1. The `shared/` ↔ `modules/` problem — the real one

Authentication is cross-cutting so it belongs in `shared/`, but verifying a token needs
`User`, which lives in `modules/`. The naive fix breaks the architecture's one hard rule.

```
              principal.py  (CurrentUser + UserAuthenticator Protocol)
                    ▲                              ▲
        dependencies.py                   modules/users/service.py
    (placeholder that RAISES)          (satisfies it STRUCTURALLY —
                                        never imports it)
                            ▲
                      app/main.py
   app.dependency_overrides[get_user_authenticator] = provide_user_authenticator
```

Neither side imports the other. One line at the composition root joins them.

```bash
grep -rn "app\.modules" app/shared/    # -> no matches
```

This is DIP applied to a **real constraint**, not a textbook example.

### 2. Services never import the framework

```bash
grep -rn "^from fastapi\|^import fastapi" app/modules/*/service.py    # -> no matches
```

Because of that, `tests/modules/donations/test_service.py` tests the application's central
business rule with no HTTP client, no authentication and no clock mocking.

**One honest exception**, because a rule with a silent exception is not a rule:
`modules/users/service.py` imports `sqlalchemy.exc.IntegrityError` — to *catch* the
unique-constraint violation a concurrent duplicate registration produces and translate it into
a domain error. Importing an exception *class* is far weaker coupling than importing `select`
or the session (no SQL is written, no query is issued), but it is still a dependency on the
persistence library. So the precise claim is **"no SQLAlchemy API in services"**, not "no
SQLAlchemy at all". Documented at the top of that file.

### 3. `PasswordHasher` as a Protocol

* tests inject a cheap fake instead of paying ~250ms per bcrypt hash;
* bcrypt → argon2 is a 20-line adapter with zero service changes.

`needs_rehash()` means the cost factor can be raised later and existing users upgrade
transparently on their next login — the only way a rounds increase ever reaches existing
accounts.

### 4. The cross-module rule *is* DIP with teeth

`DonationService` depends on `FoodItemService` — the module's public behaviour — not on
`FoodItemRepository`, its storage. That is what makes the expiry invariant unbypassable rather
than merely conventional. See `app/modules/README.md`.

---

## DRY — and where it stops

### Where DRY pays

| Duplication removed | The cost of not removing it |
|---|---|
| `BaseRepository` (7 methods × 4 modules) | ~160 lines, and 4 places to fix one bug |
| **the soft-delete filter** | one forgotten `WHERE` exposes deleted rows to users |
| `naming_convention` | non-deterministic constraint names, so migrations break on deploy |
| `type_annotation_map` | one forgotten `timezone=True` = a naive timestamp in production |
| `PageParams` | 8 chances to write `page * size` or forget `le=100` |
| `_available_filters` | a `total` that disagrees with the items returned |
| `ERROR_RESPONSES_*` | 4 dicts restated on 40 endpoints |
| `Page[T]` | a hand-built envelope with `page`/`size` swapped in one router |
| `utcnow()` | naive/aware `TypeError` in production |

**The pattern: DRY is most valuable where forgetting is silent.** A forgotten soft-delete filter
does not raise — it leaks data. That is worth an abstraction. A forgotten import raises
immediately, so it is not.

### Where DRY is deliberately *not* applied

**`_transition` appears in both `FoodItemService` and `DonationService`.** Four near-identical
lines. Extracting a `StateMachineMixin` would couple two lifecycles that have no reason to
change together, and would need a generic transition table anyway. **Duplicating four lines is
cheaper than the wrong abstraction.**

**`cancel` and `decline` in `DonationService`** are separate methods with the same state
change. Merging them behind `by_recipient: bool` would entangle two authorization rules and two
message templates in one function.

**Expiry is validated in both the schema and the service.** Not a violation — they answer
different questions. The schema asks "is this input coherent?" (422, fix the payload); the
service asks "is this donatable *right now*?" (409, an item created yesterday can expire while
it sits in the database). The service's check is the one protecting the invariant.

**Uniqueness is checked twice on registration** — a pre-check for a clean error message, and
the unique constraint for correctness under a race. Neither alone is sufficient.

---

## KISS — and the trades it made

| Simple choice | The complex alternative | Why simple wins here |
|---|---|---|
| `Depends()` for DI | a `dependency-injector` container | FastAPI already resolves the graph, scopes it per request, and makes it overridable in tests. A container adds a second parallel mechanism. |
| CHECK constraint enums | native Postgres `ENUM` | `ALTER TYPE ... ADD VALUE` is awkward and effectively irreversible; a CHECK is a two-line reversible ALTER |
| async Alembic, one driver | `psycopg2` + `asyncpg` | one URL, one place to get the connection string wrong |
| offset pagination | keyset/cursor | it is what UIs need; the migration path is documented for when a table outgrows it |
| coarse cache invalidation | per-page invalidation | working out which page changed is hard and fragile; dropping a few short-lived keys costs almost nothing |
| `BackgroundTasks` | arq worker + a fourth container | there is no external channel yet; the Protocol makes the swap a small change |
| `SELECT ... FOR UPDATE` on one path | SERIALIZABLE isolation | that would make *every* transaction retryable — a far larger change for one narrow need |
| function-scoped test engine | session-scoped + shared loop | in-memory schema creation is milliseconds; the shared-loop version is fragile (`ScopeMismatch`) |

**KISS is not "less code".** `_dummy_verify` in `UserService.authenticate` *adds* a bcrypt call
on a failure path — because without it a 100× timing difference leaks exactly what the
identical error message was hiding. Simple to *understand*, not simple to *skip*.

---

## Verify the claims yourself

Copy-pasteable, and each **should print nothing**. They match on `import` statements only —
an earlier draft of this file used looser patterns that matched its own prose in docstrings and
reported false violations, which is a good reminder that a check you have not run is not a
check.

```bash
# 1. services never import the web framework
grep -rn "^from fastapi\|^import fastapi" app/modules/*/service.py

# 2. services never use the SQLAlchemy API (see the noted IntegrityError exception)
grep -rn "^from sqlalchemy import\|session\.execute\|select(" app/modules/*/service.py

# 3. shared/ and core/ never import a feature module
grep -rn "^from app\.modules\|^import app\.modules" app/shared/ app/core/ --include=*.py

# 4. no service reaches into ANOTHER module's repository
grep -rn "^from app\.modules\.[a-z_]*\.repository" app/modules/*/service.py \
  | grep -v -E "app\.modules\.(users)/service.py.*users|donations.*donations|food_items.*food_items|notifications.*notifications"

# 5. the authentication contract is framework-free
grep -rn "^from fastapi\|^import fastapi" app/shared/security/principal.py

# 6. exactly one source of "now" — this one SHOULD print only datetime_utils.py
grep -rln "datetime\.now(" app/ --include=*.py
```

Check 4 is awkward as a one-liner because a service legitimately imports **its own**
repository; the simpler manual version is to read the four import blocks — each imports only
`app.modules.<its own name>.repository`, and `donations/service.py` imports two sibling
**services**.

Check 6 prints `app/shared/utils/datetime_utils.py` and nothing else. It caught a real
violation in `app/core/security.py`, which was minting token `iat`/`exp` from
`datetime.now(UTC)` directly — the one place a test would most want to control the clock.
