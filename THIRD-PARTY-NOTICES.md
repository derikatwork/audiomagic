# Third-party software

AudioMagic's own code (everything in this repository) is under the
[MIT licence](LICENSE). It uses other people's software, and the Flatpak
includes some of it. Each keeps its own licence. None of them places
conditions on AudioMagic's own licence: the LGPL and MPL parts are separate
programs or libraries that AudioMagic uses without modifying them.

## Included in the Flatpak

Built from source by `flatpak/io.github.derikatwork.AudioMagic.yml`, which
pins each one to an exact commit or checksum; that is also where to get the
corresponding source code. Their licence texts are installed in the Flatpak
under `/app/share/licenses/io.github.derikatwork.AudioMagic/`.

| Component | Version | Licence | Used for |
|---|---|---|---|
| [FFmpeg](https://ffmpeg.org) (`ffmpeg` program), built without its GPL or non-free parts | 8.1.3 | LGPL-2.1-or-later | exporting |
| [LAME](https://lame.sourceforge.io), linked into ffmpeg | 3.100 | LGPL-2.0-or-later | MP3 export |
| [PipeWire](https://pipewire.org) (`pw-dump` and its library) | 1.4.11 | MIT | listing devices and programs |
| [SRT](https://github.com/Haivision/srt) | 1.5.7 | MPL-2.0 | the SRT (OBS) input |
| GStreamer's SRT plugin, from [gst-plugins-bad](https://gstreamer.freedesktop.org) | 1.26.11 | LGPL-2.1-or-later | the SRT (OBS) input |

Python packages, installed from the pinned wheels in
`flatpak/python-deps.json` (their licence files are installed with them):

| Package | Version | Licence |
|---|---|---|
| NumPy | 2.5.3 | BSD-3-Clause (and 0BSD, MIT, Zlib, CC0-1.0 for small parts) |
| SciPy | 1.18.1 | BSD-3-Clause |
| aiohttp | 3.14.3 | Apache-2.0 and MIT |
| multidict, yarl, frozenlist, aiosignal, propcache | | Apache-2.0 |
| attrs | | MIT |
| aiohappyeyeballs, typing_extensions | | PSF-2.0 |
| idna | | BSD-3-Clause |

The NumPy and SciPy wheels also contain OpenBLAS (BSD-3-Clause), the GCC
Fortran runtime (GPL-3.0-or-later with the GCC Runtime Library Exception,
which allows use by software under any licence) and libquadmath
(LGPL-2.1-or-later).

## Provided by the GNOME runtime or your system

Python (PSF-2.0), PyGObject (LGPL-2.1-or-later), GLib, GTK and WebKitGTK
(LGPL-2.1-or-later and BSD), and GStreamer with its plugins
(LGPL-2.1-or-later). With the non-Flatpak install (`install.sh`) these, as
well as ffmpeg and PipeWire's tools, come from your distribution's packages.
Ubuntu's ffmpeg package is built with GPL parts; AudioMagic only runs it as a
separate program.
