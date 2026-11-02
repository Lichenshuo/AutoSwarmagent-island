"""Agent Island: a Dynamic-Island style widget pinned to the top of the screen.

The window is fixed-size and switches between two modes:

* idle (pointer elsewhere): a color key (#010101) cuts out everything but the
  island, with antialiased edges; Windows treats the whole window as
  click-through, because WebView2 paints outside the layered bitmap it hit-tests.
* hot (pointer on the island): the key is turned off and a rounded window region
  that follows the island shape frame by frame makes exactly the island solid,
  so it can be clicked and dragged while the rest stays click-through.

Expanding and collapsing are CSS transitions; the window itself never resizes.
The window is a tool window: no taskbar button, no Alt-Tab entry, never takes focus.
"""
import ctypes
import json
import sys
import threading
import time
import traceback
import webbrowser
from ctypes import wintypes

import webview

import server

WIN_W, WIN_H = 640, 600          # logical px; pywebview scales to physical itself
COLLAPSE_DELAY = 0.28            # pointer must stay out this long before collapsing
POS_FILE = server.DATA / "island-pos.json"
DISMISSED_FILE = server.DATA / "island-dismissed.json"
LOG_FILE = server.DATA / "island.log"
KEY_RGB = 0x010101

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x40000, 0x08000000
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
VK_LBUTTON = 0x01
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10


def log_error(where):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def hwnd_of(window):
    return int(window.native.Handle.ToInt64())


def place(hwnd, saved):
    """Position in physical pixels: the saved spot if it is still on a screen, else top-center of the current monitor."""
    rc = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    width = rc.right - rc.left
    if saved:
        pt = wintypes.POINT(saved["x"] + width // 2, saved["y"] + 20)
        if user32.MonitorFromPoint(pt, 0):  # MONITOR_DEFAULTTONULL: only if that point is on some monitor
            user32.SetWindowPos(hwnd, None, saved["x"], saved["y"], 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    m = info.rcMonitor
    user32.SetWindowPos(hwnd, None, m.left + (m.right - m.left - width) // 2, m.top, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


class Api:
    """Methods the page calls through window.pywebview.api."""

    def __init__(self):
        self._zone = None   # hover zone (x, y, w, h, inner_width), CSS px
        self._shape = None  # current island shape (x, y, w, h, radius, inner_width), CSS px
        self._window = None

    def report(self, x, y, w, h, inner_width):
        self._zone = (float(x), float(y), float(w), float(h), float(inner_width) or 1.0)

    def hit(self, x, y, w, h, radius, inner_width):
        self._shape = (float(x), float(y), float(w), float(h), float(radius), float(inner_width) or 1.0)

    def open_dashboard(self):
        webbrowser.open(f"http://127.0.0.1:{server.PORT}/")

    def quit(self):
        if self._window:
            self._window.destroy()

    def dismissed(self):
        return server.read_json(DISMISSED_FILE) or []

    def dismiss(self, call_ids):
        ids = sorted(set(self.dismissed()) | set(map(str, call_ids)))[-500:]
        DISMISSED_FILE.write_text(json.dumps(ids), encoding="utf-8")
        return ids

    def start_drag(self):
        # Follow the cursor while the left button is held. Polling the real button state
        # works no matter which (possibly out-of-process) WebView2 window owns mouse capture.
        hwnd = hwnd_of(self._window)
        pt, rc = wintypes.POINT(), wintypes.RECT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        dx, dy = pt.x - rc.left, pt.y - rc.top
        while user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetWindowPos(hwnd, None, pt.x - dx, max(pt.y - dy, -20), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.008)

    def reset_position(self):
        place(hwnd_of(self._window), None)
        POS_FILE.unlink(missing_ok=True)


def prepare_window(window):
    """Runs on the UI thread right before the first show."""
    try:
        from System.Drawing import Color
        key = Color.FromArgb(255, KEY_RGB >> 16 & 255, KEY_RGB >> 8 & 255, KEY_RGB & 255)
        window.native.BackColor = key
        window.native.TransparencyKey = key
        hide_from_taskbar(hwnd_of(window))
    except Exception:
        log_error("prepare_window")


def hide_from_taskbar(hwnd):
    # WinForms adds WS_EX_APPWINDOW, which forces a taskbar button even on a tool window.
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    want = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    if want != style:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
        return True
    return False


def set_idle(hwnd):
    user32.SetWindowRgn(hwnd, None, True)
    user32.SetLayeredWindowAttributes(hwnd, KEY_RGB, 0, LWA_COLORKEY)


def set_hot(hwnd, shape_px):
    x, y, w, h, r = shape_px
    user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(x, y, x + w + 1, y + h + 1, 2 * r, 2 * r), True)
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)


def save_position(rc):
    try:
        POS_FILE.write_text(json.dumps({"x": rc.left, "y": rc.top}), encoding="utf-8")
    except OSError:
        pass


def track_pointer(window, api):
    try:
        _track_pointer(window, api)
    except Exception:
        log_error("track_pointer")


def _track_pointer(window, api):
    while not window.native:
        time.sleep(0.05)
    window.events.shown.wait(10)
    hwnd = hwnd_of(window)
    place(hwnd, server.read_json(POS_FILE))
    # WinForms may re-apply its styles when it shows the form; if so, fix them and re-show
    # (without activating) so the taskbar drops the button.
    if hide_from_taskbar(hwnd):
        user32.ShowWindow(hwnd, 0)   # SW_HIDE
        user32.ShowWindow(hwnd, 8)   # SW_SHOWNA
    opened, hot_mode, left_at, last_pos, last_shape = False, False, None, None, None
    pinned_at = time.time() + 5 if "--demo-open" in sys.argv else None  # screenshot aid: stay open
    pt, rc = wintypes.POINT(), wintypes.RECT()
    while True:
        # Fast while the island is in use (its shape follows the animation), relaxed when idle.
        time.sleep(0.016 if opened or hot_mode else 0.05)
        zone = api._zone
        if not zone or not user32.GetWindowRect(hwnd, ctypes.byref(rc)) or not user32.GetCursorPos(ctypes.byref(pt)):
            continue
        if (rc.left, rc.top) != last_pos:
            if last_pos is not None:
                save_position(rc)
            last_pos = (rc.left, rc.top)
        scale = (rc.right - rc.left) / zone[4]
        zx, zy, zw, zh = (v * scale for v in zone[:4])
        hot = rc.left + zx <= pt.x < rc.left + zx + zw and rc.top + zy <= pt.y < rc.top + zy + zh
        if pinned_at and time.time() > pinned_at:
            hot = True

        if hot:
            left_at = None
            if not opened:
                window.evaluate_js("islandSetOpen(true)")
                opened = True
            shape = api._shape
            if shape:
                s = (rc.right - rc.left) / shape[5]
                px = tuple(int(round(v * s)) for v in shape[:5])
                if px != last_shape or not hot_mode:
                    set_hot(hwnd, px)
                    last_shape, hot_mode = px, True
        elif opened or hot_mode:
            left_at = left_at or time.time()
            if time.time() - left_at > COLLAPSE_DELAY:
                if opened:
                    window.evaluate_js("islandSetOpen(false)")
                set_idle(hwnd)
                opened, hot_mode, left_at, last_shape = False, False, None, None


def already_running():
    kernel32.CreateMutexW(None, False, "Local\\AgentIsland")
    return ctypes.get_last_error() == 183


def main():
    want_dashboard = "--dashboard" in sys.argv
    if already_running():
        if want_dashboard:
            webbrowser.open(f"http://127.0.0.1:{server.PORT}/")
        return
    server.start_server()
    if want_dashboard:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{server.PORT}/")).start()
    api = Api()
    screen = webview.screens[0]
    window = webview.create_window(
        "Agent Island", f"http://127.0.0.1:{server.PORT}/island",
        js_api=api, width=WIN_W, height=WIN_H, x=max(0, (screen.width - WIN_W) // 2), y=0,
        frameless=True, transparent=True, on_top=True, resizable=False, easy_drag=False, focus=False,
    )
    api._window = window
    window.events.before_show += lambda: prepare_window(window)
    threading.Thread(target=track_pointer, args=(window, api), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
