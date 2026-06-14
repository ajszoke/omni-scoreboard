---
name: run-emulator
description: Run the Omni Scoreboard in the RGBMatrixEmulator (headless browser adapter) to see/verify rendering without Raspberry Pi hardware. Use when asked to run, start, screenshot, or visually check the scoreboard.
---

# Run the emulator

Software emulation — no Pi hardware required. Canonical setup: `docs/dev_setup.md`.

Ensure the project-local env exists (see `docs/dev_setup.md`):

```bash
uv venv .venv --python 3.12 && uv pip install -r requirements.txt   # first time only
cp -n config.example.json config.json                                # seed local config (gitignored)
```

Run (browser adapter serves the panel at http://localhost:8888/):

```bash
.venv/bin/python main.py --emulated
# Omni panel profiles via matrix flags, e.g. single_64x32:
# .venv/bin/python main.py --emulated --led-cols 64 --led-rows 32
```

Non-interactive smoke test (bound it so it self-stops):

```bash
timeout 20 .venv/bin/python main.py --emulated
```

Exit 124 = it ran fine until the timeout. Confirm logs show the version banner, data fetch,
and `Server started ... http://localhost:8888/`.
