"""Standard USB HID gamepad format (the compatibility fallback).

Used when the host ignores the native XInput gadget (Windows refuses to
drive emulated 360 pads lacking hardware-authentic descriptors). This is a
verbatim adoption of the PROVEN-WORKING f_hid layout from the earlier
deck-usb-hid-controller implementation -- a classic generic gamepad that
every OS binds out of the box:

* 16 buttons + 8-bit hat (0..7 directions, 0x0F centered) + two 8-bit
  triggers + two 16-bit sticks.
* 13-byte input report written to ``/dev/hidg0``.
"""

import struct

# Button bit positions (16-bit field).
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

# Hat switch: 0..7 clockwise from up; 0x0F = centered (null state).
HAT_CENTER = 0x0F
HAT_UP = 0x0
HAT_UP_RIGHT = 0x1
HAT_RIGHT = 0x2
HAT_DOWN_RIGHT = 0x3
HAT_DOWN = 0x4
HAT_DOWN_LEFT = 0x5
HAT_LEFT = 0x6
HAT_UP_LEFT = 0x7
# Alias: neutral == centered.
HAT_NEUTRAL = HAT_CENTER

REPORT_LENGTH = 13


def clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return int(value)


# HID report descriptor: byte-identical to the deck-usb-hid-controller
# implementation that was verified end-to-end against Windows.
GAMEPAD_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x35, 0x00,        #   Physical Minimum (0)
    0x45, 0x01,        #   Physical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x10,        #   Report Count (16)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x10,        #   Usage Maximum (Button 16)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x39,        #   Usage (Hat Switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x65, 0x14,        #   Unit (Degrees)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data, Variable, Absolute, Null State)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x33,        #   Usage (Rx) -- left trigger
    0x09, 0x34,        #   Usage (Ry) -- right trigger
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x30,        #   Usage (X) -- left stick X
    0x09, 0x31,        #   Usage (Y) -- left stick Y
    0x16, 0x00, 0x80,  #   Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #   Logical Maximum (32767)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
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
    """Return the binary HID report descriptor for the 13-byte gamepad."""
    return GAMEPAD_REPORT_DESCRIPTOR


class GamepadReport:
    """A single 13-byte generic gamepad input report."""

    __slots__ = ("buttons", "hat", "left_trigger", "right_trigger",
                 "left_x", "left_y", "right_x", "right_y")

    def __init__(self):
        self.reset()

    def reset(self):
        self.buttons = 0
        self.hat = HAT_CENTER
        self.left_trigger = 0
        self.right_trigger = 0
        self.left_x = 0
        self.left_y = 0
        self.right_x = 0
        self.right_y = 0

    def set_button(self, button_bit, pressed):
        if pressed:
            self.buttons |= button_bit

    def set_hat(self, up=False, down=False, left=False, right=False):
        if up and right:
            self.hat = HAT_UP_RIGHT
        elif down and right:
            self.hat = HAT_DOWN_RIGHT
        elif down and left:
            self.hat = HAT_DOWN_LEFT
        elif up and left:
            self.hat = HAT_UP_LEFT
        elif up:
            self.hat = HAT_UP
        elif down:
            self.hat = HAT_DOWN
        elif left:
            self.hat = HAT_LEFT
        elif right:
            self.hat = HAT_RIGHT
        else:
            self.hat = HAT_CENTER

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
        frame[2] = self.hat & 0x0F
        frame[3] = self.left_trigger
        frame[4] = self.right_trigger
        struct.pack_into("<hhhh", frame, 5,
                         self.left_x, self.left_y,
                         self.right_x, self.right_y)
        return bytes(frame)


# Alias for code that predates the rename.
HidGamepadReport = GamepadReport
