"""Tests for poll.py — Phase 3.

No live Spotify call and no live Telegram call anywhere in this file.
`FakeSpotify` from `tests.fixtures_spotify` supplies every position and a
`FakeBot` records every message. `main()` is the only thing that builds real
clients and it is never invoked.

The test that matters most is
`test_a_short_response_does_not_misattribute_listening_time`.
`get_episodes()` skips nulls and can return fewer episodes than were asked
for; an implementation that zipped the response against the request would
credit one episode's listening time to a different episode, silently and
permanently. That test fails loudly for any such implementation.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot as bot_module  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import poll  # noqa: E402
import spotify as spotify_module  # noqa: E402
import triage  # noqa: E402
from tests.fixtures_spotify import (  # noqa: E402
    FINISHED,
    LONG,
    MEDIUM,
    SHORT,
    fake_spotify,
    make_episode,
)

MIN = 60_000

# Monday of the week that opened at Sunday 2026-08-30 18:00 Lisbon (17:00 UTC).
WEEK_START = "2026-08-30T17:00:00+00:00"
MONDAY = "2026-08-31T09:00:00+00:00"
TUESDAY = "2026-09-01T09:00:00+00:00"
WEDNESDAY = "2026-09-02T09:00:00+00:00"

CAPTURED = "2026-08-25T10:00:00+00:00"
INSIDE_LOCK = "2026-08-26T09:00:00+00:00"       # 23h after CAPTURED


# ==========================================================================
# Doubles and helpers
# ==========================================================================

class FakeBot:
    """Records what would have been sent. Opens no socket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return None

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


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


def seed(conn, clk, settings, episode, *, why="Looked good", state="queued"):
    """Capture one episode and force it into `state`, bypassing the lock."""
    db.upsert_episode(
        conn, episode.spotify_id, episode.title, episode.show,
        episode.description, episode.release_date, episode.duration_ms,
    )
    item = db.capture(conn, clk, settings, spotify_id=episode.spotify_id, why_note=why)
    if state != item.state:
        conn.execute("UPDATE queue SET state = ? WHERE id = ?", (state, item.id))
        conn.commit()
    return db.get_item(conn, item.id)


def rows(conn, spotify_id: str) -> list[tuple[int, int]]:
    """(position_ms, delta_ms) for one episode, oldest first."""
    return [
        (r["position_ms"], r["delta_ms"])
        for r in conn.execute(
            "SELECT position_ms, delta_ms FROM listening WHERE spotify_id = ? "
            "ORDER BY polled_at, id",
            (spotify_id,),
        ).fetchall()
    ]


def listened_min(conn, week_start: str = WEEK_START) -> int:
    row = conn.execute(
        "SELECT listened_min FROM weeks WHERE week_start = ?", (week_start,)
    ).fetchone()
    return int(row["listened_min"]) if row else 0


def alerts(conn) -> set[tuple[str, str]]:
    return {
        (r["week_start"], r["kind"])
        for r in conn.execute("SELECT week_start, kind FROM alerts_sent").fetchall()
    }


# ==========================================================================
# Keying by spotify_id — the dangerous bug
# ==========================================================================

def test_a_short_response_does_not_misattribute_listening_time(conn, settings):
    """`get_episodes()` skips nulls, so the response can be shorter than the request.

    Targets come back sorted: SHORT, MEDIUM, LONG. Spotify knows nothing about
    SHORT, so the response is [MEDIUM, LONG]. Zipping by index would credit
    MEDIUM's minutes to SHORT and LONG's to MEDIUM. Keying by `spotify_id` is
    the only correct read.
    """
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)
    seed(conn, clk, settings, MEDIUM)
    seed(conn, clk, settings, LONG)

    assert db.polling_targets(conn) == [SHORT.spotify_id, MEDIUM.spotify_id, LONG.spotify_id]

    spot = fake_spotify({MEDIUM.spotify_id: MEDIUM, LONG.spotify_id: LONG})
    spot.set_position(MEDIUM.spotify_id, 10 * MIN)
    spot.set_position(LONG.spotify_id, 30 * MIN)

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, SHORT.spotify_id) == []                    # absent, not guessed at
    assert rows(conn, MEDIUM.spotify_id) == [(10 * MIN, 10 * MIN)]
    assert rows(conn, LONG.spotify_id) == [(30 * MIN, 30 * MIN)]
    assert listened_min(conn) == 40


def test_a_gap_in_the_middle_of_the_response_is_handled(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT)
    seed(conn, clk, settings, MEDIUM)
    seed(conn, clk, settings, LONG)

    spot = fake_spotify({SHORT.spotify_id: SHORT, LONG.spotify_id: LONG})
    spot.set_position(SHORT.spotify_id, 5 * MIN)
    spot.set_position(LONG.spotify_id, 20 * MIN)

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, SHORT.spotify_id) == [(5 * MIN, 5 * MIN)]
    assert rows(conn, MEDIUM.spotify_id) == []
    assert rows(conn, LONG.spotify_id) == [(20 * MIN, 20 * MIN)]


def test_the_request_asks_for_every_polling_target(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT, state="locked")
    seed(conn, clk, settings, MEDIUM, state="queued")
    seed(conn, clk, settings, LONG, state="promoted")

    spot = fake_spotify()
    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    requested = [args for name, args in spot.calls if name == "get_episodes"]
    assert requested == [db.polling_targets(conn)]


def test_no_targets_means_no_spotify_call(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    spot = fake_spotify()

    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert spot.calls == []


# ==========================================================================
# The clamp
# ==========================================================================

def test_forward_listening_credits_the_difference(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()

    clk.set(MONDAY)
    spot.set_position(LONG.spotify_id, 20 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    clk.set(TUESDAY)
    spot.set_position(LONG.spotify_id, 55 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, LONG.spotify_id) == [(20 * MIN, 20 * MIN), (55 * MIN, 35 * MIN)]
    assert listened_min(conn) == 55


def test_a_backward_scrub_credits_zero(conn, settings):
    """resume_point is a position, not a total. An unclamped delta would go
    negative and silently credit back time that was never earned."""
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()

    clk.set(MONDAY)
    spot.set_position(LONG.spotify_id, 90 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())
    assert listened_min(conn) == 90

    clk.set(TUESDAY)
    spot.set_position(LONG.spotify_id, 30 * MIN)      # scrubbed back an hour
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, LONG.spotify_id) == [(90 * MIN, 90 * MIN), (30 * MIN, 0)]
    assert listened_min(conn) == 90                   # unchanged, never reduced


def test_a_relisten_from_zero_credits_zero_then_climbs_again(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, MEDIUM)
    spot = fake_spotify()

    clk.set(MONDAY)
    spot.set_position(MEDIUM.spotify_id, 40 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    clk.set(TUESDAY)
    spot.set_position(MEDIUM.spotify_id, 0)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    clk.set(WEDNESDAY)
    spot.set_position(MEDIUM.spotify_id, 15 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert [d for _, d in rows(conn, MEDIUM.spotify_id)] == [40 * MIN, 0, 15 * MIN]
    assert listened_min(conn) == 55


def test_no_delta_is_ever_negative(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()

    for day, position in ((MONDAY, 60), (TUESDAY, 10), (WEDNESDAY, 5)):
        clk.set(day)
        spot.set_position(LONG.spotify_id, position * MIN)
        poll.run_poll(conn, clk, settings, spot, FakeBot())

    deltas = [d for _, d in rows(conn, LONG.spotify_id)]
    assert all(d >= 0 for d in deltas)


def test_the_clamp_comes_from_spotify_not_a_local_reimplementation(conn, settings,
                                                                  monkeypatch):
    calls: list[tuple[int, int]] = []

    def spy(previous_position_ms, current_position_ms):
        calls.append((previous_position_ms, current_position_ms))
        return spotify_module.clamped_delta(previous_position_ms, current_position_ms)

    monkeypatch.setattr(poll, "clamped_delta", spy)

    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, MEDIUM)
    spot = fake_spotify()
    spot.set_position(MEDIUM.spotify_id, 12 * MIN)

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert calls == [(0, 12 * MIN)]


# ==========================================================================
# fully_played
# ==========================================================================

def test_fully_played_flips_the_item_to_played(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, FINISHED)
    spot = fake_spotify()          # FINISHED ships fully_played=True

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert db.get_item(conn, item.id).state == "played"
    assert db.get_item(conn, item.id).resolved_at is not None


def test_completion_uses_the_flag_not_position_over_duration(conn, settings):
    """Trailing credits mean episodes rarely reach 100%, and some overshoot it.

    A position at or past the duration with `fully_played` false is NOT
    completion; the flag at 97% of the duration is.
    """
    overshot = make_episode(
        "9z8Y7x6W5v4U3t2S1r0Qp9",
        title="Ran Past The End", show="Edge Cases",
        duration_ms=12 * MIN, resume_position_ms=13 * MIN, fully_played=False,
    )
    clk = clock.FrozenClock(CAPTURED)
    over_item = seed(conn, clk, settings, overshot)
    flagged = seed(conn, clk, settings, FINISHED)

    spot = fake_spotify({overshot.spotify_id: overshot, FINISHED.spotify_id: FINISHED})
    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert db.get_item(conn, over_item.id).state == "queued"
    assert db.get_item(conn, flagged.id).state == "played"


def test_a_terminal_item_is_never_flipped(conn, settings):
    """Two captures of the same episode: one dropped, one live.

    The dropped one must keep its outcome — re-resolving it would rewrite the
    funnel result the whole system exists to measure — and flipping it would
    be an illegal transition out of a terminal state.
    """
    clk = clock.FrozenClock(CAPTURED)
    first = seed(conn, clk, settings, FINISHED)
    second = db.capture(conn, clk, settings, spotify_id=FINISHED.spotify_id,
                        why_note="second run at it")
    conn.execute("UPDATE queue SET state='queued' WHERE id=?", (second.id,))
    conn.commit()
    db.set_state(conn, clk, first.id, "dropped")

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), FakeBot())

    assert db.get_item(conn, first.id).state == "dropped"
    assert db.get_item(conn, second.id).state == "played"


def test_polling_a_played_item_twice_does_not_raise(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, FINISHED)
    spot = fake_spotify()

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert db.get_item(conn, item.id).state == "played"


# ==========================================================================
# Idempotence — cron retries happen
# ==========================================================================

def test_running_the_poll_twice_changes_nothing(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()
    spot.set_position(LONG.spotify_id, 45 * MIN)

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())
    snapshot = (rows(conn, LONG.spotify_id), listened_min(conn), alerts(conn))

    bot = FakeBot()
    poll.run_poll(conn, clk, settings, spot, bot)

    assert (rows(conn, LONG.spotify_id), listened_min(conn), alerts(conn)) == snapshot
    assert bot.sent == []


def test_an_unchanged_position_the_next_day_records_nothing_new(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, SHORT, state="locked")
    spot = fake_spotify()

    for day in (MONDAY, TUESDAY, WEDNESDAY):
        clk.set(day)
        poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, SHORT.spotify_id) == [(0, 0)]


# ==========================================================================
# Alerts
# ==========================================================================

def test_no_alert_below_the_threshold(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()
    spot.set_position(LONG.spotify_id, 100 * MIN)      # 100 of 180

    clk.set(MONDAY)
    bot = FakeBot()
    poll.run_poll(conn, clk, settings, spot, bot)

    assert bot.sent == []
    assert alerts(conn) == set()


def test_approaching_fires_once_and_never_repeats(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()
    spot.set_position(LONG.spotify_id, 150 * MIN)      # 150 of 180 = 83%

    clk.set(MONDAY)
    first = FakeBot()
    poll.run_poll(conn, clk, settings, spot, first)

    assert len(first.sent) == 1
    assert "150" in first.texts[0]
    assert alerts(conn) == {(WEEK_START, "approaching")}

    # The daily cron keeps running and the allowance stays approached.
    for day in (TUESDAY, WEDNESDAY):
        clk.set(day)
        again = FakeBot()
        poll.run_poll(conn, clk, settings, spot, again)
        assert again.sent == []

    assert alerts(conn) == {(WEEK_START, "approaching")}


def test_exceeded_fires_once_and_never_repeats(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    seed(conn, clk, settings, MEDIUM)
    spot = fake_spotify()

    clk.set(MONDAY)
    spot.set_position(LONG.spotify_id, 150 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())     # approaching

    clk.set(TUESDAY)
    spot.set_position(MEDIUM.spotify_id, 43 * MIN)          # total 193 > 180
    crossed = FakeBot()
    poll.run_poll(conn, clk, settings, spot, crossed)

    assert len(crossed.sent) == 1
    assert "Over the allowance" in crossed.texts[0]
    assert "13 min over" in crossed.texts[0]
    assert alerts(conn) == {(WEEK_START, "approaching"), (WEEK_START, "exceeded")}

    clk.set(WEDNESDAY)
    quiet = FakeBot()
    poll.run_poll(conn, clk, settings, spot, quiet)
    assert quiet.sent == []


def test_a_new_week_alerts_again(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()
    spot.set_position(LONG.spotify_id, 160 * MIN)

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    # Next week: a fresh allowance and a fresh listening window.
    clk.set("2026-09-07T09:00:00+00:00")
    spot.set_position(LONG.spotify_id, 320 * MIN)
    next_week = FakeBot()
    poll.run_poll(conn, clk, settings, spot, next_week)

    assert len(next_week.sent) == 1
    kinds = {kind for _, kind in alerts(conn)}
    assert kinds == {"approaching"}
    assert len({week for week, _ in alerts(conn)}) == 2


def test_check_alerts_is_safe_to_call_directly_and_repeatedly(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET listened_min = 175 WHERE week_start = ?", (WEEK_START,))
    conn.commit()

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    poll.check_alerts(conn, clk, settings, bot)
    poll.check_alerts(conn, clk, settings, bot)

    assert len(bot.sent) == 1


def test_a_zero_effective_allowance_reports_the_crossing_not_the_approach(conn, settings):
    """Debt can eat the whole allowance. "80% of nothing" is not a warning."""
    clk = clock.FrozenClock(MONDAY)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET debt_min = 180 WHERE week_start = ?", (WEEK_START,))
    conn.commit()

    silent = FakeBot()
    poll.check_alerts(conn, clk, settings, silent)
    assert silent.sent == []                       # nothing listened yet

    conn.execute("UPDATE weeks SET listened_min = 5 WHERE week_start = ?", (WEEK_START,))
    conn.commit()
    loud = FakeBot()
    poll.check_alerts(conn, clk, settings, loud)

    assert len(loud.sent) == 1
    assert "Over the allowance" in loud.texts[0]
    assert alerts(conn) == {(WEEK_START, "exceeded")}


def test_exactly_eighty_percent_counts_as_approaching(conn, settings):
    """One minute below the threshold is silence; the threshold itself alerts."""
    clk = clock.FrozenClock(MONDAY)
    db.current_week(conn, clk, settings)

    conn.execute("UPDATE weeks SET listened_min = 143 WHERE week_start = ?", (WEEK_START,))
    conn.commit()
    quiet = FakeBot()
    poll.check_alerts(conn, clk, settings, quiet)
    assert quiet.sent == []
    assert alerts(conn) == set()

    conn.execute("UPDATE weeks SET listened_min = 144 WHERE week_start = ?", (WEEK_START,))
    conn.commit()
    loud = FakeBot()
    poll.check_alerts(conn, clk, settings, loud)
    assert len(loud.sent) == 1


def test_alerts_go_to_the_authorised_user(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET listened_min = 200 WHERE week_start = ?", (WEEK_START,))
    conn.commit()

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)

    assert all(m["chat_id"] == settings.telegram_user_id for m in bot.sent)


def test_a_failed_send_leaves_the_alert_unrecorded_for_tomorrow(conn, settings):
    """Recording before sending would swallow the alert on a Telegram outage."""
    class BrokenBot:
        def send_message(self, **kwargs):
            raise RuntimeError("telegram is down")

    clk = clock.FrozenClock(MONDAY)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET listened_min = 200 WHERE week_start = ?", (WEEK_START,))
    conn.commit()

    with pytest.raises(RuntimeError):
        poll.check_alerts(conn, clk, settings, BrokenBot())
    assert alerts(conn) == set()

    retry = FakeBot()
    poll.check_alerts(conn, clk, settings, retry)
    assert len(retry.sent) == 2                    # approaching and exceeded
    assert alerts(conn) == {(WEEK_START, "approaching"), (WEEK_START, "exceeded")}


# ==========================================================================
# Week accounting
# ==========================================================================

def test_the_week_row_is_created_before_listening_is_recorded(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, MEDIUM)
    spot = fake_spotify()
    spot.set_position(MEDIUM.spotify_id, 20 * MIN)

    assert conn.execute("SELECT COUNT(*) c FROM weeks").fetchone()["c"] == 0

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert listened_min(conn) == 20


def test_listening_is_attributed_to_the_week_it_happened_in(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, LONG)
    spot = fake_spotify()

    clk.set(MONDAY)
    spot.set_position(LONG.spotify_id, 30 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    clk.set("2026-09-08T09:00:00+00:00")            # the following week
    spot.set_position(LONG.spotify_id, 95 * MIN)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert listened_min(conn, WEEK_START) == 30
    assert listened_min(conn, "2026-09-06T17:00:00+00:00") == 65


def test_out_of_band_listening_on_a_locked_item_is_recorded(conn, settings):
    """D1.12: polling is not restricted to the promoted slate.

    Polled 23 hours after capture, so the item is genuinely still inside its
    48h lock when the position is read. `run_poll` now lifts expired locks
    daily (D5.1); polling six days after capture, as this test used to, would
    find the lock already gone and the state assertion below would be asserting
    nothing. The claim under test is unchanged: a locked item is polled, and
    polling it does not move it.
    """
    clk = clock.FrozenClock(CAPTURED)
    item = seed(conn, clk, settings, MEDIUM, state="locked")
    spot = fake_spotify()
    spot.set_position(MEDIUM.spotify_id, 25 * MIN)

    clk.set(INSIDE_LOCK)
    poll.run_poll(conn, clk, settings, spot, FakeBot())

    assert rows(conn, MEDIUM.spotify_id) == [(25 * MIN, 25 * MIN)]
    assert db.get_item(conn, item.id).state == "locked"


def test_poll_never_hard_deletes_anything(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, FINISHED)
    before = len(db.all_items(conn, include_removed=True))

    clk.set(MONDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), FakeBot())

    assert len(db.all_items(conn, include_removed=True)) == before


# ==========================================================================
# Deadline surfacing — the brief's "surfaces before it expires regardless of
# where the weekly cycle sits" (D5.1)
# ==========================================================================
#
# The week under test opens Sunday 2026-08-30 18:00 Lisbon (17:00 UTC) and
# closes Sunday 2026-09-06 18:00 Lisbon (17:00 UTC). Every deadline below is
# placed relative to that closing boundary, because the whole question this
# feature answers is whether the weekly triage post could reach the item in
# time.

NEXT_TRIAGE = "2026-09-06T17:00:00+00:00"
THURSDAY = "2026-09-03T09:00:00+00:00"
FRIDAY = "2026-09-04T09:00:00+00:00"

TUESDAY_EVENT = "2026-09-01T20:00:00+00:00"      # inside the 48h lock
THURSDAY_EVENT = "2026-09-03T20:00:00+00:00"     # after the lock, before triage
NEXT_WEEK_EVENT = "2026-09-10T20:00:00+00:00"    # after the next triage


def seed_deadline(conn, clk, settings, episode, deadline, *, why="Interview on the day"):
    """Capture one episode with a deadline, through the real capture path."""
    db.upsert_episode(
        conn, episode.spotify_id, episode.title, episode.show,
        episode.description, episode.release_date, episode.duration_ms,
    )
    return db.capture(
        conn, clk, settings,
        spotify_id=episode.spotify_id, why_note=why, deadline=deadline,
    )


def deadline_posts(bot) -> list[dict]:
    """Only the out-of-cycle deadline messages, never the allowance alerts."""
    return [m for m in bot.sent if m["text"].startswith(triage.DEADLINE_LEAD)]


def notices(conn) -> set[int]:
    return {
        int(r["queue_id"])
        for r in conn.execute("SELECT queue_id FROM deadline_notices").fetchall()
    }


def test_a_monday_capture_for_a_tuesday_event_is_surfaced_before_it_expires(conn, settings):
    """The exact case the audit found inert.

    Captured Monday for a Tuesday event, the item is `queued` at once by the
    lock exemption — and before this existed, that was the whole mechanism. It
    was posted nowhere, and Sunday's triage soft-deleted it for having passed
    its deadline. The user never saw the item they captured.
    """
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)
    assert item.state == "queued"

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    posts = deadline_posts(bot)
    assert len(posts) == 1
    text = posts[0]["text"]
    assert MEDIUM.title in text
    assert MEDIUM.show in text
    assert clock.fmt_duration(MEDIUM.duration_ms) in text
    assert "Interview on the day" in text
    assert clock.fmt_local(TUESDAY_EVENT) in text


def test_the_deadline_post_carries_triages_own_buttons_and_callback_payloads(conn, settings):
    """`bot.on_callback` must not need to know this path exists."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    markup = deadline_posts(bot)[0]["reply_markup"]
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert payloads == [
        f"promote:{item.id}", f"drop:{item.id}",
        f"holds_yes:{item.id}", f"holds_no:{item.id}",
    ]
    for payload in payloads:
        action, _, raw_id = payload.partition(":")
        assert action in bot_module.ACTIONS and raw_id.isdigit()


def test_a_deadline_item_is_surfaced_exactly_once_across_consecutive_polls(conn, settings):
    """Without `deadline_notices` the cron would repost it every morning."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)

    bot = FakeBot()
    spot = fake_spotify()
    for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY):
        clk.set(day)
        poll.run_poll(conn, clk, settings, spot, bot)

    assert len(deadline_posts(bot)) == 1
    assert notices(conn) == {item.id}


def test_a_deadline_after_the_next_triage_is_not_surfaced_early(conn, settings):
    """Triage reaches this one in time, so the daily post would be noise."""
    clk = clock.FrozenClock(MONDAY)
    seed_deadline(conn, clk, settings, MEDIUM, NEXT_WEEK_EVENT)

    bot = FakeBot()
    spot = fake_spotify()
    for day in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY):
        clk.set(day)
        poll.run_poll(conn, clk, settings, spot, bot)

    assert deadline_posts(bot) == []
    assert notices(conn) == set()


def test_the_same_item_is_surfaced_once_the_boundary_moves_past_its_deadline(conn, settings):
    """Not surfaced early, but not lost either: the following week reaches it."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, NEXT_WEEK_EVENT)

    bot = FakeBot()
    spot = fake_spotify()
    clk.set(WEDNESDAY)
    poll.run_poll(conn, clk, settings, spot, bot)
    assert deadline_posts(bot) == []

    clk.set("2026-09-08T09:00:00+00:00")            # the week after the boundary
    poll.run_poll(conn, clk, settings, spot, bot)

    assert len(deadline_posts(bot)) == 1
    assert notices(conn) == {item.id}


def test_a_deadline_falling_exactly_on_the_triage_instant_is_surfaced(conn, settings):
    """`run_triage` expires on `deadline <= now`, so triage would not post it."""
    clk = clock.FrozenClock(MONDAY)
    seed_deadline(conn, clk, settings, MEDIUM, NEXT_TRIAGE)

    bot = FakeBot()
    clk.set(FRIDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert len(deadline_posts(bot)) == 1


def test_a_locked_deadline_item_is_unlocked_by_the_poll_and_then_surfaced(conn, settings):
    """The daily lock lift is what makes this reachable at all.

    A Thursday deadline is outside the 48h lock, so the item is `locked` at
    capture and the exemption never applies. If the lock were lifted only at
    Sunday's triage, the item would still be locked on its deadline day and
    would then be expired unseen.
    """
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, THURSDAY_EVENT)
    assert item.state == "locked"

    bot = FakeBot()
    spot = fake_spotify()

    clk.set(TUESDAY)                                # still inside the 48h
    poll.run_poll(conn, clk, settings, spot, bot)
    assert deadline_posts(bot) == []
    assert db.get_item(conn, item.id).state == "locked"

    clk.set(WEDNESDAY)                              # 48h elapsed at 09:00
    poll.run_poll(conn, clk, settings, spot, bot)
    assert db.get_item(conn, item.id).state == "queued"
    assert len(deadline_posts(bot)) == 1


def test_an_item_with_no_deadline_is_never_surfaced(conn, settings):
    clk = clock.FrozenClock(CAPTURED)
    seed(conn, clk, settings, MEDIUM)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert deadline_posts(bot) == []


def test_an_already_promoted_deadline_item_is_not_surfaced(conn, settings):
    """Surfacing exists to get a decision. The decision has been made."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)
    db.promote(conn, clk, settings, item.id)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert deadline_posts(bot) == []
    assert notices(conn) == set()


def test_a_dropped_deadline_item_is_not_surfaced(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)
    db.set_state(conn, clk, item.id, "dropped")

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert deadline_posts(bot) == []


def test_promoting_a_surfaced_item_is_still_refused_by_the_allocation_gate(conn, settings):
    """No bypass. A deadline is a delivery mechanism, not a privileged category.

    LONG costs 167 minutes. With 100 already promoted against a 180 minute
    allowance it does not fit, and being surfaced by the daily poll buys it
    nothing at the gate.
    """
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, LONG, TUESDAY_EVENT)
    db.current_week(conn, clk, settings)
    conn.execute(
        "UPDATE weeks SET promoted_min = 100 WHERE week_start = ?", (WEEK_START,)
    )
    conn.commit()

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)
    assert len(deadline_posts(bot)) == 1

    with pytest.raises(db.AllowanceExceeded) as excinfo:
        db.promote(conn, clk, settings, item.id)
    assert excinfo.value.cost_min == 167
    assert excinfo.value.overage_min == 87
    assert db.get_item(conn, item.id).state == "queued"


def test_promoting_a_surfaced_item_that_fits_charges_the_week(conn, settings):
    """The other half of the same rule: it goes through `db.promote`, normally."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)
    assert len(deadline_posts(bot)) == 1

    promoted = db.promote(conn, clk, settings, item.id)
    assert promoted.state == "promoted"
    assert db.current_week(conn, clk, settings).promoted_min == 43


def test_a_failed_send_leaves_the_deadline_notice_for_tomorrow(conn, settings):
    """D3.5's ordering. Recording first would swallow it on a Telegram outage."""
    class BrokenBot:
        def send_message(self, **kwargs):
            raise RuntimeError("telegram is down")

    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)

    clk.set(TUESDAY)
    with pytest.raises(RuntimeError):
        poll.run_poll(conn, clk, settings, fake_spotify(), BrokenBot())
    assert notices(conn) == set()

    retry = FakeBot()
    clk.set(WEDNESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), retry)

    assert len(deadline_posts(retry)) == 1
    assert notices(conn) == {item.id}


def test_the_deadline_post_goes_to_the_authorised_user(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert all(m["chat_id"] == settings.telegram_user_id for m in bot.sent)


def test_surfacing_writes_no_state_change_and_hard_deletes_nothing(conn, settings):
    """A delivery mechanism. It moves nothing through the funnel by itself."""
    clk = clock.FrozenClock(MONDAY)
    item = seed_deadline(conn, clk, settings, MEDIUM, TUESDAY_EVENT)
    before = len(db.all_items(conn, include_removed=True))

    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), FakeBot())

    after = db.get_item(conn, item.id)
    assert after.state == "queued"
    assert after.cycles_seen == 0
    assert after.promoted_at is None and after.resolved_at is None
    assert len(db.all_items(conn, include_removed=True)) == before


def test_no_deadline_items_means_no_deadline_traffic(conn, settings):
    clk = clock.FrozenClock(MONDAY)
    seed(conn, clk, settings, SHORT)
    seed(conn, clk, settings, MEDIUM)

    bot = FakeBot()
    clk.set(TUESDAY)
    poll.run_poll(conn, clk, settings, fake_spotify(), bot)

    assert bot.sent == []
