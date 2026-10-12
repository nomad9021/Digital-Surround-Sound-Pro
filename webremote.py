"""
Phone remote + speaker mixer for Surround Mixer, served over your Wi-Fi.

  - /           the web app (Remote + Speakers tabs)
  - /api/state  mixer state + what's playing
  - /api/cmd    mixer changes, trackpad, keys, media controls
  - /api/pair/* one-time PIN pairing

A phone pairs once: it asks to connect, the PC shows a one-time PIN (until the
phone enters it or you press X), and the phone then gets its own secret token.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime
import hashlib
import json
import os
import queue
import secrets
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8765
SECURE_PORT = 8766          # https: phones only allow the microphone (calibration) on secure pages
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
MIXER_CMDS = {"start", "stop", "master", "master_step", "mute_all", "mode", "gain", "source",
              "speaker", "listener", "win_volume"}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent; just picks the Wi-Fi/LAN interface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------ keyboard / mouse ----

IS_WIN = sys.platform == "win32"
user32 = ctypes.WinDLL("user32", use_last_error=True) if IS_WIN else None


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


M_MOVE, M_LDOWN, M_LUP, M_RDOWN, M_RUP, M_WHEEL, M_HWHEEL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x800, 0x1000
K_EXT, K_UP, K_UNICODE = 0x1, 0x2, 0x4

VK = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "enter": 0x0D, "esc": 0x1B, "space": 0x20,
      "backspace": 0x08, "tab": 0x09, "f": 0x46, "j": 0x4A, "k": 0x4B, "l": 0x4C, "alt": 0x12,
      "shift": 0x10, "ctrl": 0x11, "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1,
      "vol_mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0xB3, 0xB0, 0xB1, 0xAD, 0xAE, 0xAF}

# remote buttons -> keys on the PC (volume = the real keyboard volume keys, so Windows handles it)
NAMED_KEYS = {"up": ["up"], "down": ["down"], "left": ["left"], "right": ["right"], "select": ["enter"],
              "back": ["esc"], "fullscreen": ["f"], "space": ["space"], "backspace": ["backspace"],
              "enter": ["enter"], "tab": ["tab"],
              "volume_up": ["vol_up"], "volume_down": ["vol_down"], "volume_mute": ["vol_mute"]}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def _key(vk, up=False):
    i = INPUT(type=1)
    flags = (K_UP if up else 0) | (K_EXT if vk in EXTENDED else 0)
    i.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    return i


def press(names):
    vks = [VK[n] for n in names]
    _send(*[_key(v) for v in vks], *[_key(v, True) for v in reversed(vks)])


def type_text(text):
    raw = text.encode("utf-16-le")
    ins = []
    for unit in (int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)):
        for up in (False, True):
            i = INPUT(type=1)
            i.u.ki = KEYBDINPUT(0, unit, K_UNICODE | (K_UP if up else 0), 0, 0)
            ins.append(i)
    if ins:
        _send(*ins)


def mouse_move(dx, dy):
    _send(_mouse(M_MOVE, int(dx), int(dy)))


def mouse_button(button, down):
    flag = {("left", True): M_LDOWN, ("left", False): M_LUP,
            ("right", True): M_RDOWN, ("right", False): M_RUP}[(button, down)]
    _send(_mouse(flag))


def click(button="left", count=1):
    for _ in range(count):
        mouse_button(button, True)
        mouse_button(button, False)


def scroll(dy, dx=0):
    ins = []
    if dy:
        ins.append(_mouse(M_WHEEL, data=int(dy)))
    if dx:
        ins.append(_mouse(M_HWHEEL, data=int(dx)))
    if ins:
        _send(*ins)


def foreground_title():
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def seek_with_keys(seconds):
    """Fallback when the player doesn't accept seeking from Windows: use its own shortcut."""
    title = foreground_title().lower()
    fwd = seconds > 0
    if "youtube" in title:
        press(["l" if fwd else "j"])                     # YouTube: 10 s
    elif "vlc" in title:
        press(["alt", "right" if fwd else "left"])       # VLC: 10 s
    else:
        press(["right" if fwd else "left"])              # Netflix, Prime, most players


def display_off():
    user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)       # broadcast SC_MONITORPOWER -> off


if not IS_WIN:                     # Linux: same functions, done with xdotool / pactl / xset
    from linux_io import (press, type_text, mouse_move, mouse_button, click, scroll,  # noqa: F811
                          foreground_title, display_off)


# ---------------------------------------------------- now playing (SMTC) ----

class Media:
    """Reads and controls whatever Windows shows in its media overlay (Spotify, browsers, ...)."""

    def __init__(self):
        self.state = {"available": False}
        self.art = (None, b"", "image/png")
        self.loop = None
        self.mgr = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        except Exception:
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main(M))

    async def _main(self, M):
        self.mgr = await M.request_async()
        while True:
            try:
                await self._refresh()
            except Exception:
                self.state = {"available": False}
            await asyncio.sleep(1.0)

    def _session(self):
        return self.mgr.get_current_session() if self.mgr else None

    @staticmethod
    def _position(s):
        tl = s.get_timeline_properties()
        pos = tl.position.total_seconds()
        dur = (tl.end_time - tl.start_time).total_seconds()
        if s.get_playback_info().playback_status == 4:          # playing: add time since last update
            pos += (datetime.datetime.now(datetime.timezone.utc) - tl.last_updated_time).total_seconds()
        return max(0.0, min(pos, dur) if dur > 0 else pos), dur, tl.start_time.total_seconds()

    async def _refresh(self):
        s = self._session()
        if not s:
            self.state = {"available": False}
            return
        p = await s.try_get_media_properties_async()
        pb = s.get_playback_info()
        pos, dur, _ = self._position(s)
        key = f"{p.title}|{p.artist}"
        if key != self.art[0]:
            data, ctype = b"", "image/png"
            if p.thumbnail:
                try:
                    from winrt.windows.storage.streams import Buffer, InputStreamOptions
                    st = await p.thumbnail.open_read_async()
                    buf = Buffer(st.size)
                    await st.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
                    data, ctype = bytes(buf), st.content_type or "image/png"
                except Exception:
                    pass
            self.art = (key, data, ctype)
        app = (s.source_app_user_model_id or "").split("!")[-1]   # "Spotify.exe", "Brave", "X_8we!App"
        app = app[:-4] if app.lower().endswith(".exe") else app
        self.state = {
            "available": True, "title": p.title or "", "artist": p.artist or p.album_title or "",
            "app": app.split(".")[-1], "playing": pb.playback_status == 4, "position": round(pos, 1),
            "duration": round(dur, 1), "can_seek": bool(pb.controls.is_playback_position_enabled),
            "art": str(abs(hash(key))) if self.art[1] else "",
        }

    def _call(self, coro, timeout=2.0):
        if not self.loop or not self._session():
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        except Exception:
            return None

    async def _do(self, action):
        s = self._session()
        if action == "play_pause":
            return await s.try_toggle_play_pause_async()
        if action == "next":
            return await s.try_skip_next_async()
        if action == "prev":
            return await s.try_skip_previous_async()
        return False

    async def _seek(self, delta):
        s = self._session()
        if not s.get_playback_info().controls.is_playback_position_enabled:
            return False
        pos, dur, start = self._position(s)
        new = max(0.0, pos + delta)
        if dur > 0:
            new = min(new, dur - 0.5)
        ok = await s.try_change_playback_position_async(int((start + new) * 10_000_000))
        await self._refresh()
        return ok

    def command(self, action):
        ok = self._call(self._do(action))
        if not ok:
            press([action])                      # media key fallback
        self._call(self._refresh())

    def seek(self, delta):
        if not self._call(self._seek(delta)):
            seek_with_keys(delta)


if not IS_WIN:                     # Linux: MPRIS players through playerctl
    from linux_io import Media  # noqa: F811


def remote_command(media, c):
    t = c.get("type")
    if t == "move":
        mouse_move(max(-500, min(500, c.get("dx", 0))), max(-500, min(500, c.get("dy", 0))))
    elif t == "click":
        click("right" if c.get("button") == "right" else "left", 2 if c.get("double") else 1)
    elif t == "button":
        mouse_button("right" if c.get("button") == "right" else "left", bool(c.get("down")))
    elif t == "scroll":
        scroll(max(-1200, min(1200, c.get("dy", 0))), max(-1200, min(1200, c.get("dx", 0))))
    elif t == "key" and c.get("name") in NAMED_KEYS:
        press(NAMED_KEYS[c["name"]])
    elif t == "text":
        type_text(str(c.get("text", ""))[:500])
    elif t == "media" and c.get("action") in ("play_pause", "next", "prev"):
        media.command(c["action"])
    elif t == "seek":
        media.seek(max(-600, min(600, float(c.get("seconds", 10)))))
    elif t == "display_off":
        display_off()
    else:
        return False
    return True


# ------------------------------------------------------------ pairing ----

class Pairing:
    """One-time PINs and the tokens of phones that have paired.

    - A phone asks to connect -> one pending request with a fresh 6-digit PIN,
      shown on the PC until the phone enters it, you dismiss it, or 2 minutes pass.
    - The QR code carries its own single-use code (valid 5 minutes).
    - A paired phone gets a random token; only its SHA-256 is stored in the config.
    """
    TTL, QR_TTL, MAX_TRIES = 120, 300, 5

    def __init__(self, devices):
        self.lock = threading.Lock()
        self.devices = [d for d in devices if d.get("hash")]
        self.pending = None
        self.qr = {}

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _pin():
        return f"{secrets.randbelow(10 ** 6):06d}"

    def valid(self, token):
        if not token:
            return False
        h = self._hash(token)
        with self.lock:
            return any(secrets.compare_digest(h, d["hash"]) for d in self.devices)

    def _live(self):
        p = self.pending
        if p and time.time() - p["t"] > self.TTL:
            self.pending = p = None
        return p

    def request(self, name, ip):
        """Returns a request id, or None if another phone is pairing right now."""
        with self.lock:
            p = self._live()
            if p:
                return p["id"] if p["ip"] == ip else None
            self.pending = {"id": secrets.token_hex(8), "pin": self._pin(), "name": name[:40],
                            "ip": ip, "t": time.time(), "tries": 0}
            return self.pending["id"]

    def current(self):
        """The request to show on the PC (or None)."""
        with self.lock:
            p = self._live()
            return dict(p) if p else None

    def dismiss(self):
        with self.lock:
            self.pending = None

    def is_pending(self, rid):
        with self.lock:
            p = self._live()
            return bool(p and p["id"] == rid) or rid in self.qr

    def make_qr(self):
        with self.lock:
            now = time.time()
            self.qr = {k: v for k, v in self.qr.items() if now - v[1] < self.QR_TTL}
            rid, pin = secrets.token_hex(8), self._pin()
            self.qr[rid] = (pin, now)
            return rid, pin

    def confirm(self, rid, pin, name):
        """Returns (token, None) or (None, "wrong" | "expired")."""
        with self.lock:
            p = self._live()
            if p and p["id"] == rid:
                if not secrets.compare_digest(pin.encode(), p["pin"].encode()):
                    p["tries"] += 1
                    if p["tries"] >= self.MAX_TRIES:
                        self.pending = None
                        return None, "expired"
                    return None, "wrong"
                self.pending = None
            elif rid in self.qr:
                qpin, t = self.qr.pop(rid)                      # single use, right or wrong
                if time.time() - t > self.QR_TTL or not secrets.compare_digest(pin.encode(), qpin.encode()):
                    return None, "expired"
            else:
                return None, "expired"
            token = secrets.token_urlsafe(32)
            self.devices.append({"name": name[:40], "hash": self._hash(token),
                                 "added": time.strftime("%Y-%m-%d")})
            return token, None

    def export(self):
        with self.lock:
            return [dict(d) for d in self.devices]

    def forget_all(self):
        with self.lock:
            self.devices.clear()
            self.pending = None
            self.qr.clear()


# ------------------------------------------------------------- server ----

STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon.png": ("icon.png", "image/png")}


def ask_app(app, cmd, timeout=8):
    """Run a command on the app's (Tk) thread and wait for its answer."""
    reply = queue.Queue()
    app.submit(dict(cmd, _reply=reply))
    try:
        return reply.get(timeout=timeout)
    except queue.Empty:
        return {"error": "The PC app is busy. Try again."}


def calibration_upload(app, body, rate, plan_id):
    import numpy as np
    import calibrate
    plan = app.cal_plan
    if not plan or plan["id"] != plan_id:
        return {"error": "That calibration already finished. Start a new one."}
    rec = np.frombuffer(body[: len(body) // 2 * 2], "<i2").astype(np.float32) / 32768
    results, quality = calibrate.analyze(rec, rate, len(plan["ids"]))
    return ask_app(app, {"type": "cal_result", "results": results, "quality": quality})


def make_handler(app, media, pairing):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except ValueError:
                return {}

        def _authed(self):
            if pairing.valid(self.headers.get("X-Token", "")):
                return True
            self._json({"error": "pair"}, 401)
            return False

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                try:
                    with open(os.path.join(WEB_DIR, name), "rb") as f:
                        return self._send(200, f.read(), ctype)
                except OSError:
                    return self._send(404)
            if url.path == "/api/pair/status":
                rid = parse_qs(url.query).get("id", [""])[0]
                return self._json({"pending": pairing.is_pending(rid)})
            if url.path == "/api/state":
                if self._authed():
                    self._json({"app": app.snapshot, "media": media.state,
                                "host": os.environ.get("COMPUTERNAME", "PC").title()})
                return
            if url.path == "/api/art":
                if self._authed():
                    _, data, ctype = media.art
                    self._send(200 if data else 404, data, ctype)
                return
            self._send(404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/cal/upload":                 # the phone's recording (16-bit PCM)
                if not self._authed():
                    return
                n = min(int(self.headers.get("Content-Length") or 0), 16 * 1024 * 1024)
                body = self.rfile.read(n)
                rate = int(self.headers.get("X-Rate") or 48000)
                return self._json(calibration_upload(app, body, rate, self.headers.get("X-Plan", "")))
            c = self._body()
            if path == "/api/pair/request":
                rid = pairing.request(str(c.get("name", "Phone")), self.client_address[0])
                return self._json({"id": rid}) if rid else self._json({"error": "busy"}, 409)
            if path == "/api/pair/confirm":
                name = str(c.get("name", "Phone"))
                token, err = pairing.confirm(str(c.get("id", "")), str(c.get("pin", "")), name)
                if token:
                    app.submit({"type": "paired", "name": name})
                    return self._json({"token": token})
                return self._json({"error": err}, 401 if err == "wrong" else 410)
            if path != "/api/cmd":
                return self._send(404)
            if not self._authed():
                return
            if c.get("type") == "cal_start":
                return self._json(ask_app(app, {"type": "cal_start"}))
            if c.get("type") in MIXER_CMDS:
                app.submit(c)
                return self._json({"ok": True})
            try:
                ok = remote_command(media, c)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            self._json({"ok": ok}, 200 if ok else 400)

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass          # phones drop connections all the time (sleep, Wi-Fi changes); not worth logging


class SecureServer(QuietServer):
    """Same app over https (self-signed certificate). The TLS handshake runs in the
    request thread, so a slow phone can't hold up everyone else."""

    def __init__(self, addr, handler, context):
        super().__init__(addr, handler)
        self.context = context

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(30)
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr


def ensure_cert(ip):
    """Self-signed certificate for this PC's address (made again if the address changes)."""
    cert, key, stamp = (os.path.join(APP_DIR, n) for n in ("remote_cert.pem", "remote_key.pem", "remote_cert.ip"))
    try:
        if open(stamp).read() == ip and os.path.exists(cert) and os.path.exists(key):
            return cert, key
    except OSError:
        pass
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Surround Mixer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=825))
         .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)),
                                                     x509.DNSName("localhost")]), critical=False)
         .sign(k, hashes.SHA256()))
    with open(key, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    with open(cert, "wb") as f:
        f.write(c.public_bytes(serialization.Encoding.PEM))
    with open(stamp, "w") as f:
        f.write(ip)
    return cert, key


def start(app, devices, port=PORT):
    """app needs: .snapshot, .cal_plan and .submit(cmd) (thread-safe). Returns the http server;
    .pairing and .secure (the https server, or None) are attached to it."""
    media = Media()
    pairing = Pairing(devices)
    handler = make_handler(app, media, pairing)
    try:
        srv = QuietServer(("0.0.0.0", port), handler)
    except OSError:
        return None
    srv.pairing = pairing
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.secure = None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*ensure_cert(lan_ip()))
        srv.secure = SecureServer(("0.0.0.0", SECURE_PORT), handler, ctx)
        threading.Thread(target=srv.secure.serve_forever, daemon=True).start()
    except Exception:
        pass                       # no https: everything but calibration still works
    return srv
