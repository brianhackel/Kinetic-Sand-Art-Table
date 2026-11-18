"""Serial link, persistence and command logic for the table LEDs.

Ported from RaspberryPi/led-web/app.py with the module-level globals turned into objects that the
plugin constructs, and with the Flask routes replaced by LedController methods that return
(payload_dict, http_status) tuples. No OctoPrint or Flask imports here, so it can be tried on its own.

Talks to the Arduino Nano running LED-Arduino/LED-Control over USB serial; the protocol is
described at the top of LED-Control.ino.
"""
import json
import os
import re
import threading
import time

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
# These pick their own colors, so the color picker has no effect (the UI disables it).
COLOR_AGNOSTIC = {PATTERNS.index(n) for n in ("Chase Rainbow", "Rainbow", "Color Wipe Random", "Color Sweep Random")}

STATE_RE = re.compile(r"STATE P=(\d+) B=(\d+) S=(\d+) C=([0-9A-Fa-f]{6})")


def speed_for(pattern, saved):
    """The speed the Nano should run at: BOOST_SPEED for speed-agnostic patterns, else the saved one (or None)."""
    return BOOST_SPEED if pattern in SPEED_AGNOSTIC else saved


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

    def __init__(self, path, log=print):
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
            log("ignoring unreadable settings file %s: %s" % (path, e))
        self._log = log

    def _save(self):
        """Write to disk; the caller holds the lock."""
        data = dict(self.values, off=self.off, patterns={str(k): v for k, v in self.per_pattern.items()})
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)  # atomic, so a power cut can't leave a half-written file
        except OSError as e:
            self._log("could not save settings to %s: %s" % (self.path, e))

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

    def snapshot(self):
        """(values copy, off) read under the lock."""
        with self.lock:
            return dict(self.values), self.off

    def commands(self):
        """Commands to bring a freshly reset Nano to the saved state (pattern first: P resets the color)."""
        values, off = self.snapshot()
        pattern = values.get("pattern")
        values["speed"] = speed_for(pattern, values.get("speed"))
        if off:
            values["brightness"] = 0
        if pattern in COLOR_AGNOSTIC:
            values.pop("color", None)
        return ["%s %s" % (self.FIELDS[k][0], values[k])
                for k in ("pattern", "brightness", "speed", "color") if values.get(k) is not None]


class SerialLog:
    """Forwards serial events to a callback (the plugin sends them to OctoPrint's debug log).

    kinds: tx (sent), rx (received), drop (bytes we discarded unread), info (everything else)
    """

    def __init__(self, emit=None):
        self.emit = emit

    def add(self, kind, text):
        if self.emit is not None:
            self.emit("serial %s: %s", kind, text)


class LedLink:
    """Owns the serial port; one command at a time."""

    def __init__(self, port, settings, serial_log, baud=115200):
        self.port = port
        self.baud = baud
        self.settings = settings
        self.serial_log = serial_log
        self.ser = None
        self.lock = threading.Lock()

    def _open(self):
        import serial  # imported lazily so fake mode works without pyserial

        self.serial_log.add("info", "opening %s @ %d" % (self.port, self.baud))
        ser = serial.Serial(self.port, self.baud, timeout=1)
        # The Nano resets when the port opens; wait for its READY line.
        deadline = time.time() + 5
        while time.time() < deadline:
            raw = ser.readline()
            if raw:
                self.serial_log.add("rx", repr(raw))
            if raw.strip() == b"READY":
                self.ser = ser
                self.serial_log.add("info", "Nano is READY; restoring saved settings")
                self._restore()
                return
        ser.close()
        self.serial_log.add("info", "Nano did not send READY within 5 s")
        raise LinkError("Nano did not send READY")

    def _exchange(self, command):
        """Send one command and return its reply (empty on timeout)."""
        # The Nano can be left holding half a line if it dropped a byte; the next command would
        # be glued onto it and rejected (e.g. "P 1" arriving as "C FFFFFP 1" -> "ERR bad color").
        # A bare newline makes it dispatch (and reject) the leftover; empty lines are ignored.
        self.ser.write(b"\n")
        self.serial_log.add("tx", repr(b"\n"))
        time.sleep(0.02)
        leftover = self.ser.read(self.ser.in_waiting)  # the reply to the leftover, if there was one
        if leftover:
            self.serial_log.add("drop", repr(leftover))
        self.ser.reset_input_buffer()
        self.ser.write((command + "\n").encode("ascii"))
        self.serial_log.add("tx", repr(command + "\n"))
        raw = self.ser.readline()
        self.serial_log.add("rx", repr(raw) if raw else "(timeout, no reply)")
        return raw.decode("ascii", "replace").strip()

    def _restore(self):
        """Re-send the remembered settings; the Nano starts from its defaults after every open."""
        for line in self.settings.commands():
            reply = self._exchange(line)
            if reply != "OK":
                self.serial_log.add("info", "restoring %r failed: %r" % (line, reply))

    def send(self, command):
        with self.lock:
            try:
                if self.ser is None:
                    self._open()
                reply = self._exchange(command)
                if reply in RETRYABLE_ERRORS:  # line got corrupted in transit; resend once
                    self.serial_log.add("info", "got %r for %r, retrying" % (reply, command))
                    reply = self._exchange(command)
            except Exception as e:  # serial.SerialException, OSError, LinkError
                self.serial_log.add("info", "link error, closing port: %s" % e)
                self._close_locked()
                raise LinkError(str(e))
        if not reply:
            raise LinkError("no reply from Nano")
        return reply

    def _close_locked(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def close(self):
        with self.lock:
            self._close_locked()


class FakeLink:
    """Mimics the Nano's replies."""

    def __init__(self, settings, serial_log):
        self.serial_log = serial_log
        self.p, self.b, self.s, self.c = 0, 64, 1000, 0xFFFFFF
        self.lock = threading.Lock()
        for line in settings.commands():
            self.send(line)

    def send(self, command):
        with self.lock:
            self.serial_log.add("tx", repr(command + "\n"))
            reply = self._reply(command)
            self.serial_log.add("rx", repr(reply + "\r\n"))
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

    def close(self):
        pass


def _bad(msg):
    return {"error": msg}, 400


def _int_field(body, name, lo, hi):
    """Returns (value, None) or (None, error response tuple)."""
    value = (body or {}).get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, _bad("%s must be an integer %d..%d" % (name, lo, hi))
    return value, None


class LedController:
    """The operations the web API exposes. Every method returns (payload dict, http status)."""

    def __init__(self, state_file, port, baud=115200, fake=False, log=print, trace=None):
        """`log` gets warnings about the state file; `trace(fmt, *args)`, if given, gets every serial event."""
        self.serial_log = SerialLog(trace)
        self.settings = SavedSettings(state_file, log)
        if fake:
            self.link = FakeLink(self.settings, self.serial_log)
        else:
            self.link = LedLink(port, self.settings, self.serial_log, baud)

    def close(self):
        self.link.close()

    # -- shared plumbing -------------------------------------------------------------------

    def _command(self, line, remember, extra=()):
        """Send a command (plus any follow-ups), then call remember() to save the setting.

        Turns link/Arduino failures into error payloads, in which case nothing is remembered.
        """
        for l in (line, *extra):
            try:
                reply = self.link.send(l)
            except LinkError as e:
                return {"error": str(e)}, 503
            if reply.startswith("ERR"):
                return {"error": reply}, 502
        remember()
        return {"ok": True}, 200

    def config(self):
        return {"patterns": PATTERNS + ["Off"], "off_pattern": OFF, "max_brightness": MAX_BRIGHTNESS,
                "min_speed": MIN_SPEED, "max_speed": MAX_SPEED,
                "speed_agnostic": sorted(SPEED_AGNOSTIC), "color_agnostic": sorted(COLOR_AGNOSTIC)}

    def ping(self):
        """Open the link (restoring saved settings to a freshly reset Nano). Raises LinkError."""
        self.link.send("?")

    # -- API operations --------------------------------------------------------------------

    def get_state(self):
        try:
            reply = self.link.send("?")
        except LinkError as e:
            return {"error": str(e)}, 503
        m = STATE_RE.fullmatch(reply)
        if not m:
            return {"error": "unexpected reply: " + reply}, 502
        values, off = self.settings.snapshot()
        # Report the user's speed, not the boosted one the Nano may be running at.
        speed = values.get("speed", int(m[3]))
        if off:  # the Nano is at brightness 0 and still on the last real pattern
            brightness = values.get("brightness", NANO_DEFAULT_BRIGHTNESS)
            return {"pattern": OFF, "brightness": brightness, "speed": speed, "color": "#" + m[4].upper()}, 200
        return {"pattern": int(m[1]), "brightness": int(m[2]), "speed": speed, "color": "#" + m[4].upper()}, 200

    def set_pattern(self, body):
        n, err = _int_field(body, "pattern", 0, OFF)
        if err:
            return err
        return self.apply_pattern(n)

    def apply_pattern(self, n):
        if n == OFF:
            return self._command("B 0", self.settings.set_off)
        r = self.settings.recall(n)
        extra = []
        if "brightness" in r:
            extra.append("B %d" % r["brightness"])
        speed = speed_for(n, r.get("speed"))
        if speed is not None:
            extra.append("S %d" % speed)
        if "color" in r:
            extra.append("C " + r["color"])
        return self._command("P %d" % n, lambda: self.settings.switch_pattern(n), extra)

    def set_brightness(self, body):
        n, err = _int_field(body, "brightness", 0, MAX_BRIGHTNESS)
        return err or self._command("B %d" % n, lambda: self.settings.update("brightness", n))

    def set_speed(self, body):
        n, err = _int_field(body, "speed", MIN_SPEED, MAX_SPEED)
        if err:
            return err
        if self.settings.snapshot()[0].get("pattern") in SPEED_AGNOSTIC:
            self.settings.update("speed", n)  # remembered for the next pattern; the Nano stays at BOOST_SPEED
            return {"ok": True}, 200
        return self._command("S %d" % n, lambda: self.settings.update("speed", n))

    def set_color(self, body):
        color = (body or {}).get("color")
        if not isinstance(color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
            return _bad("color must be a hex string like #FF8800")
        color = color.lstrip("#").upper()
        return self._command("C " + color, lambda: self.settings.update("color", color))

    # -- print-event helpers ---------------------------------------------------------------

    def lights_on(self):
        """Back to the last real pattern if the lights are currently Off."""
        values, off = self.settings.snapshot()
        if not off:
            return {"ok": True}, 200
        return self.apply_pattern(values.get("pattern", 0))

    def lights_off(self):
        return self.apply_pattern(OFF)
