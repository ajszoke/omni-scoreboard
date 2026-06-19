# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#7 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#7 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **MLB provider boundary in progress** on branch `agent/mlb-provider`: `omni/providers/` adds the `Provider` protocol + `ProviderUpdate`, an `MlbTeamRegistry` (raw StatsAPI team id → typed `BaseballTeam`; 30 clubs, colors from `colors/teams.example.json`), and `MlbStatsApiProvider` parsing the `schedule()` endpoint into typed `TeamGame` contests (matchup, status, start, venue). Raw JSON is confined to `omni/providers/`; the fetcher is injectable so tests use a fixture (`tests/fixtures/providers/mlb_schedule.json`) with **zero network**. Dogfooded **live** against the real API (14 real games, every team resolved, 0 warnings). 205 tests green; `omni/providers` at 100% stmt+branch. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the MLB provider PR** (branch `agent/mlb-provider`).
- **Live game state + CardFactory:** parse the richer StatsAPI per-game feed (`statsapi.get("game", ...)`) into a typed baseball game-state snapshot (`omni/domain/baseball.py`: score/inning/half/count/bases), then a **`CardFactory`** mapping snapshot → `LiveBaseballCardPayload` → `ScoreboardCard`. That closes provider → card → renderer; wire one card into the **emulator** (`run-emulator` skill) for an on-screen end-to-end dogfood.
- Then the **interleaved card queue + delay buffer** (`DelayBuffer`, `PriorityScorer`, `InterleavedCardQueue`). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.

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
