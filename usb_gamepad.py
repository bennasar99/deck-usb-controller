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

import os
import signal
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


def main():
    global _stop
    manager = GadgetManager(logger=log, sudo=False)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

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
    switched_to_hid = False
    hid_recoveries = 0

    log("USB XInput controller daemon started (build 2026-08-xinput-ffs); "
        "waiting for the 'USB Gamepad' game.")
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
                    mode = "hid" if switched_to_hid else "xinput"
                    log("Setting up USB %s controller..." %
                        ("HID (compatibility)" if switched_to_hid
                         else "Xbox 360 (XInput)"))
                    manager.start(mode=mode)
                    gadget_up = True
                    log("USB gadget ready (%s)" % manager.path)
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
            if gadget_up and now - last_stats >= 15.0:
                last_stats = now
                log("Status: input_events=%d polls=%s errors=%s%s" % (
                    ev_count, manager.polls, manager.errors,
                    "" if manager.enabled else " [host not configured yet]"))
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
                               if switched_to_hid
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
                    kind = ("HID" if switched_to_hid else "XInput")
                    log("First %s report consumed by the host (polls=%s). "
                        "The PC should now show a working controller."
                        % (kind, manager.polls))
                prev_polls = manager.polls
                prev_polls_since = now
            elif (manager.enabled and ev_count > 0
                  and now - last_event < 15.0
                  and now - prev_polls_since > 8.0):
                prev_polls_since = now   # rate-limit recovery to 1 / 8 s
                if not switched_to_hid:
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
                        # Fresh manager + grace period so the previous
                        # gadget's kernel state is fully gone.
                        manager = GadgetManager(logger=log, sudo=False)
                        time.sleep(1.0)
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