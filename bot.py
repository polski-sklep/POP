"""The Telegram bot: capture, the queue view, inline triage buttons, /stats.

Phase 2, Agent C. Implements CONTRACTS.md §4 exactly.

Two rules govern this module and everything else is detail:

1. **Nothing is written to the database until the why-note arrives.** The
   pending capture lives in `context.user_data` between the link and the
   reply. If the user walks away, no `episodes` row and no `queue` row is
   ever created. Writing a row and patching the note in afterwards would
   invert the entire mechanism the app exists to implement.
2. **One authorised user.** Every update is checked against
   `settings.telegram_user_id`. Anything else is dropped with no reply at
   all — nothing the sender can observe.

`db` and `triage` are imported lazily, inside the handlers, so that this
module imports cleanly when they are absent or mid-write, and so that tests
can inject fakes through `bot_data` without touching the real modules.
`spotify` is injected as an object and never constructed here.
"""
from __future__ import annotations

import functools
import logging
import re
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import clock
from config import Settings

log = logging.getLogger("pop.bot")

#: Key under which the in-flight capture is parked in ``context.user_data``.
PENDING_KEY = "pending_capture"

ACTIONS = ("promote", "drop", "holds_yes", "holds_no")
TERMINAL_STATES = ("dropped", "expired", "played")

ALREADY_RESOLVED = "Already resolved."


# --------------------------------------------------------------------------
# Lazy dependency access
# --------------------------------------------------------------------------

def _bot_data(context) -> dict:
    return getattr(context, "bot_data", None) or {}


def _settings(context) -> Settings | None:
    return _bot_data(context).get("settings")


def _db(context):
    """The `db` module, or the test double parked in ``bot_data['db']``.

    Imported lazily so bot.py stays importable while db.py is being written
    and so tests never touch the real module.
    """
    override = _bot_data(context).get("db")
    if override is not None:
        return override
    import db  # noqa: PLC0415 — deliberate lazy import

    return db


def _parse_episode_ref(spotify, text: str) -> str:
    """`spotify.parse_episode_ref` is a module function, not a client method.

    Prefer one hanging off the injected client (test doubles supply it), and
    fall back to the real module in production. Raises `InvalidEpisodeRef`,
    which is a `ValueError`, with a user-facing message.
    """
    fn = getattr(spotify, "parse_episode_ref", None)
    if fn is None:
        from spotify import parse_episode_ref as fn  # noqa: PLC0415

    return fn(text)


def _looks_like_spotify_ref(text: str) -> bool:
    low = text.lower()
    return "open.spotify.com" in low or "spotify:" in low


# --------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------

def authorised_only(handler):
    """Drop every update that is not from `settings.telegram_user_id`.

    Silently. No reply, no error, nothing the sender can observe. The brief
    is explicit: reject everything else silently.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        settings = _settings(context)
        user = getattr(update, "effective_user", None)
        if settings is None or user is None or user.id != settings.telegram_user_id:
            log.debug("dropping unauthorised update from %r", getattr(user, "id", None))
            return None
        return await handler(update, context, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# Deadline parsing
# --------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Greedy prefix, so this anchors on the LAST "by"/"before" in the text.
_TRAILER_RE = re.compile(
    r"^(?P<note>.*)\b(?:by|before)\b\s+(?P<when>\S[^\n]*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DAY_MONTH_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?$", re.IGNORECASE)
_MONTH_DAY_RE = re.compile(r"^([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?$", re.IGNORECASE)

DEADLINE_HOUR = 18


def _month_from(token: str) -> int | None:
    token = token.lower().rstrip(".")
    if len(token) < 3:
        return None
    hits = {n for name, n in _MONTHS.items() if name.startswith(token)}
    return hits.pop() if len(hits) == 1 else None


def _at_deadline_hour(day: datetime, tz: str) -> datetime:
    return datetime(day.year, day.month, day.day, DEADLINE_HOUR, 0, tzinfo=ZoneInfo(tz))


def _resolve_when(candidate: str, now_local: datetime, tz: str) -> datetime | None:
    """Resolve one recognised date form to 18:00 local, or None.

    Forms that are fully specified (ISO, today, tomorrow) are taken
    literally. Forms that are underspecified (a bare weekday, a day+month
    with no year) roll forward to the next future occurrence — that is the
    only reading a user can have meant.
    """
    c = candidate.strip().strip(".,;!?").strip().lower()
    if not c:
        return None

    if c in ("today", "tonight"):
        return _at_deadline_hour(now_local, tz)
    if c == "tomorrow":
        return _at_deadline_hour(now_local + timedelta(days=1), tz)

    m = _ISO_RE.match(c)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d, DEADLINE_HOUR, 0, tzinfo=ZoneInfo(tz))
        except ValueError:
            return None

    if c in _WEEKDAYS:
        target = _WEEKDAYS[c]
        ahead = (target - now_local.weekday()) % 7
        when = _at_deadline_hour(now_local + timedelta(days=ahead), tz)
        if when <= now_local:
            when = _at_deadline_hour(now_local + timedelta(days=ahead + 7), tz)
        return when

    day = month = None
    m = _DAY_MONTH_RE.match(c)
    if m:
        day, month = int(m.group(1)), _month_from(m.group(2))
    else:
        m = _MONTH_DAY_RE.match(c)
        if m:
            month, day = _month_from(m.group(1)), int(m.group(2))
    if day is None or month is None:
        return None

    for year in (now_local.year, now_local.year + 1):
        try:
            when = datetime(year, month, day, DEADLINE_HOUR, 0, tzinfo=ZoneInfo(tz))
        except ValueError:
            return None
        if when > now_local:
            return when
    return None


def parse_deadline(text: str, clk, tz: str) -> tuple[str, str | None]:
    """Split a trailing `by <date>` / `before <date>` off a why-note.

    Returns `(note_without_the_deadline, deadline_iso_or_None)`.

    Anything unrecognised is left alone and comes back as part of the note.
    A misparse must never eat the user's words, so the whole remainder after
    `by`/`before` has to be consumed by a recognised date form — "by the way
    this looked good" leaves "the way this looked good" unparsed and so
    stays intact, note and all.
    """
    original = text or ""
    m = _TRAILER_RE.match(original)
    if not m:
        return original.strip(), None

    now_local = clk.now().astimezone(ZoneInfo(tz))
    when = _resolve_when(m.group("when"), now_local, tz)
    if when is None:
        return original.strip(), None

    note = m.group("note").strip().rstrip(",;-–—").strip()
    return note, clock.iso(when)


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

def _episode_line(title: str, show: str, duration_ms: int) -> str:
    """Title, show and duration — the time cost, visible at capture."""
    return f"{title}\n{show} · {clock.fmt_duration(duration_ms)}"


def _confirmation(item, settings: Settings) -> str:
    lines = ["Captured.", "", _episode_line(item.title, item.show, item.duration_ms), "", f"Why: {item.why_note}"]
    if item.deadline:
        lines.append(f"Deadline: {clock.fmt_local(item.deadline, settings.tz)}")
    if item.state == "locked":
        unlocks = clock.lock_expires_at(item.captured_at, settings.lock_hours)
        lines.append(f"Locked until {clock.fmt_local(unlocks, settings.tz)}.")
    else:
        lines.append("In the queue now — the deadline falls inside the lock.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Capture conversation
# --------------------------------------------------------------------------

@authorised_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One text handler for both halves of the capture conversation."""
    message = update.effective_message
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return

    pending = context.user_data.get(PENDING_KEY)
    # A link mid-conversation replaces the pending capture rather than
    # queueing behind it, and anything that looks like a Spotify ref is
    # treated as a link attempt so a bad one is never swallowed as a note.
    if pending is None or _looks_like_spotify_ref(text):
        await _handle_link(update, context, text)
    else:
        await _handle_note(update, context, text, pending)


async def _handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    message = update.effective_message
    spotify = _bot_data(context).get("spotify")

    try:
        spotify_id = _parse_episode_ref(spotify, text)
    except ValueError as exc:
        # InvalidEpisodeRef carries a user-facing message; show it verbatim.
        # Never silently ignore malformed input.
        await message.reply_text(str(exc) or "That is not a Spotify episode link.")
        return

    try:
        episode = spotify.get_episode(spotify_id)
    except Exception:  # noqa: BLE001 — any Spotify failure is the same to the user
        log.exception("get_episode failed for %s", spotify_id)
        await message.reply_text("Could not reach Spotify just now. Send the link again in a moment.")
        return

    # Held in memory only. Nothing has touched the database.
    context.user_data[PENDING_KEY] = {"spotify_id": spotify_id, "episode": episode}
    await message.reply_text(
        _episode_line(episode.title, episode.show, episode.duration_ms) + "\n\nWhy?"
    )


async def _handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, pending: dict) -> None:
    message = update.effective_message
    settings = _settings(context)
    conn = _bot_data(context).get("conn")
    clk = _bot_data(context).get("clk")
    db = _db(context)
    episode = pending["episode"]

    note, deadline = parse_deadline(text, clk, settings.tz)
    if not note.strip():
        # The note is the whole point; a bare date is not one.
        await message.reply_text("I need a reason, not just a date. Why this one?")
        return

    db.upsert_episode(
        conn,
        pending["spotify_id"],
        episode.title,
        episode.show,
        getattr(episode, "description", None),
        getattr(episode, "release_date", None),
        episode.duration_ms,
    )
    try:
        item = db.capture(
            conn, clk, settings,
            spotify_id=pending["spotify_id"],
            why_note=note,
            deadline=deadline,
        )
    except ValueError as exc:
        log.warning("capture rejected: %s", exc)
        await message.reply_text("That did not stick. Send me the link again.")
        return

    # Seed the measurement baseline with the position the episode is already
    # at, as a zero-delta observation. `resume_point` is a POSITION, and
    # `poll._record` treats "no previous observation" as position 0, so
    # without this the first poll credits everything the user had heard
    # BEFORE the item was ever captured as listening time inside the current
    # week — and `promotion_cost_min()` charges the full duration for an
    # episode that is already half heard, which is the exact case D1.7 says
    # should cost half. Delta 0: the position is recorded, not credited.
    if getattr(episode, "resume_position_ms", 0):
        db.record_listening(
            conn, clk, pending["spotify_id"],
            int(episode.resume_position_ms), 0,
            bool(getattr(episode, "fully_played", False)),
        )

    context.user_data.pop(PENDING_KEY, None)
    await message.reply_text(_confirmation(item, settings))


# --------------------------------------------------------------------------
# Inline buttons
# --------------------------------------------------------------------------

@authorised_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, raw_id = (query.data or "").partition(":")
    if action not in ACTIONS or not raw_id.isdigit():
        await query.answer("Unrecognised button.")
        return
    item_id = int(raw_id)

    settings = _settings(context)
    conn = _bot_data(context).get("conn")
    clk = _bot_data(context).get("clk")
    db = _db(context)

    item = db.get_item(conn, item_id)
    # get_item is the one reader that DOES return soft-deleted rows, by id,
    # precisely so a button can tell "already resolved" from "never existed".
    # Both answers are the same to the user here, and neither is a raise.
    if item is None or item.state in TERMINAL_STATES:
        await query.answer(ALREADY_RESOLVED)
        return

    if action in ("holds_yes", "holds_no"):
        # Recorded independently of promote/drop. This is the cleanest
        # signal in the system and is never inferred from the other buttons.
        db.record_still_holds(conn, item_id, action == "holds_yes")
        await query.answer("Noted: still holds." if action == "holds_yes" else "Noted: no longer holds.")
        return

    if action == "drop":
        db.set_state(conn, clk, item_id, "dropped")
        await query.answer("Dropped.")
        return

    # promote
    if item.state == "promoted":
        await query.answer("Already promoted.")
        return
    try:
        db.promote(conn, clk, settings, item_id)
    except db.AllowanceExceeded as exc:
        # The gate refused: nothing was promoted. Say by how much.
        await query.answer(
            f"That would put you {exc.overage_min} min over. {exc.remaining_min} min left this week.",
            show_alert=True,
        )
        return
    except db.IllegalTransition:
        await query.answer(ALREADY_RESOLVED)
        return
    await query.answer("Promoted.")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

START_TEXT = (
    "POP holds a podcast episode for 48h before it is eligible, then you triage "
    "it against a weekly time allowance.\n\n"
    "Send me a Spotify episode link and I will ask you why."
)


@authorised_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


@authorised_only
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The current unlocked queue, read-only. No buttons: triage owns those."""
    settings = _settings(context)
    conn = _bot_data(context).get("conn")
    clk = _bot_data(context).get("clk")
    db = _db(context)

    items = db.triage_items(conn, clk, settings)
    if not items:
        await update.effective_message.reply_text("Nothing unlocked right now.")
        return

    blocks = []
    for item in items:
        block = _episode_line(item.title, item.show, item.duration_ms) + f"\nWhy: {item.why_note}"
        if item.deadline:
            block += f"\nDeadline: {clock.fmt_local(item.deadline, settings.tz)}"
        blocks.append(block)
    await update.effective_message.reply_text("\n\n".join(blocks))


@authorised_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delegates to triage.render_stats, which Phase 3 owns.

    Imported lazily so bot.py neither hard-fails nor refuses to start when
    triage.py is not there yet.
    """
    render = _bot_data(context).get("render_stats")
    if render is None:
        try:
            from triage import render_stats as render  # noqa: PLC0415
        except ImportError:
            log.warning("triage.render_stats unavailable")
            await update.effective_message.reply_text("Stats are not wired up yet.")
            return

    settings = _settings(context)
    text = render(_bot_data(context).get("conn"), _bot_data(context).get("clk"), settings)
    await update.effective_message.reply_text(text)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

GENERIC_FAILURE = (
    "Something went wrong on my end. Nothing was changed — try that again."
)


def _scrub(text: str, settings: Settings | None) -> str:
    """Remove the bot token from anything about to be logged.

    httpx and telegram exceptions can carry the full request URL, and the
    Telegram API puts the token in the path. D5.5 stopped that happening on the
    happy path; this stops the error path putting it back.
    """
    token = getattr(settings, "telegram_bot_token", "") if settings else ""
    return text.replace(token, "<TOKEN>") if token else text


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled handler exception and tell the user it failed.

    Without this, python-telegram-bot logs the traceback and drops the update
    silently: from the user's side the bot simply ignores them, which is
    indistinguishable from the deliberate silent rejection of an unauthorised
    sender. That ambiguity is the reason this exists.

    Two constraints:

    * The reply is a fixed string, never the exception text. Exception messages
      can contain request URLs, and those contain the token.
    * It replies only to the authorised user. An unauthorised sender must still
      observe nothing at all, even when their update is what crashed us.

    Never raises: a failure inside the error handler would be unhandleable.
    """
    settings = _settings(context)
    err = getattr(context, "error", None)

    detail = "".join(
        traceback.format_exception(type(err), err, err.__traceback__)
    ) if err is not None else "no exception attached"
    log.error("unhandled error while processing an update:\n%s", _scrub(detail, settings))

    user = getattr(update, "effective_user", None)
    if settings is None or user is None or user.id != settings.telegram_user_id:
        return

    try:
        query = getattr(update, "callback_query", None)
        if query is not None:
            await query.answer(GENERIC_FAILURE, show_alert=True)
            return
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text(GENERIC_FAILURE)
    except Exception:  # pragma: no cover — telling the user failed too
        log.exception("could not deliver the failure notice")


def build_app(settings: Settings, conn, clk, spotify) -> Application:
    """Assemble the Application. Constructs no client and opens no socket.

    `spotify` is any object with the `SpotifyClient` surface; the bot never
    builds one itself, so tests inject `FakeSpotify`.
    """
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data.update({
        "settings": settings,
        "conn": conn,
        "clk": clk,
        "spotify": spotify,
    })

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def _silence_token_logging() -> None:
    """Stop httpx logging the bot token in plaintext on every API call.

    httpx logs each request at INFO including the full URL, and the Telegram
    Bot API puts the token *in the path*:

        INFO:httpx:HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getMe

    A long-polling bot makes one of those calls every few seconds forever, so
    at INFO the token ends up written to the logfile thousands of times a day —
    a file that gets tailed, scp'd to the VPS and swallowed by any log shipper.
    Raising httpx to WARNING keeps real failures visible and drops the URLs.

    Shared by all three entry points; poll.py and triage.py import it.
    """
    for noisy in ("httpx", "httpcore", "telegram.request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:  # pragma: no cover — the production entry point
    import config
    import db as _dbmod
    from spotify import SpotifyClient

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    _silence_token_logging()
    settings = config.load()
    conn = _dbmod.connect(settings)
    _dbmod.init_db(conn)
    _dbmod.migrate(conn)
    app = build_app(settings, conn, clock.RealClock(), SpotifyClient(settings))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":  # pragma: no cover
    main()
