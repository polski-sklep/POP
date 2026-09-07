# Build brief: POP

A deliberate-consumption queue for podcasts. Capture an episode instead of
playing it, wait out a lock, triage weekly against a time allowance, and
measure what actually gets listened to.

Personal tool. Single user. Runs on the Hetzner VPS.

---

## What this is for

The target behaviour is the reflex: see an interesting episode, click, listen,
distract. POP inserts a required conscious act into that arc at the moment the
impulse fires. The intended replacement for listening time is nothing —
undirected thinking while cycling, walking, or working through low-attention
tasks.

**The mechanism is interruption and allocation, not enforcement.** The app
cannot stop anyone pressing play in Spotify and must not pretend to. It gates
what enters the playable slate and it measures what happened. That is all.

**The real output is the decay curve.** Three funnel stages, each measuring a
different thing:

1. captured → survived the 48h lock (how much of the impulse was momentary)
2. survived → promoted at triage (how much survives a calm reading)
3. promoted → actually played (how much was wanted versus imagined)

Every design decision below serves those three numbers. When in doubt about a
feature, ask whether it improves or corrupts them.

---

## Explicitly not in scope

- No blocking, no Screen Time integration, no attempt to prevent playback
- No categories, tags, ratings, priority scores, or "useful vs waste"
  classification. Any exemption category becomes the loophole, adjudicated at
  the moment of maximum motivated reasoning. The queue already handles
  genuinely useful items: a strong reason survives triage on its merits
- No mobile app, no web dashboard, no UI beyond the bot
- Music is out of scope and requires no filtering. `resume_point` exists only
  on episode objects, so music tracks are structurally invisible to this design

---

## Architecture

```
Spotify (phone) ──share──► Telegram bot ──► SQLite
                                              │
                    daily cron ──► poll.py ───┤   resume_point deltas
                    Sun 18:00  ──► triage.py ─┘   inline-button triage
```

Telegram is the entire interface. Capture, triage, and alerts all happen in one
chat the user is already in constantly. This is why a bot beats an iOS
Shortcut: a Shortcut can only capture, it cannot report back.

**Coexistence with Pulse.** The VPS also runs Pulse, which uses Telethon with a
*user* account (bots cannot read arbitrary group history). POP uses the *Bot
API* via python-telegram-bot or aiogram. Different auth models, different
libraries. They coexist fine but must not share client code.

---

## Capture

User shares a Spotify episode into the bot chat. Bot replies asking why. The
item is **not stored until the reply arrives** — this is what makes the note
mandatory rather than optional cleanup after the fact.

Then bot confirms with title and duration, so the time cost is visible at the
moment of capture.

Optional deadline: user can reply with a date. Handles event-tied episodes (a
founder interview before an actual interview) without creating a privileged
content category. An item with a deadline surfaces before it expires
regardless of where the weekly cycle sits. It still requires a note and still
counts against the allowance.

### URL parsing

Accept both forms:
- `https://open.spotify.com/episode/{id}?si=...` — strip query params
- `spotify:episode:{id}` — desktop copy sometimes yields this

Episode IDs are 22-character base62. Reject anything else with a clear message.
Do not silently ignore malformed input.

### The why-note

The note is **not a barrier**. A justification written at the moment of impulse
is cheap to produce and the friction wears smooth within a week. Its value is
as *evidence at trial*: the user's own words read back 48 hours later, when the
impulse has passed, displayed next to the runtime. "Looked good" next to a
two-hour duration is what kills the item.

So: mandatory, one line, and **always displayed at triage alongside duration**.

---

## Lock

48 hours from capture. Locked items do not appear in triage and cannot be
promoted. Deadline items are exempt if the deadline falls inside the lock.

---

## Triage

**Sunday 18:00 Europe/Lisbon.** Not mornings — those run slow and are already
spoken for.

Bot posts the unlocked queue. Each item shows title, show, duration, the
original why-note, and its age in cycles. Inline buttons per item:

- **Promote** — moves into the playable slate, costs its duration against the
  remaining allowance
- **Drop** — soft delete (see below)
- **Still holds?** — the yes/no on whether the original reason survives. This
  single question generates the cleanest data in the system. Log the answer.

### The allocation gate

**This is where the constraint actually bites.** Total duration of promoted
items cannot exceed the remaining weekly allowance. The bot refuses a promotion
that would breach it and says by how much.

This forces the tradeoff explicitly while the user is calm, rather than while
reaching for something on a bike. A two-hour episode costs two-thirds of the
week in a single decision, and seeing that is the point.

---

## Allowance, debt, expiry

- **Weekly allowance: 180 minutes.** Config value, not a constant. Expect it to
  be tuned once real data exists.
- **Week resets Sunday 18:00** at triage.
- **Debt, not reset.** Minutes over the allowance are deducted from next week's
  allowance. Self-correcting, mildly costly, and it removes the "week's already
  blown, may as well" collapse.
- **Promotion lapses weekly.** Unlistened promoted items return to the queue at
  reset rather than accumulating an ever-growing playable backlog that defeats
  the allocation gate. They can be promoted again if they still earn it.
- **Lifespan: two triage cycles.** Warning shown on the second cycle. Removed
  at the third. Removal is a soft delete.

### Soft delete, always

Dropped and expired items are removed from **every view**, instantly and
permanently from the user's perspective. The row stays in the database.

Hard-deleting rejections destroys the numerator of funnel stages 1 and 2 and it
cannot be reconstructed. The decay curve is the entire point of the system.
Never expose these rows except in explicit stats output.

---

## Measurement

### The mechanism

Spotify episode objects carry `resume_point` — the user's most recent position
in that episode — when the token holds the `user-read-playback-position` scope.
Plus `fully_played`, a boolean.

This is a **position, not a total**, so listening time is a delta between
polls. Store last known position per episode:

```
minutes_this_poll = max(0, current_position - previous_position)
```

**The clamp is not optional.** Relistening or scrubbing backward drops the
position, and an unclamped delta goes negative, silently crediting the user
time they did not earn back.

### Poll daily, not weekly

A weekly poll reports on Sunday that the budget was blown on Tuesday. That is a
postmortem, not a control. Daily cron gives a running figure and a remaining
allowance visible mid-week. Cost is one API call per queued episode.

### Alerts

Bot messages the user when the allowance is approached and when it is crossed.
This is the control loop and it is the main reason the interface is a bot.

### Known distortions — accept, do not solve

- Scrubbing forward reads as listening, since position moved
- Playback above 1x undercounts real minutes against elapsed time
- Use `fully_played` for completion, not `position >= duration`. Trailing
  credits mean episodes rarely reach 100%

### What is invisible

Anything played spontaneously outside the queue. This is precisely the
behaviour POP exists to catch, so it matters, but the fix is deferred (see v2).
If v1 numbers look implausibly low, that is the signal the system is being
bypassed.

---

## Spotify API notes

Verified, and two of these will waste a day if rediscovered the hard way:

- `GET /me/player/recently-played` **does not support podcast episodes.**
  Official documentation states this plainly. Dead end, do not build on it.
- `GET /me/player/currently-playing` works for episodes **only** with
  `additional_types=episode`. Without it, `item` returns null while
  `currently_playing_type` reads `"episode"`. Not used in this design — it
  would require continuous polling and would miss gaps between polls.
- `resume_point` and `fully_played` require scope
  `user-read-playback-position`. This is the whole measurement strategy.
- Auth: Authorization Code flow with a stored refresh token. One-time browser
  consent, then headless forever.

Episode metadata available and worth storing at capture: name, show, duration,
description (HTML stripped), release date. **Duration is the field that earns
its place at triage** — a three-hour episode and a forty-minute one are very
different bets against 180 minutes.

---

## Data model

```sql
episodes (
  spotify_id      TEXT PRIMARY KEY,
  title, show, description, release_date,
  duration_ms     INTEGER
)

queue (
  id              INTEGER PRIMARY KEY,
  spotify_id      TEXT NOT NULL,
  why_note        TEXT NOT NULL,
  captured_at     TEXT NOT NULL,
  deadline        TEXT,
  state           TEXT NOT NULL,  -- locked|queued|promoted|dropped|expired|played
  cycles_seen     INTEGER DEFAULT 0,
  still_holds     INTEGER,        -- null until asked
  promoted_at     TEXT,
  resolved_at     TEXT
)

listening (
  spotify_id      TEXT NOT NULL,
  polled_at       TEXT NOT NULL,
  position_ms     INTEGER NOT NULL,
  delta_ms        INTEGER NOT NULL,
  fully_played    INTEGER
)

weeks (
  week_start      TEXT PRIMARY KEY,
  allowance_min   INTEGER NOT NULL,
  debt_min        INTEGER DEFAULT 0,
  promoted_min    INTEGER DEFAULT 0,
  listened_min    INTEGER DEFAULT 0
)
```

State transitions are the funnel. Never overwrite a terminal state.

---

## Stats

One bot command, `/stats`. Reports the three funnel rates over all time and
over the last 8 weeks:

- capture → survived lock
- survived → promoted
- promoted → played (and → fully played)

Plus median time-to-drop, and the still-holds yes rate. Nothing else. This is
the output of the system and it should fit in one message.

---

## v2, deferred

**Unqueued-listening detection.** `GET /me/shows` lists followed shows,
`GET /shows/{id}/episodes` lists their episodes, each carrying `resume_point`
under the same scope. A daily sweep across followed shows catches position
changes on episodes that were never queued. That number is the honest measure
of whether the system is working and it should trend toward zero.

Deferred because it is materially heavier than queued-episode polling. Build it
only if v1 numbers look too low to be true.

**Screenshot capture fallback.** For episodes encountered on X or YouTube
before existing in Spotify context: vision extraction of show and episode
title, then Spotify search to resolve an ID. Fragile, and unnecessary given
Spotify-only listening. Do not build in v1.

---

## Constraints

- Bind the bot to a single authorised Telegram user ID. Reject everything else
  silently.
- Secrets in `.env`, gitignored: Spotify client ID/secret/refresh token,
  Telegram bot token, authorised user ID.
- Tailscale-only network exposure where anything listens. The bot itself uses
  long polling, so no inbound port is required. Prefer that over webhooks.
- **Every field added is friction the user must survive weekly.** Note,
  duration, deadline and still-holds are enough. The tool dies from admin
  burden long before it dies from bad design. Refuse feature requests that add
  per-item input.

---

## Stack

Python 3.12, python-telegram-bot (or aiogram), SQLite, `requests` or `httpx`
for Spotify, cron for the daily poll and Sunday triage. No web framework, no
frontend, no build step.

```
pop/
├── bot.py           capture, triage, alerts, /stats
├── spotify.py       auth, episode fetch, resume_point polling
├── poll.py          daily cron entry
├── triage.py        Sunday 18:00 cron entry
├── schema.sql
└── data/            gitignored
```
