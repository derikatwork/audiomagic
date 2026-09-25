"""Writing a take: one 24-bit WAV per armed track, all lined up in time.

Every input delivers blocks at its own pace. When recording starts we note
the time T0 and each track begins at the sample captured at T0 (using the
BlockClock estimate), so tracks from different devices line up.

Inputs that PipeWire keeps in lockstep (microphones, interfaces, apps) are
then simply appended. If one of them loses audio (a USB hiccup, an overloaded
machine) and falls more than 50 ms behind the inputs from other devices, the
gap is filled with silence so it stays in sync for the rest of the take.
Inputs with their own clock (internet streams, SRT, test tone) are nudged
back into line if they drift by more than 50 ms either way.
"""

import os
import threading
import time

import numpy as np

from . import SAMPLE_RATE
from . import peaks as peaks_mod
from .util import log
from .wavio import WavWriter

GAP = int(0.05 * SAMPLE_RATE)


class RecTrack:
    def __init__(self, recorder, track_id, path, channels, graph_synced):
        self.recorder = recorder
        self.track_id = track_id
        self.path = path
        self.channels = channels
        self.graph_synced = graph_synced
        self.writer = WavWriter(path, channels)
        self.peaks = peaks_mod.PeakBuilder()
        self.lock = threading.Lock()
        self.started = False
        self.done = False
        self.closed = False
        self.generation = None
        self.capture_key = None
        self.written = 0
        self.last_end_ns = None
        self.error = None
        self.filled_gaps = 0

    def _write(self, x):
        if x.shape[0] and self.error is None:
            try:
                self.writer.write(x)
            except OSError as e:
                self.error = f"could not write {os.path.basename(self.path)}: {e.strerror or e}"
                self.done = True
                log.error("%s", self.error)
                return
            self.peaks.add(x)
            self.written += x.shape[0]

    def _pad(self, frames):
        if frames > 0:
            self._write(np.zeros((frames, self.channels), np.float32))

    def feed(self, x, t0, capture):
        rec = self.recorder
        with self.lock:
            if self.done:
                return
            n = x.shape[0]
            end_ns = t0 + int(n * 1e9 / SAMPLE_RATE)
            if self.generation != capture.generation:
                # first block, or the input restarted: place it by time
                target = rec.frame_at(t0)
                if not self.started:
                    if target + n <= 0:
                        return
                    if target < 0:
                        x = x[-target:]
                        target = 0
                    self.started = True
                elif target < self.written:
                    x = x[min(x.shape[0], self.written - target):]
                    target = self.written
                self.generation = capture.generation
                self.capture_key = capture.key
                self._pad(target - self.written)
            elif not self.graph_synced:
                target = rec.timeline_frame(t0, exclude=self)
                diff = target - self.written
                if diff > GAP:
                    self._pad(diff)
                elif diff < -GAP:
                    x = x[min(x.shape[0], -diff):]
            else:
                target = rec.graph_reference(t0, self.capture_key)
                if target is not None and target - self.written > GAP:
                    # this device lost audio: fill the hole so it stays in sync with the others
                    self.filled_gaps += 1
                    log.warning("input for %s lost %.0f ms of audio; filled with silence to stay in sync",
                                self.track_id, (target - self.written) * 1000 / SAMPLE_RATE)
                    self._pad(target - self.written)
            stop = rec.stop_frame
            if stop is not None:
                remaining = stop - self.written
                if remaining <= x.shape[0]:
                    self._write(x[:max(0, remaining)])
                    self.done = True
                    return
            self._write(x)
            self.last_end_ns = end_ns

    def close(self, total):
        with self.lock:
            self.done = True
            if self.closed:
                return
            if total is not None and self.written < total:
                self._pad(total - self.written)
            elif total is not None and self.written > total:
                try:
                    self.writer.truncate(total)
                    self.written = total
                except OSError as e:
                    log.error("could not trim %s: %s", self.path, e)
            try:
                self.writer.close()
            except OSError as e:
                log.error("closing %s failed: %s", self.path, e)
            self.closed = True
        try:
            pk = self.peaks.all()
            if total is not None:
                pk = pk[:-(-total // peaks_mod.BIN)]
            peaks_mod.save(self.path, pk)
        except OSError as e:
            log.warning("could not save waveform cache: %s", e)


class Recorder:
    def __init__(self, project_path):
        self.project_path = project_path
        self.t_start = time.monotonic_ns()
        self.stop_frame = None
        self.tracks = {}

    def add_track(self, track_id, rel_path, channels, graph_synced):
        path = os.path.join(self.project_path, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rt = RecTrack(self, track_id, path, channels, graph_synced)
        self.tracks[track_id] = rt
        return rt

    def frame_at(self, t_ns):
        return int(round((t_ns - self.t_start) * SAMPLE_RATE / 1e9))

    def timeline_frame(self, t_ns, exclude=None):
        """Where time t falls on the take, following the PipeWire-locked
        tracks when there are any (they share the audio device's clock)."""
        best = None
        for t in self.tracks.values():
            if t is exclude or not t.graph_synced or not t.started or t.last_end_ns is None:
                continue
            if best is None or t.written > best.written:
                best = t
        if best is None:
            return self.frame_at(t_ns)
        return best.written + int(round((t_ns - best.last_end_ns) * SAMPLE_RATE / 1e9))

    def graph_reference(self, t_ns, capture_key):
        """Where time t falls on the take according to PipeWire inputs from
        *other* devices, or None if there are none to compare with."""
        best = None
        for t in self.tracks.values():
            if (not t.graph_synced or not t.started or t.last_end_ns is None
                    or t.capture_key == capture_key):
                continue
            if best is None or t.written > best.written:
                best = t
        if best is None:
            return None
        return best.written + int(round((t_ns - best.last_end_ns) * SAMPLE_RATE / 1e9))

    def elapsed_frames(self):
        if self.stop_frame is not None:
            return self.stop_frame
        return max(0, self.frame_at(time.monotonic_ns()))

    def request_stop(self):
        if self.stop_frame is None:
            self.stop_frame = max(0, self.frame_at(time.monotonic_ns()))

    def finalize(self, timeout=1.5):
        """Wait for every track to reach the stop point, then close the files.
        Returns the take length in frames."""
        self.request_stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not all(t.done for t in self.tracks.values()):
            time.sleep(0.02)
        for t in self.tracks.values():
            t.close(self.stop_frame)
        return self.stop_frame
