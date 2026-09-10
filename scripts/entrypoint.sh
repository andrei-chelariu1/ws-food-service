#!/usr/bin/env bash
# =============================================================================
# Container entrypoint: wait for the database -> migrate -> exec the server.
#
# WHY MIGRATIONS RUN HERE AND NOT IN THE APPLICATION LIFESPAN
# Running `alembic upgrade head` inside FastAPI's lifespan looks convenient, but with
# N replicas all N race to migrate the same database on every deploy. Alembic takes
# an advisory lock so they do not corrupt each other, but N-1 replicas then block
# until the leader finishes — and a slow migration means a failed startup probe and
# a restart loop.
#
# The entrypoint runs before the server binds a port, so an orchestrator sees the
# container as "still starting", not "started and broken". Alembic's own lock still
# serialises concurrent replicas safely.
#
# FOR REAL PRODUCTION: promote migrations to a separate pre-deploy job (Kubernetes
# Job, ECS task, CI step) that must succeed before new pods roll out. Then the
# application containers only ever serve traffic. Doing it in the entrypoint is the
# right trade for a sample and for small deployments; it is a trade, and it is worth
# knowing which one you are making.
# =============================================================================
set -euo pipefail
# -e  exit on any command failure — never start the server after a failed migration
# -u  error on unset variables — catches typo'd env var names immediately
# -o pipefail  a failure anywhere in a pipeline fails the pipeline

log() {
  # Same JSON-ish shape as the application's structured logs, so `docker compose
  # logs` reads consistently from the first line onward.
  echo "{\"event\":\"$1\",\"source\":\"entrypoint\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
}

# --- 1. Wait for PostgreSQL ---------------------------------------------------
# docker-compose already gates on `depends_on: condition: service_healthy`, so this
# is belt-and-braces. It matters outside compose (Kubernetes has no such ordering
# guarantee) and when the database is healthy but still replaying WAL.
#
# `pg_isready` rather than a `sleep 10`: it polls the actual readiness of the server,
# so startup is as fast as the database allows instead of a guess that is either too
# short (flaky) or too long (slow every time).
wait_for_postgres() {
  local host port attempt=1 max_attempts=30

  # Parse host and port out of DATABASE_URL
  # (postgresql+asyncpg://user:pass@host:port/db). Falls back to the compose service
  # name so this still works if the URL is set unusually.
  host=$(echo "${DATABASE_URL:-}" | sed -nE 's|.*@([^:/]+).*|\1|p')
  port=$(echo "${DATABASE_URL:-}" | sed -nE 's|.*@[^:]+:([0-9]+).*|\1|p')
  host=${host:-postgres}
  port=${port:-5432}

  log "waiting_for_postgres"
  until pg_isready --host="$host" --port="$port" --quiet; do
    if [ "$attempt" -ge "$max_attempts" ]; then
      log "postgres_unreachable_giving_up"
      exit 1  # fail fast and visibly, rather than retrying forever in silence
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  log "postgres_ready"
}

# --- 2. Apply migrations -----------------------------------------------------
run_migrations() {
  log "running_migrations"
  # `set -e` means a non-zero exit here terminates the container. That is intended:
  # serving traffic against a schema the code does not expect produces silent data
  # corruption, which is strictly worse than not starting.
  alembic upgrade head
  log "migrations_applied"
}

# --- 3. Hand over to the server ----------------------------------------------
main() {
  wait_for_postgres
  run_migrations

  log "starting_application"
  # `exec` REPLACES this shell with the server process, so the server becomes PID 1.
  # Without exec, the shell stays PID 1 and does not forward SIGTERM, so
  # `docker stop` would wait the full 10-second grace period and then SIGKILL —
  # cutting off in-flight requests instead of draining them. One keyword, correct
  # graceful shutdown.
  exec "$@"
}

main "$@"
