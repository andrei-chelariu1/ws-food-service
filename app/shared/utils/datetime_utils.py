"""Time helpers.

WHY A ONE-LINE `utcnow()` WRAPPER IS WORTH A MODULE
--------------------------------------------------
Two reasons, and both are load-bearing:

1. **Testability.** "An expired food item cannot be donated" is a rule *about
   time*. If services call `datetime.now(UTC)` directly, testing that rule means
   either monkeypatching a stdlib function globally (fragile, leaks between
   tests) or `sleep()`ing in a test (slow, flaky). With one wrapper, the test
   patches `app.shared.utils.datetime_utils.utcnow` — one seam, explicit.

2. **Timezone correctness by construction.** `datetime.utcnow()` is deprecated
   in 3.12 and returns a *naive* datetime — one that looks like UTC but carries
   no tzinfo. Compare it with a timezone-aware value from the database and you
   get `TypeError: can't compare offset-naive and offset-aware datetimes`, in
   production, on the one code path nobody tested. Making `utcnow()` the only
   sanctioned source means every datetime in the system is aware.

RULE: no `datetime.now()` anywhere else in `app/`. Grep enforces it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Current time, timezone-aware, in UTC. The single source of "now"."""
    return datetime.now(UTC)


def is_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    """True if `expires_at` is in the past.

    `now` is injectable so a caller that makes several time-based decisions can
    pass one consistent instant. Otherwise a single request could read the clock
    twice, straddle a boundary, and reach two contradictory conclusions.
    """
    reference = now or utcnow()
    return _as_aware(expires_at) <= reference


def days_from_now(days: int) -> datetime:
    return utcnow() + timedelta(days=days)


def seconds_until(moment: datetime, *, now: datetime | None = None) -> int:
    """Whole seconds from now until `moment`; never negative.

    Used for Redis TTLs — a revoked token's denylist entry should expire exactly
    when the token itself does, and `EXPIRE` with a negative TTL deletes the key
    immediately, which would un-revoke it.
    """
    reference = now or utcnow()
    delta = (_as_aware(moment) - reference).total_seconds()
    return max(0, int(delta))


def _as_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC.

    Defensive: values read back from SQLite (used by the test suite) can lose
    tzinfo even when the column is declared `timezone=True`. Assuming UTC is
    right here because `utcnow()` is the only writer.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
