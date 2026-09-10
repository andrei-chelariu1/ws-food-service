"""Domain errors raised by the users module.

WHY MODULE-LOCAL EXCEPTION CLASSES INSTEAD OF `raise ConflictError("...")`
-------------------------------------------------------------------------
Three payoffs, all of which come from giving a failure a *name*:

1. **Tests assert on intent, not on prose.**
       with pytest.raises(EmailAlreadyRegisteredError):
   survives someone rewording the message. `match="already registered"` does not.

2. **The HTTP mapping is declared once.** `EmailAlreadyRegisteredError` is a 409
   with code `EMAIL_ALREADY_REGISTERED`, decided here, in one place. No endpoint
   chooses a status code, so two endpoints cannot disagree about the same
   failure.

3. **This file is a readable specification.** Skim it and you know every way the
   users module can refuse a request. That is documentation that cannot go stale,
   because it *is* the code.

Open/Closed in action: adding an error means adding a class. The handlers in
`core/exceptions.py` never change, because they dispatch on the `AppError` base.
"""

from __future__ import annotations

from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


class UserNotFoundError(NotFoundError):
    """404. Note the message never distinguishes "no such id" from "not yours" —
    that distinction is itself an information leak (see permissions.py)."""

    def __init__(self, identifier: object = None) -> None:
        super().__init__("User", identifier)
        self.code = "USER_NOT_FOUND"


class EmailAlreadyRegisteredError(ConflictError):
    """409 on duplicate signup.

    SECURITY TRADE-OFF, MADE DELIBERATELY:
    This response confirms that an address has an account — a user-enumeration
    oracle. The strict alternative is to always return 201 and send an email
    ("you already have an account") instead. We chose the explicit error because
    the usability cost of silent failure is high and the information gained by an
    attacker is low for this application. The rate limit on `/auth/register`
    (3/hour) is what stops that oracle being used at scale.

    For a system where account existence is genuinely sensitive — a medical or
    legal service — invert this decision. It is a product judgement, not a
    technical one, which is exactly why it is written down here.
    """

    code = "EMAIL_ALREADY_REGISTERED"

    def __init__(self, email: str | None = None) -> None:
        super().__init__(
            f"An account with email {email} already exists"
            if email
            else "An account with this email already exists"
        )


class InvalidCredentialsError(AuthenticationError):
    """401 for a failed login.

    THE MESSAGE IS INTENTIONALLY VAGUE. "Incorrect email or password" — never
    "no such user" and never "wrong password". Distinguishing the two turns the
    login endpoint into a user-enumeration oracle that needs no successful login
    at all.

    `UserService.authenticate` also runs a dummy hash comparison when the email
    does not exist, so the *timing* does not leak what the message withholds. A
    vague message with a 200ms-vs-2ms timing difference is not vague.
    """

    code = ErrorCode.AUTHENTICATION_FAILED

    def __init__(self) -> None:
        super().__init__("Incorrect email or password")


class InactiveUserError(PermissionDeniedError):
    """403 — real credentials, disabled account. Not 401: re-authenticating
    cannot help, so telling the client to retry would send it round a loop."""

    code = "USER_INACTIVE"

    def __init__(self) -> None:
        super().__init__("This account has been deactivated")


class InvalidTokenError(AuthenticationError):
    """401 for a malformed, expired, wrong-type, or revoked token.

    One class for all four cases on purpose: an attacker probing tokens learns
    nothing about *why* one failed. The specific reason is logged server-side,
    where it is useful and harmless.
    """

    code = "INVALID_TOKEN"

    def __init__(self, reason: str = "Invalid or expired token") -> None:
        super().__init__(reason)


class TokenRevokedError(AuthenticationError):
    """401 when a token's `jti` is on the denylist.

    Distinct from `InvalidTokenError` because a revoked token being *replayed* is
    a materially different signal — it means either a normal post-logout request,
    or someone using a stolen refresh token after the legitimate holder already
    rotated it. Worth its own code so it can be alerted on.
    """

    code = "TOKEN_REVOKED"

    def __init__(self) -> None:
        super().__init__("This token has been revoked")


class WeakPasswordError(ValidationError):
    """422 when a password fails the policy.

    Lives here rather than as a Pydantic validator so the *reason* can be
    specific ("must contain a digit") while remaining a domain rule that a CLI
    user-creation command would also enforce.
    """

    code = "WEAK_PASSWORD"

    def __init__(self, reason: str) -> None:
        super().__init__(f"Password does not meet requirements: {reason}")


class SamePasswordError(ValidationError):
    """422 when a password change reuses the current password.

    Not merely pedantic: a user who believes they rotated a credential after a
    suspected compromise, but did not, is worse off than one who knows they
    didn't.
    """

    code = "SAME_PASSWORD"

    def __init__(self) -> None:
        super().__init__("The new password must differ from the current one")
