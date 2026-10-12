#!/usr/bin/env bash
# Surround Mixer installer for Linux (PulseAudio or PipeWire desktops).
#
#   ./install.sh              install (asks for your password once, to install system packages)
#   ./install.sh --startup    also start it (minimized) when you log in
#   ./install.sh --uninstall  remove it
#
# Installs to ~/.local/share/surround-mixer, adds "Surround Mixer" to your app menu and a
# `surround-mixer` command. Nothing is installed system-wide except the packages listed below.
set -euo pipefail

APP_DIR="$HOME/.local/share/surround-mixer"
BIN="$HOME/.local/bin/surround-mixer"
MENU="$HOME/.local/share/applications/surround-mixer.desktop"
AUTOSTART="$HOME/.config/autostart/surround-mixer.desktop"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES=(surround_mixer.py webremote.py calibrate.py linux_io.py make_icon.py requirements-linux.txt README.md)

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

STARTUP=0
case "${1:-}" in
  --uninstall)
    step "Removing Surround Mixer"
    pkill -f "$APP_DIR/surround_mixer.py" 2>/dev/null || true
    rm -f "$BIN" "$MENU" "$AUTOSTART"
    rm -rf "$APP_DIR"
    echo "Removed. (The system packages it installed were left in place.)"
    exit 0 ;;
  --startup) STARTUP=1 ;;
  "") ;;
  *) echo "Unknown option: $1  (use --startup or --uninstall)"; exit 1 ;;
esac

# ---------------------------------------------------------------- system packages
step "Installing system packages (Python, Tk, audio and input tools)"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y python3 python3-venv python3-tk pulseaudio-utils libpulse0 playerctl xdotool x11-xserver-utils
elif command -v dnf >/dev/null; then
  $SUDO dnf install -y python3 python3-tkinter pulseaudio-utils pulseaudio-libs playerctl xdotool xset
elif command -v pacman >/dev/null; then
  $SUDO pacman -S --needed --noconfirm python tk libpulse playerctl xdotool xorg-xset
elif command -v zypper >/dev/null; then
  $SUDO zypper install -y python3 python3-tk pulseaudio-utils libpulse0 playerctl xdotool xset
else
  warn "Unknown package manager. Install these yourself, then run this again:"
  warn "  python3 (3.10+), python3 venv + tkinter, pactl (pulseaudio-utils), libpulse, playerctl, xdotool, xset"
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10 or newer is needed."; exit 1; }
command -v pactl >/dev/null || warn "pactl is missing: volume control and the virtual output device won't work."
[ "${XDG_SESSION_TYPE:-}" = "wayland" ] && \
  warn "Note: on Wayland the phone's trackpad and keys only reach X11/XWayland apps (xdotool)."

# ---------------------------------------------------------------- app files
step "Copying the app to $APP_DIR"
pkill -f "$APP_DIR/surround_mixer.py" 2>/dev/null || true
mkdir -p "$APP_DIR/web"
if [ "$SRC" != "$APP_DIR" ]; then
  for f in "${FILES[@]}"; do cp "$SRC/$f" "$APP_DIR/"; done
  cp "$SRC"/web/* "$APP_DIR/web/"
fi

# ---------------------------------------------------------------- python packages
step "Installing Python packages (a private environment just for this app)"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/requirements-linux.txt" --quiet --disable-pip-version-check

# ---------------------------------------------------------------- launcher + menu entry
step "Adding Surround Mixer to your app menu"
mkdir -p "$(dirname "$BIN")" "$(dirname "$MENU")"
cat > "$BIN" <<EOF
#!/bin/sh
exec "$APP_DIR/venv/bin/python" "$APP_DIR/surround_mixer.py" "\$@"
EOF
chmod +x "$BIN"
desktop_entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=Surround Mixer
Comment=Surround sound across your TV and Bluetooth speakers
Exec="$BIN" $1
Icon=$APP_DIR/web/icon.png
Categories=AudioVideo;Audio;
Terminal=false
EOF
}
desktop_entry "" > "$MENU"
if [ "$STARTUP" = 1 ]; then
  mkdir -p "$(dirname "$AUTOSTART")"
  { desktop_entry "--minimized"; echo "X-GNOME-Autostart-enabled=true"; } > "$AUTOSTART"
  echo "Starts when you log in."
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$MENU")" 2>/dev/null || true

# ---------------------------------------------------------------- done
step "Done"
cat <<EOF
Surround Mixer is installed. Start it from your app menu or run:  surround-mixer

How it works on Linux: the app creates an output device called "Surround-Mixer".
Set it as your sound output (the app's banner has a "Use it" button), and the app
sends everything played there to your TV / Bluetooth speakers.
EOF
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "Add ~/.local/bin to your PATH to use the surround-mixer command." ;; esac
