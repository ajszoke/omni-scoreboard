#!/usr/bin/env bash
# SessionStart hook: orient the agent with branch + working-tree status and point it at
# the canonical docs. Read-only; safe. Referenced from .claude/settings.json.
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" 2>/dev/null || exit 0
b=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
printf 'omni-scoreboard | branch %s\n' "${b:-?}"
git status --short 2>/dev/null | head -6
printf '%s\n' '-> read AGENTS.md (canonical map) and docs/agent_context/STATUS.md'
