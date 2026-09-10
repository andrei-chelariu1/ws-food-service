"""Tests for the application factory's own routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_root_returns_a_service_banner(client: AsyncClient) -> None:
    """`/` answers "what is this and where do I go next", not "Hello World"."""
    response = await client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"]
    assert body["api"] == "/api/v1"
    assert body["health"] == "/health/ready"


async def test_root_advertises_docs_only_when_they_exist(client: AsyncClient) -> None:
    """The key must be absent rather than pointing at a 404.

    `docs_url` is None in production, so a hardcoded "/docs" would send every
    reader of the banner to a disabled endpoint.
    """
    body = (await client.get("/")).json()
    docs_url = (await client.get("/openapi.json")).status_code

    # The test app runs with ENVIRONMENT=development, so docs exist here.
    assert docs_url == 200
    assert body["docs"] == "/docs"


async def test_root_is_not_part_of_the_api_contract(client: AsyncClient) -> None:
    """A banner is not an endpoint anyone should code against."""
    schema = (await client.get("/openapi.json")).json()

    assert "/" not in schema["paths"]


async def test_root_is_not_a_health_check(client: AsyncClient) -> None:
    """It touches nothing external, so it must not be mistaken for a probe.

    Pinned because pointing an orchestrator at `/` is an easy mistake that hides
    a broken database behind a 200.
    """
    body = (await client.get("/")).json()

    assert "database" not in body
    assert "redis" not in body
    assert "status" not in body
