# CONTRACTS

Phase 1 output. Every module in Phases 2 and 3 implements exactly these
signatures and imports other modules only through them. Do not widen a
signature without updating this file first.

Shared modules already written in Phase 1 and owned by nobody in Phase 2:
`config.py`, `clock.py`, `schema.sql`.

---

## 0. Conventions

- **Timestamps.** Every stored timestamp is `clock.iso(dt)` — ISO-8601 UTC with
  an explicit `+00:00`. Fixed width, so SQL string comparison is chronological
  comparison. Parse with `clock.parse(s)`. Never store naive local time.
- **Time is injected.** Anything that needs "now" takes a `clock.Clock`. Tests
  pass `clock.FrozenClock`. No module calls `datetime.now()` directly.
- **Minutes vs milliseconds.** Spotify speaks milliseconds; the allowance
  speaks minutes. Convert at the boundary with `ms_to_min()` (round half up)
  and keep `_ms` / `_min` suffixes honest.
- **No live calls off the happy path.** `spotify.py` is the only module that
  performs HTTP. Everything else takes a `SpotifyClient`-shaped object, so
  tests inject `FakeSpotify`.

```python
def ms_to_min(ms: int) -> int:      # db.py owns the canonical implementation
    return (ms + 29_999) // 60_000  # nearest minute, ties DOWN (30.000s remainder -> 0)
```

The tie direction is deliberate and pinned by a test. It only differs from
ties-up at an exact 30.000-second remainder, and cross-module agreement on what
a minute is matters far more here than which way that one case falls.

---

## 1. The state machine

`queue.state` is the funnel. Six states, three of them terminal.

| state | terminal | meaning |
|---|---|---|
| `locked` | no | captured, inside the 48h lock, invisible to triage |
| `queued` | no | lock lifted, eligible for triage |
| `promoted` | no | in the playable slate, cost charged against the allowance |
| `dropped` | **yes** | rejected at triage — soft deleted |
| `expired` | **yes** | lifespan or deadline ran out — soft deleted |
| `played` | **yes** | `fully_played` observed |

### Legal transitions

```
locked   → queued      lock expired (lift_expired_locks)
locked   → promoted    deadline falls inside the lock, so the item is lock-exempt
locked   → dropped     user dropped a deadline-surfaced item
locked   → expired     deadline passed while still locked
locked   → played      listened out of band before the lock lifted

queued   → promoted    triage promotion, allocation gate permitting
queued   → dropped     triage drop
queued   → expired     lifespan exhausted, or deadline passed
queued   → played      listened out of band without being promoted

promoted → queued      weekly lapse at reset (not fully played)
promoted → dropped     user dropped a promoted item
promoted → expired     deadline passed while promoted
promoted → played      fully_played observed
```

Everything else is illegal, **including any transition out of a terminal
state**. `db.set_state()` raises `IllegalTransition` rather than silently
passing. A no-op transition to the same state also raises: it always means a
caller lost track of what it was doing.

```python
ACTIVE_STATES  = ("locked", "queued", "promoted")
TERMINAL_STATES = ("dropped", "expired", "played")
REMOVED_STATES = ("dropped", "expired")   # soft-deleted: hidden from every default query
```

### Soft delete

`dropped` and `expired` rows stay in the database forever and are excluded from
**every** default query. The only way to see them is `include_removed=True`,
which exists solely for the stats path. `played` is terminal but is *not*
soft-deleted — it is the success outcome and appears in stats normally, though
it never appears in triage.

There are no `DELETE` statements anywhere in this codebase. Phase 4 audits for
this.

---

## 2. `db.py` — Agent A

```python
class IllegalTransition(Exception): ...
class AllowanceExceeded(Exception):
    """Raised by promote() when the gate refuses. Carries the overage."""
    def __init__(self, message: str, overage_min: int, remaining_min: int, cost_min: int): ...

@dataclass(frozen=True)
class QueueItem:
    id: int
    spotify_id: str
    title: str
    show: str
    duration_ms: int
    why_note: str
    captured_at: str
    deadline: str | None
    state: str
    cycles_seen: int
    still_holds: int | None
    promoted_at: str | None
    resolved_at: str | None

@dataclass(frozen=True)
class Week:
    week_start: str
    allowance_min: int
    debt_min: int
    promoted_min: int
    listened_min: int
    @property
    def effective_allowance_min(self) -> int: ...   # allowance_min - debt_min, floored at 0
    @property
    def remaining_min(self) -> int: ...             # effective_allowance_min - promoted_min
```

### Connection and lifecycle

```python
def connect(settings: Settings) -> sqlite3.Connection
def init_db(conn) -> None                      # applies schema.sql; idempotent
def migrate(conn) -> None                      # no-ops at schema_version 1
```

Rows come back as `sqlite3.Row`. `foreign_keys` is ON.

### Capture

```python
def upsert_episode(conn, spotify_id: str, title: str, show: str,
                   description: str | None, release_date: str | None,
                   duration_ms: int) -> None

def capture(conn, clk: Clock, settings: Settings, *, spotify_id: str,
            why_note: str, deadline: str | None = None) -> QueueItem
```

`capture()` **requires** a non-empty `why_note` and raises `ValueError` on
empty or whitespace-only input. There is no code path that writes a `queue` row
without one. This is why the bot holds the capture in memory until the reply
arrives — see §4.

Initial state is `locked`, unless a `deadline` falls inside the lock window, in
which case the item is created `queued` (deadline exemption, per the brief).

### Reading

Every one of these excludes `REMOVED_STATES` unless told otherwise.

```python
def get_item(conn, item_id: int) -> QueueItem | None
def active_items(conn) -> list[QueueItem]                       # locked+queued+promoted
def triage_items(conn, clk, settings) -> list[QueueItem]        # queued only, lock already lifted
def promoted_items(conn) -> list[QueueItem]
def polling_targets(conn) -> list[str]                          # distinct spotify_ids worth polling
def all_items(conn, *, include_removed: bool = False) -> list[QueueItem]
```

`polling_targets()` returns episodes in `locked`, `queued` or `promoted`. Out-of-band
listening on a queued item is exactly the behaviour the brief wants visible, so
polling is not restricted to the promoted slate.

### State changes

```python
def set_state(conn, clk, item_id: int, new_state: str) -> QueueItem
def lift_expired_locks(conn, clk, settings) -> list[QueueItem]   # locked → queued
def record_still_holds(conn, item_id: int, answer: bool) -> QueueItem
def bump_cycle(conn, item_id: int) -> QueueItem                  # cycles_seen += 1
```

`set_state()` stamps `resolved_at` when entering a terminal state and
`promoted_at` on the **first** promotion only. `promoted_at` is never cleared,
including on lapse — it is the evidence for funnel stage 2.

### Weeks, allowance, debt

```python
def current_week(conn, clk, settings) -> Week          # creates the row if absent
def promotion_cost_min(conn, item: QueueItem) -> int   # remaining, not full duration
def promote(conn, clk, settings, item_id: int) -> QueueItem
def roll_over_week(conn, clk, settings) -> tuple[Week, Week]   # (closed, opened)
def record_listening(conn, clk, spotify_id: str, position_ms: int,
                     delta_ms: int, fully_played: bool | None) -> None
```

**`promotion_cost_min`** is the episode's *remaining* minutes:
`ms_to_min(max(0, duration_ms - last_known_position_ms))`. An item already half
heard costs half. Charging full duration for a re-promotion would overstate the
commitment and make the gate lie.

**`promote()` is the allocation gate.** It computes the cost, and if
`week.promoted_min + cost > week.effective_allowance_min` it raises
`AllowanceExceeded` **without changing any state**, carrying the overage in
minutes so the bot can say by how much. Otherwise it transitions
`queued → promoted` and adds the cost to `weeks.promoted_min`.

**`roll_over_week()`** closes the *outgoing* week and opens the one containing
`now`:
1. Lapse every `promoted` item that is not `played` back to `queued`.
2. `debt = clamp(listened_min - effective_allowance_min, 0, allowance_min)`.
3. Create the `weeks` row for the week containing `now`, with that debt and a
   fresh allowance from config.

**Which week gets closed, and why it is not `current_week()`.** Triage fires
*at* Sunday 18:00, and `clock.week_start_for()` treats exactly 18:00 as opening
a new week. So at the moment triage runs, `current_week()` is a week zero
seconds old with nothing in it. Closing that would compute every debt against an
empty week, debt would always be 0, and the mechanism would be dead code that
passes its tests. `roll_over_week()` therefore closes **the most recent week row
that opened before the week containing `now`**. It is idempotent across repeated
triage runs in the same week.

Debt is driven by **listened** minutes, not promoted minutes: the gate already
makes it impossible to over-promote, so the only way to exceed the allowance is
to actually listen past it. The clamp at `allowance_min` stops one blown week
from zeroing out several later ones.

### Stats

```python
@dataclass(frozen=True)
class FunnelStats:
    captured: int
    survived_lock: int
    promoted: int
    played: int
    fully_played: int
    survived_rate: float | None      # None when the denominator is 0
    promoted_rate: float | None
    played_rate: float | None
    fully_played_rate: float | None
    median_days_to_drop: float | None
    still_holds_yes_rate: float | None
    still_holds_answered: int

def funnel_stats(conn, clk, settings, *, since: str | None = None) -> FunnelStats
```

This is the **only** function permitted to read `REMOVED_STATES`.

- `captured`: all items whose lock window has closed (items still inside their
  lock are undetermined and excluded from every denominator).
- `survived_lock`: captured items that were not resolved (`dropped`/`expired`)
  before `captured_at + lock_hours`.
- `promoted`: `promoted_at IS NOT NULL`.
- `played`: any `listening` row for that episode with `delta_ms > 0`.
- `fully_played`: `state = 'played'`.
- `median_days_to_drop`: median of `resolved_at - captured_at` over
  `state = 'dropped'`.
- `still_holds_yes_rate`: `sum(still_holds) / count(still_holds IS NOT NULL)`.

`since=None` means all time; `/stats` also calls it with the `week_start` of
eight weeks ago.

---

## 3. `spotify.py` — Agent B

```python
class SpotifyError(Exception): ...
class InvalidEpisodeRef(ValueError): ...       # message is user-facing

@dataclass(frozen=True)
class Episode:
    spotify_id: str
    title: str
    show: str
    description: str | None
    release_date: str | None
    duration_ms: int
    resume_position_ms: int
    fully_played: bool
```

### Parsing

```python
EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")

def parse_episode_ref(text: str) -> str
```

Accepts exactly two forms and returns the bare 22-character base62 id:

- `https://open.spotify.com/episode/{id}` with any query string, which is
  stripped, and with an optional locale segment (`/intl-pt/episode/{id}`)
- `spotify:episode:{id}`

Anything else raises `InvalidEpisodeRef` with a message the bot shows verbatim.
A Spotify link that is a track, show, album or playlist gets a message naming
what it actually is — silent rejection is forbidden by the brief.

### Auth and fetch

```python
class SpotifyClient:
    def __init__(self, settings: Settings, *, transport=None): ...
    def access_token(self) -> str          # refreshes on first use and on expiry, transparently
    def get_episode(self, spotify_id: str) -> Episode
    def get_episodes(self, ids: list[str]) -> list[Episode]   # batched, ≤50 per request
```

Auth is refresh-token only; the access token is cached in memory with its
expiry and renewed transparently. A 401 triggers exactly one retry after a
forced refresh, then raises. `description` is HTML-stripped before it is
returned.

`resume_position_ms` comes from `resume_point.resume_position_ms` and
`fully_played` from `resume_point.fully_played`. If `resume_point` is absent —
which means the token lost the scope — that is a `SpotifyError`, not a silent
zero. Reading a missing scope as "listened to nothing" is the one failure this
system cannot afford to swallow.

### The clamp

```python
def clamped_delta(previous_position_ms: int, current_position_ms: int) -> int:
    return max(0, current_position_ms - previous_position_ms)
```

`resume_point` is a **position, not a total**. Scrubbing backward or
relistening drops it, and an unclamped delta goes negative, silently crediting
time that was not earned back. Agent B writes unit tests for four cases as it
goes: forward listening, backward scrub, relisten from zero, and no change.

### Test double

Agent B also ships `FakeSpotify` with the same surface, driven by a dict of
fixtures. Agents C and the Phase 3/4 work import that, never the real client.

```python
class FakeSpotify:
    def __init__(self, episodes: dict[str, Episode]): ...
    def set_position(self, spotify_id: str, position_ms: int,
                     fully_played: bool = False) -> None
```

---

## 4. `bot.py` — Agent C

```python
def build_app(settings: Settings, conn, clk, spotify) -> Application
def authorised_only(handler)                   # decorator
```

`spotify` is any object with the `SpotifyClient` surface. The bot never
constructs one itself, so tests inject `FakeSpotify`.

### Authorisation

Every update is checked against `settings.telegram_user_id`. Anything else is
dropped with **no reply at all** — not an error message, not a log line the
sender can observe. Per the brief: reject everything else silently.

### Capture conversation

State lives in `context.user_data`, **not** in the database:

1. User sends a link. `parse_episode_ref` runs. On failure the bot replies with
   the rejection message and the conversation ends.
2. Bot fetches metadata, holds `(spotify_id, metadata)` in `user_data`, and
   asks **"Why?"**.
3. **Nothing is written to the database yet.** If the user never replies, no
   row is ever created. This is what makes the note mandatory rather than
   optional cleanup after the fact.
4. The reply becomes `why_note`. An optional trailing deadline is parsed out of
   it. Only now does `db.capture()` run.
5. Bot confirms with **title, show and duration**, so the time cost is visible
   at the moment of capture, plus the lock expiry.

A second link sent mid-conversation replaces the pending capture rather than
queueing behind it.

### Deadline parsing

```python
def parse_deadline(text: str, clk, tz: str) -> tuple[str, str | None]
```

Returns `(why_note_without_the_deadline, deadline_iso_or_None)`. Recognises a
trailing `by <date>` / `before <date>` in a small set of forms: `by friday`,
`by 3 sep`, `by 2026-09-03`, `by tomorrow`. Anything unrecognised is left as
part of the note — a misparse must never eat the user's words. Deadlines
resolve to 18:00 local on the named day.

### Inline buttons

Callback data is `f"{action}:{item_id}"` with action in
`{"promote", "drop", "holds_yes", "holds_no"}`.

- `promote` → `db.promote()`. On `AllowanceExceeded` the button does **not**
  promote; it answers with the overage: *"That would put you 25 min over. 40 min
  left this week."*
- `drop` → `db.set_state(..., "dropped")`, a soft delete.
- `holds_yes` / `holds_no` → `db.record_still_holds()`. This is recorded
  independently of promote/drop — it is the cleanest signal in the system and
  is never inferred from the other buttons.

Callbacks on an item already in a terminal state answer "already resolved"
rather than raising.

### Commands

```
/start   one-line explanation
/queue   the current unlocked queue, read-only
/stats   the single funnel message (implementation in triage.py, §5)
```

---

## 5. `triage.py` and `poll.py` — Phase 3

```python
# triage.py
def run_triage(conn, clk, settings, spotify, bot) -> None
def render_item(item: QueueItem, week: Week, warning: bool) -> tuple[str, InlineKeyboardMarkup]
def render_deadline_notice(item: QueueItem, week: Week) -> tuple[str, InlineKeyboardMarkup]
def render_stats(conn, clk, settings) -> str

# poll.py
def run_poll(conn, clk, settings, spotify, bot) -> None
def surface_deadlines(conn, clk, settings, bot) -> None
def check_alerts(conn, clk, settings, bot) -> None
```

`run_triage()` executes in this order, and the order matters:

1. `roll_over_week()` — lapse unlistened promotions, compute debt, open the week.
2. `lift_expired_locks()` — `locked → queued`.
3. Expire anything at or past its lifespan (`cycles_seen >= lifespan_cycles`)
   and anything whose deadline has passed, both as soft deletes.
4. `bump_cycle()` on each survivor, then post it with title, show, duration,
   why-note and cycle age. `cycles_seen == lifespan_cycles` after the bump gets
   the "last cycle" warning.
5. Post the week's header: allowance, debt carried, remaining.

`run_poll()` calls `lift_expired_locks()`, fetches positions for
`polling_targets()`, computes `clamped_delta` per episode, writes `listening`
rows, updates `weeks.listened_min`, flips `fully_played` episodes to the
`played` state, and then calls `surface_deadlines()` followed by
`check_alerts()`.

`surface_deadlines()` posts each `queued` item whose deadline falls at or
before `clock.next_week_start(now)` — i.e. one the weekly triage would expire
rather than reach — using `render_deadline_notice()`, which carries triage's
own keyboard and callback payloads. Once per item, ever, tracked in
`deadline_notices`. This is the brief's "an item with a deadline surfaces
before it expires regardless of where the weekly cycle sits"; see D5.1.

`check_alerts()` sends at most one `approaching` (≥80% of effective allowance)
and one `exceeded` (>100%) message per week, tracked in `alerts_sent`.

Both send before writing their bookkeeping row, so a Telegram outage is retried
by tomorrow's poll rather than swallowed (D3.5).

Crontab lines go in `README.md`. They are not installed.
