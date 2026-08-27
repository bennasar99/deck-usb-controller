"""Checks for the XInput (Xbox 360 wired) report format and axis scaling.

Run with a plain Python 3 interpreter:

    python tests/test_report.py
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.xinput_report import (
    A, B, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_UP,
    INPUT_REPORT_LENGTH, XInputReport, HID_REPORT_DESCRIPTOR,
    parse_led, parse_rumble,
)
from backend.controller import (
    _read_axis, _to_stick, _to_trigger, _pick_axis, build_xinput_frame,
    build_hid_frame,
)
from backend import evdev_reader
from backend import xinput_ffs
from backend.gamepad_report import (
    build_report_descriptor, HidGamepadReport,
    DPAD_UP as HID_DPAD_UP, DPAD_LEFT as HID_DPAD_LEFT, REPORT_LENGTH,
)


class _Reader:
    def __init__(self, absinfo):
        self.absinfo = absinfo


def _axis(code, value, lo=-32768, hi=32767):
    reader = _Reader({code: {"min": lo, "max": hi}})
    state = {code: value}
    return _read_axis(reader, state, code, 0.10)


def test_neutral_frame():
    frame = XInputReport().to_bytes()
    assert len(frame) == INPUT_REPORT_LENGTH == 20, \
        "XInput input reports are 20 bytes"
    assert frame[0] == 0x00 and frame[1] == 0x14, \
        "report starts with type=0x00 size=0x14 marker"
    assert frame[2:] == bytes(18), "neutral report is all zeros"


def test_button_press():
    report = XInputReport()
    report.set_button(A, True)
    frame = report.to_bytes()
    buttons = struct.unpack_from("<H", frame, 2)[0]
    assert buttons == A, f"A button should map to 0x{A:x}, got 0x{buttons:x}"


def test_dpad_bits():
    report = XInputReport()
    report.set_dpad(up=True)
    assert struct.unpack_from("<H", report.to_bytes(), 2)[0] == DPAD_UP
    report.set_dpad(down=True)
    assert struct.unpack_from("<H", report.to_bytes(), 2)[0] == DPAD_DOWN
    report.set_dpad(left=True)
    assert struct.unpack_from("<H", report.to_bytes(), 2)[0] == DPAD_LEFT
    report.set_dpad(right=True)
    assert struct.unpack_from("<H", report.to_bytes(), 2)[0] == DPAD_RIGHT
    report.set_dpad(up=True, right=True)
    assert struct.unpack_from("<H", report.to_bytes(), 2)[0] == (DPAD_UP | DPAD_RIGHT)


def test_sticks_and_triggers():
    report = XInputReport()
    report.set_stick(0, 1000, -2000)
    report.set_stick(1, -300, 400)
    report.set_trigger(0, 200)
    report.set_trigger(1, 90)
    frame = report.to_bytes()
    lx, ly, rx, ry = struct.unpack_from("<hhhh", frame, 6)
    assert (lx, ly) == (1000, -2000)
    assert (rx, ry) == (-300, 400)
    assert frame[4] == 200 and frame[5] == 90


def test_scale_axis_deadzone():
    assert _axis(evdev_reader.ABS_X, 0) == 0
    assert _to_stick(_axis(evdev_reader.ABS_X, 32767)) == 32767
    assert _to_stick(_axis(evdev_reader.ABS_X, -32768)) == -32768
    assert _to_stick(_axis(evdev_reader.ABS_X, 2000)) == 0, "inside deadzone -> neutral"
    assert _to_stick(_axis(evdev_reader.ABS_X, 10000)) > 0, "outside deadzone -> active"


def test_deck_trigger_axes():
    reader = _Reader({evdev_reader.ABS_HAT2Y: {"min": 0, "max": 32767},
                      evdev_reader.ABS_HAT2X: {"min": 0, "max": 32767}})
    assert _pick_axis(reader, evdev_reader.ABS_Z, evdev_reader.ABS_HAT2Y) == evdev_reader.ABS_HAT2Y
    assert _to_trigger(_read_axis(reader, {evdev_reader.ABS_HAT2Y: 0}, evdev_reader.ABS_HAT2Y, 0.02)) == 0
    assert _to_trigger(_read_axis(reader, {evdev_reader.ABS_HAT2Y: 32767}, evdev_reader.ABS_HAT2Y, 0.02)) == 255
    assert _to_trigger(_read_axis(reader, {evdev_reader.ABS_HAT2Y: 1000}, evdev_reader.ABS_HAT2Y, 0.02)) < 255


def test_xpad_trigger_axes():
    reader = _Reader({evdev_reader.ABS_Z: {"min": 0, "max": 255},
                      evdev_reader.ABS_RZ: {"min": 0, "max": 255}})
    assert _pick_axis(reader, evdev_reader.ABS_Z, evdev_reader.ABS_HAT2Y) == evdev_reader.ABS_Z
    assert _to_trigger(_read_axis(reader, {evdev_reader.ABS_Z: 255}, evdev_reader.ABS_Z, 0.02)) == 255


def test_build_frame_from_state():
    """build_xinput_frame maps a Steam virtual pad state onto the wire format."""
    reader = _Reader({
        evdev_reader.ABS_X: {"min": -32768, "max": 32767},
        evdev_reader.ABS_Y: {"min": -32768, "max": 32767},
        evdev_reader.ABS_RX: {"min": -32768, "max": 32767},
        evdev_reader.ABS_RY: {"min": -32768, "max": 32767},
        evdev_reader.ABS_Z: {"min": 0, "max": 255},
        evdev_reader.ABS_RZ: {"min": 0, "max": 255},
    })
    state = {
        evdev_reader.BTN_A: 1,
        evdev_reader.BTN_START: 1,
        evdev_reader.ABS_HAT0X: 0,
        evdev_reader.ABS_HAT0Y: -1,   # dpad up
        evdev_reader.ABS_X: 16384,
        evdev_reader.ABS_Y: 0,
        evdev_reader.ABS_RX: 0,
        evdev_reader.ABS_RY: 0,
        evdev_reader.ABS_Z: 128,
        evdev_reader.ABS_RZ: 0,
    }
    frame = build_xinput_frame(reader, state)
    buttons = struct.unpack_from("<H", frame, 2)[0]
    assert buttons & A and buttons & 0x0010, "A + START pressed"
    assert buttons & DPAD_UP, "hat up -> DPAD_UP bit"
    lx = struct.unpack_from("<h", frame, 6)[0]
    assert lx > 0, "left stick pushed right"
    assert frame[4] == 128, "left trigger value forwarded"


def test_parse_output_commands():
    rumble = bytes([0x00, 0x08, 0x00, 0xFF, 0x40, 0x00, 0x00, 0x00])
    assert parse_rumble(rumble) == (0xFF, 0x40)
    assert parse_rumble(bytes([0x01, 0x03, 0x02])) is None
    assert parse_led(bytes([0x01, 0x03, 0x05])) == 0x05
    assert parse_led(rumble) is None


def test_hid_report_descriptor_is_valid_bytes():
    assert len(HID_REPORT_DESCRIPTOR) > 60
    assert HID_REPORT_DESCRIPTOR[0] == 0x05  # Usage Page
    assert HID_REPORT_DESCRIPTOR[-1] == 0xC0  # End Collection


def test_ffs_descriptors_blob():
    full = xinput_ffs.build_descriptors_blob()
    plain = xinput_ffs.build_descriptors_blob(force_plain=True)
    for blob in (full, plain):
        magic, length = struct.unpack_from("<II", blob, 0)
        assert magic == xinput_ffs.FUNCTIONFS_DESCRIPTORS_MAGIC_V2
        assert length == len(blob), "header length must match the blob size"

    raw_fs = b"".join(xinput_ffs.RAW_DESCRIPTORS)
    raw_ss = b"".join(xinput_ffs.RAW_DESCRIPTORS_SS)
    # Full variant: FS + HS + SS descriptor sets (PiKVM style).
    _, _, flags, fs_count, hs_count, ss_count = \
        struct.unpack_from("<IIIIII", full, 0)
    assert flags & xinput_ffs.FUNCTIONFS_HAS_FS_CONF
    assert flags & xinput_ffs.FUNCTIONFS_HAS_HS_CONF
    assert flags & xinput_ffs.FUNCTIONFS_HAS_SS_CONF
    assert fs_count == hs_count == len(xinput_ffs.RAW_DESCRIPTORS)
    assert ss_count == len(xinput_ffs.RAW_DESCRIPTORS_SS)
    assert len(full) == 24 + 2 * len(raw_fs) + len(raw_ss)
    # Plain fallback: FS + HS only.
    _, _, flags2, fs_count2, hs_count2 = struct.unpack_from("<IIIII", plain, 0)
    assert flags2 & xinput_ffs.FUNCTIONFS_HAS_FS_CONF
    assert flags2 & xinput_ffs.FUNCTIONFS_HAS_HS_CONF
    assert not (flags2 & xinput_ffs.FUNCTIONFS_HAS_SS_CONF)
    assert len(plain) == 20 + 2 * len(raw_fs)

    # PiKVM geometry: vendor class FF/5D/01 interface; IN interval 4,
    # OUT interval 8; no class-specific descriptors (kernel rejects them).
    iface = xinput_ffs.INTERFACE_0_DESCRIPTOR
    assert iface[5:8] == bytes([0xFF, 0x5D, 0x01])
    eps = [d for d in xinput_ffs.RAW_DESCRIPTORS if d[0] == 0x07]
    assert [e[2] for e in eps] == [0x81, 0x01]
    assert [e[6] for e in eps] == [4, 8], "IN/OUT bInterval"
    assert not any(d[1] == 0x21 for d in xinput_ffs.RAW_DESCRIPTORS)

    # Interface 0 must be the Xbox 360 vendor-specific one, protocol 0x01.
    iface0 = xinput_ffs.INTERFACE_0_DESCRIPTOR
    assert iface0[:5] == bytes([0x09, 0x04, 0x00, 0x00, 0x02])
    assert iface0[5:8] == bytes([0xFF, 0x5D, 0x01]), "interface class/sub/protocol"

    # Endpoint geometry must match PiKVM's proven layout:
    # IN 0x81 interval 4 and OUT 0x01 interval 8 (and nothing else).
    eps = [d for d in xinput_ffs.RAW_DESCRIPTORS if d[0] == 0x07]
    assert [e[2] for e in eps] == [0x81, 0x01]
    assert [e[6] for e in eps] == [4, 8], "IN/OUT bInterval"
    for ep in eps:
        assert ep[3] == 0x03, "all endpoints are interrupt type"
        assert ep[4] | (ep[5] << 8) == 32, "wMaxPacketSize=32"


def test_ffs_strings_blob():
    blob = xinput_ffs.build_strings_blob(("Foo", "Bar"))
    magic, length, str_count, lang_count = struct.unpack_from("<IIII", blob, 0)
    assert magic == xinput_ffs.FUNCTIONFS_STRINGS_MAGIC
    assert length == len(blob), "header length must match the blob size"
    assert str_count == 2 and lang_count == 1
    # Per language: LE16 lang code then NUL-terminated UTF-8 strings.
    lang = struct.unpack_from("<H", blob, 16)[0]
    assert lang == 0x0409
    body = blob[18:]
    parts = body.split(b"\x00")
    assert parts[0] == b"Foo" and parts[1] == b"Bar"
    assert all(part.decode("utf-8") == part.decode("utf-8")
               for part in parts if part), "strings must be valid UTF-8"


def test_dpad_diagonals():
    """Hat diagonals must press two XInput direction buttons at once."""
    reader = _Reader({
        evdev_reader.ABS_HAT0X: {"min": -1, "max": 1},
        evdev_reader.ABS_HAT0Y: {"min": -1, "max": 1},
    })
    state = {evdev_reader.ABS_HAT0X: -1, evdev_reader.ABS_HAT0Y: -1}
    buttons = struct.unpack_from("<H", build_xinput_frame(reader, state), 2)[0]
    assert buttons & DPAD_UP and buttons & DPAD_LEFT, "up-left diagonal"
    state = {evdev_reader.ABS_HAT0X: 1, evdev_reader.ABS_HAT0Y: 0}
    buttons = struct.unpack_from("<H", build_xinput_frame(reader, state), 2)[0]
    assert buttons == DPAD_RIGHT


def test_build_hid_frame():
    """build_hid_frame maps a Steam virtual pad state onto the HID format."""
    reader = _Reader({
        evdev_reader.ABS_X: {"min": -32768, "max": 32767},
        evdev_reader.ABS_Y: {"min": -32768, "max": 32767},
        evdev_reader.ABS_RX: {"min": -32768, "max": 32767},
        evdev_reader.ABS_RY: {"min": -32768, "max": 32767},
        evdev_reader.ABS_Z: {"min": 0, "max": 255},
        evdev_reader.ABS_RZ: {"min": 0, "max": 255},
    })
    state = {
        evdev_reader.BTN_A: 1,
        evdev_reader.BTN_START: 1,
        evdev_reader.ABS_HAT0X: -1,
        evdev_reader.ABS_HAT0Y: -1,   # up-left diagonal
        evdev_reader.ABS_X: 16384,
        evdev_reader.ABS_Y: 0,
        evdev_reader.ABS_RX: 0,
        evdev_reader.ABS_RY: 0,
        evdev_reader.ABS_Z: 128,
        evdev_reader.ABS_RZ: 0,
    }
    frame = build_hid_frame(reader, state)
    assert len(frame) == REPORT_LENGTH == 12
    buttons = struct.unpack_from("<H", frame, 0)[0]
    assert buttons & 0x0001, "A pressed"
    assert buttons & 0x0080, "start pressed"
    assert buttons & HID_DPAD_UP and buttons & HID_DPAD_LEFT, "up-left diagonal"
    assert frame[2] == 128, "left trigger forwarded"
    lx = struct.unpack_from("<h", frame, 4)[0]
    assert lx > 0, "left stick pushed right"


def test_hid_report_descriptor():
    desc = build_report_descriptor()
    # Vendor-defined top-level usage page (0xFF00): games must not see the
    # raw feed as a second gamepad; only the deck2xinput bridge reads it.
    assert desc[0] == 0x06 and desc[1:3] == bytes([0x00, 0xFF])
    assert desc[3] == 0x09 and desc[4] == 0x01   # Usage (1)
    assert desc[-1] == 0xC0
    # 15 buttons (incl. 4 D-pad bits) + triggers + 4 stick axes = 12 bytes.
    report = HidGamepadReport()
    report.set_button(0x0001, True)
    report.set_hat(up=True)
    report.set_trigger(1, 200)
    report.set_stick(0, -3000, 4000)
    frame = report.to_bytes()
    assert len(frame) == REPORT_LENGTH
    buttons = struct.unpack_from("<H", frame, 0)[0]
    assert buttons & 0x0001 and buttons & HID_DPAD_UP
    assert frame[3] == 200
    ly = struct.unpack_from("<h", frame, 6)[0]
    assert ly > 0


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
        print(f"ok - {test.__name__}")
    print(f"\n{len(tests)} tests passed")