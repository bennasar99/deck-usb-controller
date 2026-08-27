"""Standard USB HID gamepad format (the compatibility fallback).

Used when the host ignores the native XInput gadget (Windows refuses to
drive emulated 360 pads lacking hardware-authentic descriptors). Adopted
from the proven deck-usb-hid-controller layout with one deliberate change:
the D-pad is encoded as BUTTON BITS (11..14) inside a VENDOR-DEFINED
top-level collection (page 0xFF00) instead of a hat-switch usage.

Why: with a gamepad/hat usage, Windows exposes the raw feed as a second
gamepad that legacy DirectInput paths bind with mis-mapped directions
(180° rotations observed in games), and host-side hat heuristics differ
per driver. Button bits under a vendor page are unambiguous — only the
deck2xinput Windows bridge consumes this device (matches VID/PID), and it
replays the bits into a ViGEmBus XInput pad.

Wire layout (12 bytes):
  byte 0..1   LE16 buttons: A B X Y LB RB BACK START GUIDE L3 R3
              + D-pad bits: UP=0x0800 DOWN=0x1000 LEFT=0x2000 RIGHT=0x4000
  byte 2      left trigger  (0..255)
  byte 3      right trigger (0..255)
  byte 4..11  sticks: LX, LY, RX, RY as LE int16 (-32768..32767)
"""

import struct

# Button bit positions (LE16 field).
A = 0x0001
B = 0x0002
X = 0x0004
Y = 0x0008
LB = 0x0010
RB = 0x0020
BACK = 0x0040
START = 0x0080
GUIDE = 0x0100
L3 = 0x0200
R3 = 0x0400

# D-pad as button bits.
DPAD_UP = 0x0800
DPAD_DOWN = 0x1000
DPAD_LEFT = 0x2000
DPAD_RIGHT = 0x4000

# Aliases kept so callers can use evdev-style names interchangeably.
BTN_A = A
BTN_B = B
BTN_X = X
BTN_Y = Y
BTN_LB = LB
BTN_RB = RB
BTN_BACK = BACK
BTN_START = START
BTN_GUIDE = GUIDE
BTN_LS = L3
BTN_RS = R3

REPORT_LENGTH = 12


def clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return int(value)


# HID report descriptor: vendor-defined top-level usage (page 0xFF00) so
# games never see this raw feed as a second gamepad; D-pad as button bits
# (Usage 12..15 of the Button page) so no host hat heuristic can remap it.
GAMEPAD_REPORT_DESCRIPTOR = bytes([
    0x06, 0x00, 0xFF,  # Usage Page (Vendor Defined 0xFF00)
    0x09, 0x01,        # Usage (1)
    0xA1, 0x01,        # Collection (Application)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x0F,        #   Usage Maximum (Button 15)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x0F,        #   Report Count (15)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x03,        #   Input (Constant) -- padding bit 15
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x33,        #   Usage (Rx) -- left trigger
    0x09, 0x34,        #   Usage (Ry) -- right trigger
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x30,        #   Usage (X) -- left stick X
    0x09, 0x31,        #   Usage (Y) -- left stick Y
    0x16, 0x00, 0x80,  #   Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #   Logical Maximum (32767)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x32,        #   Usage (Z) -- right stick X
    0x09, 0x35,        #   Usage (Rz) -- right stick Y
    0x16, 0x00, 0x80,  #   Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #   Logical Maximum (32767)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0xC0,              # End Collection
])


def build_report_descriptor():
    """Return the binary HID report descriptor for the 12-byte gamepad."""
    return GAMEPAD_REPORT_DESCRIPTOR


class GamepadReport:
    """A single 12-byte generic gamepad input report."""

    __slots__ = ("buttons", "left_trigger", "right_trigger",
                 "left_x", "left_y", "right_x", "right_y")

    def __init__(self):
        self.reset()

    def reset(self):
        self.buttons = 0
        self.left_trigger = 0
        self.right_trigger = 0
        self.left_x = 0
        self.left_y = 0
        self.right_x = 0
        self.right_y = 0

    def set_button(self, button_bit, pressed):
        if pressed:
            self.buttons |= button_bit
        else:
            self.buttons &= ~button_bit

    def set_hat(self, up=False, down=False, left=False, right=False):
        """D-pad as button bits (compat API: same signature as before)."""
        self.set_button(DPAD_UP, up)
        self.set_button(DPAD_DOWN, down)
        self.set_button(DPAD_LEFT, left)
        self.set_button(DPAD_RIGHT, right)

    def set_trigger(self, which, value):
        """Set a trigger value (0..255). ``which`` is 0 left, 1 right."""
        value = clamp(value, 0, 255)
        if which == 0:
            self.left_trigger = value
        else:
            self.right_trigger = value

    def set_stick(self, which, x, y):
        x = clamp(x, -32768, 32767)
        y = clamp(y, -32768, 32767)
        if which == 0:
            self.left_x = x
            self.left_y = y
        else:
            self.right_x = x
            self.right_y = y

    def to_bytes(self):
        frame = bytearray(REPORT_LENGTH)
        struct.pack_into("<H", frame, 0, self.buttons & 0xFFFF)
        frame[2] = self.left_trigger
        frame[3] = self.right_trigger
        struct.pack_into("<hhhh", frame, 4,
                         self.left_x, self.left_y,
                         self.right_x, self.right_y)
        return bytes(frame)


# Alias for code that predates the rename.
HidGamepadReport = GamepadReport
