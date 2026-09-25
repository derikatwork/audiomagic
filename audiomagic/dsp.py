"""Per-track audio processing: low cut, noise suppression, noise gate, EQ.

Everything here works on float32 blocks shaped (frames, channels) and keeps
its own state between calls, so the same code serves live monitoring,
playback and export. Block sizes may vary from call to call.
"""

import copy
import math
import threading
from collections import deque

import numpy as np
from scipy.signal import sosfilt

from .util import db_to_lin, lin_to_db

DEFAULT_FX = {
    "ns": {"on": False, "strength": 0.5},
    "gate": {"on": False, "threshold": -45.0, "range": -40.0, "attack": 1.0, "hold": 150.0, "release": 150.0},
    "eq": {"on": False, "lowcut": True, "lowcut_hz": 80.0, "low": 0.0, "mid": 0.0, "high": 0.0},
}

# Starting points offered in the effects panel.
FX_PRESETS = {
    "voice": {
        "ns": {"on": True, "strength": 0.5},
        "gate": {"on": True, "threshold": -50.0},
        "eq": {"on": True, "lowcut": True, "lowcut_hz": 80.0, "low": -1.0, "mid": 2.0, "high": 1.5},
    },
    "off": copy.deepcopy(DEFAULT_FX),
}

EQ_LOW_HZ = 150.0
EQ_MID_HZ = 2500.0
EQ_HIGH_HZ = 8000.0


def merge_fx(fx):
    """Fill any missing keys in a stored fx dict from the defaults."""
    out = copy.deepcopy(DEFAULT_FX)
    for group, vals in (fx or {}).items():
        if group in out and isinstance(vals, dict):
            out[group].update({k: v for k, v in vals.items() if k in out[group]})
    return out


# ---------------------------------------------------------------- biquads

def _norm(b0, b1, b2, a0, a1, a2):
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def highpass(f, rate, q=0.7071):
    w = 2 * math.pi * f / rate
    c, a = math.cos(w), math.sin(w) / (2 * q)
    return _norm((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + a, -2 * c, 1 - a)


def peaking(f, gain_db, rate, q=0.8):
    if abs(gain_db) < 1e-6:
        return list(IDENTITY)
    A = 10 ** (gain_db / 40)
    w = 2 * math.pi * f / rate
    c, a = math.cos(w), math.sin(w) / (2 * q)
    return _norm(1 + a * A, -2 * c, 1 - a * A, 1 + a / A, -2 * c, 1 - a / A)


def low_shelf(f, gain_db, rate):
    if abs(gain_db) < 1e-6:
        return list(IDENTITY)
    A = 10 ** (gain_db / 40)
    w = 2 * math.pi * f / rate
    c, s = math.cos(w), math.sin(w)
    al = s / 2 * math.sqrt(2)
    sa = 2 * math.sqrt(A) * al
    return _norm(
        A * ((A + 1) - (A - 1) * c + sa),
        2 * A * ((A - 1) - (A + 1) * c),
        A * ((A + 1) - (A - 1) * c - sa),
        (A + 1) + (A - 1) * c + sa,
        -2 * ((A - 1) + (A + 1) * c),
        (A + 1) + (A - 1) * c - sa,
    )


def high_shelf(f, gain_db, rate):
    if abs(gain_db) < 1e-6:
        return list(IDENTITY)
    A = 10 ** (gain_db / 40)
    w = 2 * math.pi * f / rate
    c, s = math.cos(w), math.sin(w)
    al = s / 2 * math.sqrt(2)
    sa = 2 * math.sqrt(A) * al
    return _norm(
        A * ((A + 1) + (A - 1) * c + sa),
        -2 * A * ((A - 1) + (A + 1) * c),
        A * ((A + 1) + (A - 1) * c - sa),
        (A + 1) - (A - 1) * c + sa,
        2 * ((A - 1) - (A + 1) * c),
        (A + 1) - (A - 1) * c - sa,
    )


class Biquads:
    """A fixed number of biquad sections with state kept across blocks."""

    def __init__(self, channels, sections):
        self.sos = np.array([IDENTITY] * sections, dtype=np.float64)
        self.zi = np.zeros((sections, 2, channels))
        self.active = False

    def set(self, rows):
        self.sos = np.array(rows, dtype=np.float64)
        self.active = any(r != IDENTITY for r in rows)
        if not self.active:
            self.zi[:] = 0

    def process(self, x):
        if not self.active or x.shape[0] == 0:
            return x
        y, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y.astype(np.float32)


# ------------------------------------------------------ noise suppression

class NoiseSuppressor:
    """Spectral noise suppression for steady background noise (fans, hiss,
    hum, room tone). Noise is tracked continuously with minimum statistics,
    and a decision-directed Wiener gain is applied per frequency bin.

    Output is delayed by ``latency`` samples.
    """

    N = 1024
    H = 512
    SMOOTH = 0.85      # power smoothing per frame
    SUBWIN = 16        # frames per minimum sub-window (~170 ms)
    NSUB = 8           # sub-windows remembered (~1.4 s)
    BIAS = 2.1         # minimum-statistics bias compensation (see tests)
    DD = 0.96          # decision-directed smoothing

    latency = N

    def __init__(self, channels, strength=0.5):
        n = self.N
        self.channels = channels
        self.win = np.sqrt(np.hanning(n + 1)[:n]).astype(np.float64)[:, None]
        self.set_strength(strength)
        self._reset_buffers()
        bins = n // 2 + 1
        self.S = None
        self.sub_min = np.full((bins, channels), np.inf)
        self.hist = deque(maxlen=self.NSUB)
        self.sub_count = 0
        self.G_prev = np.ones((bins, channels))
        self.gamma_prev = np.ones((bins, channels))
        self.noise = None

    def _reset_buffers(self):
        n, h = self.N, self.H
        self.inbuf = np.zeros((n - h, self.channels))
        self.outbuf = np.zeros((h, self.channels))
        self.ola = np.zeros((n, self.channels))

    def set_strength(self, s):
        s = min(1.0, max(0.0, float(s)))
        self.strength = s
        self.floor = db_to_lin(-8.0 - 22.0 * s)
        self.oversub = 1.0 + s

    def _gain(self, P):
        if self.S is None:
            self.S = P.copy()
        else:
            self.S = self.SMOOTH * self.S + (1 - self.SMOOTH) * P
        np.minimum(self.sub_min, self.S, out=self.sub_min)
        self.sub_count += 1
        if self.sub_count >= self.SUBWIN:
            self.hist.append(self.sub_min.copy())
            self.sub_min = self.S.copy()
            self.sub_count = 0
        pmin = self.sub_min
        for h in self.hist:
            pmin = np.minimum(pmin, h)
        self.noise = pmin * self.BIAS + 1e-12
        gamma = P / (self.noise * self.oversub)
        xi = self.DD * (self.G_prev ** 2) * self.gamma_prev + (1 - self.DD) * np.maximum(gamma - 1.0, 0.0)
        G = xi / (1.0 + xi)
        G = np.maximum(G, self.floor)
        # light smoothing across frequency reduces "musical noise"
        G[1:-1] = 0.25 * G[:-2] + 0.5 * G[1:-1] + 0.25 * G[2:]
        self.G_prev = G
        self.gamma_prev = gamma
        return G

    def process(self, x):
        n_in = x.shape[0]
        N, H = self.N, self.H
        self.inbuf = np.concatenate([self.inbuf, x.astype(np.float64)])
        outs = []
        pos = 0
        while self.inbuf.shape[0] - pos >= N:
            frame = self.inbuf[pos:pos + N] * self.win
            X = np.fft.rfft(frame, axis=0)
            P = X.real ** 2 + X.imag ** 2
            Y = X * self._gain(P)
            y = np.fft.irfft(Y, n=N, axis=0) * self.win
            self.ola += y
            outs.append(self.ola[:H].copy())
            self.ola[:-H] = self.ola[H:]
            self.ola[-H:] = 0
            pos += H
        if pos:
            self.inbuf = self.inbuf[pos:]
        if outs:
            self.outbuf = np.concatenate([self.outbuf] + outs)
        out = self.outbuf[:n_in]
        self.outbuf = self.outbuf[n_in:]
        return out.astype(np.float32)

    def warmup(self, x):
        """Learn the noise floor from ``x`` without producing output."""
        self.process(x)
        self._reset_buffers()


# ---------------------------------------------------------------- gate

class Gate:
    """Noise gate with hold and smooth attack/release (1 ms control rate)."""

    SUB = 48

    def __init__(self, rate=48000):
        self.rate = rate
        self.g = 1.0
        self.hold_left = 0
        self.is_open = True
        self.set()

    def set(self, threshold=-45.0, range=-40.0, attack=1.0, hold=150.0, release=150.0, **_):
        sub_ms = 1000.0 * self.SUB / self.rate
        self.thr_open = db_to_lin(threshold)
        self.thr_close = db_to_lin(threshold - 4.0)
        self.floor = db_to_lin(range)
        self.hold_subs = int(hold / sub_ms)
        self.att = math.exp(-sub_ms / max(0.05, attack))
        self.rel = math.exp(-sub_ms / max(1.0, release))

    def process(self, x):
        n = x.shape[0]
        if n == 0:
            return x
        sub = self.SUB
        nsub = -(-n // sub)
        pad = nsub * sub - n
        a = np.abs(x).max(axis=1)
        if pad:
            a = np.concatenate([a, np.zeros(pad, dtype=a.dtype)])
        peaks = a.reshape(nsub, sub).max(axis=1).tolist()
        gains = [0.0] * nsub
        g, hold, is_open = self.g, self.hold_left, self.is_open
        thr_o, thr_c, floor = self.thr_open, self.thr_close, self.floor
        att, rel, hold_subs = self.att, self.rel, self.hold_subs
        for i, p in enumerate(peaks):
            if p >= thr_o:
                is_open = True
                hold = hold_subs
            elif p < thr_c:
                if hold > 0:
                    hold -= 1
                else:
                    is_open = False
            target = 1.0 if is_open else floor
            coef = att if target > g else rel
            g = target + (g - target) * coef
            gains[i] = g
        knots_x = np.arange(0, nsub + 1) * sub
        knots_y = np.array([self.g] + gains)
        env = np.interp(np.arange(1, n + 1), knots_x, knots_y).astype(np.float32)
        self.g, self.hold_left, self.is_open = g, hold, is_open
        return x * env[:, None]


# ------------------------------------------------------------ track chain

class TrackDSP:
    """Low cut -> noise suppression -> gate -> EQ for one track."""

    def __init__(self, channels, rate=48000):
        self.channels = channels
        self.rate = rate
        self.pre = Biquads(channels, 1)
        self.post = Biquads(channels, 3)
        self.ns = None
        self.gate = Gate(rate)
        self._fx = None
        self.gate_on = False

    def configure(self, fx):
        fx = merge_fx(fx)
        if fx == self._fx:
            return
        old = self._fx
        self._fx = fx
        eq = fx["eq"]
        if eq["on"] and eq["lowcut"]:
            self.pre.set([highpass(float(eq["lowcut_hz"]), self.rate)])
        else:
            self.pre.set([IDENTITY])
        if eq["on"]:
            self.post.set([
                low_shelf(EQ_LOW_HZ, float(eq["low"]), self.rate),
                peaking(EQ_MID_HZ, float(eq["mid"]), self.rate),
                high_shelf(EQ_HIGH_HZ, float(eq["high"]), self.rate),
            ])
        else:
            self.post.set([IDENTITY] * 3)
        ns = fx["ns"]
        if ns["on"]:
            if self.ns is None:
                self.ns = NoiseSuppressor(self.channels, ns["strength"])
            else:
                self.ns.set_strength(ns["strength"])
        else:
            self.ns = None
        g = fx["gate"]
        self.gate_on = bool(g["on"])
        if self.gate_on and (old is None or old["gate"] != g):
            self.gate.set(**g)

    @property
    def latency(self):
        return self.ns.latency if self.ns is not None else 0

    def warmup(self, x):
        if self.ns is not None:
            self.ns.warmup(self.pre.process(x) if self.pre.active else x)
            self.pre.zi[:] = 0

    def process(self, x):
        x = self.pre.process(x)
        if self.ns is not None:
            x = self.ns.process(x)
        if self.gate_on:
            x = self.gate.process(x)
        return self.post.process(x)


# ------------------------------------------------------------------ mixing

def balance_gains(pan):
    """Left/right gains for a pan position in [-1, 1]. Centre is unity."""
    pan = max(-1.0, min(1.0, float(pan)))
    return (1.0 if pan <= 0 else 1.0 - pan), (1.0 if pan >= 0 else 1.0 + pan)


def to_stereo(x, pan):
    gl, gr = balance_gains(pan)
    if x.shape[1] == 1:
        return np.concatenate([x * gl, x * gr], axis=1)
    return np.stack([x[:, 0] * gl, x[:, 1] * gr], axis=1)


def to_mono(x):
    if x.shape[1] == 1:
        return x
    return x.mean(axis=1, keepdims=True)


class Meter:
    """Accumulates peak/RMS between reads so no peak is ever missed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._peak = 0.0
        self._sumsq = 0.0
        self._count = 0
        self._clip = False

    def update(self, x, raw_peak=0.0):
        if x.size == 0:
            return
        p = float(np.max(np.abs(x)))
        s = float(np.sum(np.square(x, dtype=np.float64)))
        with self._lock:
            self._peak = max(self._peak, p)
            self._sumsq += s
            self._count += x.size
            if raw_peak >= 0.999 or p > 1.0:
                self._clip = True

    def read(self):
        with self._lock:
            peak, sumsq, count, clip = self._peak, self._sumsq, self._count, self._clip
            self._peak, self._sumsq, self._count, self._clip = 0.0, 0.0, 0, False
        rms = math.sqrt(sumsq / count) if count else 0.0
        return round(lin_to_db(peak), 1), round(lin_to_db(rms), 1), clip


def is_audible(track, any_solo):
    return not track.mute and (track.solo or not any_solo)
