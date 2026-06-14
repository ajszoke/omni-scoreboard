---
name: port-from-legacy
description: Selectively port pre-revival Omni work from the legacy fork as reference. Use when reviving a feature or behavior that existed in the old omni-scoreboard before the upstream-based reset.
---

# Port from legacy (reference only)

The pre-revival Omni history is the `legacy` remote (`legacy/master`, `legacy/dev`). Per
`AGENTS.md`: **do not blindly merge legacy into upstream** — treat it as reference/prototype.

```bash
git fetch legacy
git log legacy/master --oneline           # find the feature/commit
git show legacy/master:<path>             # inspect a file without checking it out
git checkout legacy/master -- <path>      # pull a file into the tree to adapt (then re-type it)
```

Re-implement in the typed domain (`League`, `PanelProfile`, typed `ScoreboardCard`, etc.)
rather than copying stringly-typed code. Provider JSON stays at the boundary; renderers stay
data-free. Add tests for ported behavior.
