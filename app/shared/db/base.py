"""The SQLAlchemy declarative base and its naming convention.

WHY THE NAMING CONVENTION IS THE MOST IMPORTANT THING IN THIS FILE
------------------------------------------------------------------
By default, PostgreSQL invents constraint names (`food_items_owner_id_fkey`)
and SQLAlchemy leaves unnamed CHECK/UNIQUE constraints anonymous. That breaks
migrations in a way you discover at the worst possible moment:

    op.drop_constraint("???", "food_items")   # what is it called?

`alembic revision --autogenerate` cannot emit a `DROP CONSTRAINT` for something
whose name it cannot predict. Worse, names differ between your local SQLite
test database and production Postgres, so a migration that ran fine in CI fails
on deploy.

Declaring the convention once here makes every constraint name **deterministic
and identical everywhere**. Cost: 7 lines. Benefit: migrations that work.
This is the single highest-leverage line of SQLAlchemy configuration there is.

WHY `type_annotation_map`
-------------------------
It maps Python types to column types *once*, project-wide. Without it every
model repeats `Mapped[datetime] = mapped_column(DateTime(timezone=True))` and
the day someone forgets `timezone=True` you get a naive timestamp in a
timezone-aware table. Encoding the decision once is DRY at the schema level.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy import Enum as SaEnum

# Aliased to make the dialect explicit at the use site below (N811 waived: the
# original name is an all-caps acronym, not a constant).
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import DeclarativeBase, declared_attr

# ix  = index               ix_food_items_expires_at
# uq  = unique constraint   uq_users_email
# ck  = check constraint    ck_food_items_quantity_positive
# fk  = foreign key         fk_donations_food_item_id_food_items
# pk  = primary key         pk_users
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_column(enum_class: type[StrEnum], *, length: int = 32) -> SaEnum:
    """The canonical way to map a `StrEnum` to a column in this project.

    WHY THIS HELPER EXISTS — A BUG IT PREVENTS
    The tempting shortcut is `Mapped[Role] = mapped_column(String(16))`. It works, and
    then it lies: SQLAlchemy performs no conversion, so a *freshly constructed* object
    holds a `Role` member while the *same row reloaded from the database* holds a plain
    `str`. Code that reads `user.role is Role.ADMIN` then passes in one code path and
    silently fails in the other — the worst kind of bug, because it is invisible until
    an object happens to have been reloaded.

    (This is not hypothetical: it is exactly what
    `tests/modules/donations/test_service.py::test_notification_is_created_for_the_donor`
    caught.)

    `SaEnum` converts in both directions, so the type annotation tells the truth.

    THE THREE KEYWORD CHOICES, EACH DELIBERATE:

    `native_enum=False`
        Emit `VARCHAR` rather than a native PostgreSQL `ENUM` type. Adding a value to a
        native enum needs `ALTER TYPE ... ADD VALUE`, which is awkward and effectively
        irreversible; widening a VARCHAR's CHECK constraint is a two-line reversible
        `ALTER TABLE`. Less elegant in the database, considerably less painful in
        practice.

    `create_constraint=False`
        Suppress SQLAlchemy's auto-generated CHECK constraint. The models declare their
        own CHECKs explicitly (with names that match the project naming convention and
        the handwritten migration), and two constraints enforcing the same rule under
        different names is exactly the drift this project avoids elsewhere.

    `values_callable=...`
        Store the enum's **value**, not its name. Load-bearing: `FoodUnit.KILOGRAM` has
        the value `"KG"`. SQLAlchemy's default stores names, which would put
        `"KILOGRAM"` in a column whose CHECK constraint only permits `"KG"` — an
        immediate constraint violation, and an API that returns a different string than
        it accepts.
    """
    return SaEnum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
    )


class Base(DeclarativeBase):
    """Common base for every entity in every module.

    NOTE ON MODULARITY: this single `metadata` object is the one place where the
    otherwise-independent feature modules necessarily meet — they share one
    physical database and one Alembic history. `migrations/env.py` imports every
    module's models so that autogenerate sees the complete picture. That is a
    deliberate, documented coupling point, not an accident. If you ever split a
    module into its own service, this is the seam you cut.
    """

    metadata = metadata

    type_annotation_map: dict[Any, Any] = {  # noqa: RUF012 — SQLAlchemy API
        # Always timezone-aware. Naive timestamps are a bug factory: they
        # silently mean "some timezone, hopefully UTC, ask the developer".
        datetime: DateTime(timezone=True),
        # Native Postgres uuid type — 16 bytes, not a 36-char string.
        uuid.UUID: PgUUID(as_uuid=True),
        # A bare `Mapped[str]` in Postgres would be unbounded TEXT. 255 is a
        # sane default; override per column where a different length is right.
        str: String(255),
    }

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805 — SQLAlchemy passes the class
        """Derive `snake_case` plural-ish table names from class names.

        `FoodItem` -> `food_items`, `User` -> `users`.

        Convention over configuration: no `__tablename__` line in any model, and
        no chance of two developers picking `fooditem` and `food_item`.
        Override explicitly in a model when the derivation is wrong.
        """
        name = cls.__name__
        snake = name[0].lower() + "".join(
            f"_{char.lower()}" if char.isupper() else char for char in name[1:]
        )
        # Naive pluralisation is enough for our entities; add cases if needed.
        if snake.endswith("y"):
            return f"{snake[:-1]}ies"
        if snake.endswith(("s", "x", "z", "ch", "sh")):
            return f"{snake}es"
        return f"{snake}s"

    def __repr__(self) -> str:
        """Show the primary key only.

        Deliberately does NOT dump all columns: a `User` repr would then print
        `hashed_password` into logs and tracebacks. Also avoids triggering lazy
        loads (and thus `MissingGreenlet` errors) from inside a debugger.
        """
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"
