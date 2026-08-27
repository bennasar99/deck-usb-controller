"""FunctionFS implementation of an Xbox 360 (XInput) controller.

The Xbox 360 wired controller is not a HID device, so the kernel's ``f_hid``
gadget function cannot emulate it. Instead this module uses the standard
in-kernel **FunctionFS** gadget function (``usb_f_fs`` / ``f_fs``), which lets
user space implement *arbitrary* USB interfaces -- here the exact vendor
specific layout of a wired Xbox 360 pad:

* Device identity: VID ``0x045E`` PID ``0x028E`` ("Microsoft X-Box 360 pad"),
  configured through configfs by :mod:`backend.gadget_manager`.
* Interface 0 (class ``0xFF/0x5D/0x01``): EP1 IN interrupt (input reports),
  EP2 OUT interrupt (rumble/LED commands).
* Interface 1 (class ``0xFF/0x5D/0x02``): EP3 IN / EP4 OUT interrupt
  (expansion/chatpad; opened and drained but unused).

No kernel add-ons or modifications are required: ``f_fs`` ships with every
standard Linux kernel and SteamOS loads it on demand via ``modprobe``.

Wire format details live in :mod:`backend.xinput_report`.

Usage::

    ffs = XInputFFS(logger=log)
    ffs.upload()          # after the configfs function dir exists and the
                          # functionfs instance is mounted at FFS_DIR
    # ... gadget_manager binds the UDC ...
    ffs.wait_enabled(timeout=10)
    ffs.send_report(frame)   # 20-byte XInput input report

The ep0 control loop runs in a background thread and answers the setup
requests Windows sends during enumeration (the class-specific HID descriptor
fetches); unknown requests are completed with zeros so enumeration never
hangs. Rumble commands arriving on the OUT endpoint are forwarded to an
optional callback.
"""

import errno
import os
import queue
import struct
import threading
import time

from backend.xinput_report import (
    HID_REPORT_DESCRIPTOR, INPUT_REPORT_LENGTH,
    parse_led, parse_rumble,
)

# Mount point of the functionfs instance (created/mounted by GadgetManager).
FFS_DIR = "/dev/ffs-xinput"

# linux/usb/functionfs.h constants.
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_HAS_FS_CONF = 1 << 0
FUNCTIONFS_HAS_HS_CONF = 1 << 1
FUNCTIONFS_HAS_SS_CONF = 1 << 2
FUNCTIONFS_HAS_MS_OS_DESC = 1 << 3

# usb_functionfs_event: struct usb_ctrlrequest (8 bytes) + enum (4 bytes).
EVENT_SIZE = 12

# Event types (enum usb_functionfs_event_type).
EVENT_BIND = 0
EVENT_UNBIND = 1
EVENT_ENABLE = 2
EVENT_DISABLE = 3
EVENT_SETUP = 4
EVENT_SUSPEND = 5
EVENT_RESUME = 6

_EVENT_NAMES = {
    EVENT_BIND: "BIND", EVENT_UNBIND: "UNBIND",
    EVENT_ENABLE: "ENABLE", EVENT_DISABLE: "DISABLE",
    EVENT_SETUP: "SETUP", EVENT_SUSPEND: "SUSPEND", EVENT_RESUME: "RESUME",
}

# Interrupt endpoint max packet size used by the real controller.
EP_MAX_PACKET = 32


def _ep_descriptor(address, interval):
    """Build a 7-byte interrupt endpoint descriptor."""
    return bytes([0x07, 0x05, address, 0x03,
                  EP_MAX_PACKET & 0xFF, EP_MAX_PACKET >> 8, interval])


# Raw interface+endpoint descriptors uploaded to FunctionFS. Single
# vendor-specific interface (FF/5D/01) with the exact endpoint geometry used
# by PiKVM's working XInput-over-FunctionFS implementation: IN interval 4,
# OUT interval 8, wMaxPacketSize 32. Descriptor order defines the epN file
# names: first endpoint descriptor -> "ep1", etc.
INTERFACE_0_DESCRIPTOR = bytes(
    [0x09, 0x04, 0x00, 0x00, 0x02, 0xFF, 0x5D, 0x01, 0x01])

# Reference: the out-of-spec 17-byte class descriptor real wired Xbox 360
# pads expose after their interface descriptor. It is NOT uploaded: the
# kernel's FunctionFS parser whitelists class-specific descriptor types
# (per functionfs-desc.html only DFU et al. are accepted) and rejects the
# vendor type 0x21 with EINVAL -- this matches CasperVM's independent
# finding that ffs cannot carry this vendor blob. PiKVM's working
# implementation likewise ships only iface+endpoints.
INTERFACE_0_CLASS_DESCRIPTOR = bytes([
    0x11, 0x21, 0x00, 0x01, 0x01, 0x25, 0x81, 0x14,
    0x00, 0x00, 0x00, 0x00, 0x13, 0x01, 0x08, 0x00,
    0x00,
])


def _raw_descriptor_items():
    """Descriptor list for one speed (PiKVM order): interface + endpoints."""
    return (INTERFACE_0_DESCRIPTOR,
            _ep_descriptor(0x81, 4),   # ep1: IN  - input reports
            _ep_descriptor(0x01, 8))   # ep2: OUT - rumble / LED commands


RAW_DESCRIPTORS = _raw_descriptor_items()

# Super-speed variants: same interface/endpoints plus an endpoint-companion
# descriptor after each endpoint (as python-functionfs generates).
_SS_COMPANION = bytes([0x06, 0x30, 0x00, 0x00, 0x20, 0x00])

RAW_DESCRIPTORS_SS = (
    INTERFACE_0_DESCRIPTOR,
    _ep_descriptor(0x81, 4) + _SS_COMPANION,
    _ep_descriptor(0x01, 4) + _SS_COMPANION,
)

# Strings for language 0x0409; index 1 (= iInterface) is the product name.
STRINGS = ("Controller",)


def _ms_os_active():
    """False: this kernel's FunctionFS parser rejects embedded MS OS
    descriptors AND vendor class descriptors, so neither is offered."""
    return False


def activate_fallback():
    """Kept for API compatibility; nothing to disable anymore."""


def build_descriptors_blob(force_plain=False):
    """Build the FUNCTIONFS_DESCRIPTORS_MAGIC_V2 blob (PiKVM-style).

    Primary variant: FS+HS+SS descriptor sets, mirroring python-functionfs'
    getInterfaceInAllSpeeds() -- SS endpoints carry companion descriptors.
    ``force_plain`` yields the FS+HS-only variant known to work on older
    kernels; upload() tries them in order until one is accepted.
    """
    raw_fs = b"".join(RAW_DESCRIPTORS)
    flags = FUNCTIONFS_HAS_FS_CONF | FUNCTIONFS_HAS_HS_CONF
    if force_plain:
        header = struct.pack(
            "<IIIII",
            FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
            20 + 2 * len(raw_fs),
            flags,
            len(RAW_DESCRIPTORS),                # fs_count
            len(RAW_DESCRIPTORS),                # hs_count
        )
        return header + raw_fs + raw_fs
    raw_ss = b"".join(RAW_DESCRIPTORS_SS)
    header = struct.pack(
        "<IIIIII",
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        24 + 2 * len(raw_fs) + len(raw_ss),
        flags | FUNCTIONFS_HAS_SS_CONF,
        len(RAW_DESCRIPTORS),                    # fs_count
        len(RAW_DESCRIPTORS),                    # hs_count
        len(RAW_DESCRIPTORS_SS),                 # ss_count
    )
    return header + raw_fs + raw_fs + raw_ss


DESCRIPTOR_BLOB_VARIANTS = (
    # (name, force_plain)
    ("fs-hs-ss", False),
    ("fs-hs", True),
)


def build_ms_os_descriptor(compatible_id=b"XUSB10"):
    """Microsoft OS 2.0 Extended Compatibility descriptor for interface 0.

    Layout confirmed against f_fs.c: an 11-byte *packed*
    ``usb_os_desc_header`` (interface=0, dwLength, bcdVersion=0x0100,
    wIndex=4, wCount=1) followed by one 24-byte ``usb_ext_compat_desc``
    record.
    """
    compatible_id = compatible_id[:8].ljust(8, b"\0")
    record = bytes([0x00,   # bFirstInterfaceNumber (interface 0)
                    0x01])  # Reserved1 == 1 per spec/kernel check
    record += compatible_id + bytes(8) + bytes(6)
    assert len(record) == 24
    # "<" disables padding: the kernel struct is __attribute__((packed)).
    header = struct.pack("<BIHHH",
                         0,                     # interface
                         11 + len(record),      # dwLength
                         0x0100,                # bcdVersion
                         4,                     # wIndex = EXT_COMPAT
                         1)                     # wCount (one feature desc)
    return header + record


def build_strings_blob(strings=STRINGS, lang=0x0409):
    """Build the FUNCTIONFS_STRINGS_MAGIC blob (single language).

    Per linux/usb/functionfs.h the kernel expects:

        LE32 magic | LE32 length | LE32 str_count | LE32 lang_count
        then per language: LE16 lang code followed by ``str_count``
        NUL-terminated UTF-8 strings.
    """
    body = struct.pack("<H", lang)
    for text in strings:
        body += text.encode("utf-8") + b"\x00"
    header = struct.pack("<IIII",
                         FUNCTIONFS_STRINGS_MAGIC,
                         16 + len(body),
                         len(strings), 1)
    return header + body


class XInputFFSError(Exception):
    """Raised when the FunctionFS controller cannot be set up."""

    def __init__(self, message, errno_=None):
        super().__init__(message)
        #errno of the underlying OS error, when known.
        self.errno_ = errno_


class XInputFFS:
    """User-space Xbox 360 controller running on a mounted functionfs."""

    def __init__(self, logger=None, rumble_callback=None, directory=FFS_DIR):
        self.log = logger or (lambda msg: None)
        self.rumble_callback = rumble_callback
        self.directory = directory
        self.ep0_fd = None
        self.enabled = False
        self._enabled_event = threading.Event()
        self._stop = threading.Event()
        self._threads = []
        self._fds = {}          # endpoint name -> fd
        self._lock = threading.Lock()
        self.polls = 0
        self.errors = 0
        self.last_led_pattern = 0
        self.last_poll_time = 0.0
        self._writer_queue = queue.Queue(maxsize=1)
        self._last_noep_log = 0.0
        # FunctionFS write() blocks until the host consumes the transfer --
        # O_NONBLOCK does NOT prevent that wait (only acquiring the endpoint
        # mutex). All ep1 writes therefore happen on this dedicated writer
        # thread so a silent host can never stall the forwarder.
        self._writer_thread = None

    # -- lifecycle -----------------------------------------------------------

    def upload(self):
        """Open ep0 and upload descriptors/strings.

        Must be called after the configfs ``ffs.*`` function exists and the
        matching functionfs instance is mounted at ``self.directory``, but
        *before* the gadget is bound to the UDC.
        """
        if self.ep0_fd is not None:
            return
        path = os.path.join(self.directory, "ep0")
        try:
            self.ep0_fd = os.open(path, os.O_RDWR)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise XInputFFSError(
                    f"{path}: busy (EBUSY) -- the mounted functionfs "
                    "instance is stale/dying from a previous session; the "
                    "gadget manager will remount it.", exc.errno) from exc
            raise XInputFFSError(
                f"Cannot open {path}: {exc}. Is the functionfs instance "
                "mounted?", exc.errno) from exc
        last_error = None
        for name, force_plain in DESCRIPTOR_BLOB_VARIANTS:
            try:
                blob = build_descriptors_blob(force_plain=force_plain)
                self.log(f"Uploading FunctionFS descriptors "
                         f"({len(blob)} bytes, variant={name})")
                os.write(self.ep0_fd, blob)
                os.write(self.ep0_fd, build_strings_blob())
            except OSError as exc:
                if exc.errno == errno.EBADF or exc.errno == errno.ENOSYS:
                    # ep0 dead: no point trying further variants.
                    self.close()
                    raise XInputFFSError(
                        f"Failed to upload FunctionFS descriptors: {exc}",
                        exc.errno) from exc
                self.log(f"Kernel rejected the '{name}' descriptor blob "
                         f"({exc}); trying next variant...")
                last_error = exc
                continue
            self.log("Descriptors and strings accepted by the kernel "
                     f"(variant={name}).")
            break
        else:
            hint = "no supported descriptor blob variant was accepted"
            if last_error is not None and last_error.errno == errno.EINVAL:
                hint += " (EINVAL: stale ffs instance or unsupported payload)"
            self.close()
            raise XInputFFSError(
                f"Failed to upload FunctionFS descriptors -- {hint}: "
                f"{last_error}", getattr(last_error, "errno", None))
        thread = threading.Thread(target=self._ep0_loop, daemon=True,
                                  name="xinput-ffs-ep0")
        self._threads.append(thread)
        thread.start()

    def wait_enabled(self, timeout=10.0):
        """Wait until the host has configured the device (endpoints usable)."""
        return self._enabled_event.wait(timeout)

    def stop(self):
        """Stop all threads and close every endpoint fd."""
        self._stop.set()
        # Unblock a writer parked in queue.get; closing fds below also makes
        # any in-flight blocking os.write fail and unwind.
        try:
            self._writer_queue.put_nowait(b"")
        except Exception:
            pass
        self._close_fds()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        self._writer_thread = None
        self.enabled = False
        self._enabled_event.clear()

    def close(self):
        self.stop()

    # -- data path -----------------------------------------------------------

    def send_report(self, data):
        """Hand one input report to the writer thread (never blocks).

        Returns 0 when queued for delivery, ``None`` when the endpoints are
        not open / the writer is not running. Only the newest frame is kept:
        an unconsumed one is replaced, mirroring how hosts sample pad state.
        """
        with self._lock:
            fd = self._fds.get("ep1")
        if fd is None or self._writer_thread is None \
                or not self._writer_thread.is_alive():
            now = time.time()
            if now - self._last_noep_log >= 5.0:
                self._last_noep_log = now
                self.log(f"send_report dropped frame: ep1 not open "
                         f"(enabled={self.enabled})")
            return None
        try:
            self._writer_queue.put_nowait(bytes(data))
            return 0
        except queue.Full:
            # Replace the stale frame with this fresher one.
            try:
                self._writer_queue.get_nowait()
                self._writer_queue.put_nowait(bytes(data))
                return 0
            except (queue.Empty, queue.Full):
                return 0
        except OSError as exc:
            self.errors += 1
            return exc.errno

    # -- ep1 writer thread ---------------------------------------------------

    def _writer_loop(self, fd):
        """Sole owner of ep1 writes; keeps ONE transfer perpetually queued.

        Mirrors python-functionfs' model (submit shared buffer on enable,
        resubmit on completion): the host samples the interrupt endpoint at
        its own pace and always finds our latest state available.
        write() returning EAGAIN simply means the previous transfer is still
        pending consumption -- the steady state while the host idles.
        """
        last_frame = bytes(INPUT_REPORT_LENGTH)
        last_err_log = 0.0
        while not self._stop.is_set():
            try:
                frame = self._writer_queue.get(timeout=0.05)
                if len(frame) != INPUT_REPORT_LENGTH:
                    frame = bytes(frame).ljust(INPUT_REPORT_LENGTH, b"\0")
                last_frame = frame
            except queue.Empty:
                pass
            try:
                os.write(fd, last_frame)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    # Transfer queued; host has not consumed it yet. Normal.
                    time.sleep(0.01)
                    continue
                self.errors += 1
                if exc.errno in (errno.EBADF, errno.ENODEV):
                    self._close_fds()
                    self.enabled = False
                    self._enabled_event.clear()
                    self.log(f"ep1 closed ({exc}); waiting for re-enable.")
                    return
                if time.time() - last_err_log >= 5.0:
                    last_err_log = time.time()
                    self.log(f"ep1 write failed: {exc}")
                time.sleep(0.02)
                continue
            if self.polls == 0:
                self.log("First report consumed by the host (XInput data "
                         "path confirmed).")
            self.polls += 1
            self.last_poll_time = time.time()

    # -- ep0 control loop ----------------------------------------------------

    def _ep0_loop(self):
        while not self._stop.is_set():
            try:
                raw = os.read(self.ep0_fd, EVENT_SIZE)
            except OSError:
                break
            if len(raw) < EVENT_SIZE:
                break
            # struct usb_functionfs_event: 8-byte setup + __u8 type + pad[3].
            event_type = raw[8]
            name = _EVENT_NAMES.get(event_type, str(event_type))
            if event_type == EVENT_ENABLE:
                self.log("FunctionFS ENABLE: endpoints available.")
                self._on_enable()
            elif event_type == EVENT_DISABLE:
                self.log("FunctionFS DISABLE: host released the configuration.")
                self._on_disable()
            elif event_type == EVENT_SETUP:
                self._handle_setup(raw[:8])
            elif event_type in (EVENT_BIND, EVENT_UNBIND):
                self.log(f"FunctionFS {name}")
            elif event_type == EVENT_SUSPEND:
                self.log("FunctionFS SUSPEND")
            elif event_type == EVENT_RESUME:
                self.log("FunctionFS RESUME")

    def _handle_setup(self, request):
        """Answer one SETUP packet from the host.

        Standard requests (GET_DESCRIPTOR etc.) are served by the kernel's
        composite layer from the configfs attributes; only class/vendor
        specific requests reach user space. The ones the XInput stack uses:

        * ``0xC1/0x01`` -- Get LED status: real pads return 4 bytes whose
          second byte is the current LED pattern.
        * ``0xC1/0x06`` -- class-specific ``GET_DESCRIPTOR(HID Report)`` aimed
          at interface 0; answered so enumeration stays clean even though the
          pad is not a HID-class device.
        """
        bm_request_type = request[0]
        b_request = request[1]
        w_value, w_index, w_length = struct.unpack_from("<HHH", request, 2)
        direction_in = bool(bm_request_type & 0x80)
        # Class-specific requests reach us either as 0xC1 (vendor form) or
        # 0x81 (class recipient); accept both, real captures contain both.
        class_get = bm_request_type in (0xC1, 0x81)

        if (class_get and b_request == 0x06
                and (w_value >> 8) == 0x22):
            payload = HID_REPORT_DESCRIPTOR[:w_length] or None
            self.log(f"ep0: GET_DESCRIPTOR(HID Report) type=0x{bm_request_type:02x} "
                     f"wLength={w_length}")
        elif (class_get and b_request == 0x06
                and (w_value >> 8) == 0x21):
            # HID class descriptor: 9 bytes pointing at the report descriptor.
            hid_desc = bytes([0x09, 0x21, 0x11, 0x01, 0x00, 0x01,
                              0x22, len(HID_REPORT_DESCRIPTOR) & 0xFF,
                              len(HID_REPORT_DESCRIPTOR) >> 8])
            payload = hid_desc[:w_length] or None
            self.log(f"ep0: GET_DESCRIPTOR(HID Class) type=0x{bm_request_type:02x}")
        elif (class_get and b_request == 0x01):
            # Get LED status: [echo][pattern][00][00]; the pattern echoes the
            # last LED command received on the OUT endpoint.
            payload = bytes([0x04, self.last_led_pattern, 0x00, 0x00])[:w_length] \
                or None
            self.log(f"ep0: GET_LED_STATUS -> pattern={self.last_led_pattern}")
        elif direction_in:
            # Unknown data-in request: complete it with zeros so the host is
            # never left waiting.
            payload = bytes(w_length)
            self.log(f"ep0: unhandled setup "
                     f"(type=0x{bm_request_type:02x} req=0x{b_request:02x} "
                     f"value=0x{w_value:04x}); answering zeros")
        else:
            # Data-out stage: drain and discard whatever the host sends.
            payload = None
            remaining = w_length
            while remaining > 0:
                try:
                    chunk = os.read(self.ep0_fd, remaining)
                except OSError:
                    break
                if not chunk:
                    break
                remaining -= len(chunk)
            self.log(f"ep0: unhandled data-out setup "
                     f"(type=0x{bm_request_type:02x} req=0x{b_request:02x}); "
                     "discarded")
            return

        if payload:
            try:
                os.write(self.ep0_fd, payload)
            except OSError as exc:
                self.log(f"ep0: reply failed: {exc}")

    def _on_enable(self):
        with self._lock:
            if self._fds:
                return
            try:
                self._fds["ep1"] = os.open(
                    os.path.join(self.directory, "ep1"),
                    os.O_WRONLY | os.O_NONBLOCK)
                self._fds["ep2"] = os.open(
                    os.path.join(self.directory, "ep2"), os.O_RDONLY)
            except OSError as exc:
                self.log(f"Failed to open FunctionFS endpoints: {exc}")
                self._close_fds_locked()
                return
        self.enabled = True
        self._enabled_event.set()
        self.log("Endpoints ep1/ep2 opened; starting writer thread...")
        writer = threading.Thread(target=self._writer_loop,
                                  args=(self._fds["ep1"],),
                                  daemon=True, name="xinput-ffs-writer")
        self._writer_thread = writer
        self._threads.append(writer)
        writer.start()
        # Positive control for the data path: queue one neutral report. The
        # writer logs 'First report consumed by the host' when it lands.
        self.send_report(bytes(INPUT_REPORT_LENGTH))
        thread = threading.Thread(target=self._out_loop_ep2,
                                  args=(self._fds["ep2"],),
                                  daemon=True, name="xinput-ffs-ep2")
        self._threads.append(thread)
        thread.start()

    def _on_disable(self):
        self._close_fds()
        self.enabled = False
        self._enabled_event.clear()

    def _out_loop_ep2(self, fd):
        self._out_loop(fd, "ep2")

    def _out_loop(self, fd, name):
        """Read output reports (rumble/LED) coming from the host."""
        while not self._stop.is_set():
            try:
                data = os.read(fd, EP_MAX_PACKET)
            except OSError:
                break
            if not data:
                continue
            rumble = parse_rumble(data)
            if rumble is not None:
                big, small = rumble
                if big or small:
                    self.log(f"Rumble command: big={big} small={small}")
                if self.rumble_callback is not None:
                    try:
                        self.rumble_callback(big, small)
                    except Exception as exc:  # pragma: no cover
                        self.log(f"rumble callback failed: {exc}")
                continue
            led = parse_led(data)
            if led is not None:
                self.last_led_pattern = led
                self.log(f"LED command: pattern={led}")

    # -- fd helpers ----------------------------------------------------------

    def _close_fds(self):
        with self._lock:
            self._close_fds_locked()

    def _close_fds_locked(self):
        for name, fd in list(self._fds.items()):
            try:
                os.close(fd)
            except OSError:
                pass
            del self._fds[name]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()