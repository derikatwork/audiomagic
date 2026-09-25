"""Edge cases and failure modes: odd names, extreme edits, crashes, a full disk,
and requests the local server must refuse."""

import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from audiomagic import SAMPLE_RATE
from audiomagic.config import Settings
from audiomagic.edits import Edits, History, Timeline, op_cut, op_trim
from audiomagic.engine import Engine, UserError
from audiomagic.project import ProjectStore
from audiomagic.wavio import WavReader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait(cond, timeout=8.0, engine=None):
    end = time.time() + timeout
    while time.time() < end:
        if engine is not None:
            engine.poll()
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def engine(tmp_path):
    e = Engine(store=ProjectStore(str(tmp_path / "projects")), settings=Settings(str(tmp_path / "s.json")))
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
    return take


def live(engine):
    return wait(lambda: all(t["status"] == "live" for t in engine.state()["tracks"]), engine=engine)


# ------------------------------------------------------------------ edits fuzz

def expand(tl):
    return np.concatenate([np.arange(a, b) for a, b in tl.segments]) if tl.segments else np.zeros(0, int)


def test_random_cuts_and_trims_match_reference_model():
    rng = np.random.default_rng(42)
    D = 20000
    e = Edits()
    ref = np.arange(D)
    hist = History()
    snapshots = [(e.to_dict(), ref.copy())]
    for step in range(300):
        n = ref.size
        if n < 10:
            break
        a = int(rng.integers(0, n - 1))
        b = int(rng.integers(a + 1, min(n, a + max(2, n // 6)) + 1))
        hist.push(e)
        if rng.random() < 0.85:
            e = op_cut(e, D, a, b)
            ref = np.concatenate([ref[:a], ref[b:]])
        else:
            e = op_trim(e, D, a, b)
            ref = ref[a:b]
        tl = Timeline(e, D)
        assert tl.length == ref.size, step
        assert np.array_equal(expand(tl), ref), step
        snapshots.append((e.to_dict(), ref.copy()))
    # undo everything, step by step, back to the untouched take
    for d, r in reversed(snapshots[:-1]):
        e = hist.undo(e)
        assert e.to_dict() == d
        assert np.array_equal(expand(Timeline(e, D)), r)
    assert hist.undo(e) is None


# ------------------------------------------------------------------ names

def test_awkward_names_are_safe_on_disk(engine, tmp_path):
    for name in ["../../etc/passwd", "Épisode 12 ✨", "a/b\\c:d", "   ", "x" * 300, "."]:
        try:
            path = engine.create_project(name)
        except UserError:
            assert not name.strip()
            continue
        real = os.path.realpath(path)
        assert real.startswith(os.path.realpath(str(tmp_path / "projects")) + os.sep), real
        assert os.path.isfile(os.path.join(path, "project.json"))
    (tid,) = engine.add_tracks([{"source": {"kind": "tone", "freq": 300}, "name": "Guest/Host \"mic\" 🎙"}])
    assert live(engine)
    take = record(engine, 0.6)
    tk = engine.project.take(take)
    fname = tk.tracks[0]["file"]
    assert "/" not in os.path.basename(fname) and os.path.exists(engine.project.abspath(fname))
    out = tmp_path / "exp ört"
    job = engine.start_export(take, {"format": "flac", "what": "both", "folder": str(out)})
    assert wait(lambda: engine.jobs[job]["state"] != "running", 30)
    assert engine.jobs[job]["state"] == "done", engine.jobs[job]["message"]
    for f in engine.jobs[job]["files"]:
        assert os.path.dirname(f) == str(out) and os.path.exists(f)


# ------------------------------------------------------------------ recording rules

def test_recording_rules(engine):
    with pytest.raises(UserError):
        engine.start_recording()  # nothing to record
    (tid,) = engine.add_tracks([{"source": {"kind": "tone"}}])
    assert live(engine)
    take = engine.start_recording()
    with pytest.raises(UserError):
        engine.add_tracks([{"source": {"kind": "tone"}}])
    with pytest.raises(UserError):
        engine.remove_track(tid)
    with pytest.raises(UserError):
        engine.update_track(tid, {"armed": False})
    with pytest.raises(UserError):
        engine.play(take, 0)
    assert engine.start_recording() == take  # a second press doesn't start another take
    time.sleep(0.05)
    assert engine.stop_recording() is None  # too short to keep
    assert engine.project.takes == []
    assert not os.path.exists(engine.project.abspath("audio/take-001"))
    assert engine.stop_recording() is None  # stopping twice is harmless


def test_extreme_edits_and_exports(engine, tmp_path):
    ids = engine.add_tracks([
        {"source": {"kind": "tone", "freq": 500}, "name": "Mono"},
        {"source": {"kind": "tone", "freq": 700}, "name": "Silent"},
    ])
    engine.update_track(ids[1], {"gain_db": -60})
    assert live(engine)
    take = record(engine, 1.2)
    tk = engine.project.take(take)
    dur = tk.duration / SAMPLE_RATE

    # fades longer than the take, overlapping each other
    engine.edit_take(take, "fades", {"fade_in": 30, "fade_out": 30})
    job = engine.start_export(take, {"format": "wav", "what": "mix", "channels": 1, "folder": str(tmp_path / "a")})
    assert wait(lambda: engine.jobs[job]["state"] != "running", 30)
    assert engine.jobs[job]["state"] == "done"
    r = WavReader(engine.jobs[job]["files"][0])
    assert r.channels == 1 and abs(r.frames - tk.duration) <= 1
    x = r.read(0, r.frames)
    assert np.all(np.isfinite(x)) and np.max(np.abs(x)) < 0.5
    engine.edit_take(take, "reset", {})

    # keep a tiny selection, then cut everything that's left
    engine.edit_take(take, "trim", {"start": 0.5, "end": 0.5 + 10 / SAMPLE_RATE})
    assert engine.project.take(take).edits.trim_end - engine.project.take(take).edits.trim_start == 10
    engine.edit_take(take, "cut", {"start": 0, "end": dur})
    assert Timeline(engine.project.take(take).edits, tk.duration).length == 0
    assert engine.play(take, 0) is False
    job = engine.start_export(take, {"format": "mp3", "folder": str(tmp_path / "b")})
    assert wait(lambda: engine.jobs[job]["state"] != "running", 30)
    assert engine.jobs[job]["state"] == "error" and "empty" in engine.jobs[job]["message"]
    engine.edit_take(take, "reset", {})

    # everything muted: nothing to export, with a clear message
    engine.update_track(ids[0], {"mute": True})
    engine.update_track(ids[1], {"mute": True})
    job = engine.start_export(take, {"format": "flac", "folder": str(tmp_path / "c")})
    assert wait(lambda: engine.jobs[job]["state"] != "running", 30)
    assert engine.jobs[job]["state"] == "error" and "muted" in engine.jobs[job]["message"]

    # bad edit requests
    with pytest.raises(UserError):
        engine.edit_take(take, "cut", {"start": 1, "end": 1})
    with pytest.raises(UserError):
        engine.edit_take(take, "explode", {})
    with pytest.raises(UserError):
        engine.edit_take("k999", "cut", {"start": 0, "end": 1})


def test_export_files_run_in_parallel_and_cancel_cleanly(engine, tmp_path, monkeypatch):
    from audiomagic import export

    engine.add_tracks([{"source": {"kind": "tone", "freq": f}, "name": "Mic"} for f in (300, 500, 700)])
    assert live(engine)
    take = record(engine, 1.0)

    def run(opts, folder):
        job = engine.start_export(take, dict(opts, folder=str(tmp_path / folder)))
        t0 = time.time()
        assert wait(lambda: engine.jobs[job]["state"] != "running", 60)
        return engine.jobs[job], time.time() - t0

    # three tracks with the same name: every stem still gets its own file
    info, _ = run({"format": "flac", "what": "both", "normalize": "podcast"}, "ok")
    assert info["state"] == "done", info["message"]
    names = sorted(os.path.basename(f) for f in info["files"])
    assert len(names) == 4 and len(set(names)) == 4 and any("Mic (2)" in n for n in names)
    assert sorted(os.listdir(tmp_path / "ok")) == names  # and no temp files left behind

    # cancel while loudness is being measured: stops at once, leaves nothing
    monkeypatch.setattr(export, "loudness_cmd", lambda *a: ["sleep", "30"])
    job = engine.start_export(take, {"format": "mp3", "what": "both", "normalize": "podcast",
                                     "folder": str(tmp_path / "cancel")})
    assert wait(lambda: engine.jobs[job]["message"].startswith("Encoding"), 30)
    t0 = time.time()
    engine.cancel_export(job)
    assert wait(lambda: engine.jobs[job]["state"] != "running", 10)
    assert engine.jobs[job]["state"] == "cancelled" and time.time() - t0 < 3
    assert os.listdir(tmp_path / "cancel") == []

    # one file failing stops the rest and reports the real error
    monkeypatch.setattr(export, "loudness_cmd", lambda *a: ["sh", "-c", "echo broken >&2; exit 3"])
    info, _ = run({"format": "opus", "what": "both", "normalize": "podcast"}, "fail")
    assert info["state"] == "error" and "broken" in info["message"]
    assert os.listdir(tmp_path / "fail") == []

    # not enough disk space: refused up front with a clear message
    monkeypatch.setattr(export.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    info, _ = run({"format": "wav", "what": "both"}, "full")
    assert info["state"] == "error" and "Not enough free space" in info["message"]
    assert os.listdir(tmp_path / "full") == []


def test_normalize_silent_track_is_refused(engine):
    (tid,) = engine.add_tracks([{"source": {"kind": "tone"}}])
    assert live(engine)
    take = record(engine, 0.6)
    # silence the whole take on that track: nothing left to measure
    engine.edit_take(take, "silence", {"track": tid, "start": 0, "end": 10})
    with pytest.raises(UserError):
        engine.edit_take(take, "normalize", {"track": tid})


def test_mixer_changes_are_saved_even_when_switching_right_away(engine):
    (tid,) = engine.add_tracks([{"source": {"kind": "tone"}}])
    first = engine.project.path
    for g in range(-20, 1):
        engine.update_track(tid, {"gain_db": g / 2})
    engine.create_project("Other")  # switch before the delayed save would fire
    with open(os.path.join(first, "project.json")) as f:
        assert json.load(f)["tracks"][0]["gain_db"] == 0.0


# ------------------------------------------------------------------ whole-app runs

class App:
    """AudioMagic running as its own process, driven over its HTTP API."""

    def __init__(self, tmp, limit_file_size=None):
        env = dict(os.environ, PYTHONPATH=ROOT, XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_CACHE_HOME=str(tmp / "cache"),
                   AUDIOMAGIC_ROOT=str(tmp / "projects"))

        def pre():
            if limit_file_size:
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit_file_size, limit_file_size))

        self.p = subprocess.Popen([sys.executable, "-m", "audiomagic", "--no-window"], env=env, preexec_fn=pre,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.url = self.token = None
        end = time.time() + 20
        while self.url is None and time.time() < end:
            line = self.p.stdout.readline()
            m = re.search(r"(http://\S+/)\?token=(\S+)", line)
            if m:
                self.url, self.token = m.groups()
        assert self.url, "app did not start"

    def api(self, method, path, body=None, token=True, headers=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-AudioMagic-Token"] = self.token
        h.update(headers or {})
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path.lstrip("/"), method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def stop(self, sig=signal.SIGTERM):
        if self.p.poll() is None:
            self.p.send_signal(sig)
        try:
            out, _ = self.p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            out, _ = self.p.communicate()
        return out


def test_crash_during_recording_is_recovered(tmp_path):
    app = App(tmp_path)
    app.api("POST", "/api/tracks", {"items": [{"source": {"kind": "tone", "freq": 440}, "name": "Tone"}]})
    time.sleep(1.0)
    st, r = app.api("POST", "/api/record/start")
    assert st == 200
    time.sleep(2.5)
    app.stop(signal.SIGKILL)  # no chance to clean up
    pj = tmp_path / "projects" / "My First Project" / "project.json"
    assert json.load(open(pj))["takes"][0]["state"] == "recording"

    app = App(tmp_path)  # opening the project again repairs the take
    try:
        st, state = app.api("GET", "/api/state")
        take = state["takes"][0]
        assert take["state"] == "recovered"
        assert 1.2 * SAMPLE_RATE < take["duration"] <= 2.6 * SAMPLE_RATE  # all but the last unsynced second
        wav = tmp_path / "projects" / "My First Project" / take["tracks"][0]["file"]
        x = WavReader(str(wav)).read(0, take["duration"])
        assert np.max(np.abs(x[-4800:])) > 0.1  # real audio right up to the end
        st, _ = app.api("POST", "/api/play", {"take": take["id"], "pos": 0})
        assert st == 200
    finally:
        app.stop()


def test_disk_full_stops_recording_and_keeps_audio(tmp_path):
    limit = 400_000  # bytes per file: about 2.7 s of mono 24-bit audio
    app = App(tmp_path, limit_file_size=limit)
    try:
        app.api("POST", "/api/tracks", {"items": [{"source": {"kind": "tone", "freq": 440}, "name": "Tone"}]})
        time.sleep(1.0)
        st, r = app.api("POST", "/api/record/start")
        assert st == 200
        end = time.time() + 10
        state = None
        while time.time() < end:
            st, state = app.api("GET", "/api/state")
            if not state["transport"]["recording"]:
                break
            time.sleep(0.2)
        assert not state["transport"]["recording"], "recording did not stop when the disk filled up"
        take = state["takes"][0]
        assert take["state"] == "done" and take["duration"] > 2 * SAMPLE_RATE
        wav = tmp_path / "projects" / "My First Project" / take["tracks"][0]["file"]
        assert WavReader(str(wav)).frames > 2 * SAMPLE_RATE
    finally:
        out = app.stop()
    assert "could not write" in out


def test_server_refuses_bad_requests(tmp_path):
    app = App(tmp_path)
    try:
        assert app.api("GET", "/api/state", token=False)[0] == 401
        assert app.api("GET", "/api/state", headers={"X-AudioMagic-Token": app.token[:-1] + "x"}, token=False)[0] == 401
        assert app.api("GET", "/static/../server.py")[0] in (403, 404)
        assert app.api("GET", "/static/%2e%2e/server.py")[0] in (403, 404)
        assert app.api("POST", "/api/projects/open", {"path": "/etc"})[0] == 400
        assert app.api("POST", "/api/tracks", [1, 2, 3])[0] == 400
        assert app.api("POST", "/api/tracks", b"{not json")[0] == 400
        assert app.api("PATCH", "/api/tracks/t999", {"gain_db": 1})[0] == 400
        assert app.api("GET", "/api/takes/nope/peaks/nope")[0] == 400
        assert app.api("POST", "/api/master", {"gain_db": "loud"})[0] == 400
        assert app.api("POST", "/api/tracks", b"x" * (2 * 1024 * 1024))[0] == 413
        st, _ = app.api("POST", "/api/open-folder", {"path": "/definitely/not/here"})
        assert st == 400
        # another web page in the browser can't drive the app
        assert app.api("POST", "/api/record/start", headers={"Origin": "http://evil.example"})[0] == 403
        assert app.api("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
        # the server is still healthy afterwards
        assert app.api("GET", "/api/state")[0] == 200
    finally:
        app.stop()
