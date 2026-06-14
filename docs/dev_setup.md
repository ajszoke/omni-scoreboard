# Dev setup — Omni Scoreboard (emulator)

Local development runs the scoreboard against the **RGBMatrixEmulator** — no Raspberry Pi
hardware required. Verified on WSL2 (Ubuntu, headless) with Python 3.12.3.

## Dependency isolation: `uv` + project-local `.venv`

We use [uv](https://docs.astral.sh/uv/) for the dev environment. It drives the existing
`requirements.txt`, which stays the **upstream + device-facing contract** (so pulling from
`MLB-LED-Scoreboard/mlb-led-scoreboard` upstream stays friction-free). The virtual env
lives at `.venv/` and is git-ignored.

Install `uv` (user-local, no sudo — lands in `~/.local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create the venv and install deps. **Run from the repo root** so the local path deps
(`./bullpen`, `./standings`, `./news`) resolve:

```bash
uv venv .venv --python 3.12
uv pip install -r requirements.txt
```

Optional — generate a hash-pinned lock for reproducible device flashes (does not change
`requirements.txt`):

```bash
uv pip compile requirements.txt -o requirements.lock
```

## Running the emulator

Seed a local config the first time (git-ignored):

```bash
cp config.example.json config.json
```

Run in software-emulation mode:

```bash
.venv/bin/python main.py --emulated
# or: source .venv/bin/activate && ./main.py --emulated
```

The emulator uses the `browser` display adapter by default (see `emulator_config.json`,
auto-created on first run, git-ignored) and serves the panel at:

```
http://localhost:8888/
```

### Display profiles

Default panel is 32x32. For the Omni target profiles, pass matrix dimensions, e.g.:

```bash
.venv/bin/python main.py --emulated --led-cols 64 --led-rows 32   # single_64x32
```

(64x64 and 128x64 profiles will be wired through the typed `PanelProfile` model — see
`docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md`.)

## Headless / WSL notes

- The `browser` adapter needs **no X display**. Avoid the `pygame` adapter on headless
  machines (no video device). `allow_adapter_fallback` is on by default.
- `config.json`, `emulator_config.json`, and `.venv/` are git-ignored.
- Benign first-run warnings: placeholder OpenWeather API key, and optional
  `colors/*.json` / `coordinates/*.json` overrides falling back to bundled defaults.

## Raspberry Pi devices

For physical units, use upstream `install.sh` (it creates its own `venv/` and installs the
`rpi-rgb-led-matrix` driver from `requirements.rpi.txt`). That device path is unchanged by
the `uv` dev workflow above:

```bash
sudo ./install.sh            # full hardware install
sh install.sh --no-sudo --emulator-only   # emulator-only, no hardware drivers
```
