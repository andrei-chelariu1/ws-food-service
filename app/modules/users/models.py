"""User entity and the Role enum.

WHAT BELONGS IN A MODEL FILE
----------------------------
Columns, relationships, constraints, and *invariant-free* derived properties.
Nothing else. In particular: no password hashing (that needs a configured
hasher — `core/security.py`), no queries (`repository.py`), no business rules
(`service.py`).

The temptation is to add `def check_password(self, plain)` here, because it reads
nicely. Resist it: the entity would then need a hasher, so it would need
configuration, so constructing a `User` in a test would need a hasher too. An
anaemic-looking model is the price of an entity you can instantiate in one line.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import Base, enum_column
from app.shared.db.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    # Import-only-for-typing: keeps the runtime import graph acyclic while
    # `Mapped["FoodItem"]` still type-checks. SQLAlchemy resolves the string
    # name lazily at mapper-configuration time, so nothing is needed at runtime.
    from app.modules.donations.models import Donation
    from app.modules.food_items.models import FoodItem
    from app.modules.notifications.models import Notification


class Role(StrEnum):
    """User roles.

    A `StrEnum` rather than a plain `Enum` so it serialises to `"ADMIN"` in JSON
    with no custom encoder, and compares equal to the string form coming back
    from a JWT claim or from `CurrentUser.role`.

    Stored as a VARCHAR with a CHECK constraint rather than a native Postgres
    ENUM type. Reason: adding a value to a Postgres enum requires
    `ALTER TYPE ... ADD VALUE`, which historically could not run inside a
    transaction and cannot be reversed — a genuinely painful migration. A CHECK
    constraint is a two-line, fully reversible `ALTER TABLE`. Slightly less
    elegant in the database, considerably less painful in practice (KISS applied
    to operations, not to code).
    """

    DONOR = "DONOR"  # lists surplus food
    RECIPIENT = "RECIPIENT"  # requests donations
    ADMIN = "ADMIN"  # moderates everything


class User(Base, UUIDMixin, TimestampMixin):
    """A registered account.

    No `SoftDeleteMixin`: user deletion is a GDPR question, not a CRUD one, and
    "soft-deleted" accounts holding a unique email would block that address from
    ever being reused. Deactivation (`is_active = false`) covers the realistic
    case; genuine erasure goes through `hard_delete_by_id`.
    """

    email: Mapped[str] = mapped_column(
        String(320),  # RFC 5321 maximum: 64-char local part + @ + 255-char domain
        unique=True,
        nullable=False,
        index=True,
        comment="Stored lowercase; normalised by the schema on input",
    )

    # Named `hashed_password`, never `password`. The name is a warning label: a
    # reviewer seeing `user.password` in a log statement knows instantly that
    # something is wrong, whereas `user.password` is ambiguous.
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        # This column must never be serialised. `UserRead` in schemas.py simply
        # does not declare it, so exclusion is structural rather than a rule
        # someone has to remember.
        comment="bcrypt hash — never expose in any API response",
    )

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[Role] = mapped_column(
        enum_column(Role, length=16),
        nullable=False,
        default=Role.DONOR,
        server_default=text(f"'{Role.DONOR.value}'"),
        index=True,  # supports "list all admins" and role-filtered queries
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="False = deactivated; blocked at the get_current_active_user dependency",
    )

    # --- Relationships ----------------------------------------------------
    # `lazy="raise"` is the most valuable setting on this page. The default
    # (`lazy="select"`) issues a hidden SELECT the first time an attribute is
    # touched, which in async SQLAlchemy raises the famously opaque
    # `MissingGreenlet` error — and in sync code silently produces N+1 queries.
    #
    # With `lazy="raise"`, forgetting to eager-load fails immediately with a
    # message naming the attribute. You cannot accidentally ship an N+1.
    # Loading is then always explicit: `selectinload(User.food_items)`.
    food_items: Mapped[list[FoodItem]] = relationship(
        back_populates="owner",
        lazy="raise",
        # No cascade delete: a donated item's history must outlive the account.
        passive_deletes=True,
    )
    donations: Mapped[list[Donation]] = relationship(
        back_populates="recipient",
        lazy="raise",
        passive_deletes=True,
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user",
        lazy="raise",
        # Notifications are worthless without their recipient, so they do cascade.
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Enforced in the database, not only in Python. Pydantic validation
        # protects the API; a CHECK constraint also protects against a bad data
        # migration, a manual psql UPDATE, or a second service writing to this
        # table. Defence in depth applied to data integrity.
        #
        # Generated from the enum itself, so adding a `Role` member cannot leave
        # the constraint behind — DRY between Python and SQL. The constraint name
        # is explicit because `naming_convention` needs a `constraint_name` for
        # the `ck` pattern (see shared/db/base.py).
        CheckConstraint(
            f"role IN ({', '.join(repr(r.value) for r in Role)})",
            name="role_valid",
        ),
        # Partial index: "list active admins" and similar admin screens only ever
        # look at enabled accounts, so indexing the deactivated ones is wasted
        # space and write cost.
        Index(
            "ix_users_role_active",
            "role",
            postgresql_where=text("is_active"),
        ),
    )

    @property
    def is_admin(self) -> bool:
        """Convenience predicate. Safe here — pure, no I/O, no configuration."""
        return self.role == Role.ADMIN
