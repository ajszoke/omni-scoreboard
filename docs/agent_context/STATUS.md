# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#8 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer, MLB provider boundary).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#8 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **Live game state + CardFactory in progress** on branch `agent/live-state-cardfactory`: closes **provider → card → renderer**. New `omni/domain/baseball.py` holds the baseball value objects (relocated `HalfInning`/`BaseballCount`/`BaseballBaseState`, re-exported from `events`/`cards` for back-compat) plus a `BaseballGameState` snapshot. The MLB provider gains `fetch_game_state(game_pk)` parsing the per-game feed (`statsapi.get("game", ...)` linescore: score/inning/count/bases) into `BaseballGameState`. A new `CardFactory` (`omni/cards/factory.py`) maps `TeamGame` + `BaseballGameState` → a renderable `ScoreboardCard[LiveBaseballCardPayload]`. End-to-end test: both raw fixtures → provider → factory → renderer (runners on first **and** third, beyond the PR #7 golden). Dogfooded: fixture pipeline renders to pixels on all three profiles; the live API correctly **refuses** a non-live game. 229 tests green; new/changed modules at 100% stmt+branch. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the live-state + CardFactory PR** (branch `agent/live-state-cardfactory`).
- **Emulator / matrix `Canvas` adapter:** bridge omni's `Canvas` protocol to `RGBMatrixEmulator` (and real `rgbmatrix`) + a runnable `omni preview` entry that draws an assembled card on-screen — the Phase-2 `omni preview --profile … --fixture …` DoD and a real on-screen dogfood.
- Then the **interleaved card queue + delay buffer** (`DelayBuffer`, `PriorityScorer`, `InterleavedCardQueue`): TV-delay sets `available_at`, the scorer fills `CardPriority` (the factory takes an optional priority today). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.

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
