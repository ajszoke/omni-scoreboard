# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-17)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#6 (agent context, typed core, domain, events, first card+renderer, QC hardening).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#6 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **Multi-profile renderer in progress** on branch `agent/multi-profile-renderer`: `LiveBaseballRenderer` now renders **all three** profiles — `quad_128x64` (full; golden unchanged), `stack_64x64` (full layout compressed to 64px wide), `single_64x32` (explicit compromise: abbreviations + scores + inning/half only; count/outs/bases omitted, asserted by a test). `render()` dispatches per profile with `typing.assert_never` exhaustiveness; goldens for all three (`tests/golden/`). Closes the one behavioral spec gap from QC; fixes the `LayoutSupport` doc/code drift. Green locally. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`. Coverage: `--cov=omni --cov-branch` (see `test` skill). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the multi-profile PR** (branch `agent/multi-profile-renderer`).
- **Provider / CardFactory:** MLB StatsAPI → typed `Contest`/`GameEvent`/`ScoreboardCard` behind a `Provider` interface (raw JSON confined to `omni/providers/`). Then the **interleaved card queue + delay buffer** (`DelayBuffer`, `PriorityScorer`, `InterleavedCardQueue`). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.
- With three profiles rendering, this is also the point to wire one card into the **emulator** end-to-end (`run-emulator` skill) for a real on-screen dogfood.

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
