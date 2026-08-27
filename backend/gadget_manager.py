"""USB gadget management for the Steam Deck using the kernel ``f_fs`` function.

The Steam Deck's USB-C port is a dual-role (DRD) port driven by an AMD DWC3
controller. To act as a USB *device* we have to:

1. Re-bind the DWC3 PCI device from the ``xhci_hcd`` (host) driver to the
   ``dwc3-pci`` (gadget) driver so a UDC becomes available.
2. Build a configfs USB gadget that exposes a single **FunctionFS** function
   (``usb_f_fs``). FunctionFS lets user space implement arbitrary USB
   interfaces; this app uses it to present the exact vendor-specific layout of
   a wired **Xbox 360 controller** (class ``0xFF/0x5D``), so Windows and other
   hosts load their native XInput driver -- no HID translation layer, no
   XInput wrappers, no ``raw_gadget`` kernel module.

The device identity (VID ``0x045E``, PID ``0x028E``) comes from configfs; the
interface/endpoint descriptors and the wire protocol are implemented in user
space by :mod:`backend.xinput_ffs`.

All sysfs/configfs writes are attempted directly first and fall back to
``sudo`` (the ``deck`` user has passwordless sudo on SteamOS).
"""

import errno
import glob
import os
import shlex
import subprocess
import time

from backend import xinput_ffs
from backend.xinput_ffs import FFS_DIR, XInputFFS, XInputFFSError

# Configfs mount point and gadget path.
CONFIGFS_PATH = "/sys/kernel/config"
GADGET_NAME = "deck-usb-gamepad"
GADGET_PATH = os.path.join(CONFIGFS_PATH, "usb_gadget", GADGET_NAME)
FFS_FUNCTION_NAME = "ffs.xinput"
HID_FUNCTION_NAME = "hid.usb0"
HID_DEV = "/dev/hidg0"
REPORT_LENGTH_HID = 13

# Wired Xbox 360 controller identity: this is what makes hosts load their
# native XInput driver.
VENDOR_ID = "0x045e"
PRODUCT_ID = "0x028e"
MANUFACTURER = "©Microsoft Corporation"
PRODUCT_NAME = "Controller"
SERIAL_NUMBER = "05A4FF4"
CONFIGURATION_NAME = "Controller"
DEVICE_CLASS = "0xff"       # vendor specific
DEVICE_SUBCLASS = "0xff"
DEVICE_PROTOCOL = "0xff"

# Generic HID-gamepad identity, byte-for-byte the PROVEN-WORKING one from
# deck-usb-hid-controller (0079:0006 = classic generic joystick that every
# OS maps natively). Do NOT use Valve 28DE:* ids here -- they collide with
# real Steam hardware identities and Windows driver caches.
HID_VENDOR_ID = "0x0079"
HID_PRODUCT_ID = "0x0006"
HID_MANUFACTURER = "Valve"
HID_PRODUCT_NAME = "Steam Deck Gamepad"
HID_SERIAL_NUMBER = "DECK-GAMEPAD-0001"
HID_CONFIGURATION_NAME = "Gamepad"

# AMD Van Gogh DWC3 USB controller PCI device id.
AMD_DWC3_PCI_ID = "0x15d0"
# Fallback PCI addresses used by the Steam Deck LCD.
KNOWN_DWC3_ADDRESSES = ("0000:04:00.3", "0000:04:00.4")


class GadgetError(Exception):
    """Raised when a USB gadget operation fails."""


def _functionfs_mounted(directory):
    """True when *directory* has a functionfs instance mounted on it."""
    try:
        with open("/proc/mounts", "r") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == directory \
                        and parts[2] == "functionfs":
                    return True
    except OSError:
        pass
    return False


class GadgetManager:
    """Creates and destroys the configfs ``f_fs`` XInput gadget on the Deck."""

    def __init__(self, logger=None, sudo=True):
        self.log = logger
        self._sudo = sudo
        self.ffs = None
        self.path = None
        self.mode = "xinput"          # active gadget flavor
        self.mode_backend = None      # 'raw' or 'ffs' within xinput mode
        self.raw = None
        self.hid_fd = None
        self._hid_polls = 0
        self._hid_errors = 0

    # -- public API ---------------------------------------------------------

    def start(self, mode="xinput"):
        """Bring up the gadget in *mode*: 'xinput' or 'hid'.

        XInput uses the kernel ``raw_gadget`` interface when available
        (byte-exact Xbox 360 emulation, including the vendor descriptors
        Windows' driver stack requires); it falls back to FunctionFS
        otherwise. 'hid' builds a standard HID gamepad via configfs+f_hid.
        """
        if mode not in ("xinput", "hid"):
            raise GadgetError(f"Unknown gadget mode: {mode}")
        self.mode = mode
        if mode == "xinput":
            # raw_gadget on this neptune kernel is KNOWN BROKEN for the
            # physical UDC: RUN fails EBUSY (state "not attached"), its
            # leaked registration even blocks the next configfs bind
            # (composite EBUSY) until the fd closes. Opt-in for testing via
            # the marker file; normal boots go straight to FunctionFS.
            if os.path.exists("/opt/usb-gamepad/try-raw"):
                # Load libcomposite first: a freshly mounted configfs has no
                # usb_gadget/ subtree until this module creates it.
                self._load_module_quiet("libcomposite")
                self._ensure_configfs_mounted()
                if not os.path.isdir(
                        os.path.join(CONFIGFS_PATH, "usb_gadget")):
                    raise GadgetError(
                        "configfs has no usb_gadget/ subtree even after "
                        "loading libcomposite; gadget support missing.")
                self._cleanup_stale_configfs()
                try:
                    return self._start_raw()
                except Exception as exc:
                    self._log(f"raw_gadget backend failed ({exc}); "
                              "falling back to FunctionFS.")
        # Non-raw paths need configfs.
        self._ensure_configfs_mounted()
        self._ensure_udc()
        self._ensure_function_modules()
        for attempt in (1, 2):
            try:
                self._cleanup_stale_configfs()
                if mode == "xinput":
                    self._build_configfs_gadget()
                    self.path = FFS_DIR
                else:
                    self._build_hid_gadget()
                    self.path = HID_DEV
                break
            except GadgetError as exc:
                if attempt == 1 and getattr(exc, "stale_ffs", False):
                    self._log(
                        "Recovering from a stale FunctionFS state "
                        f"({exc}); resetting and retrying once...")
                    self._force_reset()
                    continue
                raise
        return self.path

    def _raw_gadget_holders(self):
        """List PID/name of every process with /dev/raw-gadget open.

        raw_gadget allows only one registration context; a leftover holder
        (e.g. an earlier crashed daemon) makes every RUN fail EBUSY no
        matter that the UDC itself looks free.
        """
        holders = []
        for proc in os.listdir("/proc"):
            if not proc.isdigit():
                continue
            fd_dir = f"/proc/{proc}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target == "/dev/raw-gadget":
                    try:
                        with open(f"/proc/{proc}/comm") as handle:
                            name = handle.read().strip()
                        cmd = open(f"/proc/{proc}/cmdline", "r").read() \
                            .replace("\0", " ").strip()
                    except OSError:
                        name, cmd = "?", "?"
                    holders.append(f"pid {proc} ({name}: {cmd})")
                    break
        return holders

    def _load_module_quiet(self, module):
        try:
            proc = subprocess.run(["modprobe", module],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                self._log(f"Loaded kernel module {module}")
        except FileNotFoundError:
            pass

    def _start_raw(self):
        from backend.xinput_raw import RawGadgetXInput
        udc = self._detect_udc()
        if not udc:
            raise GadgetError("No UDC available for raw_gadget.")
        # raw_gadget attaches exclusively: any configfs gadget still bound
        # to the UDC makes USB_RAW_IOCTL_RUN fail with EBUSY. Free it first.
        # IMPORTANT: load libcomposite BEFORE anything else here -- a freshly
        # mounted configfs has no usb_gadget/ subtree until this module
        # creates it; cleanup+prime otherwise fail with EPERM on the parent.
        self._load_module_quiet("libcomposite")
        self._ensure_configfs_mounted()
        if not os.path.isdir(os.path.join(CONFIGFS_PATH, "usb_gadget")):
            raise GadgetError(
                "configfs has no usb_gadget/ subtree even after loading "
                "libcomposite; gadget support missing from this kernel.")
        self._cleanup_stale_configfs()
        try:
            state = open(f"/sys/class/udc/{udc}/state").read().strip()
        except OSError:
            state = "<unreadable>"
        gadgets = []
        gadget_listing_error = None
        try:
            gadgets = os.listdir(os.path.join(CONFIGFS_PATH, "usb_gadget"))
        except OSError as exc:
            gadget_listing_error = str(exc)
        self._log(f"raw_gadget: attaching to {udc} "
                  f"(state={state}, existing gadgets="
                  f"{gadgets or 'none'})"
                  + (f" [list failed: {gadget_listing_error}]"
                     if gadget_listing_error else ""))
        holders = self._raw_gadget_holders()
        if holders:
            self._log(f"raw_gadget: DEVICE ALREADY HELD by {holders} "
                      "(another process must release it or RUN gets EBUSY)")
        # "Prime" the UDC with a full configfs bind/unbind cycle before the
        # raw attach. dwc3's peripheral half can sit in a half-started state
        # (previous killed daemon, lazy teardown) where raw_gadget's driver
        # registration gets EBUSY even though the UDC reads free; a real
        # bind -> unbind via the composite driver forces dwc3 through
        # udc_start/udc_stop and clears that state. Skippable for bisecting.
        if not os.path.exists("/opt/usb-gamepad/no-raw-prime"):
            try:
                self._prime_udc(udc)
            except Exception as exc:
                self._log(f"UDC prime cycle failed (non-fatal): {exc}")
        if self.raw is None:
            self.raw = RawGadgetXInput(logger=self._log, udc=udc)
        else:
            self.raw.udc = udc
        self.raw.start()
        self.mode_backend = "raw"
        self.path = "/dev/raw-gadget"
        configured = self.raw.wait_configured(timeout=6.0)
        if configured:
            self._log("Host configured the Xbox 360 controller "
                      "(raw_gadget, native XInput).")
        else:
            self._log("Host has not configured the controller yet; "
                      "reports will flow as soon as it does.")
        return self.path

    def _prime_udc(self, udc):
        """Bind a trivial configfs gadget to *udc*, then unbind it.

        A function-less configuration is enough: the point is running the
        UDC through a complete composite-driver start/stop cycle, not
        presenting anything to the host. Runs via shell (with sudo fallback)
        because it must work even while another process interferes with
        configfs -- direct syscalls raced once and silently failed.
        """
        base = GADGET_PATH
        self._log(f"Priming UDC {udc} via a configfs bind/unbind cycle...")
        last_exc = None
        for _ in range(2):
            try:
                self._shell(f"mkdir -p {shlex.quote(base)}")
                self._shell(f"echo 0x1d6b > {shlex.quote(base + '/idVendor')}")
                self._shell(f"echo 0x0104 > {shlex.quote(base + '/idProduct')}")
                self._shell(f"echo 0x0200 > {shlex.quote(base + '/bcdUSB')}")
                self._shell(f"mkdir -p {shlex.quote(base + '/configs/c.1')}")
                # No function: an empty configuration binds fine.
                self._shell(f"echo {shlex.quote(udc)} > "
                            f"{shlex.quote(base + '/UDC')}")
                time.sleep(0.6)
                self._shell(f"echo > {shlex.quote(base + '/UDC')}")
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    try:
                        if self._read(f"{base}/UDC").strip() == "":
                            break
                    except OSError:
                        break
                    time.sleep(0.1)
                self._purge_tree(base)
                self._log("UDC prime cycle complete.")
                return
            except Exception as exc:
                last_exc = exc
                self._log(f"prime attempt failed ({exc}); retrying...")
                self._cleanup_stale_configfs()
                time.sleep(0.5)
        raise GadgetError(f"UDC prime cycle failed: {last_exc}")

    def _build_hid_gadget(self):
        """Standard HID gamepad via the kernel's f_hid function.

        Configfs sequence matches the proven deck-usb-hid-controller
        implementation exactly: generic identity, no device-class attrs
        (per-interface classing), protocol/subclass 0, then report_desc.
        """
        base = GADGET_PATH
        from backend.gamepad_report import build_report_descriptor, REPORT_LENGTH
        self._log(f"Creating configfs HID gadget at {base}")
        self._mkdir(base)
        self._write(f"{base}/idVendor", HID_VENDOR_ID + "\n")
        self._write(f"{base}/idProduct", HID_PRODUCT_ID + "\n")
        self._write(f"{base}/bcdDevice", "0x0100\n")
        self._write(f"{base}/bcdUSB", "0x0200\n")

        self._mkdir(f"{base}/strings/0x409")
        self._write(f"{base}/strings/0x409/serialnumber",
                    HID_SERIAL_NUMBER + "\n")
        self._write(f"{base}/strings/0x409/manufacturer",
                    HID_MANUFACTURER + "\n")
        self._write(f"{base}/strings/0x409/product", HID_PRODUCT_NAME + "\n")

        self._mkdir(f"{base}/configs/c.1")
        self._mkdir(f"{base}/configs/c.1/strings/0x409")
        self._write(f"{base}/configs/c.1/strings/0x409/configuration",
                    HID_CONFIGURATION_NAME + "\n")

        descriptor = build_report_descriptor()
        self._mkdir(f"{base}/functions/{HID_FUNCTION_NAME}")
        self._write(f"{base}/functions/{HID_FUNCTION_NAME}/protocol", "0\n")
        self._write(f"{base}/functions/{HID_FUNCTION_NAME}/subclass", "0\n")
        self._write(f"{base}/functions/{HID_FUNCTION_NAME}/report_length",
                    str(REPORT_LENGTH) + "\n")
        self._write_binary(
            f"{base}/functions/{HID_FUNCTION_NAME}/report_desc", descriptor)

        self._link_function_into_config(HID_FUNCTION_NAME)

        udc = self._detect_udc()
        if udc is None:
            raise GadgetError("No UDC available; cannot bind gadget.")
        self._log(f"Binding HID gadget to UDC {udc}...")
        self._write(f"{base}/UDC", udc + "\n")
        deadline = time.time() + 5.0
        while time.time() < deadline and not os.path.exists(HID_DEV):
            time.sleep(0.2)
        if not os.path.exists(HID_DEV):
            raise GadgetError("HID gadget bound but /dev/hidg0 never appeared.")
        self.hid_fd = os.open(HID_DEV,
                              os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._log("HID gamepad ready (/dev/hidg0); host should enumerate it "
                  "as a standard USB gamepad within seconds.")

    def _link_function_into_config(self, function_name):
        """Link *function_name* into configs/c.1, removing other LINKS only.

        configs/c.1 also contains kernel attribute FILES (MaxPower,
        bmAttributes, ...). Those must never be touched -- only function
        symlinks are removed here. A config containing two function links
        binds garbage, so leaving anything else is an error.
        """
        cfg = f"{GADGET_PATH}/configs/c.1"
        desired_link = f"{cfg}/{function_name}"
        for entry in sorted(os.listdir(cfg)):
            path = f"{cfg}/{entry}"
            if entry == function_name or not os.path.islink(path):
                continue
            try:
                os.unlink(path)
                self._log(f"Removed stale config link {path}")
            except OSError as exc:
                raise GadgetError(
                    f"Stale link {path} cannot be removed ({exc}); config "
                    "would contain two functions.") from exc
        if not os.path.exists(desired_link):
            os.symlink(f"{GADGET_PATH}/functions/{function_name}",
                       desired_link)

    def stop(self):
        """Tear the gadget down (returns the port to host mode)."""
        if self.raw is not None:
            try:
                self.raw.stop()
            except Exception as exc:  # pragma: no cover - best effort
                self._log(f"raw_gadget shutdown failed: {exc}")
            self.raw = None
            self.mode_backend = None
            self.path = None
            return
        if self.hid_fd is not None:
            try:
                os.close(self.hid_fd)
            except OSError:
                pass
            self.hid_fd = None
        if self.ffs is not None:
            try:
                self.ffs.stop()
            except Exception as exc:  # pragma: no cover - best effort
                self._log(f"FunctionFS shutdown failed: {exc}")
            self.ffs = None
        # Give the kernel a beat to release endpoint fds/UDC state before
        # pulling configfs out from under it (mode-switch teardown used to
        # race this and leave half-deleted gadget trees behind).
        time.sleep(0.5)
        self.path = None
        # Order matters: unmount the functionfs instance first, otherwise
        # rmdir of functions/ffs.xinput fails with EBUSY and leaves state
        # behind that makes the next session fail.
        self._unmount_functionfs()
        try:
            self._teardown_configfs_gadget()
        except Exception as exc:  # pragma: no cover - best effort
            self._log(f"gadget teardown failed: {exc}")

    def send_report(self, data):
        """Publish one input report to the host.

        XInput+raw: 20-byte report via raw_gadget. XInput+ffs: via
        FunctionFS. HID mode: 13-byte report to /dev/hidg0. Returns 0 when
        accepted, ``None`` when not ready, or the failing ``errno``.
        """
        if self.mode_backend == "raw":
            if self.raw is None:
                return None
            return self.raw.send_report(data)
        if self.mode == "hid":
            if self.hid_fd is None:
                return None
            try:
                os.write(self.hid_fd, bytes(data[:REPORT_LENGTH_HID]))
            except OSError as exc:
                return exc.errno
            self._hid_polls += 1
            return 0
        if self.ffs is None:
            return None
        return self.ffs.send_report(data)

    @property
    def polls(self):
        if self.mode_backend == "raw":
            return self.raw.polls if self.raw else 0
        if self.mode == "hid":
            return self._hid_polls
        return self.ffs.polls if self.ffs else 0

    @property
    def errors(self):
        if self.mode_backend == "raw":
            return self.raw.errors if self.raw else 0
        if self.mode == "hid":
            return self._hid_errors
        return self.ffs.errors if self.ffs else 0

    @property
    def enabled(self):
        """True when the host has configured the device (endpoints usable)."""
        if self.mode_backend == "raw":
            return bool(self.raw and self.raw.enabled)
        if self.mode == "hid":
            return self.hid_fd is not None
        return bool(self.ffs and self.ffs.enabled)

    @property
    def last_poll_time(self):
        """Monotonic time of the last report consumed by the host, else 0."""
        return self.ffs.last_poll_time if self.ffs else 0.0

    def reset_device(self):
        """Force a USB re-enumeration (the equivalent of a cable replug)."""
        if self.mode_backend == "raw":
            self.raw.reenumerate()
            return
        try:
            udc = self._read(f"{GADGET_PATH}/UDC").strip()
        except OSError as exc:
            raise GadgetError(f"Cannot read current UDC: {exc}") from exc
        if not udc:
            raise GadgetError("Gadget is not bound to a UDC; cannot reset.")
        self._log(f"Forcing USB re-enumeration (rebinding UDC {udc})...")
        self._write(f"{GADGET_PATH}/UDC", "\n")
        time.sleep(1.0)
        self._write(f"{GADGET_PATH}/UDC", udc + "\n")
        if self.ffs is not None:
            self.ffs.wait_enabled(timeout=8.0)
        self._log("Re-enumeration complete.")

    def check_hardware(self):
        """Return a diagnostic dict describing the current hardware state."""
        udc = self._detect_udc()
        return {
            "f_fs": _f_fs_available(),
            "ffs_mount": _functionfs_mounted(FFS_DIR),
            "udc": udc,
            "dwc3_pci": self._find_dwc3_pci(),
            "configfs_mounted": os.path.ismount(CONFIGFS_PATH),
            "gadget_dir": os.path.isdir(GADGET_PATH),
        }

    # -- internal helpers ---------------------------------------------------

    def _log(self, msg):
        if self.log is not None:
            self.log(msg)

    def _ensure_configfs_mounted(self):
        if os.path.ismount(CONFIGFS_PATH):
            return
        self._log("Mounting configfs...")
        self._shell("mount -t configfs configfs " + CONFIGFS_PATH)
        if not os.path.ismount(CONFIGFS_PATH):
            raise GadgetError("Failed to mount configfs.")

    def _ensure_udc(self):
        if self._detect_udc():
            return
        self._log("No UDC present, switching DWC3 controller to gadget mode...")
        self._switch_dwc3_to_gadget()
        if not self._wait_for_udc(timeout=6):
            raise GadgetError(
                "No UDC became available. Ensure the Deck is rebooted with "
                "BIOS 'USB Dual Role Device' set to 'DRD' "
                "(Setup Utility -> Advanced -> USB Configuration)."
            )
        self._log("UDC ready.")

    def _ensure_function_modules(self):
        """Make sure the composite gadget core and FunctionFS are loaded.

        ``usb_f_fs`` provides the ``ffs.*`` function type in configfs; if it
        is not loaded, ``mkdir functions/ffs.xinput`` fails with ``ENODEV``.
        Raises :class:`GadgetError` when it cannot be made available, rather
        than failing later in a hard-to-diagnose way.
        """
        modules = ("libcomposite", "usb_f_fs")
        if self.mode == "hid":
            modules = ("libcomposite", "usb_f_hid")
        for module in modules:
            if os.path.isdir(f"/sys/module/{module}"):
                continue
            try:
                proc = subprocess.run(["modprobe", module],
                                      capture_output=True, text=True)
            except FileNotFoundError:
                proc = None
            if proc is not None and proc.returncode == 0:
                self._log(f"Loaded kernel module {module}")
            else:
                detail = (proc.stderr.strip() or "modprobe not found") \
                    if proc is not None else "modprobe not found"
                raise GadgetError(
                    f"Kernel module '{module}' is required but could not be "
                    f"loaded ({detail}). SteamOS ships it as part of the "
                    "linux-neptune kernel package; check "
                    "`pacman -Q linux-neptune`.")

    def _switch_dwc3_to_gadget(self):
        address = self._find_dwc3_pci()
        if address is None:
            raise GadgetError(
                "Could not find the AMD DWC3 USB controller. "
                "The 'dwc3-pci' kernel driver may be missing."
            )
        self._log(f"Re-binding {address} from xhci_hcd to dwc3-pci...")
        self._write(f"/sys/bus/pci/drivers/xhci_hcd/unbind", address + "\n")
        time.sleep(1)
        if not os.path.isdir(f"/sys/bus/pci/devices/{address}/driver"):
            self._write(f"/sys/bus/pci/drivers/dwc3-pci/bind", address + "\n")
        time.sleep(1)

    def _find_dwc3_pci(self):
        # Already bound to the gadget driver?
        bound = f"/sys/bus/pci/drivers/dwc3-pci"
        if os.path.isdir(bound):
            for entry in sorted(os.listdir(bound)):
                if entry.startswith("0000:") and os.path.isdir(
                        os.path.join(bound, entry)):
                    return entry
        # Currently bound to xhci_hcd?
        devices = "/sys/bus/pci/devices"
        for entry in sorted(os.listdir(devices)):
            if not entry.startswith("0000:"):
                continue
            dev_path = os.path.join(devices, entry)
            try:
                device_id = self._read(f"{dev_path}/device").strip().lower()
            except OSError:
                continue
            if device_id != AMD_DWC3_PCI_ID:
                continue
            driver_link = os.path.join(dev_path, "driver")
            if os.path.islink(driver_link):
                driver = os.path.basename(os.readlink(driver_link))
                if driver in ("xhci_hcd", "dwc3-pci"):
                    return entry
        # Fallback known addresses.
        for address in KNOWN_DWC3_ADDRESSES:
            if os.path.isdir(os.path.join(devices, address)):
                return address
        return None

    # -- functionfs mount ----------------------------------------------------

    def _mount_functionfs(self):
        """Mount the functionfs instance backing the ``ffs.xinput`` function."""
        os.makedirs(FFS_DIR, exist_ok=True)
        if _functionfs_mounted(FFS_DIR):
            return
        self._log(f"Mounting functionfs at {FFS_DIR}...")
        self._shell(
            f"mount -t functionfs xinput {shlex.quote(FFS_DIR)} "
            f"-o uid=0,gid=0")
        if not _functionfs_mounted(FFS_DIR):
            raise GadgetError(
                f"Failed to mount functionfs at {FFS_DIR}. The 'f_fs' "
                "kernel function may be missing from this kernel.")

    def _unmount_functionfs(self, force=False):
        if not _functionfs_mounted(FFS_DIR):
            return
        try:
            self._shell(f"umount {shlex.quote(FFS_DIR)}")
        except Exception:
            # Regular unmount can fail while ep fds are still closing down;
            # escalate to a lazy unmount so the next mount starts clean.
            self._log("Regular unmount failed; using a lazy unmount...")
            try:
                self._shell(f"umount -l {shlex.quote(FFS_DIR)}")
            except Exception as exc:  # pragma: no cover - best effort
                self._log(f"functionfs unmount failed: {exc}")

    # -- configfs gadget construction ---------------------------------------

    def _cleanup_stale_configfs(self):
        """Remove leftovers from a previous session.

        Both a stale configfs gadget *and* an orphaned functionfs mount (a
        mount with no backing gadget, or one whose ffs instance is dead) must
        go; the latter is what makes ep0 open fail with EBUSY forever.
        """
        if os.path.exists(GADGET_PATH):
            self._log("Cleaning up stale configfs gadget...")
            # Unmount first: removing functions/ffs.xinput while its
            # functionfs instance is mounted fails with EBUSY.
            self._unmount_functionfs()
            self._teardown_configfs_gadget()
        elif _functionfs_mounted(FFS_DIR):
            self._log("Removing orphaned functionfs mount...")
            self._unmount_functionfs()

    def _force_reset(self):
        """Nuke every piece of gadget state so a fresh start can succeed."""
        try:
            self._write(f"{GADGET_PATH}/UDC", "\n")
        except Exception:
            pass
        if self.ffs is not None:
            try:
                self.ffs.stop()
            except Exception:
                pass
            self.ffs = None
        self._unmount_functionfs(force=True)
        self._teardown_configfs_gadget()

    def _build_configfs_gadget(self):
        base = GADGET_PATH
        self._log(f"Creating configfs gadget at {base}")
        self._mkdir(base)
        self._write(f"{base}/idVendor", VENDOR_ID + "\n")
        self._write(f"{base}/idProduct", PRODUCT_ID + "\n")
        self._write(f"{base}/bcdDevice", "0x0114\n")
        self._write(f"{base}/bcdUSB", "0x0200\n")
        self._write(f"{base}/bDeviceClass", DEVICE_CLASS + "\n")
        self._write(f"{base}/bDeviceSubClass", DEVICE_SUBCLASS + "\n")
        self._write(f"{base}/bDeviceProtocol", DEVICE_PROTOCOL + "\n")

        # Strings (these become iManufacturer/iProduct/iSerialNumber).
        self._mkdir(f"{base}/strings/0x409")
        self._write(f"{base}/strings/0x409/serialnumber", SERIAL_NUMBER + "\n")
        self._write(f"{base}/strings/0x409/manufacturer", MANUFACTURER + "\n")
        self._write(f"{base}/strings/0x409/product", PRODUCT_NAME + "\n")

        # Configuration.
        self._mkdir(f"{base}/configs/c.1")
        self._mkdir(f"{base}/configs/c.1/strings/0x409")
        self._write(f"{base}/configs/c.1/strings/0x409/configuration",
                    CONFIGURATION_NAME + "\n")

        # FunctionFS function: create it in configfs, then mount its
        # functionfs instance and upload our interface descriptors.
        self._mkdir(f"{base}/functions/{FFS_FUNCTION_NAME}")
        self._mount_functionfs()

        ffs = XInputFFS(logger=self._log)
        try:
            ffs.upload()
        except XInputFFSError as exc:
            error = GadgetError(str(exc))
            # EBUSY on ep0 open / EINVAL on upload == leftover dead instance
            # from an earlier session; start() will force-reset and retry.
            error.stale_ffs = exc.errno_ in (errno.EBUSY, errno.EINVAL)
            raise error from exc
        self.ffs = ffs

        # Link the function into the configuration, then bind to the UDC.
        self._link_function_into_config(FFS_FUNCTION_NAME)
        self._log("Function linked into configuration.")

        udc = self._detect_udc()
        if udc is None:
            raise GadgetError("No UDC available; cannot bind gadget.")
        self._log(f"Binding gadget to UDC {udc}...")
        self._write(f"{base}/UDC", udc + "\n")
        self._log(f"Gadget bound to UDC {udc}; waiting for the host to "
                  "configure the Xbox 360 controller...")
        # Do not fail when the host has not configured us yet (e.g. cable
        # plugged in later); wait_enabled() can be polled by the caller.
        ffs.wait_enabled(timeout=5.0)
        self._verify_enumeration(ffs)

    def _verify_enumeration(self, ffs):
        """Log the FunctionFS handshake state as enumeration evidence.

        On the peripheral side the gadget never appears under
        ``/sys/bus/usb/devices`` (that only lists devices behind host
        controllers), so the definitive device-side signals are:

        * ``FunctionFS ENABLE`` (ep0 event)  -- the host configured the
          device, i.e. USB enumeration succeeded with our descriptors.
        * ``ep0: GET_DESCRIPTOR(HID Report)`` -- Windows' XInput driver
          (xusb.sys) probing interface 0; plain HID hosts never ask for it.
        * a growing ``polls`` counter          -- input reports delivered.

        This logs which of those fired so `grep` on the log answers whether
        the pad came up as XInput.
        """
        if ffs.enabled:
            self._log(
                "Enumeration OK: host configured the Xbox 360 controller "
                "(FunctionFS ENABLE). Reports go out on ep1; grep for "
                "'GET_DESCRIPTOR(HID' to confirm the host's XInput driver.")
        else:
            self._log(
                "Host has not configured the controller yet. Replug the "
                "USB-C cable; if 'FunctionFS ENABLE' never appears in the "
                "log, the host rejected our descriptors.")

    def _teardown_configfs_gadget(self):
        if not os.path.exists(GADGET_PATH):
            return
        try:
            self._write(f"{GADGET_PATH}/UDC", "\n")
        except Exception as exc:
            self._log(f"UDC unbind write failed: {exc}")
        for _ in range(20):
            try:
                if self._read(f"{GADGET_PATH}/UDC").strip() == "":
                    break
            except OSError:
                break
            time.sleep(0.1)
        # Recursive purge: configfs directories must be emptied deepest-
        # first, and kernel releases can lag a beat behind (fds closing on
        # other threads), so retry the whole sweep until empty or timeout.
        deadline = time.time() + 8.0
        while True:
            stuck = self._purge_tree(GADGET_PATH)
            if not stuck:
                break
            # Also unmount again mid-loop: a lingering ffs mount is the usual
            # reason functions/ffs.xinput refuses to die.
            if _functionfs_mounted(FFS_DIR):
                self._unmount_functionfs()
            if time.time() > deadline:
                for path, exc in stuck:
                    self._log(f"teardown stuck on {path}: {exc}")
                break
            time.sleep(0.3)

    def _purge_tree(self, root):
        """Delete *root* recursively; return [(path, exc)] that still remain."""
        if os.path.islink(root):
            try:
                os.unlink(root)
            except OSError:
                pass
            return [] if not os.path.exists(root) else []
        if not os.path.isdir(root):
            try:
                os.unlink(root)
            except OSError:
                pass
            return []
        stuck = []
        entries = sorted(os.listdir(root), reverse=True)
        for entry in entries:
            path = f"{root}/{entry}"
            if os.path.islink(path):
                try:
                    os.unlink(path)
                    continue
                except OSError as exc:
                    stuck.append((path, exc))
                    continue
            if os.path.isdir(path):
                sub_stuck = self._purge_tree(path)
                stuck.extend(sub_stuck)
                if not sub_stuck and os.path.exists(path):
                    try:
                        os.rmdir(path)
                    except OSError as exc:
                        stuck.append((path, exc))
            else:
                try:
                    os.unlink(path)
                except OSError as exc:
                    stuck.append((path, exc))
        try:
            os.rmdir(root)
        except OSError as exc:
            stuck.append((root, exc))
        # Filter entries already gone.
        return [(p, e) for p, e in stuck if os.path.exists(p)]

    # -- filesystem helpers -------------------------------------------------

    def _wait_for_udc(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._detect_udc():
                return True
            time.sleep(0.2)
        return False

    def _detect_udc(self):
        udc_dir = "/sys/class/udc"
        if not os.path.isdir(udc_dir):
            return None
        entries = sorted(os.listdir(udc_dir))
        return entries[0] if entries else None

    def _read(self, path):
        with open(path, "r", errors="replace") as handle:
            return handle.read()

    def _write(self, path, value):
        try:
            with open(path, "w") as handle:
                handle.write(value)
        except PermissionError:
            if not self._sudo:
                raise GadgetError(
                    f"Permission denied writing {path} (this plugin needs root).")
            cmd = ["sudo", "sh", "-c",
                   f"printf '%s' {shlex.quote(value)} > {shlex.quote(path)}"]
            self._run(cmd, path)
        except OSError as exc:
            raise GadgetError(f"Failed to write {path}: {exc}") from exc

    def _write_binary(self, path, blob):
        """Write raw bytes (e.g. an f_hid report descriptor) via a temp file,
        since configfs attrs only accept binary data and hex string forms."""
        try:
            with open(path, "wb") as handle:
                handle.write(blob)
        except PermissionError:
            if not self._sudo:
                raise GadgetError(
                    f"Permission denied writing {path} (this plugin needs root).")
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(blob)
                tmp_path = tmp.name
            try:
                self._shell(f"cp {shlex.quote(tmp_path)} {shlex.quote(path)}")
            finally:
                os.unlink(tmp_path)
        except OSError as exc:
            raise GadgetError(f"Failed to write {path}: {exc}") from exc

    def _mkdir(self, path):
        try:
            os.makedirs(path, exist_ok=True)
        except PermissionError:
            if not self._sudo:
                raise GadgetError(
                    f"Permission denied creating {path} (this plugin needs root).")
            cmd = ["sudo", "mkdir", "-p", path]
            self._run(cmd, path)
        except OSError as exc:
            raise GadgetError(f"Failed to create {path}: {exc}") from exc

    def _shell(self, command):
        if self._sudo:
            command = f"sudo {command}"
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GadgetError(
                f"Command failed ({command}): {proc.stderr.strip() or proc.returncode}")

    def _run(self, cmd, path):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GadgetError(
                f"Failed to write {path}: {proc.stderr.strip() or proc.returncode}")


def _f_fs_available():
    """True when the running kernel ships the ``usb_f_fs`` gadget function."""
    if os.path.isdir("/sys/module/usb_f_fs"):
        return True
    for root in ("/lib/modules", "/usr/lib/modules"):
        for entry in glob.glob(f"{root}/*/kernel/drivers/usb/gadget/function/*ffs*"):
            return True
    return False
