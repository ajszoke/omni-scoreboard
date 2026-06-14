---
name: curate-knowledge
description: Maintain the project's agent-facing knowledge base in an ingestion-optimal shape — prune, dedupe, keep AGENTS.md a lean map and CLAUDE.md a thin wrapper, and keep local memory machine-local. Use periodically, when docs drift, or after a milestone.
---

# Curate the knowledge base

Source of truth is **agent-agnostic and committed**: `AGENTS.md` (map) + `docs/agent_context/`
(body) + `docs/agent_context/STATUS.md` (living status). `CLAUDE.md` and `.cursor/rules/` are
THIN wrappers that defer to `AGENTS.md` — keep them thin.

Run this maintenance pass to keep everything ingestion-optimal:

1. **Update `STATUS.md`** whenever state changes (branch, PR, milestone). It should be short.
2. **One concept per doc.** Keep `AGENTS.md` a lean map (links, not prose dumps).
3. **Prune.** Delete docs/sections for shipped or abandoned work; remove stale commands;
   collapse duplicates. Verify a referenced file/flag still exists before keeping the claim.
4. **Promote.** A fact repeated >3× → put it in `AGENTS.md` or a doc. A procedure repeated >3×
   → make it a skill in `.claude/skills/`.
5. **Keep `CLAUDE.md`/`.cursor` thin.** If knowledge is creeping into them, move it to
   `AGENTS.md`/`docs/` and leave a pointer.
6. **Local auto-memory** (`~/.claude/projects/-opt-omni-scoreboard/memory/`) holds only
   machine-specific facts and must point back to `AGENTS.md` — never let it override committed
   docs. Keep `MEMORY.md` a short index; delete stale memory files.
7. **Never** put secrets, tokens, or machine-local paths into committed docs.
