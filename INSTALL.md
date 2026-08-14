# Installing "Steam Deck -> USB Gamepad" on a Steam Deck

Turn any Steam Deck into a USB gamepad for a PC. While the app is running the
Deck's controls are forwarded over its USB-C port to the connected computer as
a generic HID gamepad, and Steam Input on the PC can read and remap them.

Everything happens in Game Mode: you launch a small "fake game" from your
library, it opens a window, and the Deck becomes a gamepad. Closing the "game"
returns the Deck to normal USB.

---

## How it works (short version)

Game Mode processes cannot run as root, so the app is split in two:

1. **The "game"** (`/opt/usb-gamepad/usb_gamepad`) runs as the `deck` user. It
   keeps Steam Input's virtual gamepad alive, shows a small X11 window, and
   keeps a marker file (`/home/deck/usb-gamepad-active`) fresh.
2. **A root daemon** (`usb-gamepad.service`) starts at boot. It watches the
   marker. When the game is running it sets up the USB HID gadget, reads the
   virtual gamepad and forwards reports to `/dev/hidg0`. When the game closes
   it tears the gadget down.

---

## Requirements

- A Steam Deck (LCD or OLED) and a PC with a USB-C or USB-A port.
- A USB-C cable that supports data (a plain charging cable is not enough).
- The `deck` user's password (default: `deck`).
- Internet access on the Deck (to install two packages).

---

## Step 0 — Get a terminal

Either work directly on the Deck, or from a computer over SSH.

### On the Deck (Desktop Mode)

1. Turn the Deck on and switch to **Desktop Mode** (Power -> Switch to Desktop).
2. Open **Konsole** (KDE terminal).

### From another computer (SSH)

1. On the Deck: Settings -> System -> **Enable Developer Mode** -> Advanced ->
   **Enable SSH**. Note the Deck's IP address.
2. From your computer:

   ```bash
   ssh deck@<deck-ip>
   ```

   Password is the `deck` user's password (default `deck`).

---

## Step 1 — Prepare the Deck (read-only filesystem + packages)

SteamOS protects its filesystem. Writing to it and installing packages requires
temporarily disabling read-only mode.

Run (enter the `deck` password when `sudo` asks):

```bash
sudo steamos-readonly disable
sudo pacman -Sy --noconfirm libx11 base-devel
```

- `base-devel` provides `gcc`, which compiles the native launcher.
- `libx11` provides the X11 window support so Game Mode shows the app window
  instead of the Steam loading screen.

You can re-enable read-only mode afterwards (see Step 3).

> **Note:** `sudo` will normally ask for a password — this is expected. The
> installer needs root to write to `/opt` and `/etc`.

---

## Step 2 — Copy the app files onto the Deck

The app lives in this folder. Put the whole project on the Deck, e.g.:

### Option A — from a computer

```bash
scp -r deck-usb-xinput-controller deck@<deck-ip>:~/
```

### Option B — on the Deck itself

Download the project archive in a browser (Desktop Mode) and extract it into
`~/deck-usb-xinput-controller`, or:

```bash
cd ~
git clone https://github.com/<your-repo>/deck-usb-xinput-controller
```

---

## Step 3 — Run the installer

```bash
cd ~/deck-usb-xinput-controller
chmod +x install-usb-gamepad.sh
./install-usb-gamepad.sh
```

The installer:

1. Copies the app to `/opt/usb-gamepad`.
2. Compiles the native launcher (`gcc -O2 -DHAVE_X11 ... -l:libX11.so.6`).
3. Installs and starts the root daemon as a systemd service
   (`usb-gamepad.service`, enabled at boot).

You should see:

```
Launcher built with an X11 window (Game Mode will show it).
...
Daemon enabled and running. It stays idle until the game is launched.
```

If you re-enable read-only mode, do it now (the daemon only reads from
`/opt` and writes to `/sys`, which is always writable):

```bash
sudo steamos-readonly enable
```

---

## Step 4 — Verify the daemon

```bash
systemctl status usb-gamepad
```

Should show `active (running)`. Its startup message lands in
`/home/deck/usb-gamepad.log`:

```bash
cat /home/deck/usb-gamepad.log
```

```
[hh:mm:ss] USB gamepad daemon started; waiting for the 'USB Gamepad' game.
```

---

## Step 5 — Add it to Steam

1. Put the Deck back into **Game Mode**.
2. Open Steam -> **Library**.
3. **Add a Game** (top-left) -> **Add a Non-Steam Game**.
4. **Browse...** and select:

   ```
   /opt/usb-gamepad/usb_gamepad
   ```

5. Add it. It appears in your library as "usb_gamepad". Rename it to
   "USB Gamepad" if you like (right-click -> Properties).

---

## Step 6 — Use it

1. Connect the Deck to the PC with a data-capable USB-C cable.
2. In Game Mode, launch the "USB Gamepad" game.
3. A small window titled **USB Gamepad** shows "USB Gamepad Active". The PC
   should now see a device named **Steam Deck Gamepad**.
4. Play! The Deck's sticks, triggers, buttons, D-pad and trackpads (if mapped)
   are forwarded to the PC as a gamepad.
5. To stop: close the "game" (Steam button -> the game's X, or the gamepad
   shortcut). The gadget is torn down and the Deck returns to normal USB.

---

## Logs & troubleshooting

All messages (launcher + daemon) are appended to:

```
/home/deck/usb-gamepad.log
```

The daemon's own output is also in the systemd journal:

```bash
journalctl -u usb-gamepad -f
```

| Symptom | Fix |
| --- | --- |
| Steam loading screen stays forever | `libx11` was not installed before running the installer, so a headless launcher was built. Install `libx11` (Step 1) and re-run the installer (Step 3). |
| Log shows `No UDC present` / `No UDC became available` | The Deck's USB-C port is not in device (DRD) mode. Reboot into BIOS (**Volume Up + Power**), go to Advanced -> USB Configuration, set **USB Dual Role Device** to **DRD**, save and reboot. |
| Log shows `ERROR: USB HID gadget setup failed: Permission denied ...` | The daemon is not running as root. Check `systemctl status usb-gamepad`. |
| PC sees the gamepad but no input arrives | The log must show `Reading Microsoft X-Box 360 pad 0 (...)`. If it shows `Reading Sony ...` or a built-in pad, Steam Input is not active — make sure the game launched from Game Mode is in focus, and that the Deck's controller layout is set to "Gamepad". |
| Nothing happens / no `Reading ...` line | Check the log for the latest `No gamepad found`/`Refresh` lines. Sometimes Steam takes a few seconds to expose its virtual pad. |
| Both the Decky plugin and this app run | Disable the `deck-usb-xinput-controller` Decky plugin (and its autostart) — both drive the same gadget and will fight. |
| Daemon died | `sudo systemctl restart usb-gamepad`. The service has `Restart=on-failure`. |

---

## Updating

Copy the new files to the Deck and re-run the installer:

```bash
cd ~/deck-usb-xinput-controller
./install-usb-gamepad.sh
```

The installer overwrites `/opt/usb-gamepad` and reinstalls the service.

---

## Uninstalling

```bash
sudo systemctl disable --now usb-gamepad
sudo rm -f /etc/systemd/system/usb-gamepad.service
sudo systemctl daemon-reload
sudo rm -rf /opt/usb-gamepad
```

Then remove the "USB Gamepad" entry from Steam, and delete the
`~/deck-usb-xinput-controller` folder on the Deck.

Optionally remove leftover rules from earlier versions (no longer used):

```bash
sudo rm -f /etc/sudoers.d/usb-gamepad
```

---

## Safety notes

- While the "game" is running, the Deck's USB-C port acts as a USB *device*.
  Docking/charging a connected hub behaves differently during that time.
- The Deck stays in gadget mode until you close the game (and briefly after,
  while the daemon tears the gadget down).
- The app writes reports only while the game window is open and focused.