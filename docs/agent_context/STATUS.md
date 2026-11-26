# Status — Omni Scoreboard

_Living status doc: what runs today, what's open, and what's next. Keep it short and current. Update it when state
changes, and move finished detail out instead of piling it up here. The history is in git (`git log`, the PRs) and
in the external review's log (`~/omni-review/plans/omni-build-fidelity-REVIEWS.md`, machine-local)._

## Now (2026-11-25)

**Where things stand:**
- **`main`** @ `bf1d7f7`: PRs #1–#135 are merged.
- **Gates:** 4,582 tests; `omni/` and `cloud.market_service` at 100% line and branch (`fail_under = 100`); mypy strict and black clean.
- **External review:** rounds 1–8 are done. Round 8 (2026-11-18) kept policy v2 and shared NFL opt-in until its closeout (#119) passed, and then the 11-22 NFL Sunday dogfood ran on them.

### What runs

- **The shared board** (`python -m omni.app`) runs MLB, NFL and NBA on one queue. Each league has its own delay-safe pipeline, and all share one poll plan, a quota ledger per provider account, and the tower's rotation.
  - **Defaults in code:** `--leagues mlb`, `--board-policy 1`, and Free plans.
  - **The user's launch:** policy 2, odds on, NFLMeta Builder, BALLDONTLIE ALL-STAR, favorites `nfl:CAR` and `nba:CHA`, `medium` scroll, and 40% brightness. The exact command is in the `run-on-board` skill.
  - **How frames reach the panel:** they render on the dev machine (WSL) and go over SSH to the Pi's display process (`tools/board_frame_sink.py` with `omni/app/receiver.py`). The receiver acknowledges each frame, and a card is booked on its confirmed first frame.
- **NFL's fallback** is the single-game runner, `python -m omni.app.football`.
- **Retention:** the expiry sweep runs in the shared app at start, hourly and at exit (#135). Run the board from the main checkout so the sweep covers its `build/`.

### By league

- **MLB** (off-season):
  - **Cards:** pregame, live, final, big-play and no-hitter, plus a perfect-game bid from a complete play log. They're built on StatsAPI behind a typed provider.
  - **Visuals:** logo tiles with alts for a clash, the win meter (Polymarket US first, then MLB's model), and Bovada's pregame odds.
  - **Statuses:** every StatsAPI status is mapped (#134), so a game under review or challenge stays live.
- **NFL** (NFLMeta Builder on the user's board):
  - **Cards and strip:** the live card with timeout and challenge pips, possession, and a play strip.
  - **On Builder,** every live game is read with its plays, so each card has its pips and its latest line (#129).
  - **The meter** comes from Polymarket US's public quotes.
  - **Weighting:** a 10-point win swing is offered at any time, and the first half's last two minutes get the late boost. A rotation card shows its game's latest line while that line is recent.
  - **Not built:** a pregame card (no approved design).
- **NBA** (BALLDONTLIE ALL-STAR on the user's board):
  - **Cards:** v6, on all three profiles (#121).
  - **Feed:** the provider gives records and ranks (#122). The feed deriver turns reads into baskets, free throws, fouls, technicals, timeouts by length and challenges (#127).
  - **Shared board:** NBA's breaks for the director and NBA's odds (#128): Bovada's pregame line and Polymarket's meter.
  - **Captions:** no `i/N` queue count (#130). A long player name scrolls (#126).

### Board policy and visuals

- **Policy v2** (`BOARD_POLICY.md`): opt-in with `--board-policy 2`, and the user's launches always use it.
  - **Rotation:** debt-based toward leverage-weighted targets, and the same card may repeat.
  - **The favorite floor** is `0.60 + 0.30·max(G)` (60% in a blowout, 90% close and late).
  - **Leverage** is a labeled proxy in every league until a model passes validation.
- **Clash:** two tile colors clash under CIE76 < 25 or CIEDE2000 < 20 (#124). Home keeps its primary, and away switches.
- **Meters** are lifted to WCAG 4.5:1 against black (#133). Charlotte shows a teal hornet on purple, with a teal meter.
- **Render cost** (#131): each string's raster and each matchup's treatment is cached, and the canvas pastes in C. A quad frame on the Pi 3A+ fell from 104 ms to 11.7 ms, so a Pi Zero 2 W fits every profile.

### Data rights and retention

- **The terms:** each source's terms are typed in `omni/providers/rights.py`, with the user's display ruling beside them (`AGENTS.md`, "Data policy").
- **NFLMeta** allows no standing copy, and the user's rule is one week. So recordings and run logs are stamped, and the sweep mines each one into `build/telemetry/` (a digest plus a redacted log), then deletes it.
- **`tools/redact_run_log.py --check`** gates anything bundled or shared.
- **2026-11-25:** every raw NFLMeta copy on the dev machine was mined or redacted. That includes the round-4 review bundle, whose one raw log was swapped for its redaction.
- **BALLDONTLIE's key** never goes onto a friend's board.

## Open

- **Friends' boards and the data they read:** the user's BALLDONTLIE and NFLMeta keys can't go on a friend's board. Polymarket's terms bar redistribution, and the cloud market service relays its prices to boards. These are the user's calls, best taken as one design.
- **On the device:**
  - `MatrixDisplaySink`/`MatrixCanvas` set pixels one at a time from Python; render into `PillowCanvas` and push one `SetImage`.
  - The app doesn't run on the Pi yet.
  - Waveshare P4 panels may need `--led-panel-type` (FM6126A, then FM6127, if a panel stays dark), which the sink doesn't pass yet.
- **Board:**
  - one health for the whole board (its healthiest league's), so dimming isn't per league yet;
  - leverage models in shadow, to replace the proxies;
  - the stateless decide-every-tick board, which the user parked on 2026-11-25.
- **NFL:**
  - a pregame card;
  - a persisted play cursor, to save a full re-read (about 280 rows a game) at each restart;
  - playoff overtime, where the pips are hidden and the rules not modeled.
- **NBA,** from dogfooding:
  - when a run ends;
  - timeout breaks;
  - the order of two plays in one read;
  - a timeout's onset during free throws;
  - win swings and play promotions, as NFL has;
  - Bovada's NBA coupon sometimes answers `{}`.
- **Cloud:** `ConsistentRead` on board tokens before a private fleet, and CI deploys.
- **From the upstream review** (2026-11-25, worth porting, none urgent):
  - the batter's AVG and the pitcher's ERA on the quad;
  - a `--profile` flag for the data thread;
  - hzeller's newer driver pin, when the friend boards are set up;
  - an MLB play-text line, which needs a layout round.

  Upstream code itself isn't merged: Omni runs none of it, and `upstream/dev` drops `bdfparser`, which Omni's renderers use.

## Next (suggested 2026-11-25; the user picks)

1. **Friends' boards: the data path and terms.** Decide how a friend's board gets each league without the user's keys. Run it as review round 9, with the hardware findings.
2. **The app on the Pi:**
   - target a Zero 2 W with the Adafruit Triple LED Matrix Bonnet;
   - push one `SetImage` a frame;
   - run as a `systemd` service;
   - add a panel-type option;
   - measure p50/p95/p99 frame and data costs.
3. **Device basics** (`ROADMAP.md` Phase 5, BACKLOG M5): a setup portal, a safe updater with rollback, a sleep schedule and a button.
4. **The enclosure** (M5-005), once the panels arrive to measure. A quad of Waveshare P4 64x32 panels is 512×256 mm, so it prints in sections.
5. **NBA follow-ups,** as dogfooding finds them (above).

## Milestones

| When | What | PRs |
|---|---|---|
| 2026-06 | Revived from upstream MLB-LED-Scoreboard v9.1.5; the typed domain, profiles, goldens and queue; the A0 spine and MLB's lifecycle cards | #1–#94 |
| 2026-11-01 | Live-card design round; market data (Polymarket US, Bovada) | — |
| 2026-11-15 | AWS market service; the NFL display and live runner; review round 4 | #96–#100 |
| 2026-11-16 | A1a, the tower's first slice: frames apart from decisions, `PollPlan`, play strip, card rotation; NBA research and probe | #101–#108 |
| 2026-11-17 | Round 5's gates C1–C4; NBA tiles; dogfood fixes D1–D4; round 7's policy v2 | #109–#113 |
| 2026-11-18 | H5, the data boundary; policy v2 and NFL on the shared board, opt-in; round 8 and its closeout | #114–#119 |
| 2026-11-19 | NBA: timeouts research, cards v6, the provider; NFL strip yardage | #120–#123 |
| 2026-11-25 | NFL Sunday dogfood fixes; NBA events and NBA on the shared board; clash, logos, no `i/N`, render caching, meter floor, MLB statuses, NFLMeta redaction | #124–#135 |

## Environment

- **Remotes:** `origin` is `ajszoke/omni-scoreboard` (HTTPS). `upstream` is `MLB-LED-Scoreboard/mlb-led-scoreboard`. `legacy` is `ajszoke/omni-scoreboard-legacy`.
- **Dev:** uv with `.venv` (Python 3.12); the emulator runs headless (`docs/dev_setup.md`). The gates are in the `test` skill.
- **The reference board:** a Raspberry Pi 3 Model A+ driving four 64x32 panels through an Adafruit HAT (the PWM mod). It's `ssh omni-board`; details are in `CLAUDE.local.md`. It shows the shared app's frames.
