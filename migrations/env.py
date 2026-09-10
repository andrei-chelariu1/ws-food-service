"""Alembic environment — async, single-driver.

WHY THIS FILE IMPORTS EVERY MODULE'S MODELS
-------------------------------------------
`Base.metadata` is populated as a side effect of importing model classes. Alembic
compares that metadata against the live database to autogenerate migrations. Miss
an import and autogenerate will happily emit `DROP TABLE donations`, because from
its point of view that table exists in the database and not in the code.

So the import block below is load-bearing, not decorative. **Adding a module means
adding a line there.** It is also the one place where the otherwise-independent
feature modules necessarily meet — one physical database, one migration history.
That coupling is deliberate and documented; see app/shared/db/base.py.

WHY ASYNC (a deviation from ARCHITECTURE.md)
--------------------------------------------
ARCHITECTURE.md lists `psycopg2-binary` so Alembic can run synchronously. That
means two drivers, two connection URLs, and two ways for the connection string to
be wrong. Alembic supports async natively via `connection.run_sync`, so we use one
driver (`asyncpg`) and one URL. See docs/adr/0003-async-alembic-single-driver.md.

WHY THE URL COMES FROM `Settings` AND NOT `alembic.ini`
------------------------------------------------------
One source of truth for the connection string, and no credentials in a committed
file. `alembic.ini` leaves `sqlalchemy.url` empty on purpose.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# --- Model imports: required for autogenerate. Add a line per new module. ---
# `noqa: F401` because these are imported for their registration side effect, not
# to be referenced here.
from app.modules.donations.models import Donation  # noqa: F401
from app.modules.food_items.models import FoodItem  # noqa: F401
from app.modules.notifications.models import Notification  # noqa: F401
from app.modules.users.models import User  # noqa: F401
from app.shared.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# Injected at runtime rather than read from the ini file.
config.set_main_option("sqlalchemy.url", settings.database_url_str)

target_metadata = Base.metadata


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Filter what autogenerate considers.

    Without this, running autogenerate against a database that also hosts other
    schemas or extension tables produces spurious `DROP` statements for objects this
    application does not own. Explicit is safer than sorry when the output is DDL.
    """
    return not (type_ == "table" and name in {"spatial_ref_sys"})


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade head --sql`).

    Useful in two real situations: a DBA must review the DDL before it runs, or the
    deploy pipeline has no network access to the database and hands a script to
    someone who does.
    """
    context.configure(
        url=settings.database_url_str,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render the type of every column change explicitly, so a reviewer reading
        # the generated SQL sees the actual types rather than inferring them.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """The synchronous body, executed inside `run_sync`.

    Alembic's migration machinery is synchronous. `run_sync` hands it a sync-style
    proxy over the async connection, which is what makes an async driver work
    without a second sync driver installed.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        # Detect column type changes (e.g. VARCHAR(255) -> VARCHAR(320)). Off by
        # default, which is a common cause of "autogenerate produced an empty
        # migration" confusion.
        compare_type=True,
        # Detect changes to server-side defaults too.
        compare_server_default=True,
        # Wrap each migration in its own transaction so a failure leaves the
        # database at the last complete revision rather than half-migrated.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine and run the migrations through it.

    `poolclass=pool.NullPool`: this is a short-lived one-shot process, so a
    connection pool would only delay exit while its idle connections time out.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
