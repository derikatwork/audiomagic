"""Waveform overview data: min/max per 256 frames, stored as int8.

Values are square-root companded (sign(v) * sqrt(|v|) * 127) so quiet speech
is still visible; the UI decodes them back to linear amplitude.
"""

import os
import threading

import numpy as np

from .dsp import across_channels

BIN = 256


def compand(v):
    return np.clip(np.rint(np.sign(v) * np.sqrt(np.abs(v)) * 127.0), -127, 127).astype(np.int8)


class PeakBuilder:
    def __init__(self):
        self._lock = threading.Lock()
        self._lo = np.zeros(0, np.float32)
        self._hi = np.zeros(0, np.float32)
        self._chunks = []
        self._pending = []
        self._count = 0
        self._sent = 0

    def add(self, x):
        if x.shape[0] == 0:
            return
        lo = np.concatenate([self._lo, across_channels(x, np.minimum)])
        hi = np.concatenate([self._hi, across_channels(x, np.maximum)])
        full = lo.shape[0] // BIN
        with self._lock:
            if full:
                mins = lo[:full * BIN].reshape(full, BIN).min(axis=1)
                maxs = hi[:full * BIN].reshape(full, BIN).max(axis=1)
                chunk = np.stack([compand(mins), compand(maxs)], axis=1)
                self._chunks.append(chunk)
                if len(self._chunks) > 512:
                    self._chunks = [np.concatenate(self._chunks)]
                self._pending.append(chunk)
                self._count += full
            self._lo = lo[full * BIN:]
            self._hi = hi[full * BIN:]

    def new_bins(self):
        """Bins not handed out yet (for the live waveform while recording)."""
        with self._lock:
            start = self._sent
            if not self._pending:
                return np.zeros((0, 2), np.int8), start
            new = np.concatenate(self._pending)
            self._pending = []
            self._sent = self._count
            return new, start

    def all(self):
        with self._lock:
            parts = list(self._chunks)
            if self._lo.shape[0]:
                parts.append(np.array([[compand(self._lo.min()), compand(self._hi.max())]], np.int8))
        return np.concatenate(parts) if parts else np.zeros((0, 2), np.int8)


def peaks_path(wav_path):
    return wav_path + ".peaks"


def save(wav_path, peaks):
    tmp = peaks_path(wav_path) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(np.ascontiguousarray(peaks, np.int8).tobytes())
    os.replace(tmp, peaks_path(wav_path))


def compute(reader, chunk=BIN * 1024):
    b = PeakBuilder()
    for start in range(0, reader.frames, chunk):
        b.add(reader.read(start, min(chunk, reader.frames - start)))
    return b.all()


def load_or_compute(wav_path):
    """Peaks for a recorded file, rebuilding the cache if it is missing or stale."""
    from .wavio import WavReader

    pp = peaks_path(wav_path)
    reader = WavReader(wav_path)
    expected = -(-reader.frames // BIN)
    try:
        if os.path.getmtime(pp) >= os.path.getmtime(wav_path) and os.path.getsize(pp) == expected * 2:
            with open(pp, "rb") as f:
                return np.frombuffer(f.read(), np.int8).reshape(-1, 2)
    except OSError:
        pass
    p = compute(reader)
    try:
        save(wav_path, p)
    except OSError:
        pass
    return p
