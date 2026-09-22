"""Disable the Deck's Bluetooth "speaker" (A2DP sink) role while the app's
Bluetooth is active.

On SteamOS the Bluetooth audio roles are provided by the WirePlumber bluez
monitor, not by BlueZ itself. Dropping the A2DP-sink (and headset AG) roles
with a user-level WirePlumber override stops the Deck from offering itself as
a speaker, so pairing a host for the BLE HID gamepad does not also pair it as
an audio device. The A2DP-source role is kept so the Deck can still output to
Bluetooth headphones.

WirePlumber 0.4 reads ``~/.config/wireplumber/bluetooth.lua.d/*.lua``; 0.5+
reads ``~/.config/wireplumber/bluetooth.conf.d/*.conf``. We write to whichever
the system ships and restart the deck user's wireplumber.

Everything is best effort: failures are logged and never affect the gamepad.
Set ``/home/deck/usb-gamepad-bt-keep-audio`` to opt out entirely.
"""

import os
import subprocess

DECK_USER = "deck"
DECK_HOME = "/home/deck"
KEEP_AUDIO_FLAG = os.path.join(DECK_HOME, "usb-gamepad-bt-keep-audio")

_LUA_NAME = "61-usb-gamepad.lua"
_CONF_NAME = "61-usb-gamepad.conf"

_LUA_BODY = (
    "-- Managed by usb-gamepad: drop the A2DP-sink (speaker) role while the\n"
    "-- Bluetooth gamepad is active. a2dp_source is kept for BT headphones.\n"
    "bluez_monitor.properties = bluez_monitor.properties or {}\n"
    'bluez_monitor.properties["bluez5.roles"] = "[ a2dp_source ]"\n'
)

_CONF_BODY = (
    "# Managed by usb-gamepad: drop the A2DP-sink (speaker) role while the\n"
    "# Bluetooth gamepad is active. a2dp_source is kept for BT headphones.\n"
    "monitor.bluez.properties = {\n"
    "  bluez5.roles = [ a2dp_source ]\n"
    "}\n"
)


def _log(logger, msg):
    if logger:
        logger(msg)


def _targets():
    """Yield ``(directory, filename, body)`` for the WirePlumber style(s)
    present on this system. Empty when WirePlumber is not installed."""
    targets = []
    if os.path.isdir("/usr/share/wireplumber/bluetooth.lua.d"):
        targets.append((os.path.join(DECK_HOME,
                                     ".config/wireplumber/bluetooth.lua.d"),
                        _LUA_NAME, _LUA_BODY))
    if os.path.isdir("/usr/share/wireplumber/bluetooth.conf.d"):
        targets.append((os.path.join(DECK_HOME,
                                     ".config/wireplumber/bluetooth.conf.d"),
                        _CONF_NAME, _CONF_BODY))
    return targets


def _override_paths():
    return [os.path.join(d, n) for d, n, _ in _targets()]


def is_disabled():
    """True when our speaker-role override is currently in place."""
    return any(os.path.exists(p) for p in _override_paths())


def _restart_wireplumber(logger):
    try:
        uid = subprocess.check_output(["id", "-u", DECK_USER],
                                      text=True).strip() or "1000"
    except Exception:
        uid = "1000"
    runtime = "/run/user/%s" % uid
    bus = "unix:path=%s/bus" % runtime
    attempts = [
        ["sudo", "-u", DECK_USER, "env",
         "XDG_RUNTIME_DIR=" + runtime, "DBUS_SESSION_BUS_ADDRESS=" + bus,
         "systemctl", "--user", "restart", "wireplumber"],
        ["runuser", "-u", DECK_USER, "--", "env",
         "XDG_RUNTIME_DIR=" + runtime, "DBUS_SESSION_BUS_ADDRESS=" + bus,
         "systemctl", "--user", "restart", "wireplumber"],
    ]
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=15)
            if result.returncode == 0:
                return True
        except Exception:
            continue
    _log(logger, "Could not restart wireplumber; the audio-role change may "
                 "need a re-login to take effect.")
    return False


def disable_speaker(logger=None):
    """Install the override so the Deck is not offered as a BT speaker."""
    if os.path.exists(KEEP_AUDIO_FLAG):
        return False
    targets = _targets()
    if not targets:
        _log(logger, "WirePlumber config not found; speaker role unchanged.")
        return False
    changed = False
    for directory, name, body in targets:
        path = os.path.join(directory, name)
        try:
            os.makedirs(directory, exist_ok=True)
            if not os.path.exists(path):
                changed = True
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
        except OSError as exc:
            _log(logger, "Could not write %s: %s" % (path, exc))
            return False
    if changed:
        _restart_wireplumber(logger)
        _log(logger, "Bluetooth speaker (A2DP-sink) role disabled.")
    return changed


def restore_speaker(logger=None):
    """Remove the override so the Deck can act as a speaker again."""
    removed = False
    for path in _override_paths():
        if os.path.exists(path):
            try:
                os.remove(path)
                removed = True
            except OSError as exc:
                _log(logger, "Could not remove %s: %s" % (path, exc))
    if removed:
        _restart_wireplumber(logger)
        _log(logger, "Bluetooth speaker (A2DP-sink) role restored.")
    return removed
