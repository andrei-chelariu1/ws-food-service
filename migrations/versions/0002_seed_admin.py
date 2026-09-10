"""Seed the initial admin account.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29

WHY SEEDING IS A MIGRATION AND NOT A SCRIPT
-------------------------------------------
A fresh database with no admin is unusable — there is no way to promote anyone,
because promotion requires an admin. So the first admin must be created by
something that is not the API.

Making it a migration means it runs automatically wherever migrations run: a
developer's laptop, CI, staging, production. A separate `seed.py` is something
someone must remember to run, and the failure mode is a deploy that completes
"successfully" into an unusable system.

THIS MIGRATION IS IDEMPOTENT
----------------------------
`INSERT ... WHERE NOT EXISTS` rather than a bare `INSERT`. It can be run against a
database that already has the admin (a re-run, a restored backup, a stamped-then-
upgraded history) without failing on the unique constraint.

THE PASSWORD IS HASHED HERE, NOT STORED IN PLAINTEXT
----------------------------------------------------
The migration imports the project's own `BcryptPasswordHasher`, so the seeded hash
is produced by exactly the same code path as a real registration. Writing a
hardcoded hash literal would work until someone changed the algorithm, at which
point the seeded admin would silently stop being able to log in.

SECURITY: `SEED_ADMIN_PASSWORD` comes from configuration, and `Settings.
assert_production_safe()` refuses to start if it is left at the sample value. The
credential is never committed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings
from app.core.security import BcryptPasswordHasher

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    settings = get_settings()

    hasher = BcryptPasswordHasher(rounds=settings.BCRYPT_ROUNDS)
    hashed_password = hasher.hash(settings.SEED_ADMIN_PASSWORD.get_secret_value())

    # Raw SQL rather than the ORM. A migration must keep working against the schema
    # *as it was at this revision*: if `User` later gains a column, an ORM-based
    # insert here would emit SQL referencing a column that does not yet exist when
    # this migration runs on a fresh database. Migrations pin the schema; models
    # track the present. Never mix them.
    op.execute(
        sa.text(
            """
            INSERT INTO users (email, hashed_password, full_name, role, is_active)
            SELECT :email, :hashed_password, :full_name, 'ADMIN', true
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = :email)
            """
        ).bindparams(
            email=settings.SEED_ADMIN_EMAIL.lower(),
            hashed_password=hashed_password,
            full_name="Platform Administrator",
        )
    )


def downgrade() -> None:
    """Remove the seeded admin.

    Deletes only by the exact seeded email, so a rollback cannot take other
    administrators with it.

    Will fail with a foreign-key violation if this account has listed food or
    received donations — and that is the correct behaviour. A rollback that silently
    destroyed real data would be far worse than one that stops and makes a human
    decide.
    """
    settings = get_settings()
    op.execute(
        sa.text("DELETE FROM users WHERE email = :email AND role = 'ADMIN'").bindparams(
            email=settings.SEED_ADMIN_EMAIL.lower(),
        )
    )
