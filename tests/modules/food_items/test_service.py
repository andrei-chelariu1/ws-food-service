"""Tests for `FoodItemService`: the state machine, ownership, and expiry."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.core.exceptions import PermissionDeniedError
from app.modules.food_items.exceptions import (
    FoodItemExpiredError,
    FoodItemImmutableError,
    FoodItemNotAvailableError,
    FoodItemNotFoundError,
    InvalidStatusTransitionError,
)
from app.modules.food_items.models import FoodItemStatus, FoodUnit
from app.modules.food_items.schemas import FoodItemCreate, FoodItemUpdate
from app.modules.food_items.service import FoodItemService
from app.shared.utils.datetime_utils import utcnow


def _create_payload(**overrides: Any) -> FoodItemCreate:
    defaults: dict[str, Any] = {
        "name": "Sourdough loaves",
        "description": "Baked this morning",
        "quantity": Decimal("3.000"),
        "unit": FoodUnit.KILOGRAM,
        "expires_at": utcnow() + timedelta(days=2),
        "pickup_location": "12 Main Street",
    }
    return FoodItemCreate(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
async def test_create_assigns_owner_from_the_authenticated_user(
    food_item_service: FoodItemService,
    make_user: Any,
    as_current_user: Any,
) -> None:
    """`owner_id` comes from the token, never from the payload.

    `FoodItemCreate` has no `owner_id` field, so listing food under another user's name
    is unexpressible rather than merely rejected.
    """
    donor = await make_user()

    item = await food_item_service.create(_create_payload(), as_current_user(donor))

    assert item.owner_id == donor.id
    assert item.status is FoodItemStatus.AVAILABLE


async def test_quantity_keeps_decimal_precision(
    food_item_service: FoodItemService,
    make_user: Any,
    as_current_user: Any,
) -> None:
    """`Decimal`, not `float`, all the way through.

    `0.1 + 0.2 != 0.3` in binary floating point, and these quantities are summed for
    impact reporting. The column is NUMERIC and the DTO is Decimal so the two agree.
    """
    donor = await make_user()

    item = await food_item_service.create(
        _create_payload(quantity=Decimal("0.125")), as_current_user(donor)
    )

    assert item.quantity == Decimal("0.125")
    assert isinstance(item.quantity, Decimal)


def test_create_schema_rejects_a_past_expiry_date() -> None:
    """Rejected at the edge with a 422, before any service call.

    Complements the service's own check: this one says "your input is wrong, fix it",
    the service's says "this item is no longer donatable". Different questions,
    different status codes.
    """
    with pytest.raises(ValueError, match="future"):
        _create_payload(expires_at=utcnow() - timedelta(hours=1))


def test_create_schema_rejects_a_naive_datetime() -> None:
    """A timezone-less `expires_at` is rejected rather than assumed to be UTC.

    Guessing produces an item that expires at the wrong moment for anyone outside UTC,
    and the client never learns it sent an ambiguous value.
    """
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone"):
        _create_payload(expires_at=datetime(2030, 1, 1, 12, 0, 0))


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------
async def test_owner_can_update_their_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    updated = await food_item_service.update(
        item.id, FoodItemUpdate(name="Renamed loaves"), as_current_user(donor)
    )

    assert updated.name == "Renamed loaves"


async def test_stranger_cannot_update_someone_elses_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """THE IDOR TEST at the service level.

    Valid token, well-formed id, existing row — and it must still fail. Without
    `require_ownership` this succeeds, and nothing else in the stack would catch it.
    """
    donor = await make_user()
    stranger = await make_user()
    item = await make_food_item(owner_id=donor.id)

    with pytest.raises(PermissionDeniedError):
        await food_item_service.update(
            item.id, FoodItemUpdate(name="Hijacked"), as_current_user(stranger)
        )

    assert item.name != "Hijacked"


async def test_admin_can_update_any_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Admins override ownership, and the override is logged for audit."""
    donor = await make_user()
    admin = await make_user(role="ADMIN")
    item = await make_food_item(owner_id=donor.id)

    updated = await food_item_service.update(
        item.id, FoodItemUpdate(name="Moderated"), as_current_user(admin)
    )
    assert updated.name == "Moderated"


async def test_update_is_a_true_patch(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Fields absent from the payload are left untouched.

    This is `exclude_unset=True` in the service. Without it, every omitted field would
    be overwritten with its default — a client updating the name would silently blank
    the description and the pickup location.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)
    original_location = item.pickup_location

    await food_item_service.update(
        item.id, FoodItemUpdate(name="Only the name"), as_current_user(donor)
    )

    assert item.name == "Only the name"
    assert item.pickup_location == original_location


async def test_reserved_item_cannot_be_edited(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Once reserved, the details are frozen.

    A recipient has already decided based on the quantity and pickup location as
    listed. Letting the owner change them under that decision is the bug being
    prevented.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id, status=FoodItemStatus.RESERVED)

    with pytest.raises(FoodItemImmutableError):
        await food_item_service.update(
            item.id, FoodItemUpdate(quantity=Decimal("0.500")), as_current_user(donor)
        )


# --------------------------------------------------------------------------
# Reservation — the invariant
# --------------------------------------------------------------------------
async def test_reserve_rejects_an_expired_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """The invariant, tested directly at the method that owns it.

    Note the item's status is still AVAILABLE — the check is against `expires_at`, not
    against `status`, which is the whole reason a stale status cannot let expired food
    through.
    """
    donor = await make_user()
    item = await make_food_item(
        owner_id=donor.id, expires_in_hours=-1, status=FoodItemStatus.AVAILABLE
    )

    with pytest.raises(FoodItemExpiredError):
        await food_item_service.reserve(item.id)


async def test_reserve_rejects_an_already_reserved_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id, status=FoodItemStatus.RESERVED)

    with pytest.raises(FoodItemNotAvailableError):
        await food_item_service.reserve(item.id)


async def test_expiry_is_checked_before_status(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """An expired *and* reserved item reports EXPIRED, not NOT_AVAILABLE.

    Pins the check ordering in `reserve()`. Expiry is the more fundamental failure and
    the more useful message; reporting "not available" for food that has gone off would
    be actively misleading.
    """
    donor = await make_user()
    item = await make_food_item(
        owner_id=donor.id, expires_in_hours=-1, status=FoodItemStatus.RESERVED
    )

    with pytest.raises(FoodItemExpiredError):
        await food_item_service.reserve(item.id)


async def test_reserve_missing_item_is_404(food_item_service: FoodItemService) -> None:
    import uuid

    with pytest.raises(FoodItemNotFoundError):
        await food_item_service.reserve(uuid.uuid4())


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------
async def test_donated_item_is_terminal(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """A DONATED item cannot be cancelled or re-reserved.

    Enforced by `ALLOWED_TRANSITIONS`, checked in one place. The rules are data, so
    adding a state needs no new conditionals.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id, status=FoodItemStatus.DONATED)

    with pytest.raises(InvalidStatusTransitionError):
        await food_item_service.cancel(item.id, as_current_user(donor))

    with pytest.raises(FoodItemNotAvailableError):
        await food_item_service.reserve(item.id)


async def test_owner_cannot_cancel_a_reserved_item(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """RESERVED -> CANCELLED is absent from the transition table.

    A recipient is counting on that food; the owner must decline the donation request
    instead, which notifies them. The rule is expressed by an omission in the table
    rather than by an `if` in the service.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id, status=FoodItemStatus.RESERVED)

    with pytest.raises(InvalidStatusTransitionError):
        await food_item_service.cancel(item.id, as_current_user(donor))


async def test_release_returns_a_valid_item_to_available(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    donor = await make_user()
    item = await make_food_item(
        owner_id=donor.id, expires_in_hours=48, status=FoodItemStatus.RESERVED
    )

    released = await food_item_service.release(item.id)

    assert released.status is FoodItemStatus.AVAILABLE


async def test_release_sends_an_expired_item_to_expired(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """Food that expired while reserved is not re-listed as available.

    Otherwise the release path would quietly reintroduce the exact failure the
    invariant exists to prevent.
    """
    donor = await make_user()
    item = await make_food_item(
        owner_id=donor.id, expires_in_hours=-1, status=FoodItemStatus.RESERVED
    )

    released = await food_item_service.release(item.id)

    assert released.status is FoodItemStatus.EXPIRED


# --------------------------------------------------------------------------
# Soft delete
# --------------------------------------------------------------------------
async def test_delete_is_soft_and_hides_the_item(
    food_item_service: FoodItemService,
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Deleting hides the row without removing it.

    Both halves matter: the item must disappear from reads (so clients see it gone) and
    the row must survive (so a `Donation` referencing it keeps its audit trail).
    `BaseRepository.delete` chooses soft-delete automatically because `FoodItem` carries
    the mixin — the service does not decide, and cannot get it wrong.
    """
    from app.modules.food_items.repository import FoodItemRepository

    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)
    item_id = item.id

    await food_item_service.soft_delete(item_id, as_current_user(donor))

    repo = FoodItemRepository(db_session)
    # Invisible to ordinary reads: `_base_select()` filters it out, with no explicit
    # `where` at any call site.
    assert await repo.get(item_id) is None
    # But still present when explicitly asked for.
    assert await repo.get(item_id, include_deleted=True) is not None


async def test_expire_stale_items_marks_past_date_items(
    food_item_service: FoodItemService,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """The sweep is idempotent: a second run changes nothing.

    Its WHERE clause excludes already-EXPIRED rows, so it is safe on a schedule, run
    twice concurrently, or by hand during an incident.
    """
    donor = await make_user()
    await make_food_item(owner_id=donor.id, expires_in_hours=-5)
    await make_food_item(owner_id=donor.id, expires_in_hours=-2)
    await make_food_item(owner_id=donor.id, expires_in_hours=48)  # still fresh

    assert await food_item_service.expire_stale_items() == 2
    assert await food_item_service.expire_stale_items() == 0  # idempotent
