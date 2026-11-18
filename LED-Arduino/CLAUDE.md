# LED-Arduino

Single sketch: `LED-Control/LED-Control.ino`. Runs on an Arduino Nano driving a WS2812B strip; controlled over USB serial by the Pi's web app (`RaspberryPi/led-web`, see its CLAUDE.md).

## Hardware / build
- Strip data on pin 6, `LED_COUNT` = 60. Library: WS2812FX (install via Library Manager). `NEO_GRB + NEO_KHZ800`.
- Serial 115200 baud. No test harness; compile/upload with Arduino IDE or `arduino-cli`.
- The Nano resets whenever the serial port is opened (DTR), then prints `READY`. The web app depends on that.

## Serial protocol (the contract with led-web)
One command per line, `\n` terminated, `\r` ignored, max 23 chars (longer -> `ERR line too long`). Empty lines are silently ignored (the Pi uses a bare `\n` to flush a half-received line).

| Cmd | Meaning |
|---|---|
| `P <n>` | pattern 0..10; resets color to that pattern's default |
| `B <n>` | brightness 0..255, clamped to `maximumBrightnesAllowed` (128) |
| `S <n>` | speed ms/step, clamped 2..10000 (lower = faster) |
| `C <rrggbb>` | color, exactly 6 hex digits, until next `P` |
| `?` | reply `STATE P=<n> B=<n> S=<n> C=<RRGGBB>` |

Replies: `OK`, `ERR bad command`, `ERR bad color`, `ERR pattern out of range`, `ERR line too long`.

## Patterns (index = `patterns[]` order; led-web's `PATTERNS` list in app.py must match)
0 Chase Rainbow, 1 Fade, 2 Color Wipe, 3 Color Wipe Random, 4 Rainbow, 5 Running Lights, 6 Sparkle, 7 Color Sweep Random, 8 Larson Scanner, 9 Fire Flicker, 10 Static.
Each entry has a default color (see the table in the sketch).

## Gotchas
- WS2812FX in this version treats brightness 0 as "no scaling" (full bright), so the sketch passes `max(currentBrightness, 1)` to the library while reporting the real value (0 = effectively off).
- Strip redraws disable interrupts; very fast speeds (e.g. 2 ms) cause dropped serial bytes. led-web works around this by running Static at speed 30.
- Boot state: pattern 0, brightness 64, speed 1000. The Pi re-sends saved settings after every READY.

## Keeping in sync
Changing patterns, `maximumBrightnesAllowed`, speed limits, boot defaults, or the protocol requires matching edits in `RaspberryPi/led-web/app.py` (`PATTERNS`, `MAX_BRIGHTNESS`, `NANO_DEFAULT_BRIGHTNESS`, `MIN_SPEED/MAX_SPEED`, `SPEED_AGNOSTIC`, `COLOR_AGNOSTIC`).
