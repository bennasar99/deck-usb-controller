# AGENTS.md — deck-usb-xinput-controller

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
   KeyPress X event — frames keep flowing while blacked out so gamescope
   keeps presenting it.
2. Root daemon (`/opt/usb-gamepad/usb_gamepad.py`, systemd unit
   `usb-gamepad.service`): watches the marker; when fresh it brings up the USB
   gadget, reads `/dev/input/event*` and forwards reports to the host.

All logs go to `/home/deck/usb-gamepad.log` (mirrored in journald).

## Current protocol strategy (implemented, works)

Host capability is detected **behaviorally**, not by querying the host:

| Host | Protocol | Detail |
|------|----------|--------|
| Linux PCs | Native XInput — Xbox 360 pad (`045E:028E`) via FunctionFS (`f_fs`) | Kernel `xpad` binds and consumes instantly. |
| Windows PCs | Standard HID gamepad via kernel `f_hid` (`/dev/hidg0`, generic `0079:0006` "Valve / Steam Deck Gamepad" identity, 13-byte report: 16 buttons + 8-bit hat + 2 triggers + 2 sticks) | Switch happens automatically after ~16 s: probe 8 s → one UDC re-enumeration → switch. Windows cannot drive emulated Xbox pads through FunctionFS (see below); this exact HID identity/descriptor set is field-proven on Windows (verbatim from deck-usb-hid-controller). |
| raw_gadget XInput (`backend/xinput_raw.py`) | Byte-exact Xbox 360 emulation incl. vendor descriptors | **Complete but dormant**: never engages on current SteamOS because the kernel rejects raw_gadget on the physical UDC (see known-problem #1). Auto-activates if that ever changes. |

Key files:

- `usb_gamepad.py` — root daemon: mode watchdog, frame building/dispatch.
- `backend/gadget_manager.py` — gadget lifecycle for ffs + hid modes,
  self-healing configfs teardown (`_purge_tree`), single-function-config
  invariant (`_link_function_into_config`: remove only symlinks, NEVER the
  attribute files like MaxPower).
- `backend/xinput_ffs.py` — FunctionFS Xbox pad. Descriptor blob variants are
  tried in order (`fs-hs-ss` then plain `fs-hs`); this neptune kernel rejects
  `ss`. Self-heals stale ffs instances (EBUSY/EINVAL → force reset + retry).
- `backend/xinput_raw.py` — raw_gadget backend (see status below).
- `backend/xinput_report.py` / `backend/gamepad_report.py` — wire formats.
- `backend/controller.py` — `build_xinput_frame` / `build_hid_frame`.
- `backend/evdev_reader.py` — dependency-free evdev reader.
- `tests/test_report.py` — run with any Python 3: no pytest needed
  (`python tests/test_report.py`); currently 15 tests, all passing.

Deploy: copy repo to Deck, run `./install-usb-gamepad.sh` as `deck` user
(disables steamos-readonly, installs libx11/base-devel, builds launcher,
installs+restarts the service). Installer wipes `/opt/usb-gamepad/backend`
before copying (stale modules previously caused "still behaves like old code"
bugs).

## Known problem #1 — true XInput to Windows is NOT achievable today

Goal was native XInput on every OS. On Windows this fails at three levels;
all were verified empirically against a real Deck + Windows 11 host:

1. **FunctionFS cannot carry the vendor descriptors.** The Xbox 360 pad is
   vendor-class (FF/5D/01) and embeds an out-of-spec 17-byte class descriptor
   (`0x11 0x21 ...`) between interface and endpoints. The kernel's FFS parser
   whitelist-rejects unknown class descriptor types with EINVAL (verified by
   upload attempts). Windows' xusb22.sys binds the emulated device fine but
   **never issues a single IN transfer** without hardware-authentic descriptors
   — device shows "working properly", zero polls. Corroborated by
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
   and zero holders (proven by /proc scan + standalone minimal C-less probe
   running as root outside our stack, both speeds, anon and named driver_name,
   fresh fd per attempt — always EBUSY; the identical UDC binds via configfs
   seconds later). Everything else about our raw backend is correct and
   validated against punktfunk's glass-to-glass implementation (git.unom.io/
   unom/punktfunk, packaging/linux/steam-deck-gadget) which, tellingly, only
   ever binds raw_gadget to a **dummy_hcd loopback UDC**, never to the Deck's
   physical port. Conclusion: neptune-kernel-specific dwc3/raw_gadget issue;
   blocked until Valve aligns with mainline behavior.

If a future agent revisits #1: start by re-testing
`USB_RAW_IOCTL_INIT(dwc3.1.auto)+RUN` after a SteamOS kernel update; if it
ever succeeds, `RawGadgetXInput` should work as-is (ep0 completion semantics,
VBUS_DRAW/CONFIGURE, one-outstanding-IN-transfer model are all implemented).

## Gotchas learned the hard way (do not regress)

- HID mode identity: use the proven generic `0079:0006` ("Valve / Steam Deck
  Gamepad") from deck-usb-hid-controller, plus `protocol`/`subclass`=0 attrs
  and NO device-class attrs. An earlier revision used Valve `28DE:11FF` (the
  Steam Controller USB id) with a 4-bit hat declaring logical-max 7 while the
  report emitted value 8 on diagonals (out of range) — replace with the
  reference layout before suspecting anything else when HID "doesn't work".
- `HID_DEV` was silently shadowed by a stale `HID_DEV = FFS_DIR` alias line at
  the bottom of gadget_manager.py → `os.open(EISDIR)` broke every HID switch
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
  open→INIT→RUN cycle per attempt.
- No-data OUT control transfers on raw_gadget complete with a ZERO-LENGTH
  EP0_READ (status-stage IN token) — never EP0_WRITE (EBUSY/-110).
  VBUS_DRAW + CONFIGURE ioctls are required before endpoints enable on recent
  kernels.
- configfs integer attrs take decimal strings only ("220", not "0xdc") — hex
  is rejected or misparsed, silently breaking e.g. os_desc/b_vendor_code.
- FunctionFS strings blob (kernel expects): LE32 magic | LE32 len | LE32
  str_count | LE32 lang_count, then LE16 langid + NUL-terminated UTF-8
  strings. UTF-16 or length-prefixed payloads fail EINVAL (state
  FFS_READ_STRINGS, device never activates).
- EVIOCGID unpack order is bustype,vendor,product,version — not v,p,ver,bus.
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
input_events=0 → input side (game focus/Steam Input); polls=0 with events>0 →
host not consuming (Windows → expect switch); polls>0 → working end-to-end.

## Open ideas / future work

- Re-test raw_gadget-on-dwc3 after SteamOS updates; if functional, disable
  the HID fallback for hosts where XInput now consumes (one-line change in
  `usb_gamepad.py` watchdog).
- Optionally serve rumble feedback in HID mode (feature reports on ep0,
  see gamepad_report.py layout for an output-report slot).
- Gadget identity for HID mode is deliberately neutral 28de:11ff; could become
  configurable.
