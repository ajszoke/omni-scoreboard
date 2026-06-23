# Claude Code Bootstrap Prompt — `/opt/omni-scoreboard`

Paste this file into local Claude Code as the controlling prompt for standing up the revived project.

## Mission

Stand up the revived `omni-scoreboard` project locally at `/opt/omni-scoreboard` and on GitHub under `ajszoke/omni-scoreboard`, preserving the current legacy fork as `ajszoke/omni-scoreboard-legacy`.

Do not use `v2` in the repo name.

The revived project should be based on current `MLB-LED-Scoreboard/mlb-led-scoreboard` upstream history, with old Omni work ported selectively later.

## Critical caveat: GitHub double-fork behavior

Renaming `ajszoke/omni-scoreboard` to `omni-scoreboard-legacy` frees the name, but it may not allow creating a second GitHub-recognized fork of the same upstream under the same personal account. If GitHub refuses to create a second fork or `gh repo fork --fork-name omni-scoreboard` tries to rename/reuse the existing fork, do not fight it.

Use one of these paths:

### Path A — official GitHub fork metadata

Use this only if the old fork is no longer owned by `ajszoke` as a fork of upstream, or has been deleted/detached/transferred elsewhere.

### Path B — practical clone-and-push repo

Create `ajszoke/omni-scoreboard` as a normal GitHub repo initialized from upstream history, with `upstream` remote configured. This preserves git history and supports future upstream pulls, but the GitHub UI will not show it as a fork. This is acceptable unless the human explicitly says GitHub fork metadata is required.

## Safety rules

- Do not delete any repo, branch, or tag.
- Do not force-push `main`.
- Do not commit secrets.
- Stop and report if authenticated GitHub user is not `ajszoke` or does not have access.
- Stop and report if `/opt/omni-scoreboard` already exists and is non-empty unless it is clearly the intended clone.
- Use SSH remotes for `origin` if available; HTTPS is fine for `upstream`.

## Step 1 — inspect local environment

Run:

```bash
set -euo pipefail
whoami
hostname
pwd
uname -a
git --version
python3 --version || true
gh --version || true
gh auth status || true
```

If `gh` is missing or unauthenticated, ask the human to install/authenticate it before modifying GitHub.

## Step 2 — inspect current GitHub repos

Run:

```bash
gh repo view ajszoke/omni-scoreboard --json nameWithOwner,isFork,parent,defaultBranchRef,url || true
gh repo view ajszoke/omni-scoreboard-legacy --json nameWithOwner,isFork,parent,defaultBranchRef,url || true
gh repo view MLB-LED-Scoreboard/mlb-led-scoreboard --json nameWithOwner,defaultBranchRef,url,isFork || true
```

Expected initial state:

- `ajszoke/omni-scoreboard` exists and is a fork of upstream.
- `ajszoke/omni-scoreboard-legacy` probably does not exist yet.
- Upstream default branch is likely `master`.

## Step 3 — preserve/rename legacy repo

If `ajszoke/omni-scoreboard` exists and `ajszoke/omni-scoreboard-legacy` does not exist, rename the old repo:

```bash
gh repo rename -R ajszoke/omni-scoreboard omni-scoreboard-legacy --yes
```

Verify:

```bash
gh repo view ajszoke/omni-scoreboard-legacy --json nameWithOwner,isFork,parent,defaultBranchRef,url
```

If `omni-scoreboard-legacy` already exists, do not overwrite it. Inspect and report.

## Step 4 — create the revived repo

First try Path A if it appears possible:

```bash
gh repo fork MLB-LED-Scoreboard/mlb-led-scoreboard   --fork-name omni-scoreboard   --default-branch-only   --clone=false
```

Then verify:

```bash
gh repo view ajszoke/omni-scoreboard --json nameWithOwner,isFork,parent,defaultBranchRef,url
```

If this fails because the account already has a fork, use Path B:

```bash
gh repo create ajszoke/omni-scoreboard   --public   --description "Friends-and-family omni sports LED scoreboard based on MLB-LED-Scoreboard"   --clone=false
```

## Step 5 — clone to `/opt/omni-scoreboard`

Prepare `/opt` safely:

```bash
if [ -e /opt/omni-scoreboard ] && [ "$(ls -A /opt/omni-scoreboard 2>/dev/null | wc -l)" -ne 0 ]; then
  echo "/opt/omni-scoreboard already exists and is non-empty; inspect before continuing" >&2
  exit 1
fi
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
```

If Path A succeeded:

```bash
git clone git@github.com:ajszoke/omni-scoreboard.git /opt/omni-scoreboard
cd /opt/omni-scoreboard
git remote add upstream https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git || true
git remote add legacy https://github.com/ajszoke/omni-scoreboard-legacy.git || true
git fetch --all --tags
```

If Path B is needed:

```bash
git clone https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git /opt/omni-scoreboard
cd /opt/omni-scoreboard
git remote rename origin upstream
git remote add origin git@github.com:ajszoke/omni-scoreboard.git
git remote add legacy https://github.com/ajszoke/omni-scoreboard-legacy.git || true
git fetch --all --tags
git push -u origin HEAD:main
```

If upstream default branch is `master`, decide whether local `main` should track a renamed branch or whether to keep `master`. Preferred for Omni is `main`, but preserve upstream tracking via the `upstream` remote.

If current branch is `master` and we want `main`:

```bash
git branch -M main
git push -u origin main
```

Set default branch in GitHub if needed:

```bash
gh repo edit ajszoke/omni-scoreboard --default-branch main || true
```

## Step 6 — add initial agent context

Create directories:

```bash
mkdir -p docs/agent_context .cursor/rules starter_code
```

Copy the provided context artifacts into:

```text
docs/agent_context/ROADMAP.md
docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md
docs/agent_context/BACKLOG.yaml
AGENTS.md
.cursor/rules/omni-scoreboard.mdc
starter_code/enum_core.py
starter_code/domain_model_sketch.py
```

Commit them:

```bash
git checkout -b agent/bootstrap-context

git add AGENTS.md .cursor/rules/omni-scoreboard.mdc docs/agent_context starter_code

git commit -m "Add Omni Scoreboard agent context and typed architecture plan"
git push -u origin agent/bootstrap-context
```

Create a PR if desired:

```bash
gh pr create   --repo ajszoke/omni-scoreboard   --base main   --head agent/bootstrap-context   --title "Add Omni Scoreboard roadmap and agent context"   --body "Adds roadmap, typed domain architecture, backlog, Cursor/agent rules, and bootstrap context for the revived Omni Scoreboard project."
```

If not using PRs yet, merge locally only after human approval.

## Step 7 — install/run upstream emulator

From `/opt/omni-scoreboard`, inspect upstream docs and do the least-invasive install first.

Try:

```bash
cd /opt/omni-scoreboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
```

Then inspect install files before running anything with `sudo`:

```bash
ls -la
sed -n '1,220p' install.sh || true
sed -n '1,220p' README.md || true
```

If dependencies are clear, install for emulator/dev. Prefer venv-local installs before system installs.

Try running emulator according to upstream current docs, likely one of:

```bash
./main.py --emulated
python3 main.py --emulated
```

If missing dependencies, install only what is required and record commands in `docs/dev_setup.md`.

## Step 8 — first implementation branch after bootstrap

After context PR/commit exists, create the first real implementation branch:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b agent/typed-domain-foundation
```

Implement in small steps:

1. Add `omni/core/enum.py` from `starter_code/enum_core.py`.
2. Add tests for enum serialization and coercion.
3. Add `League`, `Sport`, `PanelProfile`, `GameStatus`, `DisplayPriority`, `UpdateUrgency`.
4. Add `LeagueScopedId`, `SourceRef`, `DurationSeconds`, `RGBColor`.
5. Run tests/type checks.
6. Commit.

## Done criteria for this bootstrap

Report back with:

- GitHub repo URLs for legacy and revived repos.
- Whether revived repo is an official GitHub fork or clone-and-push normal repo.
- `/opt/omni-scoreboard` remotes.
- Current branch and latest commit hash.
- Emulator status and exact command used.
- Any blockers, especially GitHub double-fork behavior, auth, DNS, or install failures.
