"""Minimal pure-Python evdev reader.

Reads Linux input event devices (``/dev/input/event*``) without any third
party dependency. SteamOS ships Python 3.11 with the ``fcntl`` module, which is
all this needs.

The Steam Deck exposes two gamepad sources:

* Steam's virtual gamepad -- "Microsoft X-Box 360 pad 0" (28de:11ff), created
  by Steam while it is running. This is the preferred source because it is a
  plain xpad-style device with sticks, triggers, D-pad and all face buttons.
* The raw internal controller -- "Steam Deck" (28de:1205) via the ``hid-steam``
  driver, available when Steam is not running.
"""

import array
import glob
import os
import select
import struct

try:
    import fcntl  # Linux only; used to read device capabilities.
except ImportError:  # pragma: no cover - exercised only on non-Linux hosts
    fcntl = None

# ioctl encoding.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, type_, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (ord(type_) << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _ioc_read(type_, nr, size):
    return _ioc(_IOC_READ, type_, nr, size)


EVIOCGNAME = _ioc_read("E", 0x06, 256)
EVIOCGID = _ioc_read("E", 0x02, 8)
EVIOCGBIT = lambda ev, length: _ioc_read("E", 0x20 + ev, length)
EVIOCGABS = lambda code: _ioc_read("E", 0x40 + code, 24)

# Event types.
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

# Absolute axis codes.
ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02
ABS_RX = 0x03
ABS_RY = 0x04
ABS_RZ = 0x05
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11
ABS_HAT1X = 0x12
ABS_HAT1Y = 0x13
ABS_HAT2X = 0x14
ABS_HAT2Y = 0x15
ABS_HAT3X = 0x16
ABS_HAT3Y = 0x17

# Button codes.
BTN_DPAD_UP = 0x220
BTN_DPAD_DOWN = 0x221
BTN_DPAD_LEFT = 0x222
BTN_DPAD_RIGHT = 0x223
BTN_A = 0x130
BTN_B = 0x131
BTN_X = 0x133
BTN_Y = 0x134
BTN_TL = 0x136
BTN_TR = 0x137
BTN_TL2 = 0x138
BTN_TR2 = 0x139
BTN_SELECT = 0x13A
BTN_START = 0x13B
BTN_MODE = 0x13C
BTN_THUMBL = 0x13D
BTN_THUMBR = 0x13E

EV_MAX = 0x1F
KEY_MAX = 0x2FF
ABS_MAX = 0x3F

_EVENT_FORMAT = "qqHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)

# Axis -> (out_low, out_high)
STICK_AXES = {ABS_X: (-32768, 32767), ABS_Y: (-32768, 32767),
              ABS_RX: (-32768, 32767), ABS_RY: (-32768, 32767)}
TRIGGER_AXES = {ABS_Z: (0, 255), ABS_RZ: (0, 255)}

# Any ABS axis a gamepad-like device might expose. Being present on its own is
# not enough (the Deck exposes extra touchpad/sensor axes); it is combined with
# the button check below.
GAMEPAD_AXES = frozenset({
    ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ,
    ABS_HAT0X, ABS_HAT0Y, ABS_HAT1X, ABS_HAT1Y,
    ABS_HAT2X, ABS_HAT2Y, ABS_HAT3X, ABS_HAT3Y,
})

# Gamepad button codes (BTN_A == BTN_SOUTH, BTN_B == BTN_EAST, ... so the face
# buttons are covered even on devices that use the *_BTN aliases).
GAMEPAD_BUTTONS = frozenset({
    BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT,
    BTN_A, BTN_B, BTN_X, BTN_Y,
    BTN_TL, BTN_TR, BTN_TL2, BTN_TR2,
    BTN_SELECT, BTN_START, BTN_MODE,
    BTN_THUMBL, BTN_THUMBR,
})

# USB ids used by the Steam Deck's own controllers.
STEAM_VENDOR = 0x28DE
DECK_BUILTIN_PRODUCT = 0x1205
STEAM_VIRTUAL_PRODUCT = 0x11FF


class EvdevDevice:
    """Opens and reads a single ``/dev/input/event*`` device."""

    def __init__(self, path):
        if fcntl is None:
            raise EnvironmentError(
                "evdev devices are only available on Linux (this is a Steam Deck plugin).")
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            self.name = self._read_name()
            self.vendor, self.product = self._read_id()
            self.caps = self._read_caps()
            self.absinfo = self._read_absinfo()
        except Exception:
            os.close(self.fd)
            raise

    def _read_name(self):
        buf = bytearray(256)
        fcntl.ioctl(self.fd, EVIOCGNAME, buf)
        return bytes(buf).split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    def _read_id(self):
        buf = struct.pack("4H", 0, 0, 0, 0)
        fcntl.ioctl(self.fd, EVIOCGID, buf)
        # struct input_id: { bustype, vendor, product, version }.
        bustype, vendor, product, version = struct.unpack("4H", buf)
        return vendor, product

    def _read_caps(self):
        caps = {}
        for ev in range(EV_MAX + 1):
            size = _cap_bytes(ev)
            if size <= 0:
                continue
            buf = array.array("B", [0]) * size
            try:
                fcntl.ioctl(self.fd, EVIOCGBIT(ev, size), buf)
            except OSError:
                # EVIOCGBIT returns -EINVAL for event types the kernel does not
                # handle (e.g. EV_PWR and the reserved types above EV_FF_STATUS).
                continue
            bits = set()
            for i, byte in enumerate(buf):
                for bit in range(8):
                    if byte & (1 << bit):
                        bits.add(i * 8 + bit)
            caps[ev] = bits
        return caps

    def _read_absinfo(self):
        info = {}
        for code in range(ABS_MAX + 1):
            buf = bytearray(24)
            try:
                fcntl.ioctl(self.fd, EVIOCGABS(code), buf)
            except OSError:
                continue
            value, minimum, maximum, fuzz, flat, res = struct.unpack("6i", buf)
            info[code] = {"value": value, "min": minimum, "max": maximum}
        return info

    def looks_like_gamepad(self):
        """True when the device plausibly carries gamepad controls.

        This is intentionally lenient: the Deck's controls appear either as
        Steam's virtual xpad ("Microsoft X-Box 360 pad 0") or as the raw
        hid-steam device ("Steam Deck"), which differ in their caps. A device
        must offer a stick axis plus either gamepad buttons or a hat/trigger
        axis; that excludes keyboards, touchpads and accelerometers while
        accepting both Deck layouts.
        """
        abs_bits = self.caps.get(EV_ABS, set())
        key_bits = self.caps.get(EV_KEY, set())
        has_stick = bool(abs_bits & {ABS_X, ABS_Y, ABS_RX, ABS_RY})
        has_button = bool(key_bits & GAMEPAD_BUTTONS)
        has_hat = bool(abs_bits & {ABS_HAT0X, ABS_HAT0Y,
                                   ABS_HAT1X, ABS_HAT1Y,
                                   ABS_HAT2X, ABS_HAT2Y,
                                   ABS_HAT3X, ABS_HAT3Y})
        return has_stick and (has_button or has_hat)

    def is_steam_virtual(self):
        return self.vendor == STEAM_VENDOR and self.product == STEAM_VIRTUAL_PRODUCT

    def is_deck_builtin(self):
        return (self.vendor == STEAM_VENDOR and self.product == DECK_BUILTIN_PRODUCT) \
            or "steam deck" in self.name.lower()

    def describe(self):
        """Rich device descriptor used by the Decky UI and auto-pick logic."""
        if self.is_steam_virtual():
            kind = "steam-virtual"
        elif self.is_deck_builtin():
            kind = "deck-builtin"
        elif self.vendor == STEAM_VENDOR:
            kind = "steam"
        else:
            kind = "gamepad"
        return {
            "path": self.path,
            "name": self.name,
            "vendor": self.vendor,
            "product": self.product,
            "kind": kind,
            "preferred": kind in ("steam-virtual", "deck-builtin"),
        }

    def poll(self, timeout=0.05):
        """Return the next (type, code, value) event, or None on timeout."""
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return None
        try:
            raw = os.read(self.fd, _EVENT_SIZE)
        except OSError:
            return None
        if len(raw) != _EVENT_SIZE:
            return None
        _, _, ev_type, code, value = struct.unpack(_EVENT_FORMAT, raw)
        return ev_type, code, value

    def alive(self):
        """True while the kernel still exposes the device this fd refers to.

        Steam recreates its virtual gamepad when Steam Input reloads, which
        kills the old device node; a stale fd then silently returns nothing.
        EVIOCGID fails with ENODEV on a destroyed device.
        """
        try:
            buf = struct.pack("4H", 0, 0, 0, 0)
            fcntl.ioctl(self.fd, EVIOCGID, buf)
            return True
        except OSError:
            return False

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _cap_bytes(ev):
    if ev == EV_KEY:
        return (KEY_MAX + 1 + 7) // 8
    if ev == EV_ABS:
        return (ABS_MAX + 1 + 7) // 8
    if ev == EV_SYN:
        return 8
    if ev == EV_REL:
        return 32
    return 64


def discover_gamepads():
    """Return ``{"devices": [...], "notes": [...]}`` for gamepad-like devices.

    Detection is lenient so the Steam Deck's integrated controller is found
    regardless of which frontend exposes it (Steam virtual gamepad while Steam
    runs, raw hid-steam device otherwise). Devices are sorted with the Deck's
    own controller first.
    """
    devices = []
    notes = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            dev = EvdevDevice(path)
        except PermissionError as exc:
            notes.append(f"{path}: permission denied ({exc})")
            continue
        except OSError:
            continue
        try:
            if not dev.looks_like_gamepad():
                continue
            devices.append(dev.describe())
        finally:
            dev.close()
    devices.sort(key=lambda d: (0 if d["kind"] == "steam-virtual"
                                else 1 if d["kind"] == "deck-builtin" else 2,
                                d["path"]))
    return {"devices": devices, "notes": notes}
