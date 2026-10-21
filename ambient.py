"""
Ambient lights: Govee lamps take on the colours of the screen (like Ambilight).

Uses Govee's local LAN API (UDP, no cloud account, fast enough for video):
  - turn on "LAN Control" for each lamp in the Govee Home app (lamp > Settings),
  - lamps answer a multicast scan sent to 239.255.255.250:4001 on port 4002,
  - commands go straight to the lamp's IP on port 4003.

The screen is captured at 64x36 (the graphics driver does the shrinking, so it's
cheap), every lamp gets the colour of its side of the picture, and the colour is
smoothed so it glides instead of flickering. When the lights are turned off,
each lamp goes back to the colour and brightness it had before.
"""
import json
import math
import socket
import sys
import threading
import time

import numpy as np

IS_WIN = sys.platform == "win32"

SCAN_ADDR = ("239.255.255.250", 4001)
REPLY_PORT = 4002
CMD_PORT = 4003
CAP_W, CAP_H = 64, 36
FPS = 12                      # Govee lamps start dropping commands when sent much faster than this
ZONES = {"Left": (0.0, 0.4), "Right": (0.6, 1.0), "Whole screen": (0.0, 1.0)}
ZONE_NAMES = list(ZONES)


# ------------------------------------------------------------ screen ----

if IS_WIN:
    import ctypes
    import ctypes.wintypes as wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _H = ctypes.c_void_p        # handles are pointer sized; ctypes' default int would cut them on 64-bit
    _MONENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, _H, _H, ctypes.POINTER(wt.RECT), wt.LPARAM)
    for _fn, _res, _args in (
        (_user32.GetDC, _H, [_H]),
        (_user32.ReleaseDC, ctypes.c_int, [_H, _H]),
        (_user32.EnumDisplayMonitors, wt.BOOL, [_H, _H, _MONENUMPROC, wt.LPARAM]),
        (_user32.GetMonitorInfoW, wt.BOOL, [_H, ctypes.c_void_p]),
        (_gdi32.CreateCompatibleDC, _H, [_H]),
        (_gdi32.CreateCompatibleBitmap, _H, [_H, ctypes.c_int, ctypes.c_int]),
        (_gdi32.SelectObject, _H, [_H, _H]),
        (_gdi32.DeleteObject, wt.BOOL, [_H]),
        (_gdi32.DeleteDC, wt.BOOL, [_H]),
        (_gdi32.SetStretchBltMode, ctypes.c_int, [_H, ctypes.c_int]),
        (_gdi32.SetBrushOrgEx, wt.BOOL, [_H, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]),
        (_gdi32.StretchBlt, wt.BOOL, [_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      _H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]),
        (_gdi32.GetDIBits, ctypes.c_int, [_H, _H, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]),
    ):
        _fn.restype, _fn.argtypes = _res, _args

    class _BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BIH), ("bmiColors", wt.DWORD * 4)]

    class _MONITORINFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                    ("szDevice", wt.WCHAR * 32)]


def monitors():
    """Every screen, left to right: [{"name", "rect": [x, y, w, h] or None, "primary"}]."""
    if not IS_WIN:
        return [{"name": "Whole screen", "rect": None, "primary": True}]
    found = []

    def cb(hmon, hdc, rect, data):
        info = _MONITORINFOEX()
        info.cbSize = ctypes.sizeof(info)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append({"rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
                          "primary": bool(info.dwFlags & 1)})
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(cb), 0)
    found.sort(key=lambda m: (m["rect"][0], m["rect"][1]))
    for i, m in enumerate(found):
        m["name"] = f"Screen {i + 1}  ·  {m['rect'][2]}×{m['rect'][3]}" + ("  ·  main" if m["primary"] else "")
    return found or [{"name": "Whole screen", "rect": None, "primary": True}]


class Grabber:
    """Tiny screenshots of one screen. On Windows GDI shrinks the picture itself (a few ms each)."""

    def __init__(self, rect):
        self.rect = rect
        self.hdc = None

    def _open(self):
        self.hdc = _user32.GetDC(None)
        self.mdc = _gdi32.CreateCompatibleDC(self.hdc)
        self.bmp = _gdi32.CreateCompatibleBitmap(self.hdc, CAP_W, CAP_H)
        self.old = _gdi32.SelectObject(self.mdc, self.bmp)
        _gdi32.SetStretchBltMode(self.mdc, 4)               # HALFTONE: averages pixels instead of picking some
        _gdi32.SetBrushOrgEx(self.mdc, 0, 0, None)
        self.bi = _BITMAPINFO()
        h = self.bi.bmiHeader
        h.biSize, h.biWidth, h.biHeight, h.biPlanes, h.biBitCount = ctypes.sizeof(_BIH), CAP_W, -CAP_H, 1, 32
        self.buf = (ctypes.c_ubyte * (CAP_W * CAP_H * 4))()

    def grab(self):
        """(CAP_H, CAP_W, 3) float32 RGB in 0..1, or None (locked screen, UAC prompt, ...)."""
        if not IS_WIN or not self.rect:
            return self._grab_pil()
        if self.hdc is None:
            self._open()
        x, y, w, h = self.rect
        if not _gdi32.StretchBlt(self.mdc, 0, 0, CAP_W, CAP_H, self.hdc, x, y, w, h, 0x00CC0020):
            self.close()                                    # opened again next time
            return None
        _gdi32.SelectObject(self.mdc, self.old)             # GetDIBits wants the bitmap deselected
        ok = _gdi32.GetDIBits(self.mdc, self.bmp, 0, CAP_H, self.buf, ctypes.byref(self.bi), 0)
        _gdi32.SelectObject(self.mdc, self.bmp)
        if not ok:
            return None
        a = np.frombuffer(self.buf, np.uint8).reshape(CAP_H, CAP_W, 4)
        return a[:, :, 2::-1].astype(np.float32) / 255     # BGRA -> RGB

    def _grab_pil(self):
        try:
            from PIL import Image, ImageGrab
            box = None
            if self.rect:
                x, y, w, h = self.rect
                box = (x, y, x + w, y + h)
            img = ImageGrab.grab(bbox=box).convert("RGB").resize((CAP_W, CAP_H), Image.BOX)
            return np.asarray(img, np.float32) / 255
        except Exception:
            return None

    def close(self):
        if IS_WIN and self.hdc is not None:
            _gdi32.SelectObject(self.mdc, self.old)
            _gdi32.DeleteObject(self.bmp)
            _gdi32.DeleteDC(self.mdc)
            _user32.ReleaseDC(None, self.hdc)
        self.hdc = None


def zone_color(img, zone, boost=1.3):
    """The colour one lamp should show: RGB in 0..1.

    Bright, saturated pixels count the most (a red car on a grey street makes
    the lamp red, black letterbox bars count for almost nothing); the
    brightness follows how bright that part of the picture is.
    """
    x0, x1 = ZONES.get(zone, (0.0, 1.0))
    w = img.shape[1]
    a = int(x0 * w)
    px = img[:, a:max(a + 1, int(round(x1 * w)))].reshape(-1, 3)
    mx, mn = px.max(1), px.min(1)
    sat = (mx - mn) / (mx + 1e-6)
    weight = mx * mx * (0.15 + sat)
    if weight.sum() < 1e-6:
        return np.zeros(3, np.float32)
    c = (px * weight[:, None]).sum(0) / weight.sum()
    if c.max() < 1e-4:
        return np.zeros(3, np.float32)
    hue = c / c.max()
    grey = hue.mean()
    hue = np.clip(grey + (hue - grey) * boost, 0, 1)        # a bit more vivid than the screen: lamps wash out
    hue /= max(hue.max(), 1e-6)
    level = min(1.0, float(np.sqrt(np.mean(mx * mx))) * 1.4) ** 0.8
    return (hue * level).astype(np.float32)


# --------------------------------------------------------- Govee LAN ----

_port_lock = threading.Lock()       # only one listener on the reply port at a time


def _packet(cmd, data):
    return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()


def _ask(packet, dests, timeout, local_ip=None, multicast=False):
    """Send a packet and collect what lamps send back to port 4002: [(ip, cmd, data)]."""
    with _port_lock:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rx.bind(("", REPLY_PORT))
        except OSError as e:
            rx.close()
            raise OSError(f"port {REPLY_PORT} is busy (is the Govee desktop app open?)") from e
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if multicast:
                tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                if local_ip:
                    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            for _ in range(2):                               # UDP: say it twice
                for d in dests:
                    try:
                        tx.sendto(packet, d)
                    except OSError:
                        pass
                time.sleep(0.05)
            rx.settimeout(0.15)
            out, end = [], time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = rx.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    msg = json.loads(data)["msg"]
                    out.append((addr[0], msg.get("cmd"), msg.get("data") or {}))
                except (ValueError, KeyError, TypeError):
                    pass
            return out
        finally:
            tx.close()
            rx.close()


def scan(local_ip=None, known=(), timeout=2.0):
    """Lamps with LAN Control on: [{"ip", "sku", "device"}]. Known IPs are also asked directly."""
    pkt = _packet("scan", {"account_topic": "reserve"})
    dests = [SCAN_ADDR] + [(ip, SCAN_ADDR[1]) for ip in known]
    found = {}
    for ip, cmd, data in _ask(pkt, dests, timeout, local_ip, multicast=True):
        if cmd == "scan":
            ip = data.get("ip") or ip
            found[ip] = {"ip": ip, "sku": data.get("sku", ""), "device": data.get("device", "")}
    return list(found.values())


def status(ips, timeout=0.8):
    """{ip: {"onOff", "brightness", "color", "colorTemInKelvin"}} for the lamps that answer."""
    if not ips:
        return {}
    pkt = _packet("devStatus", {})
    return {ip: data for ip, cmd, data in _ask(pkt, [(ip, CMD_PORT) for ip in ips], timeout)
            if cmd == "devStatus"}


# ----------------------------------------------------------- engine ----

class Ambient:
    """Runs the screen -> lamps loop in its own thread. Settings are plain attributes."""

    def __init__(self, cfg, local_ip=None):
        self.lamps = [dict(l) for l in cfg.get("lamps", []) if l.get("ip")]
        self.screen = cfg.get("screen")                     # [x, y, w, h] of the chosen screen
        self.brightness = int(cfg.get("brightness", 100))
        self.smooth = int(cfg.get("smooth", 45))
        self.boost = int(cfg.get("boost", 130))
        self.want_on = bool(cfg.get("on", False))           # remembered: lights come back on at launch
        self.local_ip = local_ip
        self.running = False
        self.thread = None
        self.colors = {}                                    # ip -> (r, g, b) last sent
        self.frame = None                                   # last capture, uint8, for the preview
        self.fps = 0.0
        self.error = None
        self.blink = {}                                     # ip -> until when the loop leaves it alone
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def config(self):
        return {"on": self.want_on, "lamps": self.lamps, "screen": self.screen,
                "brightness": self.brightness, "smooth": self.smooth, "boost": self.boost}

    def active(self):
        return [l for l in self.lamps if l.get("enabled", True)]

    def screen_rect(self):
        mons = monitors()
        for m in mons:
            if m["rect"] == self.screen:
                return m["rect"]
        return next((m["rect"] for m in mons if m["primary"]), mons[0]["rect"])

    def send(self, ip, cmd, data):
        try:
            self.tx.sendto(_packet(cmd, data), (ip, CMD_PORT))
        except OSError:
            pass

    def set_color(self, ip, rgb, kelvin=0):
        r, g, b = (int(v) for v in rgb)
        self.send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": int(kelvin)})

    # ---- on / off
    def start(self):
        """False (with .error set) if there's nothing to light."""
        if self.running:
            return True
        if not self.active():
            self.error = "Add a lamp first (Find lamps)."
            return False
        self.error = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        t = self.thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)                             # let it put the lamps back the way they were

    def identify(self, ip):
        """Blink one lamp so you can tell which one it is."""
        def run():
            self.blink[ip] = time.time() + 2.4
            before = {} if self.running else status([ip])
            self.send(ip, "turn", {"value": 1})
            for _ in range(3):
                self.set_color(ip, (255, 90, 0))
                time.sleep(0.4)
                self.set_color(ip, (4, 4, 4))
                time.sleep(0.35)
            self.colors.pop(ip, None)                       # make the loop send its colour again
            self.blink.pop(ip, None)
            self._restore(before)
        threading.Thread(target=run, daemon=True).start()

    def _restore(self, saved):
        for ip, d in saved.items():
            c = d.get("color") or {}
            self.set_color(ip, (c.get("r", 255), c.get("g", 255), c.get("b", 255)), d.get("colorTemInKelvin", 0))
            if d.get("brightness"):
                self.send(ip, "brightness", {"value": int(d["brightness"])})
            if not d.get("onOff", 1):
                self.send(ip, "turn", {"value": 0})

    def _run(self):
        try:
            saved = status([l["ip"] for l in self.active()])
        except OSError:
            saved = {}
        for l in self.active():
            self.send(l["ip"], "turn", {"value": 1})
        grab, rect, bright_sent = None, None, None
        state, last_send = {}, {}
        frames, t_fps = 0, time.time()
        period = 1 / FPS
        try:
            while self.running:
                t0 = time.perf_counter()
                want = self.screen_rect()
                if grab is None or want != rect:
                    if grab:
                        grab.close()
                    grab, rect = Grabber(want), want
                img = grab.grab()
                lamps = self.active()
                if img is not None:
                    self.frame = (img * 255).astype(np.uint8)
                    if bright_sent != self.brightness:
                        bright_sent = self.brightness
                        for l in lamps:
                            self.send(l["ip"], "brightness", {"value": max(1, min(100, bright_sent))})
                    tau = 0.03 + (self.smooth / 100) ** 2 * 1.5          # seconds to follow a change
                    base = 1 - math.exp(-period / tau)
                    now = time.time()
                    for l in lamps:
                        ip = l["ip"]
                        if self.blink.get(ip, 0) > now:
                            continue
                        target = zone_color(img, l.get("zone", "Whole screen"), self.boost / 100)
                        s = state.setdefault(ip, target.copy())
                        a = max(base, 0.6) if np.abs(target - s).max() > 0.45 else base   # scene cut: catch up fast
                        s += a * (target - s)
                        v = np.clip(s, 0, 1) * 255
                        if v.max() < 4:                                  # black screen: just a faint glow
                            v = np.full(3, 4.0)
                        rgb = tuple(int(round(x)) for x in v)
                        old = self.colors.get(ip)
                        if old is None or max(abs(p - q) for p, q in zip(rgb, old)) >= 2 \
                                or now - last_send.get(ip, 0) > 2:       # resend now and then: UDP can drop
                            self.set_color(ip, rgb)
                            self.colors[ip], last_send[ip] = rgb, now
                    frames += 1
                if time.time() - t_fps >= 1:
                    self.fps, frames, t_fps = frames / (time.time() - t_fps), 0, time.time()
                time.sleep(max(0.0, period - (time.perf_counter() - t0)))
        except Exception as e:
            self.error = str(e)
        finally:
            if grab:
                grab.close()
            self.running = False
            self.colors = {}
            self.fps = 0.0
            self._restore(saved)
