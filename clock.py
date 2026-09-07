"""Time. All of it, in one place, injectable so tests can freeze it.

Phase 1 contract module. Owned by no Phase 2 agent; imported by all of them.

Two rules the whole codebase depends on:

1. Every stored timestamp is ISO-8601 UTC with an explicit "+00:00" offset.
   Fixed width, so string comparison IS chronological comparison in SQL.
2. Week boundaries are Sunday 18:00 in the configured local zone
   (Europe/Lisbon), converted to UTC for storage. Lisbon observes DST, so the
   UTC instant of "Sunday 18:00" shifts by an hour twice a year. Compute it,
   never hardcode it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = timezone.utc

# Sunday 18:00 local. Python's weekday(): Monday=0 … Sunday=6.
TRIAGE_WEEKDAY = 6
TRIAGE_HOUR = 18


class Clock(Protocol):
    """Injected wherever 'now' is needed. Production uses RealClock."""

    def now(self) -> datetime:
        """Timezone-aware UTC datetime."""
        ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock. Advance it explicitly; it never moves on its own."""

    def __init__(self, start: datetime | str):
        self._now = parse(start) if isinstance(start, str) else _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        """advance(hours=48), advance(days=7, minutes=5), …"""
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, when: datetime | str) -> datetime:
        self._now = parse(when) if isinstance(when, str) else _as_utc(when)
        return self._now


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime rejected: {dt!r} — always attach a timezone")
    return dt.astimezone(UTC)


# --- serialisation ----------------------------------------------------------

def iso(dt: datetime) -> str:
    """The one and only storage format."""
    return _as_utc(dt).isoformat()


def parse(s: str) -> datetime:
    """Inverse of iso(). Accepts a trailing 'Z' as well as '+00:00'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# --- week boundaries --------------------------------------------------------

def week_start_for(when: datetime, tz: str = "Europe/Lisbon") -> datetime:
    """The Sunday-18:00-local instant that opens the week containing `when`.

    Returned in UTC. A Sunday at exactly 18:00 local opens a NEW week.
    """
    zone = ZoneInfo(tz)
    local = _as_utc(when).astimezone(zone)

    # Step back to the most recent Sunday 18:00 that is <= local.
    days_since_sunday = (local.weekday() - TRIAGE_WEEKDAY) % 7
    candidate = (local - timedelta(days=days_since_sunday)).replace(
        hour=TRIAGE_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate.astimezone(UTC)


def next_week_start(when: datetime, tz: str = "Europe/Lisbon") -> datetime:
    """The Sunday-18:00-local instant that closes the week containing `when`.

    Computed in local time so a DST change shifts the UTC instant correctly;
    adding 7*24h in UTC would drift by an hour twice a year.
    """
    zone = ZoneInfo(tz)
    start_local = week_start_for(when, tz).astimezone(zone)
    naive_next = (start_local + timedelta(days=7)).replace(tzinfo=None)
    return naive_next.replace(tzinfo=zone).astimezone(UTC)


def is_locked(captured_at: datetime | str, now: datetime, lock_hours: int = 48) -> bool:
    """Pure lock arithmetic. Deadline exemption is handled by the caller."""
    captured = parse(captured_at) if isinstance(captured_at, str) else _as_utc(captured_at)
    return _as_utc(now) < captured + timedelta(hours=lock_hours)


def lock_expires_at(captured_at: datetime | str, lock_hours: int = 48) -> datetime:
    captured = parse(captured_at) if isinstance(captured_at, str) else _as_utc(captured_at)
    return captured + timedelta(hours=lock_hours)


def fmt_local(dt: datetime | str, tz: str = "Europe/Lisbon") -> str:
    """Human-facing rendering, for bot messages only. Never for storage."""
    d = parse(dt) if isinstance(dt, str) else _as_utc(dt)
    return d.astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M")


def fmt_duration(ms: int) -> str:
    """'1h 47m' / '43m'. Duration is the field that earns its place at triage."""
    total_min = round(ms / 60000)
    h, m = divmod(total_min, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"
