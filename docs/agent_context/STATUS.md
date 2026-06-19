# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#9 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer, MLB provider boundary, live-state + CardFactory).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#9 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **Emulator preview + matrix adapter in progress** on branch `agent/emulator-preview`: `omni/renderers/matrix_canvas.py` `MatrixCanvas` bridges the `Canvas` protocol onto any `SetPixel` surface (RGBMatrixEmulator now, real `rgbmatrix` later) and rasterizes **identically** to `PillowCanvas` — a test proves pixel-identity to the goldens on all three profiles. `omni/preview/` adds `build_card_from_scenario` (runs a scenario fixture — schedule row + game feed — through the real provider + factory) and a **`python -m omni.preview --profile … --fixture …`** CLI that draws the card on the emulator (browser at `http://localhost:8888/`). Scenario `fixtures/mlb/live-close-game.json` (tied, bottom 9, bases loaded, full count). Dogfooded: the CLI renders on the live emulator and exits cleanly — the Phase-2 `omni preview` DoD. 242 tests green; new modules at 100%, omni overall 99%. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`; `*/__main__.py` omitted from coverage. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the emulator preview PR** (branch `agent/emulator-preview`).
- **Interleaved card queue + delay buffer** (`omni/queue/`, ROADMAP Phase 4): `DelayBuffer` (TV-delay → a card's `available_at`), `PriorityScorer` (events/state → `CardPriority`, which the factory already accepts), `InterleavedCardQueue` (fair next-card pick across leagues/contests, dedupe via `DedupeKey`, sticky alerts). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.
- Then **MLB P0/P1 polish** (pregame/final cards, fielder sequences, contrast) and league expansion.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main`.
- **PR #1 merged** — agent context pack + project-local Claude Code.
- **PR #2 merged** — typed-domain core (`omni/core/`); pinned `pytest`, excluded `starter_code/` from lint/type.
- **PR #3 merged** — typed domain objects (`omni/domain/`: `Competitor`/`Team`/`Contest`).
- **PR #4 merged** — typed event layer (`omni/events/`: `GameEvent` + per-sport event-type enums).
- **PR #5 merged** — first card + renderer vertical (`omni/cards`, `omni/panels`, `omni/renderers`; `quad_128x64` + golden).
- **PR #6 merged** — QC hardening (`HalfInning` enum, validation, +18 gap tests, branch coverage).
- **PR #7 merged** — multi-profile renderer (`LiveBaseballRenderer` natively renders all three profiles; `single_64x32` is an explicit, tested compromise; `stack_64x64`/`single_64x32` goldens; `assert_never` dispatch).
- **PR #8 merged** — MLB StatsAPI provider boundary (`omni/providers/`: `Provider`/`ProviderUpdate`, `MlbTeamRegistry`, `schedule()` → typed `TeamGame` contests; fixture-driven, dogfooded live).
- **PR #9 merged** — live game state + `CardFactory` (`omni/domain/baseball.py` `BaseballGameState`; provider `fetch_game_state`; `omni/cards/factory.py`); closes provider → card → renderer, end-to-end test + dogfood.
