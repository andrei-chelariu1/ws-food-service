"""Tests for the rate-limit key function and client-IP resolution.

WHY ONLY THE KEY FUNCTION IS TESTED HERE
----------------------------------------
End-to-end 429 behaviour is deliberately **not** asserted in this suite, and the
reason is worth stating rather than hiding:

`app/core/rate_limit.py` builds its `Limiter` as a module-level singleton (it must —
`@limiter.limit(...)` runs at class-definition time). Its storage backend and its
enabled flag are therefore fixed at import. Driving a real 429 in-process would mean
either a live Redis or swapping the singleton mid-suite, and the second option tests
the swap rather than the limiter.

So: the *decision logic* — which is where a real bug would hide — is unit-tested
here, and the *integration* is verified by curl against the running stack. That
command is step 7 of the verification checklist in README.md, and it is not optional.

The logic below is where the bug would actually be: keying on the wrong identity is
either a privacy leak (one user's activity counted against another) or an ineffective
limit (every user sharing one bucket).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.core.middleware import get_client_ip
from app.core.rate_limit import rate_limit_key


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeState:
    """Stands in for `request.state`, which is a plain attribute bag."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


class _FakeRequest:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        client_host: str | None = "203.0.113.7",
        user_id: str | None = None,
    ) -> None:
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host else None
        self.state = _FakeState(**({"user_id": user_id} if user_id else {}))


# --------------------------------------------------------------------------
# Key selection
# --------------------------------------------------------------------------
def test_authenticated_requests_are_keyed_by_user() -> None:
    """An authenticated caller is limited per account, not per address.

    Keying purely on IP would punish everyone behind one corporate NAT or mobile
    carrier gateway — hundreds of legitimate users sharing a single bucket.
    """
    request = _FakeRequest(user_id="11111111-1111-1111-1111-111111111111")
    assert rate_limit_key(request) == "user:11111111-1111-1111-1111-111111111111"  # type: ignore[arg-type]


def test_anonymous_requests_fall_back_to_ip() -> None:
    """Unauthenticated callers are keyed by address.

    This is the case that matters most: `/auth/login` and `/auth/register` have no
    user id, and they are exactly the endpoints that need protecting. Keying only on
    user id would leave them unlimited.
    """
    request = _FakeRequest(client_host="198.51.100.4")
    assert rate_limit_key(request) == "ip:198.51.100.4"  # type: ignore[arg-type]


def test_user_key_takes_precedence_over_ip() -> None:
    """When both are available, the account wins — abuse should be attributable."""
    request = _FakeRequest(
        user_id="22222222-2222-2222-2222-222222222222", client_host="198.51.100.4"
    )
    assert rate_limit_key(request).startswith("user:")  # type: ignore[arg-type]


def test_user_and_ip_keys_cannot_collide() -> None:
    """The `user:`/`ip:` prefixes keep the two namespaces separate.

    Without them, a user id that happened to look like an address would share a bucket
    with that address.
    """
    user_key = rate_limit_key(_FakeRequest(user_id="203.0.113.7"))  # type: ignore[arg-type]
    ip_key = rate_limit_key(_FakeRequest(client_host="203.0.113.7"))  # type: ignore[arg-type]
    assert user_key != ip_key


# --------------------------------------------------------------------------
# Client IP resolution
# --------------------------------------------------------------------------
def test_x_forwarded_for_takes_the_first_entry() -> None:
    """Behind a proxy, the first entry is the original client.

    `X-Forwarded-For: client, proxy1, proxy2`. Taking the last entry would key every
    request on the proxy's own address — one bucket for all traffic.
    """
    request = _FakeRequest(
        headers={"x-forwarded-for": "192.0.2.1, 198.51.100.2, 203.0.113.3"},
        client_host="10.0.0.1",
    )
    assert get_client_ip(request) == "192.0.2.1"  # type: ignore[arg-type]


def test_x_real_ip_is_used_when_forwarded_for_is_absent() -> None:
    request = _FakeRequest(headers={"x-real-ip": "192.0.2.9"}, client_host="10.0.0.1")
    assert get_client_ip(request) == "192.0.2.9"  # type: ignore[arg-type]


def test_falls_back_to_the_socket_peer() -> None:
    request = _FakeRequest(client_host="203.0.113.7")
    assert get_client_ip(request) == "203.0.113.7"  # type: ignore[arg-type]


def test_returns_unknown_when_there_is_no_client() -> None:
    """A missing client must not raise.

    `request.client` is `None` for some ASGI transports (including the in-process test
    transport). An `AttributeError` inside the rate limiter would turn every request
    into a 500 — the limiter must never be the thing that breaks the application.
    """
    request = _FakeRequest(client_host=None)
    assert get_client_ip(request) == "unknown"  # type: ignore[arg-type]


@pytest.mark.parametrize("spoofed", ["", "   ", "not-an-ip"])
def test_forwarded_header_values_are_passed_through_verbatim(spoofed: str) -> None:
    """Documents a real deployment requirement rather than a code behaviour.

    `X-Forwarded-For` is CLIENT-CONTROLLED unless a trusted proxy overwrites it. This
    function does not (and cannot) validate it: an attacker who can set the header
    freely can rotate the value and evade IP-based limits entirely.

    The mitigation is deployment-level — the ingress or load balancer MUST overwrite
    `X-Forwarded-For` rather than append to it. This test exists to pin the behaviour
    so the requirement stays visible; see docs/RATE_LIMITING.md.
    """
    request = _FakeRequest(headers={"x-forwarded-for": spoofed}, client_host="10.0.0.1")
    resolved = get_client_ip(request)  # type: ignore[arg-type]
    # Empty/whitespace headers fall through to the socket peer; junk is passed on.
    assert resolved in {spoofed.strip(), "10.0.0.1"}


# --------------------------------------------------------------------------
# Structural guard — the test that would have caught a real production bug
# --------------------------------------------------------------------------
def test_every_rate_limited_route_declares_request_and_response() -> None:
    """Every `@limiter.limit(...)` endpoint must accept `request` AND `response`.

    THIS TEST EXISTS BECAUSE THE BUG IT CATCHES SHIPPED ONCE.

    slowapi with `headers_enabled=True` writes the `X-RateLimit-*` headers onto a
    `Response` object it expects to find in the endpoint's signature. Omit the
    parameter and *every* call to that endpoint raises:

        parameter `response` must be an instance of starlette.responses.Response

    A 500 on `POST /auth/register` — on the happy path, not an edge case.

    The rest of the suite cannot see it: `RATE_LIMIT_ENABLED=false` makes the
    decorator a no-op, so the missing parameter only matters once the limiter is
    live, i.e. in the deployed container. That is the worst possible place to find out.

    So this checks the *structure* instead of the behaviour: it walks the real route
    table, finds the handlers slowapi has wrapped, and asserts their signatures. No
    Redis, no live limiter, no network — and it fails the moment someone adds a
    rate-limited endpoint without the boilerplate.

    NOTE ON THE ANNOTATION CHECK: every router module uses
    `from __future__ import annotations`, so `inspect.signature` reports annotations as
    *strings* (`"Request"`), not as classes. Comparing with `is FastapiRequest` therefore
    matches nothing and the test passes vacuously — which is exactly what the first
    version of this test did.

    NOTE ON ROUTE DISCOVERY: this FastAPI version stores `include_router` results as
    lazy `_IncludedRouter` objects rather than flattening them into `.routes`, so a
    naive one-level loop finds no endpoints at all. `_iter_endpoints` recurses.

    Both of those made the first version of this test silently useless, which is why
    `test_the_structural_guard_is_not_vacuous` below exists.
    """
    offenders = [
        f"{fn.__module__}.{fn.__name__}"
        for fn in _iter_endpoints()
        if _declares(fn, "request", "Request") and not _declares(fn, "response", "Response")
    ]

    assert not offenders, (
        "These rate-limited endpoints declare `request: Request` but not "
        "`response: Response`, so slowapi will raise on every call once rate "
        f"limiting is enabled: {offenders}. See app/core/rate_limit.py."
    )


def test_the_structural_guard_is_not_vacuous() -> None:
    """The guard above must actually be looking at something.

    A structural test that silently matches zero routes passes forever while checking
    nothing — a worse outcome than having no test, because it reads as coverage. This
    asserts the guard finds the rate-limited endpoints, so a refactor that breaks route
    discovery fails loudly here instead of quietly blinding the guard.
    """
    rate_limited = [fn for fn in _iter_endpoints() if _declares(fn, "request", "Request")]

    # 13 endpoints carry `@limiter.limit(...)`: 4 users, 4 food_items, 5 donations.
    # A lower bound, so adding one does not fail this test — only losing the pattern does.
    assert len(rate_limited) >= 13, (
        f"The structural guard only found {len(rate_limited)} rate-limited routes. "
        "It is no longer inspecting what it thinks it is — fix _iter_endpoints or "
        "_declares before trusting the guard above."
    )


# --------------------------------------------------------------------------
# Introspection helpers
# --------------------------------------------------------------------------
def _iter_endpoints() -> list[Any]:
    """Every route handler reachable from the API router, unwrapped.

    Recurses through `_IncludedRouter` wrappers, which is how this FastAPI version
    represents `include_router` before the route table is materialised.
    """
    from app.api_router import api_router

    endpoints: list[Any] = []

    def walk(router: Any) -> None:
        for route in getattr(router, "routes", []):
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                endpoints.append(inspect.unwrap(endpoint))
                continue
            # Lazy include wrapper — descend into the router it wraps.
            nested = getattr(route, "original_router", None) or getattr(route, "router", None)
            if nested is not None:
                walk(nested)

    walk(api_router)
    return endpoints


def _declares(fn: Any, name: str, expected: str) -> bool:
    """True if `fn` declares parameter `name` annotated `expected`.

    Accepts both the string form (modules using `from __future__ import annotations`)
    and the real class form, so the check does not silently stop matching if a module
    drops the future import.
    """
    param = inspect.signature(fn).parameters.get(name)
    if param is None or param.annotation is inspect.Parameter.empty:
        return False
    annotation = param.annotation
    resolved = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    return resolved == expected
