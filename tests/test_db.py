"""Tests for db.py — the state machine, the soft delete, and the allocation gate.

No network of any kind: db.py imports only stdlib, config and clock. Time is
frozen with clock.FrozenClock, settings come from config.test_settings(), and
the database is a fresh file under tmp_path per test.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

# A Tuesday. The Sunday-18:00-Lisbon boundary before it is 2026-08-30T17:00Z
# (Lisbon is UTC+1 in summer), which is what week_start_for() returns.
START = "2026-09-01T09:00:00+00:00"
WEEK_1 = "2026-08-30T17:00:00+00:00"

MIN_MS = 60_000


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return config.test_settings(db_path=tmp_path / "pop.db")


@pytest.fixture
def clk():
    return clock.FrozenClock(START)


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


# --- helpers ----------------------------------------------------------------

def make_episode(conn, spotify_id="ep0000000000000000000a", minutes=30,
                 title="An Episode", show="A Show"):
    db.upsert_episode(conn, spotify_id, title, show, "desc", "2026-08-01",
                      minutes * MIN_MS)
    return spotify_id


def capture(conn, clk, settings, *, spotify_id=None, minutes=30, note="because",
            deadline=None):
    sid = spotify_id or make_episode(conn, f"ep{len(db.all_items(conn, include_removed=True)):020d}",
                                     minutes=minutes)
    return db.capture(conn, clk, settings, spotify_id=sid, why_note=note, deadline=deadline)


def item_in_state(conn, clk, settings, state, *, minutes=30):
    """Reach `state` using only legal moves, so the fixture cannot lie."""
    item = capture(conn, clk, settings, minutes=minutes)
    if state == "locked":
        return item
    if state == "queued":
        return db.set_state(conn, clk, item.id, "queued")
    if state in ("promoted", "dropped", "expired", "played"):
        return db.set_state(conn, clk, item.id, state)
    raise AssertionError(state)


# --- ms_to_min --------------------------------------------------------------

def test_ms_to_min_matches_the_contract_formula():
    assert db.ms_to_min(0) == 0
    assert db.ms_to_min(60_000) == 1
    assert db.ms_to_min(90_000) == 1
    assert db.ms_to_min(10_800_000) == 180
    # The contract prints this exact expression; keep it bit-identical so
    # every module charges the same minutes.
    for ms in (0, 1, 29_999, 30_000, 59_999, 60_000, 12_345_678):
        assert db.ms_to_min(ms) == (ms + 29_999) // 60_000


# --- lifecycle --------------------------------------------------------------

def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    db.init_db(conn)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def test_migrate_noops_at_version_1(conn):
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def test_foreign_keys_are_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rows_come_back_as_sqlite_row(conn):
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_upsert_episode_refreshes_metadata(conn):
    make_episode(conn, "epA", minutes=30, title="Old")
    db.upsert_episode(conn, "epA", "New", "Show", None, None, 45 * MIN_MS)
    row = conn.execute("SELECT * FROM episodes WHERE spotify_id='epA'").fetchone()
    assert row["title"] == "New" and row["duration_ms"] == 45 * MIN_MS
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1


# --- the why-note -----------------------------------------------------------

@pytest.mark.parametrize("note", ["", "   ", "\t", "\n  \n", " \t\n "])
def test_empty_why_note_is_refused(conn, clk, settings, note):
    make_episode(conn, "epA")
    with pytest.raises(ValueError):
        db.capture(conn, clk, settings, spotify_id="epA", why_note=note)
    assert conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0


def test_why_note_is_stripped_but_preserved(conn, clk, settings):
    make_episode(conn, "epA")
    item = db.capture(conn, clk, settings, spotify_id="epA", why_note="  guest is good  ")
    assert item.why_note == "guest is good"


def test_every_queue_row_has_a_why_note(conn, clk, settings):
    for i in range(4):
        capture(conn, clk, settings, note=f"note {i}")
    rows = conn.execute("SELECT why_note FROM queue").fetchall()
    assert rows and all(r["why_note"].strip() for r in rows)


def test_capture_rejects_unknown_episode(conn, clk, settings):
    with pytest.raises(ValueError):
        db.capture(conn, clk, settings, spotify_id="never-fetched", why_note="x")


# --- capture, lock and the deadline exemption -------------------------------

def test_capture_starts_locked(conn, clk, settings):
    item = capture(conn, clk, settings)
    assert item.state == "locked"
    assert item.captured_at == clock.iso(clk.now())
    assert item.cycles_seen == 0
    assert item.still_holds is None
    assert item.promoted_at is None and item.resolved_at is None


def test_deadline_inside_the_lock_starts_queued(conn, clk, settings):
    deadline = clock.iso(clk.now() + dt.timedelta(hours=12))
    item = capture(conn, clk, settings, deadline=deadline)
    assert item.state == "queued"


def test_deadline_outside_the_lock_stays_locked(conn, clk, settings):
    deadline = clock.iso(clk.now() + dt.timedelta(days=10))
    item = capture(conn, clk, settings, deadline=deadline)
    assert item.state == "locked"


def test_lift_expired_locks(conn, clk, settings):
    a = capture(conn, clk, settings)
    assert db.lift_expired_locks(conn, clk, settings) == []
    clk.advance(hours=47, minutes=59)
    assert db.lift_expired_locks(conn, clk, settings) == []
    clk.advance(minutes=2)
    lifted = db.lift_expired_locks(conn, clk, settings)
    assert [i.id for i in lifted] == [a.id]
    assert db.get_item(conn, a.id).state == "queued"
    # idempotent: nothing left to lift
    assert db.lift_expired_locks(conn, clk, settings) == []


def test_triage_items_hides_locked_and_shows_deadline_exempt(conn, clk, settings):
    locked = capture(conn, clk, settings)
    exempt = capture(conn, clk, settings,
                     deadline=clock.iso(clk.now() + dt.timedelta(hours=6)))
    assert [i.id for i in db.triage_items(conn, clk, settings)] == [exempt.id]
    clk.advance(hours=49)
    db.lift_expired_locks(conn, clk, settings)
    assert {i.id for i in db.triage_items(conn, clk, settings)} == {locked.id, exempt.id}


# --- the state machine ------------------------------------------------------

LEGAL_PAIRS = [(src, dst) for src, dsts in db.LEGAL_TRANSITIONS.items() for dst in sorted(dsts)]


def test_legal_transition_table_matches_the_contract():
    assert db.LEGAL_TRANSITIONS["locked"] == frozenset(
        {"queued", "promoted", "dropped", "expired", "played"})
    assert db.LEGAL_TRANSITIONS["queued"] == frozenset(
        {"promoted", "dropped", "expired", "played"})
    assert db.LEGAL_TRANSITIONS["promoted"] == frozenset(
        {"queued", "dropped", "expired", "played"})
    for terminal in db.TERMINAL_STATES:
        assert db.LEGAL_TRANSITIONS[terminal] == frozenset()
    assert len(LEGAL_PAIRS) == 13


@pytest.mark.parametrize("src,dst", LEGAL_PAIRS)
def test_every_legal_transition_is_allowed(conn, clk, settings, src, dst):
    item = item_in_state(conn, clk, settings, src)
    moved = db.set_state(conn, clk, item.id, dst)
    assert moved.state == dst
    assert db.get_item(conn, item.id).state == dst


ILLEGAL_PAIRS = [
    # nothing leaves a terminal state, ever
    ("dropped", "queued"), ("dropped", "promoted"), ("dropped", "played"),
    ("dropped", "locked"), ("dropped", "expired"),
    ("expired", "queued"), ("expired", "played"), ("expired", "promoted"),
    ("played", "queued"), ("played", "promoted"), ("played", "dropped"),
    ("played", "expired"), ("played", "locked"),
    # nothing goes back into the lock
    ("queued", "locked"), ("promoted", "locked"),
]


@pytest.mark.parametrize("src,dst", ILLEGAL_PAIRS)
def test_illegal_transitions_raise(conn, clk, settings, src, dst):
    item = item_in_state(conn, clk, settings, src)
    with pytest.raises(db.IllegalTransition):
        db.set_state(conn, clk, item.id, dst)
    assert db.get_item(conn, item.id).state == src


@pytest.mark.parametrize("state", db.ALL_STATES)
def test_no_op_same_state_transition_raises(conn, clk, settings, state):
    item = item_in_state(conn, clk, settings, state)
    with pytest.raises(db.IllegalTransition):
        db.set_state(conn, clk, item.id, state)
    assert db.get_item(conn, item.id).state == state


def test_unknown_state_raises(conn, clk, settings):
    item = capture(conn, clk, settings)
    for bogus in ("", "PROMOTED", "archived", "deleted"):
        with pytest.raises(db.IllegalTransition):
            db.set_state(conn, clk, item.id, bogus)
    assert db.get_item(conn, item.id).state == "locked"


def test_set_state_on_missing_item_raises(conn, clk):
    with pytest.raises(LookupError):
        db.set_state(conn, clk, 9999, "queued")


def test_resolved_at_stamped_on_terminal_entry(conn, clk, settings):
    for state in db.TERMINAL_STATES:
        item = item_in_state(conn, clk, settings, "queued")
        assert item.resolved_at is None
        clk.advance(minutes=5)
        moved = db.set_state(conn, clk, item.id, state)
        assert moved.resolved_at == clock.iso(clk.now())


def test_resolved_at_not_set_on_non_terminal_moves(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued")
    assert item.resolved_at is None
    moved = db.set_state(conn, clk, item.id, "promoted")
    assert moved.resolved_at is None


def test_promoted_at_set_on_first_promotion_only(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued")
    first = db.set_state(conn, clk, item.id, "promoted")
    assert first.promoted_at == clock.iso(clk.now())

    clk.advance(days=1)
    lapsed = db.set_state(conn, clk, item.id, "queued")
    assert lapsed.promoted_at == first.promoted_at      # never cleared on lapse

    clk.advance(days=1)
    again = db.set_state(conn, clk, item.id, "promoted")
    assert again.promoted_at == first.promoted_at       # first promotion only


def test_record_still_holds(conn, clk, settings):
    item = capture(conn, clk, settings)
    assert item.still_holds is None
    assert db.record_still_holds(conn, item.id, True).still_holds == 1
    assert db.record_still_holds(conn, item.id, False).still_holds == 0


def test_bump_cycle(conn, clk, settings):
    item = capture(conn, clk, settings)
    assert db.bump_cycle(conn, item.id).cycles_seen == 1
    assert db.bump_cycle(conn, item.id).cycles_seen == 2


# --- soft delete ------------------------------------------------------------

def test_no_delete_statements_in_db_source():
    src = (pathlib.Path(db.__file__)).read_text()
    assert not re.search(r"\bDELETE\s+FROM\b", src, re.IGNORECASE)
    assert not re.search(r"\bDROP\s+TABLE\b", src, re.IGNORECASE)


@pytest.mark.parametrize("removed_state", ["dropped", "expired"])
def test_removed_rows_are_absent_from_every_default_query(conn, clk, settings, removed_state):
    keep = item_in_state(conn, clk, settings, "queued")
    gone = item_in_state(conn, clk, settings, "queued")
    db.set_state(conn, clk, gone.id, removed_state)

    assert gone.id not in {i.id for i in db.active_items(conn)}
    assert gone.id not in {i.id for i in db.triage_items(conn, clk, settings)}
    assert gone.id not in {i.id for i in db.promoted_items(conn)}
    assert gone.id not in {i.id for i in db.all_items(conn)}
    assert gone.spotify_id not in db.polling_targets(conn)
    # and the survivor is still there
    assert keep.id in {i.id for i in db.all_items(conn)}


def test_removed_rows_are_visible_to_the_stats_path(conn, clk, settings):
    gone = item_in_state(conn, clk, settings, "queued")
    db.set_state(conn, clk, gone.id, "dropped")
    assert gone.id in {i.id for i in db.all_items(conn, include_removed=True)}
    assert conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 1  # row still there


def test_get_item_returns_removed_rows_by_id(conn, clk, settings):
    """The documented exception: callbacks must detect 'already resolved'."""
    gone = item_in_state(conn, clk, settings, "queued")
    db.set_state(conn, clk, gone.id, "dropped")
    found = db.get_item(conn, gone.id)
    assert found is not None and found.state == "dropped" and found.is_removed
    assert db.get_item(conn, 9999) is None


def test_played_is_terminal_but_not_soft_deleted(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued")
    db.set_state(conn, clk, item.id, "played")
    assert item.id in {i.id for i in db.all_items(conn)}          # visible
    assert item.id not in {i.id for i in db.active_items(conn)}   # but not active
    assert item.id not in {i.id for i in db.triage_items(conn, clk, settings)}


def test_polling_targets_covers_locked_queued_and_promoted(conn, clk, settings):
    locked = capture(conn, clk, settings)
    queued = item_in_state(conn, clk, settings, "queued")
    promoted = item_in_state(conn, clk, settings, "promoted")
    played = item_in_state(conn, clk, settings, "played")
    targets = db.polling_targets(conn)
    assert {locked.spotify_id, queued.spotify_id, promoted.spotify_id} <= set(targets)
    assert played.spotify_id not in targets


# --- weeks ------------------------------------------------------------------

def test_current_week_creates_the_row(conn, clk, settings):
    week = db.current_week(conn, clk, settings)
    assert week.week_start == WEEK_1
    assert week.allowance_min == 180
    assert week.debt_min == 0 and week.promoted_min == 0 and week.listened_min == 0
    assert db.current_week(conn, clk, settings) == week      # idempotent
    assert conn.execute("SELECT COUNT(*) FROM weeks").fetchone()[0] == 1


def test_week_properties():
    w = db.Week(WEEK_1, allowance_min=180, debt_min=60, promoted_min=40, listened_min=0)
    assert w.effective_allowance_min == 120
    assert w.remaining_min == 80
    blown = db.Week(WEEK_1, allowance_min=180, debt_min=999, promoted_min=0, listened_min=0)
    assert blown.effective_allowance_min == 0
    assert blown.remaining_min == 0


# --- the allocation gate ----------------------------------------------------

def test_promotion_cost_is_full_duration_when_unheard(conn, clk, settings):
    item = capture(conn, clk, settings, minutes=90)
    assert db.promotion_cost_min(conn, item) == 90


def test_promotion_cost_is_remaining_not_full_duration(conn, clk, settings):
    item = capture(conn, clk, settings, minutes=90)
    db.record_listening(conn, clk, item.spotify_id, position_ms=45 * MIN_MS,
                        delta_ms=45 * MIN_MS, fully_played=False)
    assert db.promotion_cost_min(conn, item) == 45


def test_promotion_cost_never_negative(conn, clk, settings):
    item = capture(conn, clk, settings, minutes=30)
    db.record_listening(conn, clk, item.spotify_id, position_ms=99 * MIN_MS,
                        delta_ms=30 * MIN_MS, fully_played=True)
    assert db.promotion_cost_min(conn, item) == 0


def test_promote_charges_the_week(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=60)
    promoted = db.promote(conn, clk, settings, item.id)
    assert promoted.state == "promoted" and promoted.promoted_at is not None
    week = db.current_week(conn, clk, settings)
    assert week.promoted_min == 60 and week.remaining_min == 120


def test_gate_allows_a_promotion_that_exactly_hits_the_limit(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=180)
    promoted = db.promote(conn, clk, settings, item.id)
    assert promoted.state == "promoted"
    week = db.current_week(conn, clk, settings)
    assert week.promoted_min == 180
    assert week.remaining_min == 0


def test_gate_allows_the_exact_last_minute(conn, clk, settings):
    first = item_in_state(conn, clk, settings, "queued", minutes=179)
    db.promote(conn, clk, settings, first.id)
    second = item_in_state(conn, clk, settings, "queued", minutes=1)
    db.promote(conn, clk, settings, second.id)
    assert db.current_week(conn, clk, settings).promoted_min == 180


def test_gate_refuses_over_budget_and_changes_nothing(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=181)
    before = db.get_item(conn, item.id)

    with pytest.raises(db.AllowanceExceeded) as exc:
        db.promote(conn, clk, settings, item.id)

    err = exc.value
    assert err.cost_min == 181
    assert err.overage_min == 1
    assert err.remaining_min == 180
    assert str(err)

    after = db.get_item(conn, item.id)
    assert after == before                       # no state, no promoted_at, nothing
    assert after.state == "queued"
    assert after.promoted_at is None
    assert db.current_week(conn, clk, settings).promoted_min == 0
    assert db.promoted_items(conn) == []


def test_gate_refuses_the_second_item_once_the_week_is_spent(conn, clk, settings):
    a = item_in_state(conn, clk, settings, "queued", minutes=120)
    db.promote(conn, clk, settings, a.id)
    b = item_in_state(conn, clk, settings, "queued", minutes=90)

    with pytest.raises(db.AllowanceExceeded) as exc:
        db.promote(conn, clk, settings, b.id)
    assert exc.value.remaining_min == 60
    assert exc.value.cost_min == 90
    assert exc.value.overage_min == 30

    assert db.get_item(conn, b.id).state == "queued"
    assert db.current_week(conn, clk, settings).promoted_min == 120


def test_gate_accounts_for_debt(conn, clk, settings):
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET debt_min = 100")
    conn.commit()
    item = item_in_state(conn, clk, settings, "queued", minutes=90)
    with pytest.raises(db.AllowanceExceeded) as exc:
        db.promote(conn, clk, settings, item.id)
    assert exc.value.remaining_min == 80        # 180 - 100 debt
    assert exc.value.overage_min == 10


def test_partly_heard_item_fits_where_the_full_one_would_not(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=240)
    db.record_listening(conn, clk, item.spotify_id, position_ms=120 * MIN_MS,
                        delta_ms=120 * MIN_MS, fully_played=False)
    db.promote(conn, clk, settings, item.id)
    assert db.current_week(conn, clk, settings).promoted_min == 120


def test_promote_on_a_terminal_item_raises_illegal_not_allowance(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=9999)
    db.set_state(conn, clk, item.id, "dropped")
    with pytest.raises(db.IllegalTransition):
        db.promote(conn, clk, settings, item.id)
    assert db.current_week(conn, clk, settings).promoted_min == 0


def test_promote_on_an_already_promoted_item_raises_illegal(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=30)
    db.promote(conn, clk, settings, item.id)
    with pytest.raises(db.IllegalTransition):
        db.promote(conn, clk, settings, item.id)
    assert db.current_week(conn, clk, settings).promoted_min == 30   # charged once


# --- listening --------------------------------------------------------------

def test_record_listening_writes_a_row_and_totals_the_week(conn, clk, settings):
    db.current_week(conn, clk, settings)
    sid = make_episode(conn, "epL", minutes=120)
    db.record_listening(conn, clk, sid, position_ms=30 * MIN_MS, delta_ms=30 * MIN_MS,
                        fully_played=False)
    clk.advance(days=1)
    db.record_listening(conn, clk, sid, position_ms=50 * MIN_MS, delta_ms=20 * MIN_MS,
                        fully_played=False)
    assert conn.execute("SELECT COUNT(*) FROM listening").fetchone()[0] == 2
    assert db.current_week(conn, clk, settings).listened_min == 50


def test_record_listening_rejects_an_unclamped_delta(conn, clk, settings):
    sid = make_episode(conn, "epL", minutes=30)
    with pytest.raises(ValueError):
        db.record_listening(conn, clk, sid, position_ms=0, delta_ms=-1, fully_played=False)
    assert conn.execute("SELECT COUNT(*) FROM listening").fetchone()[0] == 0


def test_record_listening_without_a_week_still_stores_the_row(conn, clk, settings):
    sid = make_episode(conn, "epL", minutes=30)
    db.record_listening(conn, clk, sid, position_ms=MIN_MS, delta_ms=MIN_MS, fully_played=False)
    assert conn.execute("SELECT COUNT(*) FROM listening").fetchone()[0] == 1


# --- rollover, lapse and debt ----------------------------------------------

def listen(conn, clk, sid, minutes):
    db.record_listening(conn, clk, sid, position_ms=minutes * MIN_MS,
                        delta_ms=minutes * MIN_MS, fully_played=False)


def test_roll_over_lapses_promoted_items_and_preserves_promoted_at(conn, clk, settings):
    a = item_in_state(conn, clk, settings, "queued", minutes=60)
    b = item_in_state(conn, clk, settings, "queued", minutes=60)
    db.promote(conn, clk, settings, a.id)
    db.promote(conn, clk, settings, b.id)
    db.set_state(conn, clk, b.id, "played")
    stamp = db.get_item(conn, a.id).promoted_at
    assert stamp is not None

    clk.advance(days=7)
    db.roll_over_week(conn, clk, settings)

    lapsed = db.get_item(conn, a.id)
    assert lapsed.state == "queued"
    assert lapsed.promoted_at == stamp            # evidence for funnel stage 2
    assert db.get_item(conn, b.id).state == "played"   # played is not lapsed
    assert db.promoted_items(conn) == []


def test_roll_over_opens_the_next_week_with_a_fresh_allowance(conn, clk, settings):
    db.current_week(conn, clk, settings)
    clk.advance(days=7)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.week_start == WEEK_1
    assert opened.week_start == "2026-09-06T17:00:00+00:00"
    assert opened.allowance_min == 180
    assert opened.promoted_min == 0 and opened.listened_min == 0


def test_no_debt_when_the_week_stayed_inside_the_allowance(conn, clk, settings):
    db.current_week(conn, clk, settings)
    listen(conn, clk, make_episode(conn, "epL", minutes=300), 150)
    clk.advance(days=7)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.listened_min == 150
    assert opened.debt_min == 0
    assert opened.effective_allowance_min == 180


def test_debt_carries_into_the_next_week(conn, clk, settings):
    db.current_week(conn, clk, settings)
    listen(conn, clk, make_episode(conn, "epL", minutes=300), 240)
    clk.advance(days=7)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.listened_min == 240
    assert opened.debt_min == 60                       # 240 - 180
    assert opened.effective_allowance_min == 120
    assert opened.remaining_min == 120


def test_debt_is_clamped_at_the_allowance(conn, clk, settings):
    db.current_week(conn, clk, settings)
    listen(conn, clk, make_episode(conn, "epL", minutes=2000), 1000)
    clk.advance(days=7)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.listened_min == 1000
    assert opened.debt_min == 180                      # not 820
    assert opened.effective_allowance_min == 0
    assert opened.remaining_min == 0


def test_debt_is_driven_by_listening_not_promotion(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=180)
    db.promote(conn, clk, settings, item.id)           # 180 promoted, 0 listened
    clk.advance(days=7)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.promoted_min == 180
    assert opened.debt_min == 0


def test_debt_uses_the_effective_allowance_so_it_compounds_honestly(conn, clk, settings):
    db.current_week(conn, clk, settings)
    listen(conn, clk, make_episode(conn, "epL", minutes=3000), 240)
    clk.advance(days=7)
    _, week2 = db.roll_over_week(conn, clk, settings)
    assert week2.debt_min == 60 and week2.effective_allowance_min == 120

    listen(conn, clk, make_episode(conn, "epM", minutes=3000), 150)  # 30 over 120
    clk.advance(days=7)
    closed2, week3 = db.roll_over_week(conn, clk, settings)
    assert closed2.listened_min == 150
    assert week3.debt_min == 30


def test_roll_over_from_an_empty_database(conn, clk, settings):
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert opened.week_start == WEEK_1
    assert opened.debt_min == 0
    assert closed.week_start < opened.week_start


def test_a_lapsed_item_can_be_promoted_again_next_week(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=180)
    db.promote(conn, clk, settings, item.id)
    clk.advance(days=7)
    db.roll_over_week(conn, clk, settings)
    again = db.promote(conn, clk, settings, item.id)
    assert again.state == "promoted"
    assert db.current_week(conn, clk, settings).promoted_min == 180


# --- funnel stats -----------------------------------------------------------

def test_stats_on_an_empty_database_returns_none_not_zero(conn, clk, settings):
    s = db.funnel_stats(conn, clk, settings)
    assert s.captured == 0
    assert s.survived_rate is None
    assert s.promoted_rate is None
    assert s.played_rate is None
    assert s.fully_played_rate is None
    assert s.median_days_to_drop is None
    assert s.still_holds_yes_rate is None
    assert s.still_holds_answered == 0


def test_items_inside_the_lock_are_undetermined_and_excluded(conn, clk, settings):
    capture(conn, clk, settings)
    capture(conn, clk, settings)
    assert db.funnel_stats(conn, clk, settings).captured == 0
    clk.advance(hours=49)
    assert db.funnel_stats(conn, clk, settings).captured == 2


def test_dropped_inside_the_lock_does_not_survive(conn, clk, settings):
    early = capture(conn, clk, settings)
    late = capture(conn, clk, settings)
    clk.advance(hours=10)
    db.set_state(conn, clk, early.id, "dropped")       # resolved before the lock lifted
    clk.advance(hours=40)
    db.set_state(conn, clk, late.id, "queued")
    clk.advance(hours=5)
    db.set_state(conn, clk, late.id, "dropped")        # resolved after the lock lifted

    s = db.funnel_stats(conn, clk, settings)
    assert s.captured == 2
    assert s.survived_lock == 1
    assert s.survived_rate == 0.5


def test_full_funnel(conn, clk, settings):
    survivors = [item_in_state(conn, clk, settings, "queued", minutes=30) for _ in range(4)]
    dropped_early = capture(conn, clk, settings)
    db.set_state(conn, clk, dropped_early.id, "dropped")

    db.promote(conn, clk, settings, survivors[0].id)
    db.promote(conn, clk, settings, survivors[1].id)
    listen(conn, clk, survivors[0].spotify_id, 30)
    db.set_state(conn, clk, survivors[0].id, "played")

    clk.advance(hours=49)
    s = db.funnel_stats(conn, clk, settings)
    assert s.captured == 5
    assert s.survived_lock == 4          # the early drop is out
    assert s.promoted == 2
    assert s.played == 1
    assert s.fully_played == 1
    assert s.survived_rate == 4 / 5
    assert s.promoted_rate == 2 / 4
    assert s.played_rate == 1 / 2
    assert s.fully_played_rate == 1 / 2


def test_promoted_counts_lapsed_items_via_promoted_at(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=30)
    db.promote(conn, clk, settings, item.id)
    clk.advance(days=7)
    db.roll_over_week(conn, clk, settings)
    assert db.get_item(conn, item.id).state == "queued"
    assert db.funnel_stats(conn, clk, settings).promoted == 1


def test_stats_reads_removed_rows(conn, clk, settings):
    kept = item_in_state(conn, clk, settings, "queued")
    gone = item_in_state(conn, clk, settings, "queued")
    db.set_state(conn, clk, gone.id, "expired")
    clk.advance(hours=49)
    s = db.funnel_stats(conn, clk, settings)
    assert s.captured == 2
    assert kept.id in {i.id for i in db.all_items(conn)}


def test_median_days_to_drop(conn, clk, settings):
    a = item_in_state(conn, clk, settings, "queued")
    b = item_in_state(conn, clk, settings, "queued")
    c = item_in_state(conn, clk, settings, "queued")
    clk.advance(days=2)
    db.set_state(conn, clk, a.id, "dropped")
    clk.advance(days=2)
    db.set_state(conn, clk, b.id, "dropped")       # 4 days
    clk.advance(days=6)
    db.set_state(conn, clk, c.id, "dropped")       # 10 days
    s = db.funnel_stats(conn, clk, settings)
    assert s.median_days_to_drop == 4.0


def test_median_days_to_drop_ignores_expiries(conn, clk, settings):
    a = item_in_state(conn, clk, settings, "queued")
    clk.advance(days=3)
    db.set_state(conn, clk, a.id, "expired")
    assert db.funnel_stats(conn, clk, settings).median_days_to_drop is None


def test_still_holds_yes_rate(conn, clk, settings):
    items = [item_in_state(conn, clk, settings, "queued") for _ in range(4)]
    db.record_still_holds(conn, items[0].id, True)
    db.record_still_holds(conn, items[1].id, True)
    db.record_still_holds(conn, items[2].id, False)
    clk.advance(hours=49)
    s = db.funnel_stats(conn, clk, settings)
    assert s.still_holds_answered == 3
    assert s.still_holds_yes_rate == 2 / 3


def test_still_holds_excludes_locked_items(conn, clk, settings):
    locked = capture(conn, clk, settings)
    db.record_still_holds(conn, locked.id, True)
    s = db.funnel_stats(conn, clk, settings)
    assert s.still_holds_answered == 0
    assert s.still_holds_yes_rate is None


def test_stats_since_filter(conn, clk, settings):
    old = capture(conn, clk, settings)
    clk.advance(days=30)
    cutoff = clock.iso(clk.now())
    new = capture(conn, clk, settings)
    clk.advance(hours=49)

    assert db.funnel_stats(conn, clk, settings).captured == 2
    scoped = db.funnel_stats(conn, clk, settings, since=cutoff)
    assert scoped.captured == 1


def test_played_needs_a_positive_delta(conn, clk, settings):
    item = item_in_state(conn, clk, settings, "queued", minutes=30)
    db.record_listening(conn, clk, item.spotify_id, position_ms=0, delta_ms=0,
                        fully_played=False)
    db.promote(conn, clk, settings, item.id)
    clk.advance(hours=49)
    assert db.funnel_stats(conn, clk, settings).played == 0
    db.record_listening(conn, clk, item.spotify_id, position_ms=MIN_MS, delta_ms=MIN_MS,
                        fully_played=False)
    assert db.funnel_stats(conn, clk, settings).played == 1
