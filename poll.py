"""poll.py — the daily cron entry: resume_point deltas and allowance alerts.

Phase 3. Implements CONTRACTS.md §5 exactly.

Four things in this module are load-bearing.

1. **Results are keyed by `spotify_id`, never zipped by index.**
   `get_episodes()` skips nulls, so it can return fewer episodes than were
   asked for. Zipping the response against the request would attribute one
   episode's listening time to a different episode and would do so silently,
   forever. This is the single most dangerous bug available in this file.

2. **The clamp.** `spotify.clamped_delta()` is used, never reimplemented.
   `resume_point` is a position, not a total; scrubbing backward or starting
   a relisten drops it, and an unclamped delta goes negative and credits time
   back that was never earned.

3. **Completion is Spotify's `fully_played` flag**, never
   `position >= duration`. Trailing credits mean episodes rarely reach 100%,
   so the arithmetic version would under-report completion permanently.

4. **One alert of each kind per week.** `poll.py` runs daily and the
   allowance stays crossed once crossed, so without the `alerts_sent`
   bookkeeping the bot would send the same "exceeded" message every morning
   for the rest of the week.

5. **Deadline items are surfaced here, because this is the only daily
   process.** The brief requires a deadline item to surface before it expires
   "regardless of where the weekly cycle sits", and the weekly triage post
   structurally cannot do that for a deadline falling between two Sundays —
   which is most deadlines. `deadline_notices` is the same bookkeeping idea as
   `alerts_sent`: once per item, ever. See `surface_deadlines` and D5.1.

No live call is made from anywhere in this file except `main()`, which is the
production cron path and is never executed by the test suite.
"""
from __future__ import annotations

import logging

import clock
import db
from clock import Clock
from config import Settings
from spotify import clamped_delta
from triage import SyncBot, render_deadline_notice, send

log = logging.getLogger("pop.poll")

APPROACHING = "approaching"
EXCEEDED = "exceeded"

#: ≥ 80% of the effective allowance trips the approaching alert.
APPROACHING_FRACTION = 0.8


# --------------------------------------------------------------------------
# Position bookkeeping
# --------------------------------------------------------------------------

def _last_observation(conn, spotify_id: str) -> tuple[int, bool | None] | None:
    """The most recent poll observation for this episode, or None if never polled.

    The *most recent*, not the maximum ever seen: a backward scrub genuinely
    means there is more of the episode left to hear, and `promotion_cost_min`
    reads the same row for the same reason.
    """
    row = conn.execute(
        "SELECT position_ms, fully_played FROM listening WHERE spotify_id = ? "
        "ORDER BY polled_at DESC, id DESC LIMIT 1",
        (spotify_id,),
    ).fetchone()
    if row is None:
        return None
    flag = None if row["fully_played"] is None else bool(row["fully_played"])
    return int(row["position_ms"]), flag


def _clamped_delta(spotify, previous_position_ms: int, current_position_ms: int) -> int:
    """The clamp, preferring one hanging off the injected client if it has one.

    `clamped_delta` is a module function in `spotify.py`, not a client method,
    so the module version is the normal path; the lookup exists only so a test
    double can observe the call.
    """
    fn = getattr(spotify, "clamped_delta", None) or clamped_delta
    return fn(previous_position_ms, current_position_ms)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def run_poll(conn, clk: Clock, settings: Settings, spotify, bot) -> None:
    """The daily cron entry. Read positions, write clamped deltas, then alert.

    Idempotent: a second run in the same minute sees the same positions,
    computes a zero delta against its own previous observation, records
    nothing new, and finds every alert already sent.
    """
    # Make sure the week exists before any listening is recorded, or
    # `record_listening` has no row to attribute the minutes to.
    db.current_week(conn, clk, settings)

    # Locks are time-based, so lifting them daily rather than only at Sunday's
    # triage is strictly more accurate — an item whose 48h elapsed on Tuesday
    # has genuinely been unlocked since Tuesday. It has to happen here, before
    # `surface_deadlines`, or an item captured Monday with a Thursday deadline
    # would still be `locked` on Wednesday and would never be surfaced. The
    # call is idempotent (it only moves rows whose lock has already expired),
    # so `run_triage` making it again on Sunday changes nothing.
    db.lift_expired_locks(conn, clk, settings)

    targets = db.polling_targets(conn)
    if targets:
        episodes = spotify.get_episodes(targets)

        # KEYED BY spotify_id. `get_episodes` skips nulls for unavailable or
        # unknown ids, so this list can be shorter than `targets` and in a
        # different order. Zipping by index would credit one episode's
        # listening time to another episode, silently and permanently.
        by_id = {ep.spotify_id: ep for ep in episodes}

        missing = [sid for sid in targets if sid not in by_id]
        if missing:
            log.warning("no episode returned for %d target(s): %s", len(missing), missing)

        for spotify_id in targets:
            episode = by_id.get(spotify_id)
            if episode is None:
                continue
            _record(conn, clk, spotify, spotify_id, episode)

    # Before the alerts: a deadline is time-critical and an allowance alert is
    # not, so a Telegram failure must not cost the deadline post its turn.
    surface_deadlines(conn, clk, settings, bot)

    check_alerts(conn, clk, settings, bot)


def _record(conn, clk: Clock, spotify, spotify_id: str, episode) -> None:
    """One episode's observation: the clamped delta, then the completion flip."""
    position_ms = int(episode.resume_position_ms)
    fully_played = bool(episode.fully_played)

    previous = _last_observation(conn, spotify_id)
    previous_ms = 0 if previous is None else previous[0]

    # Cron retries happen, and a locked episode nobody is listening to is
    # polled every day for weeks. An observation identical to the previous one
    # changes no number anywhere — `weeks.listened_min` is recomputed as a sum
    # of deltas — so it is skipped rather than piling up rows that say nothing.
    unchanged = previous is not None and previous == (position_ms, fully_played)
    if not unchanged:
        delta_ms = _clamped_delta(spotify, previous_ms, position_ms)
        db.record_listening(conn, clk, spotify_id, position_ms, delta_ms, fully_played)

    # Completion is Spotify's own flag, never `position >= duration`: trailing
    # credits mean episodes rarely reach 100%.
    if fully_played:
        _mark_played(conn, clk, spotify_id)


def _mark_played(conn, clk: Clock, spotify_id: str) -> None:
    """Flip every still-active queue row for this episode to `played`.

    `active_items()` returns locked, queued and promoted only, so an item that
    is already terminal — played, dropped or expired — is never touched. A
    second flip would be an illegal transition, and re-resolving a dropped
    item would rewrite the outcome the funnel is measuring.
    """
    for item in db.active_items(conn):
        if item.spotify_id == spotify_id:
            db.set_state(conn, clk, item.id, "played")


# --------------------------------------------------------------------------
# Deadline surfacing
# --------------------------------------------------------------------------

def _notice_sent(conn, queue_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deadline_notices WHERE queue_id = ?", (queue_id,)
    ).fetchone()
    return row is not None


def _mark_notice_sent(conn, clk: Clock, queue_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO deadline_notices (queue_id, sent_at) VALUES (?, ?)",
        (queue_id, clock.iso(clk.now())),
    )
    conn.commit()


def _due_before_next_triage(item, boundary) -> bool:
    """Would the weekly triage post reach this item before its deadline?

    No, when the deadline falls at or before the boundary: `run_triage` uses
    `deadline <= now` to expire, so an item whose deadline is exactly the
    triage instant is expired by that triage rather than posted by it. The
    comparison here is the mirror of `triage._will_expire_next_cycle`, and it
    has to be, or the one boundary case would reproduce the original bug.
    """
    return item.deadline is not None and clock.parse(item.deadline) <= boundary


def surface_deadlines(conn, clk: Clock, settings: Settings, bot) -> None:
    """Post queued items whose deadline lands before the next triage.

    The brief requires that "an item with a deadline surfaces before it
    expires regardless of where the weekly cycle sits". The lock exemption
    alone did not do that: it flips `locked` to `queued` at capture, which is
    a state the user never sees, and an item captured Monday for a Tuesday
    event was posted nowhere and soft-deleted at Sunday's triage for having
    passed its deadline. `poll.py` is the only daily process, so delivery
    belongs here (DECISIONS.md D5.1).

    This is a delivery mechanism and nothing more. The item is not privileged:
    it needed a note to exist at all, promotion still runs through
    `db.promote()` and is still refused by the allocation gate, and the
    buttons are the same buttons with the same callback payloads triage posts.

    **Once per item, ever.** `deadline_notices` is `alerts_sent` bookkeeping
    under a different key; without it, a poll running every morning would
    repost the same item until the deadline passed.

    **The send precedes the row**, matching D3.5. A Telegram outage leaves the
    notice unrecorded and tomorrow's poll retries it, which is why nothing here
    filters on `deadline >= now`: an outage on the deadline day must not
    silently consume the one notice the item ever gets.
    """
    boundary = clock.next_week_start(clk.now(), settings.tz)

    due = [
        item
        for item in db.active_items(conn)
        if item.state == "queued"
        and _due_before_next_triage(item, boundary)
        and not _notice_sent(conn, item.id)
    ]
    if not due:
        return

    week = db.current_week(conn, clk, settings)
    for item in due:
        text, markup = render_deadline_notice(item, week)
        send(bot, settings, text, markup)
        _mark_notice_sent(conn, clk, item.id)


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def _already_sent(conn, week_start: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE week_start = ? AND kind = ?",
        (week_start, kind),
    ).fetchone()
    return row is not None


def _mark_sent(conn, clk: Clock, week_start: str, kind: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerts_sent (week_start, kind, sent_at) VALUES (?, ?, ?)",
        (week_start, kind, clock.iso(clk.now())),
    )
    conn.commit()


def check_alerts(conn, clk: Clock, settings: Settings, bot) -> None:
    """At most one `approaching` and one `exceeded` message per week.

    This is the control loop, and it is the main reason the interface is a
    bot: a running figure mid-week is a control, whereas a Sunday report that
    the budget was blown on Tuesday is a postmortem.

    The send happens before the row is written, so a Telegram failure leaves
    the alert unrecorded and tomorrow's poll retries it rather than swallowing
    it. `INSERT OR IGNORE` on the composite key makes the write itself safe to
    repeat.
    """
    week = db.current_week(conn, clk, settings)
    effective = week.effective_allowance_min
    listened = week.listened_min

    # With the whole allowance eaten by debt, "80% of nothing" is not a
    # meaningful warning — every poll would trip it at zero minutes listened.
    # Only the crossing is reported in that case.
    approaching = effective > 0 and listened >= APPROACHING_FRACTION * effective
    exceeded = listened > effective

    if approaching and not _already_sent(conn, week.week_start, APPROACHING):
        send(bot, settings, _approaching_text(week))
        _mark_sent(conn, clk, week.week_start, APPROACHING)

    if exceeded and not _already_sent(conn, week.week_start, EXCEEDED):
        send(bot, settings, _exceeded_text(week))
        _mark_sent(conn, clk, week.week_start, EXCEEDED)


def _approaching_text(week) -> str:
    left = max(0, week.effective_allowance_min - week.listened_min)
    return (
        f"{week.listened_min} of {week.effective_allowance_min} min listened "
        f"this week. {left} min left."
    )


def _exceeded_text(week) -> str:
    over = week.listened_min - week.effective_allowance_min
    return (
        f"Over the allowance: {week.listened_min} min listened against "
        f"{week.effective_allowance_min} min. {over} min over — that comes off "
        "next week."
    )


# --------------------------------------------------------------------------
# Cron entry point
# --------------------------------------------------------------------------

def main() -> None:  # pragma: no cover — production only, never run by tests
    """Daily cron. The only place real credentials are touched."""
    import config
    from spotify import SpotifyClient

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from bot import _silence_token_logging

    _silence_token_logging()
    settings = config.load()
    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)

    spotify = SpotifyClient(settings)
    telegram_bot = SyncBot(settings.telegram_bot_token)
    try:
        run_poll(conn, clock.RealClock(), settings, spotify, telegram_bot)
    finally:
        telegram_bot.close()
        spotify.close()
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    main()
