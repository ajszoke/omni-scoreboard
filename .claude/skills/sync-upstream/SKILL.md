---
name: sync-upstream
description: Pull the latest MLB-LED-Scoreboard upstream into Omni and reconcile it. Use when updating to a new upstream release or catching up on upstream fixes.
---

# Sync from upstream

Remotes: `origin` = `ajszoke/omni-scoreboard`, `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard`
(branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy`.

```bash
git fetch upstream --tags
git switch main
git merge --no-ff upstream/master        # or rebase a feature branch onto upstream/master
```

After merging:
- Re-check the typed-domain invariants and the three display profiles (`AGENTS.md`) — do not
  let an upstream change regress them.
- Run the `test` skill.
- **Never force-push `main`.** Resolve conflicts in favor of Omni's typed domain at boundaries.
