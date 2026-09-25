"""Network inputs: SRT (as sent by OBS/ffmpeg) and internet streams (HTTP)."""

import functools
import http.server
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from audiomagic import SAMPLE_RATE

from .test_pipewire import dominant, pytestmark, wait_for  # noqa: F401  (same PipeWire requirement)


@pytest.fixture
def engine(tmp_path):
    from audiomagic.config import Settings
    from audiomagic.engine import Engine
    from audiomagic.project import ProjectStore

    e = Engine(store=ProjectStore(str(tmp_path / "projects")), settings=Settings(str(tmp_path / "settings.json")))
    e.start()
    yield e
    e.shutdown()


def record(engine, secs):
    take = engine.start_recording()
    end = time.time() + secs
    while time.time() < end:
        engine.poll()
        time.sleep(0.05)
    engine.stop_recording()
    return engine.project.take(take)


def read_track(engine, take, tid):
    from audiomagic.wavio import WavReader
    tf = take.file_for(tid)
    r = WavReader(engine.project.abspath(tf["file"]))
    return r.read(0, r.frames)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("passphrase", ["", "correct-horse-battery"])
def test_srt_input_from_obs_style_sender(engine, passphrase):
    port = 9300 + len(passphrase)
    (tid,) = engine.add_tracks([{"source": {"kind": "srt", "port": port, "passphrase": passphrase}, "name": "OBS"}])
    status = lambda: next(t for t in engine.state()["tracks"] if t["id"] == tid)
    assert wait_for(lambda: status()["status"] == "starting", engine=engine)
    assert "waiting" in status()["status_message"]
    url = f"srt://127.0.0.1:{port}?mode=caller"
    if passphrase:
        url += f"&passphrase={passphrase}&pbkeylen=16"
    sender = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "sine=f=600:r=48000",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-f", "mpegts", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert wait_for(lambda: status()["status"] == "live", timeout=15, engine=engine), status()
        take = record(engine, 2.0)
        x = read_track(engine, take, tid)
        assert x.shape[1] == 2
        loud = x[np.abs(x[:, 0]) > 0.01]
        assert loud.shape[0] > SAMPLE_RATE  # most of the take has the tone
        assert abs(dominant(x[-SAMPLE_RATE:]) - 600) < 5
    finally:
        sender.terminate()
        sender.wait(5)


def test_srt_wrong_passphrase_is_rejected(engine):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    (tid,) = engine.add_tracks([{"source": {"kind": "srt", "port": 9350, "passphrase": "the-right-secret"}}])
    sender = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-re", "-f", "lavfi", "-i", "sine=f=600:r=48000",
         "-c:a", "aac", "-f", "mpegts", "srt://127.0.0.1:9350?mode=caller&passphrase=the-wrong-secret&pbkeylen=16"])
    try:
        time.sleep(3)
        for _ in range(20):
            engine.poll()
            time.sleep(0.05)
        st = next(t for t in engine.state()["tracks"] if t["id"] == tid)
        assert st["status"] != "live"
    finally:
        sender.kill()
        sender.wait(5)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_http_stream_input(engine, tmp_path):
    media = tmp_path / "www"
    media.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=500:r=44100:d=20",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(media / "radio.mp3")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(media))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/radio.mp3"
        (tid,) = engine.add_tracks([{"source": {"kind": "url", "url": url}, "name": "Radio"}])
        status = lambda: next(t for t in engine.state()["tracks"] if t["id"] == tid)["status"]
        assert wait_for(lambda: status() == "live", timeout=10, engine=engine)
        take = record(engine, 2.0)
        x = read_track(engine, take, tid)
        assert abs(dominant(x[SAMPLE_RATE // 2:]) - 500) < 5
        # paced in real time, not decoded as fast as possible
        assert abs(x.shape[0] - take.duration) == 0 and np.abs(x[-SAMPLE_RATE // 4:]).max() > 0.1
    finally:
        srv.shutdown()


def test_bad_inputs_are_rejected(engine):
    from audiomagic.engine import UserError
    for src in ({"kind": "url", "url": "javascript:alert(1)"},
                {"kind": "srt", "port": 80},
                {"kind": "srt", "port": 9000, "passphrase": "short"},
                {"kind": "nope"}):
        with pytest.raises(UserError):
            engine.add_tracks([{"source": src}])
