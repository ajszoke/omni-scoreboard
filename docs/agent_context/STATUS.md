# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#10 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer, MLB provider boundary, live-state + CardFactory, on-screen emulator preview).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#10 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **Priority scoring in progress** on branch `agent/priority-scorer`: the first piece of the Phase-4 queue (`omni/queue/`). `PriorityScorer.score_live_baseball(game, state)` turns live state into an **explainable** `CardPriority` (band + score + reason codes) per the ROADMAP signal table — favorite team, close & late, high leverage (late + close + runners on), bases loaded, runner in scoring position, full count + two outs. The `CardFactory` already accepts the resulting priority. Pure/deterministic; 10 signal-matrix tests; `omni/queue` at 100%. Dogfooded across a drama spectrum (blowout → walk-off): NORMAL/0 → HIGH_LEVERAGE/97. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`; `*/__main__.py` omitted from coverage. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the priority-scorer PR** (branch `agent/priority-scorer`).
- **Rest of Phase 4 queue** (`omni/queue/`): `DelayBuffer` (TV-delay → a card's `available_at`; no score spoilers) and `InterleavedCardQueue` (fair next-card pick across leagues/contests, dedupe via `DedupeKey`, sticky alerts). `PriorityScorer` (done) feeds it. See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.
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
- **PR #10 merged** — on-screen emulator preview + `MatrixCanvas` (`Canvas` → LED `SetPixel`, pixel-identical to goldens); `python -m omni.preview --profile … --fixture …`; Phase-2 `omni preview` DoD.
