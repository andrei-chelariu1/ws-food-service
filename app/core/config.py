"""Typed application configuration.

WHY THIS FILE EXISTS
--------------------
This is the *only* place in the codebase that reads environment variables.
Everything else receives a `Settings` object. That gives us:

* **Single Responsibility** — one class owns "where configuration comes from".
  Nothing else has to know whether a value is from `.env`, a real env var, or
  a Kubernetes secret.
* **Fail fast** — Pydantic validates types and required fields at *startup*.
  A typo'd `DB_POOL_SIZE=ten` crashes on boot with a clear message, instead of
  producing a `TypeError` under load three hours later.
* **Testability** — tests construct `Settings(...)` directly, or override the
  `get_settings` dependency. No monkeypatching of `os.environ`.

If you find yourself writing `os.getenv(...)` anywhere else, add a field here
instead.
"""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]

# The literal placeholder shipped in .env.example. Refusing to boot in
# production with this value is cheap insurance against the single most common
# real-world deployment mistake.
INSECURE_SECRET_KEY_SENTINEL = "change-me-in-production-openssl-rand-hex-32"  # noqa: S105


class Settings(BaseSettings):
    """All runtime configuration, validated once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Unknown keys in .env are ignored rather than fatal: the same file is
        # shared with docker-compose, which needs POSTGRES_* vars we don't read.
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    ENVIRONMENT: Environment = "development"
    DEBUG: bool = True
    APP_NAME: str = "Food Waste App"
    API_V1_PREFIX: str = "/api/v1"

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_JSON: bool = False

    # --- Security ---------------------------------------------------------
    SECRET_KEY: SecretStr = SecretStr(INSECURE_SECRET_KEY_SENTINEL)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=15, ge=1, le=1440)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, ge=1, le=90)
    # 12 is the practical floor for production. Tests drop this to 4 for speed.
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=16)

    CORS_ORIGINS: list[str] = Field(default_factory=list)
    ALLOWED_HOSTS: list[str] = Field(default_factory=lambda: ["*"])

    # --- Database ---------------------------------------------------------
    DATABASE_URL: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://app:app@localhost:5432/foodwaste")
    )
    DB_POOL_SIZE: int = Field(default=10, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0)
    DB_POOL_PRE_PING: bool = True
    DB_ECHO: bool = False

    # --- Redis ------------------------------------------------------------
    REDIS_URL: RedisDsn = Field(default=RedisDsn("redis://localhost:6379/0"))
    CACHE_DEFAULT_TTL_SECONDS: int = Field(default=60, ge=1)

    # --- Rate limiting ----------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "200/minute"
    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_REGISTER: str = "3/hour"
    RATE_LIMIT_WRITE: str = "30/minute"

    # --- Seed data --------------------------------------------------------
    # `.example`, not `.local`: `EmailStr` (email-validator) rejects special-use
    # TLDs, so a `.local` seed address produces an admin who cannot log in.
    SEED_ADMIN_EMAIL: str = "admin@foodwaste.example"
    SEED_ADMIN_PASSWORD: SecretStr = SecretStr("ChangeMe123!")

    # --- Derived values ---------------------------------------------------
    # `computed_field` keeps derivations next to the data they derive from,
    # instead of scattering `settings.ENVIRONMENT == "production"` checks
    # across the codebase (DRY).

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_str(self) -> str:
        """Plain string form, for SQLAlchemy and Alembic."""
        return str(self.DATABASE_URL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url_str(self) -> str:
        return str(self.REDIS_URL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_url(self) -> str | None:
        """Swagger is disabled in production — it is API surface, not docs."""
        return None if self.is_production else "/docs"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redoc_url(self) -> str | None:
        return None if self.is_production else "/redoc"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def openapi_url(self) -> str | None:
        return None if self.is_production else "/openapi.json"

    # --- Validators -------------------------------------------------------

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def _parse_list(cls, value: object) -> object:
        """Accept a JSON array, a CSV string, or a real list.

        CSV is accepted because that is what most CI systems and Kubernetes manifests
        produce, and refusing to boot over a formatting detail is a bad trade.

        WHY THE JSON BRANCH PARSES EXPLICITLY instead of deferring to Pydantic:
        pydantic-settings JSON-decodes complex types only in the *environment* source.
        A directly constructed `Settings(CORS_ORIGINS='["http://a"]')` — which is how
        tests and any programmatic caller build one — never reaches that decoder and
        fails with `Input should be a valid list`. Parsing here makes both paths behave
        identically, which is the whole point of having one configuration entry point.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Fall through to CSV rather than failing: a value like "[a,b" is more
                # likely a hand-edited mistake than an intended JSON document.
                pass
        return [item.strip() for item in text.split(",") if item.strip()]

    def assert_production_safe(self) -> None:
        """Refuse to run an insecure configuration in production.

        Called from the app lifespan rather than from a validator: validators
        run during test construction too, and tests legitimately use the
        default key. This keeps the guard where it belongs — at boot.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if self.SECRET_KEY.get_secret_value() == INSECURE_SECRET_KEY_SENTINEL:
            problems.append("SECRET_KEY is still the .env.example placeholder")
        if len(self.SECRET_KEY.get_secret_value()) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        if self.DEBUG:
            problems.append("DEBUG must be false in production")
        if "*" in self.ALLOWED_HOSTS:
            problems.append("ALLOWED_HOSTS must not contain '*' in production")
        if "*" in self.CORS_ORIGINS:
            problems.append("CORS_ORIGINS must not contain '*' in production")
        if self.BCRYPT_ROUNDS < 12:
            problems.append("BCRYPT_ROUNDS must be >= 12 in production")
        if self.SEED_ADMIN_PASSWORD.get_secret_value() == "ChangeMe123!":
            problems.append("SEED_ADMIN_PASSWORD is still the sample value")

        if problems:
            raise RuntimeError(
                "Refusing to start with an insecure production configuration:\n  - "
                + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor, also usable as a FastAPI dependency.

    `lru_cache` makes this a lazily-created singleton: the `.env` file is read
    once per process, and every caller sees the same object. Tests clear the
    cache (`get_settings.cache_clear()`) or override the dependency.
    """
    return Settings()


def generate_secret_key() -> str:
    """Generate a production-grade SECRET_KEY.

    For operators:
        python -c "from app.core.config import generate_secret_key as g; print(g())"
    """
    return secrets.token_hex(32)
