# Omni Scoreboard Roadmap

## North star

Omni Scoreboard is a tiny, opinionated sports-awareness appliance for friends and family.

It should answer these questions at a glance:

- Is my team playing?
- What is the score?
- Is anything important happening?
- Did I miss a big play?
- Is the game close?
- Is there a no-hitter, red-zone drive, late comeback, cut-line sweat, or other high-leverage moment?
- Can a non-Linux person plug it in, configure Wi-Fi, pick teams, and mostly forget it?

Non-goals:

- No full consumer SaaS.
- No App Store app for initial setup.
- No accounts unless absolutely necessary.
- No paid data by default.
- No fragile cron-based updater that can brick every family device at once.

## Repository strategy

### Facts

- `ajszoke/omni-scoreboard` is currently a fork of `MLB-LED-Scoreboard/mlb-led-scoreboard`.
- The legacy fork has 0 forks and 0 stars/watchers in the GitHub UI; renaming it has effectively no public blast radius.
- Upstream is heavily diverged and active. The compare view shows upstream ahead by 809 commits and 177 files changed relative to the legacy fork comparison.
- Upstream already supports board dimensions including 64x32, 64x64, and 128x64.
- Upstream has useful modern foundation pieces: config schemas, plugin support, emulator mode, news/standings plugins, broadcast sync delay, API refresh configuration, Pi 5 work, frame pacing, and ABS indicator work.

### Recommendation

Use the name `omni-scoreboard` for the revived project, with no `v2` suffix.

Preferred local/git shape:

```text
origin   -> git@github.com:ajszoke/omni-scoreboard.git
upstream -> https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git
legacy   -> https://github.com/ajszoke/omni-scoreboard-legacy.git
```

Because GitHub generally does not allow multiple forks of the same upstream under one owner, the new repo may need to be a normal repo created from an upstream clone rather than a GitHub-recognized fork. That is acceptable for this project as long as `upstream` remains configured and history is preserved.

### Branching

```text
main          stable-ish human-usable branch
agent/*       agent work branches
feature/*     human-authored feature branches
legacy/*      ported feature branches from old Omni commits
upstream-sync temporary branch for upstream rebases/merge tests
```

Do not use `omni-v2` in branch or repo names unless it is a disposable local branch.

## Supported displays: equal support, different compromises

All meaningful features must declare behavior for each of these profiles.

| Profile | Physical shape | Logical size | Product role | Design stance |
|---|---:|---:|---|---|
| `single_64x32` | 1x1 | 64x32 | Primary compact family unit | Sports pager: fewer, sharper cards |
| `stack_64x64` | 2x1 vertical stack | 64x64 | New third supported layout | Rich single-game view; excellent for stat cards |
| `quad_128x64` | 2x2 | 128x64 | Legacy/rich mode | Dense dashboard; animations and multi-region layouts |

### Layout policy

Every renderer must implement one of:

1. Native support for all three profiles.
2. Native support for one or two profiles plus an explicit, tested compromise for the others.
3. A declaration that the card is not eligible for a profile, with a fallback card defined.

Do not silently cram 128x64 content into 64x32.

### Suggested display roles by profile

| Feature | 64x32 | 64x64 | 128x64 |
|---|---|---|---|
| Live MLB at-bat | Score, inning, count, outs, bases | Add batter/pitcher and abbreviated play text | Full live dashboard |
| Pregame | Matchup, start time, probable pitcher initials | Add probable pitcher names and odds/probability | Full pregame card with odds, weather, probable pitchers |
| Probability bar | Thin, high contrast | Standard bar plus percentage | Full win-prob module with trend if available |
| No-hitter/perfect game | Badge plus recurring full-screen alert | Sticky ribbon plus pitcher line | Dedicated panel region/sticky alert |
| Last K / play-of-inning | Full-screen micro-card before inning card | Micro-card plus inning transition | Preserve in lower/right event queue |
| K-zone | Maybe count-only; optional 3x3 blips | Simple zone/blips | Full pitch-location mini-map |
| Other-game ticker | Idle states only | Idle states and lower ribbon | Persistent bottom ticker during non-active states |
| Pitching change | Compact substitution card | Split outgoing/incoming stats | Full takeover animation |
| HR arcs/3D | Defer/fallback text | Simple animation | Fun rich animation |

## Phase plan

### Phase 0 — Project reset and local foundation

Goal: revive the repo cleanly without carrying old divergence as technical debt.

Tasks:

- Rename legacy repo to `omni-scoreboard-legacy`.
- Create new `omni-scoreboard` from current upstream history.
- Add remotes: `origin`, `upstream`, `legacy`.
- Confirm upstream current release and default branch.
- Add this context pack to `docs/agent_context/` or equivalent.
- Establish local path `/opt/omni-scoreboard`.
- Run upstream in emulator mode.
- Run upstream on hardware if available.
- Create `main` and first `agent/bootstrap-foundation` branch.

Definition of done:

- `git remote -v` is correct.
- `python3 main.py --emulated` or upstream equivalent launches.
- A README note documents the legacy remote and migration strategy.
- A first PR exists or the first bootstrap commit is pushed.

### Phase 1 — Typed domain core

Goal: stop primitives from leaking through the project.

Tasks:

- Add `omni/core/enum.py`, cribbed from the uploaded enum mixin style.
- Add typed `League`, `Sport`, `PanelProfile`, `GameStatus`, `CardKind`, `PriorityBand`, and `UpdateUrgency` enums.
- Add value objects for IDs, timestamps, durations, colors, logos, display dimensions, panel profiles, and source metadata.
- Define base `Competitor`, `Team`, `Athlete`, `Contest`, `GameEvent`, and `ScoreboardCard` classes.
- Add sport-specific event type enums, beginning with `BaseballGameEventType`.
- Add `BaseballTeam`, `FootballTeam`, `BasketballTeam`, `HockeyTeam`, and `Golfer`/`GolfCompetitor` domain classes.
- Add `from __future__ import annotations`, `slots=True`, `frozen=True` value objects where practical.
- Add mypy/pyright guardrails incrementally.

Definition of done:

- No new core code accepts raw league strings or team strings except at API/config boundaries.
- Provider code converts raw API data into domain objects immediately.
- Renderer code consumes domain/card objects rather than raw provider JSON.

### Phase 2 — Display profiles and preview/snapshot tooling

Goal: make all three supported layouts real and testable.

Tasks:

- Add `DisplayProfile` definitions:
  - `single_64x32`
  - `stack_64x64`
  - `quad_128x64`
- Map profiles to matrix flags/config.
- Add renderer contract: each card declares supported profiles and fallback behavior.
- Add emulator preview command.
- Add fixture record/replay command.
- Add PNG snapshot generation per profile.
- Add golden-image tests for spacing-sensitive cards.

Definition of done:

```bash
omni preview --profile single_64x32 --fixture fixtures/mlb/live-close-game.json
omni preview --profile stack_64x64 --fixture fixtures/mlb/live-close-game.json
omni preview --profile quad_128x64 --fixture fixtures/mlb/live-close-game.json
omni snapshot test
```

### Phase 3 — MLB excellence

Goal: make MLB delightful and reliable before expanding leagues.

P0/P1 fixes:

- Investigate and fix pregame cards halting the display permanently.
- Implement TV-delay presets over upstream `sync_delay_seconds` semantics.
- Fix team logo asset problems and background contrast.
- Reinstate color-clash logic with alternate logo backgrounds.
- Add probability-bar contrast validation.
- Add SWPR pitch type mapping so it does not display as `UNKW`.
- Fix stat-line debounce and horizontal bouncing.
- Fix bot-right cards: sacs, RBI, complex plays.
- Fix truncated fielder sequences like `9-6-4-`.
- Fix end-of-game winner rendering: fade loser by winner, not screen position.
- Tighten W/L/S pitcher rotation.
- Nudge runline and extra-innings spacing.

P2/P3 improvements:

- Preserve last K / play-of-inning before mid/end-inning transition.
- Add backwards K / K tracker.
- Add no-hitter/perfect-game sticky priority.
- Add challenge/ABS tightening.
- Add idle-state ticker for other games/news.
- Add mid-inning linescore, especially on 64x64 and 128x64.
- Add K-zone with pitch blips on 64x64/128x64; fallback on 64x32.
- Add pitching-change transition card.
- Add HR stats/arcs as fun rich-mode polish.

Definition of done:

- MLB runs well across all three profiles.
- Fixture tests cover no-hitter, perfect-game, strikeout, challenge, pitching change, complex play, extra innings, final, and pregame states.
- 64x32 does not feel like a broken crop of 128x64.

### Phase 4 — Delayed event queue and priority engine

Goal: replace state-only rotation with an event/card queue.

Core concepts:

```text
Provider raw data
  -> Provider normalizer
  -> Typed domain events
  -> TV-delay buffer
  -> Priority scorer
  -> Interleaved card queue
  -> Renderer selected by display profile
  -> LED matrix / emulator
```

Requirements:

- Events become eligible only after `source_time + selected_delay`.
- Cards carry display timing, dedupe keys, priority reasons, and profile support.
- Favorite-team and high-leverage events poll more often and display longer.
- No-hitter/perfect-game, red-zone, late close game, cut-line, and final alerts can become sticky.
- The queue interleaves sports rather than letting one provider monopolize the display.

Priority signals:

| Domain | High-priority signals |
|---|---|
| MLB | Favorite team, close late game, bases loaded, scoring play, HR, no-hitter/perfect-game, challenge/ABS, final |
| NFL | Favorite team, red zone, scoring play, turnover, 2-minute drill, close late game, final |
| NBA | Favorite team, clutch time, lead change, run, final minute, final |
| NHL | Favorite team, power play, empty net, tie/one-goal late game, final |
| PGA | Favorite golfers, majors, leader/cut-line movement, final-round back nine, playoff, cut sweat |

Definition of done:

- Queue behavior is deterministic under fixture replay.
- TV delay prevents score spoilers for configured sources.
- High-priority cards are visible without burying normal scoreboard updates.

### Phase 5 — Friends-and-family setup, updater, and device behavior

Goal: make a box that can be handed to someone.

Setup:

- First boot without Wi-Fi creates local AP: `OmniScoreboard-XXXX`.
- Device shows setup URL/QR or simple setup text.
- User configures Wi-Fi, favorite teams/golfers, display profile, time zone, brightness, delay preset, sleep schedule, and optional API keys.
- Wi-Fi credentials stay local.
- Hosted web page, if any, is docs/static helper only.

Updater:

- Use a `systemd` timer, not cron.
- Fetch `origin/main` or `stable` daily and on startup.
- Pull only fast-forward changes.
- Validate config before restart.
- Store previous commit for rollback.
- Health-check service after restart.

Power/schedule:

- User-defined sleep/wake schedule.
- Night dimming.
- Optional favorite-game wake override.
- GPIO physical switch:
  - Short press: display on/off.
  - Long press: setup mode.
  - Very long press: safe shutdown.

Definition of done:

- A family unit can be imaged, booted, configured locally, and updated without SSH.

### Phase 6 — NFL

Goal: first non-MLB league.

Data:

- Start with ESPN site API scoreboard data.
- Treat ESPN as unofficial/fragile; isolate behind provider interface.
- Add optional commercial fallback later only if needed.

Minimum cards:

- Pregame: matchup, kickoff, records, spread/total if available.
- Live: score, quarter, clock, possession, down/distance.
- Priority: red zone, score, turnover, 2-minute drill, close late game.
- Final: winner, score, key stat.

Definition of done:

- Favorite NFL team can be followed across all three display profiles with TV delay and priority queue integration.

### Phase 7 — NBA

Goal: frequent-game league with simpler event model than baseball.

Cards:

- Pregame matchup.
- Live score/period/clock.
- Lead change.
- Close late-game sticky.
- Final/top scorer.

Definition of done:

- Favorite NBA team support works and interleaves sanely with MLB/NFL.

### Phase 8 — NHL

Goal: add hockey with power-play and close-game urgency.

Cards:

- Pregame matchup.
- Live score/period/clock/shots if available.
- Power play.
- Goal alert.
- Empty net / late close game.
- Final.

Definition of done:

- Favorite NHL team support works across all display profiles.

### Phase 9 — PGA

Goal: support tournament/leaderboard viewing before NCAA.

Data:

- Start with ESPN PGA scoreboard endpoint for schedule/current tournament/leaderboard.
- Consider DataGolf API for live model probabilities and finish/cut-line probabilities if low-cost access fits.
- Consider SportsDataIO Golf only if a paid/trial feed becomes necessary.

Product concept:

PGA is not team-vs-team. It should use favorite golfers, tournament importance, leaderboard volatility, cut-line movement, and final-round pressure.

Minimum cards:

- Current tournament summary.
- Favorite golfer card: position, total, today, thru, next tee time if available.
- Leaderboard top 3/5.
- Cut-line card on Friday.
- Final-round back-nine pressure card.
- Playoff/final winner card.

Priority:

- Favorite golfer within 3 shots of lead.
- Favorite golfer near cut line.
- Major championship.
- Final round, back nine.
- Lead change.
- Playoff.

Definition of done:

- User can follow selected golfers and majors without needing paid data by default.

### Phase 10 — NCAA later

Goal: only after pro sports plus PGA are stable.

NCAA has high data complexity: many teams, rankings, bowls, tournaments, neutral sites, conferences, and inconsistent event payloads. Treat as a later expansion.

## Open decisions

- Do we require GitHub-recognized fork metadata, or is a normal repo cloned from upstream with an `upstream` remote enough?
- Do we target Python 3.10+ to match upstream runtime checks or push toward a newer typed baseline once hardware images are confirmed?
- Do we add Pydantic for config/API boundary validation or keep runtime dependencies lighter with dataclasses plus explicit parsing?
- What exact 64x64 physical panel arrangement is preferred: two 64x32 panels stacked vertically, or native 64x64 panel? The logical profile should support both where matrix flags allow.
- Should odds/probability be disabled by default to avoid API keys, or enabled where free endpoints suffice?
