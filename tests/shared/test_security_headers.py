"""Security-header tests, with a regression guard for the /docs CSP.

The strict `default-src 'none'` policy is correct for a JSON API and *wrong* for
the Swagger UI page FastAPI serves, which bootstraps itself with an inline
script and pulls its bundle from a CDN. Applying one policy to both produced a
blank /docs page and three CSP console errors.

These tests pin both halves of the fix: the API stays strict, the docs pages get
exactly the allowances they need, and the relaxation disappears in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.middleware import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from httpx import AsyncClient


def _csp(headers: object) -> str:
    value = headers["content-security-policy"]  # type: ignore[index]
    assert isinstance(value, str)
    return value


async def test_api_responses_get_the_strict_policy(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert _csp(response.headers) == "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


async def test_error_responses_also_get_the_headers(client: AsyncClient) -> None:
    """The middleware wraps everything, so a 404 is covered too.

    A policy that only applies to successful responses is not a policy.
    """
    response = await client.get("/no-such-route")

    assert response.status_code == 404
    assert "content-security-policy" in response.headers


async def test_docs_page_renders_under_its_own_policy(client: AsyncClient) -> None:
    response = await client.get("/docs")
    assert response.status_code == 200

    policy = _csp(response.headers)
    body = response.text

    # Everything the served HTML actually needs. Asserted against the real
    # response body so this fails if FastAPI changes its CDN or its bootstrap.
    assert "'unsafe-inline'" in policy
    assert "cdn.jsdelivr.net" in policy
    assert "fastapi.tiangolo.com" in policy

    for host in ("cdn.jsdelivr.net", "fastapi.tiangolo.com"):
        assert host in body, f"{host} no longer referenced by /docs — narrow the CSP"


async def test_docs_relaxation_does_not_leak_to_the_api(client: AsyncClient) -> None:
    """The whole point: /docs is an exception, not a global downgrade."""
    docs = await client.get("/docs")
    api = await client.get("/health/live")

    assert "'unsafe-inline'" in _csp(docs.headers)
    assert "'unsafe-inline'" not in _csp(api.headers)


@pytest.mark.parametrize("path", ["/openapi.json", "/health/ready"])
async def test_json_endpoints_stay_strict(client: AsyncClient, path: str) -> None:
    """`/openapi.json` is fetched BY the docs page but is not itself a document.

    Its own policy can stay strict — CSP governs the page doing the loading, and
    the docs policy permits `connect-src 'self'`.
    """
    response = await client.get(path)

    assert "'unsafe-inline'" not in _csp(response.headers)


def test_production_has_no_docs_exception() -> None:
    """With docs disabled, `docs_paths` is empty and the branch is unreachable.

    This is the structural argument that the relaxation is development-only:
    it is not a flag someone can forget to turn off, it is derived from whether
    the pages exist at all.
    """
    middleware = SecurityHeadersMiddleware(
        app=lambda *args: None,  # type: ignore[arg-type,return-value]
        enable_hsts=True,
        docs_paths=frozenset(),
    )

    assert middleware._docs_paths == frozenset()
    assert b"unsafe-inline" not in dict(middleware._headers)[b"content-security-policy"]


def test_hsts_is_production_only() -> None:
    """Sending HSTS over plain HTTP in dev pins localhost to https for a year."""
    dev = SecurityHeadersMiddleware(
        app=lambda *args: None,  # type: ignore[arg-type,return-value]
        enable_hsts=False,
    )
    prod = SecurityHeadersMiddleware(
        app=lambda *args: None,  # type: ignore[arg-type,return-value]
        enable_hsts=True,
    )

    assert b"strict-transport-security" not in dict(dev._headers)
    assert b"strict-transport-security" in dict(prod._headers)
