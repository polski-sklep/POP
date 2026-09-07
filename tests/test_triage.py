"""Tests for triage.py — Phase 3.

Two hard rules govern this file, as they do the whole suite:

* **No live Telegram call, ever.** `bot` is a `FakeBot` that records the
  kwargs it was handed. `main()` is the only thing that builds a real client
  and it is never called here.
* **No live Spotify call, ever.** `FakeSpotify` from `tests.fixtures_spotify`
  is injected, and `run_triage` is asserted not to touch it at all.

The ordering test is the important one. CONTRACTS.md §5 pins five steps in a
specific sequence and says the order matters; `test_run_triage_call_order`
asserts the sequence directly rather than hoping a behavioural test would
notice a swap.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import triage  # noqa: E402
from tests.fixtures_spotify import (  # noqa: E402
    FINISHED,
    LONG,
    MEDIUM,
    SHORT,
    fake_spotify,
)

# 2026-08-30 is a Sunday. Lisbon is WEST (UTC+1) in August, so Sunday 18:00
# local is 17:00 UTC — the instant that opens a week.
WEEK_1 = "2026-08-30T17:00:00+00:00"
WEEK_2 = "2026-09-06T17:00:00+00:00"
WEEK_3 = "2026-09-13T17:00:00+00:00"
WEEK_4 = "2026-09-20T17:00:00+00:00"

# Tuesday, comfortably more than 48h before WEEK_1.
CAPTURED = "2026-08-25T10:00:00+00:00"


# ==========================================================================
# Doubles and helpers
# ==========================================================================

class FakeBot:
    """Records what would have been sent. Opens no socket, awaits nothing."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return None

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


class AsyncFakeBot:
    """A bot whose `send_message` returns a coroutine, like the real one."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.pending: list = []

    def send_message(self, **kwargs):
        async def _go():
            self.sent.append(kwargs)

        coro = _go()
        self.pending.append(coro)
        return coro

    def drain(self) -> None:
        """Close any coroutine that was never awaited, so pytest stays quiet."""
        for coro in self.pending:
            coro.close()


@pytest.fixture()
def settings():
    return config.test_settings()


@pytest.fixture()
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def spotify():
    return fake_spotify()


def seed(conn, clk, settings, episode, *, why="Looked good", deadline=None):
    """Capture one episode. Mirrors what bot.py does at capture time."""
    db.upsert_episode(
        conn, episode.spotify_id, episode.title, episode.show,
        episode.description, episode.release_date, episode.duration_ms,
    )
    return db.capture(
        conn, clk, settings,
        spotify_id=episode.spotify_id, why_note=why, deadline=deadline,
    )


def states(conn) -> dict[int, str]:
    return {i.id: i.state for i in db.all_items(conn, include_removed=True)}


def cycles(conn) -> dict[int, int]:
    return {i.id: i.cycles_seen for i in db.all_items(conn, include_removed=True)}


# ==========================================================================
# render_item — CONTRACTS.md §5, and the brief's central mechanism
# ==========================================================================

def _week(**over):
    base = dict(week_start=WEEK_1, allowance_min=180, debt_min=0,
                promoted_min=0, listened_min=0)
    base.update(over)
    return db.Week(**base)


def test_render_item_shows_title_show_duration_note_and_cycle(conn):
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, LONG, why="Founder interview, sounded sharp")
    item = db.bump_cycle(conn, item.id)

    text, markup = triage.render_item(item, _week(), warning=False)

    assert LONG.title in text
    assert LONG.show in text
    assert clock.fmt_duration(LONG.duration_ms) in text      # 2h 47m
    assert "Founder interview, sounded sharp" in text
    assert "Cycle 1" in text
    assert markup is not None


def test_render_item_puts_the_why_note_directly_under_the_duration(conn):
    """"'Looked good' next to a two-hour duration is what kills the item.\""""
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, LONG, why="Looked good")

    text, _ = triage.render_item(item, _week(), warning=False)
    lines = text.split("\n")

    duration_line = next(i for i, l in enumerate(lines) if "2h 47m" in l)
    note_line = next(i for i, l in enumerate(lines) if "Looked good" in l)
    assert note_line == duration_line + 1


def test_render_item_buttons_match_bot_callback_contract(conn):
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, SHORT)

    _, markup = triage.render_item(item, _week(), warning=False)
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]

    assert set(payloads) == {
        f"promote:{item.id}", f"drop:{item.id}",
        f"holds_yes:{item.id}", f"holds_no:{item.id}",
    }


def test_render_item_payloads_parse_the_way_bot_py_parses_them(conn):
    import bot as bot_module

    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, SHORT)
    _, markup = triage.render_item(item, _week(), warning=False)

    for row in markup.inline_keyboard:
        for button in row:
            action, _, raw_id = button.callback_data.partition(":")
            assert action in bot_module.ACTIONS
            assert raw_id.isdigit() and int(raw_id) == item.id


def test_render_item_warning_only_when_asked(conn):
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, SHORT)

    plain, _ = triage.render_item(item, _week(), warning=False)
    warned, _ = triage.render_item(item, _week(), warning=True)

    assert triage.WARNING_LINE not in plain
    assert triage.WARNING_LINE in warned


def test_render_item_shows_a_deadline_when_there_is_one(conn):
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    plain = seed(conn, clk, settings, SHORT)
    dated = seed(conn, clk, settings, MEDIUM, deadline="2026-09-04T17:00:00+00:00")

    assert "Deadline" not in triage.render_item(plain, _week(), False)[0]
    assert "Deadline" in triage.render_item(dated, _week(), False)[0]


def test_render_item_carries_the_week_remaining(conn):
    clk = clock.FrozenClock(CAPTURED)
    settings = config.test_settings()
    item = seed(conn, clk, settings, SHORT)

    text, _ = triage.render_item(item, _week(debt_min=60, promoted_min=40), False)
    assert "80 min left this week" in text     # 180 - 60 - 40


# ==========================================================================
# run_triage — the order of operations
# ==========================================================================

def test_run_triage_call_order(conn, settings, spotify, monkeypatch):
    """CONTRACTS.md §5: rollover, lift, expire, bump+post, header. In order.

    Rollover must precede the lock lift, or an item whose lock lifts this
    minute is accounted against the week being closed rather than the one
    being opened. Expiry must precede the bump, or an item at its lifespan is
    shown a third time before it is removed.
    """
    clk = clock.FrozenClock(CAPTURED)
    live = seed(conn, clk, settings, SHORT)                 # will be lifted
    doomed = seed(conn, clk, settings, MEDIUM)              # at its lifespan
    conn.execute("UPDATE queue SET state = 'queued', cycles_seen = 2 WHERE id = ?",
                 (doomed.id,))
    conn.commit()

    calls: list[str] = []

    def record(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(db, "roll_over_week", record("roll_over_week", db.roll_over_week))
    monkeypatch.setattr(db, "lift_expired_locks",
                        record("lift_expired_locks", db.lift_expired_locks))
    real_set_state = db.set_state

    def tracked_set_state(c, k, item_id, new_state):
        if new_state == "expired":
            calls.append("expire")
        return real_set_state(c, k, item_id, new_state)

    monkeypatch.setattr(db, "set_state", tracked_set_state)
    monkeypatch.setattr(db, "bump_cycle", record("bump_cycle", db.bump_cycle))

    bot = FakeBot()
    real_send = triage.send

    def tracked_send(b, s, text, reply_markup=None):
        calls.append("header" if text.startswith("Week of") else "post")
        return real_send(b, s, text, reply_markup)

    monkeypatch.setattr(triage, "send", tracked_send)

    clk.set(WEEK_1)
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert calls == [
        "roll_over_week",
        "lift_expired_locks",
        "expire",
        "bump_cycle",
        "post",
        "header",
    ]
    assert live.id in cycles(conn) and cycles(conn)[live.id] == 1


def test_rollover_precedes_expiry_so_a_lapsing_item_can_expire(conn, settings, spotify):
    """A promoted item at its lifespan lapses at rollover, then expires.

    If expiry ran before the rollover the item would still be `promoted`,
    would not be in `triage_items`, and would survive a cycle it had already
    used up.
    """
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)
    conn.execute(
        "UPDATE queue SET state = 'promoted', cycles_seen = 2, promoted_at = ? WHERE id = ?",
        (CAPTURED, item.id),
    )
    conn.commit()

    clk.set(WEEK_1)
    triage.run_triage(conn, clk, settings, spotify, FakeBot())

    assert states(conn)[item.id] == "expired"


def test_run_triage_never_touches_spotify(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)
    clk.set(WEEK_1)

    triage.run_triage(conn, clk, settings, spotify, FakeBot())

    assert spotify.calls == []


def test_lock_is_lifted_and_the_item_is_posted(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)
    assert item.state == "locked"

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert states(conn)[item.id] == "queued"
    assert any(SHORT.title in t for t in bot.texts)


def test_still_locked_items_are_not_posted(conn, settings, spotify):
    """Captured Sunday morning, triaged Sunday evening: still inside the lock."""
    clk = clock.FrozenClock("2026-08-30T09:00:00+00:00")
    item = seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert states(conn)[item.id] == "locked"
    assert not any(SHORT.title in t for t in bot.texts)
    assert cycles(conn)[item.id] == 0


# ==========================================================================
# Lifespan
# ==========================================================================

def test_lifespan_two_cycles_then_warning_then_expiry(conn, settings, spotify):
    """Shown on cycle 1; shown with a warning on cycle 2; removed at the third."""
    assert settings.lifespan_cycles == 2

    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)

    # Cycle 1 — shown, no warning.
    clk.set(WEEK_1)
    bot1 = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot1)
    posts1 = [t for t in bot1.texts if SHORT.title in t]
    assert len(posts1) == 1
    assert "Cycle 1" in posts1[0]
    assert triage.WARNING_LINE not in posts1[0]
    assert cycles(conn)[item.id] == 1

    # Cycle 2 — shown, WITH the warning.
    clk.set(WEEK_2)
    bot2 = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot2)
    posts2 = [t for t in bot2.texts if SHORT.title in t]
    assert len(posts2) == 1
    assert "Cycle 2" in posts2[0]
    assert triage.WARNING_LINE in posts2[0]
    assert cycles(conn)[item.id] == 2

    # Third triage — expired, and NOT shown a third time.
    clk.set(WEEK_3)
    bot3 = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot3)
    assert not any(SHORT.title in t for t in bot3.texts)
    assert states(conn)[item.id] == "expired"
    assert cycles(conn)[item.id] == 2       # expired, not bumped a third time


def test_expiry_is_a_soft_delete(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)
    conn.execute("UPDATE queue SET state='queued', cycles_seen=2 WHERE id=?", (item.id,))
    conn.commit()

    clk.set(WEEK_1)
    triage.run_triage(conn, clk, settings, spotify, FakeBot())

    assert db.get_item(conn, item.id).state == "expired"
    assert item.id not in {i.id for i in db.all_items(conn)}
    assert item.id in {i.id for i in db.all_items(conn, include_removed=True)}
    assert db.get_item(conn, item.id).resolved_at is not None


def test_lifespan_respects_a_configured_value(conn, spotify):
    settings = config.test_settings(lifespan_cycles=1)
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    bot1 = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot1)
    posts = [t for t in bot1.texts if SHORT.title in t]
    assert triage.WARNING_LINE in posts[0]      # cycle 1 IS the last cycle

    clk.set(WEEK_2)
    triage.run_triage(conn, clk, settings, spotify, FakeBot())
    assert states(conn)[item.id] == "expired"


# ==========================================================================
# Deadlines — DECISIONS.md D1.10
# ==========================================================================

def test_deadline_item_survives_past_its_lifespan_then_expires_on_the_deadline(
        conn, settings, spotify):
    """D1.10: a deadline item is lifespan-exempt until the deadline passes.

    Expiring it the day before the event it was captured for would defeat the
    only reason the deadline field exists.
    """
    clk = clock.FrozenClock(CAPTURED)
    deadline = "2026-09-16T17:00:00+00:00"      # between WEEK_3 and WEEK_4
    item = seed(conn, clk, settings, MEDIUM, deadline=deadline)
    plain = seed(conn, clk, settings, SHORT)    # control, no deadline

    for week in (WEEK_1, WEEK_2, WEEK_3):
        clk.set(week)
        bot = FakeBot()
        triage.run_triage(conn, clk, settings, spotify, bot)

    # Three cycles in, well past lifespan_cycles=2, and still alive.
    assert states(conn)[item.id] == "queued"
    assert cycles(conn)[item.id] == 3
    assert any(MEDIUM.title in t for t in bot.texts)
    # The control expired on schedule.
    assert states(conn)[plain.id] == "expired"

    # The deadline passes.
    clk.set(WEEK_4)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert states(conn)[item.id] == "expired"
    assert not any(MEDIUM.title in t for t in bot.texts)


def test_deadline_exempt_item_surfaces_inside_its_lock(conn, settings, spotify):
    """A deadline inside the lock window makes the item queued at capture."""
    clk = clock.FrozenClock("2026-08-29T12:00:00+00:00")   # Saturday
    item = seed(conn, clk, settings, MEDIUM, deadline="2026-08-31T09:00:00+00:00")
    assert item.state == "queued"

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert any(MEDIUM.title in t for t in bot.texts)
    assert states(conn)[item.id] == "queued"


# ==========================================================================
# The week header
# ==========================================================================

def test_week_header_is_posted_last_with_allowance_debt_and_remaining(
        conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    header = bot.texts[-1]
    assert header.startswith("Week of")
    assert "Allowance 180 min" in header
    assert "Remaining 180 min" in header


def test_week_header_reports_carried_debt(conn, settings, spotify):
    """A blown week hands its overage to the next one, visible in the header."""
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)

    # Open the outgoing week and blow it: 240 listened against 180.
    clk.set("2026-08-24T10:00:00+00:00")
    db.current_week(conn, clk, settings)
    db.record_listening(conn, clk, SHORT.spotify_id, 240 * 60_000, 240 * 60_000, False)

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    header = bot.texts[-1]
    assert "Debt carried 60 min" in header
    assert "Remaining 120 min" in header


def test_empty_queue_still_posts_the_header(conn, settings, spotify):
    clk = clock.FrozenClock(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert triage.NOTHING_LINE in bot.texts
    assert bot.texts[-1].startswith("Week of")


def test_every_message_goes_to_the_authorised_user(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)
    clk.set(WEEK_1)
    bot = FakeBot()

    triage.run_triage(conn, clk, settings, spotify, bot)

    assert bot.sent
    assert all(m["chat_id"] == settings.telegram_user_id for m in bot.sent)


# ==========================================================================
# Idempotence — cron retries happen
# ==========================================================================

def test_run_triage_is_idempotent_within_a_week(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    first = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, first)
    after_first = (states(conn), cycles(conn))

    second = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, second)

    assert second.sent == []
    assert (states(conn), cycles(conn)) == after_first
    assert cycles(conn)[item.id] == 1


def test_a_retry_later_in_the_same_week_is_also_a_no_op(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    triage.run_triage(conn, clk, settings, spotify, FakeBot())

    clk.set("2026-09-02T08:00:00+00:00")     # Wednesday, same week
    late = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, late)

    assert late.sent == []


def test_the_next_week_runs_normally(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    triage.run_triage(conn, clk, settings, spotify, FakeBot())
    clk.set(WEEK_2)
    second = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, second)

    assert any(SHORT.title in t for t in second.texts)


# ==========================================================================
# Promotion lapse
# ==========================================================================

def test_promoted_but_unplayed_items_lapse_back_into_triage(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, MEDIUM)
    conn.execute("UPDATE queue SET state='queued' WHERE id=?", (item.id,))
    conn.commit()
    db.promote(conn, clk, settings, item.id)
    assert db.get_item(conn, item.id).state == "promoted"

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert states(conn)[item.id] == "queued"
    assert any(MEDIUM.title in t for t in bot.texts)
    assert db.get_item(conn, item.id).promoted_at is not None   # evidence survives


def test_played_items_never_appear_in_triage(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, FINISHED)
    db.set_state(conn, clk, item.id, "played")

    clk.set(WEEK_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert not any(FINISHED.title in t for t in bot.texts)
    assert states(conn)[item.id] == "played"


# ==========================================================================
# send() — the async seam, without a network
# ==========================================================================

def test_send_awaits_a_coroutine_returning_bot(settings):
    bot = AsyncFakeBot()
    triage.send(bot, settings, "hello")
    assert bot.sent == [{"chat_id": settings.telegram_user_id, "text": "hello"}]


def test_send_refuses_to_block_inside_a_running_loop(settings):
    bot = AsyncFakeBot()

    async def go():
        with pytest.raises(RuntimeError, match="synchronous cron entry points"):
            triage.send(bot, settings, "hello")

    asyncio.run(go())
    bot.drain()


def test_send_omits_reply_markup_when_there_is_none(settings):
    bot = FakeBot()
    triage.send(bot, settings, "hello")
    assert "reply_markup" not in bot.sent[0]


# ==========================================================================
# render_stats
# ==========================================================================

def test_render_stats_on_an_empty_database_does_not_crash(conn, settings):
    clk = clock.FrozenClock(WEEK_1)
    text = triage.render_stats(conn, clk, settings)

    assert "0%" not in text            # a missing denominator is never 0%
    assert text.count("—") >= 9        # 8 rates + median
    assert "All time" in text
    assert "Last 8 weeks" in text


def test_render_stats_reports_both_windows_and_the_two_extras(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    survivor = seed(conn, clk, settings, SHORT)
    dropped = seed(conn, clk, settings, MEDIUM)
    played = seed(conn, clk, settings, FINISHED)

    # One item dropped inside its lock: fails funnel stage 1.
    clk.set("2026-08-26T10:00:00+00:00")
    db.set_state(conn, clk, dropped.id, "dropped")

    clk.set(WEEK_1)
    db.lift_expired_locks(conn, clk, settings)
    db.record_still_holds(conn, survivor.id, True)
    db.promote(conn, clk, settings, survivor.id)
    db.promote(conn, clk, settings, played.id)
    db.record_listening(conn, clk, FINISHED.spotify_id, 30 * 60_000, 30 * 60_000, True)
    db.set_state(conn, clk, played.id, "played")

    text = triage.render_stats(conn, clk, settings)

    assert "3 captured" in text
    assert "(2/3)" in text             # survived lock
    assert "(2/2)" in text             # promoted
    assert "Median time to drop" in text
    assert "Still holds, yes: 100% (1 answered)" in text


def test_render_stats_windows_differ_when_data_is_old(conn, settings):
    old = clock.FrozenClock("2026-05-01T10:00:00+00:00")     # >8 weeks before
    seed(conn, old, settings, SHORT)

    clk = clock.FrozenClock(WEEK_1)
    text = triage.render_stats(conn, clk, settings)

    assert "All time — 1 captured" in text
    assert "Last 8 weeks — 0 captured" in text


def test_render_stats_never_renders_a_missing_rate_as_zero(conn, settings):
    """Nothing promoted: the played rate has a zero denominator, not a 0% result."""
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)

    clk.set(WEEK_1)
    text = triage.render_stats(conn, clk, settings)
    played_line = next(l for l in text.split("\n") if l.strip().startswith("played"))

    assert "—" in played_line
    assert "0%" not in played_line


def test_render_stats_matches_the_signature_bot_py_calls(conn, settings):
    """bot.cmd_stats calls render(conn, clk, settings) and replies with the text."""
    import inspect

    sig = inspect.signature(triage.render_stats)
    assert len(sig.parameters) == 3
    assert isinstance(triage.render_stats(conn, clock.FrozenClock(WEEK_1), settings), str)


def test_stats_since_is_eight_weeks_back(settings):
    clk = clock.FrozenClock(WEEK_1)
    expected = clock.iso(
        clock.week_start_for(clk.now() - _dt.timedelta(weeks=8), settings.tz)
    )
    assert expected < WEEK_1
    assert triage.STATS_WEEKS == 8
