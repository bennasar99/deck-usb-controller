# Deck USB XInput Controller

Turn any **Steam Deck** into a **wired game controller** for a PC connected
over USB-C. Launch the app from Game Mode as a non-Steam game: a small window
opens, and the Deck's sticks, triggers, buttons and D-pad are forwarded over
the USB link to the host PC.

The app picks the protocol per host automatically:

| Host | Protocol | How it works |
|------|----------|--------------|
| Linux / Steam Machine PCs | **Native XInput** (Xbox 360 pad, `045E:028E`) via FunctionFS | The kernel `xpad` driver binds and consumes reports immediately. |
| Windows PCs | Ignores emulated Xbox 360 pads lacking hardware-authentic descriptors → after one automatic retry the app switches to a **standard USB HID gamepad** (`f_hid`, `/dev/hidg0`) | Every OS maps plain HID out of the box; Steam Input converts it for modern games. |

> Requires the Deck's USB-C port to be in **DRD (Dual Role Device)** mode in
> the BIOS. See [INSTALL.md](./INSTALL.md).

## How it works

Game Mode processes cannot run as root, so the app is split in two:

1. **The "game"** (`/opt/usb-gamepad/usb_gamepad`, compiled from
   `usb_gamepad_launcher.c`) runs as the `deck` user. It is the running app that
   keeps Steam Input's virtual gamepad alive, shows a small X11 window, and
   keeps a marker file (`/home/deck/usb-gamepad-active`) fresh while it runs.
2. **A root daemon** (`usb_gamepad.py`, a systemd service enabled at boot)
   watches the marker. When the game runs it brings up the controller gadget,
   reads Steam's virtual pad and forwards reports to the PC. When the game
   closes it tears the gadget down and the Deck returns to normal USB.

### Protocol selection

* XInput mode starts first. If the host configures the device but never
  consumes a report, the daemon re-enumerates once (cable-replug equivalent),
  then switches permanently (for that session) to HID compatibility mode.
* The raw_gadget backend (`backend/xinput_raw.py`, byte-exact Xbox 360
  emulation including vendor descriptors) activates automatically whenever a
  kernel allows raw_gadget on the Deck's USB-C controller; current SteamOS
  builds reject it at `USB_RAW_IOCTL_RUN` (verified against the dwc3 UDC),
  so FunctionFS serves XInput until that changes.

```
+-------------------------+        +--------------------------+
| Steam Deck (Game Mode)  |  USB-C | PC                        |
|                         |  DRD   |                           |
|  usb_gamepad (window +  |        |  Native XInput driver     |
|  marker file)           |        |  (xusb/xpad)              |
|    │ marker fresh       |        |  ▲                        |
|    ▼                    |        |  │ input reports          |
|  usb_gamepad.py daemon  ├────────┤  │ (20 bytes each)        |
|    │ (root, systemd)    │ /dev/  │  │                        |
|    │ reads evdev        │ ffs-*  │  │                        |
|    ▼                    │        │  │                        |
|  USB gadget             │        │  │                        |
|  (FunctionFS, dwc3 DRD) ─────────┘                          |
+-------------------------+        +--------------------------+
```

- The gadget setup finds the AMD DWC3 USB controller, re-binds it from the host
  (`xhci_hcd`) driver to the gadget (`dwc3-pci`) driver and builds a configfs
  USB gadget with a **FunctionFS** function presenting the exact vendor-specific
  layout of a wired Xbox 360 pad (VID `0x045E`, PID `0x028E`; interface class
  `0xFF/0x5D`) — hosts load their native XInput driver automatically.
- The daemon reads the chosen input device (`/dev/input/event*`) with a small
  dependency-free evdev reader, rebuilds the 20-byte XInput report on every
  state change and writes it to the FunctionFS IN endpoint.

## Files

| Path                  | Role                                                            |
|-----------------------|-----------------------------------------------------------------|
| `usb_gamepad_launcher.c` | Native launcher "game": X11 window + marker file, no root.  |
| `usb_gamepad.py`      | Root forwarder daemon (systemd service); protocol watchdog.     |
| `install-usb-gamepad.sh` | Installs to `/opt/usb-gamepad`, compiles the launcher, installs the service. |
| `backend/controller.py` | State, input→report forwarding logic (XInput + HID formats).  |
| `backend/gadget_manager.py` | Gadget lifecycle: FunctionFS XInput + f_hid HID modes.    |
| `backend/xinput_ffs.py`   | User-space Xbox 360 controller on FunctionFS.               |
| `backend/xinput_raw.py`   | raw_gadget XInput backend (auto-activates when supported).  |
| `backend/xinput_report.py` | Xbox 360 wire format: report builder + parsing.           |
| `backend/gamepad_report.py` | Standard HID gamepad descriptor + 13-byte reports.       |
| `backend/evdev_reader.py` | Pure-Python evdev device reading and discovery.             |
| `INSTALL.md`          | Complete installation guide for any Steam Deck.                 |
| `tests/`              | Report/descriptor unit tests.                                   |

## Installation

Full guide in [INSTALL.md](./INSTALL.md). Quick start:

1. `sudo steamos-readonly disable`
2. `sudo pacman -Sy --noconfirm libx11 base-devel`
3. Copy this folder to the Deck, then run `./install-usb-gamepad.sh`.
4. Add `/opt/usb-gamepad/usb_gamepad` to Steam as a **Non-Steam Game**.
5. Launch it from Game Mode while connected to the PC.

## Usage

1. Connect the Deck to the PC with a data-capable USB-C cable.
2. Launch the "USB Gamepad" game from your Steam library.
3. The PC sees a controller within seconds (Xbox 360 pad on Linux; after a
   ~15 s probe, a standard gamepad on Windows). Play!
4. **OLED burn-in protection**: the app window goes fully black after 10 s
   without input — intentional, to protect the Deck's OLED panel during long
   sessions. Touch the screen (or move a finger on it) and it returns to
   normal instantly. Forwarding keeps running while blacked out.
5. Close the "game" to stop and restore the Deck's normal USB port.

## Logs & troubleshooting

All messages land in `/home/deck/usb-gamepad.log`; the daemon's output is also
in the journal (`journalctl -u usb-gamepad -f`). Common issues:

- **Steam loading screen stays** — `libx11` was missing when the installer ran
  (headless build). Install `libx11` and re-run the installer.
- **`No UDC`** — the USB-C port is not in DRD mode. Set **USB Dual Role Device**
  to **DRD** in the BIOS (Volume Up + Power).
- **`Switching to HID compatibility mode`** on Windows — expected behaviour:
  Windows' driver stack requires hardware-authentic descriptors that emulated
  Xbox 360 pads cannot present through FunctionFS, so the app switches to a
  standard HID gamepad it maps natively. Wait for
  `HID gamepad ready (/dev/hidg0)` in the log (~15 s after launch).
- **No input anywhere** — check the log shows `Reading Microsoft X-Box 360 pad
  0 (...)`, that Steam is focused in Game Mode, and that only one instance of
  the app runs (`systemctl status usb-gamepad`).

See [INSTALL.md](./INSTALL.md) for the full troubleshooting table.

## License

[MIT](./LICENSE)
