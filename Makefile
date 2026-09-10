# ---------------------------------------------------------------------------
# Discoverable entrypoints. `make help` lists everything.
#
# Why a Makefile: the exact incantations (docker compose flags, alembic args,
# pytest options) are written down ONCE here instead of living in someone's
# shell history or in three different README snippets that drift apart. DRY
# applied to operations, not just to code.
#
# On Windows, run these from Git Bash / WSL, or copy the command bodies.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
COMPOSE := docker compose
EXEC := $(COMPOSE) exec api

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Environment ----------------------------------------------------------
.PHONY: env
env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env — review SECRET_KEY before deploying")

.PHONY: lock
lock: ## Regenerate uv.lock from pyproject.toml
	uv lock

# --- Lifecycle ------------------------------------------------------------
.PHONY: up
up: env ## Build and start postgres + redis + api (migrations run on boot)
	$(COMPOSE) up --build

.PHONY: up-d
up-d: env ## Same as `up` but detached
	$(COMPOSE) up --build -d

.PHONY: down
down: ## Stop containers, keep data volumes
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop containers AND delete data volumes (destroys the database)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail api logs
	$(COMPOSE) logs -f api

.PHONY: ps
ps: ## Show container health
	$(COMPOSE) ps

.PHONY: shell
shell: ## Open a shell inside the api container
	$(EXEC) bash

.PHONY: psql
psql: ## Open psql against the running database
	$(COMPOSE) exec postgres psql -U app -d foodwaste

# --- Migrations -----------------------------------------------------------
.PHONY: migrate
migrate: ## Apply all pending migrations
	$(EXEC) alembic upgrade head

.PHONY: downgrade
downgrade: ## Roll back exactly one migration
	$(EXEC) alembic downgrade -1

.PHONY: current
current: ## Show the applied revision
	$(EXEC) alembic current

.PHONY: history
history: ## Show migration history
	$(EXEC) alembic history --verbose

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add x to y"
	@test -n "$(m)" || (echo 'Usage: make revision m="describe the change"' && exit 1)
	$(EXEC) alembic revision --autogenerate -m "$(m)"

# --- Quality --------------------------------------------------------------
.PHONY: test
test: ## Run the test suite with coverage
	$(EXEC) pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## ruff check + format check + mypy
	$(EXEC) ruff check .
	$(EXEC) ruff format --check .
	$(EXEC) mypy app

.PHONY: format
format: ## Auto-fix lint issues and format
	$(EXEC) ruff check --fix .
	$(EXEC) ruff format .

.PHONY: check
check: lint test ## Everything CI would run

# --- Smoke test -----------------------------------------------------------
.PHONY: smoke
smoke: ## Curl the health endpoints
	@curl -fsS localhost:8000/health/live && echo ""
	@curl -fsS localhost:8000/health/ready && echo ""
