# led-web

Flask app (single file `app.py` + `templates/index.html`) that controls the table LEDs via the Arduino Nano running `LED-Arduino/LED-Control` over USB serial. Serial protocol is documented in `LED-Arduino/CLAUDE.md` and at the top of the sketch. No auth; LAN only. No tests.

## Run
- No hardware: `python3 -m venv venv && venv/bin/pip install -r requirements.txt && LED_FAKE=1 venv/bin/python app.py` -> http://localhost:5001
- On the Pi: `./install-service.sh /dev/serial/by-id/<nano>` fills in `led-web.service` (template with `__USER__`/`__DIR__`/`__LED_PORT__`), installs it, runs gunicorn with **one worker** (one process must own the serial port), 4 threads, port 5001. Logs: `journalctl -u led-web -f`.
- Env vars: `LED_PORT` (default /dev/ttyUSB0), `LED_PORT_NUM` (5001; OctoPi uses 80/5000), `LED_FAKE=1`, `LED_STATE_FILE` (default `led-state.json` next to app.py, gitignored runtime state).

## app.py structure
- Constants mirroring the sketch: `PATTERNS` (11 names, same order as `patterns[]`), `MAX_BRIGHTNESS`=128, `NANO_DEFAULT_BRIGHTNESS`=64, `MIN_SPEED/MAX_SPEED`=2/10000. Must stay in sync with the .ino.
- `OFF` = len(PATTERNS): a faux pattern the Nano doesn't know; selecting it sends `B 0` and sets `settings.off`, leaving remembered settings untouched.
- `SPEED_AGNOSTIC` (Static): runs at `BOOST_SPEED`=30 so color/brightness changes show promptly (not 2 ms: dropped serial bytes). User's speed is still remembered; `/api/speed` skips the Nano for these. `speed_for()` picks the speed to send.
- `COLOR_AGNOSTIC` (Chase Rainbow, Rainbow, Color Wipe Random, Color Sweep Random): color never sent/restored; UI disables the picker.
- `SavedSettings`: persists to JSON (atomic write via tmp + `os.replace`). `values` = current settings; `per_pattern` = each pattern's own brightness/speed/color, restored when reselected (`recall`/`switch_pattern`); `off` flag. `commands()` builds the sequence to re-sync a freshly reset Nano (order matters: P first because P resets color). Loads defensively and validates every field.
- `SerialLog`: 500-entry ring buffer (kinds tx/rx/drop/info) served at `/api/log?since=<id>`.
- `LedLink`: owns the port under a lock. `_open` waits up to 5 s for `READY` then `_restore()`s saved settings. `_exchange` first sends a bare `\n` to flush any half line on the Nano, discards leftover input, then sends the command and reads one reply. `send` retries once on `RETRYABLE_ERRORS` (garbled line); on any exception closes the port and raises `LinkError` (reopened next call). `FakeLink` mimics the Nano.
- `command(line, remember, extra)`: sends line + follow-ups, only calls `remember()` if all succeed; LinkError -> 503, `ERR*` reply -> 502.
- Routes: `GET /`, `GET /api/state` (reports user's speed, and pattern=OFF when off), `GET /api/log`, `POST /api/pattern|brightness|speed|color` with JSON bodies `{pattern}`, `{brightness}`, `{speed}`, `{color:"#rrggbb"}`; 400 on invalid input.
- A daemon thread (`restore_on_startup`) retries `?` every 5 s until the Nano is reachable so settings are restored at boot without a web request.

## templates/index.html
Single page, vanilla JS, Jinja injects constants (`max_brightness`, speed limits, `speed_agnostic`, `color_agnostic`, `off_pattern`). Speed slider is 0..100 on a log scale mapped to ms (`sliderToSpeed`/`speedToSlider`; 100 = fastest). Slider/picker inputs are debounced 100 ms. After a pattern change it re-reads `/api/state` (server applies remembered settings); `refreshId` discards stale reads. `updateEnabled()` disables controls for Off / agnostic patterns. Collapsible serial log panel polls `/api/log` only while open.

## Gotchas
- Adding/reordering a pattern: update the sketch's `patterns[]`, `PATTERNS`, and the agnostic sets together.
- Don't run multiple gunicorn workers.
- Saved-state JSON format: top-level pattern/brightness/speed/color + `off` + `patterns` {index: {brightness,speed,color}}; older files without `patterns` are migrated on load.
