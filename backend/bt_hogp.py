"""Bluetooth LE HID-over-GATT (HOGP) gamepad service.

Presents the Deck as a standard BLE HID gamepad ("SteamDeckPad") using the
BlueZ daemon's D-Bus GATT server APIs — no extra kernel modules, and the
host PC pairs with it natively (Windows 10+/Linux/macOS all implement
HID-over-GATT). The 12-byte report produced by ``controller.build_hid_frame``
is the BLE report payload (same bytes as the USB HID feed).

Requires ``python3-gobject`` (PyGObject / Gio) and a BlueZ daemon with
GattManager1 + LEAdvertisingManager1 (SteamOS ships both).

Implementation notes:
* We export a GATT application object tree on the system bus
  (service -> characteristics -> descriptors) and register it with
  ``GattManager1.RegisterApplication``.
* Input reports are delivered by emitting ``PropertiesChanged`` (Value) on
  the Report characteristic; BlueZ forwards it as a GATT notification to
  bonded hosts.
* A static BLE random address (btmgmt) prevents the Deck from appearing as
  a brand-new controller after reboots (identity rotation).
"""

import os
import struct
import threading
import time

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    _GI_OK = True
    _GI_ERROR = None
except Exception as _exc:  # pragma: no cover - depends on host packages
    gi = None
    Gio = None
    GLib = None
    _GI_OK = False
    _GI_ERROR = _exc

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
GATT_MANAGER = "org.bluez.GattManager1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"

ROOT = "/org/steamdeck/hogp"
SVC = ROOT + "/service0"
DIS_SVC = ROOT + "/dis"                       # Device Information Service
CHR_REPORT = ROOT + "/report"
CHR_REPORT_MAP = ROOT + "/reportmap"
CHR_HID_INFO = ROOT + "/hidinfo"
CHR_CTRL = ROOT + "/ctrlpoint"
CHR_PROTO = ROOT + "/protocol"
CHR_PNP_ID = DIS_SVC + "/pnpid"
DSC_REPORT_REF = CHR_REPORT + "/repref"

UUID_SVC = "00001812-0000-1000-8000-00805f9b34fb"          # HID service
UUID_DIS = "0000180a-0000-1000-8000-00805f9b34fb"          # Device Information
UUID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"       # Report
UUID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"   # Report Map
UUID_HID_INFO = "00002a4a-0000-1000-8000-00805f9b34fb"     # HID Information
UUID_CTRL_POINT = "00002a4c-0000-1000-8000-00805f9b34fb"   # HID Control Point
UUID_PROTO_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"   # Protocol Mode
UUID_PNP_ID = "00002a50-0000-1000-8000-00805f9b34fb"       # PnP ID
UUID_REPORT_REF = "00002908-0000-1000-8000-00805f9b34fb"   # Report Reference

# Same identity as the USB gadget so the Windows bridge can match either
# transport by VID/PID. PnP ID source byte: 0x01 = Bluetooth SIG-assigned,
# 0x02 = USB Implementers Forum. It MUST be 0x02 so Windows reports the USB
# VID/PID (0079:0006) to the HID API instead of a Bluetooth company id.
PNP_ID_VALUE = bytes([0x02]) + struct.pack("<HHH", 0x0079, 0x0006, 0x0100)

# BLE report map = the same HID descriptor the USB gadget uses (no report
# IDs, so reports are the raw 12 bytes).
REPORT_MAP = bytes([
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
    0x81, 0x03,        #   Input (Constant)
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x16, 0x00, 0x80,  #   Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #   Logical Maximum (32767)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x16, 0x00, 0x80,  #   Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #   Logical Maximum (32767)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0xC0,              # End Collection
])

HID_INFORMATION = bytes([0x01, 0x01, 0x00, 0x02])  # bcdHID 1.01, country 0, flags
ADAPTER = "/org/bluez/hci0"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"

# Saved bond: first line = PC Bluetooth address, second line = display name.
# Written when a host pairs, used to auto-reconnect after reboots and as the
# target of the launcher "Unpair PC" button (which drops a flag file).
BOND_FILE = "/home/deck/usb-gamepad-bt-bond"
UNPAIR_FLAG = "/home/deck/usb-gamepad-bt-unpair"

# Deck-initiated reconnection to the saved bond. OFF by default: on some
# controllers (including the Deck's) initiating a LE connection while
# advertising tears the advertisement down, so the host loses the HID device.
# The persisted bond + Trusted flag is enough -- Windows reconnects to a
# bonded BLE HID device on its own. Set to True only after testing.
AUTOCONNECT = False


class BtHogpError(Exception):
    """Raised when the BLE HOGP service cannot be set up."""


class BtHogpService:
    """BLE HID gamepad peripheral served over BlueZ D-Bus."""

    def __init__(self, logger=None, adapter=ADAPTER, name="SteamDeckPad"):
        if not _GI_OK:
            raise BtHogpError(
                "python3-gobject is required for Bluetooth mode "
                f"({_GI_ERROR}). Install it with: "
                "sudo pacman -S python-gobject")
        self.log = logger or (lambda msg: None)
        self.adapter = adapter
        self.name = name
        self.running = False
        self.polls = 0
        self.errors = 0
        self.subscribed = False
        self._conn = None
        self._reg_ids = []
        self._adv_id = 0
        self._loop = None
        self._loop_thread = None
        self._last_report = bytes(12)
        self._lock = threading.Lock()
        self.bond_address = ""
        self.bond_name = ""
        self._bond_thread = None
        self._bond_stop = False
        self._last_connect_try = 0.0
        self._connect_attempted_for = ""
        self._was_connected = False

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if self.running:
            return
        self._ensure_static_address()
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            raise BtHogpError(f"Cannot connect to the system bus: {exc}") from exc

        self.running = True
        # Run the main loop before registering: BlueZ calls back into our
        # exported objects (GetManagedObjects / GetAll) while processing
        # RegisterApplication / RegisterAdvertisement, and those incoming calls
        # are only dispatched while the loop runs. Registering first makes the
        # synchronous _call below block until the D-Bus timeout (24).
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, daemon=True, name="bt-hogp-mainloop")
        self._loop_thread.start()
        try:
            self._register_objects()
            # BlueZ only publishes external GATT applications to remote peers
            # after GattManager1.RegisterApplication; without this the Deck
            # advertises the HID-service UUID but ATT discovery returns no
            # services and the pad never works as a gamepad.
            self._call(BLUEZ, self.adapter, GATT_MANAGER,
                       "RegisterApplication",
                       GLib.Variant("(oa{sv})", (ROOT, {})))
            self._start_advertising()
        except Exception:
            # Roll back a half-registered application so a later retry starts
            # from a clean state instead of leaking objects/connections.
            try:
                self.stop()
            except Exception:
                pass
            raise
        self.log("Bluetooth HOGP service started; the Deck is pairable as "
                 f"'{self.name}'.")
        self._read_bond()
        if self.bond_address:
            self.log("Saved Bluetooth PC bond: %s (%s)"
                     % (self.bond_name or "?", self.bond_address))
        self._bond_stop = False
        self._bond_thread = threading.Thread(
            target=self._bond_loop, daemon=True, name="bt-hogp-bond")
        self._bond_thread.start()

    def _derive_static_address(self):
        """Derive a per-unit random static BLE address from /etc/machine-id.

        Random static addresses must have their two most-significant bits set
        (0b11...). Deriving it per unit avoids the collisions that a single
        hard-coded address would cause when two Decks are in range.
        """
        import hashlib
        try:
            with open("/etc/machine-id", "rb") as handle:
                machine_id = handle.read(32).strip()
        except OSError:
            machine_id = b"steamdeck-no-machine-id"
        digest = hashlib.sha256(machine_id).digest()
        addr = bytearray(digest[:6])
        addr[0] |= 0xC0   # top two bits must be 1 for a static random address
        return ":".join("%02X" % octet for octet in addr)

    def _ensure_static_address(self):
        """Pin a per-unit random static BLE address so the Deck keeps its
        identity across reboots (otherwise address rotation makes Windows
        treat it as a brand-new controller). Best effort via btmgmt."""
        import subprocess
        target = self._derive_static_address()
        try:
            probe = subprocess.run(["btmgmt", "info"], capture_output=True,
                                   text=True, timeout=5)
            # `static-addr` appears in BOTH the `supported settings:` and the
            # `current settings:` lines of `btmgmt info`. Only the *current*
            # setting means an address is actually loaded, so restrict the
            # match to that line. (btmgmt's `static-addr` command has no "get"
            # form -- an address argument is required to set.)
            current = ""
            for line in probe.stdout.splitlines():
                if "current settings" in line:
                    current = line
                    break
            if "static-addr" in current.lower():
                return   # already configured
            subprocess.run(["btmgmt", "power", "off"], capture_output=True,
                           timeout=5, check=True)
            time.sleep(1)
            subprocess.run(["btmgmt", "static-addr", target],
                           capture_output=True, text=True, timeout=5,
                           check=True)
            time.sleep(1)
            subprocess.run(["btmgmt", "power", "on"], capture_output=True,
                           timeout=5)
            self.log("Static BLE address configured (%s; identity stable)."
                     % target)
        except Exception as exc:
            self.log(f"Static BLE address setup skipped: {exc}")

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._bond_stop = True
        if self._bond_thread is not None:
            self._bond_thread.join(timeout=2.0)
            self._bond_thread = None
        try:
            if self._adv_id:
                self._call(BLUEZ, self.adapter, ADV_MANAGER,
                           "UnregisterAdvertisement",
                           GLib.Variant("(o)", (self._adv_path(),)))
        except Exception:
            pass
        try:
            self._call(BLUEZ, self.adapter, GATT_MANAGER,
                       "UnregisterApplication",
                       GLib.Variant("(o)", (ROOT,)))
        except Exception:
            pass
        if self._loop is not None:
            self._loop.quit()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        for rid in self._reg_ids:
            try:
                self._conn.unregister_object(rid)
            except Exception:
                pass
        self._reg_ids = []
        self._adv_id = 0
        self._loop = None
        self._loop_thread = None
        self._conn = None
        self._configured = False
        self.log("Bluetooth HOGP service stopped.")

    # -- PC bond (persist pairing + auto-reconnect + unpair) -----------------

    def _read_bond(self):
        try:
            with open(BOND_FILE, "r") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return
        if lines and lines[0].strip():
            self.bond_address = lines[0].strip()
            self.bond_name = lines[1].strip() if len(lines) > 1 else ""

    def _write_bond(self):
        try:
            with open(BOND_FILE, "w") as handle:
                handle.write("%s\n%s\n" % (self.bond_address or "",
                                           self.bond_name or ""))
        except OSError as exc:
            self.log("Could not save Bluetooth PC bond: %s" % exc)

    def _managed_devices(self):
        reply = self._conn.call_sync(
            BLUEZ, "/", OM_IFACE, "GetManagedObjects", None, None,
            Gio.DBusCallFlags.NONE, 5000, None)
        objects = reply.unpack()[0]
        prefix = self.adapter + "/"
        devices = []
        for path, ifaces in objects.items():
            dev = ifaces.get(DEVICE_IFACE)
            if dev is not None and path.startswith(prefix):
                devices.append((path, dev))
        return devices

    @staticmethod
    def _variant_str(dev, key):
        value = dev.get(key)
        try:
            return value.get_string() if value is not None else ""
        except Exception:
            return ""

    @staticmethod
    def _variant_bool(dev, key):
        value = dev.get(key)
        try:
            return bool(value.get_boolean()) if value is not None else False
        except Exception:
            return False

    def _bond_loop(self):
        while not self._bond_stop:
            try:
                self._refresh_bond()
            except Exception as exc:
                self.log("Bluetooth bond poll failed: %s" % exc)
            for _ in range(20):
                if self._bond_stop:
                    break
                time.sleep(0.1)

    def _refresh_bond(self):
        if not self.running or self._conn is None:
            return
        # "Unpair PC" button in the launcher drops this flag file.
        if os.path.exists(UNPAIR_FLAG):
            try:
                os.remove(UNPAIR_FLAG)
            except OSError:
                pass
            self.unpair()
            return

        bonded = [(path, dev) for path, dev in self._managed_devices()
                  if self._variant_bool(dev, "Paired")]
        if not bonded:
            return

        chosen = None
        for path, dev in bonded:
            if self.bond_address and \
                    self._variant_str(dev, "Address") == self.bond_address:
                chosen = (path, dev)
                break
        if chosen is None:
            for path, dev in bonded:
                if self._variant_bool(dev, "Connected"):
                    chosen = (path, dev)
                    break
        if chosen is None:
            chosen = bonded[0]
        path, dev = chosen

        addr = self._variant_str(dev, "Address")
        name = self._variant_str(dev, "Alias") or self._variant_str(dev, "Name")
        if addr and (addr != self.bond_address or name != self.bond_name):
            self.bond_address = addr
            self.bond_name = name
            self._write_bond()
            self.log("Bluetooth PC bond saved: %s (%s)"
                     % (name or "?", addr))

        # Trust the host so BlueZ accepts its reconnects without re-pairing.
        if not self._variant_bool(dev, "Trusted"):
            try:
                self._call(BLUEZ, path, PROPS_IFACE, "Set",
                           GLib.Variant("(ssv)", (DEVICE_IFACE, "Trusted",
                                                  GLib.Variant("b", True))))
            except Exception:
                pass

        connected = self._variant_bool(dev, "Connected")
        if connected and not self._was_connected:
            self.log("Bluetooth PC connected: %s (%s)"
                     % (name or "?", addr))
        self._was_connected = connected

        # Reconnect to the saved bond after a reboot / BT toggle without the
        # host having to initiate pairing again. Retried every 10 s while
        # disconnected (a failed attempt just means the PC isn't in range yet).
        if AUTOCONNECT and not connected and addr and \
                time.time() - self._last_connect_try >= 10.0:
            self._last_connect_try = time.time()
            if self._connect_attempted_for != addr:
                self._connect_attempted_for = addr
                self.log("Bluetooth auto-connect requested for %s." % addr)
            try:
                self._call(BLUEZ, path, DEVICE_IFACE, "Connect", None)
            except Exception:
                pass

    def unpair(self):
        """Forget the bonded PC in BlueZ and clear the saved bond."""
        target = self.bond_address
        removed = []
        try:
            devices = self._managed_devices()
        except Exception as exc:
            self.log("Unpair failed to list devices: %s" % exc)
            devices = []
        for path, dev in devices:
            if not self._variant_bool(dev, "Paired"):
                continue
            addr = self._variant_str(dev, "Address")
            if target and addr != target:
                continue
            try:
                self._call(BLUEZ, self.adapter, ADAPTER_IFACE, "RemoveDevice",
                           GLib.Variant("(o)", (path,)))
                removed.append(addr or path)
            except Exception as exc:
                self.log("Unpair failed for %s: %s" % (addr or path, exc))
        try:
            os.remove(BOND_FILE)
        except OSError:
            pass
        self.bond_address = ""
        self.bond_name = ""
        self._connect_attempted_for = ""
        self._was_connected = False
        if removed:
            self.log("Unpaired Bluetooth PC: %s" % ", ".join(removed))
        else:
            self.log("Unpair requested; no matching bonded PC found.")
        return removed

    # -- GATT object registration --------------------------------------------

    def _register_objects(self):
        node = Gio.DBusNodeInfo.new_for_xml(self._root_xml())
        rid = self._conn.register_object(
            ROOT, node.interfaces[0], self._om_call, None, None)
        self._reg_ids.append(rid)

        svc_node = Gio.DBusNodeInfo.new_for_xml(
            """<node>
              <interface name="org.bluez.GattService1">
                <property name="UUID" type="s" access="read"/>
                <property name="Primary" type="b" access="read"/>
              </interface>
            </node>""")
        rid = self._conn.register_object(
            SVC, svc_node.interfaces[0], self._svc_call, None, None)
        self._reg_ids.append(rid)

        char_xml = """<node>
          <interface name="org.bluez.GattCharacteristic1">
            <method name="ReadValue">
              <arg name="options" direction="in" type="a{sv}"/>
              <arg name="value" direction="out" type="ay"/>
            </method>
            <method name="WriteValue">
              <arg name="value" direction="in" type="ay"/>
              <arg name="options" direction="in" type="a{sv}"/>
            </method>
            <method name="StartNotify"/>
            <method name="StopNotify"/>
            <property name="UUID" type="s" access="read"/>
            <property name="Service" type="o" access="read"/>
            <property name="Flags" type="as" access="read"/>
          </interface>
        </node>"""
        char_node = Gio.DBusNodeInfo.new_for_xml(char_xml)
        for path, uuid, flags in (
            (CHR_REPORT, UUID_REPORT, ["read", "notify"]),
            (CHR_REPORT_MAP, UUID_REPORT_MAP, ["read"]),
            (CHR_HID_INFO, UUID_HID_INFO, ["read"]),
            (CHR_CTRL, UUID_CTRL_POINT, ["write-without-response"]),
            (CHR_PROTO, UUID_PROTO_MODE, ["read", "write-without-response"]),
            (CHR_PNP_ID, UUID_PNP_ID, ["read"]),
        ):
            rid = self._conn.register_object(
                path, char_node.interfaces[0],
                self._make_char_call(path, uuid, flags),
                self._make_char_get(path, uuid, flags),
                None)
            self._reg_ids.append(rid)

        dsc_node = Gio.DBusNodeInfo.new_for_xml(
            """<node>
              <interface name="org.bluez.GattDescriptor1">
                <method name="ReadValue">
                  <arg name="options" direction="in" type="a{sv}"/>
                  <arg name="value" direction="out" type="ay"/>
                </method>
                <property name="UUID" type="s" access="read"/>
                <property name="Characteristic" type="o" access="read"/>
                <property name="Flags" type="as" access="read"/>
              </interface>
            </node>""")
        rid = self._conn.register_object(
            DSC_REPORT_REF, dsc_node.interfaces[0],
            self._dsc_call, None, None)
        self._reg_ids.append(rid)

    def _root_xml(self):
        return """<node name="/">
          <interface name="org.freedesktop.DBus.ObjectManager">
            <method name="GetManagedObjects">
              <arg name="objects" direction="out" type="a{oa{sa{sv}}}"/>
            </method>
          </interface>
        </node>"""

    def _om_call(self, connection, sender, path, iface, method, params,
                 invocation):
        v = GLib.Variant("(a{oa{sa{sv}}})", (self._managed_objects(),))
        invocation.return_value(v)

    def _managed_objects(self):
        svc_props = {
            "UUID": GLib.Variant("s", UUID_SVC),
            "Primary": GLib.Variant("b", True),
        }
        def char_props(uuid, flags, service=SVC):
            return {
                "UUID": GLib.Variant("s", uuid),
                "Service": GLib.Variant("o", service),
                "Flags": GLib.Variant("as", flags),
            }
        report_props = char_props(UUID_REPORT, ["read", "notify"])
        report_props["Value"] = GLib.Variant("ay", self._last_report)
        dis_props = {
            "UUID": GLib.Variant("s", UUID_DIS),
            "Primary": GLib.Variant("b", True),
        }
        objects = {
            SVC: {"org.bluez.GattService1": svc_props},
            DIS_SVC: {"org.bluez.GattService1": dis_props},
            CHR_REPORT: {"org.bluez.GattCharacteristic1": report_props},
            CHR_REPORT_MAP: {"org.bluez.GattCharacteristic1":
                             char_props(UUID_REPORT_MAP, ["read"])},
            CHR_HID_INFO: {"org.bluez.GattCharacteristic1":
                           char_props(UUID_HID_INFO, ["read"])},
            CHR_CTRL: {"org.bluez.GattCharacteristic1":
                       char_props(UUID_CTRL_POINT,
                                  ["write-without-response"])},
            CHR_PROTO: {"org.bluez.GattCharacteristic1":
                        char_props(UUID_PROTO_MODE,
                                   ["read", "write-without-response"])},
            CHR_PNP_ID: {"org.bluez.GattCharacteristic1":
                         char_props(UUID_PNP_ID, ["read"], DIS_SVC)},
            DSC_REPORT_REF: {"org.bluez.GattDescriptor1": {
                "UUID": GLib.Variant("s", UUID_REPORT_REF),
                "Characteristic": GLib.Variant("o", CHR_REPORT),
                "Flags": GLib.Variant("as", ["read"]),
            }},
        }
        return objects

    def _svc_call(self, connection, sender, path, iface, method, params,
                  invocation):
        invocation.return_value(None)

    def _make_char_call(self, path, uuid, flags):
        def handler(connection, sender, object_path, iface_name, method,
                    params, invocation):
            if method == "ReadValue":
                if path == CHR_REPORT:
                    value = self._last_report
                elif path == CHR_REPORT_MAP:
                    value = REPORT_MAP
                elif path == CHR_HID_INFO:
                    value = HID_INFORMATION
                elif path == CHR_PNP_ID:
                    value = PNP_ID_VALUE
                elif path == CHR_PROTO:
                    value = bytes([0x01])
                else:
                    value = bytes(1)
                opts = params[0]
                offset = 0
                try:
                    offset = int(opts["offset"].get_uint16())
                except (KeyError, TypeError, ValueError):
                    pass
                invocation.return_value(
                    GLib.Variant("(ay)", (value[offset:],)))
            elif method == "WriteValue":
                # Control point / protocol mode writes are accepted and
                # ignored (no suspend handling; protocol mode is fixed).
                invocation.return_value(None)
            elif method == "StartNotify":
                if path == CHR_REPORT:
                    self.subscribed = True
                    self.log("Host subscribed to HID reports (CCCD written).")
                invocation.return_value(None)
            elif method == "StopNotify":
                if path == CHR_REPORT:
                    self.subscribed = False
                    self.log("Host unsubscribed from HID reports.")
                invocation.return_value(None)
            else:
                invocation.return_error(
                    Gio.DBusError, Gio.DBusError.UNKNOWN_METHOD)
        return handler

    def _make_char_get(self, path, uuid, flags):
        def getter(connection, sender, object_path, iface_name, prop_name):
            if prop_name == "UUID":
                return GLib.Variant("s", uuid)
            if prop_name == "Service":
                # The PnP ID characteristic belongs to the Device Information
                # service; every other characteristic belongs to the HID
                # service. BlueZ uses this property to build the GATT tree.
                service = DIS_SVC if path == CHR_PNP_ID else SVC
                return GLib.Variant("o", service)
            if prop_name == "Flags":
                return GLib.Variant("as", flags)
            if prop_name == "Value" and path == CHR_REPORT:
                return GLib.Variant("ay", self._last_report)
            return None
        return getter

    def _dsc_call(self, connection, sender, path, iface, method, params,
                  invocation):
        if method == "ReadValue":
            # Report Reference = [ReportID, ReportType]. The report map has no
            # report IDs, so ReportID is 0x00; ReportType is Input (0x01).
            invocation.return_value(GLib.Variant("(ay)", (bytes([0x00, 0x01]),)))
        else:
            invocation.return_value(None)

    def _dsc_get(self, connection, sender, path, iface, prop):
        if prop == "UUID":
            return GLib.Variant("s", UUID_REPORT_REF)
        if prop == "Characteristic":
            return GLib.Variant("o", CHR_REPORT)
        if prop == "Flags":
            return GLib.Variant("as", ["read"])
        return None

    # -- advertising ---------------------------------------------------------

    def _adv_path(self):
        return ROOT + "/adv"

    def _start_advertising(self):
        adv_xml = """<node>
          <interface name="org.bluez.LEAdvertisement1">
            <property name="Type" type="s" access="read"/>
            <property name="ServiceUUIDs" type="as" access="read"/>
            <property name="LocalName" type="s" access="read"/>
            <property name="Appearance" type="q" access="read"/>
            <property name="Discoverable" type="b" access="read"/>
            <property name="Includes" type="as" access="read"/>
          </interface>
        </node>"""
        node = Gio.DBusNodeInfo.new_for_xml(adv_xml)

        def prop_get(conn, sender, path, iface, prop):
            if prop == "Type":
                return GLib.Variant("s", "peripheral")
            if prop == "ServiceUUIDs":
                return GLib.Variant("as", [UUID_SVC])
            if prop == "LocalName":
                return GLib.Variant("s", self.name)
            if prop == "Appearance":
                return GLib.Variant("q", 0x03C4)   # gamepad
            if prop == "Discoverable":
                return GLib.Variant("b", True)
            if prop == "Includes":
                return GLib.Variant("as", ["tx-power"])
            return None

        def method_call(conn, sender, path, iface, method, params, invocation):
            invocation.return_value(None)

        rid = self._conn.register_object(
            self._adv_path(), node.interfaces[0], method_call, prop_get, None)
        self._reg_ids.append(rid)
        self._adv_id = self._adv_path()
        self._call(BLUEZ, self.adapter, ADV_MANAGER, "RegisterAdvertisement",
                   GLib.Variant("(oa{sv})", (self._adv_path(), {})))
        self.log("BLE advertisement registered (gamepad appearance).")

    def _call(self, dest, path, iface, method, params):
        return self._conn.call_sync(
            dest, path, iface, method, params,
            None, Gio.DBusCallFlags.NONE, 5000, None)

    # -- reports -------------------------------------------------------------

    def send_report(self, data):
        """Notify the connected host with the 12-byte gamepad report."""
        if not self.running or self._conn is None:
            return None
        report = bytes(data)[:12].ljust(12, b"\0")
        with self._lock:
            if report == self._last_report:
                return 0
            self._last_report = report
        try:
            self._conn.emit_signal(
                None, CHR_REPORT, "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (
                    "org.bluez.GattCharacteristic1",
                    {"Value": GLib.Variant("ay", report)},
                    [])))
        except Exception:
            self.errors += 1
            return 1
        self.polls += 1
        return 0
