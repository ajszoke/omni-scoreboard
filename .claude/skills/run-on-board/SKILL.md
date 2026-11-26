---
name: run-on-board
description: Run, check, or stop the Omni Scoreboard on the physical 2x2 (quad_128x64) Raspberry Pi board over SSH. Use when asked to deploy to, run on, restart, stop or check the real hardware panel.
---

# Run on the physical board (quad_128x64)

A real 2x2 board (logical 128x64) is on the LAN. **Its SSH target is machine-local:** it's the `omni-board`
host alias in `~/.ssh/config`, documented in `CLAUDE.local.md`. Both are git-ignored and not in this public repo.
Always use the alias in committed files, never a hardcoded IP. Key-based SSH is set up, so commands run
non-interactively from WSL.

## How it runs today

**On the dev machine:** the shared app (`python -m omni.app`) renders every frame and pipes it over SSH.

**On the Pi:** `tools/board_frame_sink.py` is the display process. It runs as root under the Pi's system Python
(3.9, which has the `rgbmatrix` bindings).
- It loads the wire protocol from `omni/app/receiver.py` beside it.
- The Pi's `~/omni` holds a copy of the sink and of the repo's `omni/` package.
- It acknowledges each frame it shows, and it blanks the panel if the app goes quiet for 8 s or the stream ends.

The pre-revival code (`~/omni-scoreboard`, `run.sh`, a `screen` session) no longer runs.

**Check first (read-only, safe):**
```bash
pgrep -af "^/opt/omni-scoreboard/.venv/bin/python -m omni.app --profile"   # the app, here
ssh omni-board 'pgrep -af board_frame_sink.py | grep -v pgrep || echo "(no sink)"'
```

**Re-sync the Pi's copy after changing the receiver or the sink:**
```bash
rsync -a --delete omni/ omni-board:omni/omni/ && scp tools/board_frame_sink.py omni-board:omni/
```

**Launch** from the **main checkout** (`/opt/omni-scoreboard`). The expiry sweep's roots are the checkout's own
`build/` folders, so a board run from a worktree sweeps the wrong ones. The user's defaults are:
- odds on (`--meter`);
- policy 2 (`--board-policy 2`);
- the user's favorites;
- `--scroll-speed medium`;
- 40% brightness.

```bash
cd /opt/omni-scoreboard && setsid -f .venv/bin/python -m omni.app --profile quad_128x64 \
    --leagues nfl,nba --board-policy 2 --nflmeta-plan builder --balldontlie-plan all-star --meter \
    --favorite nfl:CAR --favorite nba:CHA --scroll-speed medium --timezone America/Denver \
    --sink-command "ssh -o ServerAliveInterval=15 omni-board sudo python3 omni/board_frame_sink.py --brightness 40" \
    >> build/logs/round-robin-live.log 2>&1
```

**Leagues:** add `mlb` to `--leagues` in season. NFL needs `NFLMETA_API_KEY` and NBA needs `BALLDONTLIE_API_KEY`,
both read from the git-ignored `.env`. Never read or print that file; the app reads it.

**After launch,** check the log: its `omni.app:` line should say `meter on` for every league. Then check that
cards go `up` with a meter color for each side.

**Stop** gracefully. The sink blanks the panel as the stream ends.
```bash
kill -INT "$(pgrep -f '^/opt/omni-scoreboard/.venv/bin/python -m omni.app --profile')"
```

**NFLMeta's lock:** only one process at a time polls NFLMeta. While the board runs NFL, `tools/nflmeta_record.py`
and the single-game runner (`python -m omni.app.football`, NFL's fallback) refuse to start.

## Cautions

- This drives a **physical** panel that is often already running. Check first, and never restart casually.
- The user sometimes powers the Pi down by pulling the plug. If SSH times out, ask before assuming it's broken.
- **Next for the board:** running the app on the Pi itself, with no dev machine in the loop. That's the
  friends-and-family path, through a safe updater.
