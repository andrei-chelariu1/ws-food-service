"""Domain errors for the donations module."""

from __future__ import annotations

from app.core.exceptions import BusinessRuleViolation, NotFoundError, PermissionDeniedError


class DonationNotFoundError(NotFoundError):
    """404."""

    def __init__(self, identifier: object = None) -> None:
        super().__init__("Donation", identifier)
        self.code = "DONATION_NOT_FOUND"


class CannotDonateToSelfError(BusinessRuleViolation):
    """409 when a user requests their own listing.

    Not merely silly: without this check a donor could reserve their own food to
    take it off the public list while keeping it, and the "food rescued" metrics
    would count self-transfers. A one-line rule that protects both the marketplace
    and the reporting.
    """

    code = "CANNOT_DONATE_TO_SELF"

    def __init__(self) -> None:
        super().__init__("You cannot request your own food listing")


class DuplicateDonationRequestError(BusinessRuleViolation):
    """409 when the recipient already has an open request for this item.

    Distinct from `FoodItemNotAvailableError`: "you already asked for this" is
    actionable ("wait for a reply"), whereas "someone else claimed it" is not.
    Collapsing the two would make the client's message wrong half the time.
    """

    code = "DUPLICATE_DONATION_REQUEST"

    def __init__(self) -> None:
        super().__init__("You already have an open request for this item")


class InvalidDonationTransitionError(BusinessRuleViolation):
    """409 for an illegal move in the donation state machine."""

    code = "INVALID_DONATION_TRANSITION"

    def __init__(self, current: str, requested: str) -> None:
        super().__init__(f"Cannot change donation status from {current} to {requested}")


class NotDonationOwnerError(PermissionDeniedError):
    """403 when someone who is neither the donor nor the recipient interferes.

    A donation has *two* legitimate parties with *different* rights: the owner
    accepts or declines, the recipient cancels. Ordinary `require_ownership` models
    a single owner, so this module needs its own rule — and having it as a named
    error makes the two-party model explicit rather than implied by scattered
    conditionals.
    """

    code = "NOT_DONATION_PARTICIPANT"

    def __init__(self, action: str = "modify this donation") -> None:
        super().__init__(f"You do not have permission to {action}")
