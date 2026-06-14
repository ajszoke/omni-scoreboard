# CLAUDE.md — Omni Scoreboard (Claude Code entry point)

This is a **thin, Claude-Code-specific wrapper**. The canonical, agent-agnostic project
guide is **`AGENTS.md`** — read it first. Put durable knowledge in `AGENTS.md` / `docs/`,
**not** here; keep this file thin.

@AGENTS.md

## Claude Code specifics (not in AGENTS.md)

- **Harness config** lives in `.claude/`: a permissions allowlist + a SessionStart status
  hook (`settings.json`) and workflow skills (`skills/`). Personal, uncommitted overrides go
  in `.claude/settings.local.json` and `CLAUDE.local.md` (both git-ignored).
- **Where to look:** current status → `docs/agent_context/STATUS.md`; dev env & emulator →
  `docs/dev_setup.md`; roadmap/architecture/backlog → `docs/agent_context/`.
- **Local auto-memory** (`~/.claude/projects/-opt-omni-scoreboard/memory/`) supplements a
  session but **never overrides** the committed docs above — `AGENTS.md` + `docs/` are the
  source of truth. Keep memory pruned and machine-local-only (see the `curate-knowledge` skill).
