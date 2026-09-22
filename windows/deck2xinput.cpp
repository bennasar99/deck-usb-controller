/*
 * deck2xinput - Steam Deck -> ViGEmBus XInput bridge (Windows side).
 *
 * Consumes the Deck's raw HID feed (12-byte gamepad reports) over BOTH
 * transports and replays it into a ViGEmBus virtual Xbox 360 controller,
 * so Windows games see genuine XInput:
 *
 *   - Wired:  USB gadget `0079:0006` "Steam Deck Gamepad" (vendor-defined
 *             top-level usage, invisible to games).
 *   - Wirele: BLE HID device "SteamDeckPad" (paired over Bluetooth; also
 *             0079:0006 via its PnP ID).
 *
 * Multiple matching devices can be attached at once; every report read
 * updates the same virtual pad (ViGEm updates are serialized).
 *
 * Report layout (12 bytes; identical on both transports):
 *   byte 0..1  buttons LE16: A B X Y LB RB BACK START GUIDE L3 R3
 *              + D-pad bits 11..14 (UP=0x0800 DOWN=0x1000 LEFT=0x2000 RIGHT=0x4000)
 *   byte 2..3  left / right trigger (0..255)
 *   byte 4..11 sticks: LX, LY, RX, RY as LE int16
 *
 * Windows HID reads prepend a report-ID byte on devices without report IDs
 * (buffer = 1 + report size) — the first byte is skipped when present.
 *
 * Usage: deck2xinput.exe [-d|--debug] [-i|--invert-y] [--no-invert-y]
 * Environment: DECK2XINPUT_INVERT_Y=1|0
 *
 * Requirements: ViGEmBus driver (https://vigem.org). Stop with Ctrl+C.
 */

#include <windows.h>
#include <hidsdi.h>
#include <hidpi.h>
#include <setupapi.h>
#include <initguid.h>
#include <stdio.h>
#include <cstring>
#include <string>
#include <vector>
#include <thread>
#include <mutex>

#include <ViGEm/Client.h>

#pragma comment(lib, "setupapi.lib")
#pragma comment(lib, "hid.lib")

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
#define RBTN_DPAD_UP    0x0800
#define RBTN_DPAD_DOWN  0x1000
#define RBTN_DPAD_LEFT  0x2000
#define RBTN_DPAD_RIGHT 0x4000

#define XBTN_A     0x1000
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
// XINPUT_GAMEPAD_DPAD_* flags (XInput spec).
#define XBTN_UP    0x0001
#define XBTN_DOWN  0x0002
#define XBTN_LEFT  0x0004
#define XBTN_RIGHT 0x0008

#define REPORT_LENGTH 12

static volatile LONG g_run = 1;
static int g_debug = 0;
static int g_list = 0;
static int g_invert_y = 1;   // default ON; --no-invert-y / env opt-out

static int rd_i16(const unsigned char *p, int off) {
    return (int)(short)((unsigned short)p[off] |
                        ((unsigned short)p[off + 1] << 8));
}

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

// ---- ViGEm plumbing --------------------------------------------------------

static PVIGEM_CLIENT g_client = nullptr;
static PVIGEM_TARGET g_pad = nullptr;
static CRITICAL_SECTION g_feed_cs;      // ViGEmClient is not thread-safe
static volatile LONG g_forwarded = 0;
static DWORD g_pad_idle_since = 0;      // tick when the last feed ended
#define PAD_GRACE_MS 4000               // keep the pad across brief reconnects

static bool VigemConnect(void) {
    g_client = vigem_alloc();
    VIGEM_ERROR err = vigem_connect(g_client);
    if (!VIGEM_SUCCESS(err)) {
        printf("[!] ViGEmBus connect failed: 0x%04X - is the ViGEmBus driver "
               "installed? (https://vigem.org)\n", err);
        return false;
    }
    return true;
}

// The virtual pad is created lazily on the first Deck feed and removed again
// once no feed is connected (after a short grace period), so an idle bridge
// leaves no ghost controller on the host. It is reused for reconnects that
// happen within the grace period, so the host does not see a new controller
// on every reconnect.
static bool EnsurePad(void) {
    bool ok = true;
    EnterCriticalSection(&g_feed_cs);
    if (!g_pad) {
        g_pad = vigem_target_x360_alloc();
        VIGEM_ERROR err = vigem_target_add(g_client, g_pad);
        if (!VIGEM_SUCCESS(err)) {
            printf("[!] Failed to add virtual Xbox 360 pad: 0x%04X\n", err);
            vigem_target_free(g_pad);
            g_pad = nullptr;
            ok = false;
        } else {
            printf("[+] Virtual Xbox 360 controller #%lu created (ViGEmBus).\n",
                   vigem_target_get_index(g_pad));
        }
    }
    LeaveCriticalSection(&g_feed_cs);
    return ok;
}

static void RemovePad(void) {
    EnterCriticalSection(&g_feed_cs);
    if (g_pad) {
        XUSB_REPORT zero{};
        vigem_target_x360_update(g_client, g_pad, zero);
        vigem_target_remove(g_client, g_pad);
        vigem_target_free(g_pad);
        g_pad = nullptr;
        printf("[-] Virtual Xbox 360 controller removed (no feed).\n");
    }
    LeaveCriticalSection(&g_feed_cs);
}

static void VigemShutdown(void) {
    RemovePad();
    if (g_client) {
        vigem_disconnect(g_client);
        vigem_free(g_client);
        g_client = nullptr;
    }
}

static void FeedReport(const unsigned char *report) {
    XUSB_REPORT x{};

    const unsigned short buttons = buttons16(report);
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

    x.bLeftTrigger = report[2];
    x.bRightTrigger = report[3];
    x.sThumbLX = (SHORT)rd_i16(report, 4);
    x.sThumbLY = (SHORT)rd_i16(report, 6);
    x.sThumbRX = (SHORT)rd_i16(report, 8);
    x.sThumbRY = (SHORT)rd_i16(report, 10);
    if (g_invert_y) {
        x.sThumbLY = (SHORT)(x.sThumbLY == -32768 ? 32767 : -x.sThumbLY);
        x.sThumbRY = (SHORT)(x.sThumbRY == -32768 ? 32767 : -x.sThumbRY);
    }

    EnterCriticalSection(&g_feed_cs);
    if (!g_pad) {
        // Pad was just torn down (all feeds gone); drop the late report.
        LeaveCriticalSection(&g_feed_cs);
        return;
    }
    VIGEM_ERROR err = vigem_target_x360_update(g_client, g_pad, x);
    LeaveCriticalSection(&g_feed_cs);
    if (!VIGEM_SUCCESS(err)) {
        printf("[!] XInput update failed: 0x%04X\n", err);
        return;
    }
    LONG n = InterlockedIncrement(&g_forwarded);
    if (n == 1)
        printf("[+] First report forwarded to the virtual pad - "
               "games now see an Xbox 360 controller.\n");
    else if ((n % 500) == 0)
        printf("[.] %ld reports forwarded...\n", n);
}

// ---- HID device discovery --------------------------------------------------

struct HidDevice {
    std::wstring path;
    std::string  tag;
};

static bool contains_ci(const std::wstring &hay, const wchar_t *needle) {
    std::wstring h = hay, n = needle;
    for (auto &c : h) c = towlower(c);
    for (auto &c : n) c = towlower(c);
    return h.find(n) != std::wstring::npos;
}

static bool IsOurFeed(HANDLE handle, HIDP_CAPS &caps, const char **kind) {
    HIDD_ATTRIBUTES attr;
    attr.Size = sizeof(attr);
    if (!HidD_GetAttributes(handle, &attr))
        return false;
    if (attr.VendorID == DECK_VID && attr.ProductID == DECK_PID) {
        // USB gadget (or BLE device exposing the same PnP ID).
        *kind = "hid(0079:0006)";
        PHIDP_PREPARSED_DATA pp = nullptr;
        bool ok = false;
        if (HidD_GetPreparsedData(handle, &pp)) {
            HIDP_CAPS c{};
            if (HidP_GetCaps(pp, &c) == HIDP_STATUS_SUCCESS &&
                c.InputReportByteLength >= REPORT_LENGTH) {
                caps = c;
                ok = true;
            }
            HidD_FreePreparsedData(pp);
        }
        return ok;
    }
    // Fallback for devices that do expose a product string. NOTE: BLE HID
    // devices do NOT -- Windows returns an empty product string for them, so
    // Bluetooth matching relies entirely on the PnP ID's USB VID/PID above
    // (the reason that ID must use Vendor ID Source 0x02).
    wchar_t product[126];
    if (HidD_GetProductString(handle, product, sizeof(product)) &&
        contains_ci(product, L"SteamDeckPad")) {
        *kind = "ble(SteamDeckPad)";
        PHIDP_PREPARSED_DATA pp = nullptr;
        bool ok = false;
        if (HidD_GetPreparsedData(handle, &pp)) {
            HIDP_CAPS c{};
            if (HidP_GetCaps(pp, &c) == HIDP_STATUS_SUCCESS &&
                c.InputReportByteLength >= REPORT_LENGTH) {
                caps = c;
                ok = true;
            }
            HidD_FreePreparsedData(pp);
        }
        return ok;
    }
    return false;
}

static size_t FindDeckFeeds(std::vector<HidDevice> &out) {
    GUID guid;
    HidD_GetHidGuid(&guid);
    HDEVINFO devs = SetupDiGetClassDevsW(&guid, nullptr, nullptr,
                                         DIGCF_PRESENT |
                                         DIGCF_DEVICEINTERFACE);
    if (devs == INVALID_HANDLE_VALUE)
        return 0;

    SP_DEVICE_INTERFACE_DATA ifdata{};
    ifdata.cbSize = sizeof(ifdata);
    for (DWORD i = 0;
         SetupDiEnumDeviceInterfaces(devs, nullptr, &guid, i, &ifdata); ++i) {
        DWORD needed = 0;
        SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, nullptr, 0,
                                         &needed, nullptr);
        if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || needed == 0)
            continue;
        std::vector<unsigned char> buf(needed);
        auto *detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W *>(
            buf.data());
        detail->cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W);
        if (!SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, detail, needed,
                                              nullptr, nullptr))
            continue;

        // Query-only handle (access 0), like hidapi: BLE HID devices often
        // reject a GENERIC_READ open, and attributes/caps need no access.
        HANDLE h = CreateFileW(detail->DevicePath, 0,
                               FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_EXISTING, 0, nullptr);
        if (h == INVALID_HANDLE_VALUE)
            continue;
        HIDP_CAPS caps{};
        const char *kind = nullptr;
        if (IsOurFeed(h, caps, &kind))
            out.push_back({detail->DevicePath, std::string(kind)});
        CloseHandle(h);
        if (out.size() >= 4)
            break;
    }
    SetupDiDestroyDeviceInfoList(devs);
    return out.size();
}

static void ListHidDevices(void) {
    GUID guid;
    HidD_GetHidGuid(&guid);
    HDEVINFO devs = SetupDiGetClassDevsW(&guid, nullptr, nullptr,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (devs == INVALID_HANDLE_VALUE) {
        printf("[!] SetupDiGetClassDevs failed (%lu)\n", GetLastError());
        return;
    }
    SP_DEVICE_INTERFACE_DATA ifdata{};
    ifdata.cbSize = sizeof(ifdata);
    printf("HID devices (VID/PID/product/inlen):\n");
    for (DWORD i = 0;
         SetupDiEnumDeviceInterfaces(devs, nullptr, &guid, i, &ifdata); ++i) {
        DWORD needed = 0;
        SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, nullptr, 0,
                                         &needed, nullptr);
        if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || needed == 0)
            continue;
        std::vector<unsigned char> buf(needed);
        auto *detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W *>(
            buf.data());
        detail->cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W);
        if (!SetupDiGetDeviceInterfaceDetailW(devs, &ifdata, detail, needed,
                                              nullptr, nullptr))
            continue;
        HANDLE h = CreateFileW(detail->DevicePath, 0,
                               FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_EXISTING, 0, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            printf("  (cannot open) %ls\n", detail->DevicePath);
            continue;
        }
        HIDD_ATTRIBUTES attr;
        attr.Size = sizeof(attr);
        BOOL haveAttr = HidD_GetAttributes(h, &attr);
        wchar_t product[126] = L"";
        wchar_t manuf[126] = L"";
        HidD_GetProductString(h, product, sizeof(product));
        HidD_GetManufacturerString(h, manuf, sizeof(manuf));
        HIDP_CAPS caps{};
        PHIDP_PREPARSED_DATA pp = nullptr;
        if (HidD_GetPreparsedData(h, &pp)) {
            HidP_GetCaps(pp, &caps);
            HidD_FreePreparsedData(pp);
        }
        HIDP_CAPS mcaps{};
        const char *kind = nullptr;
        const bool match = IsOurFeed(h, mcaps, &kind);
        const char *openmode = "rw";
        HANDLE hr = CreateFileW(detail->DevicePath, GENERIC_READ | GENERIC_WRITE,
                                FILE_SHARE_READ | FILE_SHARE_WRITE,
                                nullptr, OPEN_EXISTING, 0, nullptr);
        if (hr == INVALID_HANDLE_VALUE) {
            openmode = "ro";
            hr = CreateFileW(detail->DevicePath, GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE,
                             nullptr, OPEN_EXISTING, 0, nullptr);
        }
        if (hr == INVALID_HANDLE_VALUE)
            openmode = "no";
        else
            CloseHandle(hr);
        printf("  VID=%04X PID=%04X usagePage=%04X inlen=%u open=%s match=%s\n",
               haveAttr ? attr.VendorID : 0,
               haveAttr ? attr.ProductID : 0,
               (unsigned)caps.UsagePage,
               (unsigned)caps.InputReportByteLength,
               openmode, match ? kind : "-");
        printf("    product=\"%ls\" manufacturer=\"%ls\"\n", product, manuf);
        printf("    %ls\n", detail->DevicePath);
        CloseHandle(h);
    }
    SetupDiDestroyDeviceInfoList(devs);
}

// ---- per-device reader threads ---------------------------------------------

struct DeviceSession {
    std::wstring path;
    std::string  tag;
    HANDLE       h = INVALID_HANDLE_VALUE;
    HANDLE       stopEvent = nullptr;
    std::thread  th;
    bool         dead = false;
};

static CRITICAL_SECTION g_sessions_cs;
static std::vector<DeviceSession *> g_sessions;

static DWORD WINAPI ReaderThread(LPVOID param);

static void StartSession(const HidDevice &dev) {
    auto *s = new DeviceSession();
    s->path = dev.path;
    s->tag = dev.tag;
    s->stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);

    // BLE HID devices may require write access to open for I/O; try
    // read+write first, then fall back to read-only.
    HANDLE h = CreateFileW(dev.path.c_str(), GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING,
                           FILE_FLAG_OVERLAPPED, nullptr);
    if (h == INVALID_HANDLE_VALUE)
        h = CreateFileW(dev.path.c_str(), GENERIC_READ,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        nullptr, OPEN_EXISTING,
                        FILE_FLAG_OVERLAPPED, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        printf("[!] Cannot open %ls (err %lu)\n",
               dev.path.c_str(), GetLastError());
        CloseHandle(s->stopEvent);
        delete s;
        return;
    }
    s->h = h;

    if (!EnsurePad()) {
        printf("[!] Not starting feed %ls: no virtual pad.\n",
               dev.path.c_str());
        CloseHandle(s->stopEvent);
        CloseHandle(s->h);
        delete s;
        return;
    }
    g_pad_idle_since = 0;

    PHIDP_PREPARSED_DATA pp = nullptr;
    HIDP_CAPS caps{};
    if (HidD_GetPreparsedData(h, &pp)) {
        HidP_GetCaps(pp, &caps);
        HidD_FreePreparsedData(pp);
    }

    EnterCriticalSection(&g_sessions_cs);
    s->th = std::thread(ReaderThread, (void *)s);
    g_sessions.push_back(s);
    LeaveCriticalSection(&g_sessions_cs);
    printf("[+] Feed connected: %ls (%s)\n", dev.path.c_str(), s->tag.c_str());
}

static void ShutdownSession(DeviceSession *s) {
    SetEvent(s->stopEvent);
    CancelIoEx(s->h, nullptr);
    if (s->th.joinable())
        s->th.join();
    if (s->h != INVALID_HANDLE_VALUE)
        CloseHandle(s->h);
}

static DWORD WINAPI ReaderThread(LPVOID param) {
    auto *s = (DeviceSession *)param;
    HANDLE handles[2] = {s->stopEvent, INVALID_HANDLE_VALUE};

    PHIDP_PREPARSED_DATA pp = nullptr;
    HIDP_CAPS caps{};
    if (HidD_GetPreparsedData(s->h, &pp)) {
        HidP_GetCaps(pp, &caps);
        HidD_FreePreparsedData(pp);
    }
    DWORD buflen = caps.InputReportByteLength > REPORT_LENGTH
                       ? caps.InputReportByteLength : REPORT_LENGTH;
    std::vector<unsigned char> buf(buflen, 0);
    OVERLAPPED ov{};
    ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    handles[1] = ov.hEvent;

    for (;;) {
        if (WaitForSingleObject(s->stopEvent, 0) == WAIT_OBJECT_0)
            break;
        ResetEvent(ov.hEvent);
        BOOL ok = ReadFile(s->h, buf.data(), buflen, nullptr, &ov);
        if (!ok && GetLastError() != ERROR_IO_PENDING)
            break;
        HANDLE waitHandles[2] = {ov.hEvent, s->stopEvent};
        DWORD w = WaitForMultipleObjects(2, waitHandles, FALSE, INFINITE);
        if (w == WAIT_OBJECT_0 + 1)
            break;                       // device session shutting down
        DWORD got = 0;
        if (!GetOverlappedResult(s->h, &ov, &got, FALSE) || got == 0)
            break;
        if (got < REPORT_LENGTH)
            continue;
        // Windows prepends a report-ID byte when the collection has none.
        const unsigned char *report =
            buf.data() + (got > REPORT_LENGTH ? 1 : 0);
        FeedReport(report);
    }
    CancelIo(s->h);
    CloseHandle(ov.hEvent);
    s->dead = true;
    return 0;
}

// ---- main ------------------------------------------------------------------

static BOOL WINAPI CtrlHandler(DWORD type) {
    (void)type;
    InterlockedExchange(&g_run, 0);
    return TRUE;
}

int main(int argc, char **argv) {
    SetConsoleCtrlHandler(CtrlHandler, TRUE);
    SetConsoleOutputCP(CP_UTF8);
    setvbuf(stdout, nullptr, _IONBF, 0);
    for (int i = 1; i < argc; ++i) {
        const char *a = argv[i];
        if (strcmp(a, "-d") == 0 || strcmp(a, "--debug") == 0)
            g_debug = 1;
        else if (strcmp(a, "-l") == 0 || strcmp(a, "--list") == 0)
            g_list = 1;
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
    printf("deck2xinput - Steam Deck (HID 0079:0006 wired/BT) -> "
           "ViGEmBus XInput%s%s\n",
           g_debug ? " [debug]" : "",
           g_invert_y ? " [invert-y]" : "");

    if (g_list) {
        ListHidDevices();
        return 0;
    }

    // Single instance only: a second bridge would add a second virtual
    // controller (a ghost) for the same Deck feed.
    HANDLE singleton = CreateMutexW(nullptr, TRUE, L"deck2xinput_singleton");
    if (singleton != nullptr && GetLastError() == ERROR_ALREADY_EXISTS) {
        printf("[!] deck2xinput is already running; exiting so no second "
               "virtual controller is created.\n");
        CloseHandle(singleton);
        return 0;
    }

    InitializeCriticalSection(&g_feed_cs);
    InitializeCriticalSection(&g_sessions_cs);

    if (!VigemConnect()) {
        VigemShutdown();
        DeleteCriticalSection(&g_feed_cs);
        DeleteCriticalSection(&g_sessions_cs);
        return 1;
    }

    LONG last_forwarded = 0;
    DWORD last_sweep = 0;
    while (InterlockedAdd(&g_run, 0)) {
        // Re-scan for feeds every 2 s; start readers for new devices and
        // reap sessions whose reader thread has exited.
        DWORD nowTick = GetTickCount();
        if (nowTick - last_sweep >= 2000 || last_sweep == 0) {
            last_sweep = nowTick;
            std::vector<HidDevice> found;
            FindDeckFeeds(found);
            for (auto &dev : found) {
                bool known = false;
                EnterCriticalSection(&g_sessions_cs);
                for (auto *s : g_sessions)
                    if (!s->dead && s->path == dev.path)
                        known = true;
                LeaveCriticalSection(&g_sessions_cs);
                if (!known)
                    StartSession(dev);
            }
            EnterCriticalSection(&g_sessions_cs);
            for (int i = (int)g_sessions.size() - 1; i >= 0; --i) {
                auto *s = g_sessions[i];
                if (s->dead) {
                    ShutdownSession(s);
                    delete s;
                    g_sessions.erase(g_sessions.begin() + i);
                    printf("[-] Feed disconnected.\n");
                }
            }
            LeaveCriticalSection(&g_sessions_cs);
        }

        // Tear the virtual pad down once no feed is connected (after a grace
        // period), so an idle bridge leaves no ghost controller behind.
        bool have_feed;
        EnterCriticalSection(&g_sessions_cs);
        have_feed = !g_sessions.empty();
        LeaveCriticalSection(&g_sessions_cs);
        if (have_feed) {
            g_pad_idle_since = 0;
        } else if (g_pad && g_pad_idle_since == 0) {
            g_pad_idle_since = nowTick;
        } else if (g_pad && nowTick - g_pad_idle_since >= PAD_GRACE_MS) {
            RemovePad();
            g_pad_idle_since = 0;
        }

        // Report stale delivery only while the Deck is actively used — the
        // daemon can't know; the bridge simply notes forward counters.
        if ((LONG)InterlockedAdd(&g_forwarded, 0) != last_forwarded) {
            last_forwarded = InterlockedAdd(&g_forwarded, 0);
        }

        // Reap dead sessions periodically (handled in sweep above as well).
        Sleep(250);
        if (!InterlockedAdd(&g_run, 0))
            break;
    }

    EnterCriticalSection(&g_sessions_cs);
    for (auto *s : g_sessions) {
        SetEvent(s->stopEvent);
        CancelIoEx(s->h, nullptr);
    }
    LeaveCriticalSection(&g_sessions_cs);
    for (auto *s : g_sessions) {
        ShutdownSession(s);
        delete s;
    }
    g_sessions.clear();

    VigemShutdown();
    DeleteCriticalSection(&g_feed_cs);
    DeleteCriticalSection(&g_sessions_cs);
    printf("bye\n");
    return 0;
}
