"""Tests for `DonationService` — the cross-module business rules.

THIS IS THE MOST IMPORTANT TEST FILE IN THE PROJECT
---------------------------------------------------
`test_expired_food_cannot_be_donated` verifies the invariant the whole application
exists to protect. Notice what it takes to write it:

* no HTTP client;
* no authentication;
* no mocking of `datetime.now`;
* no Redis.

Just three objects and an item whose `expires_at` is in the past. That is only
possible because `DonationService` receives its collaborators through its
constructor and raises domain exceptions rather than HTTP ones. If the service
imported `HTTPException`, this test would have to assert on a status code — testing
the transport instead of the rule.

The test also proves the cross-module rule holds: the expiry check lives in
`FoodItemService.reserve`, and it fires here even though this test never calls it
directly. That is the guarantee described in app/modules/donations/service.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.donations.exceptions import (
    CannotDonateToSelfError,
    DuplicateDonationRequestError,
    InvalidDonationTransitionError,
    NotDonationOwnerError,
)
from app.modules.donations.models import DonationStatus
from app.modules.donations.repository import DonationRepository
from app.modules.donations.schemas import DonationCreate, DonationDecline
from app.modules.donations.service import DonationService
from app.modules.food_items.exceptions import (
    FoodItemExpiredError,
    FoodItemNotAvailableError,
)
from app.modules.food_items.models import FoodItemStatus
from app.modules.users.models import Role


@pytest.fixture
def donation_service(
    db_session: Any,
    food_item_service: Any,
    notification_service: Any,
) -> DonationService:
    """Assemble the service under test.

    Exactly the same object graph `get_donation_service` builds in `api.py`, minus
    FastAPI. Note the second argument is a `FoodItemService` — a service, not a
    repository — which is the boundary being verified here.
    """
    return DonationService(
        repository=DonationRepository(db_session),
        food_item_service=food_item_service,
        notification_service=notification_service,
    )


# --------------------------------------------------------------------------
# THE central business rule
# --------------------------------------------------------------------------
async def test_expired_food_cannot_be_donated(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """An expired item cannot be requested, even while its status still says AVAILABLE.

    This is the case a naive implementation gets wrong. The item's `status` is
    AVAILABLE because no expiry sweep has run — so a check against `status` alone
    would let it through. `FoodItemService.reserve` checks `expires_at` instead, which
    is why this fails correctly.
    """
    donor = await make_user(role=Role.DONOR)
    recipient = await make_user(role=Role.RECIPIENT)

    # Negative hours: expired an hour ago, still marked AVAILABLE.
    item = await make_food_item(
        owner_id=donor.id,
        name="Yesterday's bread",
        expires_in_hours=-1,
        status=FoodItemStatus.AVAILABLE,
    )

    with pytest.raises(FoodItemExpiredError) as exc_info:
        await donation_service.request_donation(
            DonationCreate(food_item_id=item.id),
            as_current_user(recipient),
        )

    # 409, not 400/422: the request was well formed, the world's state makes it
    # impossible. Asserting the status here documents that decision.
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "FOOD_ITEM_EXPIRED"


async def test_expired_food_rule_is_not_bypassed_by_the_donations_module(
    donation_service: DonationService,
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """No donation row is created when the reservation is refused.

    Proves the ordering in `request_donation`: reserve first, insert second. If the
    insert happened first, the row would exist in the session — visible here even
    before a commit.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id, expires_in_hours=-2)

    with pytest.raises(FoodItemExpiredError):
        await donation_service.request_donation(
            DonationCreate(food_item_id=item.id),
            as_current_user(recipient),
        )

    repo = DonationRepository(db_session)
    assert await repo.count() == 0


async def test_valid_food_can_be_donated_and_is_reserved(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """The happy path: a donation is created and the item flips to RESERVED.

    Both halves are asserted. Checking only the donation would miss the case where the
    request succeeds but the food stays AVAILABLE — allowing a second recipient to
    claim it.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id, expires_in_hours=24)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id, note="I can collect at 6pm"),
        as_current_user(recipient),
    )

    assert donation.status is DonationStatus.PENDING
    assert donation.recipient_id == recipient.id
    assert donation.note == "I can collect at 6pm"
    # The cross-module side effect.
    assert item.status is FoodItemStatus.RESERVED


# --------------------------------------------------------------------------
# Other request-time rules
# --------------------------------------------------------------------------
async def test_cannot_request_own_food(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """A donor cannot claim their own listing.

    Not just silly: it would let a donor take their food off the public list while
    keeping it, and would inflate the "food rescued" metric with self-transfers.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    with pytest.raises(CannotDonateToSelfError):
        await donation_service.request_donation(
            DonationCreate(food_item_id=item.id),
            as_current_user(donor),
        )


async def test_cannot_request_already_reserved_food(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """A second recipient gets 409 NOT_AVAILABLE once an item is reserved.

    The sequential version of the race that `SELECT ... FOR UPDATE` handles
    concurrently. NOTE: this test does not prove the *concurrent* case — SQLite
    ignores row locks. Run the suite against Postgres to exercise that; see
    docs/TESTING.md.
    """
    donor = await make_user()
    first = await make_user(role=Role.RECIPIENT)
    second = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(first)
    )

    with pytest.raises(FoodItemNotAvailableError):
        await donation_service.request_donation(
            DonationCreate(food_item_id=item.id), as_current_user(second)
        )


async def test_duplicate_request_from_same_recipient_is_rejected(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """The same recipient asking twice gets DUPLICATE, not NOT_AVAILABLE.

    A distinct error because the useful client message differs: "wait for a reply" vs
    "someone else got it". The duplicate check therefore runs *before* the
    reservation.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    with pytest.raises((DuplicateDonationRequestError, FoodItemNotAvailableError)) as exc_info:
        await donation_service.request_donation(
            DonationCreate(food_item_id=item.id), as_current_user(recipient)
        )
    # The duplicate check runs first, so this is the error we expect.
    assert isinstance(exc_info.value, DuplicateDonationRequestError)


# --------------------------------------------------------------------------
# Two-party authorization
# --------------------------------------------------------------------------
async def test_only_item_owner_can_accept(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """The recipient cannot accept their own request.

    The reason `require_ownership` is not enough for this module: a donation has two
    parties with different rights, and the "owner" of a donation row in the ordinary
    sense is its recipient — who must not be able to self-approve.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    with pytest.raises(NotDonationOwnerError):
        await donation_service.accept(donation.id, as_current_user(recipient))

    # The donor can.
    accepted = await donation_service.accept(donation.id, as_current_user(donor))
    assert accepted.status is DonationStatus.ACCEPTED


async def test_stranger_cannot_view_donation(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Only the two participants (or an admin) may read a donation.

    Donations are private, unlike food listings — they reveal who is receiving food.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    stranger = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    with pytest.raises(NotDonationOwnerError):
        await donation_service.get_by_id(donation.id, as_current_user(stranger))

    # Both participants can.
    assert await donation_service.get_by_id(donation.id, as_current_user(donor))
    assert await donation_service.get_by_id(donation.id, as_current_user(recipient))


async def test_admin_can_view_any_donation(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Admins override the participant check — and that override is logged."""
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    admin = await make_user(role=Role.ADMIN)
    item = await make_food_item(owner_id=donor.id)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    assert await donation_service.get_by_id(donation.id, as_current_user(admin))


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------
async def test_declining_releases_the_food_item(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Declining must return the food to AVAILABLE.

    The bug this prevents is real and easy to ship: cancel the donation, forget the
    item, and perfectly good food is stranded in RESERVED forever because a donor said
    no.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id, expires_in_hours=48)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )
    assert item.status is FoodItemStatus.RESERVED

    await donation_service.decline(
        donation.id, DonationDecline(reason="Promised elsewhere"), as_current_user(donor)
    )

    assert donation.status is DonationStatus.CANCELLED
    assert donation.decline_reason == "Promised elsewhere"
    assert item.status is FoodItemStatus.AVAILABLE  # released


async def test_declining_expired_item_releases_to_expired_not_available(
    donation_service: DonationService,
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Food that expired while reserved must not be re-listed as available.

    Otherwise the release path would quietly reintroduce the exact failure the whole
    invariant exists to prevent.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id, expires_in_hours=48)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    # Simulate time passing by moving the expiry into the past — cheaper and more
    # deterministic than manipulating the clock.
    from datetime import timedelta

    from app.shared.utils.datetime_utils import utcnow

    item.expires_at = utcnow() - timedelta(hours=1)
    await db_session.flush()

    await donation_service.decline(donation.id, DonationDecline(), as_current_user(donor))

    assert item.status is FoodItemStatus.EXPIRED


async def test_completed_donation_cannot_be_accepted_again(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """Terminal states are terminal.

    Enforced by `ALLOWED_TRANSITIONS` in models.py, checked once in `_transition`.
    Adding a state means editing that table; this rule needs no new code.
    """
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )
    await donation_service.accept(donation.id, as_current_user(donor))
    await donation_service.complete(donation.id, as_current_user(donor))

    assert donation.status is DonationStatus.COMPLETED
    assert item.status is FoodItemStatus.DONATED

    with pytest.raises(InvalidDonationTransitionError):
        await donation_service.accept(donation.id, as_current_user(donor))


async def test_recipient_can_cancel_and_food_is_released(
    donation_service: DonationService,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """The recipient's mirror of decline."""
    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id, expires_in_hours=48)

    donation = await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )
    await donation_service.cancel(donation.id, as_current_user(recipient))

    assert donation.status is DonationStatus.CANCELLED
    assert item.status is FoodItemStatus.AVAILABLE


async def test_notification_is_created_for_the_donor(
    donation_service: DonationService,
    db_session: Any,
    make_user: Any,
    make_food_item: Any,
    as_current_user: Any,
) -> None:
    """A request notifies the donor, in the same transaction.

    Asserts the *row*, not the email. The row is the durable part; delivery is
    best-effort and dispatched through `TaskDispatcher` (here, one that drops tasks).
    Testing the row tests the guarantee; testing the dropped task would test nothing.
    """
    from app.modules.notifications.models import NotificationType
    from app.modules.notifications.repository import NotificationRepository

    donor = await make_user()
    recipient = await make_user(role=Role.RECIPIENT)
    item = await make_food_item(owner_id=donor.id)

    await donation_service.request_donation(
        DonationCreate(food_item_id=item.id), as_current_user(recipient)
    )

    notifications = await NotificationRepository(db_session).list_for_user(donor.id)
    assert len(notifications) == 1
    assert notifications[0].type is NotificationType.DONATION_REQUESTED
    assert item.name in notifications[0].body
