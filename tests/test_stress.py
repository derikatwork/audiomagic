"""Load test: many inputs with every effect on must still record perfectly.

Effects, meters and monitoring run on their own thread and may fall behind
on a slow machine, but the recording itself must never lose a sample.
"""
import os
import subprocess
import sys
import time

import numpy as np
import pytest

from audiomagic import SAMPLE_RATE as RATE
from audiomagic.wavio import WavReader

from .test_pipewire import node_id, pytestmark, wait_for  # noqa: F401  (needs PipeWire)


@pytest.fixture(scope="module")
def load_devices():
    p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "fake_devices.py")],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        assert wait_for(lambda: node_id("am-load-iface") is not None and node_id("AmLoadApp") is not None)
        yield
    finally:
        p.terminate()
        p.wait(10)


@pytest.fixture
def engine(tmp_path, load_devices):
    from audiomagic.config import Settings
    from audiomagic.engine import Engine
    from audiomagic.project import ProjectStore

    e = Engine(store=ProjectStore(str(tmp_path / "p")), settings=Settings(str(tmp_path / "s.json")))
    e.start()
    yield e
    e.shutdown()


def glitches(x, freq):
    """Discontinuities in a pure sine: the two-term recurrence must hold everywhere."""
    x = x.astype(np.float64)
    c = 2 * np.cos(2 * np.pi * freq / RATE)
    return int(np.count_nonzero(np.abs(x[2:] - c * x[1:-1] + x[:-2]) > 2e-3))


def clicks(x):
    loud = np.nonzero((np.abs(x[1:]) > 0.3) & (np.abs(x[:-1]) <= 0.3))[0]
    out = []
    for o in loud:
        if not out or o - out[-1] > 4800:
            out.append(o)
    return np.array(out)


def test_many_inputs_with_all_effects_record_perfectly(engine):
    items = [{"source": {"kind": "device", "node": "am-load-iface", "channels": [k], "device_channels": 8},
              "name": f"In {k + 1}"} for k in range(8)]
    items += [{"source": {"kind": "app", "app": "AmLoadApp"}, "name": "App"},
              {"source": {"kind": "tone", "freq": 300}, "name": "Tone A"},
              {"source": {"kind": "tone", "freq": 5000}, "name": "Tone B"}]
    ids = engine.add_tracks(items)
    for tid in ids:
        engine.update_track(tid, {"preset": "voice"})
    for tid in (ids[0], ids[8]):
        engine.update_track(tid, {"monitor": True})
    assert wait_for(lambda: all(t["status"] == "live" for t in engine.state()["tracks"]), engine=engine)

    take_id = engine.start_recording()
    end = time.time() + 15
    while time.time() < end:
        engine.poll()
        time.sleep(0.1)
    engine.stop_recording()
    take = engine.project.take(take_id)
    data = {}
    for f in take.tracks:
        r = WavReader(engine.project.abspath(f["file"]))
        assert r.frames == take.duration, f["name"]
        data[f["name"]] = r.read(0, r.frames)[:, 0]

    for k in range(7):  # every interface input: right channel, not a single sample lost
        x = data[f"In {k + 1}"][RATE // 2:]
        spec = np.abs(np.fft.rfft(x[:RATE * 2] * np.hanning(RATE * 2)))
        assert abs(np.fft.rfftfreq(RATE * 2, 1 / RATE)[np.argmax(spec)] - (200 + 100 * k)) < 2
        assert glitches(x, 200 + 100 * k) == 0, f"In {k + 1}"
    for name, freq in (("Tone A", 300), ("Tone B", 5000)):
        assert glitches(data[name][RATE // 2:], freq) == 0, name

    a, b = clicks(data["In 8"]), clicks(data["App"])
    assert a.size >= 25 and set(np.diff(a).tolist()) == {24000}  # perfectly regular: no drops, no repeats
    n = min(a.size, b.size)
    offsets = b[:n] - a[:n]
    assert offsets.max() - offsets.min() <= 2  # the two inputs stay locked together (no drift)
