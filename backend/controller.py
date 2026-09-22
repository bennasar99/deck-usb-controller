"""Coordinates input reading and the USB gadget into a single controller.

Responsibilities:

* Start/stop a background thread that reads the selected gamepad device.
* Maintain a full button/axis state and rebuild the XInput (Xbox 360 wired)
  report on every SYN (state change) event.
* Forward the report to the host via the FunctionFS gadget.
* Expose discovery, status and hardware checks for the Decky UI.
"""

import errno
import json
import os
import threading
import time

from backend import evdev_reader
from backend.gadget_manager import GadgetError, GadgetManager
from backend.xinput_report import (
    A, B, BACK, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_UP,
    GUIDE, L3, LB, R3, RB, START, X, Y,
    XInputReport,
)

# Map evdev button codes to XInput wire bitflags.
_BUTTON_MAP = {
    evdev_reader.BTN_A: A,
    evdev_reader.BTN_B: B,
    evdev_reader.BTN_X: X,
    evdev_reader.BTN_Y: Y,
    evdev_reader.BTN_TL: LB,
    evdev_reader.BTN_TR: RB,
    evdev_reader.BTN_SELECT: BACK,
    evdev_reader.BTN_START: START,
    evdev_reader.BTN_MODE: GUIDE,
    evdev_reader.BTN_THUMBL: L3,
    evdev_reader.BTN_THUMBR: R3,
}

_STICK_DEADZONE = 0.10
_TRIGGER_DEADZONE = 0.02


class GamepadController:
    """The long-lived controller instance owned by the Decky backend."""

    def __init__(self, logger=None, settings_path=None):
        self._log = logger
        self._settings_path = settings_path
        self._gadget = None
        self._device_path = None
        self._device_name = None
        self._thread = None
        self._running = False
        self._stop = threading.Event()
        self._report_count = 0
        self._write_errors = 0
        self._last_error_log = 0.0
        self._last_frame = b""
        self._error = None
        self._autostart = False
        self._last_reconnect = 0.0
        self._load_settings()

    # -- lifecycle ----------------------------------------------------------

    def start(self, device_path=None):
        """Enable the gadget and begin forwarding input. Returns a result dict."""
        if self._running:
            return self._result("already running")

        if not device_path:
            device_path = self._pick_device()
        if not device_path:
            self._error = (
                "No gamepad detected on this Deck. The Steam Deck's integrated "
                "controller should be found automatically; make sure Steam is "
                "running (or Desktop Mode is active).")
            return self._result("no input device")

        try:
            reader = evdev_reader.EvdevDevice(device_path)
        except OSError as exc:
            self._error = f"Cannot open {device_path}: {exc}"
            return self._result("cannot open device")
        device_name = reader.name
        reader.close()

        try:
            self._gadget = GadgetManager(logger=self._log)
            self._gadget.start()
        except Exception as exc:
            self._error = str(exc)
            self._gadget = None
            return self._result("gadget setup failed")

        self._device_path = device_path
        self._device_name = device_name
        self._last_frame = b""
        self._report_count = 0
        self._write_errors = 0
        self._error = None
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(device_path,),
                                        daemon=True, name="gamepad-forwarder")
        self._thread.start()
        return self._result("started")

    def stop(self):
        """Stop forwarding and tear the gadget down."""
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._gadget is not None:
            try:
                self._gadget.stop()
            except Exception as exc:  # pragma: no cover - best effort
                if self._log:
                    self._log(f"gadget teardown failed: {exc}")
            self._gadget = None
        self._device_path = None
        self._device_name = None
        return self._result("stopped")

    # -- RPC surface --------------------------------------------------------

    def status(self):
        return self._result(None)

    def check_hardware(self):
        manager = GadgetManager(logger=self._log)
        checks = manager.check_hardware()
        checks["running"] = self._running
        checks["recommendations"] = _hardware_recommendations(checks)
        return checks

    def discover_devices(self):
        return evdev_reader.discover_gamepads()

    def set_autostart(self, enabled):
        self._autostart = bool(enabled)
        self._save_settings()
        return {"ok": True, "autostart": self._autostart}

    # -- internal -----------------------------------------------------------

    def _run(self, device_path):
        reader = None
        last_event = time.time()
        try:
            reader = evdev_reader.EvdevDevice(device_path)
            state = {}
            while self._running:
                event = reader.poll(timeout=0.2)
                now = time.time()
                if event is None:
                    if now - last_event > 2.0:
                        new_reader = self._maybe_reconnect(reader, device_path)
                        if new_reader is not reader:
                            reader = new_reader
                            state = {}
                        last_event = now
                    continue
                last_event = now
                ev_type, code, value = event
                if ev_type == evdev_reader.EV_KEY or ev_type == evdev_reader.EV_ABS:
                    state[code] = value
                elif ev_type == evdev_reader.EV_SYN:
                    frame = self._build_frame(reader, state)
                    if frame and frame != self._last_frame:
                        result = self._gadget.send_report(frame) if self._gadget else None
                        if result == 0:
                            self._report_count += 1
                            self._last_frame = frame
                        else:
                            self._write_errors += 1
                            self._log_write_error(result)
        except Exception as exc:
            self._error = f"input loop failed: {exc}"
            self._running = False
        finally:
            if reader is not None:
                reader.close()

    def _maybe_reconnect(self, reader, device_path):
        """Re-open the input device when it has gone quiet for a while.

        Steam Input destroys and recreates its virtual gamepad whenever it
        reloads (e.g. launching a game). The plugin's fd can keep pointing at
        the old, dead device object even though its ioctls still succeed, so it
        never sees the events that are now flowing to the new node. Re-opening
        always grabs the current device, so after a quiet period we refresh the
        fd (and prefer the newly discovered path).
        """
        now = time.time()
        if now - self._last_reconnect < 5.0:
            return reader
        self._last_reconnect = now
        new_path = self._pick_device() or device_path
        try:
            new_reader = evdev_reader.EvdevDevice(new_path)
        except OSError as exc:
            self._log(f"Reconnect failed: {exc}")
            return reader
        reader.close()
        self._device_path = new_path
        self._device_name = new_reader.name
        self._log(f"Refreshed input device: {new_path} ({self._device_name})")
        return new_reader

    def _log_write_error(self, result):
        now = time.time()
        if now - self._last_error_log < 2.0:
            return
        self._last_error_log = now
        detail = {
            errno.EAGAIN: "host is not polling the input endpoint",
            errno.ENODEV: "gadget endpoint is gone (device unplugged?)",
            errno.EPIPE: "transfer stalled by the host",
        }.get(result, "see plugin log")
        message = f"USB write failed (errno {result}): {detail}"
        if self._log:
            self._log(message)

    def _build_frame(self, reader, state):
        return build_xinput_frame(reader, state)

    def _pick_device(self):
        result = evdev_reader.discover_gamepads()
        devices = result["devices"]
        if not devices:
            return None
        for device in devices:
            if device["preferred"]:
                return device["path"]
        return devices[0]["path"]

    def _result(self, note):
        return {
            "ok": self._running or note == "stopped",
            "running": self._running,
            "note": note,
            "device": self._device_path,
            "device_name": self._device_name,
            "gadget": self._gadget.path if self._gadget else None,
            "report_count": self._report_count,
            "polls": self._gadget.polls if self._gadget else 0,
            "write_errors": self._gadget.errors if self._gadget else self._write_errors,
            "autostart": self._autostart,
            "error": self._error,
        }

    # -- settings persistence ----------------------------------------------

    def _load_settings(self):
        if not self._settings_path or not os.path.exists(self._settings_path):
            return
        try:
            with open(self._settings_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._autostart = bool(data.get("autostart", False))
        except (OSError, ValueError):
            pass

    def _save_settings(self):
        if not self._settings_path:
            return
        try:
            os.makedirs(os.path.dirname(self._settings_path), exist_ok=True)
            with open(self._settings_path, "w", encoding="utf-8") as handle:
                json.dump({"autostart": self._autostart}, handle)
        except OSError:
            pass


def _source_y_sign(reader):
    """Per-source stick-Y polarity normalizer for the *screen/HID* convention.

    The Steam Deck's raw built-in controller (hid-steam) reports stick Y
    with the Steam-controller convention (up = positive), while Steam's
    virtual pad and standard xpad-style devices use up = negative. The
    daemon auto-refreshes its source when the current one goes quiet, so
    without this correction the emitted Y polarity would FLIP depending on
    which source was active (observed as the invert "undoing itself").

    Returns the factor that maps a source's normalized Y onto the screen/HID
    convention (up = negative): the HID frame uses it directly, and
    build_xinput_frame negates it because XInput is up = positive.
    """
    return -1.0 if getattr(reader, "kind", "") == "deck-builtin" else 1.0


def build_xinput_frame(reader, state):
    """Rebuild the 20-byte XInput report from the current evdev state."""
    report = XInputReport()
    # _source_y_sign() yields the screen/HID Y convention (up = negative);
    # XInput uses up = positive, so negate it here. The Windows bridge reads
    # the HID frame and applies the same negation (its --invert-y default).
    _fill_xinput(report, reader, state, -_source_y_sign(reader))
    return report.to_bytes()


def build_hid_frame(reader, state):
    """Rebuild the 12-byte HID gamepad report from the current evdev state.

    Used by the HID compatibility gadget (GadgetManager mode 'hid'). Buttons,
    D-pad and triggers match the XInput path; stick Y uses the screen/HID
    convention (up = negative), which the Windows bridge negates into XInput.
    """
    from backend.gamepad_report import (
        BTN_A as H_A, BTN_B as H_B, BTN_X as H_X, BTN_Y as H_Y,
        BTN_LB as H_LB, BTN_RB as H_RB,
        BTN_BACK as H_BACK, BTN_START as H_START,
        BTN_LS as H_LS, BTN_RS as H_RS, BTN_GUIDE as H_GUIDE,
        GamepadReport,
    )
    hid_bits = {
        A: H_A, B: H_B, X: H_X, Y: H_Y,
        LB: H_LB, RB: H_RB, BACK: H_BACK, START: H_START,
        L3: H_LS, R3: H_RS, GUIDE: H_GUIDE,
    }

    hat_x = state.get(evdev_reader.ABS_HAT0X, 0)
    hat_y = state.get(evdev_reader.ABS_HAT0Y, 0)

    report = GamepadReport()
    for code, xinput_flag in _BUTTON_MAP.items():
        hid_flag = hid_bits.get(xinput_flag)
        if hid_flag is not None and state.get(code):
            report.set_button(hid_flag, True)
    report.set_hat(
        up=hat_y == -1, down=hat_y == 1,
        left=hat_x == -1, right=hat_x == 1,
    )

    sign = _source_y_sign(reader)
    report.set_stick(0,
                     _to_stick(_read_axis(reader, state, evdev_reader.ABS_X, _STICK_DEADZONE)),
                     _to_stick(sign * _read_axis(reader, state, evdev_reader.ABS_Y, _STICK_DEADZONE)))
    report.set_stick(1,
                     _to_stick(_read_axis(reader, state, evdev_reader.ABS_RX, _STICK_DEADZONE)),
                     _to_stick(sign * _read_axis(reader, state, evdev_reader.ABS_RY, _STICK_DEADZONE)))
    report.set_trigger(0, _to_trigger(_read_axis(
        reader, state, _pick_axis(reader, evdev_reader.ABS_Z, evdev_reader.ABS_HAT2Y),
        _TRIGGER_DEADZONE)))
    report.set_trigger(1, _to_trigger(_read_axis(
        reader, state, _pick_axis(reader, evdev_reader.ABS_RZ, evdev_reader.ABS_HAT2X),
        _TRIGGER_DEADZONE)))
    return report.to_bytes()


def _fill_xinput(report, reader, state, y_sign):
    """Fill an XInputReport from the current evdev state."""
    for code, flag in _BUTTON_MAP.items():
        if state.get(code):
            report.set_button(flag, True)

    # D-pad: the XInput pad uses buttons, not a hat switch. Each direction
    # maps to its own hat axis so diagonals produce two pressed buttons.
    hat_x = state.get(evdev_reader.ABS_HAT0X, 0)
    hat_y = state.get(evdev_reader.ABS_HAT0Y, 0)
    report.set_dpad(
        up=hat_y == -1,
        down=hat_y == 1,
        left=hat_x == -1,
        right=hat_x == 1,
    )

    report.set_stick(0,
                     _to_stick(_read_axis(reader, state, evdev_reader.ABS_X, _STICK_DEADZONE)),
                     _to_stick(y_sign * _read_axis(reader, state, evdev_reader.ABS_Y, _STICK_DEADZONE)))
    report.set_stick(1,
                     _to_stick(_read_axis(reader, state, evdev_reader.ABS_RX, _STICK_DEADZONE)),
                     _to_stick(y_sign * _read_axis(reader, state, evdev_reader.ABS_RY, _STICK_DEADZONE)))
    # Triggers: the virtual gamepad exposes ABS_Z/ABS_RZ (0..255) while the
    # raw built-in controller exposes ABS_HAT2Y/ABS_HAT2X (0..32767).
    report.set_trigger(0, _to_trigger(_read_axis(
        reader, state, _pick_axis(reader, evdev_reader.ABS_Z, evdev_reader.ABS_HAT2Y),
        _TRIGGER_DEADZONE)))
    report.set_trigger(1, _to_trigger(_read_axis(
        reader, state, _pick_axis(reader, evdev_reader.ABS_RZ, evdev_reader.ABS_HAT2X),
        _TRIGGER_DEADZONE)))


def _pick_axis(reader, primary, fallback):
    """Return the axis code that actually exists on the device (prefer primary)."""
    if primary in reader.absinfo:
        return primary
    return fallback


def _read_axis(reader, state, code, deadzone):
    """Normalize one raw axis reading to ``[-1.0, 1.0]`` with a deadzone."""
    info = reader.absinfo.get(code, {})
    lo = info.get("min", -32768)
    hi = info.get("max", 32767)
    if hi <= lo:
        return 0.0
    normalized = ((state.get(code, 0) - lo) / (hi - lo)) * 2.0 - 1.0
    if abs(normalized) < deadzone:
        return 0.0
    if normalized > 0:
        return (normalized - deadzone) / (1.0 - deadzone)
    return (normalized + deadzone) / (1.0 - deadzone)


def _to_stick(normalized):
    """Map ``[-1.0, 1.0]`` onto the XInput stick range ``[-32768, 32767]``."""
    if normalized >= 0:
        return int(round(normalized * 32767.0))
    return int(round(normalized * 32768.0))


def _to_trigger(normalized):
    """Map ``[-1.0, 1.0]`` (rest at -1) onto the XInput trigger range ``[0, 255]``."""
    return int(round(((normalized + 1.0) / 2.0) * 255.0))


def _hardware_recommendations(checks):
    messages = []
    if not checks.get("f_fs"):
        messages.append(
            "The 'usb_f_fs' kernel function is not available. SteamOS "
            "normally ships it; if it is missing you may need a custom "
            "kernel or kernel module package.")
    if not checks.get("udc"):
        messages.append(
            "No UDC detected. Reboot into BIOS (Volume Up + Power), open "
            "Setup Utility -> Advanced -> USB Configuration and set "
            "'USB Dual Role Device' to 'DRD'.")
    if not checks.get("dwc3_pci"):
        messages.append("The AMD DWC3 controller was not found; the dwc3-pci "
                        "kernel module may be missing.")
    return messages