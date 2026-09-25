#!/usr/bin/env bash
# Remove AudioMagic from your home folder. Your recordings and settings are kept.
set -euo pipefail

rm -rf "$HOME/.local/share/audiomagic"
rm -f "$HOME/.local/bin/audiomagic"
rm -f "$HOME/.local/share/applications/audiomagic.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/audiomagic.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true

echo "AudioMagic was removed."
echo "Kept your recordings in: $(xdg-user-dir MUSIC 2>/dev/null || echo "$HOME/Music")/AudioMagic"
echo "Kept your settings in:   ${XDG_CONFIG_HOME:-$HOME/.config}/audiomagic"
echo "The system packages installed for it were left in place."
