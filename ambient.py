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
FPS = 30                      # screen checks per second (Bluetooth lamps take each colour in ~40 ms)
LAN_RATE = 12                 # Wi-Fi lamps start dropping commands when sent much faster than this
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


class DXGrabber:
    """Frames straight from the graphics card (Desktop Duplication, via dxcam).

    About 0.1 ms a grab instead of 100+ ms for GDI on a 4K screen, and a new frame
    only arrives when the picture changed (otherwise the last one is returned).
    """

    def __init__(self, rect):
        import dxcam
        self.cam, self.last = None, None
        for dev in range(4):
            for out in range(8):
                try:
                    cam = dxcam.create(device_idx=dev, output_idx=out, output_color="BGRA")
                except Exception:
                    break
                d = cam._output.desc.DesktopCoordinates
                if rect is None or [d.left, d.top, d.right - d.left, d.bottom - d.top] == list(rect):
                    self.cam = cam
                    return
        raise RuntimeError("screen not found for Desktop Duplication")

    def grab(self):
        try:
            f = self.cam.grab()
        except Exception:
            return None                                      # display mode change, UAC prompt: try again
        if f is None:
            return self.last
        h, w = f.shape[:2]
        sy, sx = max(1, h // (CAP_H * 4)), max(1, w // (CAP_W * 4))
        sub = f[sy // 2::sy, sx // 2::sx][:CAP_H * 4, :CAP_W * 4, 2::-1].astype(np.float32)
        if sub.shape[:2] != (CAP_H * 4, CAP_W * 4):
            return self.last
        self.last = sub.reshape(CAP_H, 4, CAP_W, 4, 3).mean((1, 3)) / 255    # 4x4 average per cell
        return self.last

    def close(self):
        pass                                                 # dxcam keeps one camera per screen; reused


_grabbers = {}
_grab_lock = threading.Lock()


class SharedGrabber:
    """One grabber per screen, safe to use from the lights thread and the preview at once."""

    def __init__(self, rect):
        key = tuple(rect) if rect else None
        with _grab_lock:
            if key not in _grabbers:
                g = None
                if IS_WIN:
                    try:
                        g = DXGrabber(rect)
                    except Exception:
                        g = None                             # no dxcam / remote desktop: GDI still works
                _grabbers[key] = g or Grabber(rect)
            self.g = _grabbers[key]

    def grab(self):
        with _grab_lock:
            return self.g.grab()

    def close(self):
        pass


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


# -------------------------------------------------------- Bluetooth ----
#
# Govee bulbs whose LAN control doesn't work (H6008: the ports answer "unreachable")
# still take the commands the Govee app sends up close over Bluetooth LE:
# 20-byte packets, 0x33 = set, 0xAA = query/keep-alive, last byte = XOR of the rest.
# H6008 colour: 33 05 0D R G B. Brightness: 33 04 0-255. Power: 33 01 00/01.

BLE_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"
BLE_NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
BLE_NAMES = ("ihoment_", "govee_", "minger_")


def ble_packet(*data):
    b = list(data) + [0] * (19 - len(data))
    x = 0
    for v in b:
        x ^= v
    return bytes(b + [x])


def mac_plus(mac, n=1):
    """Govee's Bluetooth address is its Wi-Fi MAC plus one: 60:74:f4:ac:a6:a6 -> 60:74:F4:AC:A6:A7."""
    v = int(mac.replace(":", "").replace("-", ""), 16) + n
    h = f"{v:012X}"
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def ble_scan(timeout=8.0):
    """Govee lamps advertising over Bluetooth: [{"ble", "name", "sku", "rssi"}]."""
    import asyncio
    from bleak import BleakScanner

    async def run():
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for addr, (d, adv) in found.items():
            name = d.name or adv.local_name or ""
            if name.lower().startswith(BLE_NAMES):
                parts = name.split("_")
                out.append({"ble": addr.upper(), "name": name, "sku": parts[1] if len(parts) > 1 else "",
                            "rssi": adv.rssi})
        return out
    return asyncio.run(run())


class BleLink:
    """Keeps a fast Bluetooth connection to each lamp and sends it the newest colour.

    Runs its own asyncio loop in a thread. Only the latest colour is kept per lamp
    and the next one goes out as soon as the lamp confirms the last (it echoes every
    command), so nothing queues up in Windows' Bluetooth stack and the lamps never
    fall behind the picture. The link asks Windows for its low-latency
    ("throughput") connection mode: ~40 ms per colour instead of ~110-290 ms.
    A lamp that stops answering is reconnected. On first connect it reads the
    lamp's colour and brightness so they can be put back afterwards.
    """

    STALL_S = 4.0                # no reply at all for this long while sending: reconnect

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}            # addr -> {"power": .., "bright": .., "color": ..}
        self.state = {}              # addr -> "connecting" | "connected" | "not found" | error text
        self.latency = {}            # addr -> smoothed ms from command to the lamp's confirmation
        self.saved = {}              # addr -> {"color": (r, g, b), "bright": v}
        self.devices = {}            # addr -> BLEDevice from a scan (reconnects skip the scan)
        self.wake = {}               # addr -> asyncio.Event: something new to send
        self.tasks = {}
        self.loop = None
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, fn):
        while self.loop is None:
            time.sleep(0.01)
        self.loop.call_soon_threadsafe(fn)

    def set(self, addr, **kw):
        with self.lock:
            self.pending.setdefault(addr, {}).update(kw)
        ev = self.wake.get(addr)
        if ev is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(ev.set)

    def connect(self, addrs):
        """Stay connected to exactly these lamps."""
        addrs = set(addrs)

        def apply():
            for a in list(self.tasks):
                if a not in addrs:
                    self.tasks.pop(a).cancel()
            for a in addrs:
                if a not in self.tasks or self.tasks[a].done():
                    self.tasks[a] = self.loop.create_task(self._lamp(a))
        self._call(apply)

    @staticmethod
    def _fast(client):
        """Ask Windows for short Bluetooth connection intervals (Windows 11)."""
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
            client._backend._requester.request_preferred_connection_parameters(P.throughput_optimized)
        except Exception:
            pass

    async def _lamp(self, addr):
        import asyncio
        from bleak import BleakClient, BleakScanner
        wake = self.wake.setdefault(addr, asyncio.Event())
        ack = asyncio.Event()
        while True:
            try:
                self.state[addr] = "connecting"
                dev = self.devices.get(addr)
                if dev is None:
                    dev = await BleakScanner.find_device_by_address(addr, timeout=12.0)
                    if dev is None:
                        self.state[addr] = "not found"      # busy with the Govee app, or out of range
                        await asyncio.sleep(3)
                        continue
                    self.devices[addr] = dev
                client = BleakClient(dev, timeout=15.0)
                try:
                    await client.connect()
                except Exception:
                    self.devices.pop(addr, None)            # stale: scan again next time
                    raise
                try:
                    alive = [time.time()]                     # any reply at all = the lamp is still there

                    def note(_, d, addr=addr):
                        alive[0] = time.time()
                        if d[0] == 0x33:
                            ack.set()
                            return
                        s = self.saved.setdefault(addr, {})
                        if d[0] == 0xAA and d[1] == 0x05 and "color" not in s:
                            s["color"] = (d[3], d[4], d[5])
                        elif d[0] == 0xAA and d[1] == 0x04 and "bright" not in s:
                            s["bright"] = d[2]
                    await client.start_notify(BLE_NOTIFY, note)
                    self._fast(client)
                    if addr not in self.saved:                # remember how it was, to put it back later
                        await client.write_gatt_char(BLE_WRITE, ble_packet(0xAA, 0x05, 0x01), response=False)
                        await asyncio.sleep(0.15)
                        await client.write_gatt_char(BLE_WRITE, ble_packet(0xAA, 0x04), response=False)
                        await asyncio.sleep(0.15)
                    self.state[addr] = "connected"
                    last_cmd = alive[0] = time.time()
                    wake.set()                                # send whatever is waiting right away
                    while client.is_connected:
                        try:
                            await asyncio.wait_for(wake.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                        wake.clear()
                        with self.lock:
                            want = self.pending.pop(addr, None)
                        cmds = []
                        if want:
                            if "power" in want:
                                cmds.append(ble_packet(0x33, 0x01, int(want["power"])))
                            if "bright" in want:
                                cmds.append(ble_packet(0x33, 0x04, int(want["bright"])))
                            if "color" in want:
                                r, g, b = want["color"]
                                cmds.append(ble_packet(0x33, 0x05, 0x0D, r, g, b))
                        elif time.time() - last_cmd > 2:      # keep-alive, or the lamp drops us
                            cmds.append(ble_packet(0xAA, 0x01))
                        for pkt in cmds:
                            ack.clear()
                            t0 = time.perf_counter()
                            await client.write_gatt_char(BLE_WRITE, pkt, response=False)
                            last_cmd = time.time()
                            if pkt[0] != 0x33:
                                continue
                            try:                              # pace on the lamp's echo: never queue up
                                await asyncio.wait_for(ack.wait(), timeout=0.25)
                                ms = (time.perf_counter() - t0) * 1000
                                old = self.latency.get(addr)
                                self.latency[addr] = ms if old is None else old + 0.1 * (ms - old)
                            except asyncio.TimeoutError:
                                pass
                        if cmds and time.time() - alive[0] > self.STALL_S:
                            raise RuntimeError("lamp stopped answering")   # reconnect
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                self.state.pop(addr, None)
                self.latency.pop(addr, None)
                raise
            except Exception as e:
                self.state[addr] = f"reconnecting ({e})"[:60]
                self.latency.pop(addr, None)
                await asyncio.sleep(1)


_ble = None


def ble_link():
    """The one Bluetooth link (made on first use; needs the bleak package)."""
    global _ble
    if _ble is None:
        _ble = BleLink()
    return _ble


# ----------------------------------------------------------- engine ----

def lamp_key(lamp):
    return lamp.get("ble") or lamp.get("ip")


class Ambient:
    """Runs the screen -> lamps loop in its own thread. Settings are plain attributes.

    A lamp with a "ble" address is driven over Bluetooth, otherwise over Wi-Fi (LAN API).
    """

    def __init__(self, cfg, local_ip=None):
        self.lamps = [dict(l) for l in cfg.get("lamps", []) if l.get("ip") or l.get("ble")]
        self.screen = cfg.get("screen")                     # [x, y, w, h] of the chosen screen
        self.brightness = int(cfg.get("brightness", 100))
        self.smooth = int(cfg.get("smooth", 25))
        self.boost = int(cfg.get("boost", 150))
        self.want_on = bool(cfg.get("on", False))           # remembered: lights come back on at launch
        self.local_ip = local_ip
        self.running = False
        self.thread = None
        self.colors = {}                                    # lamp key -> (r, g, b) last sent
        self.frame = None                                   # last capture, uint8, for the preview
        self.fps = 0.0
        self.error = None
        self.blink = {}                                     # lamp key -> until when the loop leaves it alone
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def config(self):
        return {"on": self.want_on, "lamps": self.lamps, "screen": self.screen,
                "brightness": self.brightness, "smooth": self.smooth, "boost": self.boost}

    def active(self):
        return [l for l in self.lamps if l.get("enabled", True)]

    def link_state(self, lamp):
        """How the lamp is reached, for the UI."""
        if lamp.get("ble"):
            st = _ble.state.get(lamp["ble"]) if _ble else None
            ms = _ble.latency.get(lamp["ble"]) if _ble else None
            return "Bluetooth" + (f" · {st}" if st else "") + (f" · {ms:.0f} ms" if ms and st == "connected" else "")
        return f"Wi-Fi · {lamp['ip']}"

    def screen_rect(self):
        mons = monitors()
        for m in mons:
            if m["rect"] == self.screen:
                return m["rect"]
        return next((m["rect"] for m in mons if m["primary"]), mons[0]["rect"])

    # ---- talking to one lamp (Wi-Fi or Bluetooth)
    def send(self, ip, cmd, data):
        try:
            self.tx.sendto(_packet(cmd, data), (ip, CMD_PORT))
        except OSError:
            pass

    def power(self, lamp, on):
        if lamp.get("ble"):
            ble_link().set(lamp["ble"], power=1 if on else 0)
        else:
            self.send(lamp["ip"], "turn", {"value": 1 if on else 0})

    def bright(self, lamp, pct):
        pct = max(1, min(100, int(pct)))
        if lamp.get("ble"):
            ble_link().set(lamp["ble"], bright=max(1, round(pct * 2.55)))
        else:
            self.send(lamp["ip"], "brightness", {"value": pct})

    def color(self, lamp, rgb, kelvin=0):
        r, g, b = (int(v) for v in rgb)
        if lamp.get("ble"):
            ble_link().set(lamp["ble"], color=(r, g, b))
        else:
            self.send(lamp["ip"], "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": int(kelvin)})

    # ---- on / off
    def start(self):
        """False (with .error set) if there's nothing to light."""
        if self.running:
            return True
        if not self.active():
            self.error = "Add a lamp first (Find lamps)."
            return False
        if any(l.get("ble") for l in self.active()):
            try:
                import bleak  # noqa: F401
            except ImportError:
                self.error = "Bluetooth lamps need the 'bleak' package (pip install bleak)."
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

    def identify(self, lamp):
        """Blink one lamp so you can tell which one it is."""
        key = lamp_key(lamp)

        def run():
            self.blink[key] = time.time() + 3.0
            if lamp.get("ble"):
                ble_link().connect(self._ble_addrs() | {lamp["ble"]})
                for _ in range(40):                         # give it a moment to connect
                    if _ble.state.get(lamp["ble"]) == "connected":
                        break
                    time.sleep(0.25)
                before = {}
            else:
                before = {} if self.running else status([lamp["ip"]])
            self.power(lamp, True)
            for _ in range(3):
                self.color(lamp, (255, 90, 0))
                time.sleep(0.4)
                self.color(lamp, (4, 4, 4))
                time.sleep(0.35)
            self.colors.pop(key, None)                      # make the loop send its colour again
            self.blink.pop(key, None)
            if lamp.get("ble") and not self.running:
                self._restore_ble([lamp])
                time.sleep(0.8)
                ble_link().connect(self._ble_addrs())       # lights off: let go of it again
            self._restore(before)
        threading.Thread(target=run, daemon=True).start()

    def _ble_addrs(self):
        return {l["ble"] for l in self.active() if l.get("ble")} if self.running else set()

    def _restore(self, saved):
        """Wi-Fi lamps: back to the state read before we started."""
        for ip, d in saved.items():
            c = d.get("color") or {}
            lamp = {"ip": ip}
            self.color(lamp, (c.get("r", 255), c.get("g", 255), c.get("b", 255)), d.get("colorTemInKelvin", 0))
            if d.get("brightness"):
                self.bright(lamp, d["brightness"])
            if not d.get("onOff", 1):
                self.power(lamp, False)

    def _restore_ble(self, lamps):
        for l in lamps:
            s = _ble.saved.get(l["ble"], {}) if _ble else {}
            if "color" in s:
                ble_link().set(l["ble"], color=s["color"])
            if "bright" in s:
                ble_link().set(l["ble"], bright=s["bright"])

    def _run(self):
        lan = [l for l in self.active() if not l.get("ble")]
        ble = [l for l in self.active() if l.get("ble")]
        try:
            saved = status([l["ip"] for l in lan])
        except OSError:
            saved = {}
        if ble:
            ble_link().connect({l["ble"] for l in ble})
        for l in self.active():
            self.power(l, True)
        grab, rect, bright_sent, linked = None, None, None, None
        state, last_send = {}, {}
        frames, t_fps = 0, time.time()
        want, t_screen = self.screen_rect(), time.time()
        period = 1 / FPS
        prev = time.perf_counter()
        try:
            while self.running:
                t0 = time.perf_counter()
                dt, prev = min(0.25, t0 - prev), t0
                if time.time() - t_screen > 2:                       # screens rarely change: check now and then
                    want, t_screen = self.screen_rect(), time.time()
                if grab is None or want != rect:
                    grab, rect = SharedGrabber(want), want
                img = grab.grab()
                lamps = self.active()
                addrs = {l["ble"] for l in lamps if l.get("ble")}
                if addrs != linked and (addrs or linked):            # follow lamps switched on/off
                    ble_link().connect(addrs)
                linked = addrs
                if img is not None:
                    self.frame = (img * 255).astype(np.uint8)
                    if bright_sent != self.brightness:
                        bright_sent = self.brightness
                        for l in lamps:
                            self.bright(l, bright_sent)
                    tau = 0.015 + (self.smooth / 100) ** 2 * 0.8          # seconds to follow a change
                    base = 1 - math.exp(-dt / tau)
                    now = time.time()
                    for l in lamps:
                        key = lamp_key(l)
                        if self.blink.get(key, 0) > now:
                            continue
                        target = zone_color(img, l.get("zone", "Whole screen"), self.boost / 100)
                        s = state.setdefault(key, target.copy())
                        a = max(base, 0.7) if np.abs(target - s).max() > 0.35 else base   # scene cut: jump to it
                        s += a * (target - s)
                        v = np.clip(s, 0, 1) * 255
                        if v.max() < 4:                                  # black screen: just a faint glow
                            v = np.full(3, 4.0)
                        rgb = tuple(int(round(x)) for x in v)
                        old = self.colors.get(key)
                        gap = now - last_send.get(key, 0)
                        if not l.get("ble") and gap < 1 / LAN_RATE:      # Wi-Fi lamps drop floods of commands
                            continue
                        if old is None or max(abs(p - q) for p, q in zip(rgb, old)) >= 2 \
                                or gap > 2:                              # resend now and then: UDP can drop
                            self.color(l, rgb)                           # Bluetooth: newest only, paced by the lamp
                            self.colors[key], last_send[key] = rgb, now
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
            if ble:
                self._restore_ble(ble)
                time.sleep(0.8)                             # let the restore go out, then free the radio
                ble_link().connect(set())
