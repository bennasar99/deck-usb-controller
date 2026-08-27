/*
 * deck2xinput - Steam Deck (USB HID gamepad) -> ViGEmBus XInput bridge.
 *
 * Pairs with the SteamOS side of deck-usb-xinput-controller: when the Deck
 * falls back to HID compatibility mode it presents a generic gamepad
 * (VID 0x0079, PID 0x0006, "Steam Deck Gamepad") whose 13-byte reports this
 * tool reads and replays into a ViGEmBus virtual Xbox 360 controller, so
 * Windows games see genuine XInput without any per-game wrappers.
 *
 * Layout of the 13-byte report (see backend/gamepad_report.py):
 *   byte 0..1  buttons, LE16: A B X Y LB RB BACK START GUIDE LS RS (bit0..10)
 *   byte  2    hat switch: 0=up .. 7=up-left, 0x0F centered (low nibble)
 *   byte  3    left trigger  (0..255)
 *   byte  4    right trigger (0..255)
 *   byte 5..12 sticks: LX, LY, RX, RY as LE int16 (-32768..32767)
 *
 * Requirements:
 *   - ViGEmBus driver installed (https://vigem.org → Downloads; v1.16+).
 *   - The Deck app running in HID compatibility mode (or the Deck plugged
 *     into any USB port in that mode).
 *
 * Build (MSVC):
 *   cmake -S . -B build -G "Visual Studio 17 2022"
 *   cmake --build build --config Release
 *   (ViGEmClient sources are vendored in ./ViGEmClient)
 *
 * Stop with Ctrl+C.
 */

#include <windows.h>
#include <hidsdi.h>
#include <hidpi.h>
#include <setupapi.h>
#include <initguid.h>
#include <stdio.h>
#include <string>
#include <vector>

#include <ViGEm/Client.h>

#pragma comment(lib, "setupapi.lib")
#pragma comment(lib, "hid.lib")

// Our gadget identity (backend/gadget_manager.py, HID mode).
#define DECK_VID 0x0079
#define DECK_PID 0x0006

// ---- report layout ---------------------------------------------------------

#define RBTN_A     0x0001
#define RBTN_B     0x0002
#define RBTN_X     0x0004
#define RBTN_Y     0x0008
#define RBTN_LB    0x0010
#define RBTN_RB    0x0020
#define RBTN_BACK  0x0040
#define RBTN_START 0x0080
#define RBTN_GUIDE 0x0100
#define RBTN_LS    0x0200
#define RBTN_RS    0x0400

#define XBTN_A     0x1000  // XINPUT_GAMEPAD_A
#define XBTN_B     0x2000
#define XBTN_X     0x4000
#define XBTN_Y     0x8000
#define XBTN_LB    0x0100
#define XBTN_RB    0x0200
#define XBTN_BACK  0x0020
#define XBTN_START 0x0010
#define XBTN_GUIDE 0x0400
#define XBTN_LS    0x0040
#define XBTN_RS    0x0080
// XINPUT_GAMEPAD_DPAD_* flags (per XInput spec — these were wrong before,
// which rotated the D-pad: right→down, down→left, left→right).
#define XBTN_UP    0x0001
#define XBTN_DOWN  0x0002
#define XBTN_LEFT  0x0004
#define XBTN_RIGHT 0x0008

#define REPORT_BUTTONS_LE16 0
#define REPORT_LT           2
#define REPORT_RT           3
#define REPORT_STICKS       4   // LX, LY, RX, RY as LE int16
#define REPORT_LENGTH       12

// D-pad is encoded as button bits (nothing can mis-interpret a bitfield,
// unlike hat-switch values which every host maps differently).
#define RBTN_DPAD_UP    0x0800
#define RBTN_DPAD_DOWN  0x1000
#define RBTN_DPAD_LEFT  0x2000
#define RBTN_DPAD_RIGHT 0x4000

static volatile LONG g_run = 1;
static int g_debug = 0;
// Default ON: the Deck's HID feed arrives with stick Y already matching the
// game convention users expect on Windows (vertical = screen direction).
static int g_invert_y = 1;

static int rd_i16(const unsigned char *p, int off) {
    return (int)(short)((unsigned short)p[off] |
                        ((unsigned short)p[off + 1] << 8));
}

// ---- HID device discovery --------------------------------------------------

struct HidDevice {
    std::wstring path;
    unsigned short vid;
    unsigned short pid;
};

static bool LooksLikeOurPad(HANDLE handle, HIDP_CAPS &caps) {
    HIDD_ATTRIBUTES attr;
    attr.Size = sizeof(attr);
    if (!HidD_GetAttributes(handle, &attr))
        return false;
    if (attr.VendorID != DECK_VID || attr.ProductID != DECK_PID)
        return false;

    // The gadget uses a VENDOR-DEFINED top-level usage (page 0xFF00) so
    // games never see the raw feed as a second gamepad — only this bridge
    // consumes it. Match on VID/PID + input report size.
    PHIDP_PREPARSED_DATA preparsed = nullptr;
    bool ok = false;
    if (HidD_GetPreparsedData(handle, &preparsed)) {
        HIDP_CAPS c{};
        if (HidP_GetCaps(preparsed, &c) == HIDP_STATUS_SUCCESS &&
            c.InputReportByteLength >= REPORT_LENGTH) {
            caps = c;
            ok = true;
        }
        HidD_FreePreparsedData(preparsed);
    }
    return ok;
}

static bool FindDeckPad(HidDevice &out) {
    GUID guid;
    HidD_GetHidGuid(&guid);
    HDEVINFO devs = SetupDiGetClassDevsW(&guid, nullptr, nullptr,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (devs == INVALID_HANDLE_VALUE)
        return false;

    bool found = false;
    SP_DEVICE_INTERFACE_DATA ifdata{};
    ifdata.cbSize = sizeof(ifdata);
    for (DWORD i = 0; SetupDiEnumDeviceInterfaces(devs, nullptr, &guid, i, &ifdata); ++i) {
        DWORD needed = 0;
        SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, nullptr, 0, &needed, nullptr);
        if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || needed == 0)
            continue;
        std::vector<unsigned char> buf(needed);
        auto *detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W *>(buf.data());
        detail->cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W);
        if (!SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, detail, needed, nullptr, nullptr))
            continue;

        HANDLE h = CreateFileW(detail->DevicePath, GENERIC_READ | GENERIC_WRITE,
                               FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_EXISTING,
                               FILE_FLAG_OVERLAPPED, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            // Keyboards/mouse collections often deny R/W; try read-only.
            h = CreateFileW(detail->DevicePath, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE,
                            nullptr, OPEN_EXISTING,
                            FILE_FLAG_OVERLAPPED, nullptr);
            if (h == INVALID_HANDLE_VALUE)
                continue;
        }
        HIDP_CAPS caps{};
        if (LooksLikeOurPad(h, caps)) {
            out.path = detail->DevicePath;
            out.vid = DECK_VID;
            out.pid = DECK_PID;
            found = true;
        }
        CloseHandle(h);
        if (found)
            break;
    }
    SetupDiDestroyDeviceInfoList(devs);
    return found;
}

// ---- ViGEm plumbing --------------------------------------------------------

static PVIGEM_CLIENT g_client = nullptr;
static PVIGEM_TARGET g_pad = nullptr;

static bool VigemStart(void) {
    g_client = vigem_alloc();
    VIGEM_ERROR err = vigem_connect(g_client);
    if (!VIGEM_SUCCESS(err)) {
        printf("[!] ViGEmBus connect failed: 0x%04X — is the ViGEmBus driver "
               "installed? (https://vigem.org)\n", err);
        return false;
    }
    g_pad = vigem_target_x360_alloc();
    err = vigem_target_add(g_client, g_pad);
    if (!VIGEM_SUCCESS(err)) {
        printf("[!] Failed to add virtual Xbox 360 pad: 0x%04X\n", err);
        return false;
    }
    ULONG index = vigem_target_get_index(g_pad);
    printf("[+] Virtual Xbox 360 controller #%lu created (ViGEmBus).\n", index);
    return true;
}

static void VigemStop(void) {
    if (g_pad) {
        vigem_target_remove(g_client, g_pad);
        vigem_target_free(g_pad);
        g_pad = nullptr;
    }
    if (g_client) {
        vigem_disconnect(g_client);
        vigem_free(g_client);
        g_client = nullptr;
    }
}

// ---- report -> XInput translation ------------------------------------------

static XUSB_REPORT Translate(const unsigned char *r) {
    XUSB_REPORT x{};
    const unsigned short buttons =
        (unsigned short)(r[REPORT_BUTTONS_LE16] |
                         (r[REPORT_BUTTONS_LE16 + 1] << 8));
    if (buttons & RBTN_A)     x.wButtons |= XBTN_A;
    if (buttons & RBTN_B)     x.wButtons |= XBTN_B;
    if (buttons & RBTN_X)     x.wButtons |= XBTN_X;
    if (buttons & RBTN_Y)     x.wButtons |= XBTN_Y;
    if (buttons & RBTN_LB)    x.wButtons |= XBTN_LB;
    if (buttons & RBTN_RB)    x.wButtons |= XBTN_RB;
    if (buttons & RBTN_BACK)  x.wButtons |= XBTN_BACK;
    if (buttons & RBTN_START) x.wButtons |= XBTN_START;
    if (buttons & RBTN_GUIDE) x.wButtons |= XBTN_GUIDE;
    if (buttons & RBTN_LS)    x.wButtons |= XBTN_LS;
    if (buttons & RBTN_RS)    x.wButtons |= XBTN_RS;
    if (buttons & RBTN_DPAD_UP)    x.wButtons |= XBTN_UP;
    if (buttons & RBTN_DPAD_DOWN)  x.wButtons |= XBTN_DOWN;
    if (buttons & RBTN_DPAD_LEFT)  x.wButtons |= XBTN_LEFT;
    if (buttons & RBTN_DPAD_RIGHT) x.wButtons |= XBTN_RIGHT;

    x.bLeftTrigger = r[REPORT_LT];
    x.bRightTrigger = r[REPORT_RT];
    x.sThumbLX = (SHORT)rd_i16(r, REPORT_STICKS);
    x.sThumbLY = (SHORT)rd_i16(r, REPORT_STICKS + 2);
    x.sThumbRX = (SHORT)rd_i16(r, REPORT_STICKS + 4);
    x.sThumbRY = (SHORT)rd_i16(r, REPORT_STICKS + 6);
    if (g_invert_y) {
        x.sThumbLY = (SHORT)(x.sThumbLY == -32768 ? 32767 : -x.sThumbLY);
        x.sThumbRY = (SHORT)(x.sThumbRY == -32768 ? 32767 : -x.sThumbRY);
    }
    return x;
}

// ---- device read loop ------------------------------------------------------

static std::string DpadName(unsigned short buttons) {
    const char *dirs[4] = {"UP", "DOWN", "LEFT", "RIGHT"};
    const unsigned short bits[4] = {RBTN_DPAD_UP, RBTN_DPAD_DOWN,
                                    RBTN_DPAD_LEFT, RBTN_DPAD_RIGHT};
    std::string s;
    for (int i = 0; i < 4; ++i)
        if (buttons & bits[i]) {
            if (!s.empty())
                s += "+";
            s += dirs[i];
        }
    return s.empty() ? "CENTER" : s;
}

static unsigned short buttons16(const unsigned char *r) {
    return (unsigned short)(r[0] | (r[1] << 8));
}

static BOOL WINAPI CtrlHandler(DWORD type) {
    (void)type;
    InterlockedExchange(&g_run, 0);
    return TRUE;
}

int main(int argc, char **argv) {
    SetConsoleCtrlHandler(CtrlHandler, TRUE);
    SetConsoleOutputCP(CP_UTF8);
    setvbuf(stdout, nullptr, _IONBF, 0);   // live status output
    for (int i = 1; i < argc; ++i) {
        const char *a = argv[i];
        if (strcmp(a, "-d") == 0 || strcmp(a, "--debug") == 0)
            g_debug = 1;
        else if (strcmp(a, "-i") == 0 || strcmp(a, "--invert-y") == 0)
            g_invert_y = 1;
        else if (strcmp(a, "--no-invert-y") == 0)
            g_invert_y = 0;
    }
    {
        char env[8];
        DWORD n = GetEnvironmentVariableA("DECK2XINPUT_INVERT_Y", env, 8);
        if (n > 0 && n < 8) {
            if (env[0] == '1' || env[0] == 'y' || env[0] == 'Y')
                g_invert_y = 1;
            else if (env[0] == '0' || env[0] == 'n' || env[0] == 'N')
                g_invert_y = 0;
        }
    }
    printf("deck2xinput - Steam Deck (HID 0079:0006) -> ViGEmBus XInput%s%s\n",
           g_debug ? " [debug]" : "",
           g_invert_y ? " [invert-y]" : "");

    if (!VigemStart()) {
        VigemStop();
        return 1;
    }

    while (InterlockedAdd(&g_run, 0)) {
        HidDevice dev;
        printf("[*] Waiting for the Deck gamepad (0079:0006)...\n");
        while (InterlockedAdd(&g_run, 0) && !FindDeckPad(dev))
            Sleep(1000);
        if (!InterlockedAdd(&g_run, 0))
            break;

        HANDLE h = CreateFileW(dev.path.c_str(), GENERIC_READ,
                               FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_EXISTING,
                               FILE_FLAG_OVERLAPPED, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            printf("[!] Opened failed on %ls (err %lu); retrying...\n",
                   dev.path.c_str(), GetLastError());
            Sleep(1000);
            continue;
        }

        PHIDP_PREPARSED_DATA preparsed = nullptr;
        HIDP_CAPS caps{};
        if (HidD_GetPreparsedData(h, &preparsed)) {
            HidP_GetCaps(preparsed, &caps);
            HidD_FreePreparsedData(preparsed);
        }
        DWORD buflen = caps.InputReportByteLength > REPORT_LENGTH
                           ? caps.InputReportByteLength : REPORT_LENGTH;
        printf("[+] Deck gamepad connected:\n    %ls\n"
               "    (input reports: %lu bytes)\n",
               dev.path.c_str(), caps.InputReportByteLength);

        OVERLAPPED ov{};
        ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::vector<unsigned char> buf(buflen, 0);
        DWORD report_frames = 0;

        while (InterlockedAdd(&g_run, 0)) {
            ResetEvent(ov.hEvent);
            BOOL ok = ReadFile(h, buf.data(), buflen, nullptr, &ov);
            if (!ok && GetLastError() != ERROR_IO_PENDING) {
                printf("[-] ReadFile failed (err %lu) — device gone?\n",
                       GetLastError());
                break;
            }
            DWORD got = 0;
            if (!GetOverlappedResult(h, &ov, &got, TRUE) || got == 0) {
                if (!InterlockedAdd(&g_run, 0))
                    break;
                printf("[-] Device read error — waiting for reconnect.\n");
                break;
            }
            if (got < REPORT_LENGTH)
                continue;

            // Windows HID always prepends a report-ID byte (0x00 when the
            // descriptor has none): the actual gamepad report trails it.
            const unsigned char *report = buf.data() + (got > REPORT_LENGTH ? 1 : 0);

            if (g_debug && (report_frames % 25) == 0) {
                XUSB_REPORT d = Translate(report);
                printf("raw: %02X %02X %02X %02X | L(%6d,%6d) R(%6d,%6d)"
                       " | dpad=%s A=%d B=%d X=%d Y=%d LT=%3u RT=%3u\n",
                       report[0], report[1], report[REPORT_LT],
                       report[REPORT_RT],
                       rd_i16(report, 4), rd_i16(report, 6),
                       rd_i16(report, 8), rd_i16(report, 10),
                       DpadName(buttons16(report)).c_str(),   // D-pad bits
                       (report[0] & RBTN_A) ? 1 : 0,
                       (report[0] & RBTN_B) ? 1 : 0,
                       (report[0] & RBTN_X) ? 1 : 0,
                       (report[0] & RBTN_Y) ? 1 : 0,
                       report[REPORT_LT], report[REPORT_RT]);
            }

            XUSB_REPORT x = Translate(report);
            VIGEM_ERROR err = vigem_target_x360_update(g_client, g_pad, x);
            if (!VIGEM_SUCCESS(err)) {
                printf("[!] XInput update failed: 0x%04X\n", err);
                break;
            }
            ++report_frames;
            if ((report_frames % 250) == 0)
                printf("[.] %lu reports forwarded...\n", report_frames);
        }

        CloseHandle(ov.hEvent);
        CancelIo(h);
        CloseHandle(h);
        if (InterlockedAdd(&g_run, 0)) {
            // Zero the pad so no button sticks down during reconnects.
            XUSB_REPORT zero{};
            vigem_target_x360_update(g_client, g_pad, zero);
            Sleep(1000);
        }
    }

    VigemStop();
    printf("bye\n");
    return 0;
}
