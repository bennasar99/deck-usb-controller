# Installing "Steam Deck → USB Controller" on a Steam Deck

Turn any Steam Deck into a wired game controller for a PC. While the app is
running the Deck's controls are forwarded over its USB-C port to the connected
computer as either a **standard USB HID gamepad** (the default; pair it with
the Windows bridge on a PC for native XInput) or a **native Xbox 360 (XInput)
controller** (for Linux hosts). You pick the protocol in the app window.
Everything happens in Game Mode: you launch a small "fake game" from your
library, it opens a window, and the Deck becomes a controller. Closing the
"game" returns the Deck to normal USB.

---

## How it works (short version)

Game Mode processes cannot run as root, so the app is split in two:

1. **The "game"** (`/opt/usb-gamepad/usb_gamepad`) runs as the `deck` user. It
   shows the app window (mode buttons + status), keeps Steam Input's virtual
   gamepad alive, and keeps a marker file (`/home/deck/usb-gamepad-active`)
   fresh.
2. **A root daemon** (`usb-gamepad.service`) starts at boot. It watches the
   marker. When the game is running it brings up the selected gadget, reads
   the virtual gamepad and forwards reports to the PC. When the game closes it
   tears the gadget down.

### Modes (selected by clicking in the app window; remembered in `~/usb-gamepad-mode`)

| Mode | Behavior |
|------|----------|
| **HID** (default) | Standard USB HID gamepad — the reliable path on Windows; pair with the Windows bridge (see below) for native XInput. |
| **XInput** | Xbox 360 pad (FunctionFS). Native XInput on Linux hosts. |
| **Bluetooth** (toggle) | Advertises the Deck as a BLE HID gamepad "SteamDeckPad". Pair from the PC's Bluetooth settings; Windows maps it natively (Steam Input refines it). Works alongside any USB mode. The paired PC is remembered and reconnects after reboots; **Unpair PC** forgets it. |

---

## Requirements

- A Steam Deck (LCD or OLED) and a PC with a USB-C or USB-A port.
- A USB-C cable that supports data (a plain charging cable is not enough).
- The `deck` user's password (default: `deck`).
- Internet access on the Deck only if the prebuilt launcher is missing (then
  `gcc`/`libx11` must be installed to compile from source).
- BIOS: **USB Dual Role Device = DRD** (Setup Utility → Advanced → USB
  Configuration; enter with Volume Up + Power).

---

## Step 0 — Get a terminal

Either work directly on the Deck (Desktop Mode → Konsole) or over SSH
(Settings → System → Developer Mode → Enable SSH, then `ssh deck@<deck-ip>`).

---

## Step 1 — (Optional) Prepare the Deck to build from source

A stock SteamOS needs **no preparation**: the launcher ships precompiled
(`prebuilt/usb_gamepad-x86_64`) and SteamOS already provides `libX11` and
`python-gobject`. Only if that prebuilt binary is missing does the installer
compile from source, which requires:

```bash
sudo steamos-readonly disable
sudo pacman -Sy --noconfirm libx11 base-devel
```

- `base-devel` provides `gcc`, which compiles the native launcher.
- `libx11` provides the X11 window support so Game Mode shows the app window
  (with the mode buttons) instead of a headless build.

Re-enable read-only mode afterwards if you disabled it:
`sudo steamos-readonly enable`.

---

## Step 2 — Copy the app files onto the Deck

From a computer:

```bash
scp -r deck-usb-xinput-controller deck@<deck-ip>:~/
```

or `git clone` it on the Deck into `~/deck-usb-xinput-controller`.

---

## Step 3 — Run the installer

```bash
cd ~/deck-usb-xinput-controller
chmod +x install-usb-gamepad.sh
./install-usb-gamepad.sh
```

The installer:

1. Copies the app to `/opt/usb-gamepad` (wiping the previous backend first).
2. Installs the precompiled launcher (`prebuilt/usb_gamepad-x86_64`); if it is
   absent it compiles `usb_gamepad_launcher.c` with gcc instead.
3. Installs and **restarts** the root daemon as a systemd service
   (`usb-gamepad.service`, enabled at boot).

You should see:

```
Installed prebuilt launcher (.../prebuilt/usb_gamepad-x86_64); no compiler needed.
Daemon enabled and running. It stays idle until the game is launched.
```

(When compiling from source instead, the line reads `Launcher built with an
X11 window (Game Mode will show it).`; a `WARNING: X11 runtime library not
found` there means `libx11` was missing — install it via Step 1 and re-run.)

If you disabled read-only mode, re-enable it now:

```bash
sudo steamos-readonly enable
```

---

## Step 4 — Verify the daemon

```bash
systemctl status usb-gamepad
tail ~/usb-gamepad.log
```

Should show `active (running)` and
`USB XInput controller daemon started ... (mode=hid)`.

---

## Step 5 — Add it to Steam

1. Put the Deck back into **Game Mode**.
2. Steam → Library → **Add a Game** → **Add a Non-Steam Game** → Browse →
   `/opt/usb-gamepad/usb_gamepad`. Add it (rename to "USB Gamepad" if you
   like).

---

## Step 6 — Use it

1. Connect the Deck to the PC with a data-capable USB-C cable.
2. Launch the "USB Gamepad" game from your library. The app window shows the
   status and two mode buttons (**XInput / HID**).
3. Press buttons on the Deck — they should appear on the PC.

What to expect per mode:

| Mode | Result |
|------|--------|
| **HID** (default) | Standard USB HID gamepad. On Windows run the bridge app below for native XInput; on Linux it is a raw HID device. |
| **XInput** | Native **Xbox 360 (XInput)** controller — works directly on Linux hosts; Windows cannot consume it through FunctionFS, so use HID there. |

---

## Step 7 (optional) — Native XInput on Windows via the bridge

On the Windows PC:

1. Install the **ViGEmBus** driver from <https://vigem.org>.
2. Build the bridge (once): install Visual Studio 2022/2026 (C++ tools), then
   in the repo's `windows/` folder:
   ```bat
   cmake -S . -B build -G "Visual Studio 18 2026" -A x64
   cmake --build build --config Release
   ```
   (On VS2022 installs use `-G "Visual Studio 17 2022"`.)
3. Run `build\Release\deck2xinput.exe` while the Deck game is running.
   - `-d` prints raw reports + decoded values (debugging).
   - `-i` / `--invert-y` flips stick Y for games that expect the opposite
     convention; `--no-invert-y` forces the standard one.
4. The bridge creates a virtual **Xbox 360 controller** and replays the
   Deck's inputs — games see genuine XInput.

Keep the app mode on **HID** for this; the bridge consumes the HID feed
directly (the gadget is deliberately invisible to games).

### Alternative: Bluetooth (no cable, no bridge)

> **Experimental.** Bluetooth pairing can be unreliable — a host may need to be
> removed and re-paired, some hosts cache the device identity, and the link is
> less reliable than USB. The Deck also exposes its own Bluetooth **audio
> (speaker)** profile, so a host may detect/connect it as a speaker alongside
> "SteamDeckPad"; remove that audio device on the host.

Toggle **Bluetooth: ON** in the app window, then on the PC:
Settings → Bluetooth → connect to **SteamDeckPad** (confirm the pairing pin
on both devices). Windows 10+ recognizes it as a BLE HID gamepad natively —
no ViGEmBus or bridge needed. First-time pairing may require a mapping pass
in the game's/Steam's controller settings.

The paired PC is saved (`~/usb-gamepad-bt-bond`), marked Trusted, and
reconnects automatically after reboots (no re-pairing). To forget it, click
**Unpair PC** in the app window — the daemon removes the bond from BlueZ.

While the Bluetooth gamepad is active the Deck's own Bluetooth **speaker**
(A2DP-sink) role is disabled, so a host does not also pair it as an audio
device. This is a user-level WirePlumber override and is restored when you
turn Bluetooth off; opt out with `~/usb-gamepad-bt-keep-audio`.

USB takes priority over Bluetooth: if a USB host is connected while Bluetooth
is on, the Bluetooth feed is paused so inputs are not sent twice.

---

## Logs & troubleshooting

All messages (launcher + daemon) are appended to
`/home/deck/usb-gamepad.log`; the daemon's output is also in the journal:

```bash
journalctl -u usb-gamepad -n 50 --no-pager
```

The periodic `Status:` line decodes the live state —
`input_events=N` counts Deck inputs, `polls=M` counts reports consumed by the
host. Triage:

| Symptom | Fix |
| --- | --- |
| `input_events=0` while pressing buttons | Steam Input only feeds its virtual pad while the "USB Gamepad" game has Game Mode focus. Stay focused on the game while testing. |
| `input_events` climbs, `polls=0` | Host isn't consuming the current mode — on Windows select **HID** and run the bridge; on Linux select **XInput**. |
| `No UDC` / `state=not attached` errors | USB-C port not in DRD mode — fix in BIOS (see Requirements). |
| Steam loading screen stays forever | `libx11` was missing during install (headless launcher). Install it and re-run the installer. |
| Mode buttons don't respond | Headless launcher (see above), or taps not reaching the window — re-run the installer with `libx11` installed. |
| Daemon died / weird double starts | `sudo systemctl restart usb-gamepad`; a singleton lock prevents two instances. |
| Sticks/D-pad inverted in-game only on Windows | Ensure `deck2xinput.exe` runs (mode **HID**); try `-i` / `--invert-y` if the game expects the opposite Y convention. |

---

## Updating

Copy the new files to the Deck and re-run the installer:

```bash
cd ~/deck-usb-xinput-controller
./install-usb-gamepad.sh
```

The installer wipes `/opt/usb-gamepad/backend` (so stale modules can never
persist), recompiles the launcher, and explicitly restarts the service.

---

## Uninstalling

```bash
sudo systemctl disable --now usb-gamepad
sudo rm -f /etc/systemd/system/usb-gamepad.service
sudo systemctl daemon-reload
sudo rm -rf /opt/usb-gamepad
```

Then remove the "USB Gamepad" entry from Steam and delete
`~/deck-usb-xinput-controller` on the Deck. If the Windows bridge was
installed as a scheduled task: `schtasks /delete /tn "Deck2XInput" /f`.

---

## Safety notes

- While the "game" is running, the Deck's USB-C port acts as a USB *device*;
  docks/hubs behave differently during that time.
- The app window blanks (OLED protection) after 10 s without input; forwarding
  continues regardless.
- The app writes reports only while the game window is open and focused.
