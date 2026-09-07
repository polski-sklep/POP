# POP — unattended build instructions

This file is the build prompt, persisted so a scheduled run (which starts with
no memory of the originating conversation) can re-read it at run time.

**Read `pop-build-brief.md` in full before doing anything else. It is the
complete specification for this build.**

## Status

- **Phase 0 — COMPLETE (2026-08-26).** `.env` exists, is `chmod 600`, contains
  all Spotify + Telegram credentials and config, and `check_env.py` exits 0
  with both credentials authenticating live. Do **not** redo Phase 0. Do **not**
  re-run `auth_spotify.py`. Re-running `check_env.py` once at the start as a
  precondition check is fine and encouraged; if it fails, stop and report
  rather than attempting to re-obtain credentials.
- **Phases 1–5 — TO DO.** This is the work of the scheduled run.

Existing files: `.gitignore`, `.env.example`, `.env`, `auth_spotify.py`,
`check_env.py`, `pop-build-brief.md`, `data/`.

## Operating rules

- Do not ask questions during the build. Every decision is either in the brief
  or is yours to make. Where the brief is silent, choose the option most
  consistent with its stated principles, implement it, and record the choice
  and the reasoning in `DECISIONS.md`. The user reviews that file at the end.
- Do not deviate from the brief's explicit constraints. Several look like minor
  details and are not. In particular: the negative-delta clamp on
  `resume_point`, soft delete on every rejection path, the why-note being
  required *before* an item is stored rather than after, and the allocation
  gate refusing promotions that breach the remaining allowance. If any of these
  seems wrong, implement it as specified and argue the case in `DECISIONS.md`.
- **Never call the live Spotify or Telegram APIs during development or
  testing.** Use fixtures and mocks. The only live call in the entire build was
  the one-time Spotify OAuth consent in Phase 0, which is already done. An
  agent that gets stuck waiting on a real API has failed.
- Report progress to stdout as you go. The user wants to read what happened
  without asking.

## Phase 1 — Contracts. Single agent, no parallelism.

Before spawning anything in parallel, define the interfaces every module will
share: the SQLite schema from the brief, the state machine for `queue.state`
including which transitions are legal and which states are terminal, and the
function signatures each module exposes to the others. Write these to
`CONTRACTS.md` and `schema.sql`.

Parallel agents that invent their own interfaces produce work that does not
compose. This phase exists to prevent that.

## Phase 2 — Parallel implementation. Three agents.

Launch concurrently. Each owns its files exclusively and imports others only
through the Phase 1 contracts.

- **Agent A — data layer.** `db.py` and `schema.sql`. Connection handling,
  migrations, the state machine with illegal transitions raising rather than
  silently passing, soft-delete semantics such that dropped and expired rows are
  excluded from every default query and reachable only through an explicit
  stats path, and the week/debt accounting from the brief.
- **Agent B — Spotify integration.** `spotify.py`. Refresh-token auth with
  transparent renewal, episode metadata fetch, URL and URI parsing for both
  accepted forms with clear rejection of anything else, and `resume_point`
  polling with the clamped delta. This agent owns the measurement logic, which
  is the part most likely to be silently wrong, so it writes its own unit tests
  for the clamp as it goes: forward listening, backward scrub, relisten from
  zero, and no change.
- **Agent C — Telegram bot.** `bot.py`. Long polling, single-authorised-user
  gate rejecting all other IDs silently, the capture conversation in which the
  item is not persisted until the why-note reply arrives, optional deadline
  parsing, and confirmation replying with title and duration. Inline-button
  handlers for promote, drop and still-holds. Mock the Spotify layer against
  the Phase 1 contract.

## Phase 3 — Orchestration. Single agent.

Depends on all three above being complete.

`triage.py` for the Sunday 18:00 Europe/Lisbon cycle: post the unlocked queue
with title, show, duration, why-note and cycle age; enforce the allocation gate
against remaining allowance including carried debt; lapse unlistened promotions
back to the queue; apply the two-cycle lifespan with a warning on the second
and soft removal at the third. `poll.py` for the daily cron and the
approaching/exceeded alerts. `/stats` implementing the three funnel rates over
all time and the last eight weeks, plus median time-to-drop and the still-holds
yes rate, in a single message.

Write the crontab entries to `README.md` rather than installing them.

## Phase 4 — Verification. Separate agent.

A different agent from the ones that wrote the code. Its job is to find what
they got wrong.

Write an integration test that drives a full simulated lifecycle end to end
against fixtures with a frozen clock: capture, lock expiry, triage promotion,
partial listening, week rollover with debt, promotion lapse, second-cycle
warning, third-cycle expiry. Assert the funnel counts are correct at each step.

Then audit the implementation against the brief specifically for: the clamp
present and correct, no hard deletes anywhere, the why-note genuinely
non-optional, the allocation gate actually refusing over-budget promotions,
terminal states never overwritten, and no live API calls in any test path.
Report findings in `REVIEW.md` and fix what you find.

## Phase 5 — Deliverables

`README.md` with setup, the crontab lines, and how to run each entry point.
`DECISIONS.md` with every choice made where the brief was silent. `REVIEW.md`
from Phase 4. Confirm the test suite passes and state the single command to
start the bot.

Then stop. Do not add features.
