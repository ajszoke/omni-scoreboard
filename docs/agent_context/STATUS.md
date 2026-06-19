# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-18)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#12 (agent context, typed core, domain, events, first card+renderer, QC hardening, multi-profile renderer, MLB provider boundary, live-state + CardFactory, on-screen emulator preview, priority scoring, TV-delay buffer).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#12 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **Interleaved card queue in progress** on branch `agent/interleaved-queue` — the **Phase-4 capstone**. `InterleavedCardQueue` (`omni/queue/scheduler.py`) ingests deduped cards (by `DedupeKey`) and `next_card(now, profile)` picks what to show: "most overdue wins" = time-since-last-shown × per-band weight, so favorite/high-leverage contests get more airtime without burying normal ones; ALERT/STICKY cards take over the screen; eligibility honors `available_at`/`expires_at` and profile support. Pure/deterministic; 10 tests + a pipeline-composition test; `omni/queue` at 100%. Dogfooded a full slate: a favorite high-leverage game took 6/12 slots (others 3 each, none buried), and a walk-off ALERT took over entirely. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`; `*/__main__.py` omitted from coverage. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the interleaved-queue PR** (branch `agent/interleaved-queue`) — completes **Phase 4**. The typed pipeline is then whole: provider → score → delay → interleave → render, each piece tested + dogfooded.
- **Running-app orchestration:** wire provider → `DelayBuffer` → `PriorityScorer` → `InterleavedCardQueue` → renderer into an actual refresh/render loop (the live equivalent of `omni preview`), with the dwell loop using each card's `min_display`/`max_display`. Priority-bypass of the delay for ALERT-band cards lives here (orchestration decides what skips the buffer).
- Then **MLB P0/P1 polish** (pregame/final cards, fielder sequences, contrast) and league expansion (NFL via ESPN provider behind the same `Provider` interface).
- **Handoff:** an external full review is queued — keep `main` green and STATUS current.

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
