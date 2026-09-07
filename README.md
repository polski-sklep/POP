# POP

A deliberate-consumption queue for podcasts. Capture an episode instead of
playing it, wait out a 48-hour lock, triage weekly against a time allowance,
and measure what actually gets listened to.

Personal tool. Single user. Telegram is the entire interface.

The output is the decay curve — three funnel rates:

1. captured → survived the lock (how much of the impulse was momentary)
2. survived → promoted at triage (how much survives a calm reading)
3. promoted → actually played (how much was wanted versus imagined)

---

## Setup

### 1. Dependencies

Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install "python-telegram-bot>=21,<23" httpx pytest pytest-asyncio
```

### 2. Credentials

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored and should be
`chmod 600`.

**Spotify** — create an app at https://developer.spotify.com/dashboard.
Register the redirect URI exactly as `http://127.0.0.1:8888/callback`; it must
be the loopback IP literal, not `localhost`, which Spotify rejects on new apps.
Copy the client ID and secret into `.env`.

**Telegram** — `/newbot` to @BotFather, copy the token into `.env`. Put your own
numeric user ID in `TELEGRAM_USER_ID`; every other sender is ignored silently.
Press **Start** in the bot chat once: a Telegram bot cannot open a conversation
on its own, so without this it can never send the Sunday triage post or an
allowance alert.

### 3. One-time Spotify consent

Run this on a machine with a browser, then copy `.env` to the server. The
refresh token is portable.

```bash
.venv/bin/python auth_spotify.py
```

Grant **"View your listening position in podcasts"** — that is the
`user-read-playback-position` scope, and it is the entire measurement strategy.
Without it every `resume_point` reads null and the system silently reports that
you listen to nothing. The script refuses to store a token that lacks it.

### 4. Verify

```bash
.venv/bin/python check_env.py
```

Checks every variable is present and correctly shaped, that `.env` is `0600`,
that `.gitignore` covers the secrets, and that both credentials authenticate
live. Exit 0 means good. `--ping` additionally sends a test message to your
Telegram user, which also confirms you pressed Start.

---

## Running it

Start the bot (long polling — no inbound port, no webhook, nothing to expose):

```bash
.venv/bin/python bot.py
```

Entry points:

| command | when | what it does |
|---|---|---|
| `.venv/bin/python bot.py` | always on | capture, triage buttons, alerts, `/stats` |
| `.venv/bin/python poll.py` | daily cron | reads `resume_point`, writes clamped deltas, lifts expired locks, posts items whose deadline falls before the next triage, fires allowance alerts |
| `.venv/bin/python triage.py` | Sunday 18:00 cron | week rollover, lock lifts, expiry, posts the queue |

---

## Scheduling

Two entry points need scheduling: `poll.py` daily and `triage.py` on Sunday.

**Timezone, and it matters.** Both cron and launchd fire in *system local
time*, which is not necessarily `POP_TZ`. POP computes its week boundary in
`POP_TZ`. If the two diverge, the schedule must be offset to match, or
`roll_over_week()` closes the wrong week every Sunday — and firing *early* is
the dangerous direction. Here they agree: system timezone and `POP_TZ` are both
`Europe/Warsaw`, so triage is scheduled at a plain `18:00`.

### On macOS — launchd (used here)

**Do not use cron on a laptop.** macOS cron does not catch up on missed runs:
if the Mac is asleep on Sunday evening, triage never happens that week, the
week never rolls over, and the cycle stalls silently. launchd runs a missed
`StartCalendarInterval` job at the next wake.

Three agents live in `launchd/`:

| job | schedule | what |
|---|---|---|
| `com.jacob.pop.bot` | always on, `KeepAlive` | the bot; restarts on crash and reboot |
| `com.jacob.pop.poll` | 09:17 daily | daily poll |
| `com.jacob.pop.triage` | Sun 18:00 | weekly triage, on the week boundary |

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
for j in bot poll triage; do
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jacob.pop.$j.plist
done
launchctl list | grep jacob.pop
```

Run one immediately, without waiting for its hour:

```bash
launchctl kickstart -p gui/$(id -u)/com.jacob.pop.poll
```

Stop one: `launchctl bootout gui/$(id -u)/com.jacob.pop.triage`.

Stop any manually started bot first — Telegram permits exactly one
`getUpdates` poller per token, and two instances make *both* fail.

### On the VPS — cron

Cron is correct on an always-on host. `crontab -e`:

```cron
CRON_TZ=Europe/Warsaw   # must match POP_TZ in .env

# Daily poll: resume_point deltas, deadline surfacing, allowance alerts
17 8 * * * cd /opt/pop && .venv/bin/python poll.py >> logs/poll.log 2>&1

# Weekly triage: Sunday 18:00
0 18 * * 0 cd /opt/pop && .venv/bin/python triage.py >> logs/triage.log 2>&1
```

Set `CRON_TZ` explicitly rather than trusting the daemon to inherit your login
shell's timezone — it does not. Note `CRON_TZ` is a Linux cron extension and is
**not** reliably honoured by macOS cron, which is another reason launchd is
used above.

`CRON_TZ` and `POP_TZ` are different settings. `CRON_TZ` decides when the
daemon fires; `POP_TZ` is what POP believes its own timezone to be. POP
deliberately does not read the POSIX `TZ` variable, so a unit exporting
`TZ=UTC` cannot move the triage boundary.

---

## Tests

```bash
.venv/bin/pytest -q
```

No test touches the network. Spotify and Telegram are both faked; the only live
calls this project ever makes are `auth_spotify.py` and `check_env.py`, and
neither runs in the suite.

---

## Files

```
bot.py           capture, triage buttons, /stats
spotify.py       auth, episode fetch, resume_point polling, the clamp
poll.py          daily cron entry — deltas, deadline surfacing, alerts
triage.py        Sunday 18:00 cron entry — rollover, expiry, the queue post
db.py            state machine, soft delete, week/debt accounting, funnel stats
config.py        .env loading
clock.py         UTC storage, week boundaries, DST-correct
schema.sql
auth_spotify.py  one-time OAuth consent (run once, then never again)
check_env.py     credential validator
conftest.py      blocks network and real-.env reads across the whole suite
tests/           472 tests
data/            gitignored
```

`CONTRACTS.md` is the interface contract between these modules.
`DECISIONS.md` records every choice made where the brief was silent.
`REVIEW.md` is the independent audit of the implementation against the brief.

---

## Known distortions

Accepted deliberately, not bugs:

- Scrubbing forward reads as listening, since the position moved.
- Playback above 1x undercounts real minutes against elapsed time.
- Completion uses Spotify's `fully_played` flag, not `position >= duration` —
  trailing credits mean episodes rarely reach 100%.
- Anything played spontaneously outside the queue is invisible. That is the
  behaviour POP exists to catch, so it matters. If the numbers look
  implausibly low, that is the signal the system is being bypassed, and the
  deferred v2 sweep over followed shows is the fix.
