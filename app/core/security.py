"""Security primitives: password hashing and JWT encode/decode.

This module is deliberately *dumb*. It knows how to hash a string and how to
sign a dict. It does not know what a `User` is, it does not touch the database,
and it does not decide who may do what. That belongs to
`app/modules/users/service.py` and `app/shared/security/`.

TWO DESIGN CHOICES WORTH READING
--------------------------------

1. `PasswordHasher` is a **Protocol**, not a concrete class (Dependency
   Inversion). `UserService` depends on the Protocol, so:
     * tests inject a trivial `FakePasswordHasher` and run in milliseconds
       instead of paying 250ms per bcrypt hash;
     * migrating bcrypt -> argon2 is a new 20-line adapter, with zero changes
       to any service.
   `needs_rehash()` exists so the cost factor can be raised later and existing
   users are upgraded transparently on their next successful login.

2. Every token carries `typ` ("access" | "refresh") and `jti` (a unique id).
     * `typ` means an access token can never be replayed at `/auth/refresh`,
       and a refresh token can never authenticate a normal request. Without it,
       a stolen long-lived refresh token is a stolen access token.
     * `jti` is what makes revocation possible at all — logout and refresh
       rotation add the jti to a Redis denylist.

See docs/adr/0001-pyjwt-over-python-jose.md and 0002-bcrypt-direct-over-passlib.md
for why the libraries differ from ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

import bcrypt
import jwt
from jwt.exceptions import InvalidTokenError

from app.core.config import Settings

# `shared/utils/datetime_utils` imports only the standard library, so this does not
# create a core -> shared cycle. Using it here keeps the "one source of now" rule
# true for the whole codebase rather than almost all of it — token `iat`/`exp` are
# exactly the values a test needs to be able to control.
from app.shared.utils.datetime_utils import utcnow

# bcrypt silently truncates input at 72 bytes: a 100-char password would share
# a hash with its first 72 chars. We reject instead of truncating, so the
# behaviour is explicit rather than a surprising security footgun.
BCRYPT_MAX_PASSWORD_BYTES: Final = 72


class TokenType(StrEnum):
    """The `typ` claim. A StrEnum so it serialises to JSON as a plain string."""

    ACCESS = "access"
    REFRESH = "refresh"


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------


@runtime_checkable
class PasswordHasher(Protocol):
    """The contract services depend on (never a concrete implementation)."""

    def hash(self, plain_password: str) -> str: ...

    def verify(self, plain_password: str, hashed_password: str) -> bool: ...

    def needs_rehash(self, hashed_password: str) -> bool: ...


class PasswordTooLongError(ValueError):
    """Raised instead of silently truncating past bcrypt's 72-byte limit."""


@dataclass(frozen=True, slots=True)
class BcryptPasswordHasher:
    """bcrypt implementation of `PasswordHasher`.

    `rounds` is the cost factor: each +1 doubles the work. 12 is the current
    production floor; tests use 4.
    """

    rounds: int = 12

    def hash(self, plain_password: str) -> str:
        encoded = self._encode(plain_password)
        return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=self.rounds)).decode()

    def verify(self, plain_password: str, hashed_password: str) -> bool:
        try:
            encoded = self._encode(plain_password)
        except PasswordTooLongError:
            # An over-long candidate simply cannot match a stored hash.
            return False
        try:
            # checkpw is constant-time, so this does not leak via timing.
            return bcrypt.checkpw(encoded, hashed_password.encode())
        except ValueError:
            # Malformed/corrupt hash in the database — not a match, not a crash.
            return False

    def needs_rehash(self, hashed_password: str) -> bool:
        """True when the stored hash used a weaker cost than we now require.

        Hash format: `$2b$12$<salt+digest>` — field 2 is the cost.
        """
        try:
            cost = int(hashed_password.split("$")[2])
        except (IndexError, ValueError):
            return True  # unparseable => rehash it
        return cost < self.rounds

    @staticmethod
    def _encode(plain_password: str) -> bytes:
        encoded = plain_password.encode("utf-8")
        if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
            raise PasswordTooLongError(
                f"Password exceeds bcrypt's {BCRYPT_MAX_PASSWORD_BYTES}-byte limit"
            )
        return encoded


def get_password_hasher(settings: Settings) -> PasswordHasher:
    """Factory used by the DI providers in each module's `api.py`."""
    return BcryptPasswordHasher(rounds=settings.BCRYPT_ROUNDS)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """A decoded, signature-verified token.

    A typed object rather than a raw dict, so `payload.subject` is checked by
    mypy and a typo in a claim name fails at type-check time, not at runtime.
    """

    subject: str
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime
    role: str | None = None

    @property
    def user_id(self) -> uuid.UUID:
        return uuid.UUID(self.subject)


class TokenError(Exception):
    """Base for token problems. Translated to HTTP 401 in shared/security."""


class TokenExpiredError(TokenError):
    pass


class TokenInvalidError(TokenError):
    pass


class TokenTypeMismatchError(TokenError):
    """A refresh token was presented where an access token was required, or vice versa."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted token plus the metadata callers need.

    `jti` and `expires_at` are returned because the caller must be able to
    denylist the token later, and the denylist TTL should match the token's own
    remaining lifetime — no point storing a revoked jti past its expiry.
    """

    token: str
    jti: str
    expires_at: datetime


class JwtTokenService:
    """Encodes and decodes JWTs. Stateless — revocation lives in the users module.

    A class rather than free functions purely so `settings` is bound once at
    construction instead of being passed to every call.
    """

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.SECRET_KEY.get_secret_value()
        self._algorithm = settings.JWT_ALGORITHM
        self._access_ttl = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        self._refresh_ttl = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        self._issuer = settings.APP_NAME

    def create_access_token(self, subject: str, role: str | None = None) -> IssuedToken:
        return self._create(subject, TokenType.ACCESS, self._access_ttl, role=role)

    def create_refresh_token(self, subject: str) -> IssuedToken:
        # No role claim on refresh tokens: roles can change, and a refresh token
        # lives for days. The role is re-read from the database on every refresh.
        return self._create(subject, TokenType.REFRESH, self._refresh_ttl)

    def decode(self, token: str, expected_type: TokenType) -> TokenPayload:
        """Verify signature, expiry and token type.

        Raises `TokenExpiredError`, `TokenTypeMismatchError` or
        `TokenInvalidError` — never returns `None`, so callers cannot forget to
        check.
        """
        try:
            raw: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                # Explicitly require the claims we rely on. PyJWT will not
                # verify a claim that is simply absent unless told to.
                options={"require": ["exp", "iat", "sub", "jti", "typ"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("Token has expired") from exc
        except InvalidTokenError as exc:
            # Covers bad signature, wrong issuer, malformed token, missing claims.
            raise TokenInvalidError("Token is invalid") from exc

        actual_type = raw.get("typ")
        if actual_type != expected_type.value:
            raise TokenTypeMismatchError(
                f"Expected a {expected_type.value} token, got {actual_type!r}"
            )

        return TokenPayload(
            subject=str(raw["sub"]),
            token_type=TokenType(actual_type),
            jti=str(raw["jti"]),
            issued_at=datetime.fromtimestamp(raw["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(raw["exp"], tz=UTC),
            role=raw.get("role"),
        )

    def _create(
        self,
        subject: str,
        token_type: TokenType,
        ttl: timedelta,
        role: str | None = None,
    ) -> IssuedToken:
        now = utcnow()
        expires_at = now + ttl
        jti = uuid.uuid4().hex

        claims: dict[str, Any] = {
            "sub": subject,
            "typ": token_type.value,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
        }
        if role is not None:
            claims["role"] = role

        return IssuedToken(
            token=jwt.encode(claims, self._secret, algorithm=self._algorithm),
            jti=jti,
            expires_at=expires_at,
        )


def get_token_service(settings: Settings) -> JwtTokenService:
    return JwtTokenService(settings)
