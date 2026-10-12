# Surround Mixer

Turn a PC's audio into surround sound across several speakers at once: a TV over HDMI plus
Bluetooth speakers (built and tested with two JBL Flips). Comes with a phone remote served over Wi-Fi.

## Features

- **Multi-speaker output**: captures what the PC plays (loopback) and sends it to every speaker.
- **Surround or "Same on all"**: drag speakers on a room map; each one gets the channels that match
  where it stands (front / center / surround / sub).
- **Auto-calibration with your phone**: hold the phone at your seat; each speaker plays a short chirp
  in turn and the phone's microphone measures when and how loud each one arrives. Delays and volumes are
  then set so everything reaches you together and evenly, Bluetooth lag and distance included.
- **Sync wizard** for lining speakers up by ear, and drift held by gentle resampling (no dropouts).
- **Computer volume passthrough**: the phone's + / − press the real volume keys.
- **Phone remote** (iOS-style web app): Apple TV–style trackpad / D-pad, ±10 s skip, play/pause,
  next/prev, now playing with artwork, keyboard, and a full speaker mixer.
- **One-time PIN pairing**: a phone asks to connect, the PC shows a fresh PIN until the phone enters it
  (or you press ✕). Paired phones get their own token; only its hash is stored.
- Optional start at login (minimized).

## Install

### Windows 10 / 11

Double-click **`Install.cmd`** (add `-Startup` to start at login). It finds or installs Python 3,
puts the app in `%LOCALAPPDATA%\Programs\SurroundMixer` with its own Python environment, and adds
Desktop + Start menu shortcuts. Remove it with `Install.cmd -Uninstall`.

Then set your Windows output to a device you don't listen to (**Steam Streaming Speakers** or the free
**VB-CABLE** "CABLE Input"); Surround Mixer records it and sends the sound to your speakers.

### Linux (PulseAudio or PipeWire)

```bash
chmod +x install.sh && ./install.sh            # --startup to start at login, --uninstall to remove
```

Installs the needed packages (apt, dnf, pacman or zypper), the app in `~/.local/share/surround-mixer`,
an app-menu entry and a `surround-mixer` command. The app creates an output device called
**Surround-Mixer**; set it as your sound output (the app's banner has a "Use it" button).
The phone's trackpad/keys use `xdotool` (X11 apps), now playing uses `playerctl` (any MPRIS player).

## Phone remote

Click **📱 Phone remote** on the PC and scan the QR code (same Wi-Fi). In Safari use
*Share › Add to Home Screen* for an app-like remote.

Calibration needs the microphone, which phones only allow on secure pages, so it opens on
`https://<pc>:8766` with a certificate made by your PC: the first time, Safari says the connection
isn't private: tap *Show Details › visit this website*.

## Files

| File | What it does |
|---|---|
| `surround_mixer.py` | Desktop app (Tkinter): audio engine, room map, sync wizard, calibration |
| `webremote.py` | Web server (http + https), pairing, trackpad/keyboard input, now playing |
| `calibrate.py` | Calibration chirps and the measurement (matched filter) |
| `linux_io.py` | Linux versions of the Windows-only parts (pactl, xdotool, playerctl) |
| `web/index.html` | The phone app |
| `install.ps1`, `Install.cmd`, `install.sh` | Installers |

Settings are saved to `surround_config.json` next to the app (not committed).
