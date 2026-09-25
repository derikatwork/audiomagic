#!/bin/sh
# AudioMagic launcher inside the Flatpak
export PYTHONPATH="/app/share/audiomagic${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m audiomagic "$@"
