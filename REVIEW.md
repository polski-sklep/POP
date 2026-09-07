# REVIEW — Phase 4

Verification pass over `db.py`, `spotify.py`, `bot.py`, `triage.py`, `poll.py`,
`clock.py`, `config.py` and `schema.sql` against `pop-build-brief.md` (the
authority), `CONTRACTS.md` and `DECISIONS.md`.

**Result: 6 defects found, 6 fixed. 436 tests → 445 (9 added, 0 removed, 0
weakened).** Two further problems are reported and deliberately *not* fixed;
both are argued below.

Everything asserted here was either executed against the code or is cited to a
line. Nothing is inferred from a docstring.

---

## Verdicts, a–k

### a. The clamp — **CORRECT**, but the baseline it clamps against was wrong

`spotify.clamped_delta` is `max(0, current - previous)` (`spotify.py:266`) and
there is exactly one call site: `poll._record` via `poll._clamped_delta`
(`poll.py:72-80`, `poll.py:135`). No module recomputes a delta by hand — a grep
for subtraction against a position turns up only `db.promotion_cost_min`
(`db.py:523`, `duration - position`, itself clamped) and
`Week.effective_allowance_min` / `remaining_min` (`db.py:141,146`).

The clamp is backed twice more at the storage layer:
`db.record_listening` raises on a negative delta (`db.py:658-659`) and
`schema.sql:64` carries `CHECK (delta_ms >= 0)`. I confirmed the CHECK actually
fires: a raw `INSERT` with `delta_ms = -1` raises
`IntegrityError: CHECK constraint failed: delta_ms >= 0`.

I could not construct a case that credits **negative** time. I did construct
one that credits **phantom** time — see **Defect 1**. That was a defect in the
*baseline*, not in the clamp: with no previous observation, `poll._record` used
`previous_ms = 0` (`poll.py:127`), so the first poll of an episode that was
already part heard credited every pre-capture minute.

### b. No hard deletes — **CORRECT**

Zero `DELETE`, `DROP TABLE`, `TRUNCATE`, `REPLACE INTO` or `INSERT OR REPLACE`
statements anywhere in the codebase (or the test suite). The only writes that
remove information are `UPDATE`s to `state`. `db.py` has one `INSERT INTO queue`
(`db.py:320`) and nothing that removes a row.

Every default reader excludes `REMOVED_STATES`: `active_items` (`db.py:345`),
`triage_items` (`db.py:354`, `state = 'queued'`), `promoted_items`
(`db.py:373`), `polling_targets` (`db.py:380`), `all_items` (`db.py:393`).
`get_item` (`db.py:332`) *does* return removed rows by id, which is the one
documented exception and is load-bearing: `bot.on_callback` (`bot.py:389`)
needs to distinguish "already resolved" from "no such item". A lookup by id is
not a listing, and nothing there can leak a dropped row into a view.

`funnel_stats` is the only reader of removed rows in aggregate
(`db.py:716`, `all_items(include_removed=True)`). Verified end to end: at the
close of the lifecycle test three of four items are terminal, two of them
soft-removed, `db.all_items()` returns one row, and
`SELECT COUNT(*) FROM queue` still returns 4
(`tests/test_integration.py::test_full_lifecycle`, final block).

One stale comment at `bot.py:386` (before the fix) claimed `get_item` hides soft-deleted rows,
the opposite of what `db.get_item` documents and does. Behaviour was correct;
the comment was corrected.

### c. The why-note — **CORRECT in code, now backed by the schema**

`db.capture` is the only function that inserts into `queue`, and it strips and
rejects before touching the database (`db.py:302-304`). I tried `""`, `"   "`,
`"\t\n "` and `None`; all four raise `ValueError`. `bot._handle_note` refuses a
note that parses down to only a date (`bot.py:322-324`, D2.7) and
`bot._looks_like_spotify_ref` (`bot.py:93`) stops a rejected link from being
swallowed as the note (D2.6). Nothing is written to *either* table before the
reply arrives (D2.5) — confirmed by the existing
`test_nothing_is_written_before_the_note_arrives`.

The gap was at the storage layer: `why_note TEXT NOT NULL` accepted a
whitespace-only string by raw SQL, while the sibling invariant (the clamp) *is*
backed by a CHECK. See **Defect 5**.

### d. The allocation gate — **CORRECT**

`db.promote` (`db.py:526`) refuses when `promoted_min + cost > effective_allowance_min`
and raises before any write; legality is checked first (D2.9) so neither path
writes. Exercised at the exact boundary:

| promoted so far | cost | effective allowance | outcome |
|---|---|---|---|
| 120 | 60 | 180 | **succeeds**, `remaining_min` → 0 |
| 120 | 61 | 180 | refused, `overage_min == 1`, state unchanged, `promoted_min` still 120 |

Pinned by `test_the_gate_accepts_the_exact_remainder_and_refuses_one_minute_more`.

Debt genuinely shrinks the gate, not just the report: with `debt_min = 80`,
`effective_allowance_min` is 100 and a 120-minute promotion is refused with
`overage_min == 20`
(`test_debt_shrinks_the_gate_not_just_the_report`). The exception carries
`overage_min`, `remaining_min` and `cost_min` (`db.py:555-562`) and
`bot.on_callback` renders them verbatim (`bot.py:412-416`).

### e. Terminal states — **CORRECT**

`LEGAL_TRANSITIONS` maps each terminal state to an empty frozenset
(`db.py:66-68`) and `_check_transition` (`db.py:404`) consults it before
anything else. I attempted all 18 transitions out of `dropped`, `expired` and
`played`; every one raised `IllegalTransition`, and the row was unchanged after
each attempt. The higher-level paths are safe too:

- `db.promote` — `_check_transition` first, so a dropped item gets
  `IllegalTransition`, never a budget message (`db.py:535`).
- `poll._mark_played` iterates `db.active_items()` (`poll.py:152`), which never
  returns a terminal row, so the `fully_played` flip cannot re-resolve a
  dropped item. Polling an already-played item twice raises nothing.
- `triage.run_triage` step 3 expires only from `db.triage_items()`, which is
  `state = 'queued'` (`triage.py:274`).
- `db.roll_over_week` lapses only `promoted_items()` (`db.py:598`).
- `bot.on_callback` returns "already resolved" before any write when the item
  is terminal (`bot.py:389-391`).

The one hole I did find was in the *other* direction — into `promoted` from
`locked` — see **Defect 3**.

### f. No live API calls — **DEFECT, fixed**

The suite is hermetic, but nothing was enforcing it outside one module. See
**Defect 6**. After the fix, all 445 tests pass with DNS, socket connects, both
httpx transports and any read of the real `.env` raising at the point of
attempt. `poll.main()`, `triage.main()` and `bot.main()` are the only places
real credentials are touched and are all `# pragma: no cover`; no test imports
or calls them.

### g. `run_poll` keys by `spotify_id` — **CORRECT**

`poll.py:106` builds `by_id = {ep.spotify_id: ep for ep in episodes}` and
`poll.py:112-116` iterates `targets`, looking each one up; unmatched ids are
logged and skipped (`poll.py:108-110`). No `zip` anywhere in the file.

Verified by construction, not by reading: three queue rows (SHORT, MEDIUM,
LONG) against a `FakeSpotify` holding only two of them, returned in a different
order from the request. LONG's 30 minutes landed on LONG, MEDIUM got its own
zero-delta row, and SHORT got no row at all. An index zip would have credited
LONG's 30 minutes to SHORT.

### h. The lifespan — **CORRECT in both directions**

With `lifespan_cycles = 2`, driving four consecutive triages against a real
database:

| triage | `cycles_seen` after | shown | warned | state |
|---|---|---|---|---|
| 1 | 1 | yes | no | queued |
| 2 | 2 | yes | **yes** | queued |
| 3 | 2 | no | — | **expired** |
| 4 | 2 | no | — | expired |

Exactly "shown cycle 1, warned cycle 2, soft-removed at the third triage".
Neither off-by-one is present: expiry (`triage.py:216`, `cycles_seen >=
lifespan_cycles`) is evaluated *before* the bump (`triage.py:283`), so an item
at its lifespan is removed rather than shown a third time; and the warning is
evaluated *after* the bump, so it describes the cycle the user is looking at.
`test_lifespan_respects_a_configured_value` covers `lifespan_cycles = 1`.

The warning flag itself was wrong for deadline items — **Defect 4**.

### i. Deadline items — **partly correct; one real gap, reported not fixed**

- **Lock exemption when the deadline falls inside the lock: correct.**
  `db._deadline_exempt` (`db.py:281`) compares the deadline against
  `lock_expires_at`, and `capture` creates the item `queued` rather than
  `locked` (`db.py:316`). `triage_items` waives the lock check for it
  (`db.py:369`).
- **Still requires a note: correct.** The exemption is a lock waiver, not a
  content category; `capture` runs the same note check for it.
- **Still counts against the allowance: correct.** A deadline-exempt item
  promotes through the same gate and charged 30 minutes in
  `test_a_locked_item_cannot_be_promoted_but_a_deadline_exempt_one_can`.
- **Lifespan exemption: correct** per D1.10, and covered by an existing test.
- **Surfaced before it expires: NOT correct.** See **Reported, not fixed —
  R2**.

### j. Week boundaries across the Lisbon DST transition — **CORRECT**

`clock.next_week_start` does the arithmetic in the local zone and converts back
(`clock.py:108-111`), which is the whole reason it exists. Measured across the
October 2026 transition:

- the week containing Tue 20 Oct opens `2026-10-18T17:00:00+00:00` and closes
  `2026-10-25T18:00:00+00:00` — **169 UTC hours**, seven local days;
- both boundaries are Sunday 18:00 Europe/Lisbon, one at UTC+1 and one at
  UTC+0. No hour of drift;
- the following week is 168 hours, as it should be;
- `week_start_for(end) == end`: 18:00 exactly opens the new week, on both sides
  of the transition (`clock.py:97-98`).

Pinned by `test_a_week_across_the_lisbon_dst_change_is_seven_local_days`. Week
keys are fixed-width ISO-8601 UTC, so the `week_start < ?` comparison in
`roll_over_week` (`db.py:601-604`) stays chronological across the change.

### k. Idempotence — **CORRECT**

**Triage twice.** `run_triage` records the completed week in `meta`
(`triage.py:294`, written last so a partial run is retried, not skipped). A
second call in the same week sends nothing and changes nothing: item states and
`cycles_seen` byte-identical, the `weeks` row identical. A third call *after* a
promotion also changed nothing — in particular it did not lapse the item that
had just been promoted.

**Poll twice.** `poll._record` skips an observation identical to the previous
one (`poll.py:133-136`), so a second run in the same minute adds no `listening`
row and leaves `listened_min` unchanged. `check_alerts` is guarded by
`alerts_sent` with an `INSERT OR IGNORE` on the composite key, and sends before
it records so a Telegram failure is retried tomorrow rather than swallowed
(`poll.py:199-205`). Polling an already-`played` item twice raises nothing.

**Double promotion.** A second `promote()` on the same item raises
`IllegalTransition` (the no-op rule, D1.14) and does not charge the week twice;
`bot.on_callback` short-circuits it first anyway (`bot.py:406-408`).

**`roll_over_week` twice.** `ON CONFLICT(week_start) DO UPDATE` refreshes
allowance and debt but never touches `promoted_min` / `listened_min`
(`db.py:626-634`), and the debt it recomputes from the closed week is the same
number.

---

## Defects found and fixed

### Defect 1 — pre-capture listening was credited as this week's listening, and the gate overcharged for it
**Severity: high** (silently corrupts the allowance, the debt mechanism and the
promotion cost). **Fixed.**

`resume_point` is a position. `poll._record` treats "no previous observation"
as position 0 (`poll.py:127`), and nothing recorded the position an episode was
already at when it was captured. Measured, against an episode 58 minutes long
that the user had already heard 21 minutes of before capturing it:

```
promotion_cost_min at capture: 58        # D1.7 says this should be 37
week after promote:  promoted_min=58
after a poll in which NOTHING was listened to:  listened_min=21
listening rows: [(1260000, 1260000)]     # 21 minutes credited from nowhere
```

Both halves are wrong and they compound. The 21 minutes land in the week of the
*first poll*, which can be months after the listening happened, and feed
straight into `roll_over_week`'s debt calculation. And D1.7's stated purpose —
"an item already half heard costs half" — failed for exactly the case it
describes; the fixture that models it is named `PARTIAL` / "Half Heard
Already".

**Fix** (`bot.py:348-362`): the capture path now seeds a zero-delta baseline
observation with the position the episode is already at, when that position is
non-zero. The position is recorded; none of it is credited. After the fix the
same scenario gives `promotion_cost_min == 37`, `promoted_min == 37`, a poll
with no listening credits 0, and 20 minutes listened after capture still counts
in full. Pinned by
`test_a_part_heard_episode_does_not_arrive_with_phantom_listening_time`, which
drives the real `bot.on_message` handlers so the fix is verified across the
seam rather than at the unit that contains it.

This is the one change I made that is more than a local bug fix, so: it is
deliberately in `bot.py` rather than `db.py` because `db.capture()` cannot know
the position without a signature change, and `CONTRACTS.md` forbids widening a
signature without amending the contract first. `bot.py` is the only capture
path in the system, so the invariant holds today. **The better long-term home
is `db.capture(..., position_ms)`**, which would also let `db` compare against
existing history — see the limitation in D4.1 (DECISIONS.md). No existing test
changed: the fix is inert when the position is 0, which is every case the
current suite covers. `FakeDB` in `tests/test_bot.py` gained a
`record_listening` method so the double stays contract-shaped.

### Defect 2 — funnel stage 3 could report above 100%
**Severity: high** (corrupts the output the whole system exists to produce).
**Fixed.**

`funnel_stats` counted `played` as any item whose episode had a positive
listening delta, and `fully_played` as any item in state `played`, while
dividing both by `promoted`. Out-of-band listening on an item that was never
promoted therefore entered the numerator of a stage whose denominator excludes
it. Measured: two items, one promoted and heard, one never promoted and heard
out of band →

```
promoted 1  played 2  played_rate 2.0
```

`/stats` would have rendered `played 200% (2/1)`. The brief defines stage 3 as
"promoted → actually played (how much was wanted versus imagined)", and D1.12
asserts out-of-band listening "does not corrupt funnel stage 3, whose
denominator is `promoted_at IS NOT NULL`" — true of the denominator, false of
the numerator as written.

**Fix** (`db.py:743-759`): both numerators are restricted to items with
`promoted_at IS NOT NULL`. Same scenario now reports `promoted 1, played 1,
rate 1.0`. The listening itself is not lost — it is still in `listening` and
still charges the week's allowance, which is what D1.12 is actually protecting.
Pinned by
`test_out_of_band_listening_never_pushes_a_funnel_rate_above_100_percent`,
which asserts both. Neither existing funnel test changed; both already used
promoted items for the numerator.

### Defect 3 — `db.promote()` would promote an item still inside its 48h lock
**Severity: medium** (no live path reaches it today; the brief states it as an
invariant and nothing enforced it). **Fixed.**

The brief: "Locked items do not appear in triage and **cannot be promoted**."
`LEGAL_TRANSITIONS["locked"]` includes `"promoted"` (`db.py:63`), justified in
`CONTRACTS.md` §1 as the deadline-exemption case — but a deadline-exempt item
is created `queued`, not `locked` (`db.py:316`), so that justification never
applies and the entry only permitted the thing the brief forbids. `promote()`
checked the transition table and the budget, and nothing else:

```
state: locked | locked until 2026-09-03 10:00:00+00:00
PROMOTED A LOCKED ITEM -> promoted | promoted_min=167
```

**Fix** (`db.py:537-550`): `promote()` re-checks the lock and raises
`IllegalTransition` when the item is `locked`, still inside its window, and not
deadline-exempt. The transition table is untouched, so the contract's state
machine is unchanged. Pinned by
`test_a_locked_item_cannot_be_promoted_but_a_deadline_exempt_one_can`, which
also asserts the exempt item still promotes and still costs its minutes.

### Defect 4 — deadline items were told every week that they were about to expire
**Severity: medium** (a standing lie in the one message the user is meant to
act on). **Fixed.**

`triage._should_expire` exempts deadline items from lifespan expiry until the
deadline passes (`triage.py:207-216`, D1.10). The warning flag did not:
`warning = bumped.cycles_seen >= settings.lifespan_cycles`. A deadline item
whose deadline is two months out therefore received "Last cycle — this expires
at the next triage unless you promote it" at cycle 2 and every cycle after,
forever, while `_should_expire` went on keeping it alive:

```
triage 1: cycles=1 warned=False
triage 2: cycles=2 warned=True   <- false
triage 3: cycles=3 warned=True   <- false
triage 4: cycles=4 warned=True   <- false
```

**Fix** (`triage.py:219-232`, used at `triage.py:284`): a new
`_will_expire_next_cycle()` computes the flag as "would `_should_expire` be
true at the next triage", which is exactly what the message claims. For a
deadline item that is `deadline <= next_week_start(now)`; for everything else
it is the unchanged lifespan test. The item above is now never warned until the
week its deadline actually falls in, where the warning is true and does fire.
Pinned by
`test_a_deadline_item_is_not_told_every_week_that_it_is_about_to_expire`.
No existing test asserted a warning on a deadline item.

### Defect 5 — the schema did not back the non-empty why-note
**Severity: low** (defence in depth; `capture()` was and is correct).
**Fixed.**

`delta_ms >= 0` has a CHECK constraint backing the code-level clamp
(`schema.sql:64`); the why-note, the other invariant of the same kind, had only
`NOT NULL`, and a raw `INSERT` with `'   '` succeeded.

**Fix** (`schema.sql:33-39`): `CHECK (TRIM(why_note, ' ' || char(9) || char(10)
|| char(13)) <> '')`. The explicit character set matters — SQLite's
one-argument `TRIM` strips spaces only, so the obvious `TRIM(why_note) <> ''`
still accepted `"\t\n "`; I caught that because the test I wrote for the fix
failed on it. Verified against `''`, `'   '`, `'\t\n '` and `'\r\n\t'`, with a
real note still accepted. Pinned by
`test_the_schema_refuses_a_blank_why_note_even_by_raw_sql`.

*Limitation:* `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so the constraint
applies to newly created databases only. `data/` is empty, so there is no
existing database to migrate; if one is ever created before this ships, the
constraint would need a `migrate()` step. Recorded in DECISIONS.md D4.2.

### Defect 6 — the "no live calls" rule was enforced in only one test module
**Severity: medium** (a rule this absolute needs a mechanism, not a
convention). **Fixed.**

D2.15 records an autouse fixture that makes a real socket attempt fail
immediately. It lives in `tests/test_spotify.py:50-58` — the one module that
could not have made a live call anyway, since it injects
`httpx.MockTransport` everywhere. `test_db.py`, `test_bot.py`, `test_poll.py`
and `test_triage.py` had no guard at all, and nothing anywhere stopped a test
reading the real `.env` and picking up live credentials. There is no
`conftest.py`, so nothing was shared.

**Fix**: added `conftest.py` at the project root with an autouse fixture that
applies to every test in the repository. `socket.getaddrinfo`,
`socket.create_connection`, `socket.socket.connect`/`connect_ex` and both httpx
transports raise; so does any `read_text` of the real `.env`, naming
`config.test_settings()` as the alternative.

`socket.socket` itself is deliberately *not* blocked: asyncio builds a
self-pipe with `socket.socketpair()` on every event loop and `triage.send()`
legitimately drives a coroutine-returning bot double through `asyncio.run()`.
Blocking the constructor failed 52 tests that never go near a network — the
reason is written into the conftest so nobody tightens it back.

**With the guard in place the entire suite passes.** That is the audit result
for (f): the suite was already hermetic; it simply had no proof.

---

## Reported, not fixed

### R1 — nothing is enforced about `TZ` being a standard POSIX variable
**Severity: low. Not fixed — it is a config-naming decision, not a bug.**

`config.load()` overlays `os.environ` for every key in `_KEYS`
(`config.py:53`), and `_KEYS` includes `"TZ"` (`config.py:80`). `TZ` is a
standard POSIX variable meaning the *system* timezone, not an application
setting. A cron environment or a shell profile that exports `TZ=UTC` would
silently move triage from Sunday 18:00 Lisbon to Sunday 18:00 UTC — an hour off
for half the year, which is precisely the failure D1.3 was written to prevent,
arriving through the config layer instead of the arithmetic.

No test is affected (tests use `config.test_settings()`, which never reads the
environment). The fix is to rename the key to `POP_TZ` in `config.py` and
`.env` / `.env.example`, which touches deployment and belongs to whoever owns
the VPS.

### R2 — a deadline item is never surfaced between triages, so the deadline feature does not do what the brief says
**Severity: medium. Not fixed — the fix is a new message path, which is a
feature.**

The brief: "An item with a deadline **surfaces before it expires regardless of
where the weekly cycle sits.**" The implementation's entire answer to this is
the lock exemption: a deadline falling inside the 48h lock makes the item
`queued` at capture instead of `locked` (`db.py:316`). But being `queued` only
means it will appear at the *next Sunday triage*. Nothing runs between triages
except `poll.py`, and `poll.py` posts only allowance alerts.

So the common case fails. Captured Monday for a Tuesday event: the item is
`queued` immediately, is never posted to the user, and at the following
Sunday's triage `_should_expire` finds the deadline passed and soft-deletes it
(`triage.py:214-215`). I ran exactly that and watched it expire without ever
having been shown. The lock exemption, on its own, buys nothing: it changes a
state the user never sees.

Any deadline landing between two Sundays behaves the same way. That is most
deadlines.

The fix is for `poll.py` — the only daily process — to post deadline items
whose deadline falls before the next triage, with `alerts_sent`-style
bookkeeping so it says so once rather than every morning. That is new
user-facing behaviour with its own dedup design, it is outside `CONTRACTS.md`
§5, and Phase 4's brief says not to add features. It needs a decision, not a
patch, so I have left it and flagged it. **This is the largest gap between the
brief and the build.**

---

## Looked wrong, is actually right

- **`roll_over_week` closes the preceding week, not `current_week()`**
  (`db.py:601-604`). Reads like an off-by-one until you notice
  `week_start_for` treats exactly 18:00 as opening a new week
  (`clock.py:87,97`), so at the instant triage fires `current_week()` is zero
  seconds old. Closing it would compute every debt from an empty week and the
  debt mechanism would be dead code that passes its tests. Argued in D2.1 and
  confirmed: with 250 minutes listened, the closed week yields `debt_min = 70`
  and the opened week's effective allowance is 110.

- **`ms_to_min` claims "half up" and rounds a 30.000-second remainder down**
  (`db.py:72-74`). The contradiction is real but it is in the prose, and it was
  caught and documented rather than silently changed (D2.4). Cross-module
  agreement on what a minute is matters more than which way one boundary case
  falls. Left alone.

- **`promotion_cost_min` reads the most recent position, not the highest ever
  seen** (`db.py:509-523`), so a backward scrub *raises* the re-promotion cost.
  That looks like it rewards scrubbing until you notice it is the same
  direction as the clamp: a backward scrub means there genuinely is more left
  to hear. D2.10, consistent with D1.7.

- **`poll._record` writes a zero-delta row on the first sight of an episode
  sitting at position 0.** Looks like noise. It is the row that makes the
  *next* poll's delta correct, and `funnel_stats` requires `delta_ms > 0`
  (`db.py:741`) so it never counts as listening.

- **An item promoted on its final cycle expires the week it lapses**, so the
  brief's "they can be promoted again if they still earn it" is unreachable for
  it. This follows from D1.9 (`cycles_seen` never resets, or an item could
  ping-pong forever) and it works as intended for anything promoted on cycle 1.
  A deliberate consequence of an argued decision, not a defect. Left alone.

- **`bot.on_callback` leaves the still-holds buttons live after a promote**
  (D2.13). Correct: still-holds is recorded independently of promote/drop and
  is the cleanest signal in the system.

---

## Could not verify

- **Anything requiring a live Spotify or Telegram response.** By rule, no live
  call was made. `SpotifyClient`'s 401-retry, 429 and token-refresh paths are
  covered by `httpx.MockTransport` against hand-written response shapes
  (`tests/test_spotify.py`); whether those shapes match today's API is
  unverifiable from here. The `resume_point` contract in particular
  (`spotify.py:501-511`) is asserted against the documented shape only.
- **`SyncBot`, `main()` in all three entry-point modules, and `_LOCALE_RE`
  against real Spotify share text.** All are `# pragma: no cover` production
  glue that no test executes, deliberately.
- **Behaviour on an existing production database.** `data/` is empty, so the
  `CREATE TABLE IF NOT EXISTS` limitation on Defect 5's constraint could not be
  exercised against a real prior schema.
- **Whether Lisbon's 2026 transition dates are correct.** Taken from the
  `zoneinfo` database on this machine; the arithmetic is verified against it,
  not against an independent source.

---

## Final state

```
445 passed
```

436 before this pass, 9 added: one end-to-end lifecycle
(`test_full_lifecycle`, asserting state and `funnel_stats()` at all eight
steps) and eight regression tests, one for each defect fixed plus the gate
boundary and the DST week. No existing test was modified, deleted or weakened.
`tests/test_bot.py`'s `FakeDB` gained one method so the double still matches
the module it stands in for.

---

# Post-audit fixes

Both items from "Reported, not fixed" were escalated and have been implemented.
R2 first, because it is the one that made a documented feature of the brief
inert. Nothing else in the audit changed.

Final state: **472 passed** (445 before this pass, 27 added). One existing test
was adjusted — see "One existing test moved" below.

---

## R2 — deadline items are now surfaced daily, between triages

**Was: reported, not fixed. Now: fixed.**

The brief, `pop-build-brief.md:79-81`: "An item with a deadline surfaces before
it expires regardless of where the weekly cycle sits. It still requires a note
and still counts against the allowance." That is a requirement, not a
suggestion, and the only mechanism that existed for it was the lock exemption
(`db.py:281-291`, `db.py:316`), which flips `locked` to `queued` at capture —
a state the user never sees. I reproduced the full failure again before
changing anything: captured Monday for a Tuesday event, the item was posted
nowhere across four daily polls and `run_triage` soft-deleted it the following
Sunday for having passed its deadline (`triage.py:242`).

`poll.py` is the only daily process, so delivery went there.

**What changed**

- `schema.sql:94-105` — a `deadline_notices (queue_id PRIMARY KEY, sent_at)`
  table. `CREATE TABLE IF NOT EXISTS`, so `db.init_db()` stays idempotent and
  an existing production database picks it up on the next run of any of the
  three entry points (`poll.py:334`, `triage.py:402`, `bot.py:517`). It is
  dedup bookkeeping in exactly the sense `alerts_sent` is — no per-item user
  input, so the brief's friction constraint (`pop-build-brief.md:313-316`) is
  untouched.

- `poll.py:112` — `run_poll()` calls `db.lift_expired_locks()` before
  surfacing. Locks are time-based; lifting them daily rather than only at
  Sunday's triage is strictly more accurate, and it is required here or an
  item captured Monday with a Thursday deadline would still be `locked` on
  Wednesday and would never be surfaced. `lift_expired_locks` only moves rows
  whose lock has already expired, so `run_triage` calling it again on Sunday
  (`triage.py:297`) is a no-op.

- `poll.py:208-249` — `surface_deadlines()`. Posts every item that is
  `queued`, has a non-null deadline falling at or before
  `clock.next_week_start(now, settings.tz)`, and has no `deadline_notices`
  row. The send precedes the row (`poll.py:248-249`), matching D3.5: a
  Telegram outage leaves the notice unrecorded and tomorrow's poll retries it.

- `triage.py:176-195` — `render_deadline_notice()`, a sibling of
  `render_item()` that calls it verbatim and prepends one lead line
  (`triage.py:64`). Same fields, same order, and critically the same keyboard
  and the same `f"{action}:{item_id}"` callback payloads, so
  `bot.on_callback` (`bot.py:372-375`) needs no knowledge that this path
  exists. The dependency direction is unchanged — `poll.py` already imported
  from `triage.py`.

- `CONTRACTS.md` §5 now carries both functions and the ordering.

**No privileged category was created.** The item needed a note to exist at all
(`db.py:302-304`), promotion runs through `db.promote()` and is refused by the
allocation gate like anything else, and being surfaced changes no state: after
a poll the item is still `queued`, still `cycles_seen = 0`, still
`promoted_at IS NULL`. The funnel is unaffected — this moves messages, not
rows.

**One boundary case decided deliberately.** The comparison is
`deadline <= next_week_start`, not `<`. `run_triage` expires on
`deadline <= now` (`triage.py:242`), so an item whose deadline falls exactly on
the triage instant is expired by that triage rather than posted by it; a strict
`<` here would leave that one case reproducing the original bug. This mirrors
`triage._will_expire_next_cycle` (`triage.py:256`), which asks the same
question the same way.

**Nothing filters on `deadline >= now`**, deliberately. An outage on the
deadline day must not silently consume the single notice an item ever gets;
that is the same reasoning as D3.5 and it is written into the docstring
(`poll.py:228-232`).

**Tests** — `tests/test_poll.py:646-958`, 16 of them, including the four the
escalation named: the Monday-capture/Tuesday-event case is surfaced; it is
surfaced exactly once across five consecutive polls; a deadline after the next
triage is not surfaced early (and *is* surfaced once the boundary moves past
it); an already-promoted item is not surfaced; a surfaced item is still refused
by the allocation gate at 167 minutes against 180 with 100 already spent; and a
`BrokenBot` send leaves `deadline_notices` empty so the next day retries.

---

## R1 — `TZ` renamed to `POP_TZ` and removed from the override set

**Was: reported, not fixed. Now: fixed.**

`TZ` is the standard POSIX system-timezone variable. `config.load()` overlaid
`os.environ` for it, so a cron or systemd unit exporting `TZ=UTC` would have
moved the triage boundary off Lisbon local time — the drift D1.3 exists to
prevent, arriving through the config layer rather than the arithmetic. The
build already hit this class of bug once from the other direction: a launchd
job stamped CEST instead of WEST and fired an hour early
(`README.md:105-109`).

- `config.py:70-75` — reads `POP_TZ`, default `"Europe/Lisbon"`.
- `config.py:82-89` — `TZ` removed from `_KEYS` entirely, so no ambient value
  under that name can reach `Settings` at all. This is the part that matters:
  renaming the read alone would still have left `TZ` in the overlay.
- `.env` — the `TZ=` line replaced in place with `POP_TZ=`. No other line was
  touched, no value was printed, and the file is still `0600` (verified before
  and after).
- `.env.example:19-21`, `check_env.py:32` — same rename. An old `.env` still
  carrying `TZ` now fails the Phase 0 gate loudly instead of silently working
  under the POSIX name.
- `README.md:111-120` — `CRON_TZ` and `POP_TZ` are now explicitly distinguished.
  `CRON_TZ=Europe/Lisbon` in the crontab block is untouched: it controls when
  the daemon fires, which is a separate concern from what POP believes its
  timezone is.

**Tests** — `tests/test_config.py`, 11 of them, new module (`config.py` had no
test module of its own, which is how this reached production). The two named
in the escalation: `POP_TZ` is honoured from the env file, from the
environment, and from an explicit override; and `monkeypatch.setenv("TZ", "UTC")`
leaves `Settings.tz == "Europe/Lisbon"` both when `POP_TZ` is set and when
nothing is. There is also a structural assertion that `"TZ" not in config._KEYS`,
so a future edit to that set cannot quietly re-add it.

---

## One existing test moved

`test_out_of_band_listening_on_a_locked_item_is_recorded`
(`tests/test_poll.py:612-631`) captured at `CAPTURED` and polled six days later
at `MONDAY`, then asserted the item was still `locked`. With the daily lock
lift that assertion is false — correctly so, since the 48 hours had long
elapsed. The poll now runs 23 hours after capture (`tests/test_poll.py:49`), so
the item is genuinely inside its lock when the position is read and the test
asserts what its name claims: a locked item is polled, and polling it does not
move it. D1.12's claim is unchanged and still covered.

No other existing test was modified, deleted or weakened.

---

## Still not fixed, still worth knowing

- **`db.migrate()` remains a no-op at `schema_version = 1`** (`db.py:187-203`).
  `deadline_notices` reaches an existing database through
  `CREATE TABLE IF NOT EXISTS` in `init_db`, which works for an added table but
  would not work for an added *column* or the `why_note` CHECK from Defect 5.
  The migration hook exists and is still empty. `data/` is empty here, so as
  with the Phase 4 audit this could not be exercised against a real prior
  schema.

- **A surfaced item that is neither promoted nor dropped is still expired at
  the next triage**, which is correct — the deadline has passed by then — but
  it now expires *having been seen*, so the drop is a real user decision rather
  than a silent deletion. Worth watching in the funnel: expiry of deadline
  items was previously indistinguishable from the user never being asked.

- **`bot.py` has no knowledge of `deadline_notices`.** If a future feature
  ever re-posts an item deliberately, it will need to clear that row, and
  nothing enforces or documents that beyond the docstring.
