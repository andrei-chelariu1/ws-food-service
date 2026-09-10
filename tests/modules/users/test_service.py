"""Tests for `UserService`: registration, login, token lifecycle, passwords."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.security import JwtTokenService, TokenType
from app.modules.users.exceptions import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidTokenError,
    SamePasswordError,
    TokenRevokedError,
)
from app.modules.users.models import Role
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import LoginRequest, PasswordChange, UserCreate
from app.modules.users.service import RedisTokenDenylist, UserService

VALID_PASSWORD = "Str0ngPassphrase"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
async def test_register_creates_donor_and_hashes_password(
    user_service: UserService,
    db_session: Any,
) -> None:
    """A new account is a DONOR with a hashed password.

    Two assertions worth their space:
      * `role is Role.DONOR` — the role is assigned by the service, not taken from the
        request. `UserCreate` has no `role` field at all.
      * the stored value is neither the plaintext nor anything resembling it. The
        bcrypt prefix check is the cheap way to assert "this went through a real KDF".
    """
    user, tokens = await user_service.register(
        UserCreate(email="alice@example.com", password=VALID_PASSWORD, full_name="Alice")
    )

    assert user.role is Role.DONOR
    assert user.is_active is True
    assert user.hashed_password != VALID_PASSWORD
    assert user.hashed_password.startswith("$2b$")
    assert tokens.access_token and tokens.refresh_token


async def test_register_normalises_email_to_lowercase(user_service: UserService) -> None:
    """`Alice@Example.COM` and `alice@example.com` must be one account.

    Without normalisation both pass the unique constraint, the user cannot log in with
    the casing they remember, and the duplicate check is defeated. Normalising at the
    schema edge also keeps `ix_users_email` usable — a `lower()` in the query would
    not.
    """
    user, _ = await user_service.register(
        UserCreate(email="Alice@Example.COM", password=VALID_PASSWORD, full_name="Alice")
    )
    assert user.email == "alice@example.com"


async def test_register_rejects_duplicate_email(user_service: UserService) -> None:
    """The second signup for an address is a 409."""
    payload = UserCreate(email="bob@example.com", password=VALID_PASSWORD, full_name="Bob")
    await user_service.register(payload)

    with pytest.raises(EmailAlreadyRegisteredError) as exc_info:
        await user_service.register(payload)
    assert exc_info.value.status_code == 409


async def test_register_rejects_duplicate_email_case_insensitively(
    user_service: UserService,
) -> None:
    """Normalisation and the duplicate check work together, not separately."""
    await user_service.register(
        UserCreate(email="carol@example.com", password=VALID_PASSWORD, full_name="Carol")
    )
    with pytest.raises(EmailAlreadyRegisteredError):
        await user_service.register(
            UserCreate(email="CAROL@EXAMPLE.COM", password=VALID_PASSWORD, full_name="Carol")
        )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
async def test_login_with_correct_credentials(
    user_service: UserService,
    make_user: Any,
) -> None:
    user = await make_user(email="dave@example.com", password=VALID_PASSWORD)

    authenticated, tokens = await user_service.authenticate(
        LoginRequest(email="dave@example.com", password=VALID_PASSWORD)
    )

    assert authenticated.id == user.id
    assert tokens.token_type == "bearer"
    assert tokens.expires_in > 0


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("nobody@example.com", VALID_PASSWORD),  # unknown address
        ("erin@example.com", "WrongPassw0rd"),  # wrong password
    ],
)
async def test_login_failures_are_indistinguishable(
    user_service: UserService,
    make_user: Any,
    email: str,
    password: str,
) -> None:
    """An unknown email and a wrong password produce the identical error.

    THIS IS A SECURITY TEST, not a tidiness one. Distinguishing the two turns the
    login endpoint into a user-enumeration oracle: an attacker learns which addresses
    have accounts without ever logging in.

    The parametrisation is the point — both cases must land on the same class *and*
    the same message.
    """
    await make_user(email="erin@example.com", password=VALID_PASSWORD)

    with pytest.raises(InvalidCredentialsError) as exc_info:
        await user_service.authenticate(LoginRequest(email=email, password=password))

    assert exc_info.value.message == "Incorrect email or password"
    assert exc_info.value.status_code == 401


async def test_login_by_inactive_user_gives_the_same_error(
    user_service: UserService,
    make_user: Any,
) -> None:
    """A deactivated account also gets the generic credentials error.

    Saying "your account is disabled" would confirm the address exists. The distinct
    reason is logged server-side, where it is useful and harmless.
    """
    await make_user(email="frank@example.com", password=VALID_PASSWORD, is_active=False)

    with pytest.raises(InvalidCredentialsError):
        await user_service.authenticate(
            LoginRequest(email="frank@example.com", password=VALID_PASSWORD)
        )


# --------------------------------------------------------------------------
# Access tokens
# --------------------------------------------------------------------------
async def test_access_token_authenticates_and_returns_current_user(
    user_service: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """A valid token resolves to a `CurrentUser` — never to the ORM entity.

    The returned object has no `hashed_password` attribute at all, so the hash cannot
    travel up the stack into a log line or a debug response. That is structural, not a
    convention.
    """
    user = await make_user(role=Role.RECIPIENT)
    issued = token_service.create_access_token(str(user.id), role=str(user.role))

    current = await user_service.authenticate_access_token(issued.token)

    assert current.id == user.id
    assert current.role == Role.RECIPIENT
    assert not hasattr(current, "hashed_password")


async def test_refresh_token_is_rejected_at_the_access_endpoint(
    user_service: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """The `typ` claim stops a refresh token being used as an access token.

    Without it, theft of a 7-day refresh token would be equivalent to theft of a
    permanent access token — the short access-token lifetime would be pointless.
    """
    user = await make_user()
    refresh = token_service.create_refresh_token(str(user.id))

    with pytest.raises(InvalidTokenError):
        await user_service.authenticate_access_token(refresh.token)


async def test_deactivated_user_is_rejected_immediately(
    user_service: UserService,
    db_session: Any,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """Deactivation takes effect at once, not at token expiry.

    This is why `authenticate_access_token` queries the database instead of trusting
    the token's claims. Trusting them would leave a disabled account working for up to
    fifteen minutes — which is not what "disable this user" means to whoever clicked
    it.
    """
    user = await make_user()
    issued = token_service.create_access_token(str(user.id), role=str(user.role))

    # The token is still perfectly valid and unexpired.
    current = await user_service.authenticate_access_token(issued.token)
    assert current.is_active is True

    user.is_active = False
    await db_session.flush()

    current = await user_service.authenticate_access_token(issued.token)
    assert current.is_active is False  # the dependency layer then returns 403


# --------------------------------------------------------------------------
# Refresh rotation and revocation
# --------------------------------------------------------------------------
@pytest.fixture
def service_with_real_denylist(
    db_session: Any,
    password_hasher: Any,
    token_service: JwtTokenService,
    fake_redis: Any,
    settings: Any,
) -> UserService:
    """A service with a real (Redis-backed) denylist over the in-memory fake.

    The default `user_service` fixture uses `NullTokenDenylist` so unrelated tests do
    not depend on revocation behaviour. Revocation tests need the real thing — and can
    have it without a Redis server, because the denylist depends on the client's
    interface rather than on Redis itself.
    """
    return UserService(
        repository=UserRepository(db_session),
        password_hasher=password_hasher,
        token_service=token_service,
        denylist=RedisTokenDenylist(fake_redis),
        access_token_ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def test_refresh_rotates_and_invalidates_the_old_token(
    service_with_real_denylist: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """A refresh token works exactly once.

    THE KEY SECURITY TEST FOR TOKEN THEFT. Replaying a refresh token is something a
    correct client never does, so a second attempt means either a bug or a stolen
    credential — and it now fails and produces a `token_reuse_detected` log line to
    alert on.
    """
    user = await make_user()
    issued = token_service.create_refresh_token(str(user.id))

    first = await service_with_real_denylist.refresh(issued.token)
    assert first.access_token and first.refresh_token

    with pytest.raises(TokenRevokedError):
        await service_with_real_denylist.refresh(issued.token)


async def test_refresh_returns_a_different_token_each_time(
    service_with_real_denylist: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """Rotation actually issues a new token rather than returning the same one.

    A `jti` is minted per token, so even two refreshes in the same second differ. If
    they did not, rotation would be cosmetic — the "new" token would already be
    denylisted.
    """
    user = await make_user()
    issued = token_service.create_refresh_token(str(user.id))

    first = await service_with_real_denylist.refresh(issued.token)
    second = await service_with_real_denylist.refresh(first.refresh_token)

    assert first.refresh_token != second.refresh_token


async def test_logout_revokes_both_tokens(
    service_with_real_denylist: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """Logout must invalidate the refresh token too.

    Revoking only the access token leaves the refresh token able to mint a replacement
    — a "logout" that logs nobody out. That is a bug that passes a naive test which
    only checks the access token.
    """
    user = await make_user()
    access = token_service.create_access_token(str(user.id), role=str(user.role))
    refresh = token_service.create_refresh_token(str(user.id))

    await service_with_real_denylist.logout(access_token=access.token, refresh_token=refresh.token)

    with pytest.raises(TokenRevokedError):
        await service_with_real_denylist.authenticate_access_token(access.token)
    with pytest.raises(TokenRevokedError):
        await service_with_real_denylist.refresh(refresh.token)


async def test_logout_tolerates_an_already_invalid_token(
    service_with_real_denylist: UserService,
    make_user: Any,
    token_service: JwtTokenService,
) -> None:
    """Logging out with a garbage token succeeds rather than erroring.

    The user asked to be logged out, and that outcome is already true. Raising here
    would leave a client unable to complete logout, which is worse in every respect.
    """
    user = await make_user()
    refresh = token_service.create_refresh_token(str(user.id))

    await service_with_real_denylist.logout(
        access_token="not-a-real-token", refresh_token=refresh.token
    )
    # No exception. The valid half was still revoked:
    with pytest.raises(TokenRevokedError):
        await service_with_real_denylist.refresh(refresh.token)


async def test_denylist_fails_closed_when_redis_is_unavailable(
    db_session: Any,
    password_hasher: Any,
    token_service: JwtTokenService,
    settings: Any,
    make_user: Any,
) -> None:
    """When Redis is unreachable, every token is treated as revoked.

    The deliberate opposite of the cache, which fails *open*. A missed cache read costs
    a query; a missed revocation check means a token the user cancelled still works.
    Availability is the right thing to sacrifice for a security control — and this test
    pins that decision so a future "fix" for the outage cannot silently invert it.
    """
    from redis.exceptions import RedisError

    class BrokenRedis:
        async def exists(self, key: str) -> int:
            raise RedisError("connection refused")

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise RedisError("connection refused")

    service = UserService(
        repository=UserRepository(db_session),
        password_hasher=password_hasher,
        token_service=token_service,
        denylist=RedisTokenDenylist(BrokenRedis()),  # type: ignore[arg-type]
        access_token_ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

    user = await make_user()
    issued = token_service.create_access_token(str(user.id), role=str(user.role))

    with pytest.raises(TokenRevokedError):
        await service.authenticate_access_token(issued.token)


# --------------------------------------------------------------------------
# Password change
# --------------------------------------------------------------------------
async def test_change_password_requires_the_current_one(
    user_service: UserService,
    make_user: Any,
) -> None:
    """A valid token is not enough to change a password.

    The control that stops a stolen 15-minute access token becoming permanent account
    takeover: the thief has access, not credentials.
    """
    user = await make_user(password=VALID_PASSWORD)

    with pytest.raises(InvalidCredentialsError):
        await user_service.change_password(
            user.id,
            PasswordChange(current_password="WrongPassw0rd", new_password="An0therPassphrase"),
        )


async def test_change_password_rejects_reusing_the_same_password(
    user_service: UserService,
    make_user: Any,
) -> None:
    """Reusing the current password is rejected.

    Not pedantry: a user who believes they rotated a credential after a suspected
    compromise, but did not, is worse off than one who knows they failed.

    Note this is caught by the schema's `must_differ` validator when the two strings
    are equal, and by the service when the new password merely *hashes* to the same
    stored value — hence the service-level check as well.
    """
    user = await make_user(password=VALID_PASSWORD)

    with pytest.raises((SamePasswordError, ValueError)):
        await user_service.change_password(
            user.id,
            PasswordChange(current_password=VALID_PASSWORD, new_password=VALID_PASSWORD),
        )


async def test_change_password_then_login_with_the_new_one(
    user_service: UserService,
    make_user: Any,
) -> None:
    """End to end: the change takes effect and the old password stops working."""
    new_password = "Compl3telyNewPass"
    user = await make_user(email="grace@example.com", password=VALID_PASSWORD)

    await user_service.change_password(
        user.id,
        PasswordChange(current_password=VALID_PASSWORD, new_password=new_password),
    )

    authenticated, _ = await user_service.authenticate(
        LoginRequest(email="grace@example.com", password=new_password)
    )
    assert authenticated.id == user.id

    with pytest.raises(InvalidCredentialsError):
        await user_service.authenticate(
            LoginRequest(email="grace@example.com", password=VALID_PASSWORD)
        )


# --------------------------------------------------------------------------
# Token service internals
# --------------------------------------------------------------------------
def test_tokens_carry_distinct_jtis(token_service: JwtTokenService) -> None:
    """Every token gets a unique id, which is what makes revocation possible."""
    first = token_service.create_access_token("00000000-0000-0000-0000-000000000001")
    second = token_service.create_access_token("00000000-0000-0000-0000-000000000001")
    assert first.jti != second.jti


def test_decode_rejects_a_tampered_signature(token_service: JwtTokenService) -> None:
    """Editing the payload invalidates the signature.

    The foundational JWT property. Worth an explicit test because an implementation
    that decoded without verifying (`options={"verify_signature": False}`, or a
    library misuse) would pass every other test in this file.
    """
    from app.core.security import TokenInvalidError

    issued = token_service.create_access_token("00000000-0000-0000-0000-000000000001")
    header, payload, signature = issued.token.split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(TokenInvalidError):
        token_service.decode(tampered, TokenType.ACCESS)
