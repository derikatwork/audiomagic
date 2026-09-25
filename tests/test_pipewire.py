"""End-to-end tests against a running PipeWire (skipped when there is none).

The fixture creates its own fake devices:
* am-test-iface: a 2-channel "interface" (440 Hz on input 1, 880 Hz on input 2)
* am-test-sink:  a private output
* AmTestApp:     a program playing white noise into am-test-sink
"""

import json
import shutil
import subprocess
import time

import numpy as np
import pytest

from audiomagic import SAMPLE_RATE

pytestmark = pytest.mark.skipif(
    shutil.which("pw-cli") is None or subprocess.run(["pw-cli", "info", "0"], capture_output=True).returncode != 0,
    reason="needs a running PipeWire",
)


def dominant(x):
    x = x[:, 0] if x.ndim == 2 else x
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / SAMPLE_RATE)[np.argmax(spec)])


def node_id(name):
    from audiomagic.pw import decode_dump
    for o in decode_dump(subprocess.check_output(["pw-dump"]).decode()):
        if o.get("type") == "PipeWire:Interface:Node" and o["info"]["props"].get("node.name") == name:
            return o["id"]
    return None


def wait_for(cond, timeout=8.0, engine=None):
    end = time.time() + timeout
    while time.time() < end:
        if engine is not None:
            engine.poll()
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def fakes():
    procs = []
    subprocess.run(["pw-cli", "create-node", "adapter",
                    "{ factory.name=support.null-audio-sink node.name=am-test-sink node.description=AmTestSink "
                    "media.class=Audio/Sink audio.position=[ FL FR ] object.linger=true }"],
                   capture_output=True, check=True)
    procs.append(subprocess.Popen([
        "gst-launch-1.0", "-q", "interleave", "name=i", "!", "audioconvert", "!",
        "audio/x-raw,format=F32LE,rate=48000,channels=2", "!", "pipewiresink", "mode=provide", "client-name=AmIface",
        "stream-properties=props,media.class=Audio/Source,node.name=am-test-iface,node.description=AmTestIface",
        "audiotestsrc", "is-live=true", "freq=440", "volume=0.5", "!", "audio/x-raw,channels=1,rate=48000", "!", "i.",
        "audiotestsrc", "is-live=true", "freq=880", "volume=0.25", "!", "audio/x-raw,channels=1,rate=48000", "!", "i.",
    ]))
    procs.append(subprocess.Popen([
        "gst-launch-1.0", "-q", "audiotestsrc", "is-live=true", "wave=white-noise", "volume=0.2", "!", "audioconvert", "!",
        "audio/x-raw,channels=2", "!", "pipewiresink", "target-object=am-test-sink", "client-name=AmTestApp",
        "stream-properties=props,application.name=AmTestApp,media.name=Noise",
    ]))
    assert wait_for(lambda: node_id("am-test-iface") and node_id("AmTestApp")), "fake devices did not appear"
    yield
    for p in procs:
        p.terminate()
        p.wait(5)
    sid = node_id("am-test-sink")
    if sid:
        subprocess.run(["pw-cli", "destroy", str(sid)], capture_output=True)


@pytest.fixture
def engine(tmp_path, fakes):
    from audiomagic.config import Settings
    from audiomagic.engine import Engine
    from audiomagic.project import ProjectStore

    e = Engine(store=ProjectStore(str(tmp_path / "projects")), settings=Settings(str(tmp_path / "settings.json")))
    e.start()
    yield e
    e.shutdown()


def test_sources_are_listed(engine):
    s = engine.list_sources()
    assert s["ok"], s.get("error")
    dev = [d for d in s["devices"] if d["node"] == "am-test-iface"]
    assert dev and len(dev[0]["channels"]) == 2
    assert dev[0]["channel_labels"][0].startswith("Input 1")
    assert any(a["app"] == "AmTestApp" for a in s["apps"])
    assert any(o["node"] == "am-test-sink" for o in s["outputs"])


def test_record_play_edit_export(engine, tmp_path):
    ids = engine.add_tracks([
        {"source": {"kind": "device", "node": "am-test-iface", "channels": [0], "label": "Iface"}, "name": "Left"},
        {"source": {"kind": "device", "node": "am-test-iface", "channels": [1], "label": "Iface"}, "name": "Right"},
        {"source": {"kind": "app", "app": "AmTestApp", "label": "AmTestApp"}, "name": "App"},
        {"source": {"kind": "monitor", "node": "am-test-sink", "label": "Sink"}, "name": "SinkMon"},
        {"source": {"kind": "tone", "freq": 1000}, "name": "Tone"},
    ])
    assert len(ids) == 5
    assert len(engine.captures) == 4  # both interface channels share one capture

    def all_live():
        st = engine.state()
        return all(t["status"] == "live" for t in st["tracks"])
    assert wait_for(all_live, engine=engine), [(t["name"], t["status"], t["status_message"]) for t in engine.state()["tracks"]]

    engine.meters()
    time.sleep(0.3)
    m = engine.meters()
    for tid in ids:
        assert m["tracks"][tid][0] > -30, (tid, m["tracks"][tid])

    # monitor the tone into the private output; the output's own recording must then contain it
    engine.set_output("am-test-sink")
    engine.update_track(ids[4], {"monitor": True})
    time.sleep(0.5)

    take_id = engine.start_recording()
    t_end = time.time() + 2.5
    peaks_seen = False
    while time.time() < t_end:
        engine.poll()
        rp = engine.rec_peaks()
        peaks_seen = peaks_seen or bool(rp)
        time.sleep(0.05)
    assert engine.stop_recording() == take_id
    assert peaks_seen

    st = engine.state()
    take = next(t for t in st["takes"] if t["id"] == take_id)
    assert take["state"] == "done"
    assert 2.3 * SAMPLE_RATE < take["duration"] < 2.8 * SAMPLE_RATE

    from audiomagic.wavio import WavReader
    proj = engine.project
    readers = {tf["name"]: WavReader(proj.abspath(tf["file"])) for tf in take["tracks"]}
    assert {r.frames for r in readers.values()} == {take["duration"]}  # all tracks the same length
    data = {k: r.read(0, r.frames) for k, r in readers.items()}
    assert abs(dominant(data["Left"][SAMPLE_RATE // 2:]) - 440) < 5
    assert abs(dominant(data["Right"][SAMPLE_RATE // 2:]) - 880) < 5
    assert abs(dominant(data["Tone"][SAMPLE_RATE // 2:]) - 1000) < 5
    assert data["App"].shape[1] == 2 and data["Left"].shape[1] == 1

    # the app's stream and the output it plays into carry the same noise, captured
    # by two independent pipelines: they must line up to within a millisecond
    a = data["App"][SAMPLE_RATE // 2:SAMPLE_RATE // 2 + 24000, 0].astype(np.float64)
    b = data["SinkMon"][SAMPLE_RATE // 2:SAMPLE_RATE // 2 + 24000, 0].astype(np.float64)
    corr = np.fft.irfft(np.fft.rfft(a, 65536) * np.conj(np.fft.rfft(b, 65536)))
    lag = int(np.argmax(corr))
    lag = lag if lag < 32768 else lag - 65536
    print("app vs sink monitor lag (samples):", lag)
    assert abs(lag) <= 48 + 1024  # at most one PipeWire cycle of graph latency plus 1 ms

    # live monitoring: the monitored 1 kHz tone is audible on the output
    mon = data["SinkMon"][SAMPLE_RATE:, 0].astype(np.float64)
    spec = np.abs(np.fft.rfft(mon * np.hanning(mon.size)))
    freqs = np.fft.rfftfreq(mon.size, 1 / SAMPLE_RATE)
    tone_bin = spec[np.abs(freqs - 1000) < 3].max()
    assert tone_bin > 20 * np.median(spec), "monitored tone missing from the output"

    # waveform overview matches the file
    pk = np.frombuffer(engine.peaks(take_id, ids[0]), np.int8).reshape(-1, 2)
    assert abs(pk.shape[0] - take["duration"] / 256) <= 1 and pk[:, 1].max() > 80

    # playback moves forward and produces meters
    assert engine.play(take_id, 0.2)
    assert wait_for(lambda: (engine.meters()["pos"] or 0) > 0.8, timeout=5, engine=engine)
    m = engine.meters()
    assert m["master"] is not None and m["master"][0] > -40
    engine.pause()
    p1 = engine.meters()["pos"]
    time.sleep(0.3)
    assert abs(engine.meters()["pos"] - p1) < 0.05
    engine.stop_playback()

    # edits
    engine.edit_take(take_id, "cut", {"start": 0.5, "end": 1.0})
    tl = next(t for t in engine.state()["takes"] if t["id"] == take_id)["timeline"]
    assert tl["length"] == take["duration"] - SAMPLE_RATE // 2
    engine.edit_take(take_id, "normalize", {"track": ids[1]})
    gain = engine.project.take(take_id).edits.clip_gain_db[ids[1]]
    assert 10.0 < gain < 13.0  # 0.25 peak -> -1 dBFS is about +11 dB
    engine.edit_take(take_id, "fades", {"fade_in": 0.2, "fade_out": 0.3})
    engine.edit_take(take_id, "undo", {})
    assert engine.project.take(take_id).edits.fade_in == 0.0
    engine.edit_take(take_id, "redo", {})
    assert engine.project.take(take_id).edits.fade_in == 0.2

    # export every format
    out = tmp_path / "out"
    for fmt in ("flac", "mp3", "opus", "vorbis", "wav"):
        job = engine.start_export(take_id, {"format": fmt, "what": "both", "folder": str(out / fmt),
                                            "normalize": "podcast" if fmt == "mp3" else "peak",
                                            "tags": {"title": "Test", "artist": "Me"}})
        assert wait_for(lambda: engine.jobs[job]["state"] != "running", timeout=60), engine.jobs[job]
        info = engine.jobs[job]
        assert info["state"] == "done", info["message"]
        assert len(info["files"]) == 6  # mix + 5 stems
        for f in info["files"]:
            probe = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", f]))
            dur = float(probe["format"]["duration"])
            assert abs(dur - tl["length"] / SAMPLE_RATE) < 0.08, (f, dur)
            tags = {k.lower(): v for k, v in (probe["format"].get("tags") or probe["streams"][0].get("tags") or {}).items()}
            if fmt != "wav":
                assert tags.get("artist") == "Me", (f, tags)
        if fmt == "flac":
            assert json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", info["files"][0]]))["streams"][0][
                "bits_per_raw_sample"] == "24"


def test_device_unplug_and_return(engine):
    proc = subprocess.Popen([
        "gst-launch-1.0", "-q", "audiotestsrc", "is-live=true", "freq=300", "volume=0.3", "!", "audio/x-raw,channels=1,rate=48000", "!",
        "pipewiresink", "mode=provide", "stream-properties=props,media.class=Audio/Source,node.name=am-test-usb",
    ])
    try:
        assert wait_for(lambda: node_id("am-test-usb") is not None)
        engine.watcher.refresh()
        (tid,) = engine.add_tracks([{"source": {"kind": "device", "node": "am-test-usb", "channels": [0]}}])
        status = lambda: next(t for t in engine.state()["tracks"] if t["id"] == tid)["status"]
        assert wait_for(lambda: status() == "live", engine=engine)
        proc.terminate()
        proc.wait(5)
        engine.watcher.refresh()
        assert wait_for(lambda: status() == "offline", engine=engine)
        proc = subprocess.Popen([
            "gst-launch-1.0", "-q", "audiotestsrc", "is-live=true", "freq=300", "volume=0.3", "!", "audio/x-raw,channels=1,rate=48000",
            "!", "pipewiresink", "mode=provide", "stream-properties=props,media.class=Audio/Source,node.name=am-test-usb",
        ])
        assert wait_for(lambda: node_id("am-test-usb") is not None)
        engine.watcher.refresh()
        assert wait_for(lambda: status() == "live", engine=engine)
    finally:
        proc.terminate()
        proc.wait(5)
