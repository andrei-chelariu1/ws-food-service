"""Domain errors for the food items module.

Read this file as the specification of every way a food-item operation can be
refused. Each class fixes its own HTTP status and stable code once, so no endpoint
gets to make that decision (and no two endpoints can make it differently).
"""

from __future__ import annotations

from app.core.exceptions import BusinessRuleViolation, NotFoundError, ValidationError


class FoodItemNotFoundError(NotFoundError):
    """404."""

    def __init__(self, identifier: object = None) -> None:
        super().__init__("Food item", identifier)
        self.code = "FOOD_ITEM_NOT_FOUND"


class FoodItemExpiredError(BusinessRuleViolation):
    """409 — THE central business rule of this application.

    Expired food must never be donated. Enforced in one place
    (`FoodItemService.reserve`), which is the only route to reservation, so the
    donations module cannot bypass it even by accident.

    409 rather than 400 or 422: the request was perfectly well formed. The state of
    the world makes it impossible, and no amount of editing the payload will help.
    That distinction is what tells a client "do not retry this".
    """

    code = "FOOD_ITEM_EXPIRED"

    def __init__(self, item_name: str | None = None) -> None:
        super().__init__(
            f"'{item_name}' has expired and can no longer be donated"
            if item_name
            else "This food item has expired and can no longer be donated"
        )


class FoodItemNotAvailableError(BusinessRuleViolation):
    """409 when the item is not in a donatable state.

    Distinct from `FoodItemExpiredError` because the causes are different and a
    client should say different things: "someone already requested this" is not
    "this food has gone off".
    """

    code = "FOOD_ITEM_NOT_AVAILABLE"

    def __init__(self, current_status: str) -> None:
        super().__init__(f"Food item is {current_status} and cannot be requested")


class InvalidStatusTransitionError(BusinessRuleViolation):
    """409 for an illegal move in the state machine.

    The message names both states, because unlike the security-sensitive errors in
    the users module there is nothing to leak here — and a client that tried
    `DONATED -> AVAILABLE` genuinely needs to be told why it failed.
    """

    code = "INVALID_STATUS_TRANSITION"

    def __init__(self, current: str, requested: str) -> None:
        super().__init__(f"Cannot change status from {current} to {requested}")


class FoodItemImmutableError(BusinessRuleViolation):
    """409 when editing an item that is no longer editable.

    Once an item is RESERVED, a recipient has acted on the details as listed.
    Letting the owner then change the quantity or the pickup location would
    invalidate a decision someone else already made.
    """

    code = "FOOD_ITEM_IMMUTABLE"

    def __init__(self, current_status: str) -> None:
        super().__init__(f"Food item cannot be edited while it is {current_status}")


class InvalidExpiryDateError(ValidationError):
    """422 for an expiry date in the past.

    A `ValidationError`, not a `BusinessRuleViolation`: this one *is* the client's
    fault and *is* fixable by editing the payload. The status code is the
    difference between "retry with a correction" and "give up".
    """

    code = "INVALID_EXPIRY_DATE"

    def __init__(self) -> None:
        super().__init__("Expiry date must be in the future")
