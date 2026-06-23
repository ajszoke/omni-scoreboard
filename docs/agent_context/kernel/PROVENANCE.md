# Kernel — frozen original context pack (provenance)

This directory is the **verbatim, frozen** `omni_scoreboard_context_pack` that the external
flagship model (ChatGPT, "CGPT" / "external") authored on **2026-06-14** to seed this project.
It is the original specification the build was implemented against.

**Do not edit these files.** They are the **drift-review baseline**: each external-review round
diffs the as-built repo against this frozen plan to check the build stayed faithful. Editing them
would destroy the baseline.

## Posture (why this is housed here)

This project runs a **head / hand** split (see the `external-review` skill in `.claude/skills/`):

- **Head (planner):** the external flagship model. Owns conceptual reasoning, long-horizon
  planning, and design review. It seeded this kernel and reviews each round's bundle.
- **Hand (implementer):** Claude Code (this agent). Carries out the build, self-reviews against
  this kernel, fixes drifts, and bundles each round for the head to review.

## Living vs. frozen

The **living** project docs supersede this kernel and are the day-to-day source of truth:

- `AGENTS.md` (repo root) — the agent-agnostic map.
- `docs/agent_context/STATUS.md` — current state.
- `docs/agent_context/{ROADMAP,ARCHITECTURE_TYPED_DOMAIN,BACKLOG,SOURCES}.md` — evolved from
  the same-named files here.

When the living docs and this kernel disagree, that disagreement **is the drift** — surface it in
the review round; don't silently reconcile by editing the kernel.

## Contents

The pack as delivered: `README.md` (pack overview), `CLAUDE_CODE_BOOTSTRAP_OPT.md` (the bootstrap
prompt that stood up the repo), `AGENTS.md`, `ROADMAP.md`, `ARCHITECTURE_TYPED_DOMAIN.md`,
`BACKLOG.yaml`, `SOURCES.md`, `.cursor/rules/omni-scoreboard.mdc`, and `starter_code/`
(`enum_core.py`, `domain_model_sketch.py`).
