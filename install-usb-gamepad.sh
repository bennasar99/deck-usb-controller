#!/bin/bash
# Install the USB gamepad forwarder so it can be launched from Game Mode as a
# non-Steam game. Run ONCE on the Deck as the deck user:
#
#   chmod +x install-usb-gamepad.sh
#   ./install-usb-gamepad.sh
#
# A stock SteamOS needs NO extra packages: the launcher is precompiled
# (prebuilt/usb_gamepad-x86_64) and SteamOS already ships libX11 and
# python-gobject. When the prebuilt launcher is absent the script falls back to
# compiling from source, which needs:
#   sudo steamos-readonly disable && sudo pacman -Sy base-devel libx11
#
# Then add /opt/usb-gamepad/usb_gamepad (the native launcher) to Steam:
#   Steam -> Add a Game -> Add a Non-Steam Game -> Browse -> /opt/usb-gamepad/usb_gamepad
set -e

APP=/opt/usb-gamepad
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "Installing USB gamepad app to $APP ..."
# python-gobject is needed for the Bluetooth (BLE HID-over-GATT) mode. SteamOS
# ships it; only reach for pacman when it is actually missing (that needs the
# read-only filesystem disabled).
if ! python3 -c "import gi" >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm python-gobject >/dev/null 2>&1 || \
        echo "WARNING: python-gobject missing and could not be installed; " \
             "Bluetooth mode disabled."
fi
sudo mkdir -p "$APP"
# Remove any previous payload first: an old backend must never be merged into
# (or shadowed next to) the current one -- stale modules are how a device can
# keep enumerating as plain HID instead of XInput after an upgrade.
sudo rm -rf "$APP/backend"
sudo cp -r "$SRC/backend" "$APP/backend"
sudo cp "$SRC/usb_gamepad.py" "$APP/usb_gamepad.py"
sudo cp "$SRC/usb_gamepad_launcher.c" "$APP/usb_gamepad_launcher.c"
sudo chmod +x "$APP/usb_gamepad.py"
echo "Installed backend modules:"
(sudo ls "$APP/backend") | sed 's/^/    /'

# Compile a native ELF launcher. Steam in Game Mode does not reliably run a
# bare shell script as a non-Steam game, but a real binary always works.
echo "Installing native launcher ..."
# Prefer a prebuilt launcher shipped with the bundle so the Deck needs no
# compiler or libx11 build files (its root filesystem is read-only by
# default). Compile from source only when no prebuilt binary is present.
PREBUILT=""
for cand in "$SRC/prebuilt/usb_gamepad-x86_64" "$SRC/usb_gamepad-x86_64"; do
    if [ -f "$cand" ]; then
        PREBUILT="$cand"
        break
    fi
done

if [ -n "$PREBUILT" ]; then
    sudo cp "$PREBUILT" "$APP/usb_gamepad"
    sudo chmod +x "$APP/usb_gamepad"
    echo "Installed prebuilt launcher ($PREBUILT); no compiler needed."
    LAUNCHER="$APP/usb_gamepad"
elif command -v gcc >/dev/null 2>&1; then
    if sudo gcc -O2 -DHAVE_X11 -o "$APP/usb_gamepad" "$APP/usb_gamepad_launcher.c" -l:libX11.so.6; then
        echo "Launcher built with an X11 window (Game Mode will show it)."
    else
        echo "WARNING: X11 runtime library not found; building a headless launcher."
        echo "Game Mode will keep the game running but may show the Steam loading screen."
        sudo gcc -O2 -o "$APP/usb_gamepad" "$APP/usb_gamepad_launcher.c"
    fi
    sudo chmod +x "$APP/usb_gamepad"
    LAUNCHER="$APP/usb_gamepad"
else
    echo "ERROR: no prebuilt launcher and gcc not found; a native launcher is required."
    echo "Install it with:"
    echo "  sudo steamos-readonly disable && sudo pacman -Sy base-devel python-gobject"
    exit 1
fi

# BlueZ (>=5.50) auto-creates a Device Information service whose PnP ID
# defaults to Linux Foundation 1D6B:0246 (version = BlueZ's own version).
# Windows reads that PnP ID, so a BLE host sees 1D6B:0246 instead of our
# 0079:0006 and the Windows bridge cannot match the Deck. Pin the platform
# DeviceID to our USB identity (source=usb, VID=0079, PID=0006, ver=0100).
# Best effort: on a fresh SteamOS image main.conf ships the key commented out.
BT_CONF=/etc/bluetooth/main.conf
if [ -f "$BT_CONF" ]; then
    echo "Pinning BlueZ DeviceID to usb:0079:0006:0100 (BLE PnP identity) ..."
    if grep -qE '^[[:space:]]*#?[[:space:]]*DeviceID[[:space:]]*=' "$BT_CONF"; then
        sudo sed -i -E 's|^[[:space:]]*#?[[:space:]]*DeviceID[[:space:]]*=.*|DeviceID = usb:0079:0006:0100|' "$BT_CONF"
    else
        sudo sed -i '/^\[General\]/a DeviceID = usb:0079:0006:0100' "$BT_CONF"
    fi
    sudo systemctl restart bluetooth 2>/dev/null || true
else
    echo "WARNING: $BT_CONF not found; Bluetooth PnP ID left at BlueZ default."
fi

# Run the forwarder as root in the background. Game Mode processes cannot get
# root (setuid/sudoers are not honoured inside the game session), so the launcher
# game only keeps a marker file fresh while a systemd service does the root work.
echo "Installing the root forwarder daemon (systemd service) ..."
printf '%s\n' \
    '[Unit]' \
    'Description=Steam Deck USB gamepad forwarder daemon' \
    'After=bluetooth.service multi-user.target' \
    'Wants=bluetooth.service' \
    'PartOf=bluetooth.service' \
    '' \
    '[Service]' \
    'Type=simple' \
    "ExecStart=/usr/bin/python3 $APP/usb_gamepad.py" \
    'Restart=on-failure' \
    'RestartSec=5' \
    '' \
    '[Install]' \
    'WantedBy=multi-user.target' \
    | sudo tee /etc/systemd/system/usb-gamepad.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable usb-gamepad
# enable --now is a no-op when the service already runs, so explicitly restart
# to make sure the fresh code (not the previously loaded modules) is live.
sudo systemctl restart usb-gamepad
echo "Daemon enabled and running. It stays idle until the game is launched."

echo
echo "Done. To finish:"
echo "  1. Open Steam on the Deck in Game Mode."
echo "  2. Steam -> Add a Game -> Add a Non-Steam Game."
echo "  3. Browse to $LAUNCHER and add it."
echo "  4. Launch it from your library; the Deck is now a USB gamepad."
echo "  5. Close the 'game' to stop and restore the Deck's normal USB port."
echo
echo "Note: disable the deck-usb-xinput-controller Decky plugin (and its"
echo "autostart) so both don't drive the same gadget at once."