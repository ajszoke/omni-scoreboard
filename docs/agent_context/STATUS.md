# Status — Omni Scoreboard

_Living status doc. Keep it short; update whenever project state changes (branch, PR, milestone). This is the first thing an agent should read after `AGENTS.md`._

## Now (2026-06-17)

- Revived repo **bootstrapped from upstream MLB-LED-Scoreboard v9.1.5**; `main` = upstream history + agent context (PR #1) + typed core (PR #2) + domain objects (PR #3).
- Remotes: `origin` = `ajszoke/omni-scoreboard` (HTTPS), `upstream` = `MLB-LED-Scoreboard/mlb-led-scoreboard` (branch `master`), `legacy` = `ajszoke/omni-scoreboard-legacy` (pre-revival Omni work: `legacy/master`, `legacy/dev`).
- **PRs #1–#3 merged and cleaned up** (context, `omni/core/`, `omni/domain/`). Feature branches deleted locally and on `origin`.
- **Events layer in progress** on branch `agent/events-layer`: `omni/events/` — `base.py` (generic `GameEvent[EventType, Payload]` + `EventImportance`), `baseball.py` (full vertical: event-type enum + `BaseballCount`/`BaseballPlayPayload`/`BaseballGameEvent`), and per-sport event-type enums for football/basketball/hockey/golf (taxonomy only). Tests in `tests/test_events.py` + `tests/test_event_enums.py`. Green locally. Pending review/merge.
- Dev env: **uv + `.venv`** (Py 3.12); emulator runs headless (`http://localhost:8888/`). `pytest` pinned in `requirements.dev.txt`; `starter_code/` excluded from `mypy`/`black` as reference sketches. See `docs/dev_setup.md`.
- Physical **`quad_128x64` reference board** is live on the LAN running the pre-revival code; reachable from WSL (`ssh omni-board`). Details in `CLAUDE.local.md`. Deploying the revived repo to it is future work.

## Next

- **Land the events PR** (branch `agent/events-layer`).
- Then the **first typed card + renderer** (migration steps 3–4 in `docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`): a typed `ScoreboardCard` for one MLB live game (`omni/cards/`), then the `Renderer` contract drawing it across all three profiles (`single_64x32` / `stack_64x64` / `quad_128x64`) with a fixture/golden-image snapshot test. This is where the three-profile policy and the layout contract get exercised for real. See `ROADMAP.md` and `BACKLOG.yaml`.

## Done

- Renamed old fork → `omni-scoreboard-legacy` (history preserved).
- Created revived `omni-scoreboard` (Path B: normal repo; `upstream` remote wired for syncing).
- Cloned to `/opt/omni-scoreboard`, set remotes, pushed `main`.
- **PR #1 merged** — agent context pack + project-local Claude Code.
- **PR #2 merged** — typed-domain core (`omni/core/`: enums, ids, time, colors + tests); pinned `pytest`, excluded `starter_code/` from lint/type.
- **PR #3 merged** — typed domain objects (`omni/domain/`: `Competitor`/`Team`/`Contest` + tests).
