"""Business logic for identity: registration, login, token lifecycle, profiles.

READ THIS FILE'S IMPORTS FIRST
------------------------------
There is no `fastapi`, no `HTTPException`, no `Request`, no `select`, no `session`.
This class could be driven by a CLI command, an arq worker, or a gRPC service
without changing a line. That is not an accident — it is the property the whole
layering exists to produce.

THE ONE EXCEPTION, STATED HONESTLY: `from sqlalchemy.exc import IntegrityError`.
It is imported to *catch* the unique-constraint violation that a concurrent
duplicate registration produces, and translate it into the same domain error the
pre-check raises. Two observations:

  * importing an exception *class* is far weaker coupling than importing query
    construction (`select`) or the session — no SQL is written here, and no
    query is issued;
  * it is nonetheless a real dependency on the persistence library, so the honest
    claim is "no SQLAlchemy **API**", not "no SQLAlchemy at all".

The alternative — having the repository translate it — would push a domain error
into the data layer, which is a worse trade. Documented rather than hidden,
because a rule with a silent exception is not a rule.

What it *does* import: a repository (data), a hasher and a token service
(primitives), a denylist (revocation). All injected through the constructor, all
replaceable. `tests/modules/users/test_service.py` runs every rule below with
fakes and no database.

THE THREE RULES THIS FILE FOLLOWS
---------------------------------
1. **Never calls `commit()`.** The transaction boundary is the request
   (`shared/db/session.py`). `flush()` is used where a generated id or an early
   constraint check is needed.
2. **Raises domain exceptions**, never HTTP ones. `core/exceptions.py` translates.
3. **Does not read the clock directly** — `utcnow()` is the one seam, so
   time-dependent rules are testable.

THE TOKEN DESIGN, IN ONE PARAGRAPH
----------------------------------
A 15-minute access token and a 7-day refresh token, both carrying `typ` and
`jti`. Refresh is **single-use**: presenting one issues a new pair and denylists
the old `jti`. Replaying a refresh token therefore fails — which is what turns
theft of a long-lived credential from "silent, indefinite access" into "a 401 and
a log line you can alert on". Logout denylists both tokens. See
docs/SECURITY.md for the full threat model.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger
from app.core.redis import KEY_PREFIX_DENYLIST
from app.core.security import (
    JwtTokenService,
    PasswordHasher,
    PasswordTooLongError,
    TokenError,
    TokenExpiredError,
    TokenPayload,
    TokenType,
    TokenTypeMismatchError,
)
from app.modules.users.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
    SamePasswordError,
    TokenRevokedError,
    UserNotFoundError,
    WeakPasswordError,
)
from app.modules.users.models import Role, User
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import (
    LoginRequest,
    PasswordChange,
    TokenPair,
    UserCreate,
    UserUpdate,
)
from app.shared.security.principal import CurrentUser
from app.shared.utils.datetime_utils import seconds_until

log = get_logger(__name__)

# A pre-computed bcrypt hash of a value nobody will ever submit. Used to make a
# failed login for a *non-existent* user cost the same as one for an existing
# user — see `_dummy_verify` and the note in `authenticate`.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.6VnJ4t6xkDBhKZMXBFHkzXm3XCsGnLu"


# --------------------------------------------------------------------------
# Token revocation
# --------------------------------------------------------------------------
class TokenDenylist(Protocol):
    """Revocation store.

    A Protocol so the service is testable without Redis, and so the backing store
    can change (Redis, a database table, an in-memory set for a single-process
    deployment) without touching any business logic.
    """

    async def revoke(self, jti: str, *, ttl_seconds: int) -> None: ...

    async def is_revoked(self, jti: str) -> bool: ...


class RedisTokenDenylist:
    """Redis-backed denylist keyed by `jti`.

    WHY A DENYLIST IS NECESSARY AT ALL
    JWTs are self-contained: the server does not store them, so it cannot simply
    "delete the session" on logout. Without a denylist, a logged-out access token
    keeps working until it expires, and a stolen refresh token works for a week.

    WHY THE TTL EQUALS THE TOKEN'S REMAINING LIFETIME
    An entry is only useful while the token it revokes could still be accepted.
    Setting the TTL to exactly that window means the denylist stays small and
    bounded (proportional to tokens revoked in the last 7 days, not to all tokens
    ever issued) with no cleanup job to write or forget.

    FAIL **CLOSED**, UNLIKE THE CACHE
    `is_revoked` returns `True` when Redis is unreachable. That is the opposite of
    `RedisCache`, and the difference is deliberate:

      * a cache miss costs a database query — degraded performance;
      * a *missed revocation* means a token the user believes they cancelled
        still works — a security failure.

    So the cache fails open and the denylist fails closed. When Redis is down,
    every authenticated request is rejected — a hard outage. That is the correct
    trade for a security control, and it is why Redis is a `depends_on:
    service_healthy` dependency of the API container, not an optional extra.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(jti: str) -> str:
        return f"{KEY_PREFIX_DENYLIST}{jti}"

    async def revoke(self, jti: str, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            # Already expired: the token cannot be accepted anyway, so storing it
            # would be pointless. (`SET` with a non-positive TTL is an error.)
            return
        try:
            await self._redis.set(self._key(jti), "1", ex=ttl_seconds)
        except RedisError as exc:
            # Surfaced as an error, not swallowed: a logout that silently failed
            # to revoke is a lie told to the user.
            log.error("token_revocation_failed", jti=jti, error=str(exc))
            raise

    async def is_revoked(self, jti: str) -> bool:
        try:
            return bool(await self._redis.exists(self._key(jti)))
        except RedisError as exc:
            log.error("denylist_check_failed_failing_closed", jti=jti, error=str(exc))
            return True  # fail closed — see the class docstring


class NullTokenDenylist:
    """Revokes nothing. For unit tests that are not about revocation."""

    async def revoke(self, jti: str, *, ttl_seconds: int) -> None:
        return None

    async def is_revoked(self, jti: str) -> bool:
        return False


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
class UserService:
    """Identity use cases.

    CONSTRUCTOR INJECTION, NOT `Depends()` IN METHOD SIGNATURES.
    Four collaborators arrive as constructor arguments. Consequences:

    * `UserService(repo, hasher, tokens, denylist)` is constructible in a test
      with four fakes and no framework.
    * The dependencies are visible in one place. A class that reached for a global
      session or called `get_settings()` internally would hide them, and you would
      only discover them by reading every method.
    * FastAPI's `Depends` appears exactly once, in `api.py`'s provider function.
      The service does not know FastAPI exists.

    It also satisfies `UserAuthenticator` (shared/security/principal.py) purely by
    having the right method — no inheritance, no registration.
    """

    def __init__(
        self,
        repository: UserRepository,
        password_hasher: PasswordHasher,
        token_service: JwtTokenService,
        denylist: TokenDenylist,
        *,
        access_token_ttl_seconds: int,
    ) -> None:
        self._repo = repository
        self._hasher = password_hasher
        self._tokens = token_service
        self._denylist = denylist
        self._access_ttl_seconds = access_token_ttl_seconds

    # -- Registration ------------------------------------------------------

    async def register(self, payload: UserCreate) -> tuple[User, TokenPair]:
        """Create an account and sign the user in.

        THE ROLE IS ASSIGNED HERE, NOT TAKEN FROM THE REQUEST. `UserCreate` has no
        `role` field, and this method hardcodes `Role.DONOR`. Privilege escalation
        via the registration endpoint is therefore not something we validate
        against — it is something the type system makes unexpressible.

        Uniqueness is checked twice, on purpose:
          1. `email_exists()` — gives a clean 409 in the ordinary case;
          2. the unique constraint — the only thing that actually holds under two
             concurrent signups for the same address.
        A pre-check alone is a race; a constraint alone gives a confusing error.
        """
        if await self._repo.email_exists(payload.email):
            log.info("registration_rejected_duplicate_email", email=payload.email)
            raise EmailAlreadyRegisteredError(payload.email)

        try:
            hashed = self._hasher.hash(payload.password)
        except PasswordTooLongError as exc:
            raise WeakPasswordError(str(exc)) from exc

        user = User(
            email=payload.email,
            hashed_password=hashed,
            full_name=payload.full_name,
            role=Role.DONOR,
            is_active=True,
        )

        try:
            await self._repo.add(user)
        except IntegrityError as exc:
            # The race described above actually happened. Translate the database
            # error into the same domain error the pre-check produces, so the
            # client sees one consistent response either way.
            log.warning("registration_race_on_email", email=payload.email)
            raise EmailAlreadyRegisteredError(payload.email) from exc

        log.info("user_registered", user_id=str(user.id), role=user.role)
        return user, self._issue_token_pair(user)

    # -- Login -------------------------------------------------------------

    async def authenticate(self, payload: LoginRequest) -> tuple[User, TokenPair]:
        """Verify credentials and issue tokens.

        TWO ANTI-ENUMERATION MEASURES, BOTH NECESSARY:

        1. *One* error for every failure mode — unknown email, wrong password,
           disabled account all raise `InvalidCredentialsError` with the same
           message. Distinguishing them turns login into an oracle for "does this
           address have an account?".

        2. A dummy hash comparison when the email is unknown. Without it the
           unknown-email path returns in ~2ms (one indexed SELECT) while the
           wrong-password path takes ~250ms (a bcrypt verify). That 100x
           difference is trivially measurable, and it leaks precisely what the
           identical error message was hiding. A vague message with a loud timing
           signal is not vague.

        The inactive-account case is *logged* distinctly (server-side, where it is
        useful) while returning the same error to the client.
        """
        user = await self._repo.get_by_email(payload.email)

        if user is None:
            self._dummy_verify(payload.password)
            log.info("login_failed_unknown_email", email=payload.email)
            raise InvalidCredentialsError

        if not self._hasher.verify(payload.password, user.hashed_password):
            log.info("login_failed_bad_password", user_id=str(user.id))
            raise InvalidCredentialsError

        if not user.is_active:
            log.warning("login_failed_inactive_account", user_id=str(user.id))
            raise InvalidCredentialsError

        # Transparent cost upgrade: if BCRYPT_ROUNDS was raised since this
        # password was set, rehash now, while the plaintext is legitimately in
        # hand. Every active user migrates to the stronger factor without a
        # password reset — which is the only way a rounds increase ever reaches
        # existing accounts.
        if self._hasher.needs_rehash(user.hashed_password):
            user.hashed_password = self._hasher.hash(payload.password)
            log.info("password_rehashed_stronger_cost", user_id=str(user.id))

        log.info("login_succeeded", user_id=str(user.id), role=user.role)
        return user, self._issue_token_pair(user)

    def _dummy_verify(self, password: str) -> None:
        """Burn the same CPU a real verify would. See `authenticate`."""
        self._hasher.verify(password, _DUMMY_HASH)

    # -- Token lifecycle ---------------------------------------------------

    async def authenticate_access_token(self, token: str) -> CurrentUser:
        """Validate an access token and load the caller.

        This is the method that satisfies the `UserAuthenticator` Protocol, and it
        is on the hot path — every authenticated request runs it.

        WHY IT HITS THE DATABASE INSTEAD OF TRUSTING THE CLAIMS
        The token already carries `sub` and `role`, so this could be done with
        zero queries. It deliberately is not, because a token is a *snapshot* from
        up to 15 minutes ago:

          * an account deactivated one minute ago must stop working now, not in
            fourteen minutes;
          * a role demoted from ADMIN must take effect immediately;
          * a deleted user must not keep a valid identity.

        One indexed primary-key lookup per request is a small price for
        revocation that actually works. If this ever becomes a measured
        bottleneck, cache the user row for a few seconds — but measure first
        (KISS: do not optimise a lookup that is not slow).
        """
        payload = self._decode_or_raise(token, TokenType.ACCESS)

        if await self._denylist.is_revoked(payload.jti):
            log.info("access_token_revoked", jti=payload.jti)
            raise TokenRevokedError

        try:
            user_id = payload.user_id
        except ValueError as exc:
            # `sub` was not a UUID — a forged or corrupt token that nonetheless
            # carried a valid signature is impossible, but a token minted by an
            # older/other version of this app is not.
            raise InvalidTokenError("Malformed token subject") from exc

        user = await self._repo.get(user_id)
        if user is None:
            log.warning("token_valid_but_user_missing", user_id=str(user_id))
            raise InvalidTokenError("Token subject no longer exists")

        # Returns the framework-free value object, never the ORM entity — so
        # `hashed_password` cannot travel any further up the stack.
        return CurrentUser(
            id=user.id,
            email=user.email,
            role=str(user.role),
            is_active=user.is_active,
        )

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, invalidating the old one.

        ROTATION IS THE POINT. The old `jti` is denylisted, so each refresh token
        works exactly once. Consequences:

          * A stolen refresh token is useful only until the legitimate client next
            refreshes — after which the thief's copy fails.
          * If the *thief* refreshes first, the legitimate client's next attempt
            fails, which surfaces the compromise instead of hiding it.
          * `token_reuse_detected` in the logs is a high-signal alert: it means a
             refresh token was presented twice, which a correct client never does.

        Revocation happens *before* the new pair is issued. If it happened after,
        a crash in between would leave the old token still valid — which is the
        exact failure rotation exists to prevent.
        """
        payload = self._decode_or_raise(refresh_token, TokenType.REFRESH)

        if await self._denylist.is_revoked(payload.jti):
            log.warning("token_reuse_detected", jti=payload.jti, user_id=payload.subject)
            raise TokenRevokedError

        user = await self._repo.get(payload.user_id)
        if user is None:
            raise InvalidTokenError("Token subject no longer exists")
        if not user.is_active:
            # Checked on refresh as well as on request: otherwise a deactivated
            # account could keep minting fresh access tokens for a week.
            log.warning("refresh_rejected_inactive_user", user_id=str(user.id))
            raise InactiveUserError

        await self._denylist.revoke(
            payload.jti,
            ttl_seconds=seconds_until(payload.expires_at),
        )

        log.info("tokens_refreshed", user_id=str(user.id))
        # The new access token carries the role read from the database just now,
        # so a role change propagates on the next refresh at the latest.
        return self._issue_token_pair(user)

    async def logout(self, access_token: str, refresh_token: str | None = None) -> None:
        """Revoke the caller's tokens.

        Both tokens must be revoked. Revoking only the access token leaves the
        refresh token able to mint a new one — a "logout" that logs nobody out.
        The client is expected to send both; if it omits the refresh token we
        revoke what we can and log the gap, because refusing to log out at all
        would be worse.

        Tolerant of an already-expired or malformed token: the user asked to be
        logged out, and the outcome they want is already true. Erroring here would
        leave a client stuck unable to complete logout.
        """
        try:
            access_payload = self._decode_or_raise(access_token, TokenType.ACCESS)
            await self._denylist.revoke(
                access_payload.jti,
                ttl_seconds=seconds_until(access_payload.expires_at),
            )
            log.info("access_token_revoked_on_logout", user_id=access_payload.subject)
        except (InvalidTokenError, TokenRevokedError):
            log.info("logout_with_already_invalid_access_token")

        if refresh_token is None:
            log.info("logout_without_refresh_token")
            return

        try:
            refresh_payload = self._decode_or_raise(refresh_token, TokenType.REFRESH)
            await self._denylist.revoke(
                refresh_payload.jti,
                ttl_seconds=seconds_until(refresh_payload.expires_at),
            )
            log.info("refresh_token_revoked_on_logout", user_id=refresh_payload.subject)
        except (InvalidTokenError, TokenRevokedError):
            log.info("logout_with_already_invalid_refresh_token")

    def _issue_token_pair(self, user: User) -> TokenPair:
        """Mint an access/refresh pair. One place, so the two can never diverge."""
        access = self._tokens.create_access_token(str(user.id), role=str(user.role))
        refresh = self._tokens.create_refresh_token(str(user.id))
        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=self._access_ttl_seconds,
        )

    def _decode_or_raise(self, token: str, expected: TokenType) -> TokenPayload:
        """Decode, collapsing every failure into one domain error.

        The *reason* is logged (useful, server-side) but never returned. An
        attacker probing tokens should learn nothing about which part failed.
        """
        try:
            return self._tokens.decode(token, expected)
        except TokenExpiredError as exc:
            log.info("token_expired", expected_type=expected.value)
            raise InvalidTokenError("Token has expired") from exc
        except TokenTypeMismatchError as exc:
            # High-signal: a correct client never does this. It means either a
            # client bug or someone trying an access token at /auth/refresh.
            log.warning("token_type_mismatch", expected_type=expected.value, detail=str(exc))
            raise InvalidTokenError from exc
        except TokenError as exc:
            log.info("token_invalid", expected_type=expected.value, detail=str(exc))
            raise InvalidTokenError from exc

    # -- Profile -----------------------------------------------------------

    async def get_by_id(self, user_id: uuid.UUID) -> User:
        """Fetch a user, raising if absent.

        The repository returns `None`; this converts absence into a domain error.
        That translation belongs here, not in the repository: only the caller
        knows whether "missing" is exceptional (it is, for a profile lookup) or
        expected (it is, for the registration uniqueness check).
        """
        user = await self._repo.get(user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def update_profile(self, user_id: uuid.UUID, payload: UserUpdate) -> User:
        """Apply a partial profile update.

        `exclude_unset=True` is what makes this a real PATCH: only the fields the
        client actually sent are applied. Without it, every omitted field would be
        overwritten with its default — so a client updating `full_name` would
        silently blank everything else. This one keyword is the difference between
        PATCH and a destructive PUT.
        """
        user = await self.get_by_id(user_id)
        values = payload.model_dump(exclude_unset=True)
        if not values:
            return user
        updated = await self._repo.update(user, **values)
        log.info("profile_updated", user_id=str(user_id), fields=sorted(values))
        return updated

    async def change_password(self, user_id: uuid.UUID, payload: PasswordChange) -> None:
        """Change a password after re-verifying the current one.

        Requiring the current password even though the caller holds a valid token
        is the control that stops a stolen access token from being escalated into
        permanent account takeover: the thief has 15 minutes of access, not the
        credentials.

        NOTE ON WHAT THIS DOES NOT DO: it does not revoke the user's other
        tokens. Doing so properly needs a per-user token generation counter (bump
        it, and every token issued before the bump becomes invalid) rather than a
        per-jti denylist, since we cannot enumerate a user's outstanding tokens.
        Left out to keep the sample focused, and called out here rather than
        quietly omitted — see docs/SECURITY.md.
        """
        user = await self.get_by_id(user_id)

        if not self._hasher.verify(payload.current_password, user.hashed_password):
            log.warning("password_change_failed_bad_current", user_id=str(user_id))
            raise InvalidCredentialsError

        if self._hasher.verify(payload.new_password, user.hashed_password):
            raise SamePasswordError

        try:
            new_hash = self._hasher.hash(payload.new_password)
        except PasswordTooLongError as exc:
            raise WeakPasswordError(str(exc)) from exc

        await self._repo.update(user, hashed_password=new_hash)
        log.info("password_changed", user_id=str(user_id))

    # -- Administration ----------------------------------------------------

    async def list_users(
        self,
        *,
        offset: int,
        limit: int,
        role: Role | None = None,
    ) -> tuple[Sequence[User], int]:
        """Paginated listing with a total, for the admin screen.

        Returns `(items, total)` rather than a `Page` DTO: assembling the response
        envelope is presentation, and belongs in `api.py`. Keeping the service's
        return type framework-free is what lets a CLI reuse it.
        """
        if role is not None:
            items = await self._repo.list_by_role(role, offset=offset, limit=limit)
            total = await self._repo.count_by_role(role)
        else:
            items = await self._repo.list(offset=offset, limit=limit)
            total = await self._repo.count()
        return items, total

    async def change_role(self, user_id: uuid.UUID, new_role: Role) -> User:
        """Promote or demote. Authorization is enforced at the route
        (`require_roles(Role.ADMIN)`), so this method assumes the caller is
        already permitted — the check is declarative and visible in `api.py`
        rather than buried here."""
        user = await self.get_by_id(user_id)
        previous = user.role
        updated = await self._repo.update(user, role=new_role)
        # An audit-grade log line: privilege changes are the first thing anyone
        # asks about after an incident.
        log.warning(
            "user_role_changed",
            user_id=str(user_id),
            previous_role=str(previous),
            new_role=str(new_role),
        )
        return updated

    async def set_active(self, user_id: uuid.UUID, *, is_active: bool) -> User:
        """Enable or disable an account.

        Takes effect immediately, not at token expiry, because
        `authenticate_access_token` re-reads `is_active` from the database on
        every request. That is the design decision that makes this endpoint
        meaningful — see the note there.
        """
        user = await self.get_by_id(user_id)
        updated = await self._repo.update(user, is_active=is_active)
        log.warning("user_active_changed", user_id=str(user_id), is_active=is_active)
        return updated
