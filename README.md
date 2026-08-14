# Deck USB Gamepad Controller

Turn any **Steam Deck** into a **wired USB game controller** for a PC connected
over USB-C. Launch the app from Game Mode as a non-Steam game: a small window
opens, and the Deck's sticks, triggers, buttons and D-pad are forwarded over the
USB link as a **generic USB HID gamepad**. The PC sees a plain controller that
Steam Input (and standard OS gamepad drivers) can read and remap — no extra
drivers, and no `raw_gadget` kernel module.

> Requires the Deck's USB-C port to be in **DRD (Dual Role Device)** mode in the
> BIOS. See [INSTALL.md](./INSTALL.md).

## How it works

Game Mode processes cannot run as root, so the app is split in two:

1. **The "game"** (`/opt/usb-gamepad/usb_gamepad`, compiled from
   `usb_gamepad_launcher.c`) runs as the `deck` user. It is the running app that
   keeps Steam Input's virtual gamepad alive, shows a small X11 window, and
   keeps a marker file (`/home/deck/usb-gamepad-active`) fresh while it runs.
2. **A root daemon** (`usb_gamepad.py`, a systemd service enabled at boot)
   watches the marker. When the game is running it sets up the USB HID gadget,
   reads the virtual gamepad and forwards 13-byte HID reports to `/dev/hidg0`.
   When the game closes it tears the gadget down and the Deck returns to normal
   USB.

```
+-------------------------+        +--------------------------+
| Steam Deck (Game Mode)  |  USB-C | PC                        |
|                         |  DRD   |                           |
|  usb_gamepad (window +  |        |  Standard HID gamepad     |
|  marker file)           |        |  driver + Steam Input     |
|    │ marker fresh       |        |  ▲                        |
|    ▼                    |        |  │ HID reports            |
|  usb_gamepad.py daemon  ├────────┤  │ (13 bytes each)        |
|    │ (root, systemd)    │ /dev/  │  │                        |
|    │ reads evdev        │ hidg0  │  │                        |
|    ▼                    │        │  │                        |
|  USB HID gadget         │        │  │                        |
|  (configfs, dwc3 DRD)   ─────────┘                          |
+-------------------------+        +--------------------------+
```

- The gadget setup finds the AMD DWC3 USB controller, re-binds it from the host
  (`xhci_hcd`) driver to the gadget (`dwc3-pci`) driver and builds a configfs
  USB gadget with a single HID function exposing the Deck as a generic gamepad
  (16 buttons, hat switch, 2 triggers, 2 sticks).
- The daemon reads the chosen input device (`/dev/input/event*`) with a small
  dependency-free evdev reader, rebuilds the 13-byte HID report on every state
  change and writes it to `/dev/hidg0`.

## Files

| Path                  | Role                                                            |
|-----------------------|-----------------------------------------------------------------|
| `usb_gamepad_launcher.c` | Native launcher "game": X11 window + marker file, no root.  |
| `usb_gamepad.py`      | Root forwarder daemon (systemd service) for the HID gadget.     |
| `install-usb-gamepad.sh` | Installs to `/opt/usb-gamepad`, compiles the launcher, installs the service. |
| `backend/controller.py` | State, input→HID-report forwarding logic.                     |
| `backend/gadget_manager.py` | DRD switch + configfs USB HID gadget management.         |
| `backend/evdev_reader.py` | Pure-Python evdev device reading and discovery.             |
| `backend/gamepad_report.py` | Standard HID gamepad descriptor + 13-byte report builder. |
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
3. The PC sees a **Steam Deck Gamepad**. Play!
4. Close the "game" to stop and restore the Deck's normal USB port.

## Logs & troubleshooting

All messages land in `/home/deck/usb-gamepad.log`; the daemon's output is also
in the journal (`journalctl -u usb-gamepad -f`). Common issues:

- **Steam loading screen stays** — `libx11` was missing when the installer ran
  (headless build). Install `libx11` and re-run the installer.
- **`No UDC`** — the USB-C port is not in DRD mode. Set **USB Dual Role Device**
  to **DRD** in the BIOS (Volume Up + Power).
- **No input on the PC** — check the log shows `Reading Microsoft X-Box 360 pad
  0 (...)`, and disable the `deck-usb-xinput-controller` **Decky plugin** if it
  is installed (both would drive the same gadget).

See [INSTALL.md](./INSTALL.md) for the full troubleshooting table.

## License

[MIT](./LICENSE)
