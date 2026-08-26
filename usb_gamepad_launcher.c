/* USB Gamepad launcher for Steam Game Mode.
 *
 * Steam non-Steam games must be real ELF executables; a bare shell script is
 * not reliably launched in Game Mode. This launcher is compiled to a native
 * binary. It runs as the deck user (Game Mode processes cannot obtain root:
 * setuid and sudoers are not honoured inside the game session).
 *
 * The actual USB gadget work is done by usb_gamepad.py, run as root by a
 * systemd service. This launcher's only job is to:
 *
 *  1. Stay alive: it is the "game" Steam keeps running, so Steam Input keeps
 *     exposing its virtual gamepad. If it ever exits, Steam tears the pad down.
 *
 *  2. Signal the root daemon: it creates /home/deck/usb-gamepad-active and
 *     touches it every ~100 ms. The daemon (started by systemd at boot) treats
 *     a fresh marker as "the game is running" and brings up the USB gadget and
 *     forwards. When this process exits the marker goes stale and the daemon
 *     tears the gadget down, returning the Deck to normal USB.
 *
 *  3. Show a window: gamescope keeps showing the Steam loading screen until
 *     the game maps a window and presents frames. When built with -DHAVE_X11
 *     this launcher opens a tiny X11 window and keeps repainting it.
 *
 * X11 support is deliberately header-free: SteamOS does not ship X11 headers,
 * and the runtime libX11 (present once `pacman -S libx11` is installed) exports
 * the functions used here. Link with:  gcc ... -l:libX11.so.6
 *
 * Everything this prints is also appended to ~/usb-gamepad.log.
 *
 * Build (done by install-usb-gamepad.sh):
 *   gcc -O2 -DHAVE_X11 -o usb_gamepad usb_gamepad_launcher.c -l:libX11.so.6
 */
/* Keep the include list minimal (no <errno.h>/<string.h>): on SteamOS the
 * kernel headers can be missing, and <errno.h> pulls in <linux/errno.h>. */
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/select.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#define MARKER_PATH "/home/deck/usb-gamepad-active"
#define LOG_PATH    "/home/deck/usb-gamepad.log"
#define WIN_W       640
#define WIN_H       360

#ifdef HAVE_X11
/* -- minimal header-free declarations of the Xlib functions we use -- */
typedef struct _XDisplay Display;
typedef struct _XGC *GC;
typedef unsigned long Window;
typedef unsigned long Drawable;
typedef unsigned long Font;
typedef unsigned long XID;
typedef int (*XErrorHandler)(Display *, void *);

/* XEvent is 24 longs (192 bytes); we only ever read .type (int, first). */
typedef struct { int type; unsigned char _pad[188]; } XEvent;

extern Display *XOpenDisplay(const char *);
extern int XCloseDisplay(Display *);
extern int XDefaultScreen(Display *);
extern Window XRootWindow(Display *, int);
extern unsigned long XBlackPixel(Display *, int);
extern Window XCreateSimpleWindow(Display *, Window, int, int,
                                  unsigned int, unsigned int, unsigned int,
                                  unsigned long, unsigned long);
extern int XStoreName(Display *, Window, const char *);
extern GC XCreateGC(Display *, Drawable, unsigned long, void *);
extern Font XLoadFont(Display *, const char *);
extern int XSetFont(Display *, GC, Font);
extern int XSetForeground(Display *, GC, unsigned long);
extern int XFillRectangle(Display *, Drawable, GC, int, int,
                          unsigned int, unsigned int);
extern int XDrawString(Display *, Drawable, GC, int, int, const char *, int);
extern int XMapWindow(Display *, Window);
extern int XFlush(Display *);
extern XErrorHandler XSetErrorHandler(XErrorHandler);
extern int XSelectInput(Display *, Window, long);
extern int XConnectionNumber(Display *);
extern int XPending(Display *);
extern int XNextEvent(Display *, XEvent *);

enum { X_Expose = 12, X_ConfigureNotify = 22,
       X_MotionNotify = 6, X_ButtonPress = 4, X_ButtonRelease = 5,
       X_KeyPress = 2,
       X_ExposureMask = (1L << 15), X_StructureNotifyMask = (1L << 17),
       X_PointerMotionMask = (1L << 6), X_ButtonPressMask = (1L << 2),
       X_ButtonReleaseMask = (1L << 3), X_KeyPressMask = (1L << 0) };

static Display *dpy = NULL;
static Window win;
static GC gc;
static Font g_font = 0;

/* OLED burn-in protection state: after IDLE_SECS without pointer/button
 * activity the window goes full black; any input restores it. */
static int g_dimmed = 0;
static struct timeval g_last_input = {0, 0};

static int ignore_xerror(Display *d, void *e) {
    (void)d;
    (void)e;
    return 0;
}

static void draw(void) {
    if (!dpy)
        return;
    /* OLED burn-in protection: full black after 10 s without touch/mouse
     * input; any pointer activity returns to normal immediately. */
    if (g_dimmed) {
        XSetForeground(dpy, gc, 0x000000);
        XFillRectangle(dpy, win, gc, 0, 0, WIN_W, WIN_H);
        XFlush(dpy);
        return;
    }
    XSetForeground(dpy, gc, 0x181825); /* dark background */
    XFillRectangle(dpy, win, gc, 0, 0, WIN_W, WIN_H);
    if (g_font) {
        XSetFont(dpy, gc, g_font);
        XSetForeground(dpy, gc, 0x89B4FA);
        XDrawString(dpy, win, gc, 20, 60, "USB Gamepad Active", 18);
        XSetForeground(dpy, gc, 0xA6ADB0);
        XDrawString(dpy, win, gc, 20, 95, "Steam Deck -> PC", 16);
        XSetForeground(dpy, gc, 0xA6E3A1);
        XDrawString(dpy, win, gc, 20, 140, "Close this game to stop", 23);
        XSetForeground(dpy, gc, 0x6C7086);
        XDrawString(dpy, win, gc, 20, 175,
                    "Screen blanks after 10 s (touch to wake)", 40);
    }
    XFlush(dpy);
}

/* True once no pointer/button activity has been seen for IDLE_SECS. */
#define IDLE_SECS 10

static int screen_should_dim(void) {
    struct timeval now;
    gettimeofday(&now, NULL);
    return (now.tv_sec - g_last_input.tv_sec) >= IDLE_SECS;
}

static void note_input_activity(void) {
    gettimeofday(&g_last_input, NULL);
    g_dimmed = 0;
}

static void setup_window(void) {
    XSetErrorHandler(ignore_xerror);
    dpy = XOpenDisplay(NULL);
    if (!dpy)
        return; /* no display (console/SSH run): stay headless */
    int screen = XDefaultScreen(dpy);
    win = XCreateSimpleWindow(dpy, XRootWindow(dpy, screen), 0, 0, WIN_W, WIN_H,
                              0, XBlackPixel(dpy, screen), XBlackPixel(dpy, screen));
    XStoreName(dpy, win, "USB Gamepad");
    XSelectInput(dpy, win,
                 X_ExposureMask | X_StructureNotifyMask |
                 X_PointerMotionMask | X_ButtonPressMask |
                 X_ButtonReleaseMask | X_KeyPressMask);
    gc = XCreateGC(dpy, win, 0, NULL);
    g_font = XLoadFont(dpy, "fixed");
    gettimeofday(&g_last_input, NULL);   /* blank timer starts at launch */
    XMapWindow(dpy, win);
    draw();
}

static void pump_window(void) {
    if (!dpy)
        return;
    if (XPending(dpy)) {
        XEvent ev;
        XNextEvent(dpy, &ev);
        switch (ev.type) {
        case X_Expose:
        case X_ConfigureNotify:
            draw();
            break;
        case X_MotionNotify:
        case X_ButtonPress:
        case X_ButtonRelease:
        case X_KeyPress:
            if (g_dimmed) {
                note_input_activity();
                draw();          /* wake instantly */
            } else {
                note_input_activity();
            }
            break;
        default:
            break;
        }
        return;
    }
    /* Idle check: flip to black when the timer expires, flip back the moment
     * any event arrives above. Frames keep flowing either way so gamescope
     * keeps presenting our window. */
    int want_dim = screen_should_dim();
    if (want_dim != g_dimmed) {
        g_dimmed = want_dim;
        draw();
        return;
    }
    int xfd = XConnectionNumber(dpy);
    fd_set rfds;
    struct timeval tv = {0, 100000};
    FD_ZERO(&rfds);
    FD_SET(xfd, &rfds);
    select(xfd + 1, &rfds, NULL, NULL, &tv);
    if (FD_ISSET(xfd, &rfds))
        return; /* events queued; they will be drained next iteration */
    draw();     /* keep frames flowing so gamescope keeps showing us */
}
#endif /* HAVE_X11 */

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig) {
    (void)sig;
    g_stop = 1;
}

static void log_msg(const char *msg) {
    fputs(msg, stderr);
    fputc('\n', stderr);
    FILE *log = fopen(LOG_PATH, "a");
    if (log) {
        fputs(msg, log);
        fputc('\n', log);
        fclose(log);
    }
}

/* Create (once) and refresh the "game is running" marker the root daemon
 * watches. mtime must stay recent or the daemon tears the gadget down. */
static void touch_marker(void) {
    int fd = open(MARKER_PATH, O_WRONLY | O_CREAT, 0644);
    if (fd >= 0) {
        close(fd);
        utimes(MARKER_PATH, NULL);
    }
}

int main(void) {
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGHUP, on_signal);

    log_msg("usb_gamepad: game started; signalling the USB gamepad daemon.");
    touch_marker();

#ifdef HAVE_X11
    setup_window();
#endif

    for (;;) {
        if (g_stop)
            break;
#ifdef HAVE_X11
        if (dpy) {
            pump_window();
        } else {
            usleep(100000);
        }
#else
        usleep(100000);
#endif
        touch_marker();
    }

    unlink(MARKER_PATH);
    log_msg("usb_gamepad: game stopped; the Deck returns to normal USB.");
#ifdef HAVE_X11
    if (dpy)
        XCloseDisplay(dpy);
#endif
    return 0;
}
