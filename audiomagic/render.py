"""Rendering a take through its edits and effects (used by playback and export)."""

import math
import os

import numpy as np

from . import SAMPLE_RATE
from .dsp import Meter, TrackDSP, to_mono, to_stereo
from .edits import Timeline
from .util import db_to_lin
from .wavio import WavReader

JOIN = int(0.005 * SAMPLE_RATE)   # 5 ms ramps at cuts and silences
WARMUP = 2 * SAMPLE_RATE          # audio used to learn the noise floor


class TrackSource:
    """One track of a take, read through the edit list."""

    def __init__(self, reader, timeline, silences, clip_gain_db=0.0):
        self.reader = reader
        self.tl = timeline
        self.silences = silences or []
        self.gain = db_to_lin(clip_gain_db)
        self.channels = reader.channels

    def read(self, out_a, n):
        out = np.zeros((n, self.channels), np.float32)
        last = len(self.tl.segments) - 1
        for dst, src, length, seg_i in self.tl.pieces(out_a, n):
            x = self.reader.read(src, length)
            seg_a, seg_b = self.tl.segments[seg_i]
            pos = None
            env = None
            if seg_i > 0 and src - seg_a < JOIN:
                pos = np.arange(src, src + length)
                env = np.clip((pos - seg_a + 1) / JOIN, 0.0, 1.0)
            if seg_i < last and seg_b - (src + length) < JOIN:
                pos = np.arange(src, src + length) if pos is None else pos
                e2 = np.clip((seg_b - pos) / JOIN, 0.0, 1.0)
                env = e2 if env is None else np.minimum(env, e2)
            for sa, sb in self.silences:
                if sb + JOIN <= src or sa - JOIN >= src + length:
                    continue
                pos = np.arange(src, src + length) if pos is None else pos
                e3 = np.clip(np.maximum(sa - pos, pos - (sb - 1)) / JOIN, 0.0, 1.0)
                env = e3 if env is None else np.minimum(env, e3)
            if env is not None:
                x *= env[:, None].astype(np.float32)
            out[dst:dst + length] = x
        if self.gain != 1.0:
            out *= self.gain
        return out


def fade_envelope(pos, n, length, fade_in, fade_out):
    """Equal-power fade gains for output frames [pos, pos+n), or None."""
    fi = int(fade_in * SAMPLE_RATE)
    fo = int(fade_out * SAMPLE_RATE)
    if (fi <= 0 or pos >= fi) and (fo <= 0 or pos + n <= length - fo):
        return None
    p = np.arange(pos, pos + n, dtype=np.float64)
    env = np.ones(n)
    if fi > 0:
        env = np.minimum(env, np.sin(0.5 * math.pi * np.clip(p / fi, 0, 1)))
    if fo > 0:
        env = np.minimum(env, np.sin(0.5 * math.pi * np.clip((length - p) / fo, 0, 1)))
    return env.astype(np.float32)[:, None]


class _Item:
    __slots__ = ("track", "src", "dsp", "fx_seen", "in_pos", "meter", "peak")

    def __init__(self, track, src):
        self.track = track
        self.src = src
        self.dsp = None
        self.fx_seen = None
        self.in_pos = 0
        self.meter = Meter()
        self.peak = 0.0


class TakeRenderer:
    """Produces the mix (and optionally one stem per track) block by block.

    ``include(track)`` decides which tracks are heard; ``master()`` gives the
    master gain (linear). Both are asked on every block, so mute/solo/volume
    changes apply while playing.
    """

    def __init__(self, project, take, tracks=None, start=0, mix_channels=2, stems=False,
                 include=None, master=None):
        self.take = take
        self.timeline = Timeline(take.edits, take.duration)
        self.length = self.timeline.length
        self.mix_channels = mix_channels
        self.want_stems = stems
        self.include = include or (lambda tr: True)
        self.master = master or (lambda: db_to_lin(project.master_gain_db))
        self.mix_peak = 0.0
        self.items = []
        by_id = {t.id: t for t in (tracks if tracks is not None else project.tracks)}
        for tf in take.tracks:
            tr = by_id.get(tf["track_id"])
            path = project.abspath(tf["file"])
            if tr is None or not os.path.exists(path):
                continue
            reader = WavReader(path)
            src = TrackSource(reader, self.timeline, take.edits.silences.get(tr.id),
                              take.edits.clip_gain_db.get(tr.id, 0.0))
            self.items.append(_Item(tr, src))
        self.pos = max(0, min(int(start), self.length))
        for it in self.items:
            self._setup(it, warm=True)

    def _setup(self, it, warm):
        dsp = TrackDSP(it.src.channels)
        dsp.configure(it.track.fx)
        it.fx_seen = it.track.fx
        if dsp.ns is not None and warm and self.length:
            a = self.pos
            if self.length - a < WARMUP // 2:
                a = max(0, self.length - WARMUP)
            dsp.warmup(it.src.read(a, min(WARMUP, self.length - a)))
        lat = dsp.latency
        if lat:
            dsp.process(it.src.read(self.pos, lat))
        it.in_pos = self.pos + lat
        it.dsp = dsp

    @property
    def done(self):
        return self.pos >= self.length

    def next(self, n):
        if self.pos >= self.length:
            return None
        n = min(n, self.length - self.pos)
        env = fade_envelope(self.pos, n, self.length, self.take.edits.fade_in, self.take.edits.fade_out)
        mix = np.zeros((n, self.mix_channels), np.float32)
        stems = {} if self.want_stems else None
        for it in self.items:
            tr = it.track
            if tr.fx is not it.fx_seen:
                lat = it.dsp.latency
                it.dsp.configure(tr.fx)
                it.fx_seen = tr.fx
                if it.dsp.latency != lat:
                    self._setup(it, warm=False)
            x = it.src.read(it.in_pos, n)
            it.in_pos += n
            y = it.dsp.process(x)
            g = db_to_lin(tr.gain_db)
            if g != 1.0:
                y = y * g
            it.meter.update(y)
            if not self.include(tr):
                continue
            if env is not None:
                y = y * env
            if stems is not None:
                stems[tr.id] = y
                if y.size:
                    it.peak = max(it.peak, float(np.max(np.abs(y))))
            mix += to_stereo(y, tr.pan) if self.mix_channels == 2 else to_mono(y)
        m = self.master()
        if m != 1.0:
            mix *= m
        if mix.size:
            self.mix_peak = max(self.mix_peak, float(np.max(np.abs(mix))))
        self.pos += n
        return mix, stems
