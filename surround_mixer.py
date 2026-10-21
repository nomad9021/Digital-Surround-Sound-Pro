"""
Surround Mixer - play this PC's audio on several speakers at once
(TV over HDMI + Bluetooth speakers) and turn them into surround sound.

  1. Windows plays into a "source" device you never listen to
     (e.g. "Steam Streaming Speakers" or VB-Audio "CABLE Input").
  2. The app records it, splits it into front / center / rear / sub
     channels and sends each speaker the mix that fits where it stands
     in your room (drag them on the map).
  3. Each speaker has its own sync delay so slow Bluetooth speakers
     line up with the fast HDMI TV.
"""
import ctypes
import json
import math
import os
import queue
import secrets
import socket
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox

import numpy as np
import soundcard as sc

import ambient
import calibrate

try:
    from scipy.signal import lfilter          # fast sub filter (optional)
except Exception:
    lfilter = None

import sys
IS_WIN = sys.platform == "win32"
sys.coinit_flags = 0   # soundcard already set COM to multithreaded; comtypes must match
try:
    from pycaw.pycaw import AudioUtilities   # Windows per-device volume / mute
except Exception:
    AudioUtilities = None
linux_io = None
if not IS_WIN:
    import linux_io                           # pactl / xdotool / playerctl versions of the Windows parts

try:
    import webremote                          # phone remote + web mixer
except Exception:
    webremote = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, "surround_config.json")
ICON = os.path.join(APP_DIR, "surround.ico")
TAP_WAV = os.path.join(APP_DIR, "gentle_tap.wav")
PNG_ICON = os.path.join(APP_DIR, "web", "icon.png")
if IS_WIN:
    STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                               "Surround Mixer.lnk")
else:
    STARTUP_LNK = os.path.expanduser("~/.config/autostart/surround-mixer.desktop")
OS_NAME = "Windows" if IS_WIN else "the system"

RATE = 48000
BLOCK = 480                      # 10 ms per block
BLOCK_MS = BLOCK * 1000 / RATE
SPEED_OF_SOUND = 343.0

CHANNELS = ["FL", "FR", "C", "RL", "RR", "SUB"]
CH_ANGLE = {"FL": -30, "FR": 30, "C": 0, "RL": -110, "RR": 110}

AUTO = "Auto (from position)"


def _mono(row):
    return (row, row)


# Manual roles as (left row, right row) over columns FL FR C RL RR SUB
ROLE_ROWS = {
    "Full stereo":        ([1, 0, .5, .5, 0, 0], [0, 1, .5, 0, .5, 0]),
    "Front Left":         _mono([1, 0, .4, 0, 0, 0]),
    "Front Right":        _mono([0, 1, .4, 0, 0, 0]),
    "Front L+R (stereo)": ([1, 0, .7, 0, 0, 0], [0, 1, .7, 0, 0, 0]),
    "Center":             _mono([0, 0, 1, 0, 0, 0]),
    "Rear Left":          _mono([0, 0, 0, 1, 0, 0]),
    "Rear Right":         _mono([0, 0, 0, 0, 1, 0]),
    "Rear L+R (stereo)":  ([0, 0, 0, 1, 0, 0], [0, 0, 0, 0, 1, 0]),
    "Subwoofer":          _mono([0, 0, 0, 0, 0, 1.5]),
}
ROLES = [AUTO] + list(ROLE_ROWS)

ICON_KINDS = ["Bluetooth speaker", "TV", "Soundbar", "Bookshelf", "Subwoofer"]
STEREO_KINDS = {"TV", "Soundbar"}
SPEAKER_COLORS = ["#ff7a45", "#4f8cff", "#2ec4b6", "#c77dff", "#ffd166", "#ef476f", "#7bd389"]

ROOM_W, ROOM_D = 8.0, 6.0        # metres shown on the map

T = {
    "bg": "#12141a", "panel": "#1a1d25", "panel2": "#232733", "line": "#2e3341",
    "text": "#e8eaf0", "muted": "#8a90a2", "accent": "#5b8cff", "good": "#3ddc97",
    "bad": "#ff5d6c", "floor": "#171a22", "grid": "#1f2330", "wall": "#3a4052",
}


_EP_CACHE = {}      # Tk thread only: device name -> IAudioEndpointVolume


def endpoint_volumes(names, cached=True):
    """{device name: IAudioEndpointVolume} for the given active output devices.

    Listing every Windows audio device is slow, so the Tk thread keeps the
    results; reset_endpoint_cache() when devices change. Other threads pass
    cached=False (COM objects belong to the thread that made them).
    """
    if cached and all(n in _EP_CACHE for n in names):
        return {n: _EP_CACHE[n] for n in names}
    if linux_io:
        out = linux_io.endpoint_volumes(names)
        if cached:
            _EP_CACHE.update(out)
        return out
    out = {}
    if AudioUtilities is None:
        return out
    try:
        for d in AudioUtilities.GetAllDevices():
            if d.FriendlyName in names and d.FriendlyName not in out and d.state.name == "Active":
                try:
                    out[d.FriendlyName] = d.EndpointVolume
                except Exception:
                    pass
    except Exception:
        pass
    if cached:
        _EP_CACHE.update(out)
    return out


def reset_endpoint_cache():
    _EP_CACHE.clear()


def write_audio(player, out):
    """Write one block once it fits, polling every 2 ms (soundcard's own loop polls every 1 ms).

    Don't sleep for the whole computed gap: longer sleeps let the device fall behind
    the capture clock and the sync controller then has to skip audio (crackles).
    """
    n = len(out)
    while player._render_available_frames() < n:
        time.sleep(0.002)
    player.play(out)


def make_tap():
    """Soft 'tick', saved as gentle_tap.wav to replace Windows' volume chime."""
    t = np.arange(int(RATE * 0.03)) / RATE
    env = np.exp(-t / 0.004) * (1 - np.exp(-t / 0.0004))
    tone = np.sin(2 * np.pi * 1250 * t) + 0.35 * np.sin(2 * np.pi * 2500 * t)
    return (0.22 * env * tone).astype(np.float32)


TAP = make_tap()


def save_tap_wav(path):
    import wave
    pcm = (np.clip(TAP, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def com_init():
    # soundcard uses COM; every thread that touches audio needs it initialised.
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except Exception:
        pass


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ------------------------------------------------------------- audio ----

class Splitter:
    """Turns a captured block (stereo or 5.1/7.1) into 6 channels."""

    def __init__(self):
        self.lp_state = 0.0
        self.alpha = 1 - np.exp(-2 * np.pi * 120 / RATE)   # 120 Hz low-pass for sub

    def lowpass(self, x):
        if lfilter is not None:
            a = self.alpha
            out, zf = lfilter([a], [1, a - 1], x, zi=[self.lp_state * (1 - a)])
            self.lp_state = out[-1]
            return out.astype(np.float32)
        out = np.empty_like(x)
        s, a = self.lp_state, self.alpha
        for i, v in enumerate(x):
            s += a * (v - s)
            out[i] = s
        self.lp_state = s
        return out

    def split(self, data):
        ch = data.shape[1]
        if ch >= 6:   # real 5.1/7.1 order: FL FR FC LFE (BL BR | SL SR)
            fl, fr, c, lfe = data[:, 0], data[:, 1], data[:, 2], data[:, 3]
            if ch >= 8:
                rl, rr = (data[:, 4] + data[:, 6]) / 2, (data[:, 5] + data[:, 7]) / 2
            else:
                rl, rr = data[:, 4], data[:, 5]
            sub = lfe + self.lowpass((fl + fr) / 2) * 0.5
        else:         # stereo -> matrix upmix
            fl = data[:, 0]
            fr = data[:, 1] if ch > 1 else data[:, 0]
            c = (fl + fr) * 0.3
            amb = (fl - fr) * 0.7
            rl = fl * 0.5 + amb * 0.5
            rr = fr * 0.5 - amb * 0.5
            sub = self.lowpass((fl + fr) * 0.5)
        return np.column_stack([fl, fr, c, rl, rr, sub]).astype(np.float32)


COLS = 9             # per-sample columns sent to each speaker thread
CUSHION_MS = 40      # same extra buffer on every speaker; absorbs bursty capture
JUMP_MS = 80         # only jump (skip/insert) when this far off, e.g. after moving a delay slider
MAX_SPEED = 0.003    # play at most 0.3% faster/slower to stay locked (inaudible)


class Output:
    """One speaker: its own thread, buffer, mix matrix, delay and volume.

    The buffer between capture and this speaker is held at exactly
    CUSHION_MS + delay_ms by gently resampling (playing a hair faster or
    slower), never by inserting silence or dropping audio, so the delay stays
    rock steady and there are no dropouts.
    """

    def __init__(self, sid, device, matrix, volume, delay_ms):
        self.sid = sid
        self.device = device
        self.matrix = matrix          # (2, 6)
        self.volume = volume
        self.delay_ms = delay_ms
        self.pending = []
        self.lock = threading.Lock()
        self.running = False
        self.error = None
        self.level = 0.0
        self.click_mode = False       # sync wizard: replace music with clicks
        self.click_on = False         # ...and this speaker is one of the two clicking
        self.cal_index = -1           # auto-calibration: this speaker's turn number
        self.stats = {"underruns": 0, "jumps": 0, "speed": 1.0, "error_ms": 0.0}

    def push(self, parts, click, cal):
        with self.lock:
            self.pending.append(np.column_stack([parts, click, cal]))

    def run(self):
        com_init()
        silence = np.zeros((BLOCK, 2), dtype=np.float32)
        data = np.zeros((0, COLS), np.float32)  # 6 surround channels, sync click, calibration chirp + turn
        pos = 0.0                              # fractional read position in data
        err = 0.0                              # smoothed buffer error (frames)
        started = False
        last_target = None
        st = self.stats
        jump = JUMP_MS * RATE / 1000
        try:
            spk = sc.get_speaker(self.device)
            # 40 ms device buffer (same on every speaker, so it doesn't change the sync)
            with spk.player(samplerate=RATE, channels=2, blocksize=BLOCK * 4) as player:
                while self.running:
                    with self.lock:
                        chunks, self.pending = self.pending, []
                    if chunks:
                        data = np.concatenate([data] + chunks)
                    target = (CUSHION_MS + self.delay_ms) * RATE / 1000
                    fill = len(data) - pos

                    if not started:                       # wait until the delay is built up
                        if fill < target:
                            self.level *= 0.8
                            write_audio(player, silence)
                            continue
                        started, last_target = True, target
                        data, pos = data[int(fill - target):], 0.0
                        err = 0.0
                    elif target != last_target or abs(fill - target) > jump:
                        # delay changed (or way off): set it exactly, right now
                        st["jumps"] += target == last_target
                        diff = int(fill - target)
                        if diff > 0:
                            data = data[diff:]
                        else:
                            data = np.concatenate([np.zeros((-diff, COLS), np.float32), data])
                        last_target, err = target, 0.0
                        fill = len(data) - pos

                    err += 0.02 * ((fill - target) - err)
                    speed = 1 + max(-MAX_SPEED, min(MAX_SPEED, err / (2.0 * RATE)))
                    st["speed"], st["error_ms"] = speed, err * 1000 / RATE
                    if fill < BLOCK * speed + 2:          # ran dry (shouldn't happen)
                        st["underruns"] += 1
                        write_audio(player, silence)
                        continue

                    idx = pos + np.arange(BLOCK) * speed
                    i0 = idx.astype(np.int64)
                    frac = (idx - i0)[:, None].astype(np.float32)
                    block = data[i0] * (1 - frac) + data[i0 + 1] * frac
                    pos += BLOCK * speed
                    used = int(pos)
                    data, pos = data[used:], pos - used

                    if block[:, 8].max() > -1.5:            # calibrating: only my chirps, no music
                        mine = (np.abs(block[:, 8] - self.cal_index) < 0.01) | (block[:, 8] > calibrate.ALL - 1)
                        chirp = block[:, 7] * mine * (0.6 * self.volume)
                        out = np.column_stack([chirp, chirp])
                    elif self.click_mode:
                        click = block[:, 6]
                        out = np.column_stack([click, click]) * (0.8 if self.click_on else 0.0)
                    else:
                        out = np.clip(block[:, :6] @ self.matrix.T * self.volume, -1, 1)
                    self.level = float(np.abs(out).max())
                    write_audio(player, out.astype(np.float32))
        except Exception as e:
            self.error = f"{self.device}: {e}"
            self.running = False


class Engine:
    def __init__(self):
        self.outputs = []
        self.running = False
        self.error = None
        self.level = 0.0
        self.gain = 1.0               # input level (0-2), set from the app or phone
        self.cal_request = None       # (speakers, rounds) to calibrate (set by the app)
        self.cal_cancel = False       # the phone stopped calibrating: end the chirps now
        self.cal = None
        self.sample = 0               # capture clock, in samples

    def start(self, source_name, outputs):
        self.outputs = outputs
        self.running = True
        self.error = None
        for o in outputs:
            o.running = True
            threading.Thread(target=o.run, daemon=True).start()
        threading.Thread(target=self.capture, args=(source_name,), daemon=True).start()

    def capture(self, source_name):
        com_init()
        splitter = Splitter()
        # sync click: short 3 kHz tick every 0.6 s, generated on the same clock as the music
        every = int(RATE * 0.6)
        t = np.arange(int(RATE * 0.008)) / RATE
        burst = (np.sin(2 * np.pi * 3000 * t) * np.exp(-t / 0.0015)).astype(np.float32)
        pos = 0
        try:
            mic = sc.get_microphone(source_name, include_loopback=True)
            with mic.recorder(samplerate=RATE, blocksize=BLOCK * 4) as rec:
                while self.running:
                    data = rec.record(numframes=BLOCK).astype(np.float32)
                    if data.ndim == 1:
                        data = data[:, None]
                    if len(data) == 0:
                        continue
                    # (the computer volume is already applied by Windows to what we capture)
                    if self.gain != 1.0:
                        data *= self.gain
                    self.level = float(np.abs(data).max())
                    parts = splitter.split(data)
                    idx = (pos + np.arange(len(data))) % every
                    pos = (pos + len(data)) % every
                    click = np.zeros(len(data), np.float32)
                    hit = idx < len(burst)
                    click[hit] = burst[idx[hit]]
                    cal = self.cal_block(len(data))
                    for o in self.outputs:
                        if o.running:
                            o.push(parts, click, cal)
        except Exception as e:
            self.error = f"Source: {e}"
            self.running = False

    def cal_block(self, n):
        """Calibration columns for this block: chirp signal and whose turn it is."""
        if self.cal_cancel:
            self.cal, self.cal_cancel = None, False
        if self.cal_request:
            n, rounds = self.cal_request
            self.cal = calibrate.Sequence(n, self.sample + int(calibrate.LEAD * RATE), rounds)
            self.cal_request = None
        if self.cal:
            sig, owner, done = self.cal.block(self.sample, n)
            if done:
                self.cal = None
        else:
            sig, owner = np.zeros(n, np.float32), np.full(n, -2.0, np.float32)
        self.sample += n
        return np.column_stack([sig, owner])

    def stop(self):
        self.running = False
        for o in self.outputs:
            o.running = False


# ------------------------------------------------------ room geometry ----

def angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def geometry(sp, listener):
    """Angle (0 = straight ahead / up on the map, +right) and distance in metres."""
    dx, dy = sp["x"] - listener["x"], sp["y"] - listener["y"]
    return math.degrees(math.atan2(dx, -dy)), math.hypot(dx, dy)


def position_label(sp, angle, simple=False):
    if sp["icon"] == "Subwoofer" and (simple or sp["role"] == AUTO):
        return "Subwoofer"
    if simple:
        return "All sound (stereo)"
    if sp["role"] != AUTO:
        return sp["role"]
    a, side = abs(angle), ("Left" if angle < 0 else "Right")
    stereo = sp["icon"] in STEREO_KINDS
    if a <= 20:
        return "Front (stereo)" if stereo else "Front Center"
    if a <= 75:
        return f"Front {side}"
    if a <= 145:
        return f"Surround {side}"
    return "Rear (stereo)" if stereo else "Rear Center"


def compute_mix(speakers, listener, simple=False, calibrated=False):
    """Returns {id: (matrix(2,6), extra_delay_ms)} for enabled speakers.

    Auto speakers share each channel by how close they are (in angle) to
    where that channel should come from, power-normalised so every channel
    is heard exactly once. Closer speakers are turned down and delayed so
    everything arrives at your seat equally loud and at the same time.
    Simple mode: every speaker plays the whole song in stereo.
    Calibrated: the phone measured real arrival times and levels at the seat, so
    the distance guesses from the map are skipped.
    """
    active = [s for s in speakers if s["enabled"]]
    mats, points = {}, []
    for s in active:
        m = np.zeros((2, 6), np.float32)
        mats[s["id"]] = m
        if simple:
            if s["icon"] == "Subwoofer":
                m[:, 5] = 1.5
            else:
                m[0], m[1] = [1, 0, .5, 0, 0, 0], [0, 1, .5, 0, 0, 0]
        elif s["role"] != AUTO:
            m[0], m[1] = ROLE_ROWS[s["role"]]
        elif s["icon"] == "Subwoofer":
            m[:, 5] = 1.5
        else:
            ang = geometry(s, listener)[0]
            if s["icon"] in STEREO_KINDS:
                points += [(s["id"], (0,), ang - 30), (s["id"], (1,), ang + 30)]
            else:
                points.append((s["id"], (0, 1), ang))

    for ci, ch in enumerate(CHANNELS[:5]):
        ws = [math.exp(-(angdiff(p[2], CH_ANGLE[ch]) / 55) ** 2) for p in points]
        norm = math.sqrt(sum(w * w for w in ws))
        if norm < 1e-9:
            continue
        for p, w in zip(points, ws):
            for row in p[1]:
                mats[p[0]][row, ci] += w / norm

    dists = {s["id"]: max(0.3, geometry(s, listener)[1]) for s in active}
    dmax = max(dists.values(), default=1)
    result = {}
    for sid, m in mats.items():
        d = dists[sid]
        if calibrated:
            result[sid] = (m, 0.0)
            continue
        gain = 1.0 if simple else max(0.35, d / dmax)
        result[sid] = (m * gain, (dmax - d) / SPEED_OF_SOUND * 1000)
    return result


# -------------------------------------------------------------- icons ----

def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def draw_icon(c, kind, x, y, s, color, tags=(), dim=False):
    k = {"tags": tags}
    body = blend(color, T["bg"], .6) if dim else color
    dark = blend(body, "#000000", .45)
    light = blend(body, "#ffffff", .35)
    w2 = max(1, 2 * s)
    if kind == "TV":
        rrect(c, x - 32*s, y - 21*s, x + 32*s, y + 17*s, 5*s, fill="#23262f", outline=body, width=w2, **k)
        c.create_rectangle(x - 27*s, y - 16*s, x + 27*s, y + 12*s, fill=blend(body, "#0b0d12", .6), outline="", **k)
        c.create_polygon(x - 27*s, y + 12*s, x - 27*s, y - 16*s, x + 4*s, y - 16*s, x - 20*s, y + 12*s,
                         fill=blend(body, "#0b0d12", .45), outline="", **k)
        c.create_line(x, y + 17*s, x, y + 23*s, fill=body, width=max(1, 3*s), **k)
        c.create_line(x - 13*s, y + 24*s, x + 13*s, y + 24*s, fill=body, width=max(1, 3*s), capstyle="round", **k)
    elif kind == "Bluetooth speaker":
        rrect(c, x - 30*s, y - 14*s, x + 30*s, y + 14*s, 14*s, fill=body, outline="", **k)
        for ex in (-21, 21):
            c.create_oval(x + (ex - 9)*s, y - 9*s, x + (ex + 9)*s, y + 9*s, fill=dark, outline=light, **k)
            c.create_oval(x + (ex - 3)*s, y - 3*s, x + (ex + 3)*s, y + 3*s, fill=light, outline="", **k)
        for gx in (-8, -3, 2, 7):
            for gy in (-6, 0, 6):
                c.create_oval(x + (gx - 1.3)*s, y + (gy - 1.3)*s, x + (gx + 1.3)*s, y + (gy + 1.3)*s,
                              fill=dark, outline="", **k)
    elif kind == "Soundbar":
        rrect(c, x - 42*s, y - 8*s, x + 42*s, y + 8*s, 7*s, fill="#23262f", outline=body, width=w2, **k)
        for gx in range(-34, 35, 6):
            c.create_oval(x + (gx - 1.5)*s, y - 1.5*s, x + (gx + 1.5)*s, y + 1.5*s, fill=body, outline="", **k)
    elif kind == "Bookshelf":
        rrect(c, x - 17*s, y - 25*s, x + 17*s, y + 25*s, 5*s, fill=body, outline="", **k)
        c.create_oval(x - 5*s, y - 19*s, x + 5*s, y - 9*s, fill=dark, outline=light, **k)
        c.create_oval(x - 12*s, y - 4*s, x + 12*s, y + 20*s, fill=dark, outline=light, **k)
        c.create_oval(x - 4*s, y + 4*s, x + 4*s, y + 12*s, fill=light, outline="", **k)
    else:  # Subwoofer
        rrect(c, x - 22*s, y - 22*s, x + 22*s, y + 22*s, 6*s, fill="#23262f", outline=body, width=w2, **k)
        c.create_oval(x - 16*s, y - 16*s, x + 16*s, y + 16*s, fill=dark, outline=body, width=w2, **k)
        c.create_oval(x - 6*s, y - 6*s, x + 6*s, y + 6*s, fill=light, outline="", **k)


def draw_listener(c, x, y, s, tags=()):
    k = {"tags": tags}
    rrect(c, x - 38*s, y - 4*s, x + 38*s, y + 22*s, 9*s, fill="#343846", outline="", **k)
    rrect(c, x - 38*s, y + 12*s, x + 38*s, y + 28*s, 7*s, fill="#2a2d38", outline="", **k)
    for ax in (-38, 26):
        rrect(c, x + ax*s, y - 4*s, x + (ax + 12)*s, y + 28*s, 6*s, fill="#2a2d38", outline="", **k)
    rrect(c, x - 15*s, y - 1*s, x + 15*s, y + 13*s, 7*s, fill=T["accent"], outline="", **k)
    c.create_oval(x - 9*s, y - 16*s, x + 9*s, y + 2*s, fill="#f1c9a5", outline="", **k)
    c.create_polygon(x - 5*s, y - 22*s, x + 5*s, y - 22*s, x, y - 29*s, fill=T["accent"], outline="", **k)


# ---------------------------------------------------------------- GUI ----

class App:
    def __init__(self, root):
        self.root = root
        self.S = max(1.0, root.winfo_fpixels("1i") / 96)
        S = self.S
        self.PX = 74 * S
        self.M = 26 * S
        root.title("Surround Mixer")
        root.configure(bg=T["bg"])
        try:
            if IS_WIN:
                root.iconbitmap(ICON)
            else:                                    # Linux window managers want a PNG
                self._icon = tk.PhotoImage(file=PNG_ICON)
                root.iconphoto(True, self._icon)
        except Exception:
            pass
        self.setup_style()

        self.engine = Engine()
        self.outs = {}
        self.sel = None
        self.drag = None
        self.loading = False
        self.refresh_devices()
        cfg = self.load_cfg()
        self.listener = cfg.get("listener", {"x": ROOM_W / 2, "y": 4.0})
        self.speakers = cfg.get("speakers") or self.default_speakers()
        self.next_id = max((s["id"] for s in self.speakers), default=0) + 1
        self.source = tk.StringVar(value=cfg.get("source") or self.guess_source())

        self.mode = cfg.get("mode", "surround")
        self.calibrated = cfg.get("calibrated", False)      # delays/levels measured by the phone
        self.cal_plan = None
        self.master = tk.DoubleVar(value=cfg.get("master", 40))
        self.ep_levels = {}
        self.muted_all = False
        self.engine.gain = cfg.get("gain", 100) / 100
        self.win_volume, self.win_muted = None, False      # Windows' own volume, read straight from Windows
        self.startup = tk.BooleanVar(value=os.path.exists(STARTUP_LNK))
        self._dragging_win = False
        # phone remote: web server threads hand commands to the Tk thread through this queue
        self.web_q = queue.Queue()
        self.snapshot = {}
        self.dirty_at = None
        self.web = webremote.start(self, cfg.get("devices", [])) if webremote else None
        # Govee lamps that follow the screen's colours
        self.ambient = ambient.Ambient(cfg.get("ambient", {}), webremote.lan_ip() if webremote else None)
        if self.ambient.want_on:
            root.after(1500, self.ambient.start)
        self.build_topbar()
        self.build_toolbar()
        self.build_volbar()
        self.build_pairbar()
        self.build_banner()
        body = tk.Frame(root, bg=T["bg"])
        body.pack(fill="both", expand=True, padx=16 * S, pady=(0, 16 * S))
        self.build_map(body)
        self.build_side(body)

        self.mix = {}
        self.auto_assign()
        self.select(self.speakers[0]["id"] if self.speakers else None)
        root.after(3000, self.poll_devices)
        self.redraw()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.update_idletasks()
        if root.winfo_reqheight() > root.winfo_screenheight() - 90 * self.S:
            root.state("zoomed")   # small screen: use all of it
        self.silent_since = None
        self.autostart = cfg.get("autostart", True)
        if self.autostart:
            root.after(800, lambda: self.start(quiet=True))
        if "pin" in cfg:
            self.save_cfg(quiet=True)          # drop the old permanent PIN from the config
        if not os.path.exists(TAP_WAV):
            try:
                save_tap_wav(TAP_WAV)
            except OSError:
                pass
        if "--minimized" in sys.argv:          # started with Windows: stay out of the way
            root.after(300, root.iconify)
        self.tick()

    # ---------------- style
    def setup_style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("TCombobox", fieldbackground=T["panel2"], background=T["panel2"],
                     foreground=T["text"], arrowcolor=T["text"], bordercolor=T["line"],
                     lightcolor=T["panel2"], darkcolor=T["panel2"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", T["panel2"])],
               foreground=[("readonly", T["text"])], selectbackground=[("readonly", T["panel2"])],
               selectforeground=[("readonly", T["text"])])
        st.configure("Horizontal.TScale", background=T["accent"], troughcolor=T["panel2"],
                     bordercolor=T["panel"], lightcolor=T["accent"], darkcolor=T["accent"])
        self.root.option_add("*TCombobox*Listbox.background", T["panel2"])
        self.root.option_add("*TCombobox*Listbox.foreground", T["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", T["accent"])
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        self.f_title = ("Segoe UI Semibold", 15)
        self.f_head = ("Segoe UI Semibold", 11)
        self.f_body = ("Segoe UI", 10)
        self.f_small = ("Segoe UI", 9)

    def button(self, parent, text, cmd, accent=False, danger=False, **kw):
        bg = T["accent"] if accent else (T["panel2"])
        fg = "#ffffff" if accent else (T["bad"] if danger else T["text"])
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=blend(bg, "#ffffff", .15),
                      activeforeground=fg, relief="flat", bd=0, cursor="hand2",
                      font=self.f_head if accent else self.f_body, padx=12 * self.S, pady=5 * self.S, **kw)
        b._bg = bg
        b.bind("<Enter>", lambda e: b.config(bg=blend(b._bg, "#ffffff", .1)))
        b.bind("<Leave>", lambda e: b.config(bg=b._bg))
        return b

    # ---------------- top bar
    def build_topbar(self):
        S = self.S
        bar = tk.Frame(self.root, bg=T["bg"])
        bar.pack(fill="x", padx=16 * S, pady=14 * S)
        logo = tk.Canvas(bar, width=34 * S, height=34 * S, bg=T["bg"], highlightthickness=0)
        logo.pack(side="left")
        rrect(logo, 1, 1, 33 * S, 33 * S, 9 * S, fill=T["accent"], outline="")
        draw_icon(logo, "Bookshelf", 17 * S, 17 * S, 0.5 * S, "#ffffff")
        tk.Label(bar, text="Surround Mixer", font=self.f_title, bg=T["bg"], fg=T["text"]).pack(side="left", padx=10 * S)

        self.start_btn = self.button(bar, "▶  Start", self.toggle, accent=True, width=9)
        self.start_btn.pack(side="right")
        self.status = tk.Label(bar, text="Stopped", font=self.f_body, bg=T["bg"], fg=T["muted"])
        self.status.pack(side="right", padx=12 * S)
        self.meter = tk.Canvas(bar, width=90 * S, height=8 * S, bg=T["panel2"], highlightthickness=0)
        self.meter.pack(side="right", padx=(6 * S, 4 * S))
        self.button(bar, "⟳", self.on_refresh).pack(side="right", padx=4 * S)
        self.src_box = ttk.Combobox(bar, textvariable=self.source, width=36, state="readonly",
                                    values=self.speaker_names, font=self.f_body)
        self.src_box.pack(side="right", padx=4 * S)
        tk.Label(bar, text="Source", font=self.f_body, bg=T["bg"], fg=T["muted"]).pack(side="right")

    # ---------------- mode + master volume
    def build_toolbar(self):
        S = self.S
        bar = tk.Frame(self.root, bg=T["bg"])
        bar.pack(fill="x", padx=16 * S, pady=(0, 10 * S))
        tk.Label(bar, text="Mode", font=self.f_body, bg=T["bg"], fg=T["muted"]).pack(side="left", padx=(0, 8 * S))
        seg = tk.Frame(bar, bg=T["panel2"])
        seg.pack(side="left")
        self.mode_btns = {}
        for key, text in (("surround", "🎧  Surround"), ("simple", "🔊  Same on all")):
            b = tk.Button(seg, text=text, relief="flat", bd=0, cursor="hand2", font=self.f_body,
                          padx=14 * S, pady=4 * S, command=lambda k=key: self.set_mode(k))
            b.pack(side="left")
            self.mode_btns[key] = b
        self.paint_mode()

        self.button(bar, "📱  Phone remote", self.open_phone).pack(side="right", padx=(6 * S, 0))
        self.button(bar, "💡  Lights", self.open_lights).pack(side="right", padx=(6 * S, 0))

    # ---------------- phone pairing: one-time PIN shown while a phone asks to connect
    def build_pairbar(self):
        S = self.S
        bg = "#16325c"
        self.pairbar = tk.Frame(self.root, bg=bg, padx=16 * S, pady=10 * S)
        tk.Label(self.pairbar, text="📱", font=("Segoe UI Emoji", 18), bg=bg, fg="#ffffff").pack(side="left")
        txt = tk.Frame(self.pairbar, bg=bg)
        txt.pack(side="left", padx=12 * S)
        self.pair_title = tk.Label(txt, font=self.f_head, bg=bg, fg="#ffffff", anchor="w")
        self.pair_title.pack(anchor="w")
        tk.Label(txt, text="Enter this one-time PIN on the phone. Press ✕ if it isn't yours.",
                 font=self.f_small, bg=bg, fg="#a9c4ee", anchor="w").pack(anchor="w")
        x = tk.Button(self.pairbar, text="✕", command=self.dismiss_pair, relief="flat", bd=0, cursor="hand2",
                      font=("Segoe UI", 14), bg=bg, fg="#ffffff", activebackground="#24467a",
                      activeforeground="#ffffff", padx=10 * S)
        x.pack(side="right")
        self.pair_pin = tk.Label(self.pairbar, font=("Segoe UI Semibold", 22), bg="#0d2242", fg="#ffffff",
                                 padx=16 * S, pady=2 * S)
        self.pair_pin.pack(side="right", padx=(0, 12 * S))
        self.pair_shown = None

    def update_pairbar(self):
        p = self.web.pairing.current() if self.web else None
        rid = p["id"] if p else None
        if rid == self.pair_shown:
            return
        self.pair_shown = rid
        if p:
            self.pair_title.config(text=f"{p['name']} wants to connect")
            self.pair_pin.config(text=f"{p['pin'][:3]} {p['pin'][3:]}")
            self.pairbar.pack(fill="x", padx=16 * self.S, pady=(0, 10 * self.S), after=self.volbar)
            if self.root.state() == "iconic":
                self.root.deiconify()              # someone is pairing: show the PIN
            self.root.lift()
        else:
            self.pairbar.pack_forget()

    def dismiss_pair(self):
        if self.web:
            self.web.pairing.dismiss()
        self.update_pairbar()
        self.flash("Connection request dismissed", T["muted"])

    # ---------------- computer volume: a straight passthrough to Windows' own volume
    def build_volbar(self):
        S = self.S
        bar = self.volbar = tk.Frame(self.root, bg=T["panel"], padx=12 * S, pady=8 * S)
        bar.pack(fill="x", padx=16 * S, pady=(0, 10 * S))
        tk.Label(bar, text="🖥  Computer volume", font=self.f_body, bg=T["panel"], fg=T["text"]).pack(side="left")
        self.win_scale = ttk.Scale(bar, from_=0, to=100, length=260 * S, command=self.on_win_slider)
        self.win_scale.pack(side="left", padx=10 * S)
        self.win_lbl = tk.Label(bar, width=6, font=self.f_body, bg=T["panel"], fg=T["text"], anchor="w")
        self.win_lbl.pack(side="left")
        self.win_scale.bind("<ButtonPress-1>", lambda e: setattr(self, "_dragging_win", True))
        self.win_scale.bind("<ButtonRelease-1>", lambda e: setattr(self, "_dragging_win", False))

        tk.Checkbutton(bar, text="Start with Windows" if IS_WIN else "Start when I log in",
                       variable=self.startup, command=self.apply_startup,
                       font=self.f_body, bg=T["panel"], fg=T["text"], selectcolor=T["panel2"],
                       activebackground=T["panel"], activeforeground=T["text"], cursor="hand2").pack(side="right")
        if IS_WIN:
            self.button(bar, "🔔  Windows sounds…", self.open_sounds).pack(side="right", padx=(0, 10 * S))
        self.sync_win_volume(force=True)

    def windows_ep(self):
        """Volume control of Windows' default output - the exact one the keyboard keys change."""
        name = getattr(self, "default_name", None) or self.source.get()
        return endpoint_volumes({name}).get(name)

    def read_win_volume(self):
        try:
            ep = self.windows_ep()
            return round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())
        except Exception:
            return None, False

    def set_win_volume(self, v):
        try:
            ep = self.windows_ep()
            ep.SetMasterVolumeLevelScalar(max(0, min(100, v)) / 100, None)
            if ep.GetMute():
                ep.SetMute(0, None)
        except Exception:
            pass

    def on_win_slider(self, value):
        v = round(float(value))
        self.win_lbl.config(text=f"{v}%")
        self.set_win_volume(v)

    def sync_win_volume(self, force=False):
        """Show Windows' real volume (it can change from the keys, taskbar or phone)."""
        if self._dragging_win and not force:
            return
        v, muted = self.read_win_volume()
        self.win_volume, self.win_muted = v, muted
        if v is None:
            self.win_lbl.config(text="—")
            return
        if round(float(self.win_scale.get())) != v:
            self.win_scale.set(v)
        self.win_lbl.config(text="Muted" if muted else f"{v}%")


    def apply_startup(self):
        """Create/remove the shortcut in the Startup folder (starts minimized with Windows)."""
        try:
            if self.startup.get() and not IS_WIN:            # Linux: an autostart .desktop entry
                os.makedirs(os.path.dirname(STARTUP_LNK), exist_ok=True)
                with open(STARTUP_LNK, "w") as f:
                    f.write("[Desktop Entry]\nType=Application\nName=Surround Mixer\n"
                            f"Exec=\"{sys.executable}\" \"{os.path.abspath(__file__)}\" --minimized\n"
                            f"Icon={PNG_ICON}\nX-GNOME-Autostart-enabled=true\n")
                self.flash("Will start when you log in (minimized)", T["good"])
            elif self.startup.get():
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                script = os.path.abspath(__file__)
                ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
                      "$s.TargetPath=$env:PYW;$s.Arguments='\"'+$env:SCRIPT+'\" --minimized';"
                      "$s.WorkingDirectory=$env:DIR;$s.IconLocation=$env:ICO;$s.Save()")
                env = dict(os.environ, LNK=STARTUP_LNK, PYW=pyw, SCRIPT=script, DIR=APP_DIR, ICO=ICON)
                import subprocess
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=True,
                               creationflags=0x08000000, timeout=20)
                self.flash("Will start with Windows (minimized)", T["good"])
            elif os.path.exists(STARTUP_LNK):
                os.remove(STARTUP_LNK)
                self.flash("Won't start with Windows", T["muted"])
        except Exception as e:
            self.startup.set(os.path.exists(STARTUP_LNK))
            self.flash(f"Couldn't change startup: {e}", T["bad"])

    def open_sounds(self):
        """Windows' Sounds panel: set 'Default Beep' to (None) or to gentle_tap.wav."""
        import subprocess
        subprocess.Popen(["rundll32.exe", "shell32.dll,Control_RunDLL", "mmsys.cpl,,2"])
        messagebox.showinfo(
            "Windows sounds",
            "In the Sounds tab that just opened:\n\n"
            "1. Under Program Events, select “Default Beep”.\n"
            "2. Pick “(None)” in the Sounds list to silence it,\n"
            "    or click Browse… and choose this gentle tap:\n\n"
            f"    {TAP_WAV}\n\n"
            "3. Click OK.")

    def paint_mode(self):
        for k, b in self.mode_btns.items():
            on = k == self.mode
            b.config(bg=T["accent"] if on else T["panel2"], fg="#ffffff" if on else T["muted"],
                     activebackground=T["accent"], activeforeground="#ffffff")

    def set_mode(self, key):
        self.mode = key
        self.paint_mode()
        self.redraw()
        self.save_cfg(quiet=True)

    def apply_master(self, unmute=False):
        """Set the Windows volume of every speaker the app uses to the same level."""
        lvl = round(self.master.get())
        names = {s["device"] for s in self.speakers if s["enabled"] and s["device"]}
        if unmute:
            self.muted_all = False
        for name, ep in endpoint_volumes(names).items():
            try:
                if bool(ep.GetMute()) != self.muted_all:
                    ep.SetMute(1 if self.muted_all else 0, None)
                ep.SetMasterVolumeLevelScalar(lvl / 100, None)
            except Exception:
                pass
        self.read_levels()

    def read_levels(self):
        names = {s["device"] for s in self.speakers if s["device"]}
        levels = {}
        for name, ep in endpoint_volumes(names).items():
            try:
                levels[name] = (round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute()))
            except Exception:
                pass
        if levels != self.ep_levels:
            self.ep_levels = levels
            self.update_cards()

    # ---------------- warning when Windows bypasses the app
    def build_banner(self):
        S = self.S
        self.banner = tk.Frame(self.root, bg="#3a2a12", padx=12 * S, pady=8 * S)
        self.banner_lbl = tk.Label(self.banner, font=self.f_body, bg="#3a2a12", fg="#ffd08a",
                                   justify="left", anchor="w")
        self.banner_lbl.pack(side="left", fill="x", expand=True)
        if IS_WIN:
            b = self.button(self.banner, "Open Sound settings", lambda: os.startfile("ms-settings:sound"))
        else:
            b = self.button(self.banner, "Use it", lambda: linux_io.make_default(self.source.get()))
        b.pack(side="right")
        self.banner_shown = False
        self.check_default()

    def check_default(self):
        try:
            default = sc.default_speaker().name
        except Exception:
            return
        self.default_name = default            # the device Windows' volume keys control
        src = self.source.get()
        if default != src:
            self.banner_lbl.config(
                text=f"⚠  {OS_NAME.capitalize()} is playing sound straight to “{default}”, so the other speakers get nothing.\n"
                     f"     Set your sound output to “{src}”"
                     + (" (taskbar speaker icon › choose output, or Win+Ctrl+V)." if IS_WIN else "."))
            if not self.banner_shown:
                self.banner.pack(fill="x", padx=16 * self.S, pady=(0, 10 * self.S), after=self.root.winfo_children()[0])
                self.banner_shown = True
        elif self.banner_shown:
            self.banner.pack_forget()
            self.banner_shown = False

    # ---------------- room map
    def build_map(self, parent):
        S = self.S
        wrap = tk.Frame(parent, bg=T["panel"])
        wrap.pack(side="left", fill="both")
        w = ROOM_W * self.PX + 2 * self.M
        h = ROOM_D * self.PX + 2 * self.M
        self.cv = tk.Canvas(wrap, width=w, height=h, bg=T["panel"], highlightthickness=0)
        tk.Label(wrap, text="Drag the speakers and your seat to match your room.  Front of the room is at the top.",
                 font=self.f_small, bg=T["panel"], fg=T["muted"]).pack(side="bottom", pady=(4 * S, 14 * S))
        self.cv.pack(padx=8 * S, pady=(8 * S, 0), expand=True)
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.draw_room()

    def m2px(self, x, y):
        return self.M + x * self.PX, self.M + y * self.PX

    def draw_room(self):
        c, S = self.cv, self.S
        x1, y1 = self.m2px(0, 0)
        x2, y2 = self.m2px(ROOM_W, ROOM_D)
        rrect(c, x1, y1, x2, y2, 10 * S, fill=T["floor"], outline=T["wall"], width=2 * S)
        for i in range(1, int(ROOM_W * 2)):
            x = self.m2px(i / 2, 0)[0]
            c.create_line(x, y1 + 3, x, y2 - 3, fill=T["grid"], width=2 if i % 2 == 0 else 1)
        for i in range(1, int(ROOM_D * 2)):
            y = self.m2px(0, i / 2)[1]
            c.create_line(x1 + 3, y, x2 - 3, y, fill=T["grid"], width=2 if i % 2 == 0 else 1)
        c.create_text((x1 + x2) / 2, y1 - 12 * S, text="FRONT", fill=T["muted"], font=("Segoe UI Semibold", 8))
        c.create_text(x2 - 6 * S, y2 + 12 * S, text=f"grid = 0.5 m  ·  room {ROOM_W:g} × {ROOM_D:g} m",
                      anchor="e", fill=T["muted"], font=("Segoe UI", 8))

    def redraw(self):
        c, S = self.cv, self.S
        c.delete("scene")
        self.mix = compute_mix(self.speakers, self.listener, self.mode == "simple", self.calibrated)
        lx, ly = self.m2px(self.listener["x"], self.listener["y"])

        # ideal 5.1 guide ring around the seat
        r = 2.0 * self.PX
        c.create_oval(lx - r, ly - r, lx + r, ly + r, outline=T["grid"], dash=(3, 5), tags="scene")
        for name, ang in [("C", 0), ("L", -30), ("R", 30), ("SL", -110), ("SR", 110)]:
            gx = lx + r * math.sin(math.radians(ang))
            gy = ly - r * math.cos(math.radians(ang))
            c.create_oval(gx - 3 * S, gy - 3 * S, gx + 3 * S, gy + 3 * S, fill=T["line"], outline="", tags="scene")
            c.create_text(gx, gy - 10 * S, text=name, fill=blend(T["muted"], T["bg"], .35),
                          font=("Segoe UI", 7), tags="scene")

        for s in self.speakers:
            sx, sy = self.m2px(s["x"], s["y"])
            col = s["color"] if s["enabled"] else T["line"]
            c.create_line(lx, ly, sx, sy, fill=blend(col, T["floor"], .7), dash=(2, 4), tags="scene")

        draw_listener(c, lx, ly, S, tags=("scene", "drag", "who:listener"))
        c.create_text(lx, ly + 40 * S, text="You", fill=T["text"], font=self.f_small, tags="scene")

        for s in self.speakers:
            sx, sy = self.m2px(s["x"], s["y"])
            tag = f"who:{s['id']}"
            missing = s["device"] not in self.speaker_names
            if s["id"] == self.sel:
                c.create_oval(sx - 44 * S, sy - 44 * S, sx + 44 * S, sy + 44 * S, fill=blend(s["color"], T["floor"], .85),
                              outline=s["color"], width=2 * S, dash=(4, 3), tags=("scene", "drag", tag))
            # invisible hit area so small icons are easy to grab
            c.create_oval(sx - 38 * S, sy - 34 * S, sx + 38 * S, sy + 34 * S, fill="", outline="",
                          tags=("scene", "drag", tag))
            draw_icon(c, s["icon"], sx, sy, S, s["color"], tags=("scene", "drag", tag),
                      dim=not s["enabled"] or missing)
            ang, dist = geometry(s, self.listener)
            c.create_text(sx, sy + 36 * S, text=s["name"], fill=T["text"], font=("Segoe UI Semibold", 9),
                          tags=("scene", "drag", tag))
            sub = ("pick a device" if not s["device"] else "not connected") if missing else \
                ("off" if not s["enabled"] else position_label(s, ang, self.mode == "simple"))
            c.create_text(sx, sy + 50 * S, text=sub, font=("Segoe UI", 8),
                          fill=T["bad"] if missing else T["muted"], tags=("scene", "drag", tag))
        self.update_cards()
        self.update_info()
        self.apply_mix()

    def draw_waves(self):
        c, S = self.cv, self.S
        c.delete("waves")
        if not self.engine.running:
            return
        lx, ly = self.m2px(self.listener["x"], self.listener["y"])
        for s in self.speakers:
            o = self.outs.get(s["id"])
            if not o:
                continue
            sx, sy = self.m2px(s["x"], s["y"])
            facing = math.degrees(math.atan2(-(ly - sy), lx - sx))
            lit = min(3, int(o.level * 5.5))
            for i in range(3):
                rad = (40 + i * 11) * S
                col = blend(s["color"], T["floor"], .15 + .25 * i) if i < lit else blend(T["grid"], T["floor"], .3)
                c.create_arc(sx - rad, sy - rad, sx + rad, sy + rad, start=facing - 28, extent=56,
                             style="arc", outline=col, width=2.5 * S, tags="waves")
        c.tag_lower("waves", "scene")

    def on_press(self, e):
        item = self.cv.find_withtag("current")
        who = None
        if item:
            for t in self.cv.gettags(item[0]):
                if t.startswith("who:"):
                    who = t[4:]
        if who is None:
            return
        if who != "listener":
            self.select(int(who))
        self.drag = who

    def on_drag(self, e):
        if self.drag is None:
            return
        x = min(ROOM_W - 0.25, max(0.25, (e.x - self.M) / self.PX))
        y = min(ROOM_D - 0.25, max(0.25, (e.y - self.M) / self.PX))
        obj = self.listener if self.drag == "listener" else self.find(int(self.drag))
        obj["x"], obj["y"] = round(x, 2), round(y, 2)
        self.redraw()

    def on_release(self, e):
        if self.drag is not None:
            self.drag = None
            self.save_cfg(quiet=True)

    # ---------------- side panel
    def build_side(self, parent):
        S = self.S
        side = tk.Frame(parent, bg=T["bg"], width=340 * S)
        side.pack(side="left", fill="both", expand=True, padx=(14 * S, 0))

        head = tk.Frame(side, bg=T["bg"])
        head.pack(fill="x")
        tk.Label(head, text="Speakers", font=self.f_head, bg=T["bg"], fg=T["text"]).pack(side="left")
        self.button(head, "+ Add", self.add_speaker).pack(side="right")
        self.cards = tk.Frame(side, bg=T["bg"])
        self.cards.pack(fill="x", pady=(8 * S, 12 * S))

        ed = tk.Frame(side, bg=T["panel"], padx=14 * S, pady=12 * S)
        ed.pack(fill="both", expand=True)
        self.editor = ed

        def label(text):
            tk.Label(ed, text=text, font=self.f_small, bg=T["panel"], fg=T["muted"]).pack(anchor="w", pady=(8 * S, 2 * S))

        top = tk.Frame(ed, bg=T["panel"])
        top.pack(fill="x")
        self.name_var = tk.StringVar()
        tk.Entry(top, textvariable=self.name_var, font=self.f_head, bg=T["panel2"], fg=T["text"],
                 insertbackground=T["text"], relief="flat", width=18).pack(side="left", ipady=4 * S, ipadx=4 * S)
        self.en_var = tk.BooleanVar()
        tk.Checkbutton(top, text="On", variable=self.en_var, font=self.f_body, bg=T["panel"], fg=T["text"],
                       selectcolor=T["panel2"], activebackground=T["panel"], activeforeground=T["text"],
                       cursor="hand2").pack(side="right")

        label("Icon")
        icons = tk.Frame(ed, bg=T["panel"])
        icons.pack(anchor="w")
        self.icon_cvs = {}
        for kind in ICON_KINDS:
            ic = tk.Canvas(icons, width=54 * S, height=44 * S, bg=T["panel2"], highlightthickness=2 * S,
                           highlightbackground=T["panel2"], cursor="hand2")
            ic.pack(side="left", padx=(0, 5 * S))
            ic.bind("<Button-1>", lambda e, k=kind: self.set_field("icon", k))
            self.icon_cvs[kind] = ic

        label("Plays on device")
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(ed, textvariable=self.dev_var, state="readonly", values=self.speaker_names,
                                    font=self.f_body)
        self.dev_box.pack(fill="x")

        label("Role")
        self.role_var = tk.StringVar()
        ttk.Combobox(ed, textvariable=self.role_var, state="readonly", values=ROLES, font=self.f_body).pack(fill="x")

        self.vol_var = tk.DoubleVar()
        self.vol_lbl = self.slider_row(ed, "Volume", self.vol_var, 0, 200)
        self.delay_var = tk.DoubleVar()
        self.delay_lbl = self.slider_row(ed, "Sync delay", self.delay_var, 0, 600, nudge=True)
        tk.Label(ed, text="Bluetooth lags ~150–250 ms. Add delay to the faster\nspeakers (the TV) until "
                          "the sync clicks sound as one.", justify="left",
                 font=("Segoe UI", 8), bg=T["panel"], fg=T["muted"]).pack(anchor="w", pady=(2 * S, 0))

        self.info = tk.Label(ed, text="", justify="left", font=self.f_small, bg=T["panel2"], fg=T["text"],
                             padx=10 * S, pady=8 * S, anchor="w")
        self.info.pack(fill="x", pady=(12 * S, 0))

        bottom = tk.Frame(ed, bg=T["panel"])
        bottom.pack(fill="x", side="bottom", pady=(12 * S, 0))
        self.button(bottom, "Remove", self.remove_speaker, danger=True).pack(side="right")
        self.button(bottom, "🎯 Sync wizard", self.open_sync).pack(side="left")
        self.button(bottom, "Save", self.save_cfg).pack(side="left", padx=6 * S)

        for var, field in [(self.name_var, "name"), (self.en_var, "enabled"), (self.dev_var, "device"),
                           (self.role_var, "role"), (self.vol_var, "volume"), (self.delay_var, "delay")]:
            var.trace_add("write", lambda *_, v=var, f=field: self.on_field(v, f))

    def slider_row(self, parent, text, var, lo, hi, nudge=False):
        S = self.S
        tk.Label(parent, text=text, font=self.f_small, bg=T["panel"], fg=T["muted"]).pack(anchor="w", pady=(10 * S, 2 * S))
        row = tk.Frame(parent, bg=T["panel"])
        row.pack(fill="x")
        ttk.Scale(row, from_=lo, to=hi, variable=var).pack(side="left", fill="x", expand=True)
        lbl = tk.Label(row, width=7, font=self.f_body, bg=T["panel"], fg=T["text"], anchor="e")
        if nudge:
            for t, d in (("+", 5), ("−", -5)):
                self.button(row, t, lambda d=d: var.set(max(lo, min(hi, var.get() + d))), width=1).pack(side="right", padx=(3 * S, 0))
        lbl.pack(side="right")
        return lbl

    def on_field(self, var, field):
        if self.loading or self.sel is None:
            return
        try:
            val = var.get()
        except tk.TclError:
            return
        if field in ("volume", "delay"):
            val = round(val)
        self.set_field(field, val)

    def set_field(self, field, val):
        s = self.find(self.sel)
        if not s or s.get(field) == val:
            return
        s[field] = val
        if field in ("device", "enabled") and self.engine.running:
            self.restart()
        self.redraw()

    def select(self, sid):
        self.sel = sid
        s = self.find(sid)
        self.loading = True
        if s:
            self.name_var.set(s["name"])
            self.en_var.set(s["enabled"])
            self.dev_var.set(s["device"])
            self.role_var.set(s["role"])
            self.vol_var.set(s["volume"])
            self.delay_var.set(s["delay"])
        self.loading = False
        if hasattr(self, "mix"):
            self.redraw()

    def update_cards(self):
        S = self.S
        for w in self.cards.winfo_children():
            w.destroy()
        for s in self.speakers:
            selected = s["id"] == self.sel
            bg = T["panel2"] if selected else T["panel"]
            card = tk.Frame(self.cards, bg=bg, cursor="hand2")
            card.pack(fill="x", pady=2 * S)
            tk.Frame(card, bg=s["color"] if s["enabled"] else T["line"], width=4 * S).pack(side="left", fill="y")
            ic = tk.Canvas(card, width=58 * S, height=40 * S, bg=bg, highlightthickness=0)
            ic.pack(side="left", padx=6 * S, pady=4 * S)
            draw_icon(ic, s["icon"], 29 * S, 20 * S, 0.62 * S, s["color"], dim=not s["enabled"])
            txt = tk.Frame(card, bg=bg)
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=s["name"], font=self.f_head, bg=bg, fg=T["text"], anchor="w").pack(fill="x")
            missing = s["device"] not in self.speaker_names
            dev = s["device"]
            vol, muted = self.ep_levels.get(dev, (None, False))
            quiet = muted or (vol is not None and vol < 15)
            if not dev:
                sub = "⚠ pick a device below"
            elif missing:
                sub = "⚠ not connected · " + dev[:28]
            elif quiet:
                sub = "⚠ " + ("muted in Windows" if muted else f"Windows volume only {vol}%") + " — press Start"
            else:
                short = dev if len(dev) < 30 else dev[:28] + "…"
                sub = short + (f"  ·  {vol}%" if vol is not None else "")
            tk.Label(txt, text=sub, font=("Segoe UI", 8), bg=bg,
                     fg=T["bad"] if (missing or quiet or not dev) else T["muted"], anchor="w").pack(fill="x")
            level = tk.Label(card, text="●" if s["id"] in self.outs else "", font=self.f_small, bg=bg, fg=T["good"])
            level.pack(side="right", padx=8 * S)
            for w in (card, ic, txt, *txt.winfo_children(), level):
                w.bind("<Button-1>", lambda e, sid=s["id"]: self.select(sid))

    def update_info(self):
        s = self.find(self.sel)
        S = self.S
        for kind, ic in self.icon_cvs.items():
            ic.delete("all")
            col = s["color"] if s else T["accent"]
            draw_icon(ic, kind, 27 * S, 22 * S, 0.6 * S, col)
            ic.config(highlightbackground=col if s and s["icon"] == kind else T["panel2"])
        if not s:
            self.info.config(text="Add a speaker to get started.")
            return
        self.vol_lbl.config(text=f"{s['volume']}%")
        self.delay_lbl.config(text=f"{s['delay']} ms")
        ang, dist = geometry(s, self.listener)
        m = self.mix.get(s["id"])
        lines = [f"Position   {position_label(s, ang, self.mode == 'simple')}   ({ang:+.0f}°, {dist:.1f} m)"]
        if m:
            mat, extra = m
            share = mat.max(axis=0)
            names = {"FL": "Front L", "FR": "Front R", "C": "Center", "RL": "Rear L", "RR": "Rear R", "SUB": "Sub"}
            plays = [f"{names[ch]} {min(100, share[i] * 100 / max(1e-9, share.max())):.0f}%"
                     for i, ch in enumerate(CHANNELS) if share[i] > 0.12 * share.max()]
            lines.append("Plays      " + ", ".join(plays) if plays else "Plays      (nothing)")
            lines.append(f"Total delay  {s['delay'] + extra:.0f} ms  (sync {s['delay']} + distance {extra:.1f})")
        else:
            lines.append("Speaker is off.")
        self.info.config(text="\n".join(lines))

    # ---------------- speakers data
    def find(self, sid):
        return next((s for s in self.speakers if s["id"] == sid), None)

    def new_speaker(self, sid, name, device, icon, x, y, delay=0):
        return {"id": sid, "name": name, "device": device, "icon": icon, "role": AUTO, "volume": 100,
                "delay": delay, "x": x, "y": y, "enabled": True,
                "color": SPEAKER_COLORS[(sid - 1) % len(SPEAKER_COLORS)]}

    def default_speakers(self):
        tv = next((n for n in self.speaker_names if any(k in n.lower() for k in ("tv", "hdmi", "nvidia"))), "")
        jbls = [n for n in self.speaker_names if "jbl" in n.lower()] + ["", ""]
        return [
            self.new_speaker(1, "TV", tv, "TV", ROOM_W / 2, 0.55, delay=150),
            self.new_speaker(2, "JBL Left", jbls[0], "Bluetooth speaker", 1.7, 5.2),
            self.new_speaker(3, "JBL Right", jbls[1], "Bluetooth speaker", ROOM_W - 1.7, 5.2),
        ]

    def add_speaker(self):
        sid = self.next_id
        self.next_id += 1
        used = {s["device"] for s in self.speakers} | {self.source.get()}
        dev = next((n for n in self.speaker_names if n not in used), "")
        self.speakers.append(self.new_speaker(sid, f"Speaker {sid}", dev, "Bluetooth speaker", ROOM_W / 2, 2.2))
        if self.engine.running:
            self.restart()
        self.select(sid)

    def remove_speaker(self):
        s = self.find(self.sel)
        if not s:
            return
        self.speakers.remove(s)
        if self.engine.running:
            self.restart()
        self.select(self.speakers[0]["id"] if self.speakers else None)
        self.redraw()

    # ---------------- devices / config
    def refresh_devices(self):
        self.speaker_names = [s.name for s in sc.all_speakers()]

    def on_refresh(self):
        reset_endpoint_cache()
        self.refresh_devices()
        self.src_box.config(values=self.speaker_names)
        self.dev_box.config(values=self.speaker_names)
        self.auto_assign()
        self.select(self.sel)

    def auto_assign(self):
        """Give speakers that have no device yet any newly connected, unused output."""
        used = {s["device"] for s in self.speakers} | {self.source.get()}
        skip = ("steam", "cable", "virtual", "tv", "hdmi", "nvidia", "display")
        free = [n for n in self.speaker_names if n not in used and not any(k in n.lower() for k in skip)]
        free.sort(key=lambda n: "jbl" not in n.lower())
        for s in self.speakers:
            if not s["device"] and free:
                s["device"] = free.pop(0)

    def poll_devices(self):
        try:
            names = [s.name for s in sc.all_speakers()]
        except Exception:
            names = self.speaker_names
        if names != self.speaker_names:
            self.on_refresh()
            self.flash("Audio devices changed", T["accent"])
        self.check_default()
        self.read_levels()
        self.root.after(2000, self.poll_devices)

    def guess_source(self):
        for key in ("CABLE Input", "Steam Streaming Speakers", "Surround-Mixer", "Virtual"):
            for n in self.speaker_names:
                if key.lower() in n.lower():
                    return n
        return sc.default_speaker().name

    def load_cfg(self):
        try:
            with open(CONFIG) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cfg(self, quiet=False):
        data = {"source": self.source.get(), "listener": self.listener, "speakers": self.speakers,
                "autostart": getattr(self, "autostart", True), "mode": self.mode,
                "calibrated": self.calibrated,
                "master": round(self.master.get()), "gain": round(self.engine.gain * 100),
                "ambient": self.ambient.config(),
                "devices": self.web.pairing.export() if self.web else self.load_cfg().get("devices", [])}
        try:
            with open(CONFIG, "w") as f:
                json.dump(data, f, indent=2)
            if not quiet:
                self.flash("Setup saved", T["good"])
        except Exception as e:
            self.flash(f"Couldn't save: {e}", T["bad"])

    def flash(self, text, color):
        self.status.config(text=text, fg=color)
        self.root.after(2500, lambda: self.status.config(
            text="Playing" if self.engine.running else "Stopped",
            fg=T["good"] if self.engine.running else T["muted"]))

    # ---------------- engine
    def apply_mix(self):
        for sid, o in self.outs.items():
            s = self.find(sid)
            if not s or sid not in self.mix:
                continue
            mat, extra = self.mix[sid]
            o.matrix = mat
            o.volume = s["volume"] / 100
            o.delay_ms = s["delay"] + extra

    def toggle(self):
        # remember the choice: the app starts playing on launch if it was left playing
        self.autostart = not self.engine.running
        self.stop() if self.engine.running else self.start()
        self.save_cfg(quiet=True)

    def start(self, quiet=False):
        if self.engine.running:
            return
        self.silent_since = None
        src = self.source.get()
        self.refresh_devices()
        self.mix = compute_mix(self.speakers, self.listener, self.mode == "simple", self.calibrated)
        outs, skipped = {}, []
        for s in self.speakers:
            if not s["enabled"] or not s["device"]:
                continue
            if s["device"] == src:
                messagebox.showwarning("Surround Mixer",
                                       f"{s['name']} uses the source device. Pick a different device "
                                       "(otherwise you'd hear an echo).")
                return
            if s["device"] not in self.speaker_names:
                skipped.append(s["name"])
                continue
            mat, extra = self.mix[s["id"]]
            outs[s["id"]] = Output(s["id"], s["device"], mat, s["volume"] / 100, s["delay"] + extra)
        if not outs:
            if not quiet:
                messagebox.showinfo("Surround Mixer", "No connected speakers to play on.\n"
                                                      "Pair your Bluetooth speakers, then press ⟳.")
            return
        self.outs = outs
        # Windows may have a speaker muted or near 0% - put them all at the same level
        self.apply_master(unmute=True)
        self.engine.start(src, list(outs.values()))
        self.start_btn.config(text="■  Stop")
        self.status.config(text="Playing", fg=T["good"])
        if skipped:
            self.flash("Not connected: " + ", ".join(skipped), T["bad"])
        self.redraw()

    def stop(self):
        self.engine.stop()
        self.outs = {}
        self.start_btn.config(text="▶  Start")
        self.status.config(text="Stopped", fg=T["muted"])
        self.cv.delete("waves")
        self.redraw()

    def restart(self):
        self.stop()
        self.root.after(150, self.start)

    def open_sync(self):
        if getattr(self, "wizard", None) and self.wizard.winfo_exists():
            self.wizard.lift()
            return
        if not self.engine.running:
            self.start()
        if not self.engine.running:
            return
        self.wizard = SyncWizard(self)

    def tick(self):
        S = self.S
        self.meter.delete("all")
        if self.engine.running:
            w = min(1.0, self.engine.level) * 90 * S
            self.meter.create_rectangle(0, 0, w, 8 * S, fill=T["good"], outline="")
            # running but nothing coming in -> the player app is probably still on the old speaker
            now = time.time()
            if self.engine.level > 0.002:
                if self.silent_since is not None and self.status.cget("fg") == T["bad"]:
                    self.status.config(text="Playing", fg=T["good"])
                self.silent_since = None
            elif self.silent_since is None:
                self.silent_since = now
            elif now - self.silent_since > 6 and not self.banner_shown:
                self.status.config(text="No sound coming in — restart the app you play from",
                                   fg=T["bad"])
        visible = self.root.state() != "iconic"
        if visible:
            self.draw_waves()                      # no drawing while minimized
        self.web_tick()
        if self.ambient.error:
            self.flash(f"Lights: {self.ambient.error}", T["bad"])
            self.ambient.error = None
        errs = [self.engine.error] + [o.error for o in self.outs.values()]
        errs = [e for e in errs if e]
        if errs:
            self.stop()
            messagebox.showerror("Surround Mixer", "\n".join(errs) +
                                 "\n\nIs the Bluetooth speaker still connected? Press ⟳ and try again.")
        self.root.after(80 if visible else 150, self.tick)

    def on_close(self):
        self.engine.stop()
        self.save_cfg(quiet=True)
        self.ambient.stop()                        # lamps go back to how they were
        if self.web:
            self.web.shutdown()
            if self.web.secure:
                self.web.secure.shutdown()
        self.root.destroy()

    # ---------------- phone remote (runs on the Tk thread)
    def submit(self, cmd):
        """Called from web server threads."""
        self.web_q.put(cmd)

    def web_tick(self):
        changed = False
        while True:
            try:
                cmd = self.web_q.get_nowait()
            except queue.Empty:
                break
            try:
                result = self.web_cmd(cmd)
            except Exception as e:
                result = {"error": str(e)}
            changed |= result is True
            if "_reply" in cmd:                     # a web request is waiting for the answer
                cmd["_reply"].put(result)
        if changed:
            self.dirty_at = time.time()
        if self.dirty_at and time.time() - self.dirty_at > 1.5:      # save once things settle
            self.dirty_at = None
            self.save_cfg(quiet=True)
        n = getattr(self, "_snap_n", 0)
        self._snap_n = n + 1
        if n % 3 == 0:
            self.sync_win_volume()                         # follow the keys / taskbar / phone
            self.update_pairbar()                          # a phone asking to connect?
            self.snapshot = self.build_snapshot()

    def build_snapshot(self):
        simple = self.mode == "simple"
        sp = []
        for s in self.speakers:
            ang = geometry(s, self.listener)[0]
            o = self.outs.get(s["id"])
            sp.append({k: s[k] for k in ("id", "name", "icon", "color", "enabled", "volume", "delay", "x", "y", "device")}
                      | {"connected": s["device"] in self.speaker_names, "level": round(o.level, 3) if o else 0,
                         "label": position_label(s, ang, simple)})
        return {"running": self.engine.running, "mode": self.mode, "master": round(self.master.get()),
                "muted": self.muted_all, "gain": round(self.engine.gain * 100),
                "win_volume": self.win_volume, "win_muted": self.win_muted,
                "level": round(self.engine.level, 3) if self.engine.running else 0,
                "source": self.source.get(), "sources": self.speaker_names,
                "room": [ROOM_W, ROOM_D], "listener": self.listener, "speakers": sp,
                "ambient": {"on": self.ambient.running, "brightness": self.ambient.brightness,
                            "lamps": len(self.ambient.active()),
                            "colors": ["#%02x%02x%02x" % self.ambient.colors[l["ip"]]
                                       for l in self.ambient.active() if l["ip"] in self.ambient.colors]}}

    def web_cmd(self, c):
        t = c.get("type")
        num = lambda v, lo, hi: max(lo, min(hi, float(v)))
        if t == "start":
            self.autostart = True
            self.start(quiet=True)
        elif t == "stop":
            self.autostart = False
            self.stop()
        elif t in ("master", "master_step"):
            v = c.get("value", 0) if t == "master" else self.master.get() + c.get("delta", 0)
            self.master.set(round(num(v, 0, 100)))
            self.apply_master()
        elif t == "mute_all":
            self.muted_all = bool(c.get("on"))
            self.apply_master()
            self.flash("Muted from phone" if self.muted_all else "Unmuted", T["accent"])
        elif t == "cal_start":
            return self.cal_begin(int(num(c.get("pass", 0), 0, calibrate.MAX_PASSES - 1)))
        elif t == "cal_result":
            return self.cal_apply(c.get("results", []), c.get("quality", ""))
        elif t == "cal_stop":
            self.cal_plan = None
            self.engine.cal_request = None
            self.engine.cal_cancel = True
            self.flash("Calibration stopped", T["muted"])
        elif t == "ambient":
            self.set_lights(bool(c.get("on")))
        elif t == "ambient_brightness":
            self.ambient.brightness = int(num(c.get("value", 100), 1, 100))
        elif t == "paired":                         # a phone entered the one-time PIN
            self.update_pairbar()
            self.flash(f"{c.get('name', 'Phone')} connected", T["good"])
        elif t == "win_volume":                     # phone slider -> Windows' own volume
            self.set_win_volume(round(num(c.get("value", 50), 0, 100)))
            self.sync_win_volume(force=True)
            self.snapshot = self.build_snapshot()
            return False
        elif t == "mode" and c.get("mode") in ("surround", "simple"):
            self.set_mode(c["mode"])
        elif t == "gain":
            self.engine.gain = num(c.get("value", 100), 0, 200) / 100
        elif t == "source" and c.get("name") in self.speaker_names:
            self.source.set(c["name"])
            if self.engine.running:
                self.restart()
        elif t == "listener":
            self.listener["x"] = round(num(c.get("x", 4), 0.25, ROOM_W - 0.25), 2)
            self.listener["y"] = round(num(c.get("y", 4), 0.25, ROOM_D - 0.25), 2)
            self.redraw()
        elif t == "speaker":
            s = self.find(c.get("id"))
            field, v = c.get("field"), c.get("value")
            if not s or field not in ("volume", "delay", "enabled", "x", "y"):
                return False
            if field == "volume":
                s["volume"] = int(num(v, 0, 200))
            elif field == "delay":
                s["delay"] = int(num(v, 0, 600))
            elif field == "enabled":
                s["enabled"] = bool(v)
                if self.engine.running:
                    self.restart()
            else:
                lim = ROOM_W if field == "x" else ROOM_D
                s[field] = round(num(v, 0.25, lim - 0.25), 2)
            if s["id"] == self.sel:
                self.select(self.sel)          # refresh the editor panel
            else:
                self.redraw()
        else:
            return False
        return True

    def forget_phones(self):
        if not messagebox.askyesno("Phone remote", "Disconnect every paired phone?\n"
                                                   "They'll need a new one-time PIN to connect again.",
                                   parent=self.phone_win):
            return
        self.web.pairing.forget_all()
        self.save_cfg(quiet=True)
        self.devs_lbl.config(text="Connected phones: 0")
        self.flash("All phones forgotten", T["muted"])

    # ---------------- auto-calibration with the phone's microphone
    def cal_begin(self, pass_no=0):
        """One measuring pass. Pass 0 is the long first measurement, later passes fine-tune."""
        if not self.engine.running:
            self.start(quiet=True)
        if not self.engine.running or not self.outs:
            return {"error": "No speakers are playing. Connect them and press Start on the PC."}
        order = list(self.outs)
        for i, sid in enumerate(order):
            self.outs[sid].cal_index = i
        rounds = calibrate.rounds_for(pass_no)
        self.cal_plan = {"id": secrets.token_hex(6), "ids": order, "pass": pass_no, "rounds": rounds,
                         "eff": {sid: self.outs[sid].delay_ms for sid in order},      # delay incl. distance
                         "vol": {sid: self.find(sid)["volume"] for sid in order}}
        self.engine.cal_request = (len(order), rounds)
        self.flash("Calibrating — keep the room quiet" if pass_no == 0 else f"Fine-tuning (pass {pass_no + 1})",
                   T["accent"])
        return {"id": self.cal_plan["id"], "n": len(order), "record_s": calibrate.record_seconds(len(order), rounds),
                "names": [self.find(sid)["name"] for sid in order], "pass": pass_no,
                "max_passes": calibrate.MAX_PASSES, "tolerance_ms": calibrate.TOLERANCE_MS}

    def cal_apply(self, results, quality):
        """Line every speaker up on the measured arrival times and even out the levels.

        The first pass sets everything outright; later passes measure what's left and
        correct most of it (FINE_GAIN), so the speakers close in on one click.
        """
        plan, self.cal_plan = self.cal_plan, None
        if not plan:
            return {"error": "That calibration already finished. Start a new one."}
        first = plan["pass"] == 0
        pairs = list(zip(plan["ids"], results))
        heard = [(sid, r) for sid, r in pairs if r]
        missing = [self.find(sid)["name"] for sid, r in pairs if not r]
        if len(heard) < min(2, len(pairs)):
            return {"error": "Couldn't hear enough speakers (" + ", ".join(missing) + "). "
                             "Turn the volume up, keep the room quiet and try again.", "quality": quality}
        arrivals = [r["arrival_ms"] for _, r in heard]
        latest, mid = max(arrivals), float(np.median(arrivals))
        spread = latest - min(arrivals)                      # how far from one click this pass heard it
        gain = 1.0 if first else calibrate.FINE_GAIN
        eff = {}
        for sid, r in heard:
            need = latest - r["arrival_ms"]                  # more delay this speaker needs
            eff[sid] = plan["eff"][sid] + (gain * need if first or need >= 0.5 else 0)
        low = min(eff.values())
        levels = sorted(r["level_db"] for _, r in heard)
        target = levels[len(levels) // 2]
        report = []
        for sid, r in pairs:
            s = self.find(sid)
            if not r:
                report.append({"name": s["name"], "heard": False})
                continue
            old_delay, old_vol = s["delay"], s["volume"]
            s["delay"] = int(round(max(0, min(600, eff[sid] - low))))
            db = target - r["level_db"]
            if not first:
                db = 0.5 * db if abs(db) > 1 else 0          # levels: small, calm steps after the first pass
            s["volume"] = int(round(max(30, min(200, plan["vol"][sid] * 10 ** (db / 20)))))
            report.append({"name": s["name"], "heard": True, "delay": s["delay"], "old_delay": old_delay,
                           "volume": s["volume"], "old_volume": old_vol,
                           "offset_ms": round(r["arrival_ms"] - mid, 1), "spread_ms": round(r["spread_ms"], 1)})
        converged = spread <= calibrate.TOLERANCE_MS
        self.calibrated = True
        self.mix = compute_mix(self.speakers, self.listener, self.mode == "simple", self.calibrated)
        self.apply_mix()
        self.select(self.sel)
        self.save_cfg(quiet=True)
        if converged or plan["pass"] + 1 >= calibrate.MAX_PASSES:
            self.flash(f"Speakers calibrated: together within {spread:.1f} ms", T["good"])
        return {"ok": True, "speakers": report, "missing": missing, "quality": quality, "pass": plan["pass"],
                "spread_ms": round(spread, 1), "converged": converged,
                "done": converged or plan["pass"] + 1 >= calibrate.MAX_PASSES}

    # ---------------- ambient lights (Govee lamps follow the screen)
    def set_lights(self, on):
        self.ambient.want_on = on
        if on and not self.ambient.start():
            self.ambient.want_on = False
        elif not on:
            self.ambient.stop()
        self.save_cfg(quiet=True)

    def open_lights(self):
        if getattr(self, "lights_win", None) and self.lights_win.winfo_exists():
            self.lights_win.lift()
            return
        self.lights_win = LightsWindow(self)

    def open_phone(self):
        if getattr(self, "phone_win", None) and self.phone_win.winfo_exists():
            self.phone_win.destroy()                       # reopen: every opening gets a fresh QR code
        S = self.S
        w = tk.Toplevel(self.root, bg=T["bg"], padx=26 * S, pady=22 * S)
        self.phone_win = w
        w.title("Phone remote")
        w.resizable(False, False)
        w.transient(self.root)
        if os.path.exists(ICON):
            try:
                w.iconbitmap(ICON)
            except Exception:
                pass
        tk.Label(w, text="📱  Phone remote", font=self.f_title, bg=T["bg"], fg=T["text"]).pack(anchor="w")
        if not self.web:
            tk.Label(w, text="The web server couldn't start (is another copy of the app open?).",
                     font=self.f_body, bg=T["bg"], fg=T["bad"]).pack(anchor="w", pady=10 * S)
            return
        url = f"http://{webremote.lan_ip()}:{webremote.PORT}"
        tk.Label(w, text="Scan with your phone's camera to connect it (this code works once, for 5 minutes).\n"
                         "Phone and PC must be on the same Wi-Fi.",
                 justify="left", font=self.f_body, bg=T["bg"], fg=T["muted"]).pack(anchor="w", pady=(2 * S, 14 * S))
        try:
            import qrcode
            from PIL import ImageTk
            rid, pin = self.web.pairing.make_qr()
            img = qrcode.make(f"{url}/?pair={rid}.{pin}", border=2, box_size=8).convert("RGB")
            img = img.resize((int(240 * S), int(240 * S)))
            self._qr = ImageTk.PhotoImage(img)
            tk.Label(w, image=self._qr, bg="#ffffff", bd=0).pack(pady=(0, 16 * S))
        except Exception:
            pass
        info = tk.Frame(w, bg=T["panel"], padx=16 * S, pady=12 * S)
        info.pack(fill="x")
        row = tk.Frame(info, bg=T["panel"])
        row.pack(fill="x", pady=2 * S)
        tk.Label(row, text="Address", width=8, anchor="w", font=self.f_body, bg=T["panel"], fg=T["muted"]).pack(side="left")
        tk.Label(row, text=url, font=("Segoe UI Semibold", 13), bg=T["panel"], fg=T["text"]).pack(side="left")
        tk.Label(info, text="Or type the address on the phone — a one-time PIN then appears on this PC.",
                 font=self.f_small, bg=T["panel"], fg=T["muted"]).pack(anchor="w", pady=(4 * S, 0))

        devs = tk.Frame(w, bg=T["bg"])
        devs.pack(fill="x", pady=(14 * S, 0))
        n = len(self.web.pairing.export())
        self.devs_lbl = tk.Label(devs, text=f"Connected phones: {n}", font=self.f_body, bg=T["bg"], fg=T["text"])
        self.devs_lbl.pack(side="left")
        self.button(devs, "Forget all phones", self.forget_phones, danger=True).pack(side="right")
        tk.Label(w, text="Tip: in Safari tap Share › Add to Home Screen to get it as an app.\n"
                         "If Windows asks about network access for Python, choose Allow (private networks).",
                 justify="left", font=self.f_small, bg=T["bg"], fg=T["muted"]).pack(anchor="w", pady=(14 * S, 0))
        w.bind("<Destroy>", lambda e: e.widget is w and setattr(self, "phone_win", None))
        w.lift()
        w.attributes("-topmost", True)                     # make sure it isn't hidden behind other windows
        w.after(400, lambda: w.attributes("-topmost", False))
        w.focus_force()


class LightsWindow(tk.Toplevel):
    """Govee lamps that take on the screen's colours: find them, pick sides, tune the look."""

    PW, PH = 300, 169                     # preview of the screen (16:9)

    def __init__(self, app):
        super().__init__(app.root, bg=T["bg"])
        self.app, self.amb = app, app.ambient
        S = app.S
        self.title("Ambient lights")
        self.transient(app.root)
        self.resizable(False, False)
        if os.path.exists(ICON):
            try:
                self.iconbitmap(ICON)
            except Exception:
                pass
        self.jobs = queue.Queue()         # results from background scans
        self.preview = None               # last screenshot when the lights are off
        self.preview_at = 0
        pad = dict(padx=22 * S)

        tk.Label(self, text="💡  Ambient lights", font=app.f_title, bg=T["bg"], fg=T["text"]).pack(
            anchor="w", pady=(18 * S, 2 * S), **pad)
        tk.Label(self, text="Govee lamps take on the colours of the screen, like Ambilight.",
                 font=app.f_body, bg=T["bg"], fg=T["muted"]).pack(anchor="w", **pad)

        self.cv = tk.Canvas(self, width=(self.PW + 170) * S, height=(self.PH + 24) * S, bg=T["bg"],
                            highlightthickness=0)
        self.cv.pack(pady=(12 * S, 6 * S), **pad)

        row = tk.Frame(self, bg=T["bg"])
        row.pack(fill="x", **pad)
        self.toggle_btn = app.button(row, "Turn on", self.toggle, accent=True, width=10)
        self.toggle_btn.pack(side="left")
        self.status = tk.Label(row, font=app.f_body, bg=T["bg"], fg=T["muted"], anchor="w")
        self.status.pack(side="left", padx=12 * S)

        # ---- look
        card = tk.Frame(self, bg=T["panel"], padx=16 * S, pady=10 * S)
        card.pack(fill="x", pady=(12 * S, 0), **pad)
        tk.Label(card, text="Screen to follow", font=app.f_small, bg=T["panel"], fg=T["muted"]).pack(anchor="w")
        self.mons = ambient.monitors()
        self.screen_var = tk.StringVar()
        self.screen_box = ttk.Combobox(card, textvariable=self.screen_var, state="readonly", font=app.f_body,
                                       values=[m["name"] for m in self.mons])
        self.screen_box.pack(fill="x", pady=(2 * S, 0))
        rect = self.amb.screen_rect()
        self.screen_var.set(next((m["name"] for m in self.mons if m["rect"] == rect), self.mons[0]["name"]))
        self.screen_box.bind("<<ComboboxSelected>>", lambda e: self.on_screen())
        self.sliders = {}
        for key, text, lo, hi, fmt in (("brightness", "Brightness", 1, 100, "{}%"),
                                       ("smooth", "Smoothness", 0, 100, "{}"),
                                       ("boost", "Colour boost", 100, 200, "{}%")):
            self.slider(card, key, text, lo, hi, fmt)

        # ---- lamps
        lc = tk.Frame(self, bg=T["panel"], padx=16 * S, pady=10 * S)
        lc.pack(fill="x", pady=(12 * S, 0), **pad)
        head = tk.Frame(lc, bg=T["panel"])
        head.pack(fill="x")
        tk.Label(head, text="Lamps", font=app.f_head, bg=T["panel"], fg=T["text"]).pack(side="left")
        self.find_btn = app.button(head, "🔍  Find lamps", self.find)
        self.find_btn.pack(side="right")
        self.lamp_box = tk.Frame(lc, bg=T["panel"])
        self.lamp_box.pack(fill="x", pady=(8 * S, 0))
        add = tk.Frame(lc, bg=T["panel"])
        add.pack(fill="x", pady=(8 * S, 0))
        tk.Label(add, text="Add by IP", font=app.f_small, bg=T["panel"], fg=T["muted"]).pack(side="left")
        self.ip_var = tk.StringVar()
        ip = tk.Entry(add, textvariable=self.ip_var, font=app.f_body, bg=T["panel2"], fg=T["text"],
                      insertbackground=T["text"], relief="flat", width=16)
        ip.pack(side="left", padx=8 * S, ipady=3 * S)
        ip.bind("<Return>", lambda e: self.add_ip())
        app.button(add, "Add", self.add_ip).pack(side="left")

        tk.Label(self, text="First, in the Govee Home app open each lamp › ⚙ Settings › turn on LAN Control.\n"
                            "Blink shows which lamp is which. Left/Right = which side of the picture it copies.\n"
                            "Protected video (Netflix etc. in some browsers) can show up black to screen capture.",
                 justify="left", font=app.f_small, bg=T["bg"], fg=T["muted"]).pack(anchor="w", pady=(10 * S, 18 * S), **pad)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.render_lamps()
        self.tick()
        if not self.amb.lamps:
            self.after(300, self.find)                       # first time: go looking right away

    def slider(self, parent, key, text, lo, hi, fmt):
        S = self.app.S
        row = tk.Frame(parent, bg=T["panel"])
        row.pack(fill="x", pady=(8 * S, 0))
        tk.Label(row, text=text, width=12, anchor="w", font=self.app.f_small, bg=T["panel"],
                 fg=T["muted"]).pack(side="left")
        lbl = tk.Label(row, width=5, anchor="e", font=self.app.f_body, bg=T["panel"], fg=T["text"])
        lbl.pack(side="right")
        var = tk.DoubleVar(value=getattr(self.amb, key))

        def moved(_):
            v = round(var.get())
            setattr(self.amb, key, v)
            lbl.config(text=fmt.format(v))
            self.app.dirty_at = time.time()                   # saved once the slider settles
        ttk.Scale(row, from_=lo, to=hi, variable=var, command=moved).pack(side="left", fill="x", expand=True)
        lbl.config(text=fmt.format(getattr(self.amb, key)))

    def render_lamps(self):
        S, app = self.app.S, self.app
        for w in self.lamp_box.winfo_children():
            w.destroy()
        self.swatches = {}
        if not self.amb.lamps:
            tk.Label(self.lamp_box, text="No lamps yet. Press Find lamps.", font=app.f_body, bg=T["panel"],
                     fg=T["muted"]).pack(anchor="w")
        for lamp in self.amb.lamps:
            r = tk.Frame(self.lamp_box, bg=T["panel2"], padx=8 * S, pady=5 * S)
            r.pack(fill="x", pady=2 * S)
            sw = tk.Canvas(r, width=20 * S, height=20 * S, bg=T["panel2"], highlightthickness=0)
            sw.pack(side="left")
            self.swatches[lamp["ip"]] = sw
            txt = tk.Frame(r, bg=T["panel2"])
            txt.pack(side="left", padx=8 * S)
            tk.Label(txt, text=lamp.get("name") or "Govee lamp", font=app.f_body, bg=T["panel2"],
                     fg=T["text"], anchor="w").pack(anchor="w")
            tk.Label(txt, text=lamp["ip"], font=("Segoe UI", 8), bg=T["panel2"], fg=T["muted"],
                     anchor="w").pack(anchor="w")
            tk.Button(r, text="✕", command=lambda l=lamp: self.remove(l), relief="flat", bd=0, cursor="hand2",
                      bg=T["panel2"], fg=T["bad"], activebackground=T["panel2"], font=app.f_body).pack(side="right")
            app.button(r, "Blink", lambda ip=lamp["ip"]: self.amb.identify(ip)).pack(side="right", padx=4 * S)
            en = tk.BooleanVar(value=lamp.get("enabled", True))
            tk.Checkbutton(r, text="On", variable=en, font=app.f_small, bg=T["panel2"], fg=T["text"],
                           selectcolor=T["panel"], activebackground=T["panel2"], activeforeground=T["text"],
                           command=lambda l=lamp, v=en: self.set_lamp(l, "enabled", v.get())).pack(side="right")
            zone = tk.StringVar(value=lamp.get("zone", "Whole screen"))
            zb = ttk.Combobox(r, textvariable=zone, values=ambient.ZONE_NAMES, state="readonly", width=12,
                              font=app.f_small)
            zb.pack(side="right", padx=6 * S)
            zb.bind("<<ComboboxSelected>>", lambda e, l=lamp, v=zone: self.set_lamp(l, "zone", v.get()))

    def set_lamp(self, lamp, field, value):
        lamp[field] = value
        self.app.save_cfg(quiet=True)

    def remove(self, lamp):
        self.amb.lamps.remove(lamp)
        if not self.amb.active() and self.amb.running:
            self.app.set_lights(False)
        self.render_lamps()
        self.app.save_cfg(quiet=True)

    def add_lamps(self, found):
        """Add newly found lamps; the first goes on the left of the TV, the next on the right."""
        have = {l["ip"] for l in self.amb.lamps}
        new = 0
        for d in found:
            if d["ip"] in have:
                continue
            zones = {l.get("zone") for l in self.amb.lamps}
            zone = "Left" if "Left" not in zones else "Right" if "Right" not in zones else "Whole screen"
            self.amb.lamps.append({"ip": d["ip"], "name": f"Govee {d.get('sku') or 'lamp'}".strip(),
                                   "sku": d.get("sku", ""), "zone": zone, "enabled": True})
            new += 1
        self.render_lamps()
        self.app.save_cfg(quiet=True)
        return new

    def background(self, fn, done):
        """Run fn() off the Tk thread; done(result or exception) runs back on it."""
        def run():
            try:
                self.jobs.put((done, fn()))
            except Exception as e:
                self.jobs.put((done, e))
        threading.Thread(target=run, daemon=True).start()

    def find(self):
        self.find_btn.config(state="disabled", text="Looking…")
        known = [l["ip"] for l in self.amb.lamps]
        self.background(lambda: ambient.scan(self.amb.local_ip, known), self.found)

    def found(self, res):
        self.find_btn.config(state="normal", text="🔍  Find lamps")
        if isinstance(res, Exception):
            self.app.flash(f"Couldn't look for lamps: {res}", T["bad"])
            return
        new = self.add_lamps(res)
        if res:
            self.app.flash(f"Found {len(res)} lamp{'s' * (len(res) != 1)}" + (f" ({new} new)" if new else ""),
                           T["good"])
        else:
            messagebox.showinfo("Ambient lights",
                                "No Govee lamps answered.\n\n"
                                "• In the Govee Home app, open each lamp › ⚙ Settings › turn on LAN Control.\n"
                                "• The lamps and this PC must be on the same network.\n"
                                "• If Windows asked about network access for Python, allow it.\n\n"
                                "You can also type a lamp's IP address (your router lists it) under Add by IP.",
                                parent=self)

    def add_ip(self):
        ip = self.ip_var.get().strip()
        try:
            socket.inet_aton(ip)
            if ip.count(".") != 3:
                raise OSError
        except OSError:
            self.app.flash("That isn't an IP address (like 192.168.1.42)", T["bad"])
            return
        self.ip_var.set("")

        def done(res):
            info = res[0] if isinstance(res, list) and res else {"ip": ip}
            self.add_lamps([info])
            if not isinstance(res, list) or not res:
                self.app.flash(f"Added {ip}, but it didn't answer yet: is LAN Control on?", T["bad"])
        self.background(lambda: ambient.scan(self.amb.local_ip, [ip], timeout=1.2), done)

    def on_screen(self):
        m = next((m for m in self.mons if m["name"] == self.screen_var.get()), None)
        if m:
            self.amb.screen = m["rect"]
            self.preview_at = 0
            self.app.save_cfg(quiet=True)

    def toggle(self):
        self.app.set_lights(not self.amb.running)
        if self.amb.error:
            self.app.flash(self.amb.error, T["bad"])
            self.amb.error = None

    # ---- live preview: the screen, with each lamp glowing on its side
    def tick(self):
        if not self.winfo_exists():
            return
        while True:
            try:
                done, res = self.jobs.get_nowait()
            except queue.Empty:
                break
            done(res)
        on = self.amb.running
        frame = self.amb.frame if on else None
        if frame is None:
            if time.time() - self.preview_at > 1.0:            # lights off: still show what they'd do
                self.preview_at = time.time()
                g = ambient.Grabber(self.amb.screen_rect())
                img = g.grab()
                g.close()
                self.preview = None if img is None else (img * 255).astype(np.uint8)
            frame = self.preview
        colors = {}
        for lamp in self.amb.lamps:
            if on and lamp["ip"] in self.amb.colors:
                colors[lamp["ip"]] = "#%02x%02x%02x" % self.amb.colors[lamp["ip"]]
            elif frame is not None and lamp.get("enabled", True):
                c = ambient.zone_color(frame.astype(np.float32) / 255, lamp.get("zone"), self.amb.boost / 100)
                colors[lamp["ip"]] = "#%02x%02x%02x" % tuple(int(v * 255) for v in c)
        self.draw(frame, colors)
        for ip, sw in self.swatches.items():
            sw.delete("all")
            s = 20 * self.app.S
            sw.create_oval(2, 2, s - 2, s - 2, fill=colors.get(ip, T["line"]), outline="")
        self.toggle_btn.config(text="Turn off" if on else "Turn on")
        self.toggle_btn._bg = T["panel2"] if on else T["accent"]
        self.toggle_btn.config(bg=self.toggle_btn._bg)
        n = len(self.amb.active())
        self.status.config(text=f"On  ·  {n} lamp{'s' * (n != 1)}  ·  {self.amb.fps:.0f} updates/s" if on
                           else "Off  ·  preview of what the lamps would show", fg=T["good"] if on else T["muted"])
        self.after(100, self.tick)

    def draw(self, frame, colors):
        c, S = self.cv, self.app.S
        c.delete("all")
        W, H = self.PW * S, self.PH * S
        x0, y0 = 85 * S, 6 * S
        sides = {"Left": [], "Right": [], "Whole screen": []}
        for lamp in self.amb.lamps:
            sides.get(lamp.get("zone"), sides["Whole screen"]).append(lamp)
        for side, cx in (("Left", 40 * S), ("Right", x0 + W + 45 * S)):
            lamps = sides[side] or []
            for i, lamp in enumerate(lamps):
                cy = y0 + H / 2 + (i - (len(lamps) - 1) / 2) * 60 * S
                col = colors.get(lamp["ip"])
                if col:
                    for r, fade in ((34, 0.75), (26, 0.5)):     # soft glow around the lamp
                        c.create_oval(cx - r * S, cy - r * S, cx + r * S, cy + r * S, outline="",
                                      fill=blend(col, T["bg"], fade))
                c.create_oval(cx - 18 * S, cy - 18 * S, cx + 18 * S, cy + 18 * S, outline="",
                              fill=col or T["panel2"])
                c.create_text(cx, cy + 44 * S, text=(lamp.get("name") or "")[:14], fill=T["muted"],
                              font=("Segoe UI", 8))
        rrect(c, x0 - 6 * S, y0 - 6 * S, x0 + W + 6 * S, y0 + H + 6 * S, 8 * S, fill="#23262f", outline=T["wall"])
        if frame is not None:
            from PIL import Image, ImageTk
            img = Image.fromarray(frame).resize((int(W), int(H)), Image.BILINEAR)
            self._img = ImageTk.PhotoImage(img)
            c.create_image(x0, y0, image=self._img, anchor="nw")
        else:
            c.create_text(x0 + W / 2, y0 + H / 2, text="(screen not visible)", fill=T["muted"], font=self.app.f_small)
        for f in (ambient.ZONES["Left"][1], ambient.ZONES["Right"][0]):    # what each side copies
            c.create_line(x0 + W * f, y0, x0 + W * f, y0 + H, fill="#ffffff", dash=(3, 4))
        for i, lamp in enumerate(sides["Whole screen"]):
            cx = x0 + W / 2 + (i - (len(sides["Whole screen"]) - 1) / 2) * 30 * S
            c.create_oval(cx - 8 * S, y0 + H + 9 * S, cx + 8 * S, y0 + H + 25 * S, outline="",
                          fill=colors.get(lamp["ip"], T["line"]))


class SyncWizard(tk.Toplevel):
    """Line speakers up by ear: two speakers click together, slide until it's one click.

    Works with offsets relative to a reference speaker, so you never need to know
    which one is slower; at the end the smallest delay is moved to 0.
    """

    def __init__(self, app):
        super().__init__(app.root, bg=T["bg"])
        self.app = app
        S = app.S
        self.title("Sync speakers")
        self.transient(app.root)
        self.resizable(False, False)
        if os.path.exists(ICON):
            try:
                self.iconbitmap(ICON)
            except Exception:
                pass

        self.sp = [s for s in app.speakers if s["id"] in app.outs]
        # reference = first Bluetooth speaker (they're the slowest), else the first one
        bt = [s for s in self.sp if s["icon"] == "Bluetooth speaker"]
        self.ref = (bt or self.sp)[0]
        self.others = [s for s in self.sp if s is not self.ref]
        self.rel = {s["id"]: s["delay"] - self.ref["delay"] for s in self.sp}
        self.step = 0

        pad = dict(padx=22 * S)
        tk.Label(self, text="🎯  Sync speakers", font=app.f_title, bg=T["bg"], fg=T["text"]).pack(
            anchor="w", pady=(18 * S, 4 * S), **pad)
        tk.Label(self, text="Music is paused while this is open. Sit in your normal spot.",
                 font=app.f_body, bg=T["bg"], fg=T["muted"]).pack(anchor="w", **pad)

        card = tk.Frame(self, bg=T["panel"], padx=18 * S, pady=16 * S)
        card.pack(fill="x", pady=14 * S, **pad)
        self.step_lbl = tk.Label(card, font=app.f_small, bg=T["panel"], fg=T["accent"])
        self.step_lbl.pack(anchor="w")
        self.pair_cv = tk.Canvas(card, width=420 * S, height=86 * S, bg=T["panel"], highlightthickness=0)
        self.pair_cv.pack(pady=(6 * S, 4 * S))
        self.howto = tk.Label(card, font=app.f_body, bg=T["panel"], fg=T["text"], justify="left",
                              wraplength=420 * S)
        self.howto.pack(anchor="w")

        self.slider_box = tk.Frame(card, bg=T["panel"])
        self.slider_box.pack(fill="x", pady=(14 * S, 0))
        ends = tk.Frame(self.slider_box, bg=T["panel"])
        ends.pack(fill="x")
        self.left_end = tk.Label(ends, font=app.f_small, bg=T["panel"], fg=T["muted"])
        self.left_end.pack(side="left")
        self.right_end = tk.Label(ends, font=app.f_small, bg=T["panel"], fg=T["muted"])
        self.right_end.pack(side="right")
        self.off = tk.DoubleVar()
        ttk.Scale(self.slider_box, from_=-400, to=400, variable=self.off,
                  command=lambda _: self.on_slide()).pack(fill="x", pady=4 * S)
        row = tk.Frame(self.slider_box, bg=T["panel"])
        row.pack()
        for txt, d in (("−20", -20), ("−5", -5)):
            app.button(row, txt, lambda d=d: self.nudge(d)).pack(side="left", padx=2 * S)
        self.off_lbl = tk.Label(row, width=10, font=app.f_head, bg=T["panel"], fg=T["text"])
        self.off_lbl.pack(side="left", padx=8 * S)
        for txt, d in (("+5", 5), ("+20", 20)):
            app.button(row, txt, lambda d=d: self.nudge(d)).pack(side="left", padx=2 * S)

        nav = tk.Frame(self, bg=T["bg"])
        nav.pack(fill="x", pady=(0, 18 * S), **pad)
        self.back_btn = app.button(nav, "◀ Back", self.back)
        self.back_btn.pack(side="left")
        self.next_btn = app.button(nav, "Next ▶", self.next, accent=True)
        self.next_btn.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.finish)
        self.show()
        self.keep_clicking()

    # which speakers click right now
    def clicking(self):
        if self.step < len(self.others):
            return {self.ref["id"], self.others[self.step]["id"]}
        return {s["id"] for s in self.sp}

    def keep_clicking(self):
        if not self.winfo_exists():
            return
        on = self.clicking()
        for sid, o in self.app.outs.items():      # re-applied: outputs can be rebuilt on restart
            o.click_mode = True
            o.click_on = sid in on
        self.after(200, self.keep_clicking)

    def show(self):
        S, app = self.app.S, self.app
        c = self.pair_cv
        c.delete("all")
        last = self.step >= len(self.others)
        total = len(self.others) + 1
        self.step_lbl.config(text=f"STEP {self.step + 1} OF {total}")
        shown = self.sp if last else [self.ref, self.others[self.step]]
        gap = 420 * S / (len(shown) + 1)
        for i, s in enumerate(shown):
            x = gap * (i + 1)
            draw_icon(c, s["icon"], x, 32 * S, 0.9 * S, s["color"])
            c.create_text(x, 72 * S, text=s["name"], fill=T["text"], font=("Segoe UI Semibold", 10))
        if not last:
            c.create_text(210 * S, 32 * S, text="+", fill=T["muted"], font=("Segoe UI", 18))
            o = self.others[self.step]
            self.howto.config(text=f"You'll hear a tick from {self.ref['name']} and {o['name']}.\n"
                                   f"Slide until the two ticks melt into ONE sharp tick.\n"
                                   f"Hear {o['name']} first → slide right.  Hear it last → slide left.")
            self.left_end.config(text=f"◀ {o['name']} sooner")
            self.right_end.config(text=f"{o['name']} later ▶")
            self.off.set(self.rel[o["id"]])
            self.slider_box.pack(fill="x", pady=(14 * S, 0))
            self.next_btn.config(text="Next ▶")
        else:
            self.howto.config(text="Now every speaker ticks. It should sound like one tick from all "
                                   "around you.\nIf one still sounds off, go Back and fine-tune it.")
            self.slider_box.pack_forget()
            self.next_btn.config(text="✓ Done")
        self.back_btn.config(state="normal" if self.step > 0 else "disabled")
        self.update_label()

    def update_label(self):
        if self.step < len(self.others):
            v = self.rel[self.others[self.step]["id"]]
            self.off_lbl.config(text=f"{v:+.0f} ms")

    def on_slide(self):
        if self.step >= len(self.others):
            return
        self.rel[self.others[self.step]["id"]] = round(self.off.get())
        self.apply()

    def nudge(self, d):
        self.off.set(max(-400, min(400, self.off.get() + d)))
        self.on_slide()

    def apply(self):
        low = min(self.rel.values())
        for s in self.sp:
            s["delay"] = int(self.rel[s["id"]] - low)
        self.app.apply_mix()
        self.update_label()

    def next(self):
        if self.step >= len(self.others):
            self.finish()
            return
        self.step += 1
        self.show()

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.show()

    def finish(self):
        for o in self.app.outs.values():
            o.click_mode = False
            o.click_on = False
        self.apply()
        self.app.save_cfg(quiet=True)
        self.app.select(self.app.sel)
        self.app.flash("Speakers synced", T["good"])
        self.destroy()


def single_instance():
    """Only one copy may run (two would fight over the speakers and the phone server)."""
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 8764))                   # held for as long as this copy runs
        lock.listen(1)
        return lock
    except OSError:
        return None


if __name__ == "__main__":
    _instance = single_instance()
    if not _instance:
        tk.Tk().withdraw()
        messagebox.showinfo("Surround Mixer", "Surround Mixer is already running (check the taskbar).")
        sys.exit(0)
    if linux_io:
        linux_io.ensure_virtual_sink()          # the device programs play into
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SurroundMixer")
    except Exception:
        pass
    com_init()
    root = tk.Tk()
    App(root)
    root.mainloop()
