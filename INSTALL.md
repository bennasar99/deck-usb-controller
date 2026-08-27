# Installing "Steam Deck → USB Controller" on a Steam Deck

Turn any Steam Deck into a wired game controller for a PC. While the app is
running the Deck's controls are forwarded over its USB-C port to the connected
computer — as a **native Xbox 360 (XInput) controller** where the host accepts
it, and (after an automatic per-host probe) as a **standard USB HID gamepad**
elsewhere (the universally supported path, used on Windows). You can also pin
the protocol manually in the app window. Everything happens in Game Mode: you
launch a small "fake game" from your library, it opens a window, and the Deck
becomes a controller. Closing the "game" returns the Deck to normal USB.

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
| **Auto** (default) | Xbox 360 XInput pad first; if the host never consumes reports, one automatic re-enumeration, then a switch to a standard HID gamepad. |
| **XInput** | Forces the Xbox 360 pad. Native XInput on Linux hosts. |
| **HID** | Forces a standard USB HID gamepad — the reliable path on Windows; pair with the Windows bridge (see below) for native XInput. |

---

## Requirements

- A Steam Deck (LCD or OLED) and a PC with a USB-C or USB-A port.
- A USB-C cable that supports data (a plain charging cable is not enough).
- The `deck` user's password (default: `deck`).
- Internet access on the Deck (to install two packages).
- BIOS: **USB Dual Role Device = DRD** (Setup Utility → Advanced → USB
  Configuration; enter with Volume Up + Power).

---

## Step 0 — Get a terminal

Either work directly on the Deck (Desktop Mode → Konsole) or over SSH
(Settings → System → Developer Mode → Enable SSH, then `ssh deck@<deck-ip>`).

---

## Step 1 — Prepare the Deck (read-only filesystem + packages)

```bash
sudo steamos-readonly disable
sudo pacman -Sy --noconfirm libx11 base-devel
```

- `base-devel` provides `gcc`, which compiles the native launcher.
- `libx11` provides the X11 window support so Game Mode shows the app window
  (with the mode buttons) instead of a headless build.

You can re-enable read-only mode afterwards (Step 3).

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
2. Compiles the native launcher (`gcc -O2 -DHAVE_X11 ... -l:libX11.so.6`).
3. Installs and **restarts** the root daemon as a systemd service
   (`usb-gamepad.service`, enabled at boot).

You should see:

```
Launcher built with an X11 window (Game Mode will show it).
Daemon enabled and running. It stays idle until the game is launched.
```

If you instead see `WARNING: X11 runtime library not found; building a
headless launcher`, `libx11` was missing — install it (Step 1) and re-run.

You can re-enable read-only mode now:

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
`USB XInput controller daemon started ... (mode=auto)`.

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
   status and the three mode buttons (**Auto / XInput / HID**).
3. Press buttons on the Deck — they should appear on the PC.

What to expect per host:

| Host | Result |
|------|--------|
| Linux PC | Native **Xbox 360 (XInput)** controller immediately. |
| Windows PC | First enumerates as an Xbox 360 pad; after the ~15 s probe the app switches to a standard HID gamepad (`Switching to HID compatibility mode` in the log, then `HID gamepad ready`). |
| Windows PC + bridge | For native XInput on Windows, run the bridge app below; keep the mode on **HID**. |

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
| `input_events` climbs, `polls=0` (Windows) | Host isn't consuming XInput — expected on Windows; wait for the automatic switch to HID, or select **HID** mode manually. |
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
