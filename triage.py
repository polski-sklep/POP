"""triage.py — the Sunday 18:00 cron entry, the triage post, and /stats.

Phase 3. Implements CONTRACTS.md §5 exactly.

Three things in this module are load-bearing and easy to get silently wrong.

1. **The order of operations in `run_triage()`.** Rollover happens *before*
   locks are lifted. Reverse them and an item whose lock lifts this minute is
   swept into the lapse pass and charged against the week that is being
   closed, not the one being opened. Expiry happens *before* the cycle bump,
   so an item at its lifespan is removed rather than shown a third time and
   then removed.

2. **The why-note sits directly under the duration.** That juxtaposition is
   the entire mechanism the brief describes: "'Looked good' next to a
   two-hour duration is what kills the item." It is not decoration and it is
   not optional.

3. **Deadline items are exempt from lifespan expiry** until the deadline
   actually passes (DECISIONS.md D1.10). Expiring an item the day before the
   event it was captured for would defeat the only reason the field exists.

No live call is made from anywhere in this file except `main()`, which is the
production cron path and is never executed by the test suite.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import clock
import db
from clock import Clock
from config import Settings
from db import QueueItem, Week

log = logging.getLogger("pop.triage")

#: Callback payloads are ``f"{action}:{item_id}"``, matching bot.ACTIONS.
#: These strings are the contract between this module and bot.on_callback;
#: they are not free text and must not be prettified.
PROMOTE = "promote"
DROP = "drop"
HOLDS_YES = "holds_yes"
HOLDS_NO = "holds_no"

#: Key in the `meta` table recording the week whose triage has completed.
#: See the idempotence note on `run_triage`.
TRIAGE_MARKER_KEY = "last_triage_week"

#: How far back `/stats` looks for its second column.
STATS_WEEKS = 8

WARNING_LINE = "Last cycle — this expires at the next triage unless you promote it."
NOTHING_LINE = "Nothing to triage this week."

#: Lead line on an out-of-cycle deadline post sent by `poll.py`. Triage is
#: weekly and this item's deadline falls before the next one, so the weekly
#: post would never reach it in time.
DEADLINE_LEAD = "Deadline before the next triage — decide now or it lapses unseen."


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send(bot, settings: Settings, text: str, reply_markup=None) -> None:
    """Send one message to the single authorised user.

    `bot` is anything with a `send_message`. A real `telegram.Bot` returns a
    coroutine, a test double returns None; both are handled here so the cron
    entry points stay synchronous, which is what a cron entry point should be.
    """
    kwargs: dict = {"chat_id": settings.telegram_user_id, "text": text}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    result = bot.send_message(**kwargs)
    if inspect.isawaitable(result):
        _resolve(result)


def _resolve(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError(
        "run_triage() and run_poll() are synchronous cron entry points and "
        "cannot drive an async bot from inside a running event loop; wrap the "
        "bot in SyncBot, or call them from a thread with no loop."
    )


class SyncBot:  # pragma: no cover — production glue, never exercised by tests
    """A synchronous facade over a real `telegram.Bot`.

    python-telegram-bot's `Bot` is async and refuses to send until
    `initialize()` has been awaited. Cron entry points are synchronous, so the
    loop is owned here rather than smeared through `run_triage`/`run_poll`.
    """

    def __init__(self, token: str) -> None:
        from telegram import Bot

        self._loop = asyncio.new_event_loop()
        self._bot = Bot(token)
        self._loop.run_until_complete(self._bot.initialize())

    def send_message(self, **kwargs):
        return self._loop.run_until_complete(self._bot.send_message(**kwargs))

    def close(self) -> None:
        try:
            self._loop.run_until_complete(self._bot.shutdown())
        finally:
            self._loop.close()

    def __enter__(self) -> "SyncBot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _keyboard(item_id: int) -> InlineKeyboardMarkup:
    """Promote / Drop / Still holds?, with the callback payloads bot.py parses."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Promote", callback_data=f"{PROMOTE}:{item_id}"),
            InlineKeyboardButton("Drop", callback_data=f"{DROP}:{item_id}"),
        ],
        [
            InlineKeyboardButton("Still holds? Yes", callback_data=f"{HOLDS_YES}:{item_id}"),
            InlineKeyboardButton("No", callback_data=f"{HOLDS_NO}:{item_id}"),
        ],
    ])


def render_item(item: QueueItem, week: Week, warning: bool) -> tuple[str, InlineKeyboardMarkup]:
    """One triage post: title, show, duration, the why-note, and cycle age.

    The why-note is rendered on the line immediately after the duration, and
    that adjacency is the point of the whole exercise — the user's own words
    at the moment of impulse, read back next to what they actually cost.

    `warning` is the last-cycle flag; the caller decides it, because only the
    caller knows `settings.lifespan_cycles`. Plain text, no parse_mode: an
    episode title containing an underscore or an asterisk must not blow up a
    Markdown parse or, worse, silently swallow part of the title.
    """
    lines = [
        item.title,
        f"{item.show} · {clock.fmt_duration(item.duration_ms)}",
        f"Why: {item.why_note}",
    ]
    if item.deadline:
        # render_item takes no Settings (CONTRACTS.md §5), so the display zone
        # is clock.fmt_local's default, which is the configured default too.
        lines.append(f"Deadline: {clock.fmt_local(item.deadline)}")
    lines.append(
        f"Cycle {item.cycles_seen} · {week.remaining_min} min left this week"
    )
    if warning:
        lines.append(WARNING_LINE)
    return "\n".join(lines), _keyboard(item.id)


def render_deadline_notice(item: QueueItem, week: Week) -> tuple[str, InlineKeyboardMarkup]:
    """One out-of-cycle deadline post, for `poll.py` to send between triages.

    The brief requires that "an item with a deadline surfaces before it expires
    regardless of where the weekly cycle sits". Triage runs weekly, so it
    cannot satisfy that on its own; the daily poll delivers it instead
    (DECISIONS.md D5.1).

    The body is `render_item` verbatim — same fields in the same order, and
    critically the same keyboard and the same `f"{action}:{item_id}"` callback
    payloads, so `bot.on_callback` needs no knowledge that this path exists.
    The only addition is one lead line, because a post arriving on a Tuesday
    with triage buttons attached is otherwise unexplainable.

    `warning` is False: the last-cycle warning is a lifespan statement made at
    triage, and a deadline item is lifespan-exempt (D1.10). The lead line is
    the urgency this message carries.
    """
    text, markup = render_item(item, week, warning=False)
    return f"{DEADLINE_LEAD}\n{text}", markup


def render_week_header(week: Week) -> str:
    """Allowance, debt carried, remaining. Posted after the items."""
    lines = [
        f"Week of {clock.fmt_local(week.week_start)}",
        f"Allowance {week.allowance_min} min",
    ]
    if week.debt_min:
        lines.append(f"Debt carried {week.debt_min} min")
    lines.append(f"Remaining {week.remaining_min} min")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------

def _last_triage_week(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (TRIAGE_MARKER_KEY,)
    ).fetchone()
    return row["value"] if row else None


def _mark_triaged(conn, week_start: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (TRIAGE_MARKER_KEY, week_start),
    )
    conn.commit()


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def _should_expire(item: QueueItem, now: _dt.datetime, settings: Settings) -> bool:
    """Lifespan exhausted, or deadline passed.

    A deadline item is exempt from lifespan expiry until its deadline actually
    passes (DECISIONS.md D1.10). Once it has passed and the item was never
    promoted, it expires like anything else.
    """
    if item.deadline is not None:
        return clock.parse(item.deadline) <= now
    return item.cycles_seen >= settings.lifespan_cycles


def _will_expire_next_cycle(item: QueueItem, now: _dt.datetime, settings: Settings) -> bool:
    """Would `_should_expire` be true at the *next* triage, for this item?

    This is what the last-cycle warning claims, so it is what the warning has
    to be computed from. A deadline item is lifespan-exempt (D1.10), so the
    bare `cycles_seen >= lifespan_cycles` test told a deadline item every week
    that it was about to expire while `_should_expire` went on keeping it
    alive — a standing lie in the one message the user is meant to act on.
    """
    if item.deadline is not None:
        return clock.parse(item.deadline) <= clock.next_week_start(now, settings.tz)
    return item.cycles_seen >= settings.lifespan_cycles


def run_triage(conn, clk: Clock, settings: Settings, spotify, bot) -> None:
    """The Sunday 18:00 cron entry. CONTRACTS.md §5, in that exact order.

    1. `roll_over_week()` — lapse unlistened promotions, compute debt, open
       the week. First, because it is what defines which week everything
       after it is accounted against.
    2. `lift_expired_locks()` — locked → queued. After the rollover, so a
       freshly unlocked item is charged to the week that is opening rather
       than swept into the lapse pass of the week that is closing.
    3. Expire anything at or past its lifespan, and anything whose deadline
       has passed. Soft deletes, via `db.set_state`.
    4. `bump_cycle()` on each survivor, then post it. After the bump,
       `cycles_seen == settings.lifespan_cycles` is the last-cycle warning.
    5. Post the week header.

    **Idempotent.** Cron retries happen, and steps 3 and 4 are not naturally
    repeatable — a second run in the same week would bump every cycle again
    and expire a whole generation of items early. The week whose triage has
    completed is therefore recorded in `meta`, and a repeat run for the same
    week returns without touching anything. The marker is written last, so a
    run that dies partway is retried rather than skipped.

    `spotify` is accepted for interface symmetry with `run_poll` and is
    deliberately unused: positions are `poll.py`'s job, and the weekly triage
    post — the one moment the whole system exists for — must not be
    preventable by a Spotify outage.
    """
    now = clk.now()
    week_start = clock.iso(clock.week_start_for(now, settings.tz))
    if _last_triage_week(conn) == week_start:
        log.info("triage for %s already ran; nothing to do", week_start)
        return

    # 1. Close the outgoing week, lapse its promotions, open this one.
    _closed, week = db.roll_over_week(conn, clk, settings)

    # 2. Lift the locks whose 48 hours have elapsed.
    db.lift_expired_locks(conn, clk, settings)

    # 3. Expire, as soft deletes.
    survivors: list[QueueItem] = []
    for item in db.triage_items(conn, clk, settings):
        if _should_expire(item, now, settings):
            db.set_state(conn, clk, item.id, "expired")
        else:
            survivors.append(item)

    # 4. Bump, then post. The bump comes first so the posted cycle age is the
    #    cycle the user is looking at, not the one before it.
    for item in survivors:
        bumped = db.bump_cycle(conn, item.id)
        warning = _will_expire_next_cycle(bumped, now, settings)
        text, markup = render_item(bumped, week, warning)
        send(bot, settings, text, markup)

    if not survivors:
        send(bot, settings, NOTHING_LINE)

    # 5. The week header, last: it describes the state everything above left.
    send(bot, settings, render_week_header(db.current_week(conn, clk, settings)))

    _mark_triaged(conn, week_start)


# --------------------------------------------------------------------------
# /stats
# --------------------------------------------------------------------------

def _pct(rate: float | None) -> str:
    """A rate, or an em dash. Never 0% for a missing denominator, never a crash."""
    if rate is None:
        return "—"
    return f"{rate * 100:.0f}%"


def _ratio(numerator: int, denominator: int) -> str:
    return f"({numerator}/{denominator})"


def _funnel_block(title: str, stats: db.FunnelStats) -> list[str]:
    return [
        f"{title} — {stats.captured} captured",
        f"  survived lock   {_pct(stats.survived_rate):>5}  "
        f"{_ratio(stats.survived_lock, stats.captured)}",
        f"  promoted        {_pct(stats.promoted_rate):>5}  "
        f"{_ratio(stats.promoted, stats.survived_lock)}",
        f"  played          {_pct(stats.played_rate):>5}  "
        f"{_ratio(stats.played, stats.promoted)}",
        f"  fully played    {_pct(stats.fully_played_rate):>5}  "
        f"{_ratio(stats.fully_played, stats.promoted)}",
    ]


def render_stats(conn, clk: Clock, settings: Settings) -> str:
    """The single `/stats` message: the decay curve and nothing else.

    Three funnel rates over all time and over the last eight weeks, plus
    median time-to-drop and the still-holds yes rate. The brief is explicit
    that this fits in one message and carries nothing further.

    Every rate can legitimately be `None` — an empty database has a zero
    denominator at every stage — and is rendered as an em dash. Rendering it
    as 0% would claim a measured failure where there is simply no data.
    """
    all_time = db.funnel_stats(conn, clk, settings)
    since = clock.iso(
        clock.week_start_for(clk.now() - _dt.timedelta(weeks=STATS_WEEKS), settings.tz)
    )
    recent = db.funnel_stats(conn, clk, settings, since=since)

    lines = ["POP funnel", ""]
    lines += _funnel_block("All time", all_time)
    lines.append("")
    lines += _funnel_block(f"Last {STATS_WEEKS} weeks", recent)
    lines.append("")

    median = all_time.median_days_to_drop
    lines.append(
        "Median time to drop: "
        + ("—" if median is None else f"{median:.1f} days")
    )
    lines.append(
        f"Still holds, yes: {_pct(all_time.still_holds_yes_rate)} "
        f"({all_time.still_holds_answered} answered)"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Cron entry point
# --------------------------------------------------------------------------

def main() -> None:  # pragma: no cover — production only, never run by tests
    """Sunday 18:00 cron. The only place real credentials are touched."""
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
        run_triage(conn, clock.RealClock(), settings, spotify, telegram_bot)
    finally:
        telegram_bot.close()
        spotify.close()
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    main()
