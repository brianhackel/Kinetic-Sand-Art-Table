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
import collections
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
# Faux pattern shown last in the dropdown. The Nano doesn't know it: choosing it just sends B 0 and
# leaves the remembered per-pattern settings alone.
OFF = len(PATTERNS)
MAX_BRIGHTNESS = 128  # must match maximumBrightnesAllowed in the sketch
NANO_DEFAULT_BRIGHTNESS = 64  # currentBrightness at boot in the sketch
MIN_SPEED, MAX_SPEED = 2, 10000
# Speed doesn't change how these look, but the Nano only redraws once per speed interval, so
# color/brightness changes lag. Run them at a fast tick; the user's speed is still remembered.
# Not MIN_SPEED: each redraw of the strip disables interrupts on the Nano, and redrawing every
# 2 ms made it drop incoming serial bytes (commands arrived garbled -> "ERR bad color").
SPEED_AGNOSTIC = {PATTERNS.index("Static")}
BOOST_SPEED = 30
# Replies meaning the Nano got a mangled line (nothing was applied), so resending is safe.
RETRYABLE_ERRORS = {"ERR bad command", "ERR bad color", "ERR line too long"}
# These pick their own colors, so the color picker has no effect (the page disables it).
COLOR_AGNOSTIC = {PATTERNS.index(n) for n in ("Chase Rainbow", "Rainbow", "Color Wipe Random", "Color Sweep Random")}


def speed_for(pattern, saved):
    """The speed the Nano should run at: BOOST_SPEED for speed-agnostic patterns, else the saved one (or None)."""
    return BOOST_SPEED if pattern in SPEED_AGNOSTIC else saved

STATE_RE = re.compile(r"STATE P=(\d+) B=(\d+) S=(\d+) C=([0-9A-Fa-f]{6})")


class LinkError(Exception):
    pass


def _is_int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


class SavedSettings:
    """The last settings sent to the Nano, kept in a JSON file so they survive reboots.

    `values` is the current state of the last real pattern. `per_pattern` remembers each pattern's
    own brightness/speed/color so they come back when it is selected again. `off` means the Off
    faux pattern is selected; it never touches `values` or `per_pattern`.
    """

    # setting name -> (serial command letter, validator)
    FIELDS = {
        "pattern": ("P", lambda v: _is_int(v, 0, len(PATTERNS) - 1)),
        "brightness": ("B", lambda v: _is_int(v, 0, MAX_BRIGHTNESS)),
        "speed": ("S", lambda v: _is_int(v, MIN_SPEED, MAX_SPEED)),
        "color": ("C", lambda v: isinstance(v, str) and re.fullmatch(r"[0-9A-F]{6}", v) is not None),
    }

    PER_PATTERN = ("brightness", "speed", "color")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.values = {}
        self.off = False
        self.per_pattern = {}
        try:
            with open(path) as f:
                saved = json.load(f)
            self.values = {k: v for k, v in saved.items() if k in self.FIELDS and self.FIELDS[k][1](v)}
            self.off = saved.get("off") is True
            for key, entry in (saved.get("patterns") or {}).items():
                if key.isdigit() and self.FIELDS["pattern"][1](int(key)) and isinstance(entry, dict):
                    self.per_pattern[int(key)] = {k: v for k, v in entry.items()
                                                  if k in self.PER_PATTERN and self.FIELDS[k][1](v)}
            if "patterns" not in saved and "pattern" in self.values:  # file from before per-pattern memory
                self.per_pattern[self.values["pattern"]] = {k: self.values[k] for k in self.PER_PATTERN
                                                            if k in self.values}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            print("ignoring unreadable settings file %s: %s" % (path, e))

    def _save(self):
        """Write to disk; the caller holds the lock."""
        data = dict(self.values, off=self.off, patterns={str(k): v for k, v in self.per_pattern.items()})
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)  # atomic, so a power cut can't leave a half-written file
        except OSError as e:
            print("could not save settings to %s: %s" % (self.path, e))

    def update(self, name, value):
        """Remember a setting, for the current pattern too."""
        with self.lock:
            self.values[name] = value
            pattern = self.values.get("pattern")
            if name in self.PER_PATTERN and pattern is not None:
                self.per_pattern.setdefault(pattern, {})[name] = value
            self._save()

    def _recall(self, n):
        """Settings to apply when pattern n is selected; the caller holds the lock.

        Its remembered ones, else the current brightness/speed carry over. The Nano resets the color
        on P, so a color only comes back if this pattern has its own (never for color-agnostic ones).
        """
        r = {k: self.values[k] for k in ("brightness", "speed") if k in self.values}
        if self.off:  # the Nano is at brightness 0; make sure it gets a real one back
            r.setdefault("brightness", NANO_DEFAULT_BRIGHTNESS)
        r.update(self.per_pattern.get(n, {}))
        if n in COLOR_AGNOSTIC:
            r.pop("color", None)
        return r

    def recall(self, n):
        with self.lock:
            return self._recall(n)

    def switch_pattern(self, n):
        """Make pattern n current, with its remembered settings."""
        with self.lock:
            r = self._recall(n)
            self.values.pop("color", None)
            self.values.update(r)
            self.values["pattern"] = n
            self.off = False
            self._save()

    def set_off(self):
        with self.lock:
            self.off = True
            self._save()

    def commands(self):
        """Commands to bring a freshly reset Nano to the saved state (pattern first: P resets the color)."""
        with self.lock:
            values = dict(self.values)
            off = self.off
        pattern = values.get("pattern")
        values["speed"] = speed_for(pattern, values.get("speed"))
        if off:
            values["brightness"] = 0
        if pattern in COLOR_AGNOSTIC:
            values.pop("color", None)
        return ["%s %s" % (self.FIELDS[k][0], values[k])
                for k in ("pattern", "brightness", "speed", "color") if values.get(k) is not None]


settings = SavedSettings(os.environ.get("LED_STATE_FILE") or
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "led-state.json"))


class SerialLog:
    """Ring buffer of recent serial events, shown in the web page to diagnose link problems.

    kinds: tx (sent), rx (received), drop (bytes we discarded unread), info (everything else)
    """

    def __init__(self, size=500):
        self.entries = collections.deque(maxlen=size)
        self.lock = threading.Lock()
        self.next_id = 1

    def add(self, kind, text):
        with self.lock:
            self.entries.append({"id": self.next_id, "t": time.time(), "kind": kind, "text": text})
            self.next_id += 1

    def since(self, last_id):
        """Entries newer than last_id, and the newest id so far."""
        with self.lock:
            return [e for e in self.entries if e["id"] > last_id], self.next_id - 1


serial_log = SerialLog()


class LedLink:
    """Owns the serial port; one command at a time."""

    def __init__(self, port, baud=115200):
        self.port = port
        self.baud = baud
        self.ser = None
        self.lock = threading.Lock()

    def _open(self):
        import serial  # imported lazily so LED_FAKE works without pyserial

        serial_log.add("info", "opening %s @ %d" % (self.port, self.baud))
        ser = serial.Serial(self.port, self.baud, timeout=1)
        # The Nano resets when the port opens; wait for its READY line.
        deadline = time.time() + 5
        while time.time() < deadline:
            raw = ser.readline()
            if raw:
                serial_log.add("rx", repr(raw))
            if raw.strip() == b"READY":
                self.ser = ser
                serial_log.add("info", "Nano is READY; restoring saved settings")
                self._restore()
                return
        ser.close()
        serial_log.add("info", "Nano did not send READY within 5 s")
        raise LinkError("Nano did not send READY")

    def _exchange(self, command):
        """Send one command and return its reply (empty on timeout)."""
        # The Nano can be left holding half a line if it dropped a byte; the next command would
        # be glued onto it and rejected (e.g. "P 1" arriving as "C FFFFFP 1" -> "ERR bad color").
        # A bare newline makes it dispatch (and reject) the leftover; empty lines are ignored.
        self.ser.write(b"\n")
        serial_log.add("tx", repr(b"\n"))
        time.sleep(0.02)
        leftover = self.ser.read(self.ser.in_waiting)  # the reply to the leftover, if there was one
        if leftover:
            serial_log.add("drop", repr(leftover))
        self.ser.reset_input_buffer()
        self.ser.write((command + "\n").encode("ascii"))
        serial_log.add("tx", repr(command + "\n"))
        raw = self.ser.readline()
        serial_log.add("rx", repr(raw) if raw else "(timeout, no reply)")
        return raw.decode("ascii", "replace").strip()

    def _restore(self):
        """Re-send the remembered settings; the Nano starts from its defaults after every open."""
        for line in settings.commands():
            reply = self._exchange(line)
            if reply != "OK":
                print("restoring %r failed: %r" % (line, reply))
                serial_log.add("info", "restoring %r failed: %r" % (line, reply))

    def send(self, command):
        with self.lock:
            try:
                if self.ser is None:
                    self._open()
                reply = self._exchange(command)
                if reply in RETRYABLE_ERRORS:  # line got corrupted in transit; resend once
                    serial_log.add("info", "got %r for %r, retrying" % (reply, command))
                    reply = self._exchange(command)
            except Exception as e:  # serial.SerialException, OSError, LinkError
                serial_log.add("info", "link error, closing port: %s" % e)
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
            serial_log.add("tx", repr(command + "\n"))
            reply = self._reply(command)
            serial_log.add("rx", repr(reply + "\r\n"))
            return reply

    def _reply(self, command):
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


def command(line, remember, extra=()):
    """Send a command (plus any follow-ups), then call remember() to save the setting.

    Turns link/Arduino failures into HTTP errors, in which case nothing is remembered.
    """
    for l in (line, *extra):
        try:
            reply = link.send(l)
        except LinkError as e:
            return jsonify(error=str(e)), 503
        if reply.startswith("ERR"):
            return jsonify(error=reply), 502
    remember()
    return jsonify(ok=True)


def int_field(name, lo, hi):
    """Read an integer from the JSON body; returns (value, None) or (None, error response)."""
    value = (request.get_json(silent=True) or {}).get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, (jsonify(error="%s must be an integer %d..%d" % (name, lo, hi)), 400)
    return value, None


@app.route("/")
def index():
    return render_template("index.html", patterns=PATTERNS + ["Off"], off_pattern=OFF, max_brightness=MAX_BRIGHTNESS,
                           min_speed=MIN_SPEED, max_speed=MAX_SPEED,
                           speed_agnostic=sorted(SPEED_AGNOSTIC), color_agnostic=sorted(COLOR_AGNOSTIC))


@app.route("/api/log")
def log():
    since = request.args.get("since", 0, type=int)
    entries, last = serial_log.since(since)
    return jsonify(entries=entries, last=last)


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
    if settings.off:  # the Nano is at brightness 0 and still on the last real pattern
        brightness = settings.values.get("brightness", NANO_DEFAULT_BRIGHTNESS)
        return jsonify(pattern=OFF, brightness=brightness, speed=speed, color="#" + m[4].upper())
    return jsonify(pattern=int(m[1]), brightness=int(m[2]), speed=speed, color="#" + m[4].upper())


@app.post("/api/pattern")
def set_pattern():
    n, err = int_field("pattern", 0, OFF)
    if err:
        return err
    if n == OFF:
        return command("B 0", settings.set_off)
    r = settings.recall(n)
    extra = []
    if "brightness" in r:
        extra.append("B %d" % r["brightness"])
    speed = speed_for(n, r.get("speed"))
    if speed is not None:
        extra.append("S %d" % speed)
    if "color" in r:
        extra.append("C " + r["color"])
    return command("P %d" % n, lambda: settings.switch_pattern(n), extra)


@app.post("/api/brightness")
def set_brightness():
    n, err = int_field("brightness", 0, MAX_BRIGHTNESS)
    return err or command("B %d" % n, lambda: settings.update("brightness", n))


@app.post("/api/speed")
def set_speed():
    n, err = int_field("speed", MIN_SPEED, MAX_SPEED)
    if err:
        return err
    if settings.values.get("pattern") in SPEED_AGNOSTIC:
        settings.update("speed", n)  # remembered for the next pattern; the Nano stays at BOOST_SPEED
        return jsonify(ok=True)
    return command("S %d" % n, lambda: settings.update("speed", n))


@app.post("/api/color")
def set_color():
    color = (request.get_json(silent=True) or {}).get("color")
    if not isinstance(color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        return jsonify(error="color must be a hex string like #FF8800"), 400
    color = color.lstrip("#").upper()
    return command("C " + color, lambda: settings.update("color", color))


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
