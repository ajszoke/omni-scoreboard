# Board policy: which card the shared board shows, and when

**Version:** `omni-board-policy/2`.
- **Ratified by the user** 2026-11-17, from the design in review round 7. The favorite floor scales with
  leverage (§3.4); the rest is as designed.
- **Built** with NFL on the shared board (2026-11-17), on H5's types: `ARCHITECTURE_TYPED_DOMAIN.md`, "The
  board policy". The app decides with it under `--board-policy 2`. The default stays `--board-policy 1`, the scheduler's
  old bands (`omni/queue/scheduler.py`), until v2's live path passes its gates (`STATUS.md`), and the NFL
  single-game runner keeps its own focus as the rollback.
- **Not yet built:**
  - **leverage models:** every league runs the labeled proxy (§3.1), so no action interrupt can qualify
    from leverage; only verified feats do (§4);
  - **records:** MLB's come from the pregame details, frozen at the start; NFL's are unknown (§10);
  - **break signals the feeds lack:** NFL TV timeouts, injury timeouts and reviews in progress; MLB
    equipment and umpire stoppages. A break that can't be detected is never lent;
  - **NBA** on the shared board.
- **Readings settled after review round 8 (2026-11-18)**, built in the closeout: leases anchored at the pinned
  release less the age at detection, and a guest's grant checked again at its first frame (§5); a new
  independent interval supersedes a shorter lease (§5); a promotion names a card fixed for its event's revision,
  and a withdrawn offer never airs (§4); an unseen decisive event leads its final as a result summary (§4);
  stale games out of the targets (§3.2); final repeats and the non-base cap on confirmed time (§6); and the
  wake and output-loss contract (§6).
- Every constant here is a policy default. Changing one bumps the version; it isn't a user setting unless
  §8 says so.

**The user's intent, in brief:**
- A favorite gets a large share of the board while it's in action. That share shrinks when its game
  matters less, as in a blowout.
- Ranked favorites are a proportional factor, not an override.
- Big moments elsewhere cut in at most once a minute, and only when they matter enough. They prefer the
  favorite's micro-breaks, and wait up to 24 s for one.
- A close, late game gets about 2.5x a blowout's airtime.

## 1. Inputs: released state only

- Every decision reads one atomic **released** generation: the delayed state, never a raw receipt. That
  covers:
  - contest, league and provider identity;
  - phase, state, revision and quality;
  - break evidence;
  - the model result;
  - schedule and stakes metadata;
  - the usable display window.
- A raw receipt may schedule fetches and embargoes. It never moves ranking, favorites, records, alerts or
  the "why" text early. Today's unreleased final must not leak through a record, a standings rank or a
  playoff implication.
- Each board keeps its own per-league viewer delay.
- Unknowns never become a tie, empty bases, zero timeouts or a final.

## 2. Leverage

**Leverage is the chance for the next outcome to change who wins.** It isn't what just happened, nor how
much this game matters to the season. Per league and model version:

```text
W(s)  = P(home win) (or named win equity where ties are possible)
A(s)  = E[ |W(next state) - W(s)| ]           expected absolute swing, home perspective
LI(s) = A(s) / mean A over the model's reference population
```

- **Signed** change cancels out, so it's not used.
- **A terminal state** has no leverage. Its decisive event keeps its released predecessor's LI.

**Models.** All are built offline, versioned, and validated before they gain permissions. None is trained
on-device or downloaded automatically. Each needs a model registry entry.

| League | Win-probability base | Leverage | Until it passes validation |
|---|---|---|---|
| MLB | an original WP table from Retrosheet transitions: fit 2023–24, hold out 2025; rule modes for the automatic runner and postseason extras | a WP/LI lookup table, about 0.45 MiB | `PROXY` |
| NFL | the pinned nflfastR non-spread WP (MIT) | a fitted `\|ΔWP\|` estimator on nflverse history (CC BY 4.0; **no FTN columns**, which are share-alike). **Never trained on NFLMeta** (its terms bar it) | `PROXY`; no action interrupts from it |
| NBA | an original WP table fitted on the BALLDONTLIE sampled-state archive (its terms allow it) | a 24 s game-clock horizon, no possession (Free has none) | `PROXY`. No displayed WP; close-game snapshots only (§6) |
| NHL | later: a goal and manpower model | a 30 s horizon | n/a |

**Attribution:**
- Retrosheet's notice ships with the MLB asset and in About.
- Tango's tables are a sanity reference only: his terms bar revenue.
- Every estimate carries `model_id`, its ruleset, its feature coverage, and `VALIDATED`, `REDUCED`, `PROXY`
  or `UNKNOWN`.

**Validation gates:**
- calibration no worse than the simple baseline;
- expected-swing decile checks;
- held-out mean LI between 0.9 and 1.1;
- ranking stable under duplicate polls;
- no embargo leak;
- a measured Pi budget: MLB p99 < 1 ms, NFL p95 < 5 ms per slate update, NBA < 1 ms a game.

## 3. Allocation

### 3.1 The game's leverage weight `G`

```text
z = clamp((LI - 0.5) / 2.5, 0, 1)          # LI <= 0.5 -> 0, LI >= 3 -> 1
G = 0.75*z + 0.25*R                         # R: EWMA of z, 90 s half-life of active time
R_new = 2^(-dt/90)*R_old + (1 - 2^(-dt/90))*z_new
```

**`R` counts only fresh action time:**
- a single uncertain interval is capped at 30 s;
- `R` is frozen in confirmed breaks and on duplicate polls;
- it's reset from the current estimate after an outage.

**A reduced model** contributes at most `G = 0.2` (LI = 1), unless its missingness mode has passed
calibration.

**The proxy, while no model has passed.** It's labeled `PROXY` and is never called LI or WP:

```text
P = completed regulation fraction (<= 1),  r = remaining fraction,  d = |score difference|
C = 1 / (1 + (d/M)^2)
G_proxy = clamp(C * (0.30 + 0.70*P^2), 0, 1)
M_MLB = 1 + 2r      M_NFL = 3 + 11r      M_NBA = 3 + 15*sqrt(r)
```

- **MLB:** progress is completed scheduled outs, counting each half transition once. Extras set `r = 0`.
- **NFL:** 3,600 regulation seconds. **NBA:** 2,880.
- **Overtime:** `P = 1`, and the margin scale uses the remaining OT seconds over regulation.
- **Without score, time or progress:** `G = 0.2`, and nothing model-derived.

### 3.2 Favorites, rank and stakes

**The live pool:** fresh, released, unmuted games with a drawable card, each contest once.
- A favorite in a confirmed break leaves the pool while alternatives exist.
- A suspension or delay is a status card, not a weighted game.
- A stale favorite loses its boost at once. A stale game is out of the weighted targets entirely: it earns no
  base credit, wait or floor, and its dim last-safe card airs only as a fallback when nothing fresh is usable.

**Favorite rank.**
- **Defaults:** MLB `[ATL, COL]`, NFL `[CAR]`, NBA `[CHA]`.
- When two favorites meet, the better rank counts. A mute always wins.

```text
F = 1 + 3/rank   for a favorite in action  (rank 1: 4, rank 2: 2.5);  F = 1 otherwise
```

**Matchup quality `Q`.**
- Each team's shrunk win fraction is `(W + 0.5*T + 4) / (games + 8)`, from complete, permitted records.
- `Q = clamp((mean of the pair - 0.25) / 0.50, 0, 1)`.
- **Unknown records:** `Q = 0.5` for rotation, but `Q = 0` for an interrupt.
- Records freeze at the game's start.

**Stakes:**
- **`J`:** 1 for a confirmed postseason game, 0.5 for an NBA Cup knockout, else 0.
- **`C_season`:** 1 only for a verified clinch or elimination game, else 0. Never inferred from a record,
  games back or the week.
- **`N`:** 1 only for verified US national TV, else 0. Today only MLB has it (`schedule?hydrate=broadcasts`).

```text
T = 1 + 0.20*(Q - 0.5) + 0.15*J + 0.15*C_season + 0.03*N      # 0.90 .. 1.43
b = (1 + 1.50*G) * T * F                                        # a game's base weight
```

v2 has no rivalry, comeback, market or story bonuses.

### 3.3 League and game shares

Season length gives a small boost to short seasons:
- `s_l = (162/N_l)^0.25`, with `N_l` = MLB 162, NBA 82, NFL 17;
- user league weight `u_l`: default 1, allowed 0.5–2.0.

```text
mass_l = u_l * s_l * sum(b in league l)
q_l    = 0.80 * mass_l / sum(mass) + 0.20 / L      # L = leagues with eligible games
q_g    = q_l * b_g / sum(b in l)                    # no within-league blend: 2.5:1 stays exact
```

### 3.4 The favorite floor: ratified with leverage scaling

When favorites are in action, their **combined** share has a floor that rises with the leverage of the most
important favorite game:

```text
floor_F = 0.30 + 0.40 * max(G of the acting favorites)        # 30% in a blowout .. 70% close and late
if Q_F = sum(q_g of acting favorites) < floor_F:
    favorite q'_g    = floor_F     * q_g / Q_F
    nonfavorite q'_g = (1-floor_F) * q_g / (1 - Q_F)
```

- Recompute `Q_l = sum(q'_g in l)` and `p_g = q'_g / Q_l`. Don't reapply the league blend.
- **No projection** is needed when every candidate is a favorite.
- **A favorite's confirmed break** suspends its membership in the floor.
- **On a full slate** (ATL plus 14 other games, or CAR plus 8 others), an acting favorite gets about 70% when
  its game is close and late, 46% when typical, and 30% in a blowout.
- **With few games on,** the multiplier usually gives it more than the floor anyway.
- The floor applies to eligible base airtime, not sleep, unavailable output or auxiliary cards.

### 3.5 Turns, debt and rescue

**Accounting** runs per league, then per game, on **confirmed** visible base airtime. A matching sink receipt
counts; a cue or a write doesn't.
- Each interval `dt` adds `Q_l*dt` to each eligible league's debt, and `p_g*dt` within the shown league.
- What was actually shown is subtracted.
- Nothing accrues during alerts, results, auxiliary cards, unavailable display, or source outages.

**Selection:**
- For a 12 s slot, pick the largest `debt + 12*target`: league first, then game.
- **Ties:** the longest eligible wait since service, then the stable typed ID. Debts are rounded to
  microseconds for comparison.
- Each debt is clamped to ±24 s, and every clamp is traced.
- A shortened turn credits only its actual time.
- The same game winning twice keeps its frame, with no flicker.

**Rescue.** An item is overdue when one of these holds:
- a league has `misses >= max(6, L, ceil(2/Q_l))`;
- a game has `misses >= max(n_l, ceil(2/p_g))`.

Then:
- The oldest overdue item (by eligible wait) goes first.
- Only completed full base turns while the item was eligible count as misses.
- A confirmed service resets the count; an unacknowledged cue doesn't.
- A confirmed break freezes debt and age, and clears the miss streak.
- A final, a mute, an expiry or a service loss retires an entry; returning from an outage starts it at zero.

**After a break, a favorite gets its weight back, not ownership.**
- A still-valid background live turn may finish its 12 s.
- A card admitted only because of the break yields after its minimum.
- A reserved base recovery finishes its full turn.
- Then selection runs again.
- A correction, a hard expiry, a mute or a device blank clears at once.

## 4. Interrupts

A promoted event's value uses its **released predecessor's** leverage, never the terminal state's:

```text
I = B * (0.5 + 0.5*z_pre) * (0.65 + 0.35*Q + 0.35*J + 0.25*C_season)      # Q = 0 when records are unknown
```

| Event | `B` | Evidence required |
|---|---:|---|
| Confirmed perfect game | 3.5 | a complete outcome and a complete reach ledger; never inferred from partial stats |
| Confirmed no-hitter | 2.8 | a complete final with verified hit totals |
| A decisive late winner: an eligible walk-off, or a result-changing reversal | 1.2 | a typed event with a coherent predecessor and successor. NFL: Q4 ≤ 5:00 or OT. MLB: the 9th or extras |
| A win swing (the user's rule, 2026-11-22): a play that moved either side's win probability by 10 points or more, at any time. It takes `z_pre = 1`, because the swing is the evidence of what was at stake, and keeps its predecessor's leverage basis, so on NFL's proxy it waits for a break | 1.2 | the meter's readings around its answer: as of `METER_LAG` before the predecessor's arrival and before its own. One play per answer gets the swing: its first score, turnover or ruling, else its last play. A decisive late winner keeps its own class. NFL only, for now |
| Another major competitive event (a close-game HR, a late one-score turnover) | 0.8 | typed and coherent |
| Routine scoring, bookkeeping, an injury notice | 0 | the normal field, strip and status |

**Leverage evidence.**
- A `PROXY`, `REDUCED` or `UNKNOWN` predecessor can't qualify an event for an **action** interrupt.
- For break-only scoring, a coherent proxy predecessor uses `G_proxy` in place of `z_pre`.
- A verified rare feat uses `z_pre = 0`.

**The bars**, over all the healthy live favorites:

| Context | Required `I` |
|---|---:|
| Any favorite in action | **1.10** |
| None in action, one in a valid micro-break, the rest in breaks | **0.85** |
| All in ordinary breaks, or no live favorite | **0.35** |

One favorite's timeout doesn't lower the bar over another favorite's live drive.

**What the bars mean in the regular season:**
- **During favorite action:** only a feat (a perfect game 1.14, a no-hitter 1.16 at average `Q`) or a
  decisive late winner between two ~.630 teams clears the bar.
- **An average walk-off** scores 0.99 and waits for a micro-break.
- **A last-second NBA winner between the two worst teams** scores 0.78 and waits for a real break. On Free
  the board can only say "narrow final", never "buzzer beater".

**Cooldown:**
- **One promoted card per 60 s, board-wide**, counted from its confirmed start. It covers events, snapshots
  and reminders, the favorite's own included.
- Ordinary live turns, ordered finals and in-place strip updates don't spend it.
- An unshown cue doesn't consume it.
- It survives a config reload.

**Durations:**
- Promoted majors run 12 s (minimum 8); snapshots 8 s (minimum 6).
- Dedupe by contest, event and revision, and merge a scoring summary with its play.
- No alerts from a first baseline or outage catch-up.

**The 24 s hold.** One monotonic episode starts when the first usable outside event needs a better moment.
Its deadline is `first eligible + 24 s`, and later candidates don't extend it.
- **An ideal moment** is a verified break whose remaining lease fits the whole card plus a 6 s return
  margin, or open rotation with no live favorite.
- **An ideal card** is coherent, new, drawable on the profile, above the bar, compatible with the released
  story, outside the cooldown, and with no unresolved decisive predecessor.
- **Choosing:** the highest `I`, then the earliest expiry, then the oldest, then the typed ID. A better
  candidate can replace the choice.
- **At expiry:** an action-qualified candidate starts at the next legal boundary if its lifetime still fits.
  Below the action bar, it stays break-only until its own expiry, then goes to the results queue: a decisive
  event or confirmed feat that expires unseen becomes a result summary (its still with the final's score) that
  leads its own final in end order, spends no cooldown, and is fresh until the earlier of the final's expiry and
  5 minutes after the final became eligible; past that, the final airs plain and says its decisive play is gone.
- **An offer names its exact event:** a card fixed for that event's revision. A published view that no longer
  carries an offer withdraws it; a revision replaces it without a fresh clock; a story that has aired (promoted,
  in its own game's turn or as a result) is never promoted again. An unscored snapshot or reminder plays only in
  an ordinary break or open rotation.
- **The hold never forces a cut-in below the bar,** never overrides the cooldown, and never chains a
  second hold.

**Lifetimes.** A routine promoted event must start within 60 s of eligibility, a major within 120 s. Both
are gone by 120 s, so a 12 s major starts by +108 s. These are separate from the NFL strip's +90/+150 s
hard ends (`CLOCK_AND_SCHEDULING.md`).

## 5. Breaks and leases

**A typed `BreakObservation`** records:
- contest and kind;
- the onset, when credible;
- first receipt and release;
- its confidence, identity, explicit end and evidence revisions.

**What can open or end a lease:**
- Repeated rows, a completed ruling or an old substitution never open a new lease.
- Later released action revokes it.
- A frozen clock alone grants nothing.

**Leases are ceilings, not promises.** Each is anchored once on the delayed timeline, at its evidence's pinned
release: `ends = pinned release + ceiling − age at detection`, however late the decision that reads it.
- The age is the source's own `as_of − onset` plus a plausible transport; with the onset uncertain, one poll
  interval is subtracted and marked. A local provider hold (NFL's halftime) is aged on our own clock.
- Severe backfill gets no lease: more than half the ceiling gone at the decision. Stale or unsupported evidence
  is refused at once. Each refusal records the age and the guard.
- Only released action **after** the break began closes it.
- No stacking: a nested event of the same or lower precedence adds nothing; a new independent interval of
  higher precedence (a rain delay over a mound visit, an ordinary break over a micro one) supersedes the open
  lease from its own onset.
- A guest's grant names its exact leases and is checked again when its first frame is confirmed; one that no
  longer fits is discarded unseen (booked as an accidental display if its pixels were up).

**A guest needs its whole card plus a 6 s return margin** inside the remaining lease. Duplicates don't renew
a lease.
- When a provisional lease expires without resumption evidence, the favorite returns to weighted
  eligibility under the action bar. That's not a claim that play resumed.
- With two favorites, the shorter applicable opportunity limits a cut-in.

| League | Opportunity | Ceiling and use |
|---|---|---|
| MLB | inning break | `inningBreakLength` (120/145/175 s) − 15 s; **60 s** if unknown. Cancel on next-half action |
| MLB | mound visit; pitching change | **36 s, one turn.** The preferred hold moments. A change inside an inning break doesn't extend it |
| MLB | replay review in progress (`reviewDetails.inProgress`) | **48 s, up to two turns** |
| MLB | injury, equipment, umpire (active evidence only) | **30 s, one turn** |
| MLB | ABS challenge (~15 s), batter timeout, pinch hitter, pickoff, step-off, automatic runner, between pitches | none |
| MLB | rain or suspension | open rotation while the status stands |
| NFL | a positively identified TV timeout | **120 s.** No feed signal identifies one yet |
| NFL | an official timeout (reason unknown; captioned "OFFICIAL TIMEOUT") | **30 s, one turn** |
| NFL | team timeout | **30 s, one turn** |
| NFL | two-minute warning (explicit marker) | **90 s** |
| NFL | quarter break | **90 s** |
| NFL | halftime | the **13-minute polling quiet hold**, then up to 120 s of grace |
| NFL | injury timeout (active) | **30 s, one turn** |
| NFL | review in progress (no marker yet) | 36 s, one turn, once detectable. A ruling grants nothing |
| NFL | score → try → kickoff; a turnover; a snap gap | none |
| NBA | a one-team timeout decrement, coherent, that is the first or second of a regulation quarter (the TV timeouts) | **165 s from inferred onset** (twelve 12 s turns after 15 s detection and the margin). Cancel when the clock runs again. Measured: play resumed 220 s after the clock stopped (median; p5 182 s, n 150) |
| NBA | a later decrement in the quarter, or one in overtime | **60 s from inferred onset** (three 12 s turns after 15 s detection and the margin). Cancel when the clock runs again. Measured: 113 s from the decrement's receipt (median), with a tail that resumes inside 30 s |
| NBA | a decrement at a period's full clock (taken in the break) | none of its own: the break's ceiling holds |
| NBA | a drop the league's caps make: to 4 at Q4's full clock, to 2 at Q4's first stoppage at or under 3:00 | none: it isn't a timeout, and it doesn't count toward the quarter's order |
| NBA | a coach's challenge | none of its own. It begins as a decrement and gets that ceiling; a won challenge's restored count arrives 1–2 minutes later, with play resuming soon after |
| NBA | END Q1, END Q3 | **135 s** local or unknown; **195 s** verified national (the rule is 150/210 s) |
| NBA | halftime | **840 s** (the rule is 900 s) |
| NBA | before OT | **135 s.** A `:00` reading alone never declares OT or final |
| NBA | free throws, fouls, injuries, endgame inbounds | none |
| NHL | later, provisional | team timeout 30 s; TV timeout 30 s until rules and traces support more; intermission 16 min; reviews 36 s; icing and goals none |

**Tuning** comes only from residual-time traces: onset → publication → receipt → release → resumption. Never
from a median of a few gaps.

## 6. Ordinary cards and the non-base cap

**Priority at each legal decision:**
1. device controls, corrections and hard expiry;
2. an owed full base recovery;
3. an overdue base rescue;
4. an eligible promoted alert;
5. an initial ordered result, when the context permits;
6. a due highlight or auxiliary card;
7. the ordinary weighted base.

Minimums are always protected.

**The non-base cap:** after **30 s** of consecutive non-base cards, admit no more. Finish the admitted one,
then reserve a full 12 s base turn. This is independent of the 60 s cooldown. The run counts confirmed non-base
airtime only while a live base is on hand (neutral circulation with no live game counts nothing); an owed
recovery survives an outage, and a short or failed one stays owed.

| Card | Behavior |
|---|---|
| Live base | 12 s (minimum 8). Weight applies during favorite action as well as breaks. Same-game updates refresh in place |
| NFL strip in rotation (quad) | at most one compatible 10 s caption, if the turn's runway fits it; otherwise the field only |
| NFL strip, focused | the current field plus the newest compatible major. At most three waiting routines. No backlog on return. A first start by +60/+120 s, gone by +90/+150 s |
| Pregame | 8 s, from 90 min before the start. Due every 300/120/60/30 s across the ramps at 90–30/30–10/10–2/2–0 min. With live games, at most one per four base turns, in a break or open rotation |
| Initial final | one 8 s turn, in confirmed end order. An unseen decisive predecessor goes first. While a favorite acts, unrelated finals wait for a break unless promoted |
| Final repeats | at most two 8 s repeats, at ≥5 and ≥15 min after its first physical start, each only if its whole turn ends by 30 min after confirmation; one path with or without a live base |
| NBA Free highlights | a close-game snapshot (≤3 pts in the last 2 min), a new tie or lead change there, or a confirmed OT. Breaks and open rotation only, at most one per 90 s and two a game, and within the cooldown |
| Reminders | an MLB no-hit or perfect bid after six completed innings: when new, then every 60 open seconds, three at most. An NFL late one-score red-zone drive: one. Never into favorite action |
| Delay or suspension | a fresh explicit status opens rotation; a favorite's at most once per 120 open seconds |
| No live base | pregame, results and statuses at neutral weight; else the clock and the next favorite start. A confirmed empty slate is offday, not offline |
| Embargo or startup | the amber "Syncing..." frame. No speculative score, model value or early final |
| Stale or offline | dim at 40%; a stale live card goes at 2 min; a source is offline at 10 min |
| Wake or output loss | presentation state and old leases are invalid. A complete fresh reading is required, with at most one labeled recap. A host gap (suspend, stall) voids pre-gap polls, held state, captions and offers, and distrusts the clock; an output loss stops accounting at the receiver's last word and keeps cooldowns and debt (`ARCHITECTURE_TYPED_DOMAIN.md`, "Output and interruptions") |

## 7. Panels

**All three profiles make the same selection and timing decisions.**
- `single_64x32` and `stack_64x64` keep their compact layouts and declared compromises. `quad_128x64` adds
  the strip.
- An alert's value never depends on whether its text fits. Use a compact equivalent, or reject it with a
  traced reason.
- No leverage number or debug reason is drawn on the LED by default.

## 8. Configuration and diagnostics

**User settings:**
- ordered favorites per league;
- league and team mutes;
- per-board and per-league delay;
- league weights (0.5–2.0);
- the turn length, within safe bounds;
- promotions on or off. Off keeps weighted live rotation.

A team mute excludes the contest even when the other team is a favorite. Everything else in this document
is the versioned policy.

**Applying config** (designed in H5; there's no reload yet):
- **The safe boundary:** on the decision thread, after one refresh returns and before the next starts.
  Never during a paint.
- **One swap:** the new config is built and validated whole, then replaces the old as one value. A config
  that fails validation leaves the old one in force, and the trace says why.
- **Carried over:** cooldowns, debt, the takeover total, break and halftime holds, on-air minimums, the
  poll plan's due times and the quota ledger. None restarts at the reload. A shorter delay never releases
  held state early: what's held keeps its release instant.
- **At once:** a mute or blank. The muted contest's cards leave at the next paint, and a blank shows at once.
- **The trace:** each run opens with a `versions` line (policy, config schema and digest, rules,
  models, the code's commit) and a `config` line that renders the digested settings (`omni/app/versions.py`).
  A reload will write both again, marked `reload <old digest>-><new digest>`.

**The trace logs:**
- the policy, config and model versions;
- the source revision and confidence;
- `G`, `F` and `T`;
- the target and actual shares;
- debt and rescue;
- the event's score and bar;
- the lease, hold and cooldown;
- every refusal's reason.

**The "why this card" sentence:** one line, such as "Rockies: second favorite, close late game; Braves in
inning break". It never reveals an embargoed candidate.

## 9. Reference numbers for acceptance

These are allocation targets, all games healthy with settled `G`, from the policy arithmetic. Production
acceptance runs them through real adapters, released generations and sink receipts on all three profiles.

| Scenario | Targets |
|---|---|
| Two same-league non-favorites, `G` = 1 vs 0 | 71.43% / 28.57%: **2.5:1** |
| ATL and COL both acting, equal `G` = .4, no other game | 61.54% / 38.46% |
| COL close and late, ATL blowout | COL 60.98% / ATL 39.02%: leverage can beat rank |
| CAR and ATL only, equal | CAR 60.98% / ATL 39.02% (NFL's short season) |
| MNF + 8 NBA, no favorite, `G` = .4 | NFL 22.50% |
| 1 NFL + 15 MLB, no favorite | NFL 18.39% |
| 4 high-leverage NBA, one postseason | 27.71% for the postseason game, 24.10% for each other |
| CHA high leverage + 2 high-leverage others | CHA **70.00%** (the floor binds), others 15.00% each |
| CHA blowout + 2 high-leverage others | CHA 44.44%, others 27.78% each |
| ATL + 14 high-leverage MLB: ATL blowout / ATL close | ATL **30.00%** / **70.00%** |
| September Sunday (9 NFL, 10 MLB, `G` = .4): both favorites acting | CAR 28.24%, ATL 17.76% (46% combined); NFL 59.87% |
| The same, only CAR acting / only ATL acting | CAR 46.00% / ATL 46.00% |

**Interrupt values:**
- the NBA example: 0.78;
- a confirmed perfect game in a blowout: 1.1375;
- a no-hitter at average `Q`: 1.155.

**Interrupt timing:**
- **A mound visit releasing at t = 18** takes an action-qualified cut-in at 18–30.
- **With no break,** an `I` = 1.2 event starts at 24–36; `I` = 0.78 never starts in action.
- **Four promoted events in open rotation,** eligible at 0 with a hard end at 120: promotions at 0–12 and
  60–72 only.

**Breaks:**
- **A generic official timeout** gets one 12 s guest.
- **A positively identified TV timeout** (with 10 s detection) gets eight 12 s guests, 0–96.
- **MLB inning breaks** of 120/145/175 s give 96/120/144 s of guest airtime at zero detection age.
- **An NBA decrement** gives twelve turns at most when it is a quarter's first or second, and three otherwise.

## 10. Deferred

- season and playoff simulation (standings leverage beyond `C_season`);
- NFL and NBA national-TV signals;
- rivalry and comeback bonuses;
- displayed NBA WP;
- NHL and PGA models.

**Records sources:**
- **NFL:** the NFLMeta standings endpoint (to be probed), or nflverse results. **Never a season archive built
  from NFLMeta finals:** its terms bar systematic accumulation.
- **NBA:** a ledger of permitted finals.
