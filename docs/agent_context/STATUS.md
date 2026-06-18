# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-17)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#5 (agent context, typed core, domain objects, events, first card+renderer).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#5 merged and cleaned up.** Feature branches deleted locally and on `origin`.
- **QC hardening in progress** on branch `agent/qc-hardening`: a quality checkpoint at 5 PRs. Two adversarial audits (spec + tests) + live dogfooding found a real bug-class and branch gaps behind 99% line coverage. Fixes: `HalfInning` enum (kills a silent `half="TOP"`→bottom misrender), `BaseballCount`/score/inning validation, **+18 gap-closing tests**, `font.py` `Any` boundary comment, and `pytest-cov --cov-branch` wired into dev deps + the `test` skill so coverage self-reports. `omni/` at **99%** (5 defensive lines). Green locally. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest`/`pytest-cov` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black`. See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the QC PR** (branch `agent/qc-hardening`).
- **PR #7 — multi-profile renderer** (the one behavioral spec gap): explicit `stack_64x64` + `single_64x32` layouts for the live-baseball card, each with its own golden + draw-op tests (never a crop), consuming `LayoutSupport.fallback_card_kind`/`compromise_notes` (restores doc/code parity). Honors AGENTS.md's "consider all three profiles equally."
- Then a **provider/CardFactory** (MLB StatsAPI → typed `Contest`/`GameEvent`/`ScoreboardCard`) and the **interleaved card queue + delay buffer**. See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main`.
- **PR #1 merged** — agent context pack + project-local Claude Code.
- **PR #2 merged** — typed-domain core (`omni/core/`); pinned `pytest`, excluded `starter_code/` from lint/type.
- **PR #3 merged** — typed domain objects (`omni/domain/`: `Competitor`/`Team`/`Contest`).
- **PR #4 merged** — typed event layer (`omni/events/`: `GameEvent` + per-sport event-type enums).
- **PR #5 merged** — first card + renderer vertical (`omni/cards`, `omni/panels`, `omni/renderers`; `quad_128x64` + golden snapshot).
