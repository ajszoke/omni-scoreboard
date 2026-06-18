# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-17)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream + PRs #1–#4 (agent context, typed core, domain objects, events).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#4 merged and cleaned up** (context, `omni/core/`, `omni/domain/`, `omni/events/`). Feature branches deleted locally and on `origin`.
- **First card + renderer vertical in progress** on branch `agent/card-renderer`: `omni/cards/` (`ScoreboardCard[Payload]` + `CardKind`/`DisplayTiming`/`LayoutSupport`/`CardPriority` + `LiveBaseballCardPayload`), `omni/panels/geometry.py` (`PanelProfile`→`PanelGeometry`), `omni/renderers/` (hardware-agnostic `Canvas` Protocol with `RecordingCanvas` + `PillowCanvas`, the `Renderer[Payload]` contract, and `LiveBaseballRenderer` for `quad_128x64`). Verified by a draw-op recorder **and** a pixel-exact golden PNG (`tests/golden/`, regen via `OMNI_REGEN_GOLDEN=1`). Green locally. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black` as reference sketches. See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the card+renderer PR** (branch `agent/card-renderer`).
- Extend `LiveBaseballRenderer` to **`stack_64x64`** and **`single_64x32`** — each an explicit, golden-tested layout (never a crop of the 128x64 one), exercising the three-profile policy.
- Then a **provider/CardFactory** (MLB StatsAPI → typed `Contest`/`GameEvent`/`ScoreboardCard`) and the **interleaved card queue + delay buffer** (migration steps 5–7). See `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`, `ROADMAP.md`, `BACKLOG.yaml`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main`.
- **PR #1 merged** — agent context pack + project-local Claude Code.
- **PR #2 merged** — typed-domain core (`omni/core/`); pinned `pytest`, excluded `starter_code/` from lint/type.
- **PR #3 merged** — typed domain objects (`omni/domain/`: `Competitor`/`Team`/`Contest`).
- **PR #4 merged** — typed event layer (`omni/events/`: `GameEvent` + per-sport event-type enums).
