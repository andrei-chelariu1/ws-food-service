# Documentation Index

Two kinds of document, and the difference matters:

* **Topic docs** describe how something works *now* and are updated when the code changes.
* **ADRs** record a decision at a point in time and are **never rewritten** — if a decision is
  reversed, a new ADR supersedes it. An amended ADR loses the only thing it was for: telling you
  what was known when the call was made.

Directory `README.md` files are a third kind — they live next to the code they describe, so they
are read while you are in that directory. Everything here is cross-cutting.

---

## Topic docs

| Doc | Read it when |
|---|---|
| [SOLID.md](SOLID.md) | you want each principle pointed at a real file in this repo — plus the places DRY is deliberately **not** applied, and copy-pasteable commands to verify every claim |
| [SECURITY.md](SECURITY.md) | threat by threat: what is defended, what is deliberately not, and the deployment requirements the application cannot enforce itself |
| [DATABASE.md](DATABASE.md) | adding a migration, or wondering why constraints are named, why soft delete is centralised, or how the unit of work is scoped |
| [CACHING.md](CACHING.md) | changing what is cached — especially the rule about the cache key and per-user data |
| [RATE_LIMITING.md](RATE_LIMITING.md) | changing a limit, adding a rate-limited endpoint (**there is a required signature**), or hardening a deployment against `X-Forwarded-For` spoofing |
| [TESTING.md](TESTING.md) | writing a test, or wondering why the suite needs no containers |

## ADRs

| ADR | Decision |
|---|---|
| [0001](adr/0001-pyjwt-over-python-jose.md) | PyJWT instead of python-jose |
| [0002](adr/0002-bcrypt-direct-over-passlib.md) | `bcrypt` directly, behind a `PasswordHasher` Protocol |
| [0003](adr/0003-async-alembic-single-driver.md) | Async Alembic, one driver |
| [0004](adr/0004-backgroundtasks-behind-protocol.md) | `BackgroundTasks` behind a `TaskDispatcher` Protocol |

The first three are **deviations from `ARCHITECTURE.md`**. That document is the design brief and
is left untouched; where the implementation departs from it, the departure is argued here rather
than made silently. A design doc that quietly disagrees with the code is worse than one that is
openly amended.

---

## Reading order

**To understand the architecture** — `../app/README.md` (the layers and the dependency rule) →
`../app/modules/README.md` (the vertical-slice and cross-module rules) → `SOLID.md`.

**To review it for production** — `SECURITY.md` → `RATE_LIMITING.md` (the `X-Forwarded-For`
requirement) → `DATABASE.md` (migration workflow) → ADR 0004 (the notification limitation).

**To add a feature** — the module's own `README.md` → `TESTING.md` → `DATABASE.md` if there is a
schema change.

---

## What these docs try to do

State the **trade-off**, not just the choice. Every doc here has a "known limitations" or "what we
deliberately don't do" section, because:

* the interesting engineering content is in what was given up;
* a limitation someone rediscovers under load costs far more than one they read about;
* several of these were found by *running* the checks rather than reasoning about them, and that
  is worth showing. `SOLID.md`'s verification section exists because an earlier draft's greps
  reported false violations — a check nobody has run is not a check.

Where something is described as observed — a 500, a doubled constraint name, a blocked CSP — it
was observed, and the command that showed it is included.
