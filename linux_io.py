"""
Linux versions of the Windows-only parts (PulseAudio / PipeWire desktops):

  - volume + mute of output devices   -> pactl
  - trackpad, keys, typing (remote)   -> xdotool (X11; on Wayland it reaches X11/XWayland apps)
  - now playing, play/pause, seeking  -> playerctl (any MPRIS player: Spotify, browsers, VLC, mpv...)
  - turning the screen off            -> xset

Every tool is optional: if one is missing that feature just does nothing.
"""
import json
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

VIRTUAL_SINK = "surround_mixer"
VIRTUAL_DESC = "Surround-Mixer"


def run(*args, timeout=3):
    if not shutil.which(args[0]):
        return None
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ volume ----

def sinks():
    """{description: sink name} for every output device."""
    out = run("pactl", "-f", "json", "list", "sinks")
    try:
        return {s.get("description") or s["name"]: s["name"] for s in json.loads(out)}
    except (TypeError, ValueError, KeyError):
        pass
    found, name = {}, None                               # older pactl without JSON output
    for line in (run("pactl", "list", "sinks") or "").splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Description:") and name:
            found[line.split(":", 1)[1].strip()] = name
    return found


class PulseVolume:
    """Same methods the app uses on Windows' IAudioEndpointVolume."""

    def __init__(self, sink):
        self.sink = sink

    def GetMasterVolumeLevelScalar(self):
        out = run("pactl", "get-sink-volume", self.sink) or ""
        for part in out.split("/"):
            if part.strip().endswith("%"):
                return int(part.strip()[:-1]) / 100
        return 1.0

    def SetMasterVolumeLevelScalar(self, value, _context=None):
        run("pactl", "set-sink-volume", self.sink, f"{round(max(0.0, min(1.0, value)) * 100)}%")

    def GetMute(self):
        return "yes" in (run("pactl", "get-sink-mute", self.sink) or "")

    def SetMute(self, mute, _context=None):
        run("pactl", "set-sink-mute", self.sink, "1" if mute else "0")


def endpoint_volumes(names):
    found = sinks()
    return {n: PulseVolume(found[n]) for n in names if n in found}


def ensure_virtual_sink():
    """The device programs play into (like Steam Streaming Speakers / VB-CABLE on Windows)."""
    if VIRTUAL_SINK not in (run("pactl", "list", "short", "sinks") or ""):
        run("pactl", "load-module", "module-null-sink", f"sink_name={VIRTUAL_SINK}",
            f"sink_properties=device.description={VIRTUAL_DESC}")


def make_default(description):
    name = sinks().get(description)
    if name:
        run("pactl", "set-default-sink", name)


# ------------------------------------------------------- keyboard / mouse ----

KEYSYM = {"left": "Left", "up": "Up", "right": "Right", "down": "Down", "enter": "Return", "esc": "Escape",
          "space": "space", "backspace": "BackSpace", "tab": "Tab", "f": "f", "j": "j", "k": "k", "l": "l",
          "alt": "alt", "shift": "shift", "ctrl": "ctrl", "play_pause": "XF86AudioPlay",
          "next": "XF86AudioNext", "prev": "XF86AudioPrev"}
VOLUME = {"vol_up": ("set-sink-volume", "+2%"), "vol_down": ("set-sink-volume", "-2%"),
          "vol_mute": ("set-sink-mute", "toggle")}


def press(names):
    if len(names) == 1 and names[0] in VOLUME:            # straight to the system volume
        cmd, arg = VOLUME[names[0]]
        run("pactl", cmd, "@DEFAULT_SINK@", arg)
        return
    run("xdotool", "key", "--clearmodifiers", "+".join(KEYSYM[n] for n in names))


def type_text(text):
    run("xdotool", "type", "--delay", "8", "--", text, timeout=10)


def mouse_move(dx, dy):
    run("xdotool", "mousemove_relative", "--", str(int(dx)), str(int(dy)))


def mouse_button(button, down):
    run("xdotool", "mousedown" if down else "mouseup", "3" if button == "right" else "1")


def click(button="left", count=1):
    run("xdotool", "click", "--repeat", str(count), "3" if button == "right" else "1")


_scroll_left = [0.0, 0.0]          # leftover from small phone swipes, so slow scrolling still moves


def scroll(dy, dx=0):
    for i, (amount, pos, neg) in enumerate(((dy, "4", "5"), (dx, "7", "6"))):  # 120 = one notch on Windows
        _scroll_left[i] += amount
        steps = int(_scroll_left[i] / 60)
        if steps:
            _scroll_left[i] -= steps * 60
            run("xdotool", "click", "--repeat", str(min(abs(steps), 20)), pos if steps > 0 else neg)


def foreground_title():
    return (run("xdotool", "getactivewindow", "getwindowname") or "").strip()


def display_off():
    run("xset", "dpms", "force", "off")


# ------------------------------------------------------------ now playing ----

FORMAT = "{{status}}\t{{title}}\t{{artist}}\t{{playerName}}\t{{position}}\t{{mpris:length}}\t{{mpris:artUrl}}"


class Media:
    """Same interface as the Windows version in webremote.py."""

    def __init__(self):
        self.state = {"available": False}
        self.art = (None, b"", "image/png")
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            self._refresh()
            time.sleep(1.0)

    def _refresh(self):
        out = run("playerctl", "metadata", "--format", FORMAT)
        if not out or out.count("\t") < 6:
            self.state = {"available": False}
            return
        status, title, artist, player, pos, length, art = out.rstrip("\n").split("\t")[:7]
        key = f"{title}|{artist}"
        if key != self.art[0]:
            self.art = (key, self._fetch_art(art), "image/jpeg" if art.lower().endswith((".jpg", ".jpeg")) else "image/png")
        self.state = {
            "available": True, "title": title, "artist": artist, "app": player.title(),
            "playing": status == "Playing", "position": round(int(pos or 0) / 1e6, 1),
            "duration": round(int(length or 0) / 1e6, 1), "can_seek": bool(length),
            "art": str(abs(hash(key))) if self.art[1] else "",
        }

    @staticmethod
    def _fetch_art(url):
        try:
            if url.startswith("file://"):
                with open(urllib.parse.unquote(url[7:]), "rb") as f:
                    return f.read()
            if url.startswith(("http://", "https://")):
                with urllib.request.urlopen(url, timeout=3) as r:
                    return r.read(2_000_000)
        except (OSError, ValueError):
            pass
        return b""

    def command(self, action):
        verb = {"play_pause": "play-pause", "next": "next", "prev": "previous"}[action]
        if run("playerctl", verb) is None:
            press([action])
        self._refresh()

    def seek(self, delta):
        if run("playerctl", "position", f"{abs(delta):g}{'+' if delta > 0 else '-'}") is None:
            title = foreground_title().lower()
            fwd = delta > 0
            if "youtube" in title:
                press(["l" if fwd else "j"])
            else:
                press(["right" if fwd else "left"])
        self._refresh()
