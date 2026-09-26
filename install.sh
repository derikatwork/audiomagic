#!/usr/bin/env bash
# Install AudioMagic for the current user on Pop!_OS / Ubuntu (22.04 or newer).
#
#   ./install.sh          install (asks before installing system packages)
#   ./install.sh --yes    don't ask
#
# Everything goes into your home folder (~/.local); only the system packages
# below are installed with sudo.
set -euo pipefail

YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && YES=1

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/audiomagic"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
PY=/usr/bin/python3

PACKAGES=(
  python3 python3-gi python3-gst-1.0 python3-numpy python3-scipy python3-aiohttp
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gtk-3.0 gir1.2-webkit2-4.1
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
  gstreamer1.0-pipewire pipewire-bin ffmpeg xdg-utils python3-pip
)

say() { printf '\033[1m%s\033[0m\n' "$*"; }

if ! command -v apt-get >/dev/null; then
  echo "This installer is for Pop!_OS / Ubuntu (apt). Install the equivalents of these packages, then run it again:"
  printf '  %s\n' "${PACKAGES[@]}"
  exit 1
fi

missing=()
for p in "${PACKAGES[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done

if ((${#missing[@]})); then
  say "AudioMagic needs these packages:"
  printf '  %s\n' "${missing[@]}"
  if ((YES == 0)); then
    read -r -p "Install them now with sudo? [Y/n] " answer
    [[ "${answer:-y}" =~ ^[Yy] ]] || { echo "Stopped. Nothing was changed."; exit 1; }
  fi
  # a broken third-party repository shouldn't stop the install
  sudo apt-get update || echo "Note: 'apt-get update' reported errors (often an old PPA); trying to install anyway."
  sudo apt-get install -y "${missing[@]}"
fi

# Use the distribution's Python (the one the apt packages above belong to),
# even if "python3" was pointed somewhere else.
for cand in /usr/bin/python3 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10; do
  if [[ -x "$cand" ]] && "$cand" -c "import gi, numpy, scipy, aiohttp" 2>/dev/null; then
    PY="$cand"
    break
  fi
done

say "Copying AudioMagic to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APP_DIR/audiomagic"
cp -r "$SRC/audiomagic" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$BIN_DIR/audiomagic" <<EOF
#!/bin/sh
# AudioMagic launcher (installed by install.sh)
PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec $PY -m audiomagic "\$@"
EOF
chmod +x "$BIN_DIR/audiomagic"

# The terminal interface (audiomagic --tui) uses Textual, which is newer than
# the distribution's package. It goes in its own folder that only the terminal
# interface loads, so it can't affect anything else.
say "Installing the terminal interface's libraries (Textual)"
rm -rf "$APP_DIR/vendor"
if ! "$PY" -m pip install --quiet --disable-pip-version-check --no-warn-script-location --only-binary=:all: \
    --require-hashes --target "$APP_DIR/vendor" -r "$SRC/requirements-tui.txt"; then
  rm -rf "$APP_DIR/vendor"
  echo "Note: couldn't download Textual, so 'audiomagic --tui' won't work until you run this again online."
  echo "      The app window is not affected."
fi

cp "$SRC/audiomagic/web/img/icon.svg" "$ICON_DIR/audiomagic.svg"
sed "s|@BIN@|$BIN_DIR/audiomagic|" "$SRC/data/audiomagic.desktop" > "$DESKTOP_DIR/audiomagic.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

say "Checking the installation"
"$BIN_DIR/audiomagic" --check || true

echo
say "Done! Open AudioMagic from your app menu, or run: audiomagic"
echo "To use it inside a terminal instead of a window, run: audiomagic --tui"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "(To use the 'audiomagic' command, log out and back in so $BIN_DIR is on your PATH.)" ;;
esac
echo "Recordings are saved in: $(xdg-user-dir MUSIC 2>/dev/null || echo "$HOME/Music")/AudioMagic"
