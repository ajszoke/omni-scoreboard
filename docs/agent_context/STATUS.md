# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-14)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**. `main` = pure upstream history.
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PR #1** (`agent/bootstrap-context`) open: agent context pack + project-local agent setup. Not merged.
- Dev env verified: **uv + `.venv`** (Py 3.12); emulator runs headless (RGBMatrixEmulator browser adapter, `http://localhost:8888/`). See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code (`screen` `omni` + `run.sh`); reachable from WSL (`ssh omni-board`, key auth verified). Connection details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Step 8 — typed-domain foundation:** start `omni/core/enum.py`; add `League`, `Sport`, `PanelProfile`, `GameStatus`, `DisplayPriority`, `UpdateUrgency`; value types `LeagueScopedId`, `SourceRef`, `DurationSeconds`, `RGBColor`; tests for serialization/coercion. See `docs/agent_context/ROADMAP.md` and `BACKLOG.yaml`; sketches in `starter_code/`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo, not GitHub fork metadata; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main` (default branch).
- Committed agent context pack; stood up project-local Claude Code (`.claude/`, thin `CLAUDE.md`, slim `.cursor` rule).
