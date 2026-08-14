#!/usr/bin/env python3
"""Steam Deck -> USB gamepad forwarder daemon (runs as root).

Steam Input only feeds its virtual gamepad ("Microsoft X-Box 360 pad 0") while
a running app/game has focus. Game Mode launches a non-Steam game that cannot
obtain root (setuid and sudoers are not honoured inside the game session), so
the root work lives HERE: this daemon is started as root by a systemd service
at boot and just waits.

The game (usb_gamepad_launcher.c) keeps a marker file at
/home/deck/usb-gamepad-active fresh while it runs. This daemon watches it:

  marker fresh + gadget down  -> set up the HID gadget, wait for Steam's
                                 virtual pad, then forward its reports to the
                                 PC via /dev/hidg0.
  marker stale (game closed)  -> tear the gadget down, return to idle.

Requires root for the configfs gadget setup and /dev/hidg0 access.

Install:  ./install-usb-gamepad.sh
Add /opt/usb-gamepad/usb_gamepad to Steam as a non-Steam game.
"""

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import evdev_reader
from backend.controller import (
    _BUTTON_MAP, _STICK_DEADZONE, _TRIGGER_DEADZONE,
    _hat_direction, _pick_axis, _read_axis, _to_stick, _to_trigger,
)
from backend.gamepad_report import GamepadReport
from backend.gadget_manager import GadgetManager, HID_DEV


DECK_HOME = "/home/deck" if os.path.isdir("/home/deck") else os.path.expanduser("~")
LOG_FILE = os.path.join(DECK_HOME, "usb-gamepad.log")
MARKER_FILE = os.path.join(DECK_HOME, "usb-gamepad-active")
MARKER_STALE_SECS = 5.0


def log(msg):
    """Print to stdout and append to ~/usb-gamepad.log.

    Game Mode swallows the game's stdout, so messages are mirrored to the log
    file the launcher already uses, otherwise failures are invisible there.
    """
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def pick_device():
    """Choose the best gamepad source: Steam's virtual pad, else raw deck."""
    devices = evdev_reader.discover_gamepads()["devices"]
    if not devices:
        return None
    for kind in ("steam-virtual", "deck-builtin"):
        for device in devices:
            if device["kind"] == kind:
                return device["path"]
    return devices[0]["path"]


def build_frame(reader, state):
    """Rebuild the 13-byte HID gamepad report from the current state."""
    report = GamepadReport()
    buttons = 0
    for code, flag in _BUTTON_MAP.items():
        if state.get(code):
            buttons |= flag
    report.buttons = buttons
    report.hat = _hat_direction(state.get(evdev_reader.ABS_HAT0X, 0),
                                state.get(evdev_reader.ABS_HAT0Y, 0))
    report.set_stick(0,
                     _to_stick(_read_axis(reader, state,
                                          evdev_reader.ABS_X, _STICK_DEADZONE)),
                     _to_stick(_read_axis(reader, state,
                                          evdev_reader.ABS_Y, _STICK_DEADZONE)))
    report.set_stick(1,
                     _to_stick(_read_axis(reader, state,
                                          evdev_reader.ABS_RX, _STICK_DEADZONE)),
                     _to_stick(_read_axis(reader, state,
                                          evdev_reader.ABS_RY, _STICK_DEADZONE)))
    report.set_trigger(0, _to_trigger(_read_axis(
        reader, state,
        _pick_axis(reader, evdev_reader.ABS_Z, evdev_reader.ABS_HAT2Y),
        _TRIGGER_DEADZONE)))
    report.set_trigger(1, _to_trigger(_read_axis(
        reader, state,
        _pick_axis(reader, evdev_reader.ABS_RZ, evdev_reader.ABS_HAT2X),
        _TRIGGER_DEADZONE)))
    return report.to_bytes()


def marker_fresh():
    """True while the launcher game keeps updating its marker file."""
    try:
        st = os.stat(MARKER_FILE)
    except OSError:
        return False
    return (time.time() - st.st_mtime) < MARKER_STALE_SECS


_stop = False


def _shutdown(_signum=None, _frame=None):
    global _stop
    _stop = True


def main():
    global _stop
    manager = GadgetManager(logger=log, sudo=False)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    gadget_up = False
    reader = None
    state = {}
    last_frame = b""
    last_event = time.time()

    log("USB gamepad daemon started; waiting for the 'USB Gamepad' game.")
    try:
        while not _stop:
            if not marker_fresh():
                if gadget_up:
                    log("Game session ended; returning the Deck to normal USB.")
                    try:
                        manager.stop()
                    except Exception as exc:
                        log("teardown failed: %s" % exc)
                    gadget_up = False
                reader = None
                state = {}
                last_frame = b""
                time.sleep(1.0)
                continue

            if not gadget_up:
                try:
                    log("Setting up USB HID gadget...")
                    manager.start()
                    gadget_up = True
                    log("USB gadget ready (%s)" % HID_DEV)
                except Exception as exc:
                    log("ERROR: USB HID gadget setup failed: %s" % exc)
                    time.sleep(5.0)
                    continue

            if reader is None:
                device_path = pick_device()
                if not device_path:
                    time.sleep(0.5)
                    continue
                try:
                    reader = evdev_reader.EvdevDevice(device_path)
                except OSError:
                    reader = None
                    time.sleep(1.0)
                    continue
                state = {}
                last_frame = b""
                log("Reading %s (%s)" % (reader.name, device_path))
                log("Forwarding to the PC. Close the 'USB Gamepad' game to stop.")

            try:
                event = reader.poll(timeout=0.2)
            except OSError:
                reader = None
                continue
            now = time.time()
            if event is None:
                if now - last_event > 2.0:
                    new_path = pick_device()
                    if new_path:
                        try:
                            reader.close()
                        except Exception:
                            pass
                        reader = evdev_reader.EvdevDevice(new_path)
                        state = {}
                        last_frame = b""
                        last_event = now
                        log("Refreshed input device: %s" % new_path)
                continue
            last_event = now
            ev_type, code, value = event
            if ev_type in (evdev_reader.EV_KEY, evdev_reader.EV_ABS):
                state[code] = value
            elif ev_type == evdev_reader.EV_SYN:
                frame = build_frame(reader, state)
                if frame and frame != last_frame:
                    if manager.write_report(frame) == 0:
                        last_frame = frame
    finally:
        if gadget_up:
            try:
                manager.stop()
            except Exception as exc:
                log("teardown failed: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())