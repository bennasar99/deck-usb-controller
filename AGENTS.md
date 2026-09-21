# AGENTS.md Ã¢â‚¬â€ deck-usb-xinput-controller

## What this project is

A Steam Deck (SteamOS) app that turns the Deck into a wired USB game
controller for a PC connected over USB-C. Launched from Game Mode as a
non-Steam game ("fake game" = `usb_gamepad`, compiled from
`usb_gamepad_launcher.c`), it forwards the Deck's controls to the host PC.
Closing the game restores normal USB.

Runtime topology (Game Mode processes cannot be root):

1. Launcher "game" (`deck` user): keeps Steam Input's virtual pad alive,
   shows an X11 window, refreshes marker file `/home/deck/usb-gamepad-active`
   every 100 ms. The window goes full black after 10 s without pointer/touch
   input (OLED burn-in protection; `g_dimmed`/`note_input_activity` in
   usb_gamepad_launcher.c) and wakes instantly on any Motion/ButtonPress/
   KeyPress X event Ã¢â‚¬â€ frames keep flowing while blacked out so gamescope
   keeps presenting it.
2. Root daemon (`/opt/usb-gamepad/usb_gamepad.py`, systemd unit
   `usb-gamepad.service`): watches the marker; when fresh it brings up the USB
   gadget, reads `/dev/input/event*` and forwards reports to the host.

Protocol selection: the launcher window has three clickable buttons
(Auto / XInput / HID) that write `/home/deck/usb-gamepad-mode`
("auto"|"xinput"|"hid"). The daemon polls that file and hot-restarts the
gadget on change. In "xinput" the auto XInputÃ¢â€ â€™HID watchdog switch is
disabled (one re-enumeration recovery, then stays). In "auto" the original
probe-then-switch behavior applies. The daemon reads the file each loop; the
launcher persists the click. NOTE: XButtonEvent x/y coordinates on LP64 are
at byte offsets 64/68 of the event (read via memcpy from the local XEvent
padding in usb_gamepad_launcher.c Ã¢â‚¬â€ ev_x/ev_y).

All logs go to `/home/deck/usb-gamepad.log` (mirrored in journald).

## Current protocol strategy (implemented, works)

Host capability is detected **behaviorally**, not by querying the host:

| Host | Protocol | Detail |
|------|----------|--------|
| Linux PCs | Native XInput Ã¢â‚¬â€ Xbox 360 pad (`045E:028E`) via FunctionFS (`f_fs`) | Kernel `xpad` binds and consumes instantly. |
| Windows PCs | Standard HID gamepad via kernel `f_hid` (`/dev/hidg0`, generic `0079:0006` "Valve / Steam Deck Gamepad" identity, 12-byte report: 15 buttons (incl. D-pad as button bits 11..14, vendor top-level usage 0xFF00) + 2 triggers + 2 sticks) | Switch happens automatically after ~16 s: probe 8 s Ã¢â€ â€™ one UDC re-enumeration Ã¢â€ â€™ switch. Windows cannot drive emulated Xbox pads through FunctionFS (see below); this exact HID identity/descriptor set is field-proven on Windows (verbatim from deck-usb-hid-controller). |
| raw_gadget XInput (`backend/xinput_raw.py`) | Byte-exact Xbox 360 emulation incl. vendor descriptors | **Complete but dormant**: never engages on current SteamOS because the kernel rejects raw_gadget on the physical UDC (see known-problem #1). Auto-activates if that ever changes. |

Key files:

- `usb_gamepad.py` Ã¢â‚¬â€ root daemon: mode watchdog, frame building/dispatch.
- `backend/gadget_manager.py` Ã¢â‚¬â€ gadget lifecycle for ffs + hid modes,
  self-healing configfs teardown (`_purge_tree`), single-function-config
  invariant (`_link_function_into_config`: remove only symlinks, NEVER the
  attribute files like MaxPower).
- `backend/xinput_ffs.py` Ã¢â‚¬â€ FunctionFS Xbox pad. Descriptor blob variants are
  tried in order (`fs-hs-ss` then plain `fs-hs`); this neptune kernel rejects
  `ss`. Self-heals stale ffs instances (EBUSY/EINVAL Ã¢â€ â€™ force reset + retry).
- `backend/xinput_raw.py` Ã¢â‚¬â€ raw_gadget backend (see status below).
- `backend/xinput_report.py` / `backend/gamepad_report.py` Ã¢â‚¬â€ wire formats.
- `backend/controller.py` Ã¢â‚¬â€ `build_xinput_frame` / `build_hid_frame`.
- `backend/evdev_reader.py` Ã¢â‚¬â€ dependency-free evdev reader.
- `backend/bt_hogp.py` - BLE HID-over-GATT gamepad (BlueZ D-Bus GATT server + LE advertisement, static BLE address via btmgmt). Opt-in via the launcher `Bluetooth: ON/OFF` toggle (`~/usb-gamepad-bt`); requires python-gobject. BT is independent of the USB modes and keeps forwarding when the game is closed. A background bond thread saves the paired PC (address + name) to `~/usb-gamepad-bt-bond`, marks it Trusted and auto-reconnects after reboots; the launcher `Unpair PC` button writes `~/usb-gamepad-bt-unpair`, which makes the daemon call `Adapter1.RemoveDevice` and clear the bond. BLE PnP ID MUST use source byte `0x02` (USB IF) so Windows exposes USB VID/PID `0079:0006` to the HID API (source `0x01` = Bluetooth SIG, which breaks the bridge's VID/PID match).
- `tests/test_report.py` Ã¢â‚¬â€ run with any Python 3: no pytest needed
  (`python tests/test_report.py`); currently 15 tests, all passing.

Deploy: copy repo to Deck, run `./install-usb-gamepad.sh` as `deck` user
(disables steamos-readonly, installs libx11/base-devel, builds launcher,
installs+restarts the service). Installer wipes `/opt/usb-gamepad/backend`
before copying (stale modules previously caused "still behaves like old code"
bugs).

## Known problem #1 Ã¢â‚¬â€ true XInput to Windows is NOT achievable today

Goal was native XInput on every OS. On Windows this fails at three levels;
all were verified empirically against a real Deck + Windows 11 host:

1. **FunctionFS cannot carry the vendor descriptors.** The Xbox 360 pad is
   vendor-class (FF/5D/01) and embeds an out-of-spec 17-byte class descriptor
   (`0x11 0x21 ...`) between interface and endpoints. The kernel's FFS parser
   whitelist-rejects unknown class descriptor types with EINVAL (verified by
   upload attempts). Windows' xusb22.sys binds the emulated device fine but
   **never issues a single IN transfer** without hardware-authentic descriptors
   Ã¢â‚¬â€ device shows "working properly", zero polls. Corroborated by
   CasperVM/360-raw-gadget README.
2. **MS OS descriptors can't rescue it.** Embedded MS-OS 2.0 ExtCompat
   descriptors (`XUSB10`) through the FFS blob are rejected EINVAL by this
   kernel's f_fs despite matching mainline byte layout (checked against
   linux/master f_fs.c: packed 11-byte `usb_os_desc_header`). configfs-side
   os_desc handshake also implemented once (worked around hex-vs-decimal
   attr bug), removed again after the blob rejection made it moot. Windows'
   Compatible IDs remained generic (`Class_FF...`), never `MS_COMP_XUSB10`.
3. **raw_gadget cannot attach to the physical UDC.** `USB_RAW_IOCTL_RUN` on
   `dwc3.1.auto` returns EBUSY with the UDC state literally `not attached`
   and zero holders (proven by /proc scan + standalone minimal probe running
   as root outside our stack, both speeds, anon and named driver_name, fresh
   fd per attempt Ã¢â‚¬â€ always EBUSY; the identical UDC binds via configfs
   seconds later). DECISIVE new evidence: while a raw_gadget registration is
   pending, an **empty configfs gadget's UDC bind also fails EBUSY** (the
   kernel's own composite driver is refused!) Ã¢â‚¬â€ and after the raw fd closes,
   the same bind succeeds. So raw_gadget's RUN leaks a pending registration
   that blocks ALL drivers on that UDC on this neptune build. This matches
   punktfunk (git.unom.io/unom/punktfunk, packaging/linux/steam-deck-gadget)
   whose glass-to-glass implementation only ever binds raw_gadget to a
   **dummy_hcd loopback UDC**, never the physical port. Conclusion:
   neptune-kernel-specific dwc3/raw_gadget incompatibility; raw backend is
   now OPT-IN (`/opt/usb-gamepad/try-raw` marker) and skipped by default so
   boots go straight to the working FunctionFS/HID stack.

If a future agent revisits #1: start by re-testing
`USB_RAW_IOCTL_INIT(dwc3.1.auto)+RUN` after a SteamOS kernel update
(`sudo touch /opt/usb-gamepad/try-raw`, restart service); if it
ever succeeds, `RawGadgetXInput` should work as-is (ep0 completion semantics,
VBUS_DRAW/CONFIGURE, one-outstanding-IN-transfer model are all implemented).
Also try filing with Valve referencing this evidence trail.

## Gotchas learned the hard way (do not regress)

- BLE PnP identity is owned by BlueZ, not the app. BlueZ >=5.50 auto-creates a
  Device Information service whose PnP ID defaults to Linux Foundation
  `1D6B:0246` (version = BlueZ's own, e.g. `rev&0553` = 5.53) and Windows reads
  *that* instead of the app's DIS/PnP, so the Windows bridge can never match
  `0079:0006` over Bluetooth. Fix: set `DeviceID = usb:0079:0006:0100` under
  `[General]` in `/etc/bluetooth/main.conf` (source `usb` => PnP source 0x02),
  restart `bluetooth`, then `usb-gamepad`. Windows caches the PnP ID, so
  remove+re-pair after changing it. (`DeviceID = false` also works but drops
  the whole DIS; the installer pins the DeviceID instead.)
- BLE HID over GATT: `HidD_GetProductString`/`GetManufacturerString` return
  EMPTY for BLE devices, so the Windows bridge can only match by the PnP ID's
  USB VID/PID. Also enumerate/query HID handles with access 0 (not
  `GENERIC_READ`) and open for I/O with `GENERIC_READ|GENERIC_WRITE` first --
  some BLE HID devices refuse a read-only open.
- HID mode identity: use the proven generic `0079:0006` ("Valve / Steam Deck
  Gamepad") from deck-usb-hid-controller, plus `protocol`/`subclass`=0 attrs
  and NO device-class attrs. An earlier revision used Valve `28DE:11FF` (the
  Steam Controller USB id) with a 4-bit hat declaring logical-max 7 while the
  report emitted value 8 on diagonals (out of range) Ã¢â‚¬â€ replace with the
  reference layout before suspecting anything else when HID "doesn't work".
- HID wire layout (v2): D-pad is BUTTON BITS 11..14 (UP=0x0800, DOWN=0x1000,
  LEFT=0x2000, RIGHT=0x4000), NOT a hat-switch usage byte. Triggers live at
  bytes 2..3 and sticks at 4..11; report is 12 bytes. Reason: the hat usage
  byte was heuristically re-interpreted by host-side HID stacks/games (user
  saw rightÃ¢â€ â€™down, downÃ¢â€ â€™right, leftÃ¢â€ â€™right rotations and inverted stick Y);
  button bits are unambiguous. Also: the top-level usage is vendor-defined
  (0xFF00) so the raw feed is invisible to games Ã¢â‚¬â€ only
  `windows/deck2xinput.exe` consumes it (matches by VID/PID). When changing
  the wire layout, update gamepad_report.py, controller.build_hid_frame,
  the daemon heartbeat decode, AND deck2xinput.cpp together.
- Windows HID reads prepend a report-ID byte (InputReportByteLength = 1 +
  report size); deck2xinput skips the first byte when got > report size.
- `HID_DEV` was silently shadowed by a stale `HID_DEV = FFS_DIR` alias line at
  the bottom of gadget_manager.py Ã¢â€ â€™ `os.open(EISDIR)` broke every HID switch
  while the gadget itself bound fine. Grepping for duplicate constant
  definitions catches this class of bug.
- configfs `configs/c.1/` contains attribute FILES (`MaxPower`,
  `bmAttributes`). Only unlink symlinks there; `rmdir`/`unlink` of attributes
  fails EPERM and previously aborted setup.
- Teardown order matters: unmount functionfs BEFORE removing
  `functions/ffs.xinput`, else EBUSY forever. `_purge_tree` retries with
  kernel-lag sleeps; mode switches additionally use a fresh `GadgetManager`
  instance + 1 s grace period.
- A failed `USB_RAW_IOCTL_RUN` advances the fd state: retrying RUN on the same
  fd yields EINVAL even after the real blocker clears. Always full
  openÃ¢â€ â€™INITÃ¢â€ â€™RUN cycle per attempt.
- No-data OUT control transfers on raw_gadget complete with a ZERO-LENGTH
  EP0_READ (status-stage IN token) Ã¢â‚¬â€ never EP0_WRITE (EBUSY/-110).
  VBUS_DRAW + CONFIGURE ioctls are required before endpoints enable on recent
  kernels.
- configfs integer attrs take decimal strings only ("220", not "0xdc") Ã¢â‚¬â€ hex
  is rejected or misparsed, silently breaking e.g. os_desc/b_vendor_code.
- FunctionFS strings blob (kernel expects): LE32 magic | LE32 len | LE32
  str_count | LE32 lang_count, then LE16 langid + NUL-terminated UTF-8
  strings. UTF-16 or length-prefixed payloads fail EINVAL (state
  FFS_READ_STRINGS, device never activates).
- EVIOCGID unpack order is bustype,vendor,product,version Ã¢â‚¬â€ not v,p,ver,bus.
- ep writes to ffs IN endpoints BLOCK until the host consumes; O_NONBLOCK
  does not prevent that wait. All ep1 writes live on a dedicated writer thread
  keeping exactly ONE transfer outstanding (latest-frame-wins queue). Without
  this the whole daemon freezes on first unconsumed write.
- Steam Input feeds its virtual pad ("Microsoft X-Box 360 pad 0",
  28de:11ff evdev) ONLY while the launched game has Game Mode focus. Testing
  from Desktop Mode/SSH yields zero input events; this masqueraded as a data-
  path failure repeatedly.
- Windows caches per-device-instance driver bindings: reused ports/identities
  poison future probes. Prefer different physical ports when testing protocol
  changes.
- The installer must explicitly `systemctl restart` (enable --now is a no-op
  on already-running services) and wipes the old backend dir first.

## Windows bridge (deck2xinput.exe)

Reads the 12-byte feed from ANY matching HID device - the wired USB gadget
(0079:0006) and the BLE "SteamDeckPad" (matched by VID/PID or product
string); one reader thread per device, latest report wins, ViGEm updates
serialized. Flags: -d debug, -i/--invert-y (default ON), --no-invert-y,
env DECK2XINPUT_INVERT_Y. When changing the wire layout, update
gamepad_report.py, controller.build_hid_frame, the daemon heartbeat decode,
AND deck2xinput.cpp together.

## Verification workflow

```bash
# Local (any OS, no deps):
python tests/test_report.py          # 15 tests must pass

# On Deck:
./install-usb-gamepad.sh
journalctl -u usb-gamepad -n 50 --no-pager     # unfiltered evidence FIRST
grep -E "Switching|HID gamepad ready|consumed|Status:" ~/usb-gamepad.log
```

Status heartbeat (`Status: input_events=N polls=M errors=K`) disambiguates:
input_events=0 Ã¢â€ â€™ input side (game focus/Steam Input); polls=0 with events>0 Ã¢â€ â€™
host not consuming (Windows Ã¢â€ â€™ expect switch); polls>0 Ã¢â€ â€™ working end-to-end.
The heartbeat also decodes the current frame (buttons/hat/sticks) Ã¢â‚¬â€ compare it
against `deck2xinput.exe -d` on the PC (Windows/windows folder) which prints
the raw HID report + decoded values, to localize direction/button bugs to the
Deck side vs the Windows side. Known issue under investigation (as of this
writing): user reports sticks + D-pad rotated 180Ã‚Â° on Windows in games; unit
tests and wire-format checks all pass, so capture both ends (deck heartbeat
frame vs `deck2xinput.exe -d` output) while pressing directions before
touching mapping code.

## Open ideas / future work

- Re-test raw_gadget-on-dwc3 after SteamOS updates; if functional, disable
  the HID fallback for hosts where XInput now consumes (one-line change in
  `usb_gamepad.py` watchdog).
- Optionally serve rumble feedback in HID mode (feature reports on ep0,
  see gamepad_report.py layout for an output-report slot).
- Gadget identity for HID mode is deliberately neutral 28de:11ff; could become
  configurable.
