# Deck USB XInput Controller

Turn any **Steam Deck** into a **wired game controller** for a PC connected
over USB-C. Launch the app from Game Mode as a non-Steam game: a small window
opens with a **mode selector** (Auto / XInput / HID), and the Deck's sticks,
triggers, buttons and D-pad are forwarded over the USB link to the host PC.

| Mode | Behavior |
|------|----------|
| **Auto** (default) | Native **XInput** (Xbox 360 pad, `045E:028E`) via FunctionFS on Linux hosts; on Windows — which ignores emulated Xbox pads — the app probes, retries once, then switches itself to a **standard USB HID gamepad** (`f_hid`, `/dev/hidg0`) that Steam Input maps for modern games. |
| **XInput** | Forces the Xbox 360 gadget (FunctionFS). Native XInput on Linux hosts. |
| **HID** | Forces the standard HID gamepad — pair it with the Windows bridge app (`windows/deck2xinput.exe`, ViGEmBus) for native XInput on Windows. |

| **HID** | Forces the standard HID gamepad — pair it with the Windows bridge app (`windows/deck2xinput.exe`, ViGEmBus) for native XInput on Windows. |
| **Bluetooth** (toggle) | Independent of the USB modes: advertises the Deck as a BLE HID gamepad ("SteamDeckPad"). Pair it from the PC's Bluetooth settings — Windows 10+ maps it natively, Steam Input refines it. |

The selection is remembered (written to `~/usb-gamepad-mode` and
`~/usb-gamepad-bt`) and applied immediately — switching takes ~2 s, no
relaunch needed. Bluetooth runs alongside any USB mode.

> Requires the Deck's USB-C port to be in **DRD (Dual Role Device)** mode in
> the BIOS. See [INSTALL.md](./INSTALL.md).

## How it works

Game Mode processes cannot run as root, so the app is split in two:

1. **The "game"** (`/opt/usb-gamepad/usb_gamepad`, compiled from
   `usb_gamepad_launcher.c`) runs as the `deck` user. It shows the app window
   (mode buttons + status), keeps Steam Input's virtual gamepad alive, and
   keeps a marker file (`/home/deck/usb-gamepad-active`) fresh while it runs.
2. **A root daemon** (`usb_gamepad.py`, a systemd service enabled at boot)
   watches the marker. When the game runs it brings up the selected gadget,
   reads Steam's virtual pad and forwards reports to the PC. When the game
   closes it tears the gadget down and the Deck returns to normal USB.

### Protocol selection

* **Auto**: XInput starts first. If the host configures the device but never
  consumes a report (with inputs actively flowing), the daemon re-enumerates
  once (cable-replug equivalent), then switches permanently (for that session)
  to HID compatibility mode.
* **XInput**: FunctionFS serves the Xbox 360 pad. A `raw_gadget` backend
  (`backend/xinput_raw.py`, byte-exact emulation incl. vendor descriptors)
  exists and would deliver full XInput to Windows, but current SteamOS
  kernels reject raw_gadget on the Deck's USB-C controller
  (`USB_RAW_IOCTL_RUN` → EBUSY; verified and documented in `AGENTS.md`),
  so it is dormant: opt in with `/opt/usb-gamepad/try-raw`.
* **HID**: `f_hid` presents a generic gamepad (`0079:0006`) with a
  vendor-defined top-level usage — invisible to games, consumed only by the
  Windows bridge — with the D-pad encoded as button bits to avoid host
  hat-switch heuristics.
* **Bluetooth**: a BLE HID-over-GATT service (`backend/bt_hogp.py`) presents
  the Deck as a "SteamDeckPad" gamepad over BlueZ (D-Bus GATT server +
  LE advertisement, static BLE address for stable identity). Windows 10+
  pairs with it natively; no bridge or ViGEmBus needed. Requires the
  `python-gobject` package (installed by the installer).

## Files

| Path | Role |
|------|------|
| `usb_gamepad_launcher.c` | Native launcher "game": X11 window (mode buttons, OLED blanking) + marker file, no root. |
| `usb_gamepad.py` | Root forwarder daemon (systemd service); protocol watchdog. |
| `install-usb-gamepad.sh` | Installs to `/opt/usb-gamepad`, compiles the launcher, installs the service. |
| `backend/controller.py` | State, input→report forwarding (XInput + HID formats), per-source Y normalization. |
| `backend/gadget_manager.py` | Gadget lifecycle: FunctionFS XInput + f_hid HID modes, raw_gadget hook. |
| `backend/xinput_ffs.py` | User-space Xbox 360 controller on FunctionFS. |
| `backend/xinput_raw.py` | raw_gadget XInput backend (opt-in via `/opt/usb-gamepad/try-raw`). |
| `backend/xinput_report.py` | Xbox 360 wire format: report builder + rumble/LED parsing. |
| `backend/gamepad_report.py` | HID gamepad descriptor (vendor usage) + 12-byte reports. |
| `backend/evdev_reader.py` | Pure-Python evdev device reading and discovery. |
| `windows/` | `deck2xinput.exe` source: HID → ViGEmBus XInput bridge for Windows (ViGEmClient SDK vendored). |
| `tests/test_report.py` | Unit tests (run: `python tests/test_report.py`). |
| `AGENTS.md` | Repo status, investigation history, gotchas — for humans and agents. |

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
3. Pick a mode in the window (or stay on **Auto**). The PC sees a controller
   within seconds on Linux; on Windows expect the ~15 s probe, then a
   re-enumeration into HID mode (run `windows/deck2xinput.exe` there for
   native XInput).
4. **OLED burn-in protection**: the window goes fully black after 10 s
   without input. Touch the screen to wake it; forwarding keeps running
   while blacked out.
5. Close the "game" to stop and restore the Deck's normal USB port.

## Logs & troubleshooting

All messages land in `/home/deck/usb-gamepad.log`; the daemon's output is also
in the journal (`journalctl -u usb-gamepad -f`). The periodic `Status:` line
decodes the live controller state (`buttons`/`hat` or `lt`/`rt`/sticks).
Common issues:

- **Headless launcher / no window** — `libx11` was missing when the installer
  ran. Install `libx11` and re-run the installer.
- **`No UDC`** — the USB-C port is not in DRD mode. Set **USB Dual Role
  Device** to **DRD** in the BIOS (Volume Up + Power).
- **`Switching to HID compatibility mode`** on Windows — expected in Auto
  mode (Windows can't drive emulated Xbox pads through FunctionFS). Wait for
  `HID gamepad ready (/dev/hidg0)`, then start `deck2xinput.exe` on the PC.
- **No input anywhere** — the log must show `Reading Microsoft X-Box 360 pad
  0 (...)` and climbing `input_events=`; Steam Input only feeds the virtual
  pad while the game has Game Mode focus.
- **Mode buttons don't respond to taps** — check you're running the X11
  build (`Launcher built with an X11 window` during install) and not the
  headless fallback.

See [INSTALL.md](./INSTALL.md) for the full troubleshooting table and
[windows/README.md](./windows/README.md) for the Windows bridge.

## License

[MIT](./LICENSE)
