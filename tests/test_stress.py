"""Load test: many inputs with every effect on must still record perfectly.

Effects, meters and monitoring run on their own thread and may fall behind
on a slow machine, but the recording itself must never lose a sample.

The virtual devices can themselves drop audio on a starved machine (a busy
VM, say), so PipeWire's own recorder takes a reference copy alongside; the
test only blames the app for gaps the reference doesn't have.
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
        assert wait_for(lambda: all(node_id(n) is not None for n in ("am-load-iface", "AmLoadApp", "am-load-mon")))
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
    """Start of each click burst (480 samples long, every 24000)."""
    loud = np.nonzero((np.abs(x[1:]) > 0.3) & (np.abs(x[:-1]) <= 0.3))[0]
    # a click starts after silence (a take can begin in the middle of one)
    loud = [o for o in loud if o >= 100 and np.abs(x[o - 100:o - 3]).max() < 0.05]
    out = []
    for o in loud:
        if not out or o - out[-1] > 4800:
            out.append(o)
    return np.array(out)


def test_many_inputs_with_all_effects_record_perfectly(engine, tmp_path):
    items = [{"source": {"kind": "device", "node": "am-load-iface", "channels": [k], "device_channels": 8},
              "name": f"In {k + 1}"} for k in range(8)]
    items += [{"source": {"kind": "app", "app": "AmLoadApp"}, "name": "App"},
              {"source": {"kind": "tone", "freq": 300}, "name": "Tone A"},
              {"source": {"kind": "tone", "freq": 5000}, "name": "Tone B"}]
    ids = engine.add_tracks(items)
    for tid in ids:
        engine.update_track(tid, {"preset": "voice"})
    # monitor into the test's own output, whatever this machine's default output is
    engine.set_output("am-load-mon")
    for tid in (ids[0], ids[8]):
        engine.update_track(tid, {"monitor": True})
    assert wait_for(lambda: all(t["status"] == "live" for t in engine.state()["tracks"]), engine=engine)

    # PipeWire's own recorder takes a reference copy of the virtual devices at the same time:
    # if that one has gaps too, the devices dropped audio and the app can't be judged
    refs = {"iface": ["--target", "am-load-iface", "--channels", "8", "--channel-map", ",".join(f"AUX{k}" for k in range(8))],
            "app": ["--target", "AmLoadApp", "--channels", "2"]}
    ref_procs = [subprocess.Popen(["pw-record", *args, "--format", "f32", str(tmp_path / f"ref-{name}.wav")])
                 for name, args in refs.items()]
    take_id = engine.start_recording()
    end = time.time() + 15
    while time.time() < end:
        engine.poll()
        time.sleep(0.1)
    engine.stop_recording()
    for p in ref_procs:
        p.terminate()
        p.wait(10)

    take = engine.project.take(take_id)
    data = {}
    for f in take.tracks:
        r = WavReader(engine.project.abspath(f["file"]))
        assert r.frames == take.duration, f["name"]
        data[f["name"]] = r.read(0, r.frames)[:, 0]
    for k in range(7):  # every interface input landed on the right track
        x = data[f"In {k + 1}"][RATE // 2:]
        spec = np.abs(np.fft.rfft(x[:RATE * 2] * np.hanning(RATE * 2)))
        assert abs(np.fft.rfftfreq(RATE * 2, 1 / RATE)[np.argmax(spec)] - (200 + 100 * k)) < 2

    problems = signal_problems({f"In {k + 1}": data[f"In {k + 1}"] for k in range(8)}, data["App"])
    problems += [name for name, freq in (("Tone A", 300), ("Tone B", 5000)) if glitches(data[name][RATE // 2:], freq)]
    if problems:
        iface = WavReader(str(tmp_path / "ref-iface.wav"))
        iface = iface.read(0, iface.frames)
        app = WavReader(str(tmp_path / "ref-app.wav"))
        ref_problems = signal_problems({f"In {k + 1}": iface[:, k] for k in range(8)}, app.read(0, app.frames)[:, 0],
                                       check_sync=False)
        if ref_problems:
            pytest.skip(f"the virtual test devices dropped audio on this machine (PipeWire's own recorder "
                        f"lost it too: {ref_problems}); the app's recording had {problems}")
    assert not problems  # not a single sample lost, and the inputs stay locked together


def signal_problems(interface, app, check_sync=True):
    """What's wrong with a recording of the virtual devices (an empty list if it is perfect)."""
    problems = [f"{name} has gaps" for k, (name, x) in enumerate(list(interface.items())[:7])
                if glitches(x[RATE // 2:], 200 + 100 * k)]
    a, b = clicks(interface["In 8"]), clicks(app)
    for name, c in (("In 8", a), ("App", b)):
        expected = len(interface["In 8"]) // 24000 - 1
        if c.size < expected or set(np.diff(c).tolist()) != {24000}:  # perfectly regular: no drops, no repeats
            problems.append(f"{name} clicks are irregular")
    if check_sync and not problems:
        offsets = np.array([b[np.argmin(np.abs(b - t))] - t for t in a])  # each click against its twin
        if offsets.max() - offsets.min() > 2:
            problems.append(f"App drifted against In 8 by {offsets.max() - offsets.min()} samples")
    return problems


def test_a_stalled_python_loses_nothing(engine, tmp_path):
    """Python can stall for a while (a long GC pass, a busy thread holding the
    GIL); PipeWire must keep delivering meanwhile and the audio catch up after."""
    engine.add_tracks([{"source": {"kind": "device", "node": "am-load-iface", "channels": [k], "device_channels": 8},
                              "name": f"In {k + 1}"} for k in range(8)] +
                            [{"source": {"kind": "app", "app": "AmLoadApp"}, "name": "App"}])
    assert wait_for(lambda: all(t["status"] == "live" for t in engine.state()["tracks"]), engine=engine)
    take_id = engine.start_recording()
    time.sleep(2)
    old = sys.getswitchinterval()
    sys.setswitchinterval(5.0)  # this thread now keeps the GIL until it sleeps
    try:
        end = time.perf_counter() + float(os.environ.get("AM_STALL", "2.0"))  # longer than BlockClock.WINDOW_NS
        while time.perf_counter() < end:
            pass
    finally:
        sys.setswitchinterval(old)
    end = time.time() + 3
    while time.time() < end:
        engine.poll()
        time.sleep(0.1)
    engine.stop_recording()
    take = engine.project.take(take_id)
    data = {f["name"]: WavReader(engine.project.abspath(f["file"])).read(0, take.duration)[:, 0] for f in take.tracks}
    assert take.duration > 5 * RATE
    assert not signal_problems({f"In {k + 1}": data[f"In {k + 1}"] for k in range(8)}, data["App"])
