"""Business logic for food items — including the rule the whole app exists around.

THIS FILE OWNS ONE INVARIANT THAT NOTHING MAY BYPASS
----------------------------------------------------
    Expired or non-AVAILABLE food cannot be reserved for donation.

It is enforced in exactly one method, `reserve()`. The donations module *cannot*
reserve an item any other way, because it holds a `FoodItemService`, not a
`FoodItemRepository` — so there is no code path from a donation request to the
`food_items` table that skips this check. That is what the cross-module rule in
ARCHITECTURE.md buys: not tidiness, but an invariant that is structurally
impossible to circumvent.

Note also that `reserve()` uses `get_for_update()` rather than `get()`, so the
check-then-write is atomic under concurrency. Two simultaneous requests for the
last loaf cannot both succeed — see `FoodItemRepository.get_for_update`.

CACHE INVALIDATION LIVES HERE, NOT IN THE CONTROLLER
----------------------------------------------------
Every method that mutates an item calls `_invalidate_list_cache()`. Putting that
in the router would mean remembering it on every write endpoint, and the first
forgotten call serves stale data for a TTL. In the service it sits next to the
write it corresponds to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta

from app.core.logging import get_logger
from app.modules.food_items.exceptions import (
    FoodItemExpiredError,
    FoodItemImmutableError,
    FoodItemNotAvailableError,
    FoodItemNotFoundError,
    InvalidExpiryDateError,
    InvalidStatusTransitionError,
)
from app.modules.food_items.models import FoodItem, FoodItemStatus
from app.modules.food_items.repository import FoodItemRepository
from app.modules.food_items.schemas import FoodItemCreate, FoodItemUpdate
from app.shared.cache.cache import Cache, build_cache_key
from app.shared.security.permissions import require_ownership
from app.shared.security.principal import CurrentUser
from app.shared.utils.datetime_utils import is_expired, utcnow

log = get_logger(__name__)

# One namespace for every cached list page, so a single `delete_prefix` call
# invalidates all of them. Defined as a constant because it is written in two
# places (build and invalidate) and a typo would produce a cache that is never
# invalidated — a bug with no error message.
_LIST_CACHE_NAMESPACE = "food_items:available"

# Statuses an owner may still edit. RESERVED is excluded: a recipient has already
# acted on the details as listed, and changing the quantity or pickup location
# under them would invalidate a decision someone else made.
_EDITABLE_STATUSES = frozenset({FoodItemStatus.AVAILABLE})


class FoodItemService:
    """Food-listing use cases.

    Collaborators are injected: a repository and a `Cache`. The `Cache` is the
    `Protocol` from `shared/cache/cache.py`, so tests pass `NullCache` and this
    class behaves identically — which is the proof that caching here is an
    optimisation and not a correctness dependency.
    """

    def __init__(self, repository: FoodItemRepository, cache: Cache) -> None:
        self._repo = repository
        self._cache = cache

    # -- Commands ----------------------------------------------------------

    async def create(self, payload: FoodItemCreate, owner: CurrentUser) -> FoodItem:
        """List new surplus food.

        `owner_id` comes from the authenticated caller, never from the request
        body — `FoodItemCreate` has no such field. A client therefore cannot list
        food under another user's name; it is not validated against, it is
        unexpressible.
        """
        if is_expired(payload.expires_at):
            # Also checked by the schema. Repeated here because a service must be
            # correct when driven by something other than this HTTP endpoint — a
            # CLI import, a data migration, a future bulk-upload job.
            raise InvalidExpiryDateError

        item = FoodItem(
            name=payload.name,
            description=payload.description,
            quantity=payload.quantity,
            unit=payload.unit,
            expires_at=payload.expires_at,
            pickup_location=payload.pickup_location,
            status=FoodItemStatus.AVAILABLE,
            owner_id=owner.id,
        )
        await self._repo.add(item)
        await self._invalidate_list_cache()

        log.info(
            "food_item_created",
            food_item_id=str(item.id),
            owner_id=str(owner.id),
            expires_at=item.expires_at.isoformat(),
        )
        return item

    async def update(
        self,
        item_id: uuid.UUID,
        payload: FoodItemUpdate,
        actor: CurrentUser,
    ) -> FoodItem:
        """Edit a listing you own.

        Three gates, in this order:
          1. it exists;
          2. you own it (or you are an admin) — the IDOR check;
          3. it is still editable.

        Ownership before editability, so a stranger probing ids cannot learn an
        item's status from which error they get back.
        """
        item = await self._get_or_raise(item_id)
        require_ownership(actor, item.owner_id, resource="food item")

        if item.status not in _EDITABLE_STATUSES:
            raise FoodItemImmutableError(str(item.status))

        values = payload.model_dump(exclude_unset=True)  # true PATCH semantics
        if not values:
            return item

        updated = await self._repo.update(item, **values)
        await self._invalidate_list_cache()
        log.info("food_item_updated", food_item_id=str(item_id), fields=sorted(values))
        return updated

    async def reserve(self, item_id: uuid.UUID) -> FoodItem:
        """Move an item AVAILABLE -> RESERVED. **The invariant lives here.**

        Called by `DonationService.request_donation` — never by a controller
        directly. It is the only way an item becomes RESERVED, which is what makes
        the expiry rule unbypassable.

        Uses `get_for_update()`, so the row is locked for the rest of the
        transaction. Two concurrent requests for the same item are serialised: the
        second blocks, re-reads RESERVED, and is correctly rejected. With a plain
        `get()` both would pass the check and both would succeed — see
        `FoodItemRepository.get_for_update` for the full sequence.

        Note the ordering of the two checks: **expiry first, status second.**
        Expiry is the more fundamental failure and gives the more useful message.
        An expired-but-AVAILABLE item is the common case (nothing has swept it
        yet), and reporting "not available" for it would be actively misleading.

        Deliberately takes no `actor`: reservation is a consequence of a donation
        request, and the authorization for *that* belongs to the donations module.
        Duplicating it here would mean two places to keep in step.
        """
        item = await self._repo.get_for_update(item_id)
        if item is None:
            raise FoodItemNotFoundError(item_id)

        # `expires_at` is the authority, not `status`. An item whose date passed a
        # second ago is expired even though no sweep has marked it — which is
        # exactly why the check is against the timestamp.
        if is_expired(item.expires_at):
            log.info(
                "reservation_rejected_expired",
                food_item_id=str(item_id),
                expired_at=item.expires_at.isoformat(),
            )
            raise FoodItemExpiredError(item.name)

        if item.status is not FoodItemStatus.AVAILABLE:
            log.info(
                "reservation_rejected_status",
                food_item_id=str(item_id),
                status=str(item.status),
            )
            raise FoodItemNotAvailableError(str(item.status))

        updated = await self._transition(item, FoodItemStatus.RESERVED)
        await self._invalidate_list_cache()
        log.info("food_item_reserved", food_item_id=str(item_id))
        return updated

    async def release(self, item_id: uuid.UUID) -> FoodItem:
        """RESERVED -> AVAILABLE, when a recipient cancels their request.

        The counterpart to `reserve`. Without it a cancelled request would strand
        the food in RESERVED forever — perfectly good food that nobody can claim.
        Also called by the donations module, never by a controller.
        """
        item = await self._repo.get_for_update(item_id)
        if item is None:
            raise FoodItemNotFoundError(item_id)

        if item.status is not FoodItemStatus.RESERVED:
            raise InvalidStatusTransitionError(str(item.status), FoodItemStatus.AVAILABLE)

        # Expired food is released to EXPIRED, not back to AVAILABLE: re-listing
        # food that went off while reserved would put the invariant's failure
        # mode back on the table.
        target = FoodItemStatus.EXPIRED if is_expired(item.expires_at) else FoodItemStatus.AVAILABLE
        updated = await self._transition(item, target)
        await self._invalidate_list_cache()
        log.info("food_item_released", food_item_id=str(item_id), new_status=str(target))
        return updated

    async def mark_donated(self, item_id: uuid.UUID) -> FoodItem:
        """RESERVED -> DONATED. Terminal. Called when a handover completes."""
        item = await self._repo.get_for_update(item_id)
        if item is None:
            raise FoodItemNotFoundError(item_id)

        updated = await self._transition(item, FoodItemStatus.DONATED)
        await self._invalidate_list_cache()
        log.info("food_item_donated", food_item_id=str(item_id))
        return updated

    async def cancel(self, item_id: uuid.UUID, actor: CurrentUser) -> FoodItem:
        """Withdraw your own listing. Terminal.

        An owner may not cancel a RESERVED item: a recipient is already counting on
        it. They must decline the donation request first, which releases the item
        and notifies the other party. The state machine expresses this by omitting
        `RESERVED -> CANCELLED` from `ALLOWED_TRANSITIONS`, so the rule is enforced
        by the table rather than by an `if` here.
        """
        item = await self._get_or_raise(item_id)
        require_ownership(actor, item.owner_id, resource="food item")

        updated = await self._transition(item, FoodItemStatus.CANCELLED)
        await self._invalidate_list_cache()
        log.info("food_item_cancelled", food_item_id=str(item_id), actor_id=str(actor.id))
        return updated

    async def soft_delete(self, item_id: uuid.UUID, actor: CurrentUser) -> None:
        """Remove a listing from view, keeping the row.

        `BaseRepository.delete` soft-deletes automatically because `FoodItem`
        carries `SoftDeleteMixin` — the service does not choose, and cannot get it
        wrong.
        """
        item = await self._get_or_raise(item_id)
        require_ownership(actor, item.owner_id, resource="food item")

        await self._repo.delete(item)
        await self._invalidate_list_cache()
        log.info("food_item_deleted", food_item_id=str(item_id), actor_id=str(actor.id))

    async def expire_stale_items(self) -> int:
        """Sweep past-date items into EXPIRED. Returns how many were changed.

        Idempotent by construction (the WHERE clause excludes already-EXPIRED
        rows), so it is safe to run on a schedule, twice concurrently, or by hand
        during an incident.

        Not required for correctness — `reserve()` checks `expires_at` directly, so
        an unswept item still cannot be donated. This exists so listings look right
        and reporting is cheap. Worth knowing which of the two is the safety
        mechanism: it is the check, not the sweep.
        """
        count = await self._repo.mark_expired_batch(utcnow())
        if count:
            await self._invalidate_list_cache()
            log.info("food_items_expired_batch", count=count)
        return count

    # -- Queries -----------------------------------------------------------

    async def list_available(
        self,
        *,
        offset: int,
        limit: int,
        page: int,
        size: int,
        search: str | None = None,
    ) -> tuple[Sequence[FoodItem] | list[dict[str, object]], int, bool]:
        """The public browse query, cache-aside.

        Returns `(items, total, from_cache)`. `from_cache` exists so the router
        knows whether it received ORM entities or already-serialised dicts, and so
        the cache can be observed in tests and logs rather than taken on faith.

        THE CACHE-ASIDE PATTERN, AND WHY THIS ENDPOINT
        1. look in the cache;
        2. on a miss, query the database;
        3. store the result with a TTL;
        4. invalidate on every write.

        This is the right endpoint for it: unauthenticated, identical for every
        caller, the most-requested read in the application, and backed by two
        queries (rows + `COUNT(*)`). Contrast `/food-items/mine`, which is
        per-user — caching that would multiply keys by users for a fraction of the
        traffic.

        WHY THE CACHE IS KEYED ONLY ON THE QUERY, NOT THE USER
        The response contains no per-user data, so one cached page is correct for
        everyone. If a personalised field were ever added, the key must include the
        user id — otherwise user A is served user B's view. That is the classic
        cache-poisoning-by-omission bug, and the reason this endpoint is
        anonymous-only.

        THE STALENESS WINDOW IS BOUNDED AND ACCEPTED
        Writes invalidate, so the usual case is fresh. If an invalidation is lost
        (Redis blip — `delete_prefix` fails open), the 60-second TTL caps the
        damage. Worst case a browser sees a listing that was claimed a moment ago,
        and `reserve()` then rejects the request with a correct 409. The cache can
        make the UI slightly stale; it cannot make the system incorrect.
        """
        cache_key = build_cache_key(_LIST_CACHE_NAMESPACE, page=page, size=size, search=search)

        cached = await self._cache.get(cache_key)
        if cached is not None:
            log.debug("food_items_served_from_cache", key=cache_key)
            return cached["items"], cached["total"], True

        now = utcnow()  # read once, so rows and count agree on "now"
        items = await self._repo.list_available(offset=offset, limit=limit, now=now, search=search)
        total = await self._repo.count_available(now=now, search=search)

        # Serialisation for the cache happens in the router (it owns the DTOs).
        # The service returns entities on a miss and the router writes through via
        # `cache_available_page`. Splitting it this way keeps the service free of
        # presentation concerns while keeping the cache key definition here, where
        # invalidation also lives.
        return items, total, False

    async def cache_available_page(
        self,
        *,
        page: int,
        size: int,
        search: str | None,
        serialised_items: list[dict[str, object]],
        total: int,
    ) -> None:
        """Store an already-serialised page. Called by the router after a miss."""
        cache_key = build_cache_key(_LIST_CACHE_NAMESPACE, page=page, size=size, search=search)
        await self._cache.set(cache_key, {"items": serialised_items, "total": total})

    async def get_by_id(self, item_id: uuid.UUID) -> FoodItem:
        """Fetch one item. Public — anyone may view a listing."""
        return await self._get_or_raise(item_id)

    async def list_mine(
        self,
        owner: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: FoodItemStatus | None = None,
    ) -> tuple[Sequence[FoodItem], int]:
        """ "My listings". Scoped to the token's user id, never to a path parameter,
        so there is nothing for a client to tamper with. Not cached: per-user and
        low-traffic."""
        items = await self._repo.list_by_owner(owner.id, offset=offset, limit=limit, status=status)
        total = await self._repo.count_by_owner(owner.id, status=status)
        return items, total

    async def list_expiring_soon(self, *, within_hours: int = 24) -> Sequence[FoodItem]:
        """Items expiring within the window — drives expiry reminders."""
        return await self._repo.list_expiring_before(utcnow() + timedelta(hours=within_hours))

    # -- Internals ---------------------------------------------------------

    async def _get_or_raise(self, item_id: uuid.UUID) -> FoodItem:
        item = await self._repo.get(item_id)
        if item is None:
            raise FoodItemNotFoundError(item_id)
        return item

    async def _transition(self, item: FoodItem, new_status: FoodItemStatus) -> FoodItem:
        """The single gate for every status change.

        Four lines, because the rules are data (`ALLOWED_TRANSITIONS` in models.py)
        rather than code. Adding a state means editing that table; this method never
        changes. That is the Open/Closed Principle doing actual work — and it is why
        no method above contains a nested status conditional.
        """
        if not item.can_transition_to(new_status):
            raise InvalidStatusTransitionError(str(item.status), str(new_status))
        return await self._repo.update(item, status=new_status)

    async def _invalidate_list_cache(self) -> None:
        """Drop every cached page of the available list.

        Prefix-scoped, so it can never touch rate-limit counters or the JWT
        denylist in the same Redis instance (see `RedisCache.clear`).

        Coarse on purpose: working out which page a changed item appeared on is
        both hard and fragile, while dropping a few dozen short-lived keys costs
        almost nothing. Correct and simple beats clever and subtly wrong (KISS).
        """
        await self._cache.delete_prefix(_LIST_CACHE_NAMESPACE)
