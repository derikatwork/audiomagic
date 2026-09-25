import os
import struct

import numpy as np
import pytest

from audiomagic import dsp, wavio
from audiomagic.edits import Edits, History, Timeline, op_cut, op_silence, op_trim, op_unsilence, merge_ranges
from audiomagic.project import Project, ProjectStore

RATE = 48000


def sine(freq, secs, amp=0.5, ch=1):
    t = np.arange(int(secs * RATE)) / RATE
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.repeat(x[:, None], ch, axis=1)


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12)


# ------------------------------------------------------------------ wavio

def test_wav_roundtrip_24bit(tmp_path):
    p = str(tmp_path / "a.wav")
    x = sine(440, 1.0, ch=2)
    x[:, 1] *= -0.5
    w = wavio.WavWriter(p, 2)
    for i in range(0, len(x), 1000):
        w.write(x[i:i + 1000])
    w.close()
    r = wavio.WavReader(p)
    assert r.frames == len(x) and r.channels == 2 and r.rate == RATE
    y = r.read(0, len(x))
    assert np.max(np.abs(y - x)) < 2e-7
    # reading past the end pads with silence
    z = r.read(len(x) - 10, 20)
    assert z.shape == (20, 2) and np.all(z[10:] == 0)
    assert np.all(r.read(-5, 5) == 0)


def test_pcm24_decoding_is_exact():
    ints = np.array([0, 1, -1, 8388607, -8388608, 123456, -654321, 255, 256, -256], np.int32)
    raw = ints.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    assert np.array_equal(wavio.pcm24_to_float(raw), ints / np.float32(8388608.0))
    assert wavio.pcm24_to_float(raw[:3]).tolist() == [0.0]
    assert wavio.pcm24_to_float(b"").shape == (0,)


def test_wav_crash_repair(tmp_path):
    p = str(tmp_path / "crash.wav")
    w = wavio.WavWriter(p, 1, sync_interval=100.0)
    w.write(sine(100, 0.5))
    w._f.flush()  # simulate a crash: data on disk, header never updated
    w._f.close()
    w._f = None
    with open(p, "rb") as f:
        f.seek(40)
        assert struct.unpack("<I", f.read(4))[0] == 0
    assert wavio.read_info(p).frames == 24000  # reader already copes
    assert wavio.repair(p) == 24000
    with open(p, "rb") as f:
        f.seek(40)
        assert struct.unpack("<I", f.read(4))[0] == 24000 * 3


def test_wav_header_synced_while_writing(tmp_path):
    p = str(tmp_path / "live.wav")
    w = wavio.WavWriter(p, 1, sync_interval=0.1)
    w.write(sine(100, 0.25))
    with open(p, "rb") as f:
        f.seek(40)
        assert struct.unpack("<I", f.read(4))[0] == 12000 * 3
    w.close()


# -------------------------------------------------------------------- dsp

def test_noise_suppressor_reconstruction_and_delay():
    """With unity gain the STFT path reconstructs the input exactly, delayed by `latency`."""
    ns = dsp.NoiseSuppressor(1)
    ns._gain = lambda P: 1.0
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((RATE, 1)) * 0.3).astype(np.float32)
    y = np.concatenate([ns.process(x[i:i + 700]) for i in range(0, len(x), 700)])
    assert y.shape == x.shape
    L = ns.latency
    assert np.max(np.abs(y[:L])) < 1e-6
    assert np.max(np.abs(y[L:] - x[:-L])) < 1e-5


def speechlike(secs, amp=0.3, seed=1):
    """Tone bursts with gaps and changing pitch, like syllables."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(secs * RATE), np.float32)
    pos = 0
    while pos < len(out):
        on = int(rng.uniform(0.12, 0.35) * RATE)
        off = int(rng.uniform(0.08, 0.25) * RATE)
        f0 = rng.uniform(120, 250)
        t = np.arange(min(on, len(out) - pos)) / RATE
        burst = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
        env = np.sin(np.pi * np.arange(len(t)) / max(1, len(t))) ** 0.5
        out[pos:pos + len(t)] = amp * 0.5 * burst * env
        pos += on + off
    return out[:, None]


def test_noise_suppressor_reduces_noise_keeps_tone():
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal((RATE * 4, 1)) * 0.02).astype(np.float32)
    tone = speechlike(4.0)
    # 2 s of noise only, then "speech" + noise
    x = noise.copy()
    x[RATE * 2:] += tone[RATE * 2:]
    ns = dsp.NoiseSuppressor(1, strength=0.6)
    y = np.concatenate([ns.process(x[i:i + 1024]) for i in range(0, len(x), 1024)])
    L = ns.latency
    y = y[L:]
    noise_only_in = rms_db(x[RATE:RATE * 2 - L])
    noise_only_out = rms_db(y[RATE:RATE * 2 - L])
    assert noise_only_out < noise_only_in - 12  # at least 12 dB quieter
    tone_in = rms_db(x[RATE * 3:RATE * 4 - L])
    tone_out = rms_db(y[RATE * 3:RATE * 4 - L])
    assert abs(tone_out - tone_in) < 1.5  # speech survives


def test_noise_suppressor_block_size_invariant():
    rng = np.random.default_rng(3)
    x = (rng.standard_normal((RATE, 2)) * 0.1).astype(np.float32)
    a = dsp.NoiseSuppressor(2).process(x)
    ns = dsp.NoiseSuppressor(2)
    b = np.concatenate([ns.process(x[i:i + 333]) for i in range(0, len(x), 333)])
    assert np.allclose(a, b, atol=1e-6)


def test_gate_closes_on_quiet_and_opens_on_loud():
    g = dsp.Gate()
    g.set(threshold=-40, range=-60, attack=1, hold=50, release=50)
    quiet = sine(200, 1.0, amp=0.001)  # -60 dBFS
    loud = sine(200, 0.5, amp=0.3)
    y_q = g.process(quiet)
    assert rms_db(y_q[RATE // 2:]) < rms_db(quiet) - 50
    y_l = g.process(loud)
    assert abs(rms_db(y_l[RATE // 10:]) - rms_db(loud[RATE // 10:])) < 0.5


def test_eq_shapes():
    fx = dsp.merge_fx({"eq": {"on": True, "lowcut": True, "lowcut_hz": 100, "low": 0, "mid": 6, "high": 0}})
    d = dsp.TrackDSP(1)
    d.configure(fx)
    low = d.process(sine(30, 1.0))
    assert rms_db(low[RATE // 2:]) < rms_db(sine(30, 1.0)) - 12  # low cut
    d = dsp.TrackDSP(1)
    d.configure(fx)
    mid = d.process(sine(dsp.EQ_MID_HZ, 1.0))
    assert 5.0 < rms_db(mid[RATE // 2:]) - rms_db(sine(dsp.EQ_MID_HZ, 1.0)) < 7.0


def test_trackdsp_off_is_identity():
    d = dsp.TrackDSP(2)
    d.configure(dsp.DEFAULT_FX)
    x = sine(440, 0.1, ch=2)
    assert np.array_equal(d.process(x), x)
    assert d.latency == 0


def test_merge_fx_fills_defaults():
    fx = dsp.merge_fx({"ns": {"on": True}, "bogus": {}})
    assert fx["ns"] == {"on": True, "strength": 0.5}
    assert "bogus" not in fx and fx["gate"]["threshold"] == -45.0


def test_meter_and_pan():
    m = dsp.Meter()
    m.update(np.full((100, 1), 0.5, dtype=np.float32))
    peak, rms, clip = m.read()
    assert peak == pytest.approx(-6.0, abs=0.1) and rms == pytest.approx(-6.0, abs=0.1) and not clip
    assert m.read()[0] == -120.0  # reset after read
    m.update(np.full((10, 1), 0.1, dtype=np.float32), raw_peak=1.0)
    assert m.read()[2] is True
    assert dsp.balance_gains(0) == (1.0, 1.0)
    assert dsp.balance_gains(-1) == (1.0, 0.0)
    st = dsp.to_stereo(np.ones((4, 1), np.float32), 0.5)
    assert np.allclose(st[0], [0.5, 1.0])


# ------------------------------------------------------------------ edits

def test_timeline_and_cut():
    e = Edits()
    tl = Timeline(e, 1000)
    assert tl.length == 1000 and tl.segments == [(0, 1000)]
    e = op_cut(e, 1000, 100, 200)
    tl = Timeline(e, 1000)
    assert tl.segments == [(0, 100), (200, 1000)] and tl.length == 900
    # cut again across the join: output [50,150) = source [50,100) + [200,250)
    e = op_cut(e, 1000, 50, 150)
    assert e.cuts == [[50, 250]]
    assert Timeline(e, 1000).length == 800


def test_trim_and_pieces():
    e = op_cut(Edits(), 1000, 400, 500)
    e = op_trim(e, 1000, 300, 700)  # output 300..700 -> source 300..400 + 500..800
    tl = Timeline(e, 1000)
    assert tl.segments == [(300, 400), (500, 800)]
    assert list(tl.pieces(50, 100)) == [(0, 350, 50, 0), (50, 500, 50, 1)]


def test_silence_and_unsilence():
    e = op_silence(Edits(), 1000, "t1", 100, 300)
    e = op_silence(e, 1000, "t1", 250, 400)
    assert e.silences == {"t1": [[100, 400]]}
    e = op_unsilence(e, 1000, "t1", 200, 250)
    assert e.silences == {"t1": [[100, 200], [250, 400]]}
    e = op_unsilence(e, 1000, "t1", 0, 1000)
    assert e.silences == {}


def test_history_undo_redo():
    h = History()
    e0 = Edits()
    e1 = op_cut(e0, 1000, 0, 10)
    h.push(e0)
    back = h.undo(e1)
    assert back.to_dict() == e0.to_dict()
    fwd = h.redo(back)
    assert fwd.to_dict() == e1.to_dict()


def test_merge_ranges():
    assert merge_ranges([[5, 10], [0, 3], [3, 4], [9, 12], [20, 20]]) == [[0, 4], [5, 12]]


# ---------------------------------------------------------------- project

def test_project_store_create_list_reopen(tmp_path):
    store = ProjectStore(str(tmp_path))
    p = store.create("My Show")
    tid = p.next_id("t")
    from audiomagic.project import Track
    p.tracks.append(Track({"id": tid, "name": "Host", "source": {"kind": "device", "node": "x", "channels": [1], "label": "Mic"}}))
    p.save()
    p2 = store.create("My Show", template=p)
    assert os.path.basename(p2.path) == "My Show (2)"
    assert [t.name for t in p2.tracks] == ["Host"]
    names = [x["name"] for x in store.list()]
    assert sorted(names) == ["My Show", "My Show"]
    q = store.open(p.path)
    assert q.tracks[0].channels == 1 and q.tracks[0].fx["ns"]["on"] is False


def test_project_recovers_interrupted_take(tmp_path):
    store = ProjectStore(str(tmp_path))
    p = store.create("Crashy")
    os.makedirs(os.path.join(p.path, "audio/take-001"))
    w = wavio.WavWriter(os.path.join(p.path, "audio/take-001/a.wav"), 1, sync_interval=100)
    w.write(sine(100, 0.1))
    w._f.flush()
    from audiomagic.project import Take
    p.takes.append(Take({"id": "k1", "number": 1, "state": "recording",
                         "tracks": [{"track_id": "t1", "file": "audio/take-001/a.wav", "channels": 1}]}))
    p.save()
    q = Project.load(p.path)
    assert q.takes[0].state == "recovered" and q.takes[0].duration == 4800
