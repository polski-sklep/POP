"""Phase 4 — one end-to-end lifecycle, plus regression tests for the defects
that lifecycle exposed.

The per-module suites are thorough and all of them pass. What they cannot see
is the seam: `triage.run_triage` driving `db.roll_over_week` driving
`db.set_state`, with `poll.run_poll` writing clamped deltas into the same
`weeks` row that the allocation gate reads back a week later. Everything here
is one database, one frozen clock, one `FakeSpotify` and one bot double,
walked forward in real order.

State AND `db.funnel_stats()` are asserted at every step, because a state
machine that is right and a funnel that is wrong is the failure mode this
project cannot detect from the inside — the decay curve is the output, and a
corrupted numerator looks exactly like a result.

No live call is possible from this file: the episodes come from `FakeSpotify`,
the bot is a list, and `conftest.py` turns any socket or `.env` read into a
failure.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import poll  # noqa: E402
import triage  # noqa: E402
from spotify import Episode, FakeSpotify  # noqa: E402

MIN = 60_000

# --- the cast ---------------------------------------------------------------
# Four items, chosen so that all three funnel stages end with a non-trivial
# numerator: one promoted-and-part-heard, one dropped at triage, one promoted
# and fully played, one that the allocation gate refuses and that is then
# listened to out of band anyway.

A_ID = "AaaaaaaaaaaaaaaaaaaaaA"      # 120 min — promoted, part heard, lapses, expires
B_ID = "BbbbbbbbbbbbbbbbbbbbbB"      #  40 min — dropped at the first triage
C_ID = "CccccccccccccccccccccC"      #  30 min — promoted and fully played
D_ID = "DdddddddddddddddddddddD"[:22]  # 100 min — refused by the gate, heard anyway


def _episode(spotify_id: str, title: str, minutes: int) -> Episode:
    return Episode(
        spotify_id=spotify_id,
        title=title,
        show="The Seam",
        description="Fixture.",
        release_date="2026-08-01",
        duration_ms=minutes * MIN,
        resume_position_ms=0,
        fully_played=False,
    )


A = _episode(A_ID, "The Long Haul", 120)
B = _episode(B_ID, "The Impulse", 40)
C = _episode(C_ID, "The Good One", 30)
D = _episode(D_ID, "The Overreach", 100)
CAST = {e.spotify_id: e for e in (A, B, C, D)}

# --- the calendar -----------------------------------------------------------
# Europe/Lisbon is WEST (UTC+1) throughout September 2026, so Sunday 18:00
# local is 17:00Z. Every instant below is derived, never hand-rolled, except
# these anchors.

CAPTURED = "2026-09-01T10:00:00+00:00"   # Tuesday morning, the impulse
LOCK_LIFTS = "2026-09-03T10:00:00+00:00"  # exactly 48h later
TRIAGE_1 = "2026-09-06T17:00:00+00:00"   # Sunday 18:00 local
MONDAY = "2026-09-07T09:00:00+00:00"
TUESDAY = "2026-09-08T09:00:00+00:00"
WEDNESDAY = "2026-09-09T09:00:00+00:00"
TRIAGE_2 = "2026-09-13T17:00:00+00:00"
TRIAGE_3 = "2026-09-20T17:00:00+00:00"

WEEK_1 = TRIAGE_1                        # the week opened by the first triage
WEEK_2 = TRIAGE_2


class FakeBot:
    """Records what would have been sent. Opens no socket, awaits nothing."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_message(self, **kwargs):
        self.texts.append(kwargs["text"])
        return None

    def post_for(self, title: str) -> str:
        matches = [t for t in self.texts if title in t]
        assert len(matches) == 1, f"expected exactly one post for {title!r}, got {matches}"
        return matches[0]

    def posted(self, title: str) -> bool:
        return any(title in t for t in self.texts)


@pytest.fixture()
def settings():
    return config.test_settings()


@pytest.fixture()
def conn(settings):
    connection = db.connect(settings)
    db.init_db(connection)
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture()
def spotify():
    return FakeSpotify(dict(CAST))


def _capture(conn, clk, settings, episode, note, deadline=None):
    db.upsert_episode(conn, episode.spotify_id, episode.title, episode.show,
                      episode.description, episode.release_date, episode.duration_ms)
    return db.capture(conn, clk, settings, spotify_id=episode.spotify_id,
                      why_note=note, deadline=deadline)


def _states(conn):
    return {i.id: i.state for i in db.all_items(conn, include_removed=True)}


def _funnel(conn, clk, settings):
    return db.funnel_stats(conn, clk, settings)


# ==========================================================================
# The lifecycle
# ==========================================================================

def test_full_lifecycle(conn, settings, spotify):
    clk = clock.FrozenClock(CAPTURED)

    # ------------------------------------------------------------------
    # 1. Capture. Locked, note stored, nothing promotable, nothing counted.
    # ------------------------------------------------------------------
    a = _capture(conn, clk, settings, A, "he mentioned the 2008 thing")
    b = _capture(conn, clk, settings, B, "looked good")
    c = _capture(conn, clk, settings, C, "the one Marta sent")
    d = _capture(conn, clk, settings, D, "background for the talk")

    assert _states(conn) == {a.id: "locked", b.id: "locked", c.id: "locked", d.id: "locked"}
    assert a.why_note == "he mentioned the 2008 thing"
    assert b.why_note == "looked good"      # the evidence at trial, 48h from now
    assert db.triage_items(conn, clk, settings) == []
    assert [i.id for i in db.active_items(conn)] == [a.id, b.id, c.id, d.id]

    # A locked item is not promotable, by the brief: "Locked items do not
    # appear in triage and cannot be promoted."
    with pytest.raises(db.IllegalTransition):
        db.promote(conn, clk, settings, a.id)
    assert db.get_item(conn, a.id).state == "locked"

    # Everything is still inside its lock, so nothing is determined yet.
    s = _funnel(conn, clk, settings)
    assert (s.captured, s.survived_lock, s.promoted, s.played, s.fully_played) == (0, 0, 0, 0, 0)
    assert s.survived_rate is None and s.promoted_rate is None and s.played_rate is None

    # ------------------------------------------------------------------
    # 2. The lock expires at 48h. queued, and now visible to triage.
    # ------------------------------------------------------------------
    clk.set(LOCK_LIFTS)
    lifted = db.lift_expired_locks(conn, clk, settings)
    assert {i.id for i in lifted} == {a.id, b.id, c.id, d.id}
    assert set(_states(conn).values()) == {"queued"}
    assert [i.id for i in db.triage_items(conn, clk, settings)] == [a.id, b.id, c.id, d.id]

    s = _funnel(conn, clk, settings)
    assert (s.captured, s.survived_lock, s.promoted, s.played) == (4, 4, 0, 0)
    assert s.survived_rate == 1.0
    assert s.promoted_rate == 0.0
    assert s.played_rate is None            # denominator 0, not a measured 0%

    # ------------------------------------------------------------------
    # 3. First triage. The gate charges the week; the refusal charges nothing.
    # ------------------------------------------------------------------
    clk.set(TRIAGE_1)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    for episode in (A, B, C, D):
        assert bot.posted(episode.title)
    assert "Why: looked good" in bot.post_for(B.title)      # note, next to the duration
    assert "40m" in bot.post_for(B.title)
    assert triage.WARNING_LINE not in bot.post_for(A.title)  # cycle 1 of 2
    assert {i.cycles_seen for i in db.all_items(conn)} == {1}

    week = db.current_week(conn, clk, settings)
    assert week.week_start == WEEK_1
    assert (week.allowance_min, week.debt_min, week.promoted_min) == (180, 0, 0)

    db.promote(conn, clk, settings, a.id)                   # 120 of 180
    assert db.current_week(conn, clk, settings).remaining_min == 60
    db.promote(conn, clk, settings, c.id)                   # 30 more, 30 left
    assert db.current_week(conn, clk, settings).remaining_min == 30

    # D costs 100 against 30 remaining. The gate refuses and writes nothing.
    with pytest.raises(db.AllowanceExceeded) as excinfo:
        db.promote(conn, clk, settings, d.id)
    assert excinfo.value.cost_min == 100
    assert excinfo.value.remaining_min == 30
    assert excinfo.value.overage_min == 70
    assert "70 min over" in str(excinfo.value)
    assert db.get_item(conn, d.id).state == "queued"
    assert db.current_week(conn, clk, settings).promoted_min == 150

    # The user answers "still holds?" on B and drops it. Both are recorded.
    db.record_still_holds(conn, b.id, False)
    db.set_state(conn, clk, b.id, "dropped")

    assert _states(conn) == {a.id: "promoted", b.id: "dropped",
                             c.id: "promoted", d.id: "queued"}
    assert b.id not in {i.id for i in db.all_items(conn)}            # soft deleted
    assert b.id in {i.id for i in db.all_items(conn, include_removed=True)}

    s = _funnel(conn, clk, settings)
    assert (s.captured, s.survived_lock, s.promoted, s.played, s.fully_played) == (4, 4, 2, 0, 0)
    assert s.survived_rate == 1.0            # B was dropped after its lock lifted
    assert s.promoted_rate == 2 / 4
    assert s.played_rate == 0.0
    assert s.still_holds_answered == 1 and s.still_holds_yes_rate == 0.0

    # ------------------------------------------------------------------
    # 4. Partial listening. Clamped deltas accrue; the item is not complete.
    # ------------------------------------------------------------------
    clk.set(MONDAY)
    spotify.set_position(A_ID, 50 * MIN)
    poll.run_poll(conn, clk, settings, spotify, FakeBot())

    assert db.current_week(conn, clk, settings).listened_min == 50
    assert db.get_item(conn, a.id).state == "promoted"      # heard, not complete
    s = _funnel(conn, clk, settings)
    assert (s.promoted, s.played, s.fully_played) == (2, 1, 0)

    # A backward scrub. THE CLAMP: this must credit zero, never -20 minutes.
    clk.set(TUESDAY)
    spotify.set_position(A_ID, 30 * MIN)
    spotify.set_position(C_ID, 30 * MIN, fully_played=True)
    poll.run_poll(conn, clk, settings, spotify, FakeBot())

    a_deltas = [r["delta_ms"] for r in conn.execute(
        "SELECT delta_ms FROM listening WHERE spotify_id = ? ORDER BY id", (A_ID,))]
    assert a_deltas == [50 * MIN, 0]
    assert db.current_week(conn, clk, settings).listened_min == 80   # 50 + 0 + 30
    assert db.get_item(conn, c.id).state == "played"        # fully_played, not position
    assert db.get_item(conn, a.id).state == "promoted"

    # Forward again from the scrubbed position, and D is heard out of band.
    clk.set(WEDNESDAY)
    spotify.set_position(A_ID, 95 * MIN)
    spotify.set_position(D_ID, 90 * MIN)
    poll.run_poll(conn, clk, settings, spotify, FakeBot())

    assert [r["delta_ms"] for r in conn.execute(
        "SELECT delta_ms FROM listening WHERE spotify_id = ? ORDER BY id", (A_ID,))] == \
        [50 * MIN, 0, 65 * MIN]
    assert min(r["delta_ms"] for r in conn.execute("SELECT delta_ms FROM listening")) >= 0
    week = db.current_week(conn, clk, settings)
    assert week.listened_min == 235                          # 115 + 30 + 90
    assert week.effective_allowance_min == 180               # over by 55

    s = _funnel(conn, clk, settings)
    # D was heard but never promoted, so it is not in stage 3's numerator: the
    # stage measures "promoted -> played", and a rate above 100% is not a rate.
    assert (s.promoted, s.played, s.fully_played) == (2, 2, 1)
    assert s.played_rate == 1.0 and s.fully_played_rate == 0.5

    # ------------------------------------------------------------------
    # 5. Week rollover WITH debt, and 6. the promotion lapse. Same triage.
    # ------------------------------------------------------------------
    clk.set(TRIAGE_2)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    closed = db._to_week(conn.execute(                      # noqa: SLF001 — assertion only
        "SELECT * FROM weeks WHERE week_start = ?", (WEEK_1,)).fetchone())
    assert closed.listened_min == 235

    week = db.current_week(conn, clk, settings)
    assert week.week_start == WEEK_2
    assert week.debt_min == 55                               # 235 listened - 180 effective
    assert week.effective_allowance_min == 125               # debt bites next week
    assert week.remaining_min == 125
    assert "Debt carried 55 min" in bot.texts[-1]

    # 6. The lapse: promoted-but-not-played returns to the queue. The played
    #    item is terminal and is not touched. promoted_at survives both.
    assert db.get_item(conn, a.id).state == "queued"
    assert db.get_item(conn, a.id).promoted_at is not None
    assert db.get_item(conn, c.id).state == "played"
    assert db.promoted_items(conn) == []

    # ------------------------------------------------------------------
    # 7. Second cycle. The warning is in the message the user actually gets.
    # ------------------------------------------------------------------
    assert db.get_item(conn, a.id).cycles_seen == 2
    assert triage.WARNING_LINE in bot.post_for(A.title)
    assert triage.WARNING_LINE in bot.post_for(D.title)
    assert not bot.posted(C.title)                           # played never triages
    assert not bot.posted(B.title)                           # dropped is invisible

    # Re-promotion after the lapse costs the remainder, not the full duration.
    assert db.promotion_cost_min(conn, db.get_item(conn, a.id)) == 25   # 120 - 95

    s = _funnel(conn, clk, settings)
    assert (s.captured, s.survived_lock, s.promoted, s.played, s.fully_played) == (4, 4, 2, 2, 1)

    # ------------------------------------------------------------------
    # 8. Third triage. Soft removal: gone from every view, still in stats.
    # ------------------------------------------------------------------
    clk.set(TRIAGE_3)
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)

    assert _states(conn) == {a.id: "expired", b.id: "dropped",
                             c.id: "played", d.id: "expired"}
    assert not bot.posted(A.title) and not bot.posted(D.title)
    assert triage.NOTHING_LINE in bot.texts

    visible = {i.id for i in db.all_items(conn)}
    assert visible == {c.id}                                 # played is not soft deleted
    assert db.triage_items(conn, clk, settings) == []
    assert db.active_items(conn) == []
    assert db.polling_targets(conn) == []
    assert db.get_item(conn, a.id).resolved_at is not None

    # ...and every one of them is still in the funnel.
    s = _funnel(conn, clk, settings)
    assert (s.captured, s.survived_lock, s.promoted, s.played, s.fully_played) == (4, 4, 2, 2, 1)
    assert s.survived_rate == 1.0
    assert s.promoted_rate == 0.5
    assert s.played_rate == 1.0
    assert s.fully_played_rate == 0.5
    assert s.median_days_to_drop == pytest.approx(5 + 7 / 24)
    assert s.still_holds_answered == 1 and s.still_holds_yes_rate == 0.0

    # The whole database is still there. Nothing was ever hard deleted.
    assert conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 4
    assert triage.render_stats(conn, clk, settings)           # renders, does not raise


# ==========================================================================
# Regressions for the defects the lifecycle exposed (Phase 4)
# ==========================================================================

def test_the_gate_accepts_the_exact_remainder_and_refuses_one_minute_more(
        conn, settings, spotify):
    """The boundary the allocation gate turns on: cost == remaining SUCCEEDS."""
    clk = clock.FrozenClock(CAPTURED)
    exact = _episode("EeeeeeeeeeeeeeeeeeeeeE", "Exactly Sixty", 60)
    over = _episode("FffffffffffffffffffffF", "Sixty One", 61)
    big = _episode("GgggggggggggggggggggggG"[:22], "One Twenty", 120)
    items = {}
    for ep in (big, exact, over):
        items[ep.spotify_id] = _capture(conn, clk, settings, ep, "why")
    clk.set(LOCK_LIFTS)
    db.lift_expired_locks(conn, clk, settings)

    db.promote(conn, clk, settings, items[big.spotify_id].id)
    assert db.current_week(conn, clk, settings).remaining_min == 60

    with pytest.raises(db.AllowanceExceeded) as excinfo:
        db.promote(conn, clk, settings, items[over.spotify_id].id)
    assert excinfo.value.overage_min == 1
    assert db.get_item(conn, items[over.spotify_id].id).state == "queued"
    assert db.current_week(conn, clk, settings).promoted_min == 120

    db.promote(conn, clk, settings, items[exact.spotify_id].id)
    week = db.current_week(conn, clk, settings)
    assert week.promoted_min == 180 and week.remaining_min == 0


def test_debt_shrinks_the_gate_not_just_the_report(conn, settings, spotify):
    """Debt has to reduce what the gate will actually let through."""
    clk = clock.FrozenClock(TRIAGE_1)
    item = _capture(conn, clk, settings, A, "why")
    week = db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET listened_min = 260 WHERE week_start = ?", (week.week_start,))
    conn.commit()

    clk.set(TRIAGE_2)
    _closed, opened = db.roll_over_week(conn, clk, settings)
    assert opened.debt_min == 80 and opened.effective_allowance_min == 100

    db.lift_expired_locks(conn, clk, settings)
    with pytest.raises(db.AllowanceExceeded) as excinfo:
        db.promote(conn, clk, settings, item.id)      # 120 min against 100
    assert excinfo.value.overage_min == 20


def test_a_locked_item_cannot_be_promoted_but_a_deadline_exempt_one_can(
        conn, settings, spotify):
    """The brief: locked items cannot be promoted. The deadline exemption is
    the only way into the playable slate from inside the lock window."""
    clk = clock.FrozenClock(CAPTURED)
    locked = _capture(conn, clk, settings, A, "impulse")
    exempt = _capture(conn, clk, settings, C, "event tomorrow",
                      deadline="2026-09-02T18:00:00+00:00")

    assert locked.state == "locked" and exempt.state == "queued"
    with pytest.raises(db.IllegalTransition, match="lock"):
        db.promote(conn, clk, settings, locked.id)
    assert db.get_item(conn, locked.id).state == "locked"
    assert db.current_week(conn, clk, settings).promoted_min == 0

    # The exempt item still needs its note and still costs its minutes.
    assert exempt.why_note == "event tomorrow"
    db.promote(conn, clk, settings, exempt.id)
    assert db.current_week(conn, clk, settings).promoted_min == 30


def test_out_of_band_listening_never_pushes_a_funnel_rate_above_100_percent(
        conn, settings, spotify):
    """Stage 3 is "promoted -> played". An item that was never promoted is
    recorded honestly in `listening` (D1.12) but cannot enter its numerator."""
    clk = clock.FrozenClock(CAPTURED)
    promoted = _capture(conn, clk, settings, A, "why")
    never = _capture(conn, clk, settings, D, "why")
    clk.set(LOCK_LIFTS)
    db.lift_expired_locks(conn, clk, settings)
    db.promote(conn, clk, settings, promoted.id)

    db.record_listening(conn, clk, A_ID, 10 * MIN, 10 * MIN, False)
    db.record_listening(conn, clk, D_ID, 90 * MIN, 90 * MIN, False)
    db.set_state(conn, clk, never.id, "played")      # heard to the end, unpromoted

    s = db.funnel_stats(conn, clk, settings)
    assert s.promoted == 1
    assert s.played == 1 and s.played_rate == 1.0
    assert s.fully_played == 0 and s.fully_played_rate == 0.0
    # The listening itself is not lost — it still charges the week.
    assert db.current_week(conn, clk, settings).listened_min == 100


def test_a_deadline_item_is_not_told_every_week_that_it_is_about_to_expire(
        conn, settings, spotify):
    """`_should_expire` exempts deadline items from the lifespan (D1.10). The
    warning has to agree with it, or it is a standing lie in the one message
    the user is meant to act on."""
    clk = clock.FrozenClock(CAPTURED)
    far = "2026-11-01T18:00:00+00:00"
    item = _capture(conn, clk, settings, A, "before the interview", deadline=far)

    for _ in range(4):
        clk.set(clock.next_week_start(clk.now(), settings.tz))
        bot = FakeBot()
        triage.run_triage(conn, clk, settings, spotify, bot)
        assert db.get_item(conn, item.id).state == "queued"
        assert triage.WARNING_LINE not in bot.post_for(A.title)

    assert db.get_item(conn, item.id).cycles_seen == 4       # well past lifespan 2

    # In the week the deadline actually falls in, the warning is true and fires.
    clk.set("2026-10-25T18:00:00+00:00")                     # Sunday 18:00 local
    bot = FakeBot()
    triage.run_triage(conn, clk, settings, spotify, bot)
    assert triage.WARNING_LINE in bot.post_for(A.title)

    clk.set("2026-11-01T18:00:00+00:00")
    triage.run_triage(conn, clk, settings, spotify, FakeBot())
    assert db.get_item(conn, item.id).state == "expired"


def test_the_schema_refuses_a_blank_why_note_even_by_raw_sql(conn, settings):
    """capture() is the only INSERT into `queue`, and it strips-and-rejects.
    The CHECK constraint is the same guarantee at the storage layer, so the
    invariant does not depend on that staying true."""
    db.upsert_episode(conn, A_ID, A.title, A.show, None, None, A.duration_ms)
    for blank in ("", "   ", "\t\n "):
        with pytest.raises(Exception, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO queue (spotify_id, why_note, captured_at, state) "
                "VALUES (?, ?, ?, 'locked')", (A_ID, blank, CAPTURED))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0


def test_a_week_across_the_lisbon_dst_change_is_seven_local_days(settings):
    """169 UTC hours, and triage still lands at 18:00 local on both sides."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    lisbon = ZoneInfo(settings.tz)
    mid_week = dt.datetime(2026, 10, 20, 12, 0, tzinfo=lisbon)   # before the change
    start = clock.week_start_for(mid_week, settings.tz)
    end = clock.next_week_start(mid_week, settings.tz)

    assert (end - start).total_seconds() / 3600 == 169           # not 168
    for boundary in (start, end):
        local = boundary.astimezone(lisbon)
        assert (local.hour, local.minute, local.weekday()) == (18, 0, 6)
    assert start.utcoffset() == end.utcoffset()                  # both stored UTC
    assert clock.week_start_for(end, settings.tz) == end         # 18:00 opens the new week


def test_a_part_heard_episode_does_not_arrive_with_phantom_listening_time(
        conn, settings):
    """`resume_point` is a POSITION. `poll._record` reads "no previous
    observation" as position zero, so an episode that was already part heard
    when it was captured would have every one of those pre-capture minutes
    credited to the week of its first poll — and would be charged its full
    duration by a gate that is supposed to charge the remainder (D1.7).

    Driven through the real `bot.on_message`, because the seam is the point:
    the fix lives in the capture path and only `poll.py` can show it worked.
    """
    import asyncio

    import bot

    part_heard = Episode(
        spotify_id=A_ID, title="Half Heard Already", show="The Seam",
        description=None, release_date=None,
        duration_ms=58 * MIN, resume_position_ms=21 * MIN, fully_played=False,
    )
    spotify = FakeSpotify({A_ID: part_heard})
    clk = clock.FrozenClock(CAPTURED)

    class Msg:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)

    class Update:
        def __init__(self, text):
            self.effective_user = type("U", (), {"id": settings.telegram_user_id})()
            self.effective_message = Msg(text)

    class Ctx:
        def __init__(self):
            self.bot_data = {"settings": settings, "conn": conn, "clk": clk,
                             "spotify": spotify}
            self.user_data = {}

    ctx = Ctx()
    asyncio.run(bot.on_message(Update(f"spotify:episode:{A_ID}"), ctx))
    asyncio.run(bot.on_message(Update("finish the second half"), ctx))

    item = db.all_items(conn)[0]
    assert item.why_note == "finish the second half"

    # The position is on record; none of it is credited as listening.
    baseline = [tuple(r) for r in conn.execute(
        "SELECT position_ms, delta_ms FROM listening")]
    assert baseline == [(21 * MIN, 0)]

    # The gate charges what is left to hear, not the whole episode.
    assert db.promotion_cost_min(conn, item) == 37

    clk.set(LOCK_LIFTS)
    db.lift_expired_locks(conn, clk, settings)
    db.promote(conn, clk, settings, item.id)
    assert db.current_week(conn, clk, settings).promoted_min == 37

    # A poll in which nothing was listened to credits nothing.
    poll.run_poll(conn, clk, settings, spotify, FakeBot())
    assert db.current_week(conn, clk, settings).listened_min == 0

    # And real listening after capture is still counted in full.
    spotify.set_position(A_ID, 41 * MIN)
    poll.run_poll(conn, clk, settings, spotify, FakeBot())
    assert db.current_week(conn, clk, settings).listened_min == 20
