"""Request/response DTOs for the users module.

THE READ/CREATE/UPDATE SPLIT IS NOT BOILERPLATE
-----------------------------------------------
It is the mechanism that makes mass assignment impossible. Consider a single
shared `UserSchema` used for both input and output:

* It must contain `role`, because responses show it. Now a client can
  `POST /auth/register {"role": "ADMIN"}` and self-promote.
* It must contain `id`, so a client can attempt to choose its own primary key.
* If it contains `hashed_password` for internal use, one careless
  `return user_schema` leaks the hash.

Three narrow schemas make each of those *structurally impossible* rather than
prevented by a validator someone has to remember. `UserCreate` has no `role`
field, so `extra="forbid"` (set on `ApiModel`) rejects the attempt with a 422
before any code runs. Security by construction beats security by vigilance.

`role` therefore appears in `UserRead` (output) and in `UserRoleUpdate`
(admin-only endpoint) — and nowhere a normal user can reach.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Final

from pydantic import EmailStr, Field, field_validator, model_validator

from app.modules.users.models import Role
from app.shared.schemas.base_schema import ApiModel, TimestampedSchema

# --------------------------------------------------------------------------
# Password policy
# --------------------------------------------------------------------------
# WHY 12 AND NOT 8: 8 characters is brute-forceable offline. Length is the single
# most effective factor, far more than character-class requirements.
#
# WHY 72 IS A HARD CEILING: bcrypt truncates at 72 *bytes*. Silently accepting
# more would mean a 100-character password is no stronger than its first 72
# characters — and two different passwords could share a hash. Rejecting is
# honest; see BCRYPT_MAX_PASSWORD_BYTES in core/security.py.
MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 72

# Deliberately modest: one lower, one upper, one digit. NIST SP 800-63B actually
# advises *against* elaborate composition rules — they push users toward
# "Password1!" and password reuse. Length plus a breach-list check is the modern
# recommendation. These three exist because most stakeholders still expect them;
# the length floor is doing the real work.
_HAS_LOWER = re.compile(r"[a-z]")
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_DIGIT = re.compile(r"\d")

PasswordStr = Annotated[
    str,
    Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description=(
            f"{MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters, "
            "with at least one lowercase letter, one uppercase letter and one digit"
        ),
        examples=["Str0ngPassphrase"],
    ),
]


def _validate_password_strength(value: str) -> str:
    """Shared password rule. One function, used by register and by change-password.

    Written once because a policy enforced in two places eventually becomes two
    policies — and the weaker one is the one that matters.
    """
    if not _HAS_LOWER.search(value):
        raise ValueError("must contain a lowercase letter")
    if not _HAS_UPPER.search(value):
        raise ValueError("must contain an uppercase letter")
    if not _HAS_DIGIT.search(value):
        raise ValueError("must contain a digit")
    if len(value.encode("utf-8")) > MAX_PASSWORD_LENGTH:
        # Length is checked in bytes as well as characters: 30 emoji are under
        # the 72-character limit but over the 72-byte one.
        raise ValueError(f"must not exceed {MAX_PASSWORD_LENGTH} bytes when UTF-8 encoded")
    return value


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class UserCreate(ApiModel):
    """Registration payload.

    NOTE WHAT IS MISSING: no `role`, no `is_active`, no `id`. A new account is
    always a `DONOR` (the service decides), always active, always
    server-assigned. Promotion happens through the admin-only role endpoint,
    which is a separate, separately-authorized code path.
    """

    email: EmailStr = Field(
        max_length=320,
        description="Normalised to lowercase before storage",
        examples=["alice@example.com"],
    )
    password: PasswordStr
    full_name: str = Field(min_length=1, max_length=255, examples=["Alice Donor"])

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Lowercase the address.

        Email domains are case-insensitive, and no realistic provider treats
        local parts as case-sensitive. Without normalisation, `Alice@x.com` and
        `alice@x.com` become two accounts that pass the unique constraint —
        confusing users and defeating the duplicate check.

        Normalising at the *edge* means every downstream lookup can compare
        directly; no `func.lower()` in queries, so the index stays usable.
        """
        return value.lower()

    @field_validator("password")
    @classmethod
    def check_password_strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    @model_validator(mode="after")
    def password_must_not_contain_email(self) -> UserCreate:
        """Reject passwords derived from the address.

        A cross-field rule, so it cannot live in a `field_validator`. Cheap, and
        it blocks the single most guessable pattern.
        """
        local_part = self.email.split("@")[0]
        if len(local_part) >= 4 and local_part in self.password.lower():
            raise ValueError("password must not contain your email address")
        return self


class UserUpdate(ApiModel):
    """Self-service profile edit. Every field optional — this is a PATCH.

    `full_name` only. Email changes need a verification flow (otherwise an
    attacker with a stolen token moves the account to their own address, and the
    real owner cannot recover it), and `role`/`is_active` are administrative.
    Each of those omissions is a deliberate decision, not an oversight.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def at_least_one_field(self) -> UserUpdate:
        """Reject an empty PATCH body.

        `PATCH {}` would otherwise report success while doing nothing — the kind
        of silent no-op that has clients retrying a broken integration for hours.
        """
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


class UserRoleUpdate(ApiModel):
    """Admin-only role change. Isolated in its own schema *and* its own endpoint.

    Splitting it from `UserUpdate` is what lets the router demand
    `require_roles(Role.ADMIN)` for privilege escalation while leaving ordinary
    profile edits open to the owner. One schema for both would force the
    authorization check down into the service, where it is easy to miss.
    """

    role: Role = Field(description="New role")


class PasswordChange(ApiModel):
    """Change password while authenticated.

    `current_password` is required even though the caller already holds a valid
    token. That is the point: it re-proves possession of the *password*, so a
    stolen access token alone cannot lock the real owner out of their account.
    """

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: PasswordStr

    @field_validator("new_password")
    @classmethod
    def check_password_strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    @model_validator(mode="after")
    def must_differ(self) -> PasswordChange:
        if self.current_password == self.new_password:
            raise ValueError("the new password must differ from the current one")
        return self


class LoginRequest(ApiModel):
    """Login payload.

    JSON rather than OAuth2's `application/x-www-form-urlencoded`: the rest of
    this API is JSON, and one content type for everything is simpler for clients
    and for us (KISS). The trade-off is that Swagger's built-in OAuth2 password
    flow will not drive this endpoint — you log in via `POST /auth/login`, then
    paste the token into "Authorize". Acceptable; documented so nobody is
    puzzled.

    No `min_length` on `password`: this is a *login*, and validating the length of
    a submitted password tells an attacker about the policy while rejecting
    legacy passwords that predate it. Verification either succeeds or it does not.
    """

    email: EmailStr
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.lower()


class RefreshRequest(ApiModel):
    """Exchange a refresh token for a new pair."""

    refresh_token: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class UserRead(TimestampedSchema):
    """Public view of a user.

    THE ALLOWLIST. `hashed_password` is absent, so it cannot be serialised —
    not by this endpoint, not by a future one that reuses this schema, and not
    after someone adds a column to the model. Compare the failure mode of a
    denylist (`exclude={"hashed_password"}`): it protects only the places where
    somebody remembered to write it.
    """

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool


class TokenPair(ApiModel):
    """The login/refresh response.

    `expires_in` is included so a client can schedule a refresh *before* the
    token dies, instead of discovering expiry by getting a 401 on a user action.
    Seconds, per OAuth 2.0 convention.

    The refresh token is returned in the body rather than set as an HttpOnly
    cookie. For a browser SPA the cookie is the stronger choice (JavaScript
    cannot read it, so XSS cannot steal it) at the cost of needing CSRF
    protection. Body delivery is right for the mobile and service clients this
    API targets, and keeps the auth flow inspectable in Swagger. Revisit if a
    browser SPA becomes the primary consumer — see docs/SECURITY.md.
    """

    access_token: str = Field(description="Short-lived; send as `Authorization: Bearer <token>`")
    refresh_token: str = Field(description="Single-use — it is rotated on every refresh")
    token_type: str = Field(default="bearer", description="Always 'bearer'")
    expires_in: int = Field(description="Access-token lifetime in seconds")


class RegisterResponse(ApiModel):
    """Registration returns the user *and* a token pair, so the client is signed
    in immediately. Saves a round-trip and the awkward state where registration
    succeeded but the follow-up login failed."""

    user: UserRead
    tokens: TokenPair
