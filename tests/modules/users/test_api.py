"""Integration tests for `/auth/*` and `/users/*`.

These drive the real ASGI app: real routers, real middleware, real exception
handlers, real dependency graph. They verify the things a unit test structurally
cannot — status codes, the error envelope, authorization wiring, and response
headers.

They are also where the *controller advice* is proven: several tests below assert on
`application/problem+json` and on the `code` field, which no service test can see.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.modules.users.models import Role

pytestmark = pytest.mark.integration

VALID_PASSWORD = "Str0ngPassphrase"
API = "/api/v1"


# --------------------------------------------------------------------------
# Registration and login
# --------------------------------------------------------------------------
async def test_register_returns_201_with_user_and_tokens(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/register",
        json={"email": "new@example.com", "password": VALID_PASSWORD, "full_name": "New User"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["role"] == "DONOR"
    assert body["tokens"]["access_token"]
    # THE MOST IMPORTANT ASSERTION IN THIS FILE: the hash is not in the response.
    # `UserRead` is an allowlist that simply does not declare the field, so this holds
    # even after someone adds a column to the model.
    assert "hashed_password" not in body["user"]
    assert "password" not in body["user"]


async def test_register_rejects_client_supplied_role(client: AsyncClient) -> None:
    """Privilege escalation via the registration body is a 422.

    `UserCreate` has no `role` field and `ApiModel` sets `extra="forbid"`, so this is
    rejected before any application code runs. Self-promotion to ADMIN is not something
    we validate against — it is unexpressible.
    """
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": VALID_PASSWORD,
            "full_name": "Sneaky",
            "role": "ADMIN",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_weak_password_is_rejected_with_field_details(client: AsyncClient) -> None:
    """A weak password returns 422 with the offending field named.

    Verifies the validation handler's field-level `errors` array, which is what lets a
    client highlight the right input rather than showing a generic banner.
    """
    response = await client.post(
        f"{API}/auth/register",
        json={"email": "weak@example.com", "password": "short", "full_name": "Weak"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(error["field"] == "password" for error in body["errors"])


async def test_login_then_access_protected_endpoint(client: AsyncClient) -> None:
    """The full round trip: register, login, use the token."""
    await client.post(
        f"{API}/auth/register",
        json={"email": "flow@example.com", "password": VALID_PASSWORD, "full_name": "Flow"},
    )

    login = await client.post(
        f"{API}/auth/login",
        json={"email": "flow@example.com", "password": VALID_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "flow@example.com"


async def test_login_with_bad_credentials_returns_401_problem_json(
    client: AsyncClient,
    make_user: Any,
) -> None:
    """A failed login is 401 in the standard envelope, with `WWW-Authenticate`."""
    await make_user(email="real@example.com", password=VALID_PASSWORD)

    response = await client.post(
        f"{API}/auth/login",
        json={"email": "real@example.com", "password": "WrongPassw0rd"},
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["code"] == "AUTHENTICATION_FAILED"
    assert body["detail"] == "Incorrect email or password"


# --------------------------------------------------------------------------
# Authentication wiring
# --------------------------------------------------------------------------
async def test_protected_endpoint_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/users/me")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"


async def test_protected_endpoint_with_garbage_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/users/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


async def test_inactive_user_gets_403_not_401(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """A deactivated account is 403, not 401.

    The distinction is not cosmetic: 401 tells the client "retry with credentials",
    which sends a disabled user round a login loop they cannot win. 403 tells them the
    credentials are fine and the answer is still no.
    """
    user = await make_user(is_active=False)

    response = await client.get(f"{API}/users/me", headers=auth_headers(user))

    assert response.status_code == 403
    assert response.json()["code"] in {"PERMISSION_DENIED", "USER_INACTIVE"}


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
async def test_non_admin_cannot_list_users(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """`require_roles(Role.ADMIN)` blocks a DONOR at the route, before the handler."""
    donor = await make_user(role=Role.DONOR)

    response = await client.get(f"{API}/users", headers=auth_headers(donor))

    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_admin_can_list_users(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    admin = await make_user(role=Role.ADMIN)
    await make_user(role=Role.DONOR)

    response = await client.get(f"{API}/users", headers=auth_headers(admin))

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] >= 2
    assert "items" in body
    # Computed pagination fields are present and consistent.
    assert body["meta"]["pages"] >= 1
    assert body["meta"]["has_previous"] is False


async def test_user_cannot_read_another_users_profile(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """THE IDOR TEST. User A must not be able to read user B by id.

    This is the vulnerability that automated scanners miss and happy-path tests never
    reach: the URL is well formed, the token is valid, the row exists. Only
    `require_ownership` stops it.
    """
    alice = await make_user()
    bob = await make_user()

    response = await client.get(f"{API}/users/{bob.id}", headers=auth_headers(alice))

    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_user_can_read_own_profile_by_id(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    alice = await make_user()

    response = await client.get(f"{API}/users/{alice.id}", headers=auth_headers(alice))

    assert response.status_code == 200
    assert response.json()["id"] == str(alice.id)


async def test_admin_can_read_any_profile(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """Admins override ownership — and the override is logged (see permissions.py)."""
    admin = await make_user(role=Role.ADMIN)
    bob = await make_user()

    response = await client.get(f"{API}/users/{bob.id}", headers=auth_headers(admin))

    assert response.status_code == 200


async def test_admin_can_change_a_role(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    admin = await make_user(role=Role.ADMIN)
    donor = await make_user(role=Role.DONOR)

    response = await client.patch(
        f"{API}/users/{donor.id}/role",
        headers=auth_headers(admin),
        json={"role": "RECIPIENT"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "RECIPIENT"


async def test_donor_cannot_change_a_role(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """The escalation path is closed at the route, not in the service."""
    donor = await make_user(role=Role.DONOR)
    victim = await make_user(role=Role.DONOR)

    response = await client.patch(
        f"{API}/users/{victim.id}/role",
        headers=auth_headers(donor),
        json={"role": "ADMIN"},
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Controller advice / error envelope
# --------------------------------------------------------------------------
async def test_404_uses_the_same_problem_json_envelope(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """Every error shares one shape, including `request_id`.

    Verifies the whole controller-advice chain: a domain `NotFoundError` raised in a
    service is translated by `app_error_handler` into RFC 9457, and the `request_id`
    matches the `X-Request-ID` response header set by the middleware — which is what
    makes a user's bug report greppable in the logs.
    """
    admin = await make_user(role=Role.ADMIN)
    missing_id = "00000000-0000-0000-0000-000000000000"

    response = await client.get(f"{API}/users/{missing_id}", headers=auth_headers(admin))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    for field in ("type", "title", "status", "detail", "instance", "code", "request_id"):
        assert field in body, f"missing {field} in problem+json body"

    assert body["status"] == 404
    assert body["code"] == "USER_NOT_FOUND"
    assert body["instance"] == f"{API}/users/{missing_id}"
    # The correlation id in the body matches the header.
    assert body["request_id"] == response.headers["x-request-id"]


async def test_malformed_uuid_is_422_in_the_same_envelope(
    client: AsyncClient,
    make_user: Any,
    auth_headers: Any,
) -> None:
    """FastAPI's own validation error is normalised into our envelope too.

    Without `validation_error_handler`, 422 would be the one error shape clients have
    to special-case — which defeats the point of having a single envelope.
    """
    admin = await make_user(role=Role.ADMIN)

    response = await client.get(f"{API}/users/not-a-uuid", headers=auth_headers(admin))

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "request_id" in body


async def test_unknown_route_is_404_in_the_same_envelope(client: AsyncClient) -> None:
    """Even a typo'd URL returns the standard shape.

    Starlette's built-in 404 would otherwise produce `{"detail": "Not Found"}` — a
    different shape from every real error. `http_exception_handler` re-wraps it.
    """
    response = await client.get(f"{API}/no-such-endpoint")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------
async def test_security_headers_are_present(client: AsyncClient) -> None:
    """`SecurityHeadersMiddleware` applies to every response, errors included."""
    response = await client.get(f"{API}/food-items")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers
    assert "referrer-policy" in response.headers
    # HSTS is production-only — sending it over plain HTTP in dev would pin
    # localhost to https in the developer's browser for a year.
    assert "strict-transport-security" not in response.headers


async def test_request_id_is_echoed_and_generated(client: AsyncClient) -> None:
    """A correlation id is always returned, generated when absent."""
    response = await client.get(f"{API}/food-items")
    assert response.headers.get("x-request-id")


async def test_supplied_request_id_is_adopted(client: AsyncClient) -> None:
    """An upstream `X-Request-ID` is reused, so a trace can span services."""
    response = await client.get(f"{API}/food-items", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["x-request-id"] == "trace-abc-123"


async def test_response_time_header_is_present(client: AsyncClient) -> None:
    response = await client.get(f"{API}/food-items")
    assert float(response.headers["x-response-time-ms"]) >= 0


# --------------------------------------------------------------------------
# Token lifecycle over HTTP
# --------------------------------------------------------------------------
async def test_refresh_endpoint_rotates_tokens(client: AsyncClient) -> None:
    """`POST /auth/refresh` returns a new pair.

    NOTE: replay rejection is NOT asserted here, because the `client` fixture wires a
    `NullTokenDenylist` through the default service provider. That behaviour is covered
    in `test_service.py::test_refresh_rotates_and_invalidates_the_old_token` with a real
    denylist. Stated rather than left as a silent gap.
    """
    await client.post(
        f"{API}/auth/register",
        json={"email": "refresh@example.com", "password": VALID_PASSWORD, "full_name": "R"},
    )
    login = await client.post(
        f"{API}/auth/login",
        json={"email": "refresh@example.com", "password": VALID_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 200
    assert response.json()["access_token"] != login.json()["access_token"]


async def test_access_token_rejected_at_refresh_endpoint(client: AsyncClient) -> None:
    """An access token cannot be used to refresh — the `typ` claim in action, over HTTP."""
    await client.post(
        f"{API}/auth/register",
        json={"email": "typ@example.com", "password": VALID_PASSWORD, "full_name": "T"},
    )
    login = await client.post(
        f"{API}/auth/login", json={"email": "typ@example.com", "password": VALID_PASSWORD}
    )

    response = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": login.json()["access_token"]}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"
