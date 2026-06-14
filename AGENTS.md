# AGENTS.md — Omni Scoreboard

Read this first before changing code.

## Project intent

Omni Scoreboard is a friends-and-family LED sports scoreboard. It is not a full consumer SaaS/product company.

Prioritize reliability, local setup, typed code, fixture replay, and delightful small-display behavior over feature sprawl.

## Status & repository map

This file is the **single, agent-agnostic source of truth**. Tool-specific configs are thin
wrappers that defer here: `CLAUDE.md` (Claude Code) and `.cursor/rules/` (Cursor). Harness
config (permissions/skills) lives in `.claude/` but carries no project knowledge.

- **Current status:** `docs/agent_context/STATUS.md` (living — update when state changes).
- **Deep context:** `docs/agent_context/` — `ROADMAP.md`, `ARCHITECTURE_TYPED_DOMAIN.md`, `BACKLOG.yaml`, `SOURCES.md`.
- **Dev environment:** uv + project-local `.venv`; software emulator, no Pi needed — `docs/dev_setup.md`.
- **Code:** `main.py` (entry), `renderers/`, `data/`, local packages `bullpen/ standings/ news/`; `starter_code/` holds typed-domain sketches to grow into `omni/`.
- **Reference hardware:** a physical `quad_128x64` (2×2) board runs headless on the LAN (`screen` + `run.sh`); its SSH target is machine-local (`CLAUDE.local.md`, git-ignored). Use the `run-on-board` skill. It currently runs the pre-revival code.

## Operating notes

- **Commit identity:** author as `ajszoke <aszoke1@gmail.com>`, set **repo-local** (not global). Push over **HTTPS + `gh`** — SSH may resolve to a different GitHub identity on some machines. Never force-push `main`.
- **Never commit:** secrets, `config.json`, `emulator_config.json`, `.venv/`, build artifacts/caches.
- **Knowledge-base hygiene (keep it ingestion-optimal):** one concept per doc; keep this map lean (links, not prose dumps); prune docs for shipped/abandoned work; promote a thrice-repeated fact into a doc and a repeated procedure into a `.claude/skills/` skill; verify a referenced file/flag still exists before relying on it. Claude Code's machine-local auto-memory supplements but never overrides these committed docs.

## Supported displays

All significant UI work must consider these three profiles equally:

- `single_64x32`: 1x1 64x32 panel.
- `stack_64x64`: 2x1 vertical stack of two 64x32 panels, logical 64x64.
- `quad_128x64`: 2x2 matrix, logical 128x64.

A feature may compromise by profile, but the compromise must be explicit and tested.

## Repo posture

The revived repo should use `omni-scoreboard`, not `omni-scoreboard-v2` or similar.

The old fork should be preserved as `omni-scoreboard-legacy`. The new repo should be based on current upstream `MLB-LED-Scoreboard/mlb-led-scoreboard`, with old Omni work ported selectively.

Do not blindly merge the legacy fork into upstream. Use legacy code as reference/prototype material.

## Immediate priorities

1. Stand up clean repo/local clone.
2. Run upstream in emulator.
3. Add typed enum/domain foundation.
4. Add display profile model for 64x32, 64x64, 128x64.
5. Add fixture replay and snapshot tests.
6. Fix MLB P0/P1 bugs before broad league expansion.
7. Implement generic delay/event/card queue.
8. Add setup portal, safe updater, sleep schedule, and physical button support.
9. Add NFL, NBA, NHL, PGA, then NCAA later.

## Type policy

Do not introduce new raw stringly-typed league/team/event/card concepts.

Use typed enums and frozen/slotted dataclasses:

- `League`, not `"mlb"`.
- `BaseballTeam`, not `"COL"`.
- `BaseballGameEventType.HOME_RUN`, not `"home_run"`.
- `PanelProfile.SINGLE_64X32`, not `(64, 32)`.
- `DisplayTiming`, not loose `available_at`, `expires_at`, `min_seconds`, `max_seconds` primitives scattered across code.

Raw JSON may exist only in provider boundary modules and fixtures.

## Rendering policy

Renderers should consume typed `ScoreboardCard` payloads and a `PanelProfile`. They should not fetch data. They should not parse provider JSON. They should not own TV-delay semantics.

Every spacing/animation bug fixed should get a fixture or golden-image snapshot test.

## Data policy

Free/low-cost by default:

- MLB: upstream MLB StatsAPI integration.
- NFL/NBA/NHL/PGA initial spike: ESPN site APIs, isolated behind provider interfaces.
- PGA probability/cut-line optional: DataGolf if worth the key/cost.
- Commercial fallback only after free sources fail product needs.

## Device policy

For friends/family units:

- Local Wi-Fi setup portal, not mobile app.
- Safe `systemd` updater with rollback, not cron `git pull`.
- Local config and credentials.
- Sleep/wake schedule.
- Physical switch/button support.
- Avoid cloud dependencies.

## Never do these without explicit human approval

- Delete or force-push legacy branches.
- Rewrite published history on `main`.
- Add paid data/API dependency as required default.
- Add user accounts/cloud backend.
- Drop support for any of the three display profiles.
- Commit secrets, Wi-Fi credentials, or API keys.
