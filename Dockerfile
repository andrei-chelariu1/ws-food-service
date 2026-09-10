# =============================================================================
# Multi-stage build.
#
# WHY MULTI-STAGE: the builder needs uv, compilers and build metadata. The runtime
# needs none of that. Separating them removes ~150 MB and, more importantly, removes
# tools an attacker could use if they achieved code execution in the container. A
# smaller image is a smaller attack surface, not just a faster pull.
#
# WHY uv: it resolves and installs an order of magnitude faster than pip or Poetry,
# and `uv sync --frozen` fails if uv.lock disagrees with pyproject.toml — so a build
# cannot silently install different versions than were tested.
# =============================================================================

# ------------------------------------------------------------------ builder
FROM python:3.12-slim-bookworm AS builder

# Pinned uv version. `:latest` would make builds non-reproducible: the same commit
# could build with different tooling next month.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency manifests FIRST, before application code. This is the single most
# important line ordering in the file: Docker caches per layer, and source code
# changes far more often than dependencies. Copying code first would reinstall every
# package on every one-line edit.
COPY pyproject.toml uv.lock* ./

# --mount=type=cache keeps uv's download cache across builds without baking it into
# a layer.
# --frozen: install exactly what the lockfile says. Fails loudly on drift instead of
# quietly resolving something new.
# --no-install-project: dependencies only; the project itself is installed after the
# source is copied, so this layer stays cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now the source, then install the project itself.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ------------------------------------------------------------------ runtime
FROM python:3.12-slim-bookworm AS runtime

# PYTHONUNBUFFERED=1 is essential in containers: without it, stdout is block-
# buffered when not a TTY, so logs appear minutes late or vanish entirely if the
# container is killed. Every "my container produces no logs" mystery is this.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

# curl for HEALTHCHECK; postgresql-client for the entrypoint's wait-for-db (pg_isready).
# Both are deliberate, minimal additions — cleaning the apt lists in the same RUN
# keeps them out of the layer.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# NON-ROOT USER. If an attacker gains code execution, they land as an unprivileged
# user who cannot install packages, write to system paths, or bind low ports. This
# is one of the highest-value, lowest-effort container hardening steps there is.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --create-home appuser

WORKDIR /app

# --chown avoids a second layer that duplicates every file just to change ownership.
COPY --from=builder --chown=appuser:appuser /app/.venv ./.venv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser scripts/entrypoint.sh ./scripts/entrypoint.sh

RUN chmod +x ./scripts/entrypoint.sh

USER appuser

EXPOSE 8000

# Probes /health/ready (not /live): the container is only "healthy" once it can
# actually reach Postgres and Redis, which is what docker-compose's
# `depends_on: service_healthy` needs to mean something.
#
# start-period=20s covers migrations running at boot without the container being
# reported unhealthy while they do.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/ready || exit 1

# The entrypoint waits for the database, applies migrations, then execs the server.
# See scripts/entrypoint.sh for why each step is there.
ENTRYPOINT ["./scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
