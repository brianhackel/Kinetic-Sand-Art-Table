# OctoPrint LED Control

An OctoPrint 2.0 plugin that puts the sand table's LED controls (pattern, brightness, speed, color, serial log) in the OctoPrint **sidebar**. It talks to the Arduino Nano running `LED-Arduino/LED-Control` over its own USB serial port. It replaces the standalone `../led-web` Flask app, whose serial/persistence code it reuses (`octoprint_ledcontrol/led_link.py`).

## Layout

```
pyproject.toml
octoprint_ledcontrol/
  __init__.py        plugin class: settings, permission, API routes, print-event hooks
  led_link.py        serial link, saved settings, command logic (no OctoPrint imports)
  templates/         sidebar + settings pane
  static/            sidebar view model (Knockout) and CSS
scripts/
  deploy.sh          rsync to the Pi for day-to-day iteration
  build-archive.sh   zip for Plugin Manager's "from an uploaded file"
```

## First install on the Pi

1. Stop the old app so the plugin can own the serial port:
   `sudo systemctl disable --now led-web`
2. Build the archive: `scripts/build-archive.sh` -> `dist/OctoPrint-LedControl-<version>.zip`
3. In OctoPrint: Settings -> Plugin Manager -> Get More... -> "...from an uploaded file", pick the zip, install, restart OctoPrint.
4. Settings -> LED Control: set **Serial port** to the Nano's `/dev/serial/by-id/...` path (not the GRBL Uno's).
5. The OctoPrint user must be in `dialout` (already true for `pi` on OctoPi).

The LED state (`led-state.json`) lives in OctoPrint's plugin data folder (`~/.octoprint/data/ledcontrol/`). Copy the old file there to keep your remembered per-pattern settings.

## Updating while developing

The archive install is a one-off. After that, use `deploy.sh`, which rsyncs the source to the Pi and keeps an editable install pointing at it, so no reinstall is needed per change.

```
cp scripts/deploy.env.example scripts/deploy.env   # set PI_HOST (needs ssh key access)
scripts/deploy.sh --install      # first time: rsync + pip install -e into OctoPrint's venv
scripts/deploy.sh                # every change after that: rsync only
scripts/deploy.sh --dry-run      # preview
scripts/deploy.sh --restart      # also restart OctoPrint via ssh (needs passwordless sudo)
```

Then restart OctoPrint from the web UI (power menu -> Restart OctoPrint) to load Python changes. JS/CSS/template edits usually need only a hard browser refresh.

If you installed from the archive first, `--install` makes pip replace that copy with the editable one.

**Roll back:** `git checkout <good commit> -- RaspberryPi/octoprint-led`, run `scripts/deploy.sh`, restart OctoPrint.

`pip install -e` re-runs automatically only when `pyproject.toml` changes (the Pi keeps a checksum in `.installed-sum`).

## Trying it without hardware

On a dev machine with a venv containing OctoPrint 2.0:

```
pip install -e .
octoprint serve
```

Then enable "Simulate the Nano" in Settings -> LED Control.

## Behavior notes

- Controls need the **Control LEDs** permission (granted to Admin and User groups by default).
- Optional settings: switch lights on at print start, Off after a print ends.
- Changing the port/baud/simulate settings reconnects immediately.
- Pattern names and limits are duplicated from the Arduino sketch (`PATTERNS`, `MAX_BRIGHTNESS` in `led_link.py`). Keep them in sync, as with `led-web`.
