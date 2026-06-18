# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-17)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream history + agent context pack (PR #1) + typed-domain core (PR #2).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1 and #2 merged and cleaned up**: agent context + `omni/core/` (enums + value types). Both feature branches deleted locally and on `origin`.
- **Domain layer in progress** on branch `agent/domain-objects`: `omni/domain/` — `base.py` (`Competitor` protocol + `LogoAsset`), `teams.py` (`Team`/`BaseballTeam`), `athletes.py` (`Golfer`), `contest.py` (`Contest`/`TeamGame`/`GolfTournament`); tests in `tests/test_domain_*.py`. All domain records are `frozen/slots/kw_only`; `Competitor` is a read-only `@runtime_checkable` Protocol. Green locally (pytest / `mypy .` / `black --check .`). Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black` as reference sketches. See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the domain PR** (branch `agent/domain-objects`).
- Then continue the migration (see `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`): sport-specific **event-type enums** + generic `GameEvent`/`EventImportance` (`omni/events/`), then a typed **`ScoreboardCard`** for one MLB live card, then the **renderer contract** across all three panel profiles. See `ROADMAP.md` and `BACKLOG.yaml`; sketches in `starter_code/`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main`.
- **PR #1 merged** — agent context pack + project-local Claude Code.
- **PR #2 merged** — typed-domain core (`omni/core/`: enums, ids, time, colors + tests); pinned `pytest`, excluded `starter_code/` from lint/type.
