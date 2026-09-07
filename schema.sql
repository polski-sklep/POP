-- POP schema. Applied by db.init_db(); safe to re-run.
--
-- Timestamps are ISO-8601 UTC strings produced by clock.iso() —
-- e.g. "2026-08-30T08:27:00+00:00". Fixed width, so lexicographic
-- comparison is chronological comparison. Never store naive local time.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Episode metadata, fetched once at capture. One row per Spotify episode,
-- shared by every queue row that ever referenced it.
CREATE TABLE IF NOT EXISTS episodes (
    spotify_id    TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    show          TEXT NOT NULL,
    description   TEXT,              -- HTML stripped at fetch time
    release_date  TEXT,              -- Spotify's own string; may be YYYY or YYYY-MM or YYYY-MM-DD
    duration_ms   INTEGER NOT NULL
);

-- The funnel. One row per capture event.
--
-- state is the funnel. Legal transitions are enforced in db.set_state();
-- 'dropped', 'expired' and 'played' are terminal and are never overwritten.
--
-- 'dropped' and 'expired' are SOFT DELETES: excluded from every default
-- query, reachable only via the explicit stats path. Hard-deleting them
-- would destroy the numerator of funnel stages 1 and 2, which is the entire
-- point of the system.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY,
    spotify_id    TEXT NOT NULL REFERENCES episodes(spotify_id),
    -- Mandatory: the row does not exist without one. db.capture() is the only
    -- INSERT into this table and rejects an empty or whitespace-only note
    -- before it gets here; this is the same guarantee at the storage layer,
    -- so the invariant does not depend on that staying true. SQLite's
    -- one-argument TRIM strips spaces only, hence the explicit character set.
    why_note      TEXT NOT NULL
                  CHECK (TRIM(why_note, ' ' || char(9) || char(10) || char(13)) <> ''),
    captured_at   TEXT NOT NULL,
    deadline      TEXT,              -- optional ISO-8601 UTC
    state         TEXT NOT NULL CHECK (state IN
                    ('locked','queued','promoted','dropped','expired','played')),
    cycles_seen   INTEGER NOT NULL DEFAULT 0,
    still_holds   INTEGER,           -- NULL until asked; 1 = yes, 0 = no
    promoted_at   TEXT,              -- set on FIRST promotion, never cleared (funnel stage 2 evidence)
    resolved_at   TEXT               -- set when a terminal state is entered
);

CREATE INDEX IF NOT EXISTS idx_queue_state      ON queue(state);
CREATE INDEX IF NOT EXISTS idx_queue_spotify_id ON queue(spotify_id);
CREATE INDEX IF NOT EXISTS idx_queue_captured   ON queue(captured_at);

-- Poll history. One row per (episode, poll) where a position was observed.
--
-- position_ms is Spotify's resume_point: a POSITION, not a total. delta_ms is
-- the clamped difference from the previous observation. The clamp is not
-- optional — see spotify.clamped_delta().
CREATE TABLE IF NOT EXISTS listening (
    id            INTEGER PRIMARY KEY,
    spotify_id    TEXT NOT NULL REFERENCES episodes(spotify_id),
    polled_at     TEXT NOT NULL,
    position_ms   INTEGER NOT NULL,
    delta_ms      INTEGER NOT NULL CHECK (delta_ms >= 0),   -- clamp enforced at the storage layer too
    fully_played  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_listening_episode ON listening(spotify_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_listening_polled  ON listening(polled_at);

-- Weekly accounting. One row per week, keyed by the ISO-8601 UTC instant of
-- the Sunday 18:00 Europe/Lisbon that opens it.
--
--   effective_allowance = allowance_min - debt_min
--   remaining_for_promotion = effective_allowance - promoted_min
--   debt carried to next week = clamp(listened_min - effective_allowance, 0, allowance_min)
CREATE TABLE IF NOT EXISTS weeks (
    week_start    TEXT PRIMARY KEY,
    allowance_min INTEGER NOT NULL,          -- snapshotted from config at week creation
    debt_min      INTEGER NOT NULL DEFAULT 0,
    promoted_min  INTEGER NOT NULL DEFAULT 0,
    listened_min  INTEGER NOT NULL DEFAULT 0
);

-- Alert bookkeeping, so poll.py does not re-send the same alert every day.
-- Not user-facing: adds no per-item friction.
CREATE TABLE IF NOT EXISTS alerts_sent (
    week_start    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('approaching','exceeded')),
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (week_start, kind)
);

-- Deadline-surfacing bookkeeping, so poll.py posts an imminent-deadline item
-- once rather than every morning until the deadline passes.
--
-- Same shape and same justification as alerts_sent: this is dedup state, not
-- user input. It adds no per-item field the user must fill in weekly, so it
-- does not breach the brief's friction constraint, and it creates no
-- privileged content category — an item surfaced here is promoted through
-- db.promote() and charged against the allowance like anything else.
CREATE TABLE IF NOT EXISTS deadline_notices (
    queue_id      INTEGER PRIMARY KEY REFERENCES queue(id),
    sent_at       TEXT NOT NULL
);

-- Schema version, for migrations.
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
