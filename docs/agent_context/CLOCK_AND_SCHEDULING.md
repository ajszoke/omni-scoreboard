# Clock and scheduling: toward one clock and one "tower"

Status: **accepted in external review round 4** (2026-11-15). A1a is built as the tower's first slice
(see "Steps"), and the head's rulings below amend the target. Nothing here is built yet beyond what
"Today" describes. This doc is the target that new timing code should move toward, so that we don't add
more one-off timers in the meantime.

## Why

With several leagues live at once, the board's airtime is the scarce resource. Each moment of
display should go to the most valuable thing the viewer can be shown without a spoiler. Content then
has to be handled like flights at a control tower:
- each item is scheduled for a moment;
- it is sequenced against everything else on priority and freshness;
- it can be amended, swapped or cancelled as new information lands (a review reverses a play, or a
  skipped score turns up late);
- it all runs off one clock that can be replayed.

## Today: the clocks we have

| Where | What it times | Driven by |
|---|---|---|
| `omni/app/runner.py` `run_forever` + `AppLoop.run_once` (MLB) | polling, pipeline, card queue **and rendering** | one fixed tick, 12 s by default |
| `ProviderSupervisor` | backoff after failures | `now` passed per call |
| `LiveBaseballPipeline` (`DelayPolicy`, `DelayedFeed`, `DelayedEventStream`) | TV delay anchored to when a play happened; meter readings; the final's embargo | `now` per tick |
| `InterleavedCardQueue` | which card next: two-level rotation, BURST takeover windows, RECURRING cooldowns | `now` per tick; **dwell is "the render loop's job", and no such loop exists** |
| `FootballBoard.step` (single-game NFL) | poll cadence, a receipt-anchored `--delay` buffer, meter lag, closing out a period before a break | ~10 fps frame loop that **also polls, synchronously** |
| `PlayStrip` | frames (10 s min, 20 s max each), stoppage holds, clearing on a field change, close-out | `now` per frame |
| `NflMetaLiveProvider` | halftime hold (plus its grace), correction watch, score backstop, injury news window | private datetimes on per-game state |
| Market (`PolymarketUsProvider`, the market-service client, the heartbeat thread, the cloud poller) | 30 s REST refresh; 10 s/60 s freshness; 30 s retry; 300 s heartbeat; 1 min cloud schedule | each its own |
| Rendering (`text_lane.py`, `LaneClock`, the halftime countdown) | marquee offsets, line start times, the colon blink | `RenderContext.now`; `LaneClock` keeps render-side memory |
| Launchers | time itself | `SystemClock` (MLB); raw `datetime.now`/`time.monotonic` (football) |

### What that adds up to

1. **Three cadences are conflated.** Data arrives every few seconds to minutes, per source. Decisions
   should happen whenever something changes. Frames need 10+ fps.
   - The MLB app does all three on one 12-second tick. It therefore draws a frame only every 12
     seconds, so a scrolling line is sampled far below its motion and can jump backward between
     frames. That is the "baseball jerk", and countdowns and blinks can't animate there either.
   - Football separates frames but still polls on the frame thread, so a slow request freezes the
     board.
2. **Timers are scattered.** A dozen objects each keep their own deadline. Nothing can answer "what
   happens next, and when?", which makes end-to-end replay and tuning by taste harder than they
   should be.
3. **There are three notions of delay.** TV delay is anchored to when a play happened (MLB). Football
   anchors its delay to when we received the play. The win meter applies its own relative lag. These
   should be one rule: estimate when a thing happened, then release it at that moment plus the
   viewer's delay.
4. **The same scheduling problem is solved twice.** The card queue picks which card comes next; the
   football strip picks which line comes next. Both need slots with minimum and maximum dwell,
   priority, freshness and preemption.

## Target

- **One clock seam, two readings.**
  - **UTC** dates observations and release times.
  - **Monotonic time** measures dwell, freshness since receipt, backoff, cooldown, animation phase and
    pacing.
  - Everything still takes `now` as an argument, as today. Replay advances both readings, and can jump
    the wall clock alone to test a clock change.
- **Owners.** Workers publish typed observations to one owner of planning state. The render loop reads
  immutable presentation snapshots. Reuse `Observation`, `DelayPolicy`, `DisplayTiming` and the existing
  attention modes rather than starting over.
- **The air clock.** The viewer's broadcast position is `now − viewer delay`.
  - Each source gets a lag model. NFLMeta runs about 20–30 s behind live, Polymarket's public quotes
    0–30 s, and StatsAPI is close to real time.
  - Each observation carries an estimated `happened_at` and becomes eligible at
    `happened_at + viewer delay`.
  - This replaces football's `--delay` and the meter lag, and generalizes `DelayPolicy`.
- **Flights (segments).** A segment is a typed unit of airtime. It specifies:
  - **What and where:** a card, or a line in a card's lane (the football strip, a ticker), and which
    surface it shows on.
  - **When:** the earliest start (when it clears the air clock) and a freshness deadline or decay.
  - **For how long:** minimum and maximum dwell.
  - **Priority**, with reasons.
  - **A preemption class:** may interrupt; waits for the current slot to end; or holds until a
    trigger fires, such as "play resumes".
  - **Sequence constraints:** within a game, plays air in order and before the break that followed
    them, and a correction replaces its original.
  - **Identity for edits:** a dedupe key plus a revision, so a segment can be amended, cancelled or
    replaced in place.
- **The tower.** It holds the plan for each surface.
  - **When it re-plans:** whenever a new observation, an edit or a cancellation arrives, and at
    timer boundaries.
  - **How it chooses:** by value (priority × freshness), within the rules: dwell, game order,
    fairness across games and leagues, and bounded takeovers.
  - **What it exposes:** what's showing now, what's next, and why.
- **Deadlines, not one heap for everything** (round 4).
  - Planning deadlines (polls due, holds ending, backstops, card expiries) share one deadline interface,
    and the planner wakes for the earliest.
  - Frames and in-flight I/O keep their own wakeups, so a slow network or display process can't hold up
    decisions.
  - The planner publishes `next_deadline` and a decision trace from A1a's first step.
- **Three loops:**
  1. **Ingest workers:** one per provider, off the render thread, posting observations to the tower.
  2. **The tower:** event-driven planning.
  3. **The renderer:** a fixed frame rate, or redraw-on-change for the Pi's 93 ms draws. It reads
     the tower's current plan and never waits on a network call.

## How today's pieces map onto it

- `DelayPolicy`, `DelayedFeed` and `DelayedEventStream` become the air clock and eligibility.
- `InterleavedCardQueue`'s rotation, BURST takeovers and RECURRING cooldowns become the tower's
  selection policy for the board surface, with dwell finally enforced there.
- `PlayStrip`'s rules (frames, stoppage holds, clearing on a field change, close-out) become the
  tower's policy for a card lane. The same machinery should drive baseball's Due Up and at-bat lines
  and any future ticker.
- Provider-side timers (halftime hold, correction watch, score backstop) and supervisor backoff
  become poll plans registered in the timer heap.
- `LaneClock` stays render-side as pure presentation. Once segments exist, it is keyed by segment
  identity rather than by lane geometry.

## Steps, smallest first (each useful on its own)

Round 4 groups the first five as A1a:
- step 1 and its traces (1 and 8, basic);
- step 2 with the typed `PollPlan` (2 and 3);
- step 3, PlayStrip first, compared on decisions from the PIT@CIN replay rather than frame for frame
  (4);
- step 4, the board's rotation (5).

1. **Split frames from ticks in the MLB app.** Draw the current card at a frame rate, and run
   `run_once` on its own cadence. This fixes the baseball jerk and makes countdowns and blinks live.
   Small.
2. **Take provider fetches off the frame thread**, in both football and MLB. A worker posts
   observations and drawing never blocks. Small to medium.
3. **One clock seam everywhere.** The football launcher uses `SystemClock`, and monotonic time is
   used only to pace sleeps. Small.
4. **Build the scheduling primitives** (segment, surface/lane, timer heap, tower interface) with
   `PlayStrip` as the first consumer. Behavior stays identical, proven by replaying the recorded
   PIT@CIN game frame for frame. Medium.
5. **Port the board's card queue onto the tower** and enforce minimum and maximum display there.
   Medium.
6. **Build the air clock and per-source lag models.** Fold football's delay and the meter lag into
   it, with lags measured from recordings (e.g., market jumps against NFLMeta's play times). Medium.
7. **The value squeeze:** freshness decay by segment kind, preemption classes, and fairness across
   leagues. Large; needs M6-004's multi-league plumbing.
8. **A plan inspector (a "departures board")**: a log or local view of the next N scheduled segments
   and why each is there, for tuning by taste and for debugging. Small and high-leverage. It can come
   early.

## Decisions (external review round 4, 2026-11-15)

- **Screen model:**
  - One full-card presentation per board on all three profiles, with typed child lanes tied to the
    card occurrence that's showing.
  - Card and strip have separate lifetimes and share the scheduling primitives. A timeout can hold the
    strip while its game is up, and the card can still yield the board.
  - Persistent regions wait until simultaneous content earns its place on the quad.
- **Preemption:**
  - Ordinary sports priority never cuts a visible minimum. A minimum starts at the first frame actually
    presented, so a delayed or failed render can't spend it offscreen.
  - Factual invalidation, hard expiry and device controls may clear or correct a region at once.
  - A field that moves without its play clears the caption that no longer describes it. With its play,
    field and caption switch together, with no blank.
- **Freshness:**
  - Semantic expiry, traced. Current situation coalesces per game and field. In focused NFL mode, every
    timely play description airs, in order; players' lines are opportunistic.
  - A stoppage ends on credible resumption, on another confirmed stoppage, or on a traced
    stale-source timeout.
  - In rotation, a latest start is kept apart from the hard end. Starting values: about 60 s for
    routine descriptions and 120 s for major plays, from air eligibility, all tunable.
  - Expired, unseen material stays in replay with its reason. Period closers get a bounded drain, and a
    decisive available play is an explicit predecessor of FINAL.
- **Viewer delay:**
  - Explicit settings **per board, per league** (the user's call, 2026-11-15). Occurrence, upstream
    as-of and actual receipt stay separate.
  - Release at the later of receipt and the latest credible event time plus the delay; with an
    uncertain occurrence, at receipt plus the delay. A revision or retraction is a new observation with
    its own release.
  - Clamp against an untrusted clock jump. Never shrink the embargo by a median provider lag.
  - Market and provider leads are measured in shadow mode, and local TV delay stays manual. A quote
    that can't be shown compatible with the visible score and play hides the meter.
- **Fairness:**
  - First filter for safety, eligibility, required ordering and a usable dwell window.
  - Bound the total seconds of burst takeovers, even under back-to-back alerts.
  - Then pay down airtime debt across eligible leagues and games: a base rotation slot after a burst,
    configurable favorite weighting, stable tie-breaks, and measured on committed airtime. An inactive
    league earns no debt.
  - Every wait or expiry carries a reason. Needs the mixed-league identity work first (M6-004).
- **The presentation contract** (A1a step 3):
  - stable identity and revision;
  - surface and parent;
  - typed payload and provenance;
  - availability;
  - latest start and hard expiry, where they differ;
  - minimum and maximum dwell;
  - typed prerequisites for closes and holds.

  How edits land:
  - A pending revision replaces the pending item.
  - A material correction to an aired item airs as an explicit correction.
  - A cosmetic edit keeps its dwell.

  Commit acknowledges first presentation, repeat and cooldown use, and a lane's start. On a failed
  frame or a reconnect, the last known presentation is kept and the current image re-sent.
