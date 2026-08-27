#!/usr/bin/env python3
"""Steam Deck -> USB XInput controller forwarder daemon (runs as root).

Steam Input only feeds its virtual gamepad ("Microsoft X-Box 360 pad 0") while
a running app/game has focus. Game Mode launches a non-Steam game that cannot
obtain root (setuid and sudoers are not honoured inside the game session), so
the root work lives HERE: this daemon is started as root by a systemd service
at boot and just waits.

The game (usb_gamepad_launcher.c) keeps a marker file at
/home/deck/usb-gamepad-active fresh while it runs. This daemon watches it:

  marker fresh + gadget down  -> set up the FunctionFS gadget implementing a
                                 wired Xbox 360 controller, wait for Steam's
                                 virtual pad, then forward its state to the
                                 PC as native XInput reports.
  marker stale (game closed)  -> tear the gadget down, return to idle.

Requires root for the configfs gadget setup and functionfs access.

Install:  ./install-usb-gamepad.sh
Add /opt/usb-gamepad/usb_gamepad to Steam as a non-Steam game.
"""

import fcntl
import os
import signal
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import evdev_reader
from backend.controller import build_xinput_frame, build_hid_frame
from backend.gadget_manager import GadgetManager, FFS_DIR


DECK_HOME = "/home/deck" if os.path.isdir("/home/deck") else os.path.expanduser("~")
LOG_FILE = os.path.join(DECK_HOME, "usb-gamepad.log")
MARKER_FILE = os.path.join(DECK_HOME, "usb-gamepad-active")
MARKER_STALE_SECS = 5.0
# User-selected controller protocol, written by the launcher UI
# ("auto" | "xinput" | "hid"). "auto" = XInput first, automatic HID
# fallback when the host ignores it.
MODE_FILE = os.path.join(DECK_HOME, "usb-gamepad-mode")


def read_mode():
    """Read the user-selected mode; 'auto' when absent or unrecognized."""
    try:
        with open(MODE_FILE, "r") as handle:
            value = handle.read(16).strip().lower()
        if value in ("auto", "xinput", "hid"):
            return value
    except OSError:
        pass
    return "auto"


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


def _acquire_singleton():
    """Ensure only one daemon instance runs.

    Multiple concurrent instances race each other on configfs (observed as
    random EACCES during gadget setup and mysterious double-starts under
    `systemctl restart`), so hold an exclusive flock for our lifetime.
    """
    lock_path = "/run/usb-gamepad.lock" if os.path.isdir("/run") \
        else os.path.join(DECK_HOME, "usb-gamepad.lock")
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log("Another usb_gamepad daemon instance is already running; "
            "exiting.")
        return None
    return handle


def main():
    global _stop
    manager = GadgetManager(logger=log, sudo=False)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    lock_handle = _acquire_singleton()
    if lock_handle is None:
        return 0

    gadget_up = False
    reader = None
    state = {}
    last_frame = b""
    pending = b""
    delivered_gadget_up = False
    last_event = time.time()
    last_stats = time.time()
    ev_count = 0
    logged_first_event = False
    prev_polls = 0
    prev_polls_since = time.time()
    resets_done = 0
    hid_recoveries = 0
    applied_wanted = read_mode()      # mode selection applied to the gadget
    switched_to_hid = False           # auto-mode XInput->HID switch happened

    log("USB XInput controller daemon started (build 2026-08-xinput-ffs, "
        "mode=%s); waiting for the 'USB Gamepad' game." % applied_wanted)
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
                pending = b""
                time.sleep(1.0)
                continue

            if not gadget_up:
                try:
                    mode = "hid" if (applied_wanted == "hid"
                                     or switched_to_hid) else "xinput"
                    log("Setting up USB %s controller..." %
                        ("HID (compatibility)" if mode == "hid"
                         else "Xbox 360 (XInput)"))
                    manager.start(mode=mode)
                    gadget_up = True
                    log("USB gadget ready (%s) [mode=%s, selection=%s]"
                        % (manager.path, manager.mode, applied_wanted))
                except Exception as exc:
                    log("ERROR: USB gadget setup failed: %s" % exc)
                    try:
                        manager.stop()
                    except Exception:
                        pass
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
                pending = b""
                last_event = time.time()
                log("Reading %s (%s)" % (reader.name, device_path))
                log("Forwarding to the PC as an Xbox 360 controller. "
                    "Close the 'USB Gamepad' game to stop.")

            try:
                event = reader.poll(timeout=0.2)
            except OSError:
                reader = None
                continue
            now = time.time()
            # Manual mode selection from the launcher UI: switching between
            # explicit modes (or back to auto) restarts the gadget.
            wanted = read_mode()
            if gadget_up and wanted != applied_wanted:
                log("Controller mode selection changed: %s -> %s; "
                    "restarting gadget..." % (applied_wanted, wanted))
                applied_wanted = wanted
                switched_to_hid = False
                gadget_up = False
                reader = None
                state = {}
                last_frame = b""
                pending = b""
                delivered_gadget_up = False
                prev_polls = 0
                prev_polls_since = now
                resets_done = 0
                hid_recoveries = 0
                try:
                    manager.stop()
                except Exception as exc:
                    log("teardown during mode switch failed: %s" % exc)
                manager = GadgetManager(logger=log, sudo=False)
                time.sleep(1.0)
                continue
            if gadget_up and now - last_stats >= 15.0:
                last_stats = now
                extra = ""
                if pending and len(pending) >= 12:
                    buttons = struct.unpack_from("<H", pending, 0)[0]
                    lt, rt = pending[2], pending[3]
                    sticks = struct.unpack_from("<hhhh", pending, 4)
                    if manager.mode == "hid":
                        fmt = ("hid: buttons=0x%04x lt=%d rt=%d "
                               "L(%d,%d) R(%d,%d)")
                        vals = (buttons, lt, rt) + sticks
                    else:
                        fmt = ("xinput: buttons=0x%04x lt=%d rt=%d "
                               "L(%d,%d) R(%d,%d)")
                        vals = (struct.unpack_from("<H", pending, 2)[0],
                                pending[4], pending[5],
                                ) + struct.unpack_from("<hhhh", pending, 6)
                    extra = " | " + (fmt % vals)
                log("Status: input_events=%d polls=%s errors=%s%s%s" % (
                    ev_count, manager.polls, manager.errors,
                    "" if manager.enabled else " [host not configured yet]",
                    extra))
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
                        pending = b""
                        last_event = now
                        log("Refreshed input device: %s" % new_path)
            else:
                last_event = now
                ev_count += 1
                if not logged_first_event:
                    logged_first_event = True
                    log("First input event received: type=0x%x code=0x%x "
                        "value=%d" % event)
                ev_type, code, value = event
                if ev_type in (evdev_reader.EV_KEY, evdev_reader.EV_ABS):
                    state[code] = value
                elif ev_type == evdev_reader.EV_SYN:
                    pending = (build_hid_frame(reader, state)
                               if manager.mode == "hid"
                               else build_xinput_frame(reader, state))

            # Delivery is decoupled from event building: ep1 writes block
            # until the host consumes them, so they run on the writer thread
            # inside the gadget; here we only enqueue (never blocks) and
            # detect actual consumption via the polls counter.
            if pending and pending != last_frame and manager.enabled:
                if manager.send_report(pending) == 0:
                    last_frame = pending

            # Watchdog. NOTE: an idle pad freezes polls by design (frames are
            # only written on state change), so "host not consuming" may only
            # be declared while the user is ACTIVELY generating input events;
            # otherwise an AFK user would trigger endless recoveries.
            if manager.polls > prev_polls:
                if not delivered_gadget_up:
                    delivered_gadget_up = True
                    kind = ("HID" if manager.mode == "hid" else "XInput")
                    log("First %s report consumed by the host (polls=%s). "
                        "The PC should now show a working controller."
                        % (kind, manager.polls))
                prev_polls = manager.polls
                prev_polls_since = now
            elif (manager.enabled and ev_count > 0
                  and now - last_event < 15.0
                  and now - prev_polls_since > 8.0):
                prev_polls_since = now   # rate-limit recovery to 1 / 8 s
                if manager.mode == "xinput" and applied_wanted == "auto":
                    resets_done += 1
                    if resets_done == 1:
                        # Host configured us but never consumes reports --
                        # force ONE re-enumeration (= cable replug).
                        try:
                            manager.reset_device()
                        except Exception as exc:
                            log("re-enumeration failed: %s" % exc)
                    else:
                        # Give up on XInput for this host: switch to a plain
                        # HID gamepad, which every OS maps out of the box.
                        log("Host ignored the XInput controller after one "
                            "re-enumeration; switching to HID compatibility "
                            "mode. The Deck will now appear as a standard "
                            "USB gamepad.")
                        switched_to_hid = True
                        hid_recoveries = 0
                        gadget_up = False
                        reader = None
                        state = {}
                        last_frame = b""
                        pending = b""
                        delivered_gadget_up = False
                        prev_polls = 0
                        try:
                            manager.stop()
                        except Exception as exc:
                            log("teardown during mode switch failed: %s" % exc)
                        manager = GadgetManager(logger=log, sudo=False)
                        time.sleep(1.0)
                elif manager.mode == "xinput":
                    # User forced XInput: recover once, then stay put —
                    # switching protocols was explicitly disabled.
                    if resets_done == 0:
                        resets_done = 1
                        try:
                            manager.reset_device()
                        except Exception as exc:
                            log("re-enumeration failed: %s" % exc)
                else:
                    # HID delivery stalled while inputs flow: restart the HID
                    # gadget at most twice, then stay quiet (the controller
                    # usually keeps working; endless resets just eject it off
                    # the bus, which is what Windows then drops).
                    hid_recoveries += 1
                    if hid_recoveries <= 2:
                        log("HID report delivery stalled while inputs are "
                            "flowing; restarting the HID gadget "
                            "(attempt %d of 2)..." % hid_recoveries)
                        try:
                            manager.stop()
                        except Exception as exc:
                            log("HID restart teardown failed: %s" % exc)
                        manager = GadgetManager(logger=log, sudo=False)
                        gadget_up = False
                        reader = None
                        state = {}
                        last_frame = b""
                        pending = b""
                        delivered_gadget_up = False
                        prev_polls = 0
                        time.sleep(1.0)
                    else:
                        log("HID delivery still stalled; leaving the gadget "
                            "up to avoid disconnecting the host further.")
    finally:
        if gadget_up:
            try:
                manager.stop()
            except Exception as exc:
                log("teardown failed: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())