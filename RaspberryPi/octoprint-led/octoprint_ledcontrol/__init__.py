"""OctoPrint plugin: control the sand table's WS2812 LEDs (Arduino Nano over serial) from the sidebar."""
import os
import threading

import flask
import octoprint.plugin
from octoprint.access import ADMIN_GROUP, USER_GROUP
from octoprint.access.permissions import Permissions
from octoprint.events import Events

from .led_link import LedController, LinkError

__plugin_name__ = "LED Control"
__plugin_version__ = "0.1.0"
__plugin_description__ = "Control the Kinetic Sand Art Table LEDs from the OctoPrint sidebar."
__plugin_pythoncompat__ = ">=3.9,<4"

PERMISSION_KEY = "CONTROL"  # becomes Permissions.PLUGIN_LEDCONTROL_CONTROL


class LedControlPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._controller = None
        self._controller_lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------------------------

    def _build_controller(self):
        """(Re)create the controller from the current settings, closing any previous serial port."""
        with self._controller_lock:
            if self._controller is not None:
                self._controller.close()
            state_file = os.path.join(self.get_plugin_data_folder(), "led-state.json")
            self._controller = LedController(
                state_file,
                port=self._settings.get(["port"]),
                baud=self._settings.get_int(["baud"]),
                fake=self._settings.get_boolean(["fake"]),
                log=self._logger.warning,
            )
            return self._controller

    @property
    def controller(self):
        return self._controller or self._build_controller()

    def on_after_startup(self):
        self._build_controller()
        threading.Thread(target=self._restore_on_startup, daemon=True, name="ledcontrol-restore").start()

    def _restore_on_startup(self):
        """Open the link so the saved settings reach the Nano without waiting for a web request.

        The Nano may not have enumerated yet right after a Pi boot, so keep retrying.
        """
        controller = self._controller
        while not self._stop.is_set() and controller is self._controller:
            try:
                controller.ping()
                return
            except LinkError as e:
                self._logger.info("waiting for the Nano to restore settings: %s", e)
                self._stop.wait(5)

    def on_shutdown(self):
        self._stop.set()
        if self._controller is not None:
            self._controller.close()

    # -- settings --------------------------------------------------------------------------

    def get_settings_defaults(self):
        return {
            "port": "/dev/ttyUSB0",  # prefer /dev/serial/by-id/... so it doesn't swap with the GRBL Uno
            "baud": 115200,
            "fake": False,  # simulated Nano, for trying the UI without hardware
            "on_at_print_start": False,  # switch the lights back on when a print starts
            "off_after_print": False,  # switch the lights off when a print finishes, fails or is cancelled
        }

    def on_settings_save(self, data):
        old = (self._settings.get(["port"]), self._settings.get(["baud"]), self._settings.get(["fake"]))
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        new = (self._settings.get(["port"]), self._settings.get(["baud"]), self._settings.get(["fake"]))
        if new != old:
            self._build_controller()
            threading.Thread(target=self._restore_on_startup, daemon=True, name="ledcontrol-restore").start()

    # -- templates and assets --------------------------------------------------------------

    def get_template_configs(self):
        return [
            {"type": "sidebar", "name": "LEDs", "icon": "lightbulb", "custom_bindings": True,
             "template": "ledcontrol_sidebar.jinja2"},
            {"type": "settings", "name": "LED Control", "custom_bindings": False,
             "template": "ledcontrol_settings.jinja2"},
        ]

    def get_assets(self):
        return {"js": ["js/ledcontrol.js"], "css": ["css/ledcontrol.css"]}

    # -- permissions -----------------------------------------------------------------------

    def get_additional_permissions(self):
        return [{
            "key": PERMISSION_KEY,
            "name": "Control LEDs",
            "description": "Allows changing the LED pattern, brightness, speed and color.",
            "roles": ["control"],
            "default_groups": [ADMIN_GROUP, USER_GROUP],
        }]

    # -- API (served under /plugin/ledcontrol/) ---------------------------------------------

    def is_blueprint_csrf_protected(self):
        # CSRF-protect every route; the JS client sends the token. (Default only since 2.0.0.)
        return True

    def is_template_autoescaped(self):
        # Escape template variables. (Default only since 2.1.0.)
        return True

    @staticmethod
    def _allowed():
        return Permissions.PLUGIN_LEDCONTROL_CONTROL.can()

    @staticmethod
    def _reply(result):
        payload, status = result
        return flask.make_response(flask.jsonify(payload), status)

    def _guard(self, fn):
        if not self._allowed():
            return flask.make_response(flask.jsonify(error="Forbidden"), 403)
        return self._reply(fn())

    @octoprint.plugin.BlueprintPlugin.route("/config", methods=["GET"])
    def api_config(self):
        return self._guard(lambda: (self.controller.config(), 200))

    @octoprint.plugin.BlueprintPlugin.route("/state", methods=["GET"])
    def api_state(self):
        return self._guard(lambda: self.controller.get_state())

    @octoprint.plugin.BlueprintPlugin.route("/log", methods=["GET"])
    def api_log(self):
        since = flask.request.args.get("since", 0, type=int)
        return self._guard(lambda: self.controller.log_since(since))

    @octoprint.plugin.BlueprintPlugin.route("/pattern", methods=["POST"])
    def api_pattern(self):
        body = flask.request.get_json(silent=True)
        return self._guard(lambda: self.controller.set_pattern(body))

    @octoprint.plugin.BlueprintPlugin.route("/brightness", methods=["POST"])
    def api_brightness(self):
        body = flask.request.get_json(silent=True)
        return self._guard(lambda: self.controller.set_brightness(body))

    @octoprint.plugin.BlueprintPlugin.route("/speed", methods=["POST"])
    def api_speed(self):
        body = flask.request.get_json(silent=True)
        return self._guard(lambda: self.controller.set_speed(body))

    @octoprint.plugin.BlueprintPlugin.route("/color", methods=["POST"])
    def api_color(self):
        body = flask.request.get_json(silent=True)
        return self._guard(lambda: self.controller.set_color(body))

    # -- print events ----------------------------------------------------------------------

    def on_event(self, event, payload):
        if event == Events.PRINT_STARTED and self._settings.get_boolean(["on_at_print_start"]):
            action = self.controller.lights_on
        elif event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED) \
                and self._settings.get_boolean(["off_after_print"]):
            action = self.controller.lights_off
        else:
            return
        # Serial can block for seconds; keep it off OctoPrint's event thread.
        threading.Thread(target=self._run_event_action, args=(event, action), daemon=True).start()

    def _run_event_action(self, event, action):
        payload, status = action()
        if status != 200:
            self._logger.warning("LED action for %s failed: %s", event, payload.get("error"))


__plugin_implementation__ = LedControlPlugin()
__plugin_hooks__ = {
    "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
}
