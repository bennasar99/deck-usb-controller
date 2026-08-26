"""Native XInput controller via the kernel ``raw_gadget`` interface.

FunctionFS cannot carry the vendor-specific descriptors Windows' XInput
driver stack requires (its parser whitelist-rejects them -- see
CasperVM/360-raw-gadget for independent confirmation). ``raw_gadget``
instead delegates *every* descriptor and control transfer to user space,
letting us present a byte-exact wired Xbox 360 pad:

* Device: VID 0x045E PID 0x028E (wired receiver-pad identity), full speed,
  bcdUSB 0x0110 -- matching what real hardware advertises.
* Config: one vendor interface (class 0xFF/0x5D/0x01) carrying the
  authentic 17-byte class descriptor block real pads embed between the
  interface and its endpoints, followed by interrupt IN (input reports)
  and interrupt OUT (rumble/LED) endpoints.
* Control transfers answered in user space, including Windows'
  GET_LED_STATUS probes.
* Input path keeps ONE interrupt-IN transfer outstanding at all times
  (rewritten with the latest pad state), mirroring real controller
  behaviour: the host samples whenever it likes.

Requires ``modprobe raw_gadget`` (available on SteamOS; the daemon loads it).
"""

import errno
import fcntl
import queue
import struct
import threading
import time

from backend.xinput_report import (
    INPUT_REPORT_LENGTH, parse_led, parse_rumble,
)

RAW_DEVICE = "/dev/raw-gadget"

# linux/usb/raw_gadget.h ioctls ('U' = 0x55).
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, type_, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (ord(type_) << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _io(type_, nr):
    return _ioc(0, type_, nr, 0)


def _iow(type_, nr, size):
    return _ioc(_IOC_WRITE, type_, nr, size)


def _ior(type_, nr, size):
    return _ioc(_IOC_READ, type_, nr, size)


# struct usb_raw_init { char driver_name[128]; char device_name[128];
#                       __u8 speed; }                        -> 257 bytes
USB_RAW_IOCTL_INIT = _iow("U", 0, 128 + 128 + 1)
USB_RAW_IOCTL_RUN = _io("U", 1)
# struct usb_raw_event { __u32 type; __u16 length; __u8 data[]; } -> 8 bytes
USB_RAW_IOCTL_EVENT_FETCH = _ior("U", 2, 8)
# struct usb_raw_ep_io { __u16 ep; __u16 flags; __u32 length; __u8 data[]; }
_USB_RAW_EP_IO_HDR = 8
USB_RAW_IOCTL_EP0_WRITE = _iow("U", 3, _USB_RAW_EP_IO_HDR)
USB_RAW_IOCTL_EP0_READ = _iow("U", 4, _USB_RAW_EP_IO_HDR)
USB_RAW_IOCTL_EP_ENABLE = _iow("U", 5, 7)      # usb_endpoint_descriptor
USB_RAW_IOCTL_EP_DISABLE = _iow("U", 6, 4)
USB_RAW_IOCTL_EP_WRITE = _iow("U", 7, _USB_RAW_EP_IO_HDR)
USB_RAW_IOCTL_EP_READ = _iow("U", 8, _USB_RAW_EP_IO_HDR)
# int-valued and no-arg ioctls (value argument MUST be 0 for the latter).
USB_RAW_IOCTL_VBUS_DRAW = _iow("U", 10, 4)
USB_RAW_IOCTL_CONFIGURE = _io("U", 9)

# Event types.
USB_RAW_EVENT_CONNECT = 0
USB_RAW_EVENT_CONTROL = 1
USB_RAW_EVENT_SUSPEND = 2
USB_RAW_EVENT_RESUME = 3
USB_RAW_EVENT_DISCONNECT = 4

USB_RAW_IO_FLAGS_NONBLOCK = 0x0002

USB_SPEED_FULL = 2              # real 360 pads are full-speed devices

_DESCRIPTOR_TYPE_STRING = 0x03


def _le16(value):
    return struct.pack("<H", value)


DEVICE_DESCRIPTOR = bytes([
    0x12, 0x01,                 # bLength, bDescriptorType (device)
]) + _le16(0x0110) + bytes([
    0xFF, 0xFF, 0xFF,           # class/subclass/protocol: vendor specific
    0x40,                       # bMaxPacketSize0 (64)
]) + _le16(0x045E) + _le16(0x028E) + _le16(0x0114) + bytes([
    0x01, 0x02, 0x03,           # iManufacturer, iProduct, iSerialNumber
    0x01,                       # bNumConfigurations
])

# The authentic in-config vendor class descriptor block (real pads carry
# this between the interface descriptor and its endpoints; impossible to
# ship through FunctionFS, served verbatim here).
CLASS_BLOB = bytes([
    0x11, 0x21, 0x00, 0x01, 0x01, 0x25, 0x81, 0x14,
    0x00, 0x00, 0x00, 0x00, 0x13, 0x01, 0x08, 0x00,
    0x00,
])


def _endpoint(address, attributes, max_packet, interval):
    return bytes([0x07, 0x05, address, attributes]) \
        + _le16(max_packet) + bytes([interval])


CONFIGURATION_DESCRIPTOR = (
    bytes([0x09, 0x02])                     # config descriptor header
    + _le16(9 + 9 + len(CLASS_BLOB) + 14)   # wTotalLength
    + bytes([
        0x01,                               # bNumInterfaces
        0x01,                               # bConfigurationValue
        0x00,                               # iConfiguration
        0x80,                               # bmAttributes: bus powered
        0x32,                               # bMaxPower (100 mA)
        # Interface 0: vendor specific, Xbox 360 gamepad protocol.
        0x09, 0x04, 0x00, 0x00, 0x02, 0xFF, 0x5D, 0x01, 0x00,
    ])
    + CLASS_BLOB
    + _endpoint(0x81, 0x03, 32, 4)          # EP1 IN:  input reports
    + _endpoint(0x01, 0x03, 32, 8)          # EP2 OUT: rumble / LED
)

LANGID_DESCRIPTOR = bytes([0x04, _DESCRIPTOR_TYPE_STRING]) + _le16(0x0409)

STRINGS = {
    1: "\N{COPYRIGHT SIGN}Microsoft Corporation",
    2: "Controller",
    3: "05A4FF4",
}


def _string_descriptor(index):
    text = STRINGS[index]
    body = text.encode("utf-16-le")
    return bytes([len(body) + 2, _DESCRIPTOR_TYPE_STRING]) + body


class RawGadgetError(Exception):
    """Raised when the raw_gadget controller cannot be set up."""


class RawGadgetXInput:
    """User-space wired Xbox 360 controller served through raw_gadget."""

    def __init__(self, logger=None, rumble_callback=None,
                 device=RAW_DEVICE, udc=""):
        self.log = logger or (lambda msg: None)
        self.rumble_callback = rumble_callback
        self.device = device
        self.udc = udc
        self.fd = None
        self.enabled = False
        self._configured = False
        self.polls = 0
        self.errors = 0
        self.last_poll_time = 0.0
        self.last_led_pattern = 0
        self._stop = threading.Event()
        self._threads = []
        self._writer_queue = queue.Queue(maxsize=1)
        self.ep1_id = None
        self.ep2_id = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        """Attach raw_gadget to the UDC and present the controller.

        Tries INIT variants in order (some kernels reject anonymous
        registrations -- empty driver_name -- with EBUSY at RUN despite a
        free UDC): bare driver_name first, then a named one. Each variant
        gets a completely fresh open -> INIT -> RUN cycle.
        """
        last_error = None
        for variant, driver_name in ((f"anon-attempt-{i}", b"")
                                     for i in (1, 2)):
            try:
                self._attach_once(variant)
                self.log(f"raw_gadget attached ({variant}); waiting for "
                         "the host to enumerate.")
                return
            except RawGadgetError as exc:
                last_error = exc
                self.log(f"raw_gadget {variant} failed: {exc}")
                time.sleep(1.5)
        try:
            self._attach_once("named", b"deck_usb_xinput")
            self.log("raw_gadget attached (named driver); waiting for the "
                     "host to enumerate.")
            return
        except RawGadgetError as exc:
            raise RawGadgetError(
                f"All raw_gadget attach variants failed; last error: {exc}")

    def _attach_once(self, variant, driver_name=b""):
        try:
            self.fd = open(self.device, "rb+", buffering=0)
        except OSError as exc:
            raise RawGadgetError(
                f"Cannot open {self.device}: {exc}. Load raw_gadget with "
                "`modprobe raw_gadget`.") from exc
        try:
            fcntl.ioctl(self.fd, USB_RAW_IOCTL_INIT, self._init_blob(driver_name))
        except OSError as exc:
            self.close()
            raise RawGadgetError(f"USB_RAW_IOCTL_INIT failed: {exc}") from exc
        try:
            fcntl.ioctl(self.fd, USB_RAW_IOCTL_RUN, 0)
        except OSError as exc:
            self.close()
            raise RawGadgetError(
                f"[{variant}] USB_RAW_IOCTL_RUN failed: {exc} "
                f"(UDC '{self.udc}' state: {self._udc_state()})")
        self._start_thread(self._event_loop, "raw-event")

    def _udc_state(self):
        try:
            with open(f"/sys/class/udc/{self.udc}/state") as handle:
                return handle.read().strip()
        except OSError:
            return "<unreadable>"

    def _init_blob(self, driver_name=b""):
        return struct.pack("<128s128sB",
                           driver_name[:127], (self.udc or "").encode()[:127],
                           USB_SPEED_FULL)

    def start(self):
        """Attach raw_gadget to the UDC and present the controller.

        Tries INIT variants in order (some kernels reject anonymous
        registrations -- empty driver_name -- with EBUSY at RUN despite a
        free UDC): bare driver_name first, then a named one. Each variant
        gets a completely fresh open -> INIT -> RUN cycle.
        """
        last_error = None
        for variant, driver_name in ((f"anon-attempt-{i}", b"")
                                     for i in (1, 2)):
            try:
                self._attach_once(variant)
                self.log(f"raw_gadget attached ({variant}); waiting for "
                         "the host to enumerate.")
                return
            except RawGadgetError as exc:
                last_error = exc
                self.log(f"raw_gadget {variant} failed: {exc}")
        try:
            self._attach_once("named", b"deck_usb_xinput")
            self.log("raw_gadget attached (named driver); waiting for the "
                     "host to enumerate.")
            return
        except RawGadgetError as exc:
            last_error = exc
            raise RawGadgetError(
                f"All raw_gadget attach variants failed; last: {exc}")

    def stop(self):
        self._stop.set()
        # Closing the fd detaches from the UDC; stuck blocking ioctls fail.
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        self.close()
        self.enabled = False

    def close(self):
        if self.fd is not None:
            try:
                self.fd.close()
            except OSError:
                pass
            self.fd = None
        self._configured = False

    def reenumerate(self):
        """Force a fresh enumeration cycle (equivalent of a cable replug)."""
        self.log("raw_gadget: forcing re-enumeration...")
        self.stop()
        self._stop.clear()
        time.sleep(1.0)
        self.start()

    def wait_configured(self, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._configured:
                return True
            if self.fd is None:
                return False
            time.sleep(0.1)
        return False

    # -- data path -----------------------------------------------------------

    def send_report(self, data):
        """Queue the latest 20-byte report; never blocks the caller."""
        if not self._configured or self.fd is None:
            return None
        try:
            self._writer_queue.put_nowait(bytes(data))
            return 0
        except queue.Full:
            # Replace stale with fresher state.
            try:
                self._writer_queue.get_nowait()
                self._writer_queue.put_nowait(bytes(data))
                return 0
            except (queue.Empty, queue.Full):
                return 0
        except OSError as exc:
            return exc.errno

    def _start_thread(self, target, name):
        thread = threading.Thread(target=target, daemon=True, name=name)
        self._threads.append(thread)
        thread.start()

    # -- event loop (ep0 + configuration) ------------------------------------

    def _event_loop(self):
        buf = bytearray(8 + 4096)
        while not self._stop.is_set():
            try:
                fcntl.ioctl(self.fd, USB_RAW_IOCTL_EVENT_FETCH, buf,
                            mutate_flag=True)
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.EINTR):
                    break
                self.log(f"EVENT_FETCH failed: {exc}")
                continue
            ev_type, length = struct.unpack_from("<IH", buf, 0)
            data = bytes(buf[8:8 + length])
            if ev_type == USB_RAW_EVENT_CONNECT:
                self.log("Host connected (USB reset done).")
            elif ev_type == USB_RAW_EVENT_DISCONNECT:
                self.log("Host disconnected.")
                self._on_disconnect()
            elif ev_type == USB_RAW_EVENT_SUSPEND:
                self.log("Bus suspended.")
            elif ev_type == USB_RAW_EVENT_RESUME:
                self.log("Bus resumed.")
            elif ev_type == USB_RAW_EVENT_CONTROL:
                if length >= 8:
                    self._handle_control(data[:8])

    def _on_disconnect(self):
        with self._lock:
            self._configured = False
            self.enabled = False
            self.ep1_id = None
            self.ep2_id = None

    def _ep0_write(self, payload):
        arg = struct.pack("<HHI", 0, 0, len(payload)) + bytes(payload)
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP0_WRITE, arg)

    def _ep0_read(self, length):
        arg = bytearray(struct.pack("<HHI", 0, 0, length) + bytes(length))
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP0_READ, arg, mutate_flag=True)
        got = struct.unpack_from("<I", arg, 4)[0]
        return bytes(arg[_USB_RAW_EP_IO_HDR:_USB_RAW_EP_IO_HDR + got])

    def _ep0_ack(self):
        """Complete a no-data OUT control transfer.

        Per punktfunk's validated implementation: the status stage of a
        no-data OUT control is an IN token the device completes by a
        ZERO-LENGTH EP0_READ -- never by writing data (that yields EBUSY /
        -110 'can't set config').
        """
        self._ep0_read(0)

    def _ep0_stall(self):
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP0_STALL, 0)

    def _handle_control(self, setup):
        (req_type, request, w_value, w_index,
         w_length) = struct.unpack("<BBHHH", setup)
        direction_in = bool(req_type & 0x80)

        if req_type == 0x80 and request == 0x06:       # GET_DESCRIPTOR
            kind = w_value >> 8
            index = w_value & 0xFF
            if kind == 0x01:
                self.log("ep0: GET_DESCRIPTOR(Device)")
                self._ep0_write(DEVICE_DESCRIPTOR[:w_length])
                return
            if kind == 0x02:
                self.log(f"ep0: GET_DESCRIPTOR(Config) wLength={w_length}")
                self._ep0_write(CONFIGURATION_DESCRIPTOR[:w_length])
                return
            if kind == 0x03:
                payload = (LANGID_DESCRIPTOR if index == 0
                           else _string_descriptor(index))
                self.log(f"ep0: GET_DESCRIPTOR(String {index})")
                self._ep0_write(payload[:w_length])
                return
            self.log(f"ep0: GET_DESCRIPTOR({kind:#x}) -> zeros")
            self._ep0_write(bytes(w_length))
            return
        if req_type == 0x00 and request == 0x09:       # SET_CONFIGURATION
            self.log(f"ep0: SET_CONFIGURATION({w_value})")
            if w_value == 1:
                try:
                    # Recent kernels: the gadget only enters CONFIGURED
                    # after userspace issues VBUS_DRAW + CONFIGURE here.
                    fcntl.ioctl(self.fd, USB_RAW_IOCTL_VBUS_DRAW, 100)
                    fcntl.ioctl(self.fd, USB_RAW_IOCTL_CONFIGURE, 0)
                except OSError as exc:
                    self.log(f"VBUS_DRAW/CONFIGURE failed: {exc} "
                             "(older kernels auto-configure; continuing)")
                self._configure()
            self._ep0_ack()
            return
        if req_type == 0x00 and request == 0x05:       # SET_ADDRESS: kernel
            self._ep0_ack()
            return
        if req_type == 0x00 and request == 0x01:       # CLEAR_FEATURE
            self._ep0_ack()
            return
        if req_type == 0xC1 and request == 0x01:       # Get LED status
            payload = bytes([0x04, self.last_led_pattern, 0x00, 0x00])
            self._ep0_write(payload[:min(w_length, 4)])
            return
        if req_type == 0xC1 and request == 0x06:       # class descriptor
            self._ep0_write(CLASS_BLOB[:w_length])
            return
        if req_type == 0x00 and request == 0x0A:       # SET_INTERFACE
            self._ep0_ack()
            return
        if direction_in:
            self.log(f"ep0: vendor GET type={req_type:#04x} "
                     f"req={request:#04x} -> zeros")
            self._ep0_write(bytes(w_length))
            return
        # Data-out stage: drain it.
        remaining = w_length
        while remaining > 0:
            chunk = self._ep0_read(min(remaining, 256))
            if not chunk:
                break
            remaining -= len(chunk)
        if req_type == 0x21 and request == 0x09:       # LED command
            self.log(f"LED command: pattern={w_value >> 8 & 0xFF}")

    def _configure(self):
        if self._configured:
            return
        ep1 = fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP_ENABLE,
                          bytearray(_endpoint(0x81, 0x03, 32, 4)))
        ep2 = fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP_ENABLE,
                          bytearray(_endpoint(0x01, 0x03, 32, 8)))
        with self._lock:
            self.ep1_id, self.ep2_id = ep1, ep2
            self._configured = True
            self.enabled = True
        self.log("Endpoints enabled; streaming starts (XInput ready).")
        self._start_thread(self._writer_loop, "raw-writer")
        self._start_thread(self._rumble_loop, "raw-rumble")

    # -- streaming -----------------------------------------------------------

    def _ep_write(self, ep_id, payload):
        arg = struct.pack("<HHI", ep_id, 0, len(payload)) + bytes(payload)
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP_WRITE, arg)

    def _writer_loop(self):
        last_frame = bytes(INPUT_REPORT_LENGTH)
        try:
            self._ep_write(self.ep1_id, last_frame)
            self.polls += 1
            self.last_poll_time = time.time()
            self.log("First report consumed by the host "
                     "(raw XInput data path confirmed).")
        except OSError as exc:
            self._writer_error(exc)
        while not self._stop.is_set():
            try:
                frame = self._writer_queue.get(timeout=0.05)
                if len(frame) != INPUT_REPORT_LENGTH:
                    frame = bytes(frame).ljust(INPUT_REPORT_LENGTH, 0)
                last_frame = frame
            except queue.Empty:
                pass
            if self.fd is None or not self._configured:
                return
            try:
                # Blocking write: resubmits as soon as the host consumes,
                # i.e. one transfer always outstanding, like real pads.
                self._ep_write(self.ep1_id, last_frame)
            except OSError as exc:
                if self._writer_error(exc):
                    return
                time.sleep(0.02)
                continue
            self.polls += 1
            self.last_poll_time = time.time()

    def _writer_error(self, exc):
        self.errors += 1
        self.log(f"ep1 write failed: {exc}")
        if exc.errno in (errno.EBADF, errno.ENODEV, errno.ESHUTDOWN):
            self._on_disconnect()
            return True
        return False

    def _rumble_loop(self):
        """Read rumble/LED commands arriving on the OUT endpoint."""
        while not self._stop.is_set():
            if self.fd is None or not self._configured:
                time.sleep(0.05)
                continue
            try:
                arg = bytearray(struct.pack("<HHI", self.ep2_id, 0, 32)
                                + bytes(32))
                fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP_READ, arg,
                            mutate_flag=True)
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.ENODEV, errno.ESHUTDOWN):
                    self._on_disconnect()
                    return
                time.sleep(0.02)
                continue
            got = struct.unpack_from("<I", arg, 4)[0]
            data = bytes(arg[_USB_RAW_EP_IO_HDR:_USB_RAW_EP_IO_HDR + got])
            rumble = parse_rumble(data)
            if rumble is not None:
                big, small = rumble
                if big or small:
                    self.log(f"Rumble command: big={big} small={small}")
                if self.rumble_callback is not None:
                    try:
                        self.rumble_callback(big, small)
                    except Exception as exc:
                        self.log(f"rumble callback failed: {exc}")
                continue
            led = parse_led(data)
            if led is not None:
                self.last_led_pattern = led
                self.log(f"LED command: pattern={led}")
