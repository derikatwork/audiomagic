import numpy as np

from audiomagic import SAMPLE_RATE
from audiomagic.capture import BlockClock
from audiomagic.recorder import Recorder
from audiomagic.wavio import WavReader

NS = 1_000_000_000


class FakeCapture:
    _n = 0

    def __init__(self, synced=True):
        FakeCapture._n += 1
        self.generation = 1
        self.graph_synced = synced
        self.key = ("fake", FakeCapture._n)


def ramp_blocks(total, block, start_value=0):
    """Blocks whose sample values encode their absolute index."""
    vals = (np.arange(start_value, start_value + total) % 1000) / 1000.0
    x = vals.astype(np.float32)[:, None]
    return [x[i:i + block] for i in range(0, total, block)]


def test_block_clock_tracks_lower_envelope():
    c = BlockClock()
    t = 1_000 * NS
    starts = []
    rng = np.random.default_rng(0)
    for k in range(200):
        arrival = t + (k + 1) * 1024 * NS // SAMPLE_RATE + int(rng.uniform(0, 3e6))  # up to 3 ms late
        starts.append(c.update(1024, arrival))
    ideal = t + 199 * 1024 * NS // SAMPLE_RATE
    assert abs(starts[-1] - ideal) < 0.5e6  # within 0.5 ms despite 3 ms jitter


def test_tracks_align_to_record_start(tmp_path):
    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    a = rec.add_track("a", "a.wav", 1, True)
    b = rec.add_track("b", "b.wav", 1, True)
    cap_a, cap_b = FakeCapture(), FakeCapture()
    # both inputs carry the same signal; input b's blocks straddle the start differently
    full = ((np.arange(-4800, 48000) % 1000) / 1000.0).astype(np.float32)[:, None]

    def feed(rt, cap, block, offset):
        for i in range(0, full.shape[0], block):
            x = full[i:i + block]
            first = i - 4800  # sample index relative to T0
            rt.feed(x, t0 + int(first * NS / SAMPLE_RATE) + offset, cap)

    feed(a, cap_a, 1024, 0)
    feed(b, cap_b, 700, 0)
    rec.stop_frame = 40000
    rec.finalize(timeout=0.1)
    ya = WavReader(str(tmp_path / "a.wav")).read(0, 40000)
    yb = WavReader(str(tmp_path / "b.wav")).read(0, 40000)
    assert np.max(np.abs(ya - yb)) < 1e-6
    assert abs(ya[0, 0] - 0.0) < 1e-3  # starts exactly at the sample captured at T0


def test_restart_gap_is_filled_with_silence(tmp_path):
    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    rt = rec.add_track("a", "a.wav", 1, True)
    cap = FakeCapture()
    one = np.ones((4800, 1), np.float32)
    rt.feed(one, t0, cap)  # 0..0.1 s
    cap.generation += 1   # input restarted, next block arrives 0.5 s in
    rt.feed(one, t0 + NS // 2, cap)
    rec.stop_frame = 48000
    rec.finalize(timeout=0.1)
    y = WavReader(str(tmp_path / "a.wav")).read(0, 48000)[:, 0]
    assert np.all(y[:4800] > 0.999) and np.all(y[4800:24000] == 0) and np.all(y[24000:28800] > 0.999)


def test_unsynced_input_drift_is_corrected(tmp_path):
    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    rt = rec.add_track("net", "n.wav", 2, False)
    cap = FakeCapture(synced=False)
    block = np.ones((480, 2), np.float32)
    # the stream delivers 1% too few samples: without correction it would end 0.1 s short
    for k in range(1000):
        t = t0 + int(k * 485 * NS / SAMPLE_RATE)
        rt.feed(block, t, cap)
    expected = int(1000 * 485)
    assert abs(rt.written - expected) <= 2400 + 480
    rec.stop_frame = rt.written
    rec.finalize(timeout=0.1)


def test_device_that_loses_audio_is_kept_in_sync(tmp_path):
    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    good = rec.add_track("good", "g.wav", 1, True)
    lossy = rec.add_track("lossy", "l.wav", 1, True)
    cap_good, cap_lossy = FakeCapture(), FakeCapture()
    cap_good.key, cap_lossy.key = ("pw", "a"), ("pw", "b")
    block = np.ones((1024, 1), np.float32)
    for k in range(200):
        t = t0 + int(k * 1024 * NS / SAMPLE_RATE)
        good.feed(block, t, cap_good)
        if not 50 <= k < 60:  # the second device drops ten buffers (~0.2 s)
            lossy.feed(block * 0.5, t, cap_lossy)
    assert abs(lossy.written - good.written) < 1024 * 2
    assert lossy.filled_gaps >= 1
    rec.stop_frame = good.written
    rec.finalize(timeout=0.1)
    y = WavReader(str(tmp_path / "l.wav")).read(0, rec.stop_frame)[:, 0]
    # audio after the hole is back in its right place
    assert np.all(y[170 * 1024:199 * 1024] > 0.4)


def test_single_device_is_never_adjusted(tmp_path):
    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    a = rec.add_track("a", "a.wav", 1, True)
    b = rec.add_track("b", "b.wav", 1, True)
    cap = FakeCapture()
    cap.key = ("pw", "same")
    block = np.ones((1024, 1), np.float32)
    # clock drifts against the system clock by 1%: nothing to compare with, so nothing is inserted
    for k in range(300):
        t = t0 + int(k * 1024 * 1.01 * NS / SAMPLE_RATE)
        a.feed(block, t, cap)
        b.feed(block, t, cap)
    assert a.written == b.written == 300 * 1024 and a.filled_gaps == b.filled_gaps == 0
    rec.stop_frame = a.written
    rec.finalize(timeout=0.1)


def test_stop_waits_for_an_input_catching_up_but_not_for_a_silent_one(tmp_path):
    import threading
    import time

    rec = Recorder(str(tmp_path))
    t0 = rec.t_start
    late = rec.add_track("late", "late.wav", 1, True)
    dead = rec.add_track("dead", "dead.wav", 1, True)
    cap_l, cap_d = FakeCapture(), FakeCapture()
    blocks = ramp_blocks(48000, 1024)
    dead.feed(blocks[0], t0, cap_d)  # and then nothing more (unplugged)
    rec.stop_frame = 48000

    def backlog():  # after a stall, a second of queued audio arrives over 1.5 s
        for i, b in enumerate(blocks):
            late.feed(b, t0 + int(i * 1024 * NS / SAMPLE_RATE), cap_l)
            time.sleep(1.5 / len(blocks))

    th = threading.Thread(target=backlog)
    th.start()
    start = time.monotonic()
    rec.finalize()
    took = time.monotonic() - start
    th.join()
    y = WavReader(str(tmp_path / "late.wav")).read(0, 48000)[:, 0]
    assert np.max(np.abs(y - (np.arange(48000) % 1000) / 1000.0)) < 1e-4  # all of it, nothing padded
    assert 1.2 < took < 2.5  # waited for the input catching up, not the full timeout for the silent one
    assert WavReader(str(tmp_path / "dead.wav")).frames == 48000
