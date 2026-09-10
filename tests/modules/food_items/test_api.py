"""Integration tests for `/food-items`."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.food_items.models import FoodItemStatus
from app.shared.utils.datetime_utils import utcnow

pytestmark = pytest.mark.integration

API = "/api/v1"


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Fresh vegetables",
        "description": "Assorted, from today's market",
        "quantity": "4.500",
        "unit": "KG",
        "expires_at": (utcnow() + timedelta(days=2)).isoformat(),
        "pickup_location": "45 Market Square",
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------
# Public access
# --------------------------------------------------------------------------
async def test_browse_is_public(client: AsyncClient) -> None:
    """`GET /food-items` requires no token.

    A deliberate product decision: someone in need should be able to see what is
    available before creating an account. It is also why this is the one endpoint safe
    to cache globally — the response contains no per-user data.
    """
    response = await client.get(f"{API}/food-items")

    assert response.status_code == 200
    assert "items" in response.json()


async def test_browse_excludes_expired_and_non_available_items(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """Only AVAILABLE, unexpired, undeleted items are listed.

    Three exclusions in one assertion, because all three are produced by the same
    filter set (`_available_filters` plus the soft-delete filter in `_base_select`). If
    any one leaked, expired food would appear browsable — and a user would travel for
    food that has gone off.
    """
    donor = await make_user()
    fresh = await make_food_item(owner_id=donor.id, name="Fresh bread", expires_in_hours=24)
    await make_food_item(owner_id=donor.id, name="Old bread", expires_in_hours=-1)
    await make_food_item(owner_id=donor.id, name="Claimed bread", status=FoodItemStatus.RESERVED)

    response = await client.get(f"{API}/food-items")

    names = [item["name"] for item in response.json()["items"]]
    assert names == [fresh.name]


async def test_get_single_item_is_public(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    response = await client.get(f"{API}/food-items/{item.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(item.id)


async def test_soft_deleted_fields_are_not_exposed(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """`is_deleted`/`deleted_at` never reach a client.

    `FoodItemRead` does not declare them, so the internal storage mechanism stays
    internal — clients are not told about rows they can never see.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    body = (await client.get(f"{API}/food-items/{item.id}")).json()

    assert "is_deleted" not in body
    assert "deleted_at" not in body


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
async def test_create_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(f"{API}/food-items", json=_payload())
    assert response.status_code == 401


async def test_create_returns_201_with_the_created_item(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    donor = await make_user()

    response = await client.post(f"{API}/food-items", headers=auth_headers(donor), json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["owner_id"] == str(donor.id)
    assert body["status"] == "AVAILABLE"
    assert body["id"]  # server-assigned, so no follow-up GET is needed


async def test_create_rejects_a_client_supplied_owner_id(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """Listing food under someone else's name is a 422.

    `FoodItemCreate` has no `owner_id`, and `extra="forbid"` rejects the attempt before
    any application code runs.
    """
    donor = await make_user()
    victim = await make_user()

    response = await client.post(
        f"{API}/food-items",
        headers=auth_headers(donor),
        json=_payload(owner_id=str(victim.id)),
    )

    assert response.status_code == 422


async def test_create_rejects_a_client_supplied_status(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """A client cannot declare its own item DONATED and skip the state machine."""
    donor = await make_user()

    response = await client.post(
        f"{API}/food-items", headers=auth_headers(donor), json=_payload(status="DONATED")
    )

    assert response.status_code == 422


async def test_create_rejects_zero_quantity(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """`gt=0` in the schema, and a CHECK constraint in the database.

    Two layers on purpose: Pydantic gives a clean 422, the constraint protects against
    a bad migration or a manual UPDATE.
    """
    donor = await make_user()

    response = await client.post(
        f"{API}/food-items", headers=auth_headers(donor), json=_payload(quantity="0")
    )

    assert response.status_code == 422


async def test_stranger_cannot_edit_another_users_item(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
    auth_headers: Any,
) -> None:
    """THE IDOR TEST, over HTTP. Valid token, real id, someone else's row -> 403."""
    donor = await make_user()
    stranger = await make_user()
    item = await make_food_item(owner_id=donor.id)

    response = await client.patch(
        f"{API}/food-items/{item.id}",
        headers=auth_headers(stranger),
        json={"name": "Hijacked"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_empty_patch_body_is_rejected(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
    auth_headers: Any,
) -> None:
    """`PATCH {}` is a 422, not a silent success.

    A no-op that reports success is the kind of bug that has clients retrying a broken
    integration for hours.
    """
    donor = await make_user()
    item = await make_food_item(owner_id=donor.id)

    response = await client.patch(
        f"{API}/food-items/{item.id}", headers=auth_headers(donor), json={}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Route ordering
# --------------------------------------------------------------------------
async def test_mine_route_is_not_swallowed_by_the_uuid_route(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
    auth_headers: Any,
) -> None:
    """`/food-items/mine` resolves to the static route, not to `/{item_id}`.

    FastAPI matches in registration order, so if `/{item_id}` were declared first this
    would fail with a 422 trying to parse "mine" as a UUID. A confusing bug with a
    one-line fix (ordering), and worth a regression test because reordering routes
    looks harmless.
    """
    donor = await make_user()
    await make_food_item(owner_id=donor.id, name="My listing")

    response = await client.get(f"{API}/food-items/mine", headers=auth_headers(donor))

    assert response.status_code == 200
    assert [i["name"] for i in response.json()["items"]] == ["My listing"]


async def test_mine_only_returns_your_own_items(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
    auth_headers: Any,
) -> None:
    """Scoped to the token, so there is nothing in the URL to tamper with."""
    donor = await make_user()
    other = await make_user()
    await make_food_item(owner_id=donor.id, name="Mine")
    await make_food_item(owner_id=other.id, name="Theirs")

    response = await client.get(f"{API}/food-items/mine", headers=auth_headers(donor))

    assert [i["name"] for i in response.json()["items"]] == ["Mine"]


async def test_mine_includes_non_available_items(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
    auth_headers: Any,
) -> None:
    """Unlike the public browse, your own list shows every status.

    You need to see your reserved and donated items; strangers do not.
    """
    donor = await make_user()
    await make_food_item(owner_id=donor.id, name="Reserved", status=FoodItemStatus.RESERVED)

    response = await client.get(f"{API}/food-items/mine", headers=auth_headers(donor))

    assert [i["status"] for i in response.json()["items"]] == ["RESERVED"]


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------
async def test_pagination_metadata_is_consistent(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """`total` counts all matches; `items` is capped at `size`.

    The two come from separate queries, and both apply `_available_filters` — the shared
    helper that stops them drifting apart. A `total` that disagrees with `items` looks
    like a pagination bug and is actually a copy-paste bug.
    """
    donor = await make_user()
    for index in range(5):
        await make_food_item(owner_id=donor.id, name=f"Item {index}")

    response = await client.get(f"{API}/food-items", params={"page": 1, "size": 2})

    body = response.json()
    assert len(body["items"]) == 2
    assert body["meta"]["total"] == 5
    assert body["meta"]["pages"] == 3
    assert body["meta"]["has_next"] is True
    assert body["meta"]["has_previous"] is False


async def test_page_size_is_capped(client: AsyncClient) -> None:
    """`size` above `MAX_PAGE_SIZE` is a 422.

    A DoS control, not an aesthetic limit: unbounded `size` lets one request materialise
    the whole table.
    """
    response = await client.get(f"{API}/food-items", params={"size": 10_000})
    assert response.status_code == 422


async def test_page_zero_is_rejected(client: AsyncClient) -> None:
    """`page` is 1-based; `page=0` would compute a negative offset."""
    response = await client.get(f"{API}/food-items", params={"page": 0})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
async def test_search_filters_by_name_case_insensitively(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    donor = await make_user()
    await make_food_item(owner_id=donor.id, name="Sourdough Bread")
    await make_food_item(owner_id=donor.id, name="Carrots")

    response = await client.get(f"{API}/food-items", params={"search": "bread"})

    assert [i["name"] for i in response.json()["items"]] == ["Sourdough Bread"]


async def test_search_wildcards_are_escaped(
    client: AsyncClient,
    make_user: Any,
    make_food_item: Any,
) -> None:
    """A literal `%` matches nothing rather than everything.

    Not an injection defence — SQLAlchemy parameterises the value. It is a correctness
    fix: an unescaped `%` in a LIKE pattern matches every row, so the user gets nonsense
    results and the database does needless work.
    """
    donor = await make_user()
    await make_food_item(owner_id=donor.id, name="Carrots")

    response = await client.get(f"{API}/food-items", params={"search": "%"})

    assert response.json()["items"] == []
