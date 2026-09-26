# QA and stress test report

A quality-assurance pass (code review plus edge-case tests) and a set of
stress tests, run on the finished app. Every problem found was fixed and most
are now covered by an automated test.

**Test machine:** 4-core Intel Xeon at 2.8 GHz, 15 GB RAM, PipeWire 1.0.5
(headless, with virtual devices). That's slower per core than a typical
desktop, so these numbers are on the pessimistic side.

## Summary

| Area | Result |
|---|---|
| 16 inputs, all effects on, 4 monitored, 10 minutes | **Pass.** Not one sample lost or repeated on any input, no drift between devices (click offset constant to the sample for 10 minutes), all files exactly the same length. |
| Recording a device that drops out | **Pass** (after fix). The gap is filled with silence so it stays in sync. |
| Full test suite, 10 runs in a row | **Pass** (after fix). Before the fix, 1 of 7 runs froze (bug 1 below) and 2 had a 43 ms glitch on the virtual interface; neither has happened since. |
| Crash or power cut while recording | **Pass.** Audio up to the crash is recovered on the next start. |
| Disk full while recording | **Pass.** Recording stops with a message; everything recorded so far is kept. |
| 2-hour take (4.1 GB), 500 cuts | **Pass.** Editing stays instant (5 ms per cut); the app opens it in under a second. |
| 2-hour export, mix + 3 stems, loudness | **Pass.** Every file complete and full length. Exports are now 2.2× faster (the files are encoded in parallel). |
| Hammering the controls and the API | **Pass.** No errors, no leaks, nothing lost. |
| Memory | **Pass.** Flat while idle; grows about 1 MB a minute while recording 16 inputs (the live waveforms). |

## Bugs found and fixed

Serious (could lose or damage a recording):

1. **Rare freeze when monitoring or playback started.** PipeWire 1.0.5's
   GStreamer output element (`pipewiresink`) can deadlock when a stream
   starts: its streaming thread and PipeWire's event thread each wait for a
   lock the other holds. That event thread is shared with every PipeWire
   input in the app, so every input would have stopped delivering audio and
   the app would have frozen. It showed up about once in six full test runs,
   and a stack dump confirmed the lock cycle. Ubuntu 24.04, which Pop!_OS
   24.04 is built on, ships this same PipeWire version. Outputs now go through PipeWire's PulseAudio service
   (`pulsesink`), which has no shared state with the inputs. As a bonus,
   monitoring delay dropped from about 77 ms to 39 ms. `audiomagic --check`
   warns if that service is missing. Two other full runs had a single 43 ms
   glitch on the virtual interface while monitors were running; that hasn't
   happened again in 10 runs since the switch, although I couldn't prove the
   two were linked.
2. **Heavy load lost audio.** With 16 inputs and every effect on, the
   effects ran on the same threads that capture audio. When the machine got
   busy they fell behind and audio was dropped: 126–305 glitches per input
   in a one-minute test, inputs from different devices drifted 0.9 s apart
   within that minute, and the files ended up different lengths. Capture
   threads now only record; effects, meters and monitoring run on their own
   thread and skip blocks when they can't keep up (the status bar says so).
   A 3-second buffer absorbs short stalls such as a slow disk.
3. **A device that dropped out stayed out of sync.** If a USB device lost
   audio for a moment, it stayed that far behind for the rest of the take.
   The gap is now filled with silence.
4. **Files could end a few blocks longer than the take.** They're now trimmed
   to the exact length, and the waveform cache is trimmed to match (it was
   being rebuilt every time the take was opened).

Found later, while packaging the Flatpak (fixed):

- **A busy moment in Python could still drop audio.** The 3-second buffer
  behind each input held on to PipeWire's own buffers, and PipeWire only
  lends about 170 ms of them, so a stall longer than that (a long
  garbage-collection pass, a busy thread) dropped audio on every input: a
  deliberate 0.8 s stall lost about 540 ms. Each block is now copied out and
  PipeWire's buffer returned at once, so the buffer really does ride out
  stalls; the same 0.8 s stall now loses nothing (`tests/test_stress.py`).
- **After a long stall, AudioMagic could think an input had lost audio when
  it hadn't.** Timing was taken when a block reached Python, so blocks that
  had waited in the buffer looked late, and the gap filler inserted silence
  into perfectly good audio (about 1 full-suite run in 10 showed a 51 ms
  false gap). The time each block spent waiting is now subtracted; a 2.5 s
  stall now leaves every input intact and in sync.
- **Stopping right after such a stall could cut off the end.** Stopping now
  waits for inputs that are still catching up (up to 4 s), but not for one
  that has gone quiet.

Other bugs:

5. Exporting with every track muted exported *all* of them.
6. Cancelling an export didn't stop the loudness measurement (minutes, on a
   long take), and could leave a half-written file behind.
7. Stopping a recording twice at once (the button and the R key) could race.
8. Changing the output device during playback left playback on the old one.
9. The project list failed if a project folder had been deleted.
10. Meters froze while a slow operation (loading a long take) ran, and one
    stuck window could hold up the updates for everyone else.
11. Quitting while a recording was being stopped could cut the stop short.
12. Dragging a mixer slider rewrote the project file for every small change;
    it's now saved within a second of the last change (and always on quit).

Speed-ups:

- The first look at a 2-hour stereo track (building its waveform): 40 s →
  6 s. Reading 24-bit audio is about 4× faster, which also speeds up
  playback and export, and the stereo min/max is 24× faster.
- The timeline no longer re-lays itself out on every frame while recording
  (40 → 58 frames per second with 16 inputs).
- Exports encode their files in parallel, at low priority so recording and
  the interface stay smooth, and check there's enough disk space first. The
  same 26-minute export (mix + 3 stems, FLAC, voice effects, podcast
  loudness) went from 565 s to 255 s on this 4-core machine, with identical
  output.

## Stress tests in detail

### 16 inputs for 10 minutes

8 inputs from a virtual 8-channel interface, 4 programs, 2 test tones, an
SRT stream and the desktop audio. All 16 with the "voice" preset (high-pass,
noise suppression, gate, EQ); 4 monitored. The virtual devices play known
signals (steady sines and a click every half second) so every recorded sample
can be checked.

| | Before fixes (1-minute run) | After (10-minute run) |
|---|---|---|
| Lost/repeated audio | 126–305 glitches per input | **0** |
| Drift between devices | 0.9 s within the minute | **0 samples** (offset constant to the sample) |
| Track lengths | unequal | **identical** |
| CPU (all 16 with effects) | 130% of one core | 91% of one core |
| Memory growth | +2.2 MB in the minute | +10.6 MB over 10 minutes (the live waveforms) |
| Stop recording | up to 5 s | 277 ms |
| Meter updates | stalled under load | 23.5 per second, never more than 190 ms apart |

Along the way the first set of virtual devices turned out to drop audio
themselves when the machine was busy, so they were replaced with ones that
play pre-rendered files (PipeWire pulls those on demand, so they can't
starve). The final run also recorded the virtual interface separately with
`pw-record` as a reference: 601 s, not one glitch, clicks exactly 24,000
samples apart, which confirms the test signal was clean.

The effects thread was 42% busy for 16 voice-preset inputs, so this machine
has headroom for about twice that. The voice preset costs about 11.5 ms of
CPU per second of audio per input (noise suppression is half of that). On an overloaded machine the status bar
warns that monitoring may stutter; the recording stays perfect (checked by
`tests/test_stress.py`).

### A 2-hour take

Three tracks (host, guest, a stereo music bed), 4.1 GB of 24-bit WAV.

| | |
|---|---|
| App start with it | 0.9 s |
| Waveform, first time (mono / stereo) | 3.5 s / 6 s (was 40 s), then cached: 28 ms |
| Interface ready with all waveforms | 0.8 s; zoom and scroll redraws 1 ms (worst 6 ms) |
| 500 cuts | 5 ms each (worst 47 ms) |
| Start playback anywhere | at most 91 ms |
| Normalize a 2-hour track | 2.9 s |
| Export mix + 3 stems, FLAC, voice effects, podcast loudness | 24 min before the export changes (4× faster than real time); now about half that |

### Hammering

| Test | Result |
|---|---|
| 20 windows open at once, 20 s | every one got 24.4 meter updates a second; app CPU 11% |
| 1,000 mixer changes one after another | 1.2 ms each |
| 1,000 mixer changes, 10 at a time | 0.4 s total; the last value is the one saved |
| 30 record/stop cycles | 30 takes, every file exactly the take length |
| 200 play/seek/pause/stop requests | 2 s, no errors |
| Add 4 inputs and remove them, 25 times | no leaked threads or file handles |
| Malformed and hostile API requests | all refused cleanly (missing or wrong key, requests from another website, wrong Host, `../` path tricks, bad JSON, a 2 MB body); the server carries on normally |

### Edge cases (automated, `tests/test_qa.py`)

- 300 random cuts, trims, undos and redos checked against a simple reference model.
- Names like `../../etc/passwd`, `a/b\c:d`, `.`, blank, 300 characters, accents, emoji and quotes: always a safe folder inside the projects folder.
- Fades longer than the take, a 10-sample selection, cutting everything.
- Normalizing a silent track (refused with a message).
- A crash mid-recording (the app is killed), then restart: audio recovered.
- The disk filling up mid-recording.
- Exports: files with the same name, cancelling, a failing encoder, not
  enough disk space.

## Known limits

- Exporting needs temporary space: about 700 MB per mono track per hour,
  plus the finished files. The export checks this first.
- A take can't be recorded while another is playing (no overdubbing yet).
- Export speed depends on the loudness option: two-pass loudness
  normalization reads everything twice.

## Reproducing

```bash
python3 -m pytest tests            # everything, including the load test
python3 -m pytest tests/test_stress.py tests/test_qa.py
```

The longer runs (10-minute, 2-hour) used scripts built on the same virtual
devices as `tests/fake_devices.py`.
