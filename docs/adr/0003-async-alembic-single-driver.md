# ADR 0003 — Async Alembic with one driver

**Status:** accepted · **Deviates from:** `ARCHITECTURE.md`, which lists `psycopg2-binary` for
synchronous Alembic

## Context

The application is async and uses `asyncpg`. Alembic's default `env.py` is synchronous. The usual
workaround is to add `psycopg2-binary` and give Alembic its own sync URL.

## Decision

Keep **`asyncpg` only**. `migrations/env.py` drives migrations through an async engine using
`connection.run_sync(do_run_migrations)`.

## Why

**One URL.** Two drivers means two connection strings that must agree on host, port, database,
user and password, differing only in scheme (`postgresql+asyncpg://` vs `postgresql://`). They
drift. The failure mode is the worst kind: **migrations run against the wrong database and
succeed**, or a password rotation updates one and not the other and the API works while
deployment migrations fail.

`settings.DATABASE_URL` is the single source, and `env.py` reads it from `Settings` — not from
`alembic.ini`, which is why `sqlalchemy.url` there is a placeholder.

**One dependency.** `psycopg2-binary` is a compiled wheel with its own libpq bundling and its own
CVE stream, added solely to run migrations. Removing it removes a build-time and
supply-chain concern.

**One set of semantics.** Both paths connect the same way, so a TLS or pool setting cannot be
right in the API and wrong in migrations.

**The mechanism is small:**

```python
async def run_async_migrations() -> None:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()
```

`run_sync` hands Alembic a synchronous-looking connection backed by the async one — Alembic's own
code stays unchanged. `NullPool` because a migration run is one short-lived connection; pooling it
serves no purpose and leaves connections open at exit.

## Consequences

* `env.py` is ~15 lines longer than the generated template, with the async wrapper spelled out.
  Worth reading once.
* `alembic upgrade head` requires the async stack, so it cannot be run in an environment with only
  a sync driver installed. In practice it runs from the same container as the app.
* **Not the sync-driver path most Alembic tutorials show.** Someone copying a snippet from a blog
  post may be confused; `migrations/README.md` explains it.
* Autogenerate works normally — `run_sync` gives Alembic real DDL introspection.

## Alternatives

| Option | Rejected because |
|---|---|
| `psycopg2` for Alembic (as specified) | two URLs, two drivers, silent drift |
| `psycopg` 3 for both (it does sync and async) | a single-driver option and a reasonable choice; `asyncpg` is faster and is what SQLAlchemy's async docs lead with |
| Raw SQL files run by a shell script | loses autogenerate, version tracking and `downgrade` |
