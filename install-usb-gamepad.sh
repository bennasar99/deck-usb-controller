#!/bin/bash
# Installs the standalone USB gamepad forwarder so it can be launched from
# Game Mode as a non-Steam game. Run ONCE on the Deck as the deck user:
#
#   chmod +x install-usb-gamepad.sh
#   ./install-usb-gamepad.sh
#
# Then add /opt/usb-gamepad/usb_gamepad (the native launcher) to Steam:
#   Steam -> Add a Game -> Add a Non-Steam Game -> Browse -> /opt/usb-gamepad/usb_gamepad
set -e

APP=/opt/usb-gamepad
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "Installing USB gamepad app to $APP ..."
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
echo "Building native launcher ..."
if command -v gcc >/dev/null 2>&1; then
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
    echo "ERROR: gcc not found; a native launcher is required."
    echo "Install it with:"
    echo "  sudo steamos-readonly disable && sudo pacman -Sy base-devel"
    exit 1
fi

# Run the forwarder as root in the background. Game Mode processes cannot get
# root (setuid/sudoers are not honoured inside the game session), so the launcher
# game only keeps a marker file fresh while a systemd service does the root work.
echo "Installing the root forwarder daemon (systemd service) ..."
printf '%s\n' \
    '[Unit]' \
    'Description=Steam Deck USB gamepad forwarder daemon' \
    'After=multi-user.target' \
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