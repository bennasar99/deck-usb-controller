"""XInput (Xbox 360 wired controller) USB report format.

The Xbox 360 wired controller is *not* a HID device: it uses vendor-specific
USB interfaces (class ``0xFF``, subclass ``0x5D``) and a simple binary wire
protocol. This module implements that protocol's data formats:

* The 20-byte input report sent from the device to the host on interface 0's
  IN endpoint (``0x81``) whenever the pad state changes.
* Parsing of the output (rumble / LED) commands the host sends to the OUT
  endpoint (``0x01``).
* The HID report descriptor returned for the class-specific
  ``GET_DESCRIPTOR(HID Report)`` control request Windows issues while
  enumerating interface 0. Its content is not used by the ``xusb`` driver
  (binding happens by VID/PID/class), but answering it keeps enumeration
  smooth.
"""

import struct

# ---------------------------------------------------------------------------
# Input report (device -> host, endpoint 0x81, 20 bytes)
# ---------------------------------------------------------------------------

INPUT_REPORT_TYPE = 0x00
INPUT_REPORT_SIZE = 0x14          # marker byte: "this report is 20 bytes"
INPUT_REPORT_LENGTH = 20

# Button bits inside the little-endian 16-bit field at bytes 2..3.
DPAD_UP = 0x0001
DPAD_DOWN = 0x0002
DPAD_LEFT = 0x0004
DPAD_RIGHT = 0x0008
START = 0x0010
BACK = 0x0020
L3 = 0x0040
R3 = 0x0080
LB = 0x0100
RB = 0x0200
GUIDE = 0x0400
A = 0x1000
B = 0x2000
X = 0x4000
Y = 0x8000

# ---------------------------------------------------------------------------
# Output reports (host -> device, endpoint 0x01)
# ---------------------------------------------------------------------------

RUMBLE_TYPE = 0x00                # [0x00][len][?][big motor][small motor]...
LED_TYPE = 0x01                   # [0x01][len][pattern]

LED_OFF = 0x00
LED_BLINK_ALL = 0x01
LED_PLAYER_1 = 0x02
LED_PLAYER_2 = 0x03
LED_PLAYER_3 = 0x04
LED_PLAYER_4 = 0x05


def clamp(value, lo, hi):
    """Clamp *value* into the inclusive range ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return int(value)


class XInputReport:
    """A single 20-byte XInput input report."""

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

    def set_dpad(self, up=False, down=False, left=False, right=False):
        """Set the D-pad bits directly (the XInput pad has no hat switch)."""
        self.buttons &= ~(DPAD_UP | DPAD_DOWN | DPAD_LEFT | DPAD_RIGHT)
        if up:
            self.buttons |= DPAD_UP
        if down:
            self.buttons |= DPAD_DOWN
        if left:
            self.buttons |= DPAD_LEFT
        if right:
            self.buttons |= DPAD_RIGHT

    def set_trigger(self, which, value):
        """Set a trigger value (0..255). ``which`` is 0 for left, 1 for right."""
        value = clamp(value, 0, 255)
        if which == 0:
            self.left_trigger = value
        else:
            self.right_trigger = value

    def set_stick(self, which, x, y):
        """Set a stick position (-32768..32767). ``which`` is 0 for left, 1 for right."""
        x = clamp(x, -32768, 32767)
        y = clamp(y, -32768, 32767)
        if which == 0:
            self.left_x = x
            self.left_y = y
        else:
            self.right_x = x
            self.right_y = y

    def to_bytes(self):
        frame = bytearray(INPUT_REPORT_LENGTH)
        frame[0] = INPUT_REPORT_TYPE
        frame[1] = INPUT_REPORT_SIZE
        # Bytes 2..3: button bitfield (little endian).
        struct.pack_into("<H", frame, 2, self.buttons & 0xFFFF)
        # Bytes 4..5: analog triggers.
        frame[4] = self.left_trigger
        frame[5] = self.right_trigger
        # Bytes 6..13: sticks as four little-endian int16 values.
        struct.pack_into("<hhhh", frame, 6,
                         self.left_x, self.left_y,
                         self.right_x, self.right_y)
        # Bytes 14..19 stay zero (reserved).
        return bytes(frame)


def parse_rumble(data):
    """Extract ``(big_motor, small_motor)`` from a rumble command, or None."""
    if len(data) >= 5 and data[0] == RUMBLE_TYPE:
        return data[3], data[4]
    return None


def parse_led(data):
    """Return the LED pattern byte of an LED command, or None."""
    if len(data) >= 3 and data[0] == LED_TYPE:
        return data[2]
    return None


# ---------------------------------------------------------------------------
# HID report descriptor answered on the class-specific GET_DESCRIPTOR request
# ---------------------------------------------------------------------------
#
# Windows' xusb driver requests this descriptor from interface 0 during
# enumeration even though the pad is not a HID-class device. The real
# controller answers with a gamepad descriptor; the driver does not act on its
# content, but a well-formed answer keeps the enumeration clean.

HID_REPORT_DESCRIPTOR = bytes([
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