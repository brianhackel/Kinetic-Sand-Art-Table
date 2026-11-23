/*
 * LED Control sidebar view model.
 *
 * Requests go through OctoPrint's JS client (OctoPrint.get / OctoPrint.postJson) rather than bare
 * fetch(), because OctoPrint 2.0 CSRF-protects blueprint routes and the client adds the token.
 */
$(function () {
    function LedControlViewModel(parameters) {
        const self = this;
        const BASE = OctoPrint.getBlueprintUrl("ledcontrol");

        self.loginState = parameters[0];

        // Constants rendered into the sidebar by the server (data-config), so the dropdown never
        // depends on a request that could run before login finishes.
        const cfg = JSON.parse(document.getElementById("ledcontrol_panel").dataset.config);
        self.patterns = ko.observableArray(cfg.patterns.map((name, id) => ({id: id, name: name})));
        self.maxBrightness = ko.observable(cfg.max_brightness);
        const minSpeed = cfg.min_speed, maxSpeed = cfg.max_speed, offPattern = cfg.off_pattern;
        const speedAgnostic = cfg.speed_agnostic, colorAgnostic = cfg.color_agnostic;

        // current state
        self.pattern = ko.observable();
        self.brightness = ko.observable(0);
        self.speedSlider = ko.observable(50);  // 0..100, 100 = fastest; mapped to ms on a log scale
        self.color = ko.observable("#ffffff");

        self.statusText = ko.observable("");
        self.statusIsError = ko.observable(false);

        const isOff = () => Number(self.pattern()) === offPattern;
        self.controlsEnabled = ko.pureComputed(() => !isOff());
        self.speedEnabled = ko.pureComputed(() => !isOff() && !speedAgnostic.includes(Number(self.pattern())));
        self.colorEnabled = ko.pureComputed(() => !isOff() && !colorAgnostic.includes(Number(self.pattern())));

        // slider position 0..100 (100 = fastest) <-> ms per step, on a log scale
        const ratio = () => Math.log(maxSpeed / minSpeed);
        const sliderToSpeed = v => Math.round(minSpeed * Math.exp(ratio() * (1 - Number(v) / 100)));
        const speedToSlider = s => Math.round(100 * (1 - Math.log(s / minSpeed) / ratio()));

        function show(msg, isError) {
            self.statusText(msg);
            self.statusIsError(!!isError);
        }

        function errorMessage(xhr) {
            return (xhr && xhr.responseJSON && xhr.responseJSON.error) || (xhr && xhr.statusText) || "request failed";
        }

        function get(path) {
            return OctoPrint.get(BASE + path).then(
                data => { show(""); return data; },
                xhr => { show(errorMessage(xhr), true); return null; });
        }

        function post(path, body) {
            return OctoPrint.postJson(BASE + path, body).then(
                data => { show(""); return data; },
                xhr => { show(errorMessage(xhr), true); return null; });
        }

        function debounce(fn, ms) {
            let t;
            return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
        }

        // Bumped on every pattern change so a slower, older state read can't overwrite the newer choice.
        let refreshId = 0;

        self.refresh = async function () {
            const id = ++refreshId;
            const s = await get("state");
            if (!s || id !== refreshId) return;
            self.pattern(s.pattern);
            self.brightness(s.brightness);
            self.speedSlider(speedToSlider(s.speed));
            self.color(s.color.toLowerCase());
        };

        // The server restores the new pattern's remembered settings, so re-read state afterwards.
        self.onPattern = async function (vm, event) {
            refreshId++;  // discard any state read already in flight
            await post("pattern", {pattern: Number(event.target.value)});
            self.refresh();
        };

        const sendBrightness = debounce(() => post("brightness", {brightness: Number(self.brightness())}), 100);
        const sendSpeed = debounce(() => post("speed", {speed: sliderToSpeed(self.speedSlider())}), 100);
        const sendColor = debounce(() => post("color", {color: self.color()}), 100);

        // Read the value off the event so we never depend on whether Knockout's binding ran first.
        self.onBrightness = function (vm, event) { self.brightness(Number(event.target.value)); sendBrightness(); };
        self.onSpeed = function (vm, event) { self.speedSlider(Number(event.target.value)); sendSpeed(); };
        self.swatches = ["#ffffff", "#ff0000", "#ff7f00", "#ffd000", "#80ff00", "#00ff00",
                         "#00ff95", "#00ffff", "#0080ff", "#0000ff", "#8000ff", "#ff00ff"];
        self.onSwatch = function (hex) { self.color(hex); sendColor(); };

        // The state endpoint needs a logged-in user whose permissions are loaded, which can happen
        // after startup finishes, so load on login as well as when startup completes.
        const refreshIfLoggedIn = () => { if (self.loginState.loggedIn()) self.refresh(); };
        self.onStartupComplete = refreshIfLoggedIn;
        self.onUserLoggedIn = refreshIfLoggedIn;
        self.onDataUpdaterReconnect = refreshIfLoggedIn;
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: LedControlViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#sidebar_plugin_ledcontrol"],
    });
});
