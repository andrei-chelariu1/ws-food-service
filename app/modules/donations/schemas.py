"""DTOs for the donations module."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.modules.donations.models import DonationStatus
from app.modules.food_items.schemas import FoodItemRead
from app.shared.schemas.base_schema import ApiModel, TimestampedSchema


class DonationCreate(ApiModel):
    """Request a food item.

    `recipient_id` is absent — it comes from the token. `status` is absent — it is
    always PENDING. A client cannot create a request on someone else's behalf, nor
    a pre-accepted one; with `extra="forbid"` both attempts are a 422.

    `food_item_id` in the body rather than the path because the resource being
    created is a *donation*: `POST /donations` is the correct REST shape, and the
    item is an attribute of the thing being created.
    """

    food_item_id: uuid.UUID
    note: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional message to the donor",
        examples=["I can collect this evening after 6pm."],
    )


class DonationDecline(ApiModel):
    """Owner declines a request."""

    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Shown to the recipient",
        examples=["Already promised to a local shelter."],
    )


class DonationRead(TimestampedSchema):
    """Donation without the nested item — the list-view shape.

    Deliberately flat. The nested variant below costs an extra JOIN, so the cheap
    shape is the default and callers opt into the expensive one. Two schemas is the
    honest way to expose that choice; one schema with an optional nested field
    would hide it and quietly produce N+1 queries.
    """

    id: uuid.UUID
    food_item_id: uuid.UUID
    recipient_id: uuid.UUID
    status: DonationStatus
    note: str | None
    decline_reason: str | None


class DonationWithItemRead(DonationRead):
    """Donation with its food item embedded — the detail-view shape.

    Inherits from `DonationRead` rather than redeclaring seven fields, so adding a
    field to the base automatically appears in both (DRY).

    The service must eager-load `food_item` before this is validated. It cannot
    silently lazy-load: `lazy="raise"` on the relationship turns that mistake into
    an immediate, obvious error instead of an N+1 that only shows up under load.
    """

    food_item: FoodItemRead
