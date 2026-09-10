"""Business logic for donations — and the cross-module boundary in action.

THE MOST IMPORTANT THING IN THIS FILE IS ITS CONSTRUCTOR
--------------------------------------------------------
    def __init__(self, repository, food_item_service, notification_service)
                              ▲                ▲
                              │                └── another module's SERVICE
                              └── this module's own repository

`food_item_service`, never `FoodItemRepository`. That single choice is what the
cross-module rule in ARCHITECTURE.md is for, and here is what it actually buys:

* **The expiry invariant cannot be bypassed.** To reserve an item this service must
  call `FoodItemService.reserve()`, which checks `expires_at` and the state
  machine. With a repository it could write `status = RESERVED` directly and
  quietly skip both. The rule is not stylistic — it is why "expired food cannot be
  donated" is *guaranteed* rather than *usually true*.
* **Rules stay where they are owned.** The food-item state machine lives in the
  food_items module. If a rule changes there, this module inherits the change for
  free. Two modules writing the same table is how invariants rot.
* **The seam is visible.** `grep -rn "food_item_service" app/modules/donations/`
  lists every cross-module interaction. Extracting food_items into its own service
  later means replacing one injected object with an HTTP client — and nothing else.

Note that `repository.py` in this module *does* import `FoodItem` (the model) in
order to JOIN. Sharing a schema is what one database means; sharing *behaviour* is
what creates coupling. The rule is about behaviour.

TRANSACTIONALITY
----------------
`accept()` writes two tables (donation status, food item status) and enqueues a
notification. All of it shares one transaction, because the commit boundary is the
request (shared/db/session.py). If the food-item transition raises, the donation
status change is rolled back too — there is no path to "accepted donation, still
AVAILABLE item". Nobody had to write that rollback.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.logging import get_logger
from app.modules.donations.exceptions import (
    CannotDonateToSelfError,
    DonationNotFoundError,
    DuplicateDonationRequestError,
    InvalidDonationTransitionError,
    NotDonationOwnerError,
)
from app.modules.donations.models import Donation, DonationStatus
from app.modules.donations.repository import DonationRepository
from app.modules.donations.schemas import DonationCreate, DonationDecline
from app.modules.food_items.service import FoodItemService
from app.modules.notifications.service import NotificationService
from app.shared.security.permissions import is_admin
from app.shared.security.principal import CurrentUser

log = get_logger(__name__)


class DonationService:
    """Donation use cases: request, accept, decline, complete, cancel."""

    def __init__(
        self,
        repository: DonationRepository,
        food_item_service: FoodItemService,
        notification_service: NotificationService,
    ) -> None:
        self._repo = repository
        # Another module's service — the boundary discussed in the module docstring.
        self._food_items = food_item_service
        self._notifications = notification_service

    # -- Request -----------------------------------------------------------

    async def request_donation(
        self,
        payload: DonationCreate,
        recipient: CurrentUser,
    ) -> Donation:
        """Ask for a food item.

        Order of operations matters, and each step is here for a reason:

        1. **Load the item** (via the food_items service) — needed for the
           self-donation check, and it 404s cleanly if the id is bogus.
        2. **Reject self-donation** — before the expensive locking read.
        3. **Reject a duplicate open request** — a fast indexed check that gives a
           precise error, rather than letting the user create a second identical
           request.
        4. **Reserve the item** — `FoodItemService.reserve()`. This is where the
           expiry rule and the state machine are enforced, under a row lock. It
           raises `FoodItemExpiredError` (409) or `FoodItemNotAvailableError` (409).
        5. **Create the donation row** — only after the reservation succeeded.

        Step 5 last is the important one. If the row were inserted first and the
        reservation then failed, the transaction would roll it back — correct, but
        it would also have taken a write lock on `donations` for no reason. Doing
        the work that can fail *before* the work that must persist keeps the
        failing path cheap.
        """
        item = await self._food_items.get_by_id(payload.food_item_id)

        if item.owner_id == recipient.id:
            log.info("self_donation_rejected", user_id=str(recipient.id))
            raise CannotDonateToSelfError

        if await self._repo.has_open_request(recipient.id, payload.food_item_id):
            raise DuplicateDonationRequestError

        # THE INVARIANT CHECK. Delegated, not reimplemented. This call is the only
        # way this module can reserve an item, so the rule cannot be skipped here.
        await self._food_items.reserve(payload.food_item_id)

        donation = Donation(
            food_item_id=payload.food_item_id,
            recipient_id=recipient.id,
            status=DonationStatus.PENDING,
            note=payload.note,
        )
        await self._repo.add(donation)

        # Tell the donor someone wants their food. Enqueued, not sent inline — see
        # NotificationService for why the request does not wait on delivery.
        await self._notifications.notify_donation_requested(
            donor_id=item.owner_id,
            recipient_name=recipient.email,
            food_item_name=item.name,
            donation_id=donation.id,
        )

        log.info(
            "donation_requested",
            donation_id=str(donation.id),
            food_item_id=str(payload.food_item_id),
            recipient_id=str(recipient.id),
        )
        return donation

    # -- Owner actions -----------------------------------------------------

    async def accept(self, donation_id: uuid.UUID, actor: CurrentUser) -> Donation:
        """The donor agrees. PENDING -> ACCEPTED.

        Only the *item owner* may accept. Note this is not `require_ownership` on
        the donation: the donation's "owner" in the ordinary sense is its
        recipient, and they must not be able to accept their own request. The
        two-party model is why this module has its own `_assert_is_item_owner`.
        """
        donation = await self._get_with_item_or_raise(donation_id)
        await self._assert_is_item_owner(donation, actor, action="accept this donation")

        updated = await self._transition(donation, DonationStatus.ACCEPTED)

        await self._notifications.notify_donation_accepted(
            recipient_id=donation.recipient_id,
            food_item_name=donation.food_item.name,
            pickup_location=donation.food_item.pickup_location,
            donation_id=donation.id,
        )

        log.info("donation_accepted", donation_id=str(donation_id), actor_id=str(actor.id))
        return updated

    async def decline(
        self,
        donation_id: uuid.UUID,
        payload: DonationDecline,
        actor: CurrentUser,
    ) -> Donation:
        """The donor refuses. PENDING/ACCEPTED -> CANCELLED, and the item is released.

        RELEASING THE ITEM IS THE POINT. Without it the food stays RESERVED
        forever — perfectly good food that no one else can claim, because a donor
        said no. Two writes, one transaction: either both happen or neither does.

        `release()` is again the food_items service, which decides whether the item
        returns to AVAILABLE or goes to EXPIRED (it may have gone off while
        reserved). This module does not need to know that rule, and deliberately
        does not.
        """
        donation = await self._get_with_item_or_raise(donation_id)
        await self._assert_is_item_owner(donation, actor, action="decline this donation")

        updated = await self._transition(
            donation,
            DonationStatus.CANCELLED,
            decline_reason=payload.reason,
        )
        await self._food_items.release(donation.food_item_id)

        await self._notifications.notify_donation_declined(
            recipient_id=donation.recipient_id,
            food_item_name=donation.food_item.name,
            reason=payload.reason,
            donation_id=donation.id,
        )

        log.info("donation_declined", donation_id=str(donation_id), actor_id=str(actor.id))
        return updated

    async def complete(self, donation_id: uuid.UUID, actor: CurrentUser) -> Donation:
        """The handover happened. ACCEPTED -> COMPLETED, item -> DONATED.

        Confirmed by the donor, not the recipient: the donor is the one who knows
        the food physically left. Both statuses advance to terminal states in one
        transaction, so the pair can never disagree.
        """
        donation = await self._get_with_item_or_raise(donation_id)
        await self._assert_is_item_owner(donation, actor, action="complete this donation")

        updated = await self._transition(donation, DonationStatus.COMPLETED)
        await self._food_items.mark_donated(donation.food_item_id)

        await self._notifications.notify_donation_completed(
            recipient_id=donation.recipient_id,
            food_item_name=donation.food_item.name,
            donation_id=donation.id,
        )

        log.info("donation_completed", donation_id=str(donation_id))
        return updated

    # -- Recipient actions -------------------------------------------------

    async def cancel(self, donation_id: uuid.UUID, actor: CurrentUser) -> Donation:
        """The recipient withdraws. Releases the item, notifies the donor.

        The mirror image of `decline`: same state change, opposite party, opposite
        notification. Kept as a separate method rather than a shared one with a
        `by_recipient` flag — the authorization differs, the notification differs,
        and a boolean parameter that switches both is how two behaviours get
        entangled in one function.
        """
        donation = await self._get_with_item_or_raise(donation_id)

        if donation.recipient_id != actor.id and not is_admin(actor):
            raise NotDonationOwnerError("cancel this donation")

        updated = await self._transition(donation, DonationStatus.CANCELLED)
        await self._food_items.release(donation.food_item_id)

        await self._notifications.notify_donation_cancelled(
            donor_id=donation.food_item.owner_id,
            food_item_name=donation.food_item.name,
            donation_id=donation.id,
        )

        log.info("donation_cancelled_by_recipient", donation_id=str(donation_id))
        return updated

    # -- Queries -----------------------------------------------------------

    async def get_by_id(self, donation_id: uuid.UUID, actor: CurrentUser) -> Donation:
        """Fetch a donation, visible only to its two parties (or an admin).

        Unlike a food listing, a donation is private: it reveals who is receiving
        food, which is sensitive. So this is authorized, while
        `GET /food-items/{id}` is public — a deliberate difference, not an
        inconsistency.
        """
        donation = await self._get_with_item_or_raise(donation_id)

        is_participant = actor.id in (donation.recipient_id, donation.food_item.owner_id)
        if not is_participant and not is_admin(actor):
            raise NotDonationOwnerError("view this donation")
        return donation

    async def list_my_requests(
        self,
        recipient: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: DonationStatus | None = None,
    ) -> tuple[Sequence[Donation], int]:
        """Requests I made, with items eagerly loaded (two queries, not N+1)."""
        items = await self._repo.list_for_recipient_with_items(
            recipient.id, offset=offset, limit=limit, status=status
        )
        total = await self._repo.count_for_recipient(recipient.id, status=status)
        return items, total

    async def list_requests_for_my_food(
        self,
        donor: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: DonationStatus | None = None,
    ) -> tuple[Sequence[Donation], int]:
        """The donor's inbox: requests for food I listed."""
        items = await self._repo.list_for_donor(donor.id, offset=offset, limit=limit, status=status)
        total = await self._repo.count_for_donor(donor.id, status=status)
        return items, total

    # -- Internals ---------------------------------------------------------

    async def _get_with_item_or_raise(self, donation_id: uuid.UUID) -> Donation:
        """Always loads the food item eagerly.

        Every caller needs it — for the owner check, for the notification text, or
        for the response — so loading it unconditionally is simpler than four
        `if needs_item` branches, and it is what makes `donation.food_item.name`
        safe despite `lazy="raise"`.
        """
        donation = await self._repo.get_with_item(donation_id)
        if donation is None:
            raise DonationNotFoundError(donation_id)
        return donation

    async def _assert_is_item_owner(
        self,
        donation: Donation,
        actor: CurrentUser,
        *,
        action: str,
    ) -> None:
        """Only the food's owner (or an admin) may take donor-side actions."""
        if donation.food_item.owner_id == actor.id:
            return
        if is_admin(actor):
            log.info(
                "admin_override_donation_action",
                admin_id=str(actor.id),
                donation_id=str(donation.id),
                action=action,
            )
            return
        log.warning(
            "donation_permission_denied",
            actor_id=str(actor.id),
            donation_id=str(donation.id),
            action=action,
        )
        raise NotDonationOwnerError(action)

    async def _transition(
        self,
        donation: Donation,
        new_status: DonationStatus,
        **extra: object,
    ) -> Donation:
        """The single gate for every donation status change.

        Same shape as `FoodItemService._transition`, and the repetition is
        deliberate: each module owns its own state machine. Extracting a shared
        `StateMachineMixin` would couple two lifecycles that have no reason to
        change together, and would need a generic transition table anyway. Four
        lines duplicated once is cheaper than the wrong abstraction (see
        docs/SOLID.md on where DRY stops applying).
        """
        if not donation.can_transition_to(new_status):
            raise InvalidDonationTransitionError(str(donation.status), str(new_status))
        return await self._repo.update(donation, status=new_status, **extra)
