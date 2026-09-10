"""DTOs for the food items module.

Same Create/Update/Read split as the users module, and for the same reason: input
schemas are *allowlists*. `FoodItemCreate` has no `owner_id` and no `status`, so
a client cannot list food on someone else's behalf or declare its own item
`DONATED`. Both are set by the service from the authenticated caller and the state
machine.

Note `quantity: Decimal`, not `float`. The column is NUMERIC (see models.py); using
`float` in the DTO would reintroduce binary rounding at the JSON boundary and
defeat the point of the column type.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.modules.food_items.models import FoodItemStatus, FoodUnit
from app.shared.schemas.base_schema import ApiModel, TimestampedSchema
from app.shared.utils.datetime_utils import utcnow

# `max_digits`/`decimal_places` mirror `Numeric(10, 3)` exactly. Kept aligned so
# a value that passes validation cannot then be rejected by the database — a 422
# from Pydantic is a clear error, a NUMERIC overflow from Postgres is a 500.
QuantityField = Annotated[
    Decimal,
    Field(
        gt=0,
        max_digits=10,
        decimal_places=3,
        description="Must be greater than zero",
        examples=["2.500"],
    ),
]


class FoodItemCreate(ApiModel):
    """New listing.

    Absent by design: `owner_id` (taken from the token) and `status` (always
    AVAILABLE). With `extra="forbid"`, sending either is a 422 — a client cannot
    list food under another user's name.
    """

    name: str = Field(min_length=2, max_length=255, examples=["Fresh sourdough loaves"])
    description: str | None = Field(default=None, max_length=1000)
    quantity: QuantityField
    unit: FoodUnit
    expires_at: datetime = Field(description="Timezone-aware; must be in the future")
    pickup_location: str = Field(min_length=3, max_length=500, examples=["12 Main St, Cluj"])

    @field_validator("expires_at")
    @classmethod
    def must_be_future(cls, value: datetime) -> datetime:
        """Reject a past expiry date at the edge.

        This duplicates a check the service also performs, and the duplication is
        deliberate rather than a DRY violation. They answer different questions:

          * here: "is this input coherent?" -> 422, fix the payload;
          * in the service: "is this item donatable *right now*?" -> 409, because
            an item created yesterday can expire while it sits in the database.

        The service's check is the one that protects the invariant. This one exists
        to give an immediately actionable error instead of accepting a listing that
        is dead on arrival.
        """
        if value.tzinfo is None:
            # Rejected rather than assumed-UTC: guessing produces an item that
            # expires at the wrong moment for anyone outside UTC, and the client
            # never learns it sent an ambiguous value.
            raise ValueError("expires_at must include a timezone offset, e.g. 2026-08-01T18:00:00Z")
        if value <= utcnow():
            raise ValueError("expires_at must be in the future")
        return value


class FoodItemUpdate(ApiModel):
    """Partial edit by the owner. All fields optional.

    No `status` field: status changes go through dedicated endpoints
    (`/cancel`) or through the donations flow, because each transition has its own
    rules and its own authorization. A general-purpose `status` field would make
    the state machine bypassable with a single PATCH.
    """

    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    quantity: QuantityField | None = None
    unit: FoodUnit | None = None
    expires_at: datetime | None = None
    pickup_location: str | None = Field(default=None, min_length=3, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def must_be_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        if value <= utcnow():
            raise ValueError("expires_at must be in the future")
        return value

    @model_validator(mode="after")
    def at_least_one_field(self) -> FoodItemUpdate:
        """An empty PATCH reports success while changing nothing — the kind of
        silent no-op that has clients retrying a broken integration for hours."""
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


class FoodItemRead(TimestampedSchema):
    """Public view of a listing.

    `is_deleted`/`deleted_at` are deliberately absent: soft deletion is an
    internal storage concern, and exposing it tells clients about rows they can
    never see.

    `is_expired` is computed rather than stored, so it can never contradict
    `expires_at` — a boolean column would go stale the moment the clock passed the
    date.
    """

    id: uuid.UUID
    name: str
    description: str | None
    quantity: Decimal
    unit: FoodUnit
    expires_at: datetime
    status: FoodItemStatus
    pickup_location: str
    owner_id: uuid.UUID

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utcnow()


class FoodItemCancel(ApiModel):
    """Optional reason when withdrawing a listing."""

    reason: str | None = Field(default=None, max_length=500)
