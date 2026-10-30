"""Small web interface for the table LEDs.

Talks to the Arduino Nano running LED-Arduino/LED-Control over USB serial.
See the protocol description at the top of LED-Control.ino.

Environment:
  LED_PORT  serial device (default /dev/ttyUSB0; /dev/serial/by-id/... is more stable)
  LED_PORT_NUM  HTTP port (default 5001; OctoPi already uses 80 and 5000)
  LED_FAKE=1  use a simulated Nano, for trying the UI without hardware
"""
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

STATE_RE = re.compile(r"STATE P=(\d+) B=(\d+) S=(\d+) C=([0-9A-Fa-f]{6})")


class LinkError(Exception):
    pass


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
                return
        ser.close()
        raise LinkError("Nano did not send READY")

    def send(self, command):
        with self.lock:
            try:
                if self.ser is None:
                    self._open()
                self.ser.reset_input_buffer()
                self.ser.write((command + "\n").encode("ascii"))
                reply = self.ser.readline().decode("ascii", "replace").strip()
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


def command(line):
    """Send a command, turning link/Arduino failures into HTTP errors."""
    try:
        reply = link.send(line)
    except LinkError as e:
        return jsonify(error=str(e)), 503
    if reply.startswith("ERR"):
        return jsonify(error=reply), 502
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
                           min_speed=MIN_SPEED, max_speed=MAX_SPEED)


@app.route("/api/state")
def state():
    try:
        reply = link.send("?")
    except LinkError as e:
        return jsonify(error=str(e)), 503
    m = STATE_RE.fullmatch(reply)
    if not m:
        return jsonify(error="unexpected reply: " + reply), 502
    return jsonify(pattern=int(m[1]), brightness=int(m[2]), speed=int(m[3]), color="#" + m[4].upper())


@app.post("/api/pattern")
def set_pattern():
    n, err = int_field("pattern", 0, len(PATTERNS) - 1)
    return err or command("P %d" % n)


@app.post("/api/brightness")
def set_brightness():
    n, err = int_field("brightness", 0, MAX_BRIGHTNESS)
    return err or command("B %d" % n)


@app.post("/api/speed")
def set_speed():
    n, err = int_field("speed", MIN_SPEED, MAX_SPEED)
    return err or command("S %d" % n)


@app.post("/api/color")
def set_color():
    color = (request.get_json(silent=True) or {}).get("color")
    if not isinstance(color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        return jsonify(error="color must be a hex string like #FF8800"), 400
    return command("C " + color.lstrip("#"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("LED_PORT_NUM", 5001)))
