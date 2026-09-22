# deck2xinput — Steam Deck HID → XInput (ViGEmBus)

Windows companion app for **deck-usb-xinput-controller**. It reads the Deck's
12-byte gamepad HID reports and replays them into a **virtual Xbox 360
controller** through the [ViGEmBus](https://vigem.org) driver — so Windows
games see genuine XInput without per-game wrappers.

It automatically consumes the Deck's feed from **either transport**:

- **Wired (USB-C)**: the gadget `0079:0006` "Steam Deck Gamepad" (HID mode;
  vendor-defined usage, invisible to games).
- **Wireless (Bluetooth)**: the BLE HID device **"SteamDeckPad"** (paired
  from the PC's Bluetooth settings; same 0079:0006 PnP identity).

Both can be attached at once — the most recent report wins.

## Prerequisites

1. **ViGEmBus driver** installed on the PC — grab the latest installer from
   <https://vigem.org> (Downloads → ViGEmBus Setup). Version 1.16 or newer.
2. The Deck app deployed on the Steam Deck, set to **HID** mode (the default),
   and the Deck connected by USB-C (or paired over Bluetooth). This tool can
   also be started *before* the Deck is plugged in — it waits for the device.

## Build

Requires Visual Studio 2022 (Desktop development with C++) or any MSVC
toolchain. ViGEmClient SDK sources are vendored under `ViGEmClient/`.

```bat
cd windows
cmake -S . -B build -G "Visual Studio 18 2026" -A x64
cmake --build build --config Release
```

Output: `build\Release\deck2xinput.exe`

Notes:
- On machines with Visual Studio 2022 (MSVC v143) use
  `-G "Visual Studio 17 2022"` instead — pick the generator that matches your
  installed toolchain, or CMake fails with `MSB8020: build tools ... cannot
  be found`.
- MinGW works too: `cmake -S . -B build -G "MinGW Makefiles" && cmake --build build`.

## Run

```bat
deck2xinput.exe
```

Options:

| Flag | Effect |
|------|--------|
| `-d`, `--debug` | Print raw report bytes + decoded D-pad/stick values every 25 reports |
| `-i`, `--invert-y` | Invert both stick Y axes — some games/visualizers expect the opposite Y convention; use if the camera moves the wrong way vertically |

`DECK2XINPUT_INVERT_Y=1` (environment variable) also enables the invert.

Expected console flow:

```
[*] Waiting for the Deck gamepad (0079:0006)...
[+] Feed connected: \\?\hid#vid_0079&pid_0006#... (hid(0079:0006))
[+] Virtual Xbox 360 controller #1 created (ViGEmBus).
[.] 500 reports forwarded...
```

Keep it running while you play. On the PC, games see an **Xbox 360
Controller** (native XInput, glyph-complete). Ctrl+C exits cleanly and
releases the virtual pad.

The virtual controller is created **lazily** when a Deck feed connects and
removed ~4 s after it disconnects, so an idle bridge leaves no ghost
controller. Only one instance runs at a time (a second launch exits with a
message), and the same virtual controller is reused across quick reconnects.

Run it automatically at login (optional):

```bat
schtasks /create /tn "Deck2XInput" /tr "<path>\deck2xinput.exe" /sc onlogon /rl highest
```

## Mapping

| Deck control | Virtual XInput |
|---|---|
| A B X Y | A B X Y |
| LB / RB | LB / RB |
| Back / Start / Guide | Back / Start / Guide |
| L3 / R3 | LS / RS |
| D-pad (hat 0..7) | D-pad |
| Triggers (0..255) | Left / right trigger |
| Sticks (int16) | Left / right stick |

Deadzone handling, glyphs and any per-game remapping stay in Steam Input /
the game — this app feeds 1:1 raw values.

## Troubleshooting

- **"ViGEmBus connect failed"** — the ViGEmBus driver is not installed (or is
  the pre-1.16 legacy build). Install/upgrade from vigem.org and reboot once.
- **Stuck on "Waiting for the Deck gamepad"** — the Deck app must be in HID
  mode (or Bluetooth toggled ON and paired). Check `~/usb-gamepad.log` on the
  Deck for `HID gamepad ready (/dev/hidg0)`; verify the device under Device
  Manager identifies as `0079:0006` or "SteamDeckPad".
- **Inputs land in joy.cpl but not the virtual pad** — another feeder (e.g.
  a leftover x360ce instance) may hold the device; close it.
