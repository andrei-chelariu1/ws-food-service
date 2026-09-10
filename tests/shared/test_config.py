"""Tests for `Settings` — configuration correctness and the production guard."""

from __future__ import annotations

import pytest

from app.core.config import INSECURE_SECRET_KEY_SENTINEL, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "ENVIRONMENT": "development",
        "SECRET_KEY": "a" * 40,
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The seeded admin must be able to log in
# --------------------------------------------------------------------------
def test_seed_admin_email_is_accepted_by_the_login_schema() -> None:
    """The seeded admin address must pass the same validation the login endpoint applies.

    THIS TEST EXISTS BECAUSE THE DEFAULT WAS WRONG. `admin@foodwaste.local` looks
    perfectly reasonable, and migration 0002 creates the row happily — but
    `LoginRequest.email` is an `EmailStr`, and email-validator rejects special-use TLDs
    (`.local`, `.test`, `.invalid`, `.localhost`). So the seeded administrator got a
    **422 from the login endpoint's own validation** and could never authenticate.

    The result is the worst kind of bug: `docker compose up` succeeds, the migration
    reports success, the row is visibly present in psql — and the one account that
    exists to bootstrap the system is unusable. Nothing in the unit suite could see it,
    because nothing else ever fed that address through `EmailStr`.

    Found by actually logging in as the seeded admin against the running stack. This
    test closes the loop.
    """
    from app.modules.users.schemas import LoginRequest

    settings = _settings()

    # Would raise ValidationError if the configured address were unusable.
    request = LoginRequest(email=settings.SEED_ADMIN_EMAIL, password="whatever")  # type: ignore[arg-type]
    assert request.email == settings.SEED_ADMIN_EMAIL.lower()


def test_seed_admin_email_is_stored_lowercase_comparable() -> None:
    """Migration 0002 inserts `.lower()`, and `LoginRequest` normalises the same way,
    so a mixed-case configured value still matches on login."""
    from app.modules.users.schemas import LoginRequest

    settings = _settings(SEED_ADMIN_EMAIL="Admin@FoodWaste.Example")
    assert (
        LoginRequest(email=settings.SEED_ADMIN_EMAIL, password="x").email  # type: ignore[arg-type]
        == settings.SEED_ADMIN_EMAIL.lower()
    )


# --------------------------------------------------------------------------
# Derived values
# --------------------------------------------------------------------------
def test_docs_are_disabled_in_production() -> None:
    """Swagger is API surface and a free schema dump. Off in production."""
    assert _settings(ENVIRONMENT="development").docs_url == "/docs"
    assert _settings(ENVIRONMENT="production", DEBUG=False).docs_url is None
    assert _settings(ENVIRONMENT="production", DEBUG=False).openapi_url is None


def test_csv_and_json_list_forms_are_both_accepted() -> None:
    """CI systems and Kubernetes manifests emit CSV; failing to boot over a formatting
    detail is a bad trade."""
    assert _settings(CORS_ORIGINS="http://a.com,http://b.com").CORS_ORIGINS == [
        "http://a.com",
        "http://b.com",
    ]
    assert _settings(CORS_ORIGINS='["http://a.com"]').CORS_ORIGINS == ["http://a.com"]


# --------------------------------------------------------------------------
# The production safety guard
# --------------------------------------------------------------------------
def test_development_never_trips_the_guard() -> None:
    """Tests and local runs legitimately use insecure defaults, which is why the guard
    lives in a method called from lifespan rather than in a validator."""
    _settings(SECRET_KEY=INSECURE_SECRET_KEY_SENTINEL).assert_production_safe()


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"SECRET_KEY": INSECURE_SECRET_KEY_SENTINEL}, "placeholder"),
        ({"SECRET_KEY": "short"}, "at least 32"),
        ({"DEBUG": True}, "DEBUG"),
        ({"ALLOWED_HOSTS": ["*"]}, "ALLOWED_HOSTS"),
        ({"CORS_ORIGINS": ["*"]}, "CORS_ORIGINS"),
        ({"BCRYPT_ROUNDS": 4}, "BCRYPT_ROUNDS"),
        ({"SEED_ADMIN_PASSWORD": "ChangeMe123!"}, "SEED_ADMIN_PASSWORD"),
    ],
)
def test_production_refuses_insecure_configuration(
    overrides: dict[str, object],
    expected_fragment: str,
) -> None:
    """Each of these is a real deployment mistake, and each must stop the process.

    Failing at boot means the orchestrator never routes traffic to the container. The
    alternative — starting anyway — is a production service with a known-public signing
    key, and nobody finds out until it matters.
    """
    # Merged as one dict: `**overrides` alongside explicit keywords would raise
    # TypeError on any key that appears in both, which is most of them.
    production: dict[str, object] = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "SECRET_KEY": "b" * 64,
        "ALLOWED_HOSTS": ["api.example.com"],
        "CORS_ORIGINS": ["https://app.example.com"],
        "BCRYPT_ROUNDS": 12,
        "SEED_ADMIN_PASSWORD": "a-real-secret",
    }
    settings = _settings(**{**production, **overrides})

    with pytest.raises(RuntimeError, match="insecure production configuration") as exc_info:
        settings.assert_production_safe()

    assert expected_fragment in str(exc_info.value)


def test_a_correct_production_configuration_boots() -> None:
    """The guard must not be so strict that a legitimate deployment cannot start."""
    _settings(
        ENVIRONMENT="production",
        DEBUG=False,
        SECRET_KEY="b" * 64,
        ALLOWED_HOSTS=["api.example.com"],
        CORS_ORIGINS=["https://app.example.com"],
        BCRYPT_ROUNDS=12,
        SEED_ADMIN_PASSWORD="a-real-secret",
    ).assert_production_safe()
