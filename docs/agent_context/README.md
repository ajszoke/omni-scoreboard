# Omni Scoreboard Context Pack

Purpose: provide durable, agent-ingestible context for reviving `omni-scoreboard` as a friends-and-family LED sports scoreboard project.

Primary product scope:

- Build small scoreboard boxes for friends and family.
- Do not chase full consumer-grade competitor scope.
- Equally support three display profiles:
  - `single_64x32`: 1x1 64x32 panel.
  - `stack_64x64`: 2x1 stack of two 64x32 panels, yielding 64x64.
  - `quad_128x64`: 2x2 matrix of four 64x32 panels, yielding 128x64.
- MLB remains the quality baseline.
- Expansion order: MLB first, then NFL, NBA, NHL, PGA, then NCAA only after the above are stable.

Files in this pack:

- `ROADMAP.md` — comprehensive product and implementation roadmap.
- `ARCHITECTURE_TYPED_DOMAIN.md` — typed/OOP architecture proposal and model sketches.
- `BACKLOG.yaml` — issue-like structured backlog for agents.
- `AGENTS.md` — repo-level agent orientation; drop at repository root.
- `.cursor/rules/omni-scoreboard.mdc` — Cursor rule file; drop into `.cursor/rules/`.
- `CLAUDE_CODE_BOOTSTRAP_OPT.md` — separate local Claude Code prompt for standing up GitHub and `/opt/omni-scoreboard`.
- `starter_code/enum_core.py` — starter enum mixins and initial enums, cribbing the uploaded enum style.
- `starter_code/domain_model_sketch.py` — typed domain/event/card sketch.
- `SOURCES.md` — research basis and URLs.

Important repository caveat:

Renaming `ajszoke/omni-scoreboard` to `omni-scoreboard-legacy` frees the repository name, but it may not by itself permit creating a second personal GitHub fork of the same upstream. The bootstrap guide includes two paths: an official-fork path if the legacy fork is transferred/deleted/detached, and a practical clone-and-push path that preserves upstream history without GitHub fork metadata.
