# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-23)

- **Operating mode: the external-review cadence is live** (head/hand — the flagship/CGPT plans, Claude Code implements; *not* `/code-review`). **Round 1 (build-fidelity) is in flight:** the bundle was sent to CGPT and we're awaiting its verdict. **Do not build the next phase ahead of the verdict — process it first.** State + how-to: `~/omni-review/` (machine-local home), the `omni-review-cadence` auto-memory, and the `external-review` skill.
- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#16 (typed pipeline through Phase 4; then handoff docs, the **frozen kernel** at `docs/agent_context/kernel/`, the `external-review` skill, and the ARCHITECTURE as-built reconciliation). PR #17 (review-home relocation) open.
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#16 merged and cleaned up;** #17 open. `main` is green.
- **Phase 4 complete — the typed pipeline is whole:** provider → score → delay → interleave → render, each stage typed, tested, and dogfooded. 269 tests; `omni/` ~99% (queue/providers/factory 100% incl. branch); `mypy`/`black` clean. **Now under external review** (round 1 in flight).
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`; `*/__main__.py` omitted from coverage. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

- **Process the CGPT round-1 verdict** — the immediate step (blocks the rest). Archive it verbatim to `~/omni-review/rounds/round-1-verdict.md`, adjudicate each finding, **verify its `file:line` claims in-code**, then fold the sequenced next phase into this list. **The verdict sequences the options below — don't build ahead of it.**
- **Running-app orchestration** (option A): wire provider → `DelayBuffer` → `PriorityScorer` → `InterleavedCardQueue` → renderer into an actual refresh/render loop (the live equivalent of `omni preview`), with the dwell loop using each card's `min_display`/`max_display`. Priority-bypass of the delay for ALERT-band cards lives here.
- **MLB P0/P1 polish** (option B / M3: pregame/final cards, fielder sequences, contrast) and **league expansion** (option C / M6 NFL via an ESPN provider behind the same `Provider` interface).

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
- **PR #12 merged** — `DelayBuffer[T]` (`omni/queue/delay_buffer.py`): generic TV-delay holding buffer (`push`/`release`); no score spoilers.
- **PR #13 merged** — `InterleavedCardQueue` (`omni/queue/scheduler.py`): fair, priority-weighted rotation across contests (dedupe, sticky alerts, profile-aware); **completes Phase 4**.
