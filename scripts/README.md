# `scripts/` — Container Entrypoint

## `entrypoint.sh`

Runs as the container's `ENTRYPOINT`, before the server:

```
wait for PostgreSQL  ──►  alembic upgrade head  ──►  exec uvicorn
```

If any step fails, `set -euo pipefail` terminates the container. **That is intended:** serving
traffic against a schema the code does not expect produces silent data corruption, which is
strictly worse than not starting.

---

## Four decisions worth understanding

### `set -euo pipefail`

| Flag | Prevents |
|---|---|
| `-e` | starting the server after a failed migration |
| `-u` | a typo'd env var name silently expanding to empty |
| `-o pipefail` | a failure mid-pipeline being masked by a successful last command |

### `pg_isready`, not `sleep 10`

It polls the database's actual readiness, so startup is as fast as the database allows instead
of a guess that is either too short (flaky) or too long (slow every single time). Capped at 30
attempts, then it exits 1 — failing fast and visibly beats retrying forever in silence.

`docker-compose.yml` already gates on `depends_on: condition: service_healthy`, so this is
belt-and-braces. It earns its place outside compose (Kubernetes has no such ordering guarantee)
and when the database is accepting connections but still replaying WAL.

### `exec "$@"` — one keyword, correct graceful shutdown

`exec` **replaces** the shell with the server process, so the server becomes PID 1 and receives
signals directly.

Without it, the shell stays PID 1 and does not forward `SIGTERM`. `docker stop` would then wait
the full 10-second grace period and `SIGKILL` — cutting off in-flight requests instead of
draining them. Every "my deploys drop requests" mystery is this.

### Migrations here, not in the app lifespan

See `migrations/README.md` for the full reasoning. Short version: the entrypoint runs before
the port is bound, so an orchestrator sees "still starting" rather than "started and broken".
For real production, promote migrations to a pre-deploy job.

---

## Log format

The script emits the same JSON-ish shape as the application's structured logs:

```json
{"event":"postgres_ready","source":"entrypoint","timestamp":"2026-07-29T07:18:00Z"}
```

so `docker compose logs api` reads consistently from the very first line, and a log shipper
does not need a second parser for the boot sequence.

---

## Line endings

This file **must** have LF endings. A `\r` makes the shebang `#!/usr/bin/env bash\r`, which
Linux tries to execute as a program named `bash\r`:

```
exec /app/scripts/entrypoint.sh: no such file or directory
```

A famously confusing error on Windows checkouts. If you hit it:

```bash
git config core.autocrlf input     # then re-checkout
# or: dos2unix scripts/entrypoint.sh
```
