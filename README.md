# AudioMagic

A simple multi-input recorder for Linux, built on PipeWire. It sits between
Audacity and Ardour: record several microphones, interface inputs, programs
and streams at once, each on its own track with its own controls. Tidy up
the take, then export it as FLAC, Ogg or MP3.

![AudioMagic editing a take](docs/screenshots/editor-dark.png)

## What it does

- **Multiple inputs, each with its own controls.** Every input is a track with
  record-arm, mute, solo, live monitoring, volume, pan, a level meter and a clip light.
- **Anything PipeWire can see:**
  - microphones and USB interfaces, with each input jack as its own track if you like
  - one program's audio, such as a Discord, Zoom or Jitsi call with remote guests, a browser or a game
  - everything playing through an output
- **Internet streams and other software:**
  - Icecast/HTTP radio, HLS and RTSP
  - OBS, ffmpeg or another computer over **SRT**, encrypted with a passphrase
- **Per-track effects:** noise suppression, noise gate and EQ with a low cut, plus
  a "Voice" preset. Effects never touch the raw recording. You hear them while
  monitoring and playing back, and they're applied when you export.
- **Light editing:** cut a section from all tracks, silence a section on one
  track, keep only a selection, fade in and out, normalize a track. Undo and
  redo work for all of it. Edits never modify the recorded files.
- **Export** to FLAC, Ogg Opus, Ogg Vorbis, MP3 or WAV:
  - as one mix, one file per track, or both
  - stereo or mono, with tags
  - optional loudness normalization (podcast -16 LUFS, streaming -14 LUFS, broadcast -23 LUFS)
- **Safe recording:**
  - each track is written straight to a 24-bit / 48 kHz WAV file as it records
  - a take cut off by a crash is repaired the next time you open the project
  - all tracks line up in time, even from different devices

It's a desktop app: it opens in its own window and runs on your computer.
Nothing is uploaded anywhere.

## Install (Pop!_OS / Ubuntu 22.04 or newer)

```bash
git clone https://github.com/derikatwork/audiomagic.git
cd audiomagic
./install.sh
```

The installer asks before installing the system packages it needs
(GStreamer, PipeWire tools, ffmpeg, WebKitGTK, NumPy/SciPy, aiohttp). It then
puts AudioMagic in your home folder and adds it to the app menu.

To remove it later, run `./uninstall.sh`. Your recordings are kept.

To run it straight from the source folder without installing:
`python3 -m audiomagic`. To see whether anything is missing: `python3 -m audiomagic --check`.

## Using it

1. **Add inputs.** Click **Add input** and choose:

   | Tab | What it records |
   |---|---|
   | Microphones & interfaces | A device, or a single input of an audio interface ("Each input as its own track"). |
   | Programs | One program's sound. Start the call or playback first so the program shows up. |
   | Everything you hear | Everything playing on an output. |
   | Internet stream | Paste a stream address. |
   | OBS / network (SRT) | Shows the exact address to paste into OBS. |
   | Test tone | A steady tone for checking the setup. |

2. **Check levels.** Speak at a normal volume. The meter should peak in the
   yellow. A red clip light means the source itself is too loud: turn it down
   on the microphone, the interface or in the system sound settings.
3. **Record.** Every input with its red **R** lit is recorded. The **R** key
   starts and stops recording.
4. **Edit.** Drag across the waveform to select part of it. Then:
   - **Cut** removes the selection from every track.
   - **Silence** mutes the selection on the track you dragged on.
   - **Keep selection** trims everything else away.

   Fades are in the edit bar. Normalize is in each track's **⋯** menu.
5. **Export.** Pick the format, whether you want the mix and/or separate
   tracks, a loudness target and tags.

![Add input](docs/screenshots/add-input.png)

### Remote guests

Have your guests join a normal call (Discord, Zoom, Jitsi, Teams in the
browser…), then add that program under **Programs**. The call is recorded on
its own track, alongside your microphone. Use headphones so the call doesn't
leak into your mic.

### OBS and other computers (SRT)

Add an **OBS / network (SRT)** input. AudioMagic listens on the port shown and
displays an address like:

```
srt://127.0.0.1:9000?mode=caller&passphrase=…&pbkeylen=16
```

In OBS, go to *Settings → Stream*, choose Service **Custom…**, paste that
address as the Server and leave the Stream Key empty. The passphrase encrypts
the stream (AES-128). Turn on "Allow other computers on my network" to accept
streams from another machine; then use this computer's IP address instead of
`127.0.0.1`.

### Monitoring

The headphones button on a track plays that input live, with its effects, to
the output chosen at the top right. Expect roughly 30–60 ms of delay, which is
fine for checking sound but noticeable if you're singing along. For zero
delay while performing, use your interface's direct monitoring.

### Keyboard

| Key | Action |
|---|---|
| Space | Play / pause |
| R | Start / stop recording |
| Delete | Cut the selection |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo |
| + / - / F | Zoom in / out / fit (Ctrl+scroll zooms too) |
| Home / End | Jump to start / end |
| Esc | Clear the selection |

## Where things are saved

```
~/Music/AudioMagic/<Project>/
    project.json                 inputs, takes and edits
    audio/take-001/01-host.wav   the raw recordings (24-bit WAV, one per track)
    exports/                     your exported files
```

Deleted takes and removed tracks go to the desktop trash, so they can be
restored. Settings live in `~/.config/audiomagic/`.

## Troubleshooting

- **An input says "device not connected".** Plug it back in; AudioMagic picks
  it up again automatically.
- **A program isn't listed.** Programs only appear while they are playing
  sound. Start the call or video, then press **Refresh**.
- **"Effects are using more CPU than this computer has".** It can't run the
  live effects for this many inputs in real time, so meters and monitoring
  may stutter. **Your recordings are not affected** (effects are only applied
  live for listening; the files are always recorded raw). Turn off noise
  suppression on some inputs to make it go away.
- **An input "lost audio".** If a device drops out for a moment (a USB
  hiccup), the gap is filled with silence so it stays in sync with the other
  inputs, and the log says so.
- **Export says there isn't enough space.** Exporting writes temporary files
  next to the export folder (about 700 MB per mono track per hour, plus the
  finished files). Free some space or choose another folder.
- **No window, it opened in the browser.** WebKitGTK is missing:
  `sudo apt install gir1.2-webkit2-4.1`. Or run `audiomagic --browser` on purpose.
- **Something else.** Run `audiomagic --check`, and `audiomagic --debug` for
  detailed logs.

## How it's built

| Part | Technology |
|---|---|
| Capture | PipeWire, through GStreamer's `pipewiresrc`. Inputs are found and watched with `pw-dump`. |
| Monitoring and playback | Played through PipeWire's PulseAudio service (`pulsesink`), which PipeWire mixes and routes. (PipeWire 1.0's own `pipewiresink` can deadlock when a stream starts; it's only used if that service isn't running.) |
| Effects | Written in NumPy/SciPy, so live monitoring, playback and export sound identical. |
| Export | ffmpeg: encoding, tags and two-pass loudness normalization. |
| Interface | A local web page (HTML/JS, no build step) in a GTK WebKit window. The server listens only on 127.0.0.1 and every request needs a per-launch secret key. |

Code map (`audiomagic/`):

| File | Role |
|---|---|
| `engine.py` | Ties everything together |
| `capture.py` | Inputs |
| `recorder.py` | Writing takes, lining tracks up |
| `dsp.py` | Effects |
| `render.py` | Edits + effects + mix |
| `export.py` | Export |
| `server.py` | Local API |
| `app.py` | Window and start-up |
| `web/` | The interface |

### Tests

```bash
python3 -m pytest tests
```

The tests in `tests/test_pipewire.py`, `test_network.py` and
`test_server.py` need a running PipeWire. They create their own virtual
devices, record from them and check the results: timing alignment between
inputs, monitoring, playback, edits and every export format.
`test_stress.py` records 11 inputs with every effect on and checks that not a
single sample is lost. `test_qa.py` covers edge cases: fuzzed edits, awkward
file names, crashes and a full disk during recording, cancelled exports and
malformed API requests. [docs/QA-REPORT.md](docs/QA-REPORT.md) has the results
of the longer stress runs.

## Not yet

- Recording new tracks while playing earlier ones (overdubbing).
- Built-in invite links for remote guests (use a call program for now).
- The noise suppressor is a classic spectral one: great for fans, hiss and
  hum, less so for sudden noises like keyboard clicks or dogs.
