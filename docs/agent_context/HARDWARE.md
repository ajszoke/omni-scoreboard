# Hardware — friends-and-family boards

_Scouted 2026-11-25 for the production run of family boards. Prices move, so re-check them before any order._

## The reference board

The reference board is a Raspberry Pi 3 Model A+ (512 MB, `isolcpus` on core 3), driving four 64x32 panels in a U (`quad_128x64`) through an Adafruit HAT with the GPIO 4–18 PWM mod (`adafruit-hat-pwm`).
- **Today:** it runs only the display process (`tools/board_frame_sink.py`); the app renders on a dev machine.
- **The panels:** the original Adafruit panels are no longer sold.

## Panels

**Waveshare RGB-Matrix-P4-64x32** (Amazon B0B2ZC85KN), the user's pick for new boards:

| | |
|---|---|
| Resolution | 64x32 |
| Pitch | 4 mm |
| Size | 256x128 mm |
| Scan | 1/16 |
| Connectors | HUB75 in and out |
| Power | up to 20 W (5 V, 4 A) a panel |
| In the box | a 16-pin ribbon and a power-terminal adapter |
| Price, Waveshare direct | about $16–18 |

**Profile sizes with these panels:** single 256x128 mm, stack 256x256 mm, quad 512x256 mm.
- A quad is larger than most print beds, so a printed frame comes in sections.
- Supply about 4 A a panel at 5 V: 16 A for a quad.

**Panel type:** Waveshare's pages for this panel name no driver chip, and batches vary (Waveshare's P5 panel asks for FM6127).
- Bring the first panel up with no panel type. If it stays dark, try `--led-panel-type=FM6126A`, then `FM6127`.
- `tools/board_frame_sink.py` doesn't pass a panel type yet.

## Pi and adapter

**What decides the Pi is render cost.** Median milliseconds a frame on one core, measured 2026-11-25:

| | Quad NFL | Quad NBA | Stack NFL | Single NFL |
|---|---|---|---|---|
| Pi 3A+, before #131 | 104 | 82 | 61 | 39 |
| Pi 3A+, after #131 | 11.7 | | | |
| i7-1360P, before #131 | 2.6 | 2.2 | 1.6 | 1.0 |

- **Why the drop:** before #131, about 55% of a frame was re-rasterizing the same text in pure Python, and about 17% was recomputing each matchup's visual treatment. Caching both and pasting in C took a quad frame from 104 ms to 11.7 ms, within the app's 10 fps.
- **The Pi Zero 2 W:** the same A53 cores, estimated at about 16 ms a quad frame, so it fits every profile.
- **Still to do on the device:** `MatrixDisplaySink`/`MatrixCanvas` set pixels one at a time from Python. Render into `PillowCanvas` and push one `SetImage`, as the sink does.

**Pis, 2026:**

| Model | Price | Notes |
|---|---|---|
| Zero 2 W | $15 | no 2026 rise; LPDDR2 stock; in production to at least 2030 |
| Pi 3 line | | no 2026 rise; in production to at least 2030 |
| Pi 4, 2 GB | about $67.50 | rose with memory costs from October 2026 |
| Pi 5, 2 GB | about $77.50 | rose with memory costs; the 16 GB is $385 from 2 Nov 2026 |

**Adapter:** the Adafruit Triple LED Matrix Bonnet (#6358), $9.95.
- It has three chains and works on Pi 0–5, but has no onboard power.
- Its regular and `active3` pinouts put OE on GPIO 18, so hardware pulses need no jumper.
- A three-chain build needs `--parallel` and the `regular` GPIO mapping in the sink, which doesn't pass them yet.

**Drivers:**
- hzeller's rpi-rgb-led-matrix supports Pi 1–5. The Pi 5 uses the RP1's RIO or PIO backends; the README's own status note on it is inconsistent.
- Adafruit's Piomatter, for the Pi 5, is alpha.
- `requirements.rpi.txt` pins hzeller at 41809e4. Upstream moved to 39e03bc in 2026-11, so take that bump when setting up the family boards, and smoke-test the sink on it.

## Leading choice (to confirm with a prototype)

- **The board:** a Pi Zero 2 W plus the Triple Bonnet plus Waveshare P4 panels, with a 5 V supply sized to the panel count.
- **The prototype:** one Zero 2 W and one bonnet, to bring up the app on the device (STATUS, Next 2) and settle the panel type.
