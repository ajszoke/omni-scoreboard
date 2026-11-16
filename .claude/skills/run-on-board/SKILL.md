---
name: run-on-board
description: Run, check, or attach to the Omni Scoreboard on the physical 2x2 (quad_128x64) Raspberry Pi board over SSH. Use when asked to deploy to, run on, restart, attach to, or check the real hardware panel.
---

# Run on the physical board (quad_128x64)

There is a real 2x2 board (logical 128x64) on the LAN. **Its SSH target is machine-local** — it
is the `omni-board` host alias in `~/.ssh/config`, documented in `CLAUDE.local.md` (both
git-ignored / NOT in this public repo). Always use the alias, never a hardcoded IP, in
committed files. Key-based SSH is set up, so commands can run non-interactively from WSL.

Check status (read-only, safe — do this first):

```bash
ssh omni-board 'whoami; hostname; screen -list | grep -i omni || echo "(no omni session)"'
```

Attach to the live session (interactive):

```bash
ssh -t omni-board 'screen -x omni'
```

Start headless only if not already running (the canonical one-liner — launches the board's
runner in a detached screen named `omni`):

```bash
ssh omni-board "screen -list | grep -q '\\.omni' && screen -x omni || screen -dmS omni bash -c 'cd ~/omni-scoreboard && ./run.sh'"
```

Show one live NFL game (until football joins the main app). The app renders here and
pipes frames to `tools/board_frame_sink.py`, which owns the panel under the board's system
Python (it is 3.9 there, and the app needs 3.12). Copy the sink over once, then:

```bash
ssh omni-board 'mkdir -p ~/omni' && scp tools/board_frame_sink.py omni-board:omni/
.venv/bin/python -m omni.app.football --profile quad_128x64 --teams PIT,CIN \
    --sink-command "ssh -o ServerAliveInterval=15 omni-board sudo python3 omni/board_frame_sink.py"
```

The sink blanks the panel when the stream ends, and the app restarts it if SSH drops (and resends
the current frame to the new display process).

Opt-in flags:
- `--odds`: the win meter from the market. It is experimental: its lag is measured from request time,
  so it can lead the feed.
- `--record`: keeps NFLMeta's responses under `build/recordings/nflmeta/` to diagnose the feed.
  NFLMeta's terms allow no standing copy, so delete recordings after use.
- `--play-feed every-poll`: reads the play feed every tick, for about twice the requests (suited to a
  paid quota).

**Cautions**
- This drives a PHYSICAL panel and is often already running — check first; never restart casually.
- The board currently runs the **pre-revival** code at `~/omni-scoreboard` on the Pi via
  `run.sh`. Deploying *this* revived repo to the board is future work (ROADMAP: safe updater).
  Until then treat the board as the legacy/reference runtime for the 128x64 profile.
- Stop a session intentionally (blanks the panel): `ssh omni-board 'screen -S omni -X quit'`.
