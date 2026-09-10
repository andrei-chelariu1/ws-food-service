# Food Waste App — Backend Architecture (Python)

## Multi-Layer Architecture — Feature-Based Modules + Shared Module

We will adopt a multi-layer architecture for better separation of concerns, which will help us choose the right tools to speed up development.

---

## Project Structure

```
food-waste-app/
├── app/
│   ├── main.py                      # FastAPI app entrypoint, mounts routers
│   ├── core/                        # cross-cutting app config (part of "shared")
│   │   ├── config.py                # Pydantic Settings
│   │   ├── security.py              # JWT, password hashing
│   │   ├── logging.py               # structlog setup
│   │   ├── exceptions.py            # global exception handlers
│   │   └── middleware.py            # CORS, rate limit, request-id
│   │
│   ├── shared/                      # SHARED MODULE (cross-feature reusable code)
│   │   ├── db/
│   │   │   ├── base.py              # SQLAlchemy Base, naming conventions
│   │   │   ├── session.py           # async engine + session factory
│   │   │   └── mixins.py            # TimestampMixin, UUIDMixin, SoftDeleteMixin
│   │   ├── repository/
│   │   │   └── base_repository.py   # Generic CRUD repository (Spring Data JpaRepository<T,ID> equivalent)
│   │   ├── schemas/
│   │   │   └── base_schema.py       # BaseResponseModel, Pagination, ErrorResponse
│   │   ├── security/
│   │   │   ├── dependencies.py      # get_current_user, require_role()
│   │   │   └── permissions.py       # RBAC helpers
│   │   └── utils/
│   │       ├── pagination.py
│   │       └── datetime_utils.py
│   │
│   ├── modules/                     # FEATURE MODULES (vertical slices)
│   │   ├── users/
│   │   │   ├── api.py               # router / controller layer
│   │   │   ├── schemas.py           # Pydantic DTOs (request/response)
│   │   │   ├── models.py            # SQLAlchemy entity
│   │   │   ├── repository.py        # extends BaseRepository
│   │   │   ├── service.py           # business logic
│   │   │   └── exceptions.py        # module-specific exceptions
│   │   │
│   │   ├── food_items/
│   │   │   ├── api.py
│   │   │   ├── schemas.py
│   │   │   ├── models.py
│   │   │   ├── repository.py
│   │   │   ├── service.py
│   │   │   └── exceptions.py
│   │   │
│   │   ├── donations/
│   │   │   ├── api.py
│   │   │   ├── schemas.py
│   │   │   ├── models.py
│   │   │   ├── repository.py
│   │   │   ├── service.py
│   │   │   └── exceptions.py
│   │   │
│   │   └── notifications/
│   │       ├── api.py
│   │       ├── schemas.py
│   │       ├── service.py
│   │       └── tasks.py             # Celery/arq background tasks
│   │
│   └── api_router.py                # aggregates all module routers into /api/v1
│
├── migrations/                      # Alembic (imports models from all modules)
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── modules/
│   │   ├── users/
│   │   ├── food_items/
│   │   └── donations/
│   └── shared/
│
├── pyproject.toml
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

**Why this structure:** each module is a self-contained vertical slice (package-by-feature style, similar to `com.company.app.foodwaste.*` in Java), while `shared/` holds generic/reusable plumbing (like a common `core`/`commons` module in a multi-module Maven/Gradle project). This scales better than layering by technical type (`all_controllers/`, `all_services/`) as the app grows.

---

## Libraries by Layer

### API / Controller Layer

| Library | Why needed |
|---|---|
| **fastapi** | Main framework — routing, DI, auto OpenAPI/Swagger |
| **uvicorn[standard]** | ASGI server for local dev |
| **pydantic** | Request/response validation & serialization |
| **pydantic-settings** | Config from `.env` / env vars |
| **python-multipart** | File uploads (e.g. product photos), form parsing (OAuth2 login form) |
| **email-validator** | Email field validation in Pydantic schemas |

### Service Layer (business logic)

| Library | Why needed |
|---|---|
| **structlog** or **loguru** | Structured JSON logging, per-request context |
| **tenacity** | Retry logic for external API/service calls |
| **dependency-injector** (optional) | Formal DI container if `Depends()` isn't enough |
| **celery** + **redis**, or **arq** | Background jobs (expiry notifications, cleanup) |

### Repository Layer (data access)

| Library | Why needed |
|---|---|
| **sqlalchemy** (2.0, async) | ORM — entities, relationships, unit of work, sessions (Hibernate equivalent) |
| **asyncpg** | Async PostgreSQL driver used by SQLAlchemy async |
| **greenlet** | Required internally by SQLAlchemy's async engine |
| **alembic** | DB schema migrations (Flyway/Liquibase equivalent) |
| **psycopg2-binary** | Sync PostgreSQL driver (Alembic migrations run sync) |

### Model Layer

| Library | Why needed |
|---|---|
| **sqlmodel** (optional) | Merge Pydantic + SQLAlchemy models, reduces DTO/Entity duplication |

### Security (cross-cutting, in `shared/`)

| Library | Why needed |
|---|---|
| **python-jose[cryptography]** | JWT creation/validation |
| **passlib[bcrypt]** | Password hashing |
| **casbin** (optional) | RBAC/ABAC authorization |
| **slowapi** | Rate limiting per endpoint |

### Testing

| Library | Why needed |
|---|---|
| **pytest** | Test framework |
| **pytest-asyncio** | Test async functions |
| **httpx** | Async HTTP client for integration tests via `TestClient` |
| **pytest-cov** | Coverage reports |
| **factory-boy** / **faker** | Test data generation |
| **pytest-mock** | Mocking in unit tests |

### Tooling / DevEx

| Library | Why needed |
|---|---|
| **ruff** | Linter + formatter (replaces flake8+black+isort) |
| **mypy** | Static type checking — critical with clear module boundaries |
| **pre-commit** | Git hooks for lint/format/test |
| **poetry** or **uv** | Dependency management (Maven/Gradle equivalent) |

---

## `pyproject.toml`

```toml
[project]
name = "food-waste-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "email-validator>=2.2",
    "sqlalchemy>=2.0",
    "asyncpg>=0.29",
    "psycopg2-binary>=2.9",
    "alembic>=1.13",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "python-multipart>=0.0.9",
    "structlog>=24.4",
    "tenacity>=9.0",
    "arq>=0.26",
    "slowapi>=0.1.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "pytest-cov>=5.0",
    "factory-boy>=3.3",
    "pytest-mock>=3.14",
    "ruff>=0.7",
    "mypy>=1.11",
    "pre-commit>=3.8",
]
```

---

## Key Architectural Rules Per Module

- **`api.py`** — no business logic, no direct DB/SQLAlchemy access; only calls `service.py`, handles HTTP concerns (status codes, request/response mapping).
- **`service.py`** — business rules (e.g. "expired food item can't be donated"); orchestrates one or more repositories; no HTTP-specific code.
- **`repository.py`** — extends `shared/repository/base_repository.py`; only place that talks to SQLAlchemy/DB for that module.
- **`models.py`** — SQLAlchemy entities only; never returned directly from `api.py` (always mapped to `schemas.py`).
- **`schemas.py`** — Pydantic DTOs for request/response; keeps DB structure decoupled from API contract.
- **Cross-module communication** happens only through a module's `service.py` (never import another module's `repository.py` directly) — keeps modules loosely coupled, similar to enforcing package boundaries in a Java multi-module project.