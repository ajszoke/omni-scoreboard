# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-14)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream history + merged agent context pack (PR #1).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PR #1 merged** into `main` (`a6891a3`): agent context pack + project-local agent setup. Local branch `agent/bootstrap-context` retired; `origin/agent/bootstrap-context` still present (safe to delete).
- **Step 8 (typed-domain foundation) in progress** on branch `agent/typed-domain-foundation`: `omni/core/` landed — `enum.py` (mixins + `Sport`/`League`/`PanelProfile`/`GameStatus`/`DisplayPriority`/`UpdateUrgency` + `try_coerce_enum`), `ids.py`, `time.py`, `colors.py`, with tests in `tests/test_core_*.py`. Green locally (pytest / `mypy .` / `black --check .`; 24 new core tests). Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (RGBMatrixEmulator browser adapter, `http://localhost:8888/`). `pytest` now pinned in `requirements.dev.txt` (was undeclared); `starter_code/` excluded from `mypy`/`black` as reference sketches. See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code (`screen` `omni` + `run.sh`); reachable from WSL (`ssh omni-board`, key auth verified). Connection details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the foundation PR** (branch `agent/typed-domain-foundation`).
- Then grow the typed domain per `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md` migration steps: sport-specific **event-type enums** (`omni/events/`), **domain value objects** (`Competitor`/`Team`/`Contest` in `omni/domain/`), then wrap one **MLB live card** through the typed `ScoreboardCard` + renderer contract across all three panel profiles. See `ROADMAP.md` and `BACKLOG.yaml`; sketches in `starter_code/`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo, not GitHub fork metadata; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main` (default branch).
- Committed agent context pack; stood up project-local Claude Code (`.claude/`, thin `CLAUDE.md`, slim `.cursor` rule).
- **PR #1 merged** (agent context pack + project-local agent setup).
