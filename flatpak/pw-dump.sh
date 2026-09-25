#!/bin/sh
# pw-dump from AudioMagic's own PipeWire build (the runtime has no PipeWire
# command-line tools), kept apart from the runtime's PipeWire library.
dir=/app/pwtools
export LD_LIBRARY_PATH="$dir/lib" SPA_PLUGIN_DIR="$dir/lib/spa-0.2" \
       PIPEWIRE_MODULE_DIR="$dir/lib/pipewire-0.3" PIPEWIRE_CONFIG_DIR="$dir/share/pipewire"
exec "$dir/bin/pw-dump" "$@"
