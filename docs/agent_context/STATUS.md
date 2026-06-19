# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#11 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer, MLB provider boundary, live-state + CardFactory, on-screen emulator preview, priority scoring).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#11 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **TV-delay buffer in progress** on branch `agent/delay-buffer`: the second Phase-4 queue piece. `DelayBuffer[T]` (`omni/queue/delay_buffer.py`) is a generic holding buffer — `push(item, observed_at=…)` holds it, `release(now)` yields items once their fixed TV-delay has elapsed (in push order), so a unit next to a delayed broadcast never spoils a score. Also `pending()` / `next_release_at()`. Pure/deterministic; 7 tests; `omni/queue` at 100%. Dogfooded: a 30s-delayed HR is held to t=29 and released at t=30. Priority-bypass for ALERT-band cards is left to the queue layer. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`; `*/__main__.py` omitted from coverage. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the delay-buffer PR** (branch `agent/delay-buffer`).
- **Finish Phase 4** (`omni/queue/`): `InterleavedCardQueue` — fair next-card pick across leagues/contests, dedupe via `DedupeKey`, sticky alerts, priority-bypass of the delay for ALERT-band cards. Consumes `PriorityScorer` (done) + `DelayBuffer` (done). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.
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
- **PR #11 merged** — `PriorityScorer` (`omni/queue/priority.py`): explainable `CardPriority` (band + score + reasons) from live state; first Phase-4 queue piece.
