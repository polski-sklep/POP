"""db.py — SQLite persistence, the state machine, and the allocation gate.

Agent A, Phase 2. Implements CONTRACTS.md §2 exactly.

Four invariants this module exists to enforce. Everything else here is
plumbing around them.

1. **The state machine is data, not control flow.** ``LEGAL_TRANSITIONS``
   below is the single source of truth. ``set_state()`` consults it and raises
   ``IllegalTransition`` for anything absent — including every transition out
   of a terminal state, and including a no-op transition to the same state,
   which always means a caller lost track of what it was doing. There is no
   silent pass anywhere in this file.

2. **Soft delete, always.** ``dropped`` and ``expired`` rows stay in the
   database forever and are excluded from every default query. The only
   function permitted to read them is ``funnel_stats()`` (plus
   ``all_items(include_removed=True)``, which exists to serve it). There are
   **zero** ``DELETE`` statements in this file. Hard-deleting rejections would
   destroy the numerator of funnel stages 1 and 2, which is the whole point of
   the system.

   ``get_item()`` is the one deliberate exception: it returns a removed row
   when asked for it *by id*. Telegram callback buttons need to distinguish
   "already resolved" from "no such item", and they only ever hold an id they
   were given. Looking one up is not the same as listing them.

3. **No queue row without a why-note.** ``capture()`` is the only function
   that inserts into ``queue``, and it raises ``ValueError`` on an empty or
   whitespace-only note before it touches the database.

4. **promoted_at and resolved_at are evidence, not status.** ``promoted_at``
   is stamped on the first promotion only and is never cleared — not on a
   weekly lapse, not ever. It is what funnel stage 2 is counted from.
   ``resolved_at`` is stamped on entry to a terminal state, which by
   construction happens at most once.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3
import statistics
from dataclasses import dataclass

import clock
from clock import Clock
from config import Settings

ROOT = pathlib.Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
SCHEMA_VERSION = 1

ACTIVE_STATES = ("locked", "queued", "promoted")
TERMINAL_STATES = ("dropped", "expired", "played")
REMOVED_STATES = ("dropped", "expired")
ALL_STATES = ACTIVE_STATES + TERMINAL_STATES

#: The legal-transition table from CONTRACTS.md §1, verbatim, as data.
#: Terminal states map to an empty frozenset: nothing leaves them. A state is
#: never a member of its own set, so a same-state transition raises too.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "locked": frozenset({"queued", "promoted", "dropped", "expired", "played"}),
    "queued": frozenset({"promoted", "dropped", "expired", "played"}),
    "promoted": frozenset({"queued", "dropped", "expired", "played"}),
    "dropped": frozenset(),
    "expired": frozenset(),
    "played": frozenset(),
}


def ms_to_min(ms: int) -> int:
    """Milliseconds to minutes, rounded half up. The canonical implementation."""
    return (int(ms) + 29_999) // 60_000


# --- exceptions -------------------------------------------------------------

class IllegalTransition(Exception):
    """Raised by set_state() for any transition not in LEGAL_TRANSITIONS."""


class AllowanceExceeded(Exception):
    """Raised by promote() when the gate refuses. Carries the overage.

    The bot renders this verbatim to the user, which is why the numbers travel
    with the exception rather than being recomputed at the call site: "that
    would put you 25 min over, 40 min left this week".
    """

    def __init__(self, message: str, overage_min: int, remaining_min: int, cost_min: int):
        super().__init__(message)
        self.message = message
        self.overage_min = overage_min
        self.remaining_min = remaining_min
        self.cost_min = cost_min


class ItemNotFound(LookupError):
    """No queue row with that id. A caller bug, never a user-visible state."""


# --- dataclasses ------------------------------------------------------------

@dataclass(frozen=True)
class QueueItem:
    id: int
    spotify_id: str
    title: str
    show: str
    duration_ms: int
    why_note: str
    captured_at: str
    deadline: str | None
    state: str
    cycles_seen: int
    still_holds: int | None
    promoted_at: str | None
    resolved_at: str | None

    @property
    def is_removed(self) -> bool:
        return self.state in REMOVED_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class Week:
    week_start: str
    allowance_min: int
    debt_min: int
    promoted_min: int
    listened_min: int

    @property
    def effective_allowance_min(self) -> int:
        """This week's allowance after last week's debt. Floored at zero."""
        return max(0, self.allowance_min - self.debt_min)

    @property
    def remaining_min(self) -> int:
        """What is still promotable. Floored at zero; the gate keeps it there."""
        return max(0, self.effective_allowance_min - self.promoted_min)


@dataclass(frozen=True)
class FunnelStats:
    captured: int
    survived_lock: int
    promoted: int
    played: int
    fully_played: int
    survived_rate: float | None
    promoted_rate: float | None
    played_rate: float | None
    fully_played_rate: float | None
    median_days_to_drop: float | None
    still_holds_yes_rate: float | None
    still_holds_answered: int


# --- connection and lifecycle ----------------------------------------------

def connect(settings: Settings) -> sqlite3.Connection:
    """Open the database. Rows come back as sqlite3.Row, foreign keys ON."""
    path = settings.db_path
    target = str(path)
    if target != ":memory:":
        pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema.sql. Idempotent — every statement is CREATE IF NOT EXISTS."""
    conn.executescript(SCHEMA_PATH.read_text())
    # executescript commits and can reset pragmas that are connection-scoped.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """No-op at schema_version 1. The hook exists so version 2 has somewhere to go."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = int(row["value"]) if row else 0
    if version >= SCHEMA_VERSION:
        return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# --- row mapping ------------------------------------------------------------

_ITEM_SELECT = """
SELECT q.id            AS id,
       q.spotify_id    AS spotify_id,
       e.title         AS title,
       e.show          AS show,
       e.duration_ms   AS duration_ms,
       q.why_note      AS why_note,
       q.captured_at   AS captured_at,
       q.deadline      AS deadline,
       q.state         AS state,
       q.cycles_seen   AS cycles_seen,
       q.still_holds   AS still_holds,
       q.promoted_at   AS promoted_at,
       q.resolved_at   AS resolved_at
  FROM queue q
  JOIN episodes e ON e.spotify_id = q.spotify_id
"""

_NOT_REMOVED = "q.state NOT IN ('dropped','expired')"


def _to_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        spotify_id=row["spotify_id"],
        title=row["title"],
        show=row["show"],
        duration_ms=row["duration_ms"],
        why_note=row["why_note"],
        captured_at=row["captured_at"],
        deadline=row["deadline"],
        state=row["state"],
        cycles_seen=row["cycles_seen"],
        still_holds=row["still_holds"],
        promoted_at=row["promoted_at"],
        resolved_at=row["resolved_at"],
    )


def _to_week(row: sqlite3.Row) -> Week:
    return Week(
        week_start=row["week_start"],
        allowance_min=row["allowance_min"],
        debt_min=row["debt_min"],
        promoted_min=row["promoted_min"],
        listened_min=row["listened_min"],
    )


def _require(conn: sqlite3.Connection, item_id: int) -> QueueItem:
    item = get_item(conn, item_id)
    if item is None:
        raise ItemNotFound(f"no queue item with id {item_id}")
    return item


# --- capture ----------------------------------------------------------------

def upsert_episode(conn: sqlite3.Connection, spotify_id: str, title: str, show: str,
                   description: str | None, release_date: str | None,
                   duration_ms: int) -> None:
    """Store or refresh episode metadata. Safe to call on every capture."""
    conn.execute(
        """
        INSERT INTO episodes (spotify_id, title, show, description, release_date, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(spotify_id) DO UPDATE SET
            title        = excluded.title,
            show         = excluded.show,
            description  = excluded.description,
            release_date = excluded.release_date,
            duration_ms  = excluded.duration_ms
        """,
        (spotify_id, title, show, description, release_date, int(duration_ms)),
    )
    conn.commit()


def _deadline_exempt(captured_at: str, deadline: str | None, lock_hours: int) -> bool:
    """True when the deadline falls inside the lock window, so the lock is waived.

    Without this, a deadline-tied episode captured on Friday for a Saturday
    event would still be invisible on Sunday. An exemption is not a content
    category: the item still needs a note and still costs its minutes.
    """
    if deadline is None:
        return False
    return clock.parse(deadline) <= clock.lock_expires_at(captured_at, lock_hours)


def capture(conn: sqlite3.Connection, clk: Clock, settings: Settings, *, spotify_id: str,
            why_note: str, deadline: str | None = None) -> QueueItem:
    """Create a queue row. The only INSERT into `queue` in the codebase.

    Raises ValueError on an empty or whitespace-only why_note — the note is
    what the item is read back against 48 hours later, so a row without one is
    worthless and is never created. The bot therefore holds the pending
    capture in memory until the reply arrives.
    """
    note = (why_note or "").strip()
    if not note:
        raise ValueError("why_note is required and cannot be empty or whitespace")

    episode = conn.execute(
        "SELECT spotify_id FROM episodes WHERE spotify_id = ?", (spotify_id,)
    ).fetchone()
    if episode is None:
        raise ValueError(
            f"unknown episode {spotify_id!r} — call upsert_episode() before capture()"
        )

    captured_at = clock.iso(clk.now())
    deadline_iso = clock.iso(clock.parse(deadline)) if deadline is not None else None
    state = "queued" if _deadline_exempt(captured_at, deadline_iso, settings.lock_hours) else "locked"

    cur = conn.execute(
        """
        INSERT INTO queue (spotify_id, why_note, captured_at, deadline, state,
                           cycles_seen, still_holds, promoted_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL)
        """,
        (spotify_id, note, captured_at, deadline_iso, state),
    )
    conn.commit()
    return _require(conn, int(cur.lastrowid))


# --- reading ----------------------------------------------------------------

def get_item(conn: sqlite3.Connection, item_id: int) -> QueueItem | None:
    """Look one item up by id.

    **The one reader that returns soft-deleted rows.** Inline-button callbacks
    carry an id and must be able to tell "already resolved" from "never
    existed"; answering the first as if it were the second would be a lie. This
    is a lookup, not a listing — nothing here can leak a dropped item into a
    view.
    """
    row = conn.execute(_ITEM_SELECT + " WHERE q.id = ?", (item_id,)).fetchone()
    return _to_item(row) if row else None


def active_items(conn: sqlite3.Connection) -> list[QueueItem]:
    """locked + queued + promoted, oldest capture first."""
    rows = conn.execute(
        _ITEM_SELECT + f" WHERE {_NOT_REMOVED} AND q.state != 'played'"
        " ORDER BY q.captured_at, q.id"
    ).fetchall()
    return [_to_item(r) for r in rows]


def triage_items(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[QueueItem]:
    """Queued items whose lock has lifted, plus deadline-exempt ones.

    A deadline-exempt item is created `queued` while still inside its lock
    window; it is meant to surface, so the lock check is waived for it.
    """
    now = clk.now()
    rows = conn.execute(
        _ITEM_SELECT + " WHERE q.state = 'queued' ORDER BY q.captured_at, q.id"
    ).fetchall()
    out: list[QueueItem] = []
    for row in rows:
        item = _to_item(row)
        unlocked = not clock.is_locked(item.captured_at, now, settings.lock_hours)
        if unlocked or _deadline_exempt(item.captured_at, item.deadline, settings.lock_hours):
            out.append(item)
    return out


def promoted_items(conn: sqlite3.Connection) -> list[QueueItem]:
    rows = conn.execute(
        _ITEM_SELECT + " WHERE q.state = 'promoted' ORDER BY q.promoted_at, q.id"
    ).fetchall()
    return [_to_item(r) for r in rows]


def polling_targets(conn: sqlite3.Connection) -> list[str]:
    """Distinct spotify_ids worth a poll: locked, queued or promoted.

    Not restricted to the promoted slate. Out-of-band listening on a queued
    item is exactly the behaviour the brief wants visible.
    """
    rows = conn.execute(
        "SELECT DISTINCT spotify_id FROM queue "
        "WHERE state IN ('locked','queued','promoted') ORDER BY spotify_id"
    ).fetchall()
    return [r["spotify_id"] for r in rows]


def all_items(conn: sqlite3.Connection, *, include_removed: bool = False) -> list[QueueItem]:
    """Every item. `include_removed=True` exists solely for the stats path."""
    sql = _ITEM_SELECT
    if not include_removed:
        sql += f" WHERE {_NOT_REMOVED}"
    sql += " ORDER BY q.captured_at, q.id"
    return [_to_item(r) for r in conn.execute(sql).fetchall()]


# --- state changes ----------------------------------------------------------

def _check_transition(current: str, new_state: str, item_id: int) -> None:
    if new_state not in ALL_STATES:
        raise IllegalTransition(f"{new_state!r} is not a state")
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if new_state in allowed:
        return
    if current in TERMINAL_STATES:
        raise IllegalTransition(
            f"item {item_id} is {current!r}, which is terminal; "
            f"refusing {current!r} -> {new_state!r}"
        )
    if current == new_state:
        raise IllegalTransition(
            f"item {item_id} is already {current!r}; a no-op transition means "
            "the caller lost track of its own state"
        )
    raise IllegalTransition(f"illegal transition for item {item_id}: {current!r} -> {new_state!r}")


def set_state(conn: sqlite3.Connection, clk: Clock, item_id: int, new_state: str) -> QueueItem:
    """Move an item, or refuse. Never silently passes.

    Stamps `resolved_at` on entry to a terminal state and `promoted_at` on the
    **first** promotion only. `promoted_at` is never cleared, including on the
    weekly lapse back to `queued` — it is the evidence for funnel stage 2.
    """
    item = _require(conn, item_id)
    _check_transition(item.state, new_state, item_id)

    now_iso = clock.iso(clk.now())
    sets = ["state = ?"]
    params: list[object] = [new_state]

    if new_state == "promoted" and item.promoted_at is None:
        sets.append("promoted_at = ?")
        params.append(now_iso)
    if new_state in TERMINAL_STATES and item.resolved_at is None:
        sets.append("resolved_at = ?")
        params.append(now_iso)

    params.append(item_id)
    conn.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return _require(conn, item_id)


def lift_expired_locks(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[QueueItem]:
    """locked -> queued for every item whose 48h has elapsed."""
    now = clk.now()
    rows = conn.execute(
        _ITEM_SELECT + " WHERE q.state = 'locked' ORDER BY q.captured_at, q.id"
    ).fetchall()
    lifted: list[QueueItem] = []
    for row in rows:
        item = _to_item(row)
        if not clock.is_locked(item.captured_at, now, settings.lock_hours):
            lifted.append(set_state(conn, clk, item.id, "queued"))
    return lifted


def record_still_holds(conn: sqlite3.Connection, item_id: int, answer: bool) -> QueueItem:
    """Log the yes/no on whether the original reason survives.

    Recorded independently of promote/drop and never inferred from them: it is
    the cleanest signal in the system precisely because it is asked directly.
    """
    _require(conn, item_id)
    conn.execute("UPDATE queue SET still_holds = ? WHERE id = ?", (1 if answer else 0, item_id))
    conn.commit()
    return _require(conn, item_id)


def bump_cycle(conn: sqlite3.Connection, item_id: int) -> QueueItem:
    """cycles_seen += 1. Called once per item per triage run."""
    _require(conn, item_id)
    conn.execute("UPDATE queue SET cycles_seen = cycles_seen + 1 WHERE id = ?", (item_id,))
    conn.commit()
    return _require(conn, item_id)


# --- weeks, allowance, debt -------------------------------------------------

def _week_row(conn: sqlite3.Connection, week_start: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,)).fetchone()


def _ensure_week(conn: sqlite3.Connection, week_start: str, allowance_min: int,
                 debt_min: int = 0) -> Week:
    conn.execute(
        "INSERT OR IGNORE INTO weeks (week_start, allowance_min, debt_min, promoted_min, listened_min) "
        "VALUES (?, ?, ?, 0, 0)",
        (week_start, int(allowance_min), int(debt_min)),
    )
    conn.commit()
    row = _week_row(conn, week_start)
    assert row is not None
    return _to_week(row)


def current_week(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Week:
    """The week containing now, created with a fresh allowance if absent."""
    week_start = clock.iso(clock.week_start_for(clk.now(), settings.tz))
    return _ensure_week(conn, week_start, settings.weekly_allowance_min)


def promotion_cost_min(conn: sqlite3.Connection, item: QueueItem) -> int:
    """The episode's *remaining* minutes, not its full duration.

    An item already half heard costs half. Charging full duration for a
    re-promotion would overstate the commitment and make the gate lie. The
    "last known position" is the most recent poll's position, not the maximum
    ever seen — a backward scrub genuinely means there is more left to hear.
    """
    row = conn.execute(
        "SELECT position_ms FROM listening WHERE spotify_id = ? "
        "ORDER BY polled_at DESC, id DESC LIMIT 1",
        (item.spotify_id,),
    ).fetchone()
    position_ms = int(row["position_ms"]) if row else 0
    return ms_to_min(max(0, int(item.duration_ms) - position_ms))


def promote(conn: sqlite3.Connection, clk: Clock, settings: Settings, item_id: int) -> QueueItem:
    """The allocation gate. This is where the constraint actually bites.

    Refuses with `AllowanceExceeded` and changes **nothing** when the cost
    would breach the effective allowance. Transition legality is checked
    first, so a dropped item gets `IllegalTransition` rather than a misleading
    budget message; neither path writes.
    """
    item = _require(conn, item_id)
    _check_transition(item.state, "promoted", item_id)

    # The brief: "Locked items do not appear in triage and cannot be
    # promoted." The transition table permits `locked -> promoted` for the
    # deadline-exempt case, so the lock itself has to be re-checked here or
    # the gate would happily promote an item that is still inside its 48h.
    # No live path posts a button for a locked item today; this is the
    # enforcement the invariant claims to have.
    if item.state == "locked" and clock.is_locked(
        item.captured_at, clk.now(), settings.lock_hours
    ) and not _deadline_exempt(item.captured_at, item.deadline, settings.lock_hours):
        raise IllegalTransition(
            f"item {item_id} is still inside its {settings.lock_hours}h lock "
            f"(until {clock.iso(clock.lock_expires_at(item.captured_at, settings.lock_hours))}); "
            "locked items cannot be promoted"
        )

    week = current_week(conn, clk, settings)
    cost = promotion_cost_min(conn, item)

    if week.promoted_min + cost > week.effective_allowance_min:
        overage = week.promoted_min + cost - week.effective_allowance_min
        raise AllowanceExceeded(
            f"that would put you {overage} min over; {week.remaining_min} min left this week",
            overage_min=overage,
            remaining_min=week.remaining_min,
            cost_min=cost,
        )

    promoted = set_state(conn, clk, item_id, "promoted")
    conn.execute(
        "UPDATE weeks SET promoted_min = promoted_min + ? WHERE week_start = ?",
        (cost, week.week_start),
    )
    conn.commit()
    return promoted


def roll_over_week(conn: sqlite3.Connection, clk: Clock,
                   settings: Settings) -> tuple[Week, Week]:
    """Close the week that just ended, open the one `now` sits in. -> (closed, opened)

    1. Lapse every `promoted` item back to `queued`. Anything actually played
       is already in `played` and is not touched. `promoted_at` survives.
    2. `debt = clamp(listened_min - effective_allowance_min, 0, allowance_min)`.
       Debt is driven by **listened** minutes, not promoted ones: the gate makes
       over-promotion impossible, so the only way to exceed the allowance is to
       actually listen past it. The clamp at `allowance_min` stops one blown
       week from zeroing out several later ones.
    3. Open the new week with that debt and a fresh allowance from config.

    Note which week gets closed. Triage runs *at* Sunday 18:00, and a Sunday at
    exactly 18:00 already belongs to the new week, so the week being closed is
    the most recent one that opened **before** the one containing `now` — not
    `current_week()`, which by then is the new one. Closing `current_week()`
    here would silently compute every debt from an empty week.
    """
    now = clk.now()
    opened_start = clock.iso(clock.week_start_for(now, settings.tz))

    # 1. Lapse. Every promoted item returns to the queue; it can earn promotion
    #    again next cycle. This is what stops an ever-growing playable backlog
    #    from defeating the gate.
    for item in promoted_items(conn):
        set_state(conn, clk, item.id, "queued")

    prior = conn.execute(
        "SELECT * FROM weeks WHERE week_start < ? ORDER BY week_start DESC LIMIT 1",
        (opened_start,),
    ).fetchone()
    if prior is None:
        # First run, or no week was ever opened. Materialise the immediately
        # preceding week — one second before the opening boundary lands inside
        # it — so there is something honest to return as `closed`. Its counters
        # are zero, so it carries no debt.
        previous_start = clock.iso(
            clock.week_start_for(
                clock.parse(opened_start) - _dt.timedelta(seconds=1), settings.tz
            )
        )
        closed = _ensure_week(conn, previous_start, settings.weekly_allowance_min)
    else:
        closed = _to_week(prior)

    # 2. Debt from listened minutes, clamped.
    raw_debt = closed.listened_min - closed.effective_allowance_min
    debt = max(0, min(raw_debt, closed.allowance_min))

    # 3. Open the new week. If the row already exists (a repeated triage run,
    #    or promotions made before rollover), refresh its debt and allowance
    #    but leave promoted/listened accounting alone.
    conn.execute(
        """
        INSERT INTO weeks (week_start, allowance_min, debt_min, promoted_min, listened_min)
        VALUES (?, ?, ?, 0, 0)
        ON CONFLICT(week_start) DO UPDATE SET
            allowance_min = excluded.allowance_min,
            debt_min      = excluded.debt_min
        """,
        (opened_start, settings.weekly_allowance_min, debt),
    )
    conn.commit()

    opened_row = _week_row(conn, opened_start)
    assert opened_row is not None
    closed_row = _week_row(conn, closed.week_start)
    assert closed_row is not None
    return _to_week(closed_row), _to_week(opened_row)


def record_listening(conn: sqlite3.Connection, clk: Clock, spotify_id: str, position_ms: int,
                     delta_ms: int, fully_played: bool | None) -> None:
    """Write one poll observation and re-total the week it belongs to.

    `delta_ms` must already be clamped (see spotify.clamped_delta); a negative
    value is a caller bug and is rejected rather than stored, because an
    unclamped delta silently credits time that was not earned back.

    The week's `listened_min` is *recomputed* from the sum of `delta_ms` in the
    window rather than incremented, so per-poll rounding cannot drift. The
    window runs from that week's start to the next week's start, or open-ended
    while it is still the newest week.
    """
    if delta_ms < 0:
        raise ValueError(f"delta_ms must be clamped to >= 0, got {delta_ms}")

    polled_at = clock.iso(clk.now())
    conn.execute(
        "INSERT INTO listening (spotify_id, polled_at, position_ms, delta_ms, fully_played) "
        "VALUES (?, ?, ?, ?, ?)",
        (spotify_id, polled_at, int(position_ms), int(delta_ms),
         None if fully_played is None else int(bool(fully_played))),
    )

    # Attribute to the week open at poll time. No settings here, so the week is
    # located by timestamp against the rows that exist rather than recomputed
    # from a timezone. If no week has been opened yet, the observation is still
    # recorded; there is simply no allowance to charge it against.
    week = conn.execute(
        "SELECT week_start FROM weeks WHERE week_start <= ? ORDER BY week_start DESC LIMIT 1",
        (polled_at,),
    ).fetchone()
    if week is not None:
        start = week["week_start"]
        nxt = conn.execute(
            "SELECT week_start FROM weeks WHERE week_start > ? ORDER BY week_start ASC LIMIT 1",
            (start,),
        ).fetchone()
        end = nxt["week_start"] if nxt else "9999-12-31T23:59:59+00:00"
        conn.execute(
            """
            UPDATE weeks SET listened_min = (
                SELECT (COALESCE(SUM(delta_ms), 0) + 29999) / 60000
                  FROM listening WHERE polled_at >= ? AND polled_at < ?
            ) WHERE week_start = ?
            """,
            (start, end, start),
        )
    conn.commit()


# --- stats ------------------------------------------------------------------

def _rate(numerator: int, denominator: int) -> float | None:
    """None, never 0 and never a crash, when the denominator is 0."""
    if denominator == 0:
        return None
    return numerator / denominator


def funnel_stats(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                 since: str | None = None) -> FunnelStats:
    """The decay curve. The **only** function permitted to read REMOVED_STATES.

    Items still inside their lock window are undetermined and are excluded
    from every denominator: counting a two-hour-old capture as "did not
    survive" would make the first funnel stage a function of when the report
    was run.
    """
    now = clk.now()
    universe: list[QueueItem] = []
    for item in all_items(conn, include_removed=True):
        if since is not None and item.captured_at < since:
            continue
        if clock.is_locked(item.captured_at, now, settings.lock_hours):
            continue
        universe.append(item)

    captured = len(universe)

    survived = 0
    for item in universe:
        lock_end = clock.lock_expires_at(item.captured_at, settings.lock_hours)
        resolved_early = (
            item.state in REMOVED_STATES
            and item.resolved_at is not None
            and clock.parse(item.resolved_at) < lock_end
        )
        if not resolved_early:
            survived += 1

    promoted = sum(1 for item in universe if item.promoted_at is not None)

    listened_ids = {
        r["spotify_id"]
        for r in conn.execute(
            "SELECT DISTINCT spotify_id FROM listening WHERE delta_ms > 0"
        ).fetchall()
    }

    # Funnel stage 3 is "promoted -> actually played", so both numerators are
    # restricted to items that were actually promoted. Out-of-band listening
    # on an item that was never promoted is still recorded honestly in
    # `listening` (D1.12) and still charged against the allowance, but
    # counting it here against a denominator of `promoted` produced rates
    # above 100% and made the stage measure something other than what the
    # brief defines.
    played = sum(
        1 for item in universe
        if item.promoted_at is not None and item.spotify_id in listened_ids
    )
    fully_played = sum(
        1 for item in universe
        if item.state == "played" and item.promoted_at is not None
    )

    drop_days = [
        (clock.parse(item.resolved_at) - clock.parse(item.captured_at)).total_seconds() / 86400.0
        for item in universe
        if item.state == "dropped" and item.resolved_at is not None
    ]
    median_days_to_drop = statistics.median(drop_days) if drop_days else None

    answered = [item.still_holds for item in universe if item.still_holds is not None]
    still_holds_yes_rate = _rate(sum(answered), len(answered))

    return FunnelStats(
        captured=captured,
        survived_lock=survived,
        promoted=promoted,
        played=played,
        fully_played=fully_played,
        survived_rate=_rate(survived, captured),
        promoted_rate=_rate(promoted, survived),
        played_rate=_rate(played, promoted),
        fully_played_rate=_rate(fully_played, promoted),
        median_days_to_drop=median_days_to_drop,
        still_holds_yes_rate=still_holds_yes_rate,
        still_holds_answered=len(answered),
    )
