# Clock and scheduling: toward one clock and one "tower"

Status: **accepted in external review round 4** (2026-11-15). A1a is built as the tower's first slice
(see "Steps"), and the head's rulings below amend the target. All four of its steps are built
(2026-11-16):
1. the two-reading clock, decisions apart from frames, fetches and frame writes off the frame path, and
   basic traces;
2. MLB's fetches from a typed `PollPlan`;
3. the scheduling primitives (`omni/tower`: segments and the lane), with the football play strip as
   their first user, checked on the PIT@CIN replay;
4. the board's card rotation on the same primitives, checked on an MLB slate replay.

"Today" describes the result. This doc is the target that new timing code should move toward, so that
we don't add more one-off timers in the meantime.

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
| `omni/app/runner.py` `run_forever`, `AppLoop.refresh` and `AppLoop.paint` (MLB) | refreshes (pipeline into the card queue, the source's health) on a worker thread, reading the plan's answers, each published as one numbered view; painting what the board has up on the main thread, choosing from the latest complete view and booking at the clock reading taken after the display commits the frame | a refresh when an answer lands, at the pipeline's or the source's next deadline, and at least every 5 s; frames at `--fps`, 10 by default |
| `PollPlan` + `Poller` + `PlannedMlb` (MLB) | what to ask when: slates for the previous and current local dates (each date's games kept on their own until final and shown), each live game's feed (current for two cadences after its receipt), pregame details and odds, and, with `--meter`, market and model probabilities; one ask in flight per key, its lease (`answer_within`, 30 s) started when a worker begins it and checked again before the request, with ask numbers never reused; priority cadence within a per-source quota; three quick retries, last good until `keep_for` | the plan on monotonic time; a poller thread checking every 250 ms, with workers per source, so blocked feeds can't starve the slate |
| `ProviderSupervisor` | last-known-good and stale status for the slate (its backoff is off for a planned slate) | `now` passed per call |
| `LiveBaseballPipeline` (`DelayPolicy`, `DelayedFeed`, `DelayedEventStream`) | TV delay anchored to when a play happened, pinned at admission on the monotonic clock (`ReleaseGuard`: receipt + the full delay when the occurrence isn't credible or the wall clock just jumped); meter readings, admitted only up to the visible state's receipt; a final only from the feed's own final, then its embargo; a live card stale two cadences after its last good answer, or at the first failed read | a `ClockReading` per refresh; releases on monotonic time |
| `InterleavedCardQueue` | which card should air next (`peek`): eligibility, required order (finals by when their games ended, else by first confirmation, traced; a final after its walk-off; a predecessor that can no longer fit its minimum doesn't block), a usable window, then a takeover, a due reminder, or two-level rotation; `book` records a turn at commit | UTC for availability and expiry; reminder cooldowns on monotonic time |
| The board, a `BoardSession` (`omni/tower/board.py`) | one card at a time: dwell from the first committed frame, preemption after a minimum, a turn (`--slot`, 12 s) within the card's min and max, a cap on takeovers back to back, then a reserved rotation turn a reminder can't cut, a lone takeover or spent reminder leaving at its turn, a cue booked only while the published view still holds the card as cued, explicit idle, held and offline frames, stale cards dimmed; a card the display can't draw set aside for a minute or until it changes | the frame thread; dwell on monotonic time; `next_deadline` |
| `FootballBoard.step` (single-game NFL) | a `--delay` buffer released at monotonic receipt + the delay; meter lag, with the meter aged at every draw; closing out a period before a break within `CLOSING_WAIT` (30 s: a decisive caption keeps its minimum, routine backlog that can't fit is dropped and traced); a dead feed (40 s past due, outside a halftime hold) dimmed, its strip cleared; strip time credited when the display acknowledges the frame | ~10 fps frame loop on `ClockReading`s, taking whatever answers have arrived; publishes `next_deadline` |
| `GameFeed` in a `BackgroundFeed` (`football_feed.py`) | poll cadence, retry after failure, a terminal read on the final drain's boundary (a gap traced if it's incomplete) before parking, re-reads for 30 s while a matched play is missing; answers dated by arrival on both clocks | its own thread, checking every 250 ms whether an ask is due |
| `BackgroundFrameWriter` (`frame_stream.py`) | writing id-stamped frames (`OMN2`) to the display process, which acknowledges each after its swap; a new process is a new generation, and a frame counts as shown only when its generation acknowledges it (a display process that never acknowledges is credited after 5 s, logged as unconfirmed); a failed spawn retries | its own thread, plus a reader for acknowledgements; only the newest frame waits |
| The play strip, a `Lane` of segments (`omni/tower`) | frames (10 s min, 20 s max each), stoppage holds, invalidation on a field change, close-out; availability, latest start, expiry, edits by revision. On the NFL strip: a latest start of 60 s routine and 120 s major, a late routine play (written over 2 min before it arrived) dropped, at most 3 routine entries waiting, and the strip cleared after a wall-clock jump of over 30 s | dwell on monotonic time from each frame's first commit; `next_deadline` |
| `NflMetaLiveProvider` | halftime hold (13 minutes by default, `--halftime-hold`; plus its grace, after a short drain when the half's last read failed), correction watch, score backstop, injury news window | private datetimes on per-game state |
| Market (`PolymarketUsProvider`, the market-service client, the heartbeat thread, the cloud poller; the live meter is opt-in on both boards: `--meter` for MLB, `--odds` for NFL) | 30 s REST refresh; 10 s/60 s freshness; 30 s retry; 300 s heartbeat; 1 min cloud schedule | each its own |
| Rendering (`text_lane.py`, `LaneClock`, the halftime countdown) | marquee offsets, line start times, the colon blink | a line's start on monotonic time, by presentation (a card's time on the board, a strip frame) and lane, kept once its frame is committed; the countdown and its blink off `RenderContext.now` |
| Launchers | time itself | `SystemClock` in both, read two ways (`ClockReading`: UTC and monotonic); loops paced by `Pacer` on monotonic time |

### What that adds up to

1. **Three cadences are conflated.** Data arrives every few seconds to minutes, per source. Decisions
   should happen whenever something changes. Frames need 10+ fps.
   - The MLB app does all three on one 12-second tick. It therefore draws a frame only every 12
     seconds, so a scrolling line is sampled far below its motion and can jump backward between
     frames. That is the "baseball jerk", and countdowns and blinks can't animate there either.
   - Football separates frames but still polls on the frame thread, so a slow request freezes the
     board.
   - A1a step 1 (2026-11-16) split these: MLB decides on its own thread and paints at a frame rate,
     and football's feed and frame writes run on threads of their own. Since step 4, MLB refreshes
     when an answer lands or a deadline passes, and the board's choices run at frame rate.
2. **Timers are scattered.** A dozen objects each keep their own deadline. Nothing can answer "what
   happens next, and when?", which makes end-to-end replay and tuning by taste harder than they
   should be.
3. **There are three notions of delay.** TV delay is anchored to when a play happened (MLB). Football
   anchors its delay to when we received the play. The win meter applies its own relative lag. These
   should be one rule: estimate when a thing happened, then release it at that moment plus the
   viewer's delay.
4. **The same scheduling problem is solved twice.** The card queue picks which card comes next; the
   football strip picks which line comes next. Both need slots with minimum and maximum dwell,
   priority, freshness and preemption. Since A1a steps 3 and 4 both run on the tower's primitives: the
   strip on a `Lane`, the board's cards on a `BoardSession`.

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
- `InterleavedCardQueue`'s rotation, BURST takeovers and RECURRING cooldowns became the tower's
  selection policy for the board surface, with dwell enforced there (A1a step 4).
- `PlayStrip`'s rules (frames, stoppage holds, clearing on a field change, close-out) became the
  lane's (A1a step 3). The same machinery should drive baseball's Due Up and at-bat lines and any
  future ticker.
- Provider-side timers (halftime hold, correction watch, score backstop) and supervisor backoff
  become poll plans registered in the timer heap.
- `LaneClock` stays render-side as pure presentation. Since A1a step 4 it is keyed by presentation
  (a card occurrence, a strip frame) and lane, not by lane geometry alone.

## Steps, smallest first (each useful on its own)

Round 4 groups the first five as A1a:
- step 1 and its traces (1 and 8, basic);
- step 2 with the typed `PollPlan` (2 and 3);
- step 3, PlayStrip first, compared on decisions from the PIT@CIN replay rather than frame for frame
  (4);
- step 4, the board's rotation (5).

1. **Split frames from ticks in the MLB app.** Draw the current card at a frame rate, and run
   `run_once` on its own cadence. This fixes the baseball jerk and makes countdowns and blinks live.
   Small. *Done in A1a step 1.*
2. **Take provider fetches off the frame thread**, in both football and MLB. A worker posts
   observations and drawing never blocks. Small to medium. *A1a step 1 moved them off the frame
   paths. In A1a step 2, MLB's fetches left the decision pass too: the `PollPlan` says what is due,
   the poller's workers ask it, and decisions read the answers.*
3. **One clock seam everywhere.** The football launcher uses `SystemClock`, and monotonic time is
   used only to pace sleeps. Small. *Done in A1a steps 1 and 2: MLB states are now dated by receipt.*
4. **Build the scheduling primitives** (segment, surface/lane, timer heap, tower interface) with
   `PlayStrip` as the first consumer. Behavior stays identical, proven by replaying the recorded
   PIT@CIN game frame for frame. Medium. *Done in A1a step 3, checked on decisions rather than frames
   (round 4):*
   - `omni/tower/segment.py` holds the presentation contract, and `omni/tower/lane.py` one surface's
     schedule with its `next_deadline`. The board publishes its own `next_deadline`: a strip frame's
     time, a reading clearing the delay, a break's wait.
   - `omni/replay/football.py` replays a football trace through the board and lists its decisions.
     `tools/nflmeta_replay_trace.py` builds the PIT@CIN trace from the recording, minimized: no
     NFLMeta text or ids, with stand-ins for the snap and the clock.
   - Before the port, the board made 564 decisions on that trace. The ported board makes the same 564.
   - Stepped only at arrivals and deadlines, the board decides as it does stepped every 10 ms, which
     shows its deadlines miss nothing.
5. **Port the board's card queue onto the tower** and enforce minimum and maximum display there.
   Medium. *Done in A1a step 4:*
   - **The board** (`omni/tower/board.py`, a `BoardSession`) airs one card at a time as a segment on
     `Surface.BOARD`. The queue says what should air (`peek`) and records it only when the card's
     first frame is committed (`book`). So a turn, a reminder's cooldown and a takeover are spent only
     on airtime the panel showed (round-4 H4).
   - **Dwell:** a card gets its minimum from its first commit. After that it yields to a stronger claim
     (a takeover over a reminder, a reminder over rotation); after its turn (`--slot`, 12 s by default,
     held within its own min and max) to any other card; and at once if it leaves the queue, expires,
     or its source goes offline. A card with nothing else waiting stays up. (Since the A1a closeout, only a rotation card does: a lone takeover or spent reminder leaves at its turn.)
   - **Takeovers:** a BURST card takes over once, the first time it airs. After 30 s of takeovers back
     to back, a rotation card gets a full turn before the next one. (Since the A1a closeout, that turn is reserved: a due reminder can't cut it.)
   - **Order:** finals that haven't aired go in the order their games ended, and a final airs after its
     game's walk-off (`ScoreboardCard.after`); its reveal waits for the game's plays still in the delay.
   - **Explicit frames:** *idle* (nothing to show), *held* (a game's card is still inside the TV delay)
     and *offline* (the schedule silent for over 10 minutes, or a game's feed down and its card gone),
     each traced with its reason. Each is the board clock on every profile: alone when idle, over a
     red `OFFLINE`, or over an amber `Syncing...` with a bar that fills toward when the first held card
     clears the delay (the user's design, 2026-11-16; on the single, a 2-pixel bar along the bottom
     edge). A card whose data is past its age is drawn dimmed: a live card whose feed has stopped
     answering, which comes down 2 minutes later, or any card while the schedule is over 2 minutes old.
   - **When:** a refresh runs when a poll answer lands (the poller wakes it), at the pipeline's next
     release or the source's next change in health, and at least every 5 s. The board's choice runs on
     the frame thread. `AppLoop.next_deadline` publishes both.
   - **The marquee epoch at commit (R4-6):** a line's start is kept on the monotonic clock by
     presentation (the board's card occurrence, or the strip frame) and lane, and stands only once its
     frame is committed. A new presentation starts its lines at their heads; there is no 5 s forgetting.
   - **Import order:** the clock moved to `omni/core/clock.py`, with every import updated and no
     re-export left behind.
   - **Checked on an MLB slate replay** (`tests/mlb_slate.py`): six games over 55 minutes behind a 45 s
     delay, with back-to-back home runs, four big plays within 20 s, a walk-off, a no-hitter, a rain
     delay and a 12-minute outage. The harness now steps at the timeline's changes and the loop's
     deadlines. The baseline from the old loop was recorded first (commit 1 of step 4), and the
     decisions changed as intended:
     - each card's turn is timed from its own commit, not the 12 s decision tick, and a takeover or
       reminder cuts in at the current card's minimum rather than at the next tick;
     - the CHC triple, fourth in a burst of big plays, kept its takeover (the old loop lost it when
       its 8 s window ran out), after a rotation turn once three home runs had run past the cap;
     - through the outage the old loop rotated frozen cards for 12 minutes; now live cards dim at
       once, come down after 2 minutes, finals dim once the schedule is stale, and the offline frame
       goes up at 10 minutes, until the source answers again.
   - A fixed mixed MLB/NFL fixture through the same scheduler is round 5's question 4.
6. **Build the air clock and per-source lag models.** Fold football's delay and the meter lag into
   it, with lags measured from recordings (e.g., market jumps against NFLMeta's play times). Medium.
7. **The value squeeze:** freshness decay by segment kind, preemption classes, and fairness across
   leagues. Large; needs M6-004's multi-league plumbing.
8. **A plan inspector (a "departures board")**: a log or local view of the next N scheduled segments
   and why each is there, for tuning by taste and for debugging. Small and high-leverage. It can come
   early. *A1a step 1 added basic traces (`omni/app/trace.py`): each decision that changes the pick,
   with its reason and the next decision, and each card's first committed frame with how its teams
   were drawn.*

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

  As built in A1a step 3 (`omni/tower`), for a lane:
  - A segment carries all of the above.
  - A lane cues one frame at a time; the display's commit starts that frame's dwell, so a frame
    that never drew spends none of it.
  - A newer revision replaces a waiting segment in its place, or swaps the frames of the one on air
    and keeps its dwell; an edit to one already gone doesn't bring it back.
  - Latest start and expiry drop a segment unseen, with the reason traced.
  - A football frame counts as committed only when the display process acknowledges it, by frame id and
    sink generation (since the A1a closeout); proof that it lit the LEDs stays with A1b.

  As built in A1a step 4 (`omni/tower/board.py`), for the board:
  - A card airs as a segment: its dedupe key, its revision as refreshes replace it, its game as the
    parent, its display timing as availability, expiry and dwell.
  - Repeat, rotation and cooldown use are booked at the first commit, and a line's marquee starts at
    its frame's commit.
  - A refreshed card on air swaps in place and keeps its dwell; one that leaves the queue comes down at
    once, as a factual invalidation.

## After A1a: the rulings still open (self-review for round 5, 2026-11-16)

**Round 5 (2026-11-16)** signed A1a off conditionally. The four gates C1–C4, its sequence, and how it
ruled on the table below are in STATUS "Next" and `~/omni-review/plans/omni-build-fidelity-REVIEWS.md`.
- **Latest start:** step 7, before shared NFL rotation.
- **Inline fetches, and the release guard against clock jumps:** both move into C3.
- **The football strip's semantics:** they land with NFL on the shared board (blessed).
- **Lane identity:** semantic lane ids within a presentation come with shared NFL.

**The A1a closeout (C1–C4, PR #109, 2026-11-16)** built the four gates, each tested at its production
entry point:
- **C1, the frame actually shown:** MLB books a card at the clock reading taken after the display commits
  its frame, so a slow swap moves the dwell, the booking and the marquee epoch with it. The NFL display
  process acknowledges each frame it swaps (`ACK <id>`, wire `OMN2`), a restarted process is a new
  generation, and the strip credits only acknowledged frames. A short stall keeps the caption on air; a
  lost generation presents it again, with a full first dwell.
- **C2, one board input per refresh:** a refresh stages its card changes and health and publishes them
  as one numbered view; a cue superseded while it rendered doesn't book. After 30 s of takeovers the
  rotation turn is reserved, a lone takeover leaves at its turn, a game's own final hands off from its
  live card at the live card's minimum, and an unusable predecessor no longer blocks a final (traced as
  too late, expired or failed to draw).
- **C3, the production MLB path:** poll leases start when a worker begins the ask, each source has its own
  workers, and a given-up ask makes no request. Each date's slate keeps its games until they're final
  and shown. Pregame details and odds are plan sources, so no decision fetches. The meter is opt-in
  (`--meter`) and admits a reading only if it arrived by the visible state's receipt. A final comes only
  from the feed's own final; while the schedule says final and the feed doesn't, the card keeps
  updating, and after `CONFIRM_WITHIN` (30 s) it dims and comes down 2 minutes later, with no score
  invented. A later different final corrects the card in place. A live card is stale two cadences after
  its last good answer, or at the first failed read. Release is pinned at admission on the monotonic
  clock (`ReleaseGuard` in `omni/queue/delay_policy.py`), with receipt + the full delay for an implausible
  source time and for 2 minutes after a wall-clock jump.
- **C4, the NFL closeout:** the final drain reads once on its 2-minute boundary and traces a gap if that
  read is incomplete. A period's close is bounded (`CLOSING_WAIT`, 30 s) apart from a decisive
  caption's minimum; routine backlog that can't fit is dropped and traced, and an undrawable caption is
  dropped (`failed to draw`). Closing finishes while the display is down. A dead feed (40 s past due,
  outside a halftime hold) dims the card, clears the strip and hides the meter; the meter is aged at
  every draw; a matched play missing from a complete page is re-read for 30 s; and the halftime hold is
  13 minutes by default (the user accepted the recommendation, 2026-11-16).

Bounds picked during the closeout:
- **Ratified by the user (2026-11-16):** a feed answer is current for two cadences; the final's
  confirmation bound (30 s); the NFL dead-feed bound (40 s past due) and mismatch re-reads (30 s).
- **Kept by round 6 as sanity bounds:** the release guard's plausibility bounds (a source time up to 5 s
  ahead of receipt or 2 minutes behind it) and its 2-minute settle after a jump.

**Round 6 (2026-11-16)** closed C1 and C2 and held C3 and C4 open on four narrow fixes. Each was reproduced
by a probe at its production entry point. They were built as D1–D4 in one PR:
- **D1, a wrong host clock can't release a play early:**
  - Before, a host clock running ahead by under 2 minutes shortened MLB's delay by that much, so a
    big-play card could air at receipt.
  - Now a play's age is told on the source's own clock: the answer's `as_of` less the play's end time.
  - Release is receipt + the delay less that age, never before receipt. Our wall clock only gates whether
    the age is believed.
  - A play with no end time waits the full delay from receipt (its at-bat's start dated it too early).
- **D2, a missing count is unknown, not zero:**
  - A linescore missing a side's runs or hits once that side has batted, or either side's runs in an
    answer the feed calls final, is a failed read, so the last good answer stands. A partial final had
    shown FINAL 0-0.
  - A perfect game needs walks, hit-by-pitch and errors present.
- **D3, the NFL final drain gets its reads:**
  - While a final drains, the feed asks at the live pace (10 s at most), never past the drain's end.
    NFLMeta's own recommended interval runs to about 15 minutes once no game is live.
  - A last read that leaves plays missing is noted.
  - A play the drain brings after FINAL is up airs on FINAL's strip, and a changed score revises FINAL at
    its own release.
  - The feed's due times, the halftime hold, the drain and the provider's other spans run on the
    monotonic clock.
- **D4, the strip's bounds:**
  - Each caption has a hard end, 90 s after release for routine and 150 s for major (the latest start plus
    a 30 s finishing allowance; it must still start by its latest start).
  - A caption cued but never shown is dropped at its latest start or when the field moves on. A stoppage
    caption that is on air and still current stays until play resumes.
  - The lane pins a strip entry's bounds to the monotonic clock when it's offered.
  - A major written more than 2 minutes before it arrived is dropped as late, like a routine one.
  - After a wake, the card dims and the meter hides until an answer received after the jump is released.

Still open from round 6:
- ~~MLB membership ordered by UTC freezes after a backward step~~: done in H5 (scopes ordered by monotonic receipt);
- a receive watchdog on the Pi (A1b);
- clock trust attested by NTP (later).

An audit of A1a against the round-4 rulings found four defects, fixed before round 5:
- **A card the display can't draw** held the board: it was cued again every frame and nothing else
  aired. Now it comes down (`drop … (failed to draw)`) and is set aside until it changes or a minute
  passes. A display that fails outright still spends nothing.
- **A request that never returns** held its poll key forever (StatsAPI sets no timeout). Now an ask is
  given up on after its source's `answer_within` (30 s), and whatever it brings back late is ignored.
  StatsAPI's requests also get a 15 s timeout.
- **The football board spent strip time on frames nobody saw**: a failed or stalled write to the
  display process still counted. Now a frame counts only while the display is taking frames.
- **A football draw that raised stopped the app.** Now it's logged, and the strip waits.

Fixed with them:
- the stack and single no longer "show" strip entries their card doesn't draw;
- timeouts and challenges stay unknown until the play feed has been read through;
- a card that expires before it ever airs is traced (`unseen`);
- a rotation card cut short no longer restarts the takeover run.

Still open, with where each lands:

| Ruling (round 4) | Today | Lands with |
|---|---|---|
| Latest start apart from hard end (about 60 s routine, 120 s major, from eligibility) | done for the NFL strip (PR #111): 60 s routine, 120 s major, from availability, plus late-play drops and a burst cap of 3. No board card sets one: a big play can start up to about 114 s after release (its expiry is 120 s) | step 7 (freshness), for board cards |
| Airtime debt across leagues | one league's weighted rotation | step 7, after the mixed-league identity work (M6-004) |
| Clamp release against a wall-clock jump | done for releases (C3, C4). A queued card's availability and expiry still read UTC, so a backward jump lengthens its window; nothing releases early | step 6 (the air clock) |
| A final from an authoritative result besides the feed | only the feed's own final is shown; the schedule's score isn't carried on `TeamGame` | H5, with the typed observations |
| A DELAYED or SUSPENDED game from an earlier date whose slate keeps failing | its status card stays up until the slate answers (it reveals no score). Since H5 that date's scope goes stale on its own clock, but the board doesn't dim by scope yet | NFL onto the shared board (per-league health on the board) |
| The MLB meter aged at paint | its age is bounded by the state releases and by a stale card's 2.5 minutes; it is opt-in | NFL onto the shared board |
| Device contrast from config (round-4 L2) | the default policy everywhere | A1b, with the hardware |
| Football: revisions replace what's pending; corrections and retractions explicit (round-4 M2) | the lane supports revisions, but `FootballEvent` carries none. Only review and conversion corrections are announced | NFL onto the shared board |
| Football: field and caption move together; an invalid caption clears at once | the caption keeps its 10 s minimum while the field moves | NFL onto the shared board |
| Football: a stoppage ends on credible resumption or a traced stale-source timeout; a dead feed is shown as such | a dead feed is shown, and its timeout ends a held stoppage, traced (C4); any new entry still ends a hold | NFL onto the shared board |
| A stalled display link recovers in bounded time (R4-5) | frames count only when acknowledged, by generation (C1), but nothing restarts a stalled link | A1b (the app on the Pi) |

The takeover cap bounds back-to-back takeovers at the cap plus one takeover's turn ("after 30 s of
takeovers, a rotation card gets a full turn": the default the user ratified, 2026-11-16).

