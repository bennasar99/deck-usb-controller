# Deck USB XInput Controller

Turn any **Steam Deck** into a **wired game controller** for a PC connected
over USB-C. Launch the app from Game Mode as a non-Steam game: a small window
opens with a **mode selector** (XInput / HID), and the Deck's sticks,
triggers, buttons and D-pad are forwarded over the USB link to the host PC.

| Mode | Behavior |
|------|----------|
| **HID** (default) | Standard USB HID gamepad (`f_hid`, `/dev/hidg0`, generic `0079:0006`) — pair it with the Windows bridge app (`windows/deck2xinput.exe`, ViGEmBus) for native XInput on Windows. |
| **XInput** | Xbox 360 gadget (FunctionFS, `045E:028E`). Native XInput on Linux hosts. |
| **Bluetooth** (toggle) | Independent of the USB modes: advertises the Deck as a BLE HID gamepad ("SteamDeckPad"). Pair it from the PC's Bluetooth settings — Windows 10+ maps it natively, Steam Input refines it. The paired PC is remembered (`~/usb-gamepad-bt-bond`) and reconnects after reboots; use the window's **Unpair PC** button to forget it. |

The selection is remembered (written to `~/usb-gamepad-mode` and
`~/usb-gamepad-bt`) and applied immediately — switching takes ~2 s, no
relaunch needed. Bluetooth runs alongside any USB mode.

> **XInput is USB-only; Bluetooth is always HID.** The XInput/HID selector
> applies to the USB gadget only — there is no native XInput over Bluetooth.
> With Bluetooth on, the Deck always advertises the BLE HID gamepad
> ("SteamDeckPad") regardless of the mode selected. On a **Linux host** that
> feed is a vendor-defined HID device and there is no Linux bridge, so it does
> not work as a gamepad over Bluetooth: use **USB + XInput** on Linux.

> **Bluetooth is experimental.** Pairing can be flaky: a host may need to be
> removed and re-paired, some hosts cache the device identity, and the link is
> less reliable than USB. Also note the Deck exposes its own **Bluetooth audio
> (speaker) profile**, so a host may detect/connect it as a speaker in addition
> to "SteamDeckPad" — remove that audio device on the host. The app disables
> the Deck's speaker role while the Bluetooth gamepad is active, but it may
> still be offered before/after.

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

* **HID** (default): `f_hid` presents a generic gamepad (`0079:0006`) with a
  vendor-defined top-level usage — invisible to games, consumed only by the
  Windows bridge — with the D-pad encoded as button bits to avoid host
  hat-switch heuristics.
* **XInput**: FunctionFS serves the Xbox 360 pad. A `raw_gadget` backend
  (`backend/xinput_raw.py`, byte-exact emulation incl. vendor descriptors)
  exists and would deliver full XInput to Windows, but current SteamOS
  kernels reject raw_gadget on the Deck's USB-C controller
  (`USB_RAW_IOCTL_RUN` → EBUSY; verified and documented in `AGENTS.md`),
  so it is dormant: opt in with `/opt/usb-gamepad/try-raw`.
* **Bluetooth**: a BLE HID-over-GATT service (`backend/bt_hogp.py`) presents
  the Deck as a "SteamDeckPad" gamepad over BlueZ (D-Bus GATT server +
  LE advertisement, static BLE address for stable identity). Windows 10+
  pairs with it natively; no bridge or ViGEmBus needed. Requires the
  `python-gobject` package (installed by the installer). While it is active
  the Deck's Bluetooth speaker (A2DP-sink) role is disabled
  (`backend/bt_audio.py`), so a host does not also pair it as audio; opt out
  with `~/usb-gamepad-bt-keep-audio`.
* **Transport priority**: USB wins. While a USB host has the gadget
  configured, the Bluetooth feed is paused (one neutral report is sent) so
  the same inputs are never delivered twice.

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
| `prebuilt/usb_gamepad-x86_64` | Precompiled launcher so Game Mode installs need no compiler. |
| `tests/test_report.py` | Unit tests (run: `python tests/test_report.py`). |
| `AGENTS.md` | Repo status, investigation history, gotchas — for humans and agents. |

## Installation

Full guide in [INSTALL.md](./INSTALL.md). Quick start:

1. Copy this folder to the Deck (the launcher ships **precompiled** — no
   `gcc`/`libx11` install needed; SteamOS already provides `libX11` and
   `python-gobject`).
2. Run `./install-usb-gamepad.sh`.
3. Add `/opt/usb-gamepad/usb_gamepad` to Steam as a **Non-Steam Game**.
4. Launch it from Game Mode while connected to the PC.

## Usage

1. Connect the Deck to the PC with a data-capable USB-C cable.
2. Launch the "USB Gamepad" game from your Steam library.
3. Pick a mode in the window (**XInput** on a Linux PC; **HID** for Windows,
   the default). On Windows run `windows/deck2xinput.exe` there for native
   XInput.
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
- **Windows can't see a native Xbox pad** — expected: Windows can't drive
  emulated Xbox pads through FunctionFS. Use **HID** mode, wait for
  `HID gamepad ready (/dev/hidg0)`, then start `deck2xinput.exe` on the PC.
- **XInput over Bluetooth does nothing (Linux host)** — expected: XInput is a
  USB-only gadget and the Bluetooth feed is a vendor-defined HID device with no
  Linux bridge. Use **USB + XInput** on Linux.
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
