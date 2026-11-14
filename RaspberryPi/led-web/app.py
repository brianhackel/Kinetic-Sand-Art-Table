"""Small web interface for the table LEDs.

Talks to the Arduino Nano running LED-Arduino/LED-Control over USB serial.
See the protocol description at the top of LED-Control.ino.

Environment:
  LED_PORT  serial device (default /dev/ttyUSB0; /dev/serial/by-id/... is more stable)
  LED_PORT_NUM  HTTP port (default 5001; OctoPi already uses 80 and 5000)
  LED_FAKE=1  use a simulated Nano, for trying the UI without hardware
  LED_STATE_FILE  where settings are remembered across restarts
                  (default led-state.json next to this file)
"""
import json
import os
import re
import threading
import time

from flask import Flask, jsonify, render_template, request

# Must match the patterns[] table in LED-Control.ino
PATTERNS = [
    "Chase Rainbow",
    "Fade",
    "Color Wipe",
    "Color Wipe Random",
    "Rainbow",
    "Running Lights",
    "Sparkle",
    "Color Sweep Random",
    "Larson Scanner",
    "Fire Flicker",
    "Static",
]
MAX_BRIGHTNESS = 128  # must match maximumBrightnesAllowed in the sketch
MIN_SPEED, MAX_SPEED = 2, 10000
# Speed doesn't change how these look, but the Nano only redraws once per speed interval, so
# color/brightness changes lag. Run them at the fastest tick; the user's speed is still remembered.
SPEED_AGNOSTIC = {PATTERNS.index("Static")}
# These pick their own colors, so the color picker has no effect (the page disables it).
COLOR_AGNOSTIC = {PATTERNS.index(n) for n in ("Chase Rainbow", "Rainbow", "Color Wipe Random", "Color Sweep Random")}


def speed_for(pattern, saved):
    """The speed the Nano should run at: MIN_SPEED for speed-agnostic patterns, else the saved one (or None)."""
    return MIN_SPEED if pattern in SPEED_AGNOSTIC else saved

STATE_RE = re.compile(r"STATE P=(\d+) B=(\d+) S=(\d+) C=([0-9A-Fa-f]{6})")


class LinkError(Exception):
    pass


def _is_int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


class SavedSettings:
    """The last settings sent to the Nano, kept in a JSON file so they survive reboots."""

    # setting name -> (serial command letter, validator)
    FIELDS = {
        "pattern": ("P", lambda v: _is_int(v, 0, len(PATTERNS) - 1)),
        "brightness": ("B", lambda v: _is_int(v, 0, MAX_BRIGHTNESS)),
        "speed": ("S", lambda v: _is_int(v, MIN_SPEED, MAX_SPEED)),
        "color": ("C", lambda v: isinstance(v, str) and re.fullmatch(r"[0-9A-F]{6}", v) is not None),
    }

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.values = {}
        try:
            with open(path) as f:
                saved = json.load(f)
            self.values = {k: v for k, v in saved.items() if k in self.FIELDS and self.FIELDS[k][1](v)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            print("ignoring unreadable settings file %s: %s" % (path, e))

    def update(self, name, value):
        with self.lock:
            self.values[name] = value
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self.values, f)
                os.replace(tmp, self.path)  # atomic, so a power cut can't leave a half-written file
            except OSError as e:
                print("could not save settings to %s: %s" % (self.path, e))

    def commands(self):
        """Commands to bring a freshly reset Nano to the saved state (pattern first: P resets the color)."""
        with self.lock:
            values = dict(self.values)
        values["speed"] = speed_for(values.get("pattern"), values.get("speed"))
        return ["%s %s" % (self.FIELDS[k][0], values[k])
                for k in ("pattern", "brightness", "speed", "color") if values.get(k) is not None]


settings = SavedSettings(os.environ.get("LED_STATE_FILE") or
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "led-state.json"))


class LedLink:
    """Owns the serial port; one command at a time."""

    def __init__(self, port, baud=115200):
        self.port = port
        self.baud = baud
        self.ser = None
        self.lock = threading.Lock()

    def _open(self):
        import serial  # imported lazily so LED_FAKE works without pyserial

        ser = serial.Serial(self.port, self.baud, timeout=1)
        # The Nano resets when the port opens; wait for its READY line.
        deadline = time.time() + 5
        while time.time() < deadline:
            if ser.readline().strip() == b"READY":
                self.ser = ser
                self._restore()
                return
        ser.close()
        raise LinkError("Nano did not send READY")

    def _exchange(self, command):
        """Send one command and return its reply (empty on timeout)."""
        # The Nano can be left holding half a line if it dropped a byte; the next command would
        # be glued onto it and rejected (e.g. "P 1" arriving as "C FFFFFP 1" -> "ERR bad color").
        # A bare newline makes it dispatch (and reject) the leftover; empty lines are ignored.
        self.ser.write(b"\n")
        time.sleep(0.02)
        self.ser.reset_input_buffer()  # drops the reply to the leftover, if there was one
        self.ser.write((command + "\n").encode("ascii"))
        return self.ser.readline().decode("ascii", "replace").strip()

    def _restore(self):
        """Re-send the remembered settings; the Nano starts from its defaults after every open."""
        for line in settings.commands():
            reply = self._exchange(line)
            if reply != "OK":
                print("restoring %r failed: %r" % (line, reply))

    def send(self, command):
        with self.lock:
            try:
                if self.ser is None:
                    self._open()
                reply = self._exchange(command)
            except Exception as e:  # serial.SerialException, OSError, LinkError
                if self.ser is not None:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                raise LinkError(str(e))
        if not reply:
            raise LinkError("no reply from Nano")
        return reply


class FakeLink:
    """Mimics the Nano's replies."""

    def __init__(self):
        self.p, self.b, self.s, self.c = 0, 64, 1000, 0xFFFFFF
        self.lock = threading.Lock()
        for line in settings.commands():
            self.send(line)

    def send(self, command):
        with self.lock:
            cmd, arg = command[:1], command[1:].strip()
            if cmd == "?":
                return "STATE P=%d B=%d S=%d C=%06X" % (self.p, self.b, self.s, self.c)
            if cmd == "P":
                self.p = int(arg)
            elif cmd == "B":
                self.b = int(arg)
            elif cmd == "S":
                self.s = int(arg)
            elif cmd == "C":
                self.c = int(arg, 16)
            else:
                return "ERR bad command"
            return "OK"


link = FakeLink() if os.environ.get("LED_FAKE") else LedLink(os.environ.get("LED_PORT", "/dev/ttyUSB0"))
app = Flask(__name__)


def command(line, name, value, extra=()):
    """Send a command (plus any follow-ups) and remember the setting, turning link/Arduino failures into HTTP errors."""
    for l in (line, *extra):
        try:
            reply = link.send(l)
        except LinkError as e:
            return jsonify(error=str(e)), 503
        if reply.startswith("ERR"):
            return jsonify(error=reply), 502
    settings.update(name, value)
    return jsonify(ok=True)


def int_field(name, lo, hi):
    """Read an integer from the JSON body; returns (value, None) or (None, error response)."""
    value = (request.get_json(silent=True) or {}).get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, (jsonify(error="%s must be an integer %d..%d" % (name, lo, hi)), 400)
    return value, None


@app.route("/")
def index():
    return render_template("index.html", patterns=PATTERNS, max_brightness=MAX_BRIGHTNESS,
                           min_speed=MIN_SPEED, max_speed=MAX_SPEED,
                           speed_agnostic=sorted(SPEED_AGNOSTIC), color_agnostic=sorted(COLOR_AGNOSTIC))


@app.route("/api/state")
def state():
    try:
        reply = link.send("?")
    except LinkError as e:
        return jsonify(error=str(e)), 503
    m = STATE_RE.fullmatch(reply)
    if not m:
        return jsonify(error="unexpected reply: " + reply), 502
    # Report the user's speed, not the boosted one the Nano may be running at.
    speed = settings.values.get("speed", int(m[3]))
    return jsonify(pattern=int(m[1]), brightness=int(m[2]), speed=speed, color="#" + m[4].upper())


@app.post("/api/pattern")
def set_pattern():
    n, err = int_field("pattern", 0, len(PATTERNS) - 1)
    if err:
        return err
    speed = speed_for(n, settings.values.get("speed"))
    return command("P %d" % n, "pattern", n, extra=[] if speed is None else ["S %d" % speed])


@app.post("/api/brightness")
def set_brightness():
    n, err = int_field("brightness", 0, MAX_BRIGHTNESS)
    return err or command("B %d" % n, "brightness", n)


@app.post("/api/speed")
def set_speed():
    n, err = int_field("speed", MIN_SPEED, MAX_SPEED)
    if err:
        return err
    if settings.values.get("pattern") in SPEED_AGNOSTIC:
        settings.update("speed", n)  # remembered for the next pattern; the Nano stays at MIN_SPEED
        return jsonify(ok=True)
    return command("S %d" % n, "speed", n)


@app.post("/api/color")
def set_color():
    color = (request.get_json(silent=True) or {}).get("color")
    if not isinstance(color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        return jsonify(error="color must be a hex string like #FF8800"), 400
    color = color.lstrip("#").upper()
    return command("C " + color, "color", color)


def restore_on_startup():
    """Open the link so the saved settings reach the Nano without waiting for a web request.

    The Nano may not have enumerated yet right after a Pi boot, so keep retrying.
    """
    while True:
        try:
            link.send("?")
            return
        except LinkError as e:
            print("waiting for the Nano to restore settings: %s" % e)
            time.sleep(5)


threading.Thread(target=restore_on_startup, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("LED_PORT_NUM", 5001)))
