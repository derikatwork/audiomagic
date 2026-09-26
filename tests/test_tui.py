"""The terminal interface (audiomagic --tui), driven key by key through Textual's
test pilot against the real engine, plus one run in a real pseudo-terminal."""

import asyncio
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

pytest.importorskip("textual")

from audiomagic import SAMPLE_RATE  # noqa: E402
from audiomagic.config import Settings  # noqa: E402
from audiomagic.engine import Engine  # noqa: E402
from audiomagic.peaks import BIN  # noqa: E402
from audiomagic.project import ProjectStore  # noqa: E402
from audiomagic.tui import (AudioMagicTUI, ConfirmScreen, ExportScreen, Timeline, TrackList, amplitude,  # noqa: E402
                            envelope, fmt_time)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HAVE_PIPEWIRE = shutil.which("pw-cli") is not None and \
    subprocess.run(["pw-cli", "info", "0"], capture_output=True).returncode == 0
needs_pipewire = pytest.mark.skipif(not HAVE_PIPEWIRE, reason="needs a running PipeWire")


@pytest.fixture(autouse=True)
def quick_pilot(monkeypatch):
    """After each key the pilot waits for the process to go idle, which never
    happens with an audio engine running in the same process."""
    import textual.app
    import textual.pilot

    async def quick(*_args, **_kw):
        await asyncio.sleep(0.005)
    monkeypatch.setattr(textual.pilot, "wait_for_idle", quick)
    monkeypatch.setattr(textual.app, "wait_for_idle", quick)


@pytest.fixture
def engine(tmp_path):
    e = Engine(store=ProjectStore(str(tmp_path / "projects")), settings=Settings(str(tmp_path / "s.json")))
    e.start()
    yield e
    e.shutdown()


def tone(freq=440):
    return {"name": f"Tone {freq}", "source": {"kind": "tone", "freq": freq}}


def run(engine, body, size=(120, 36)):
    async def main():
        app = AudioMagicTUI(engine)
        async with app.run_test(size=size) as pilot:
            await until(pilot, lambda: app.state is not None)
            await body(app, pilot)
        return app
    return asyncio.run(main())


async def until(pilot, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting")


async def record(app, pilot, secs=1.5):
    await pilot.press("r")
    await until(pilot, lambda: app.state["transport"]["recording"])
    await pilot.pause(secs)
    await pilot.press("r")
    await until(pilot, lambda: not app.state["transport"]["recording"] and app.state["takes"]
                and app.state["takes"][-1]["state"] == "done")
    return app.state["takes"][-1]


def line_text(widget, y):
    return "".join(seg.text for seg in widget.render_line(y))


# ------------------------------------------------------------------ helpers

def test_time_formats():
    assert fmt_time(0) == "0:00"
    assert fmt_time(61.26, 1) == "1:01.3"
    assert fmt_time(3725) == "1:02:05"
    assert fmt_time(-3) == "0:00"


def test_amplitude_takes_the_larger_of_min_and_max():
    data = np.array([[-10, 5], [-2, 90], [-127, 127]], np.int8).tobytes()
    assert amplitude(data).tolist() == [10, 90, 127]


def test_waveform_columns_follow_the_edits():
    amp = np.concatenate([np.full(100, 10), np.full(100, 100)]).astype(np.uint8)
    # a cut removed bins 50..150: what's left plays bins 0..50, then 150..200
    segments = [[0, 50 * BIN, 0], [150 * BIN, 200 * BIN, 50 * BIN]]
    levels, src_mid = envelope(amp, segments, 0.0, 10 * BIN / SAMPLE_RATE, 12)
    assert levels[:5].tolist() == [10] * 5
    assert levels[5:10].tolist() == [100] * 5
    assert (src_mid[:10] >= 0).all() and (src_mid[10:] < 0).all()  # past the end


# ----------------------------------------------------------- inputs and mixer

def test_add_an_input_and_use_the_mixer(engine):
    async def body(app, pilot):
        await pilot.press("i")
        await pilot.pause(0.3)
        await pilot.press("6")  # the test tone tab
        field = app.screen.query_one("#tone-freq")
        field.value = ""
        await pilot.press("3", "3", "0", "enter")
        await until(pilot, lambda: len(app.tracks) == 1)
        assert app.tracks[0]["source"] == {"kind": "tone", "freq": 330.0, "label": "Test tone 330 Hz"}
        await until(pilot, lambda: not app.screen.is_modal)

        await pilot.press("a", "m", "s", "h", "plus", "plus", "right", "right", "right")
        tr = engine.project.tracks[0]
        await until(pilot, lambda: (not tr.armed and tr.mute and tr.solo and tr.monitor
                                    and tr.gain_db == 2.0 and abs(tr.pan - 0.3) < 1e-9))
        await pilot.press("0", "h", "down", "minus")  # the last row is the master
        await until(pilot, lambda: tr.gain_db == 0.0 and not tr.monitor and engine.project.master_gain_db == -1.0)

        await pilot.press("up", "n")
        await pilot.pause(0.2)
        app.screen.query_one("#value").value = "Host mic"
        await pilot.press("enter")
        await until(pilot, lambda: tr.name == "Host mic")
        await until(pilot, lambda: "Host mic" in line_text(app.query_one(TrackList), 0))
    run(engine, body)


@needs_pipewire
def test_add_devices_and_programs(engine):
    """The Add input lists, against a virtual 8-input interface and a program playing sound."""
    devices = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_devices.py")],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert devices.stdout.readline().strip() == "ready"

        async def body(app, pilot):
            await pilot.press("i")
            await pilot.pause(0.2)
            dialog = app.screen
            await until(pilot, lambda: dialog.sources is not None and any(
                d["label"] == "LoadTestInterface" for d in dialog.sources["devices"]))
            await until(pilot, lambda: any(len(v) == 8 for v in dialog.choices.values()))
            dev_list = dialog.query_one("#dev-list")
            ids = [dev_list.get_option_at_index(i).id for i in range(dev_list.option_count)]
            each = next(k for k, v in dialog.choices.items() if len(v) == 8)  # "Each input as its own track"
            dev_list.highlighted = ids.index(each)
            await pilot.press("enter")
            await until(pilot, lambda: len(app.tracks) == 8)
            assert [t["source"]["channels"] for t in app.tracks] == [[k] for k in range(8)]

            await pilot.press("i")
            await pilot.pause(0.2)
            dialog = app.screen
            await pilot.press("2")  # Programs
            await until(pilot, lambda: dialog.sources is not None and any(
                a["label"] == "AmLoadApp" or a["app"] == "AmLoadApp" for a in dialog.sources["apps"]))
            app_list = dialog.query_one("#app-list")
            assert app_list.has_focus
            await until(pilot, lambda: app_list.option_count >= 1 and app_list.get_option_at_index(0).id)
            index = next(i for i in range(app_list.option_count)
                         if "AmLoadApp" in str(app_list.get_option_at_index(i).prompt))
            app_list.highlighted = index
            await pilot.press("enter")
            await until(pilot, lambda: len(app.tracks) == 9)
            assert app.tracks[-1]["source"]["kind"] == "app"
            await until(pilot, lambda: all(t["status"] == "live" for t in app.tracks), 20)
        run(engine, body)
    finally:
        devices.terminate()
        devices.wait(timeout=10)


def test_effects(engine):
    engine.add_tracks([tone()])

    async def body(app, pilot):
        await pilot.press("e")
        await pilot.pause(0.3)
        await pilot.press("space", "down", "right", "right")  # noise suppression on, stronger
        fx = lambda: engine.project.tracks[0].fx  # noqa: E731
        await until(pilot, lambda: fx()["ns"]["on"] and abs(fx()["ns"]["strength"] - 0.6) < 1e-9)
        await pilot.press("v")  # the voice preset
        await until(pilot, lambda: fx()["gate"]["on"] and fx()["eq"]["on"])
        await pilot.pause(0.3)
        knobs = {k.path: k.value for k in app.screen.query("Knob")}
        assert knobs["gate.on"] is True and knobs["eq.mid"] == 2.0
        await pilot.press("o")  # all off
        await until(pilot, lambda: not fx()["ns"]["on"] and not fx()["gate"]["on"])
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.is_modal)
    run(engine, body)


def test_removing_an_input_with_audio_asks_first(engine):
    engine.add_tracks([tone(), tone(880)])

    async def body(app, pilot):
        await record(app, pilot, 0.8)
        await pilot.press("delete")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "has audio in 1 take" in str(app.screen.message)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert len(engine.project.tracks) == 2
        await pilot.press("delete")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.click("#yes")
        await until(pilot, lambda: len(engine.project.tracks) == 1)
    run(engine, body)


# ------------------------------------------------ recording, editing, export

def test_record_edit_and_export(engine, tmp_path):
    engine.add_tracks([tone(440), tone(880)])

    async def body(app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: app.state["transport"]["recording"])
        tl = app.query_one(Timeline)
        await until(pilot, lambda: len(tl.live) == 2 and all(b[1] > 0 for b in tl.live.values()))
        await pilot.pause(1.5)
        await pilot.press("r")
        await until(pilot, lambda: app.state["takes"] and app.state["takes"][-1]["state"] == "done")
        take = engine.project.takes[-1]
        length = take.duration / SAMPLE_RATE
        assert length > 1.5

        await pilot.press("tab")
        assert tl.has_focus
        await until(pilot, lambda: all(k in tl.peaks for k in [(take.id, "t1"), (take.id, "t2")]))
        rows = [line_text(tl, y) for y in range(tl.size.height)]
        assert "0:00" in rows[1]
        assert any("█" in r for r in rows[2:]), rows

        # select from 0.5 s for 10 columns and cut it
        spc = tl.spc(tl.take())
        assert tl.zoom is None and abs(spc - length / tl._wave_width()) < 1e-6
        tl.cursor = 0.5
        await pilot.press(*["shift+right"] * 10)
        a, b = tl.selection()
        assert a == 0.5 and abs(b - (0.5 + 10 * spc)) < 1e-9
        await pilot.press("c")
        await until(pilot, lambda: take.edits.cuts)
        cut = take.edits.cuts[0]
        assert abs((cut[1] - cut[0]) / SAMPLE_RATE - 10 * spc) < 0.01
        await pilot.press("u")
        await until(pilot, lambda: not take.edits.cuts)
        await pilot.press("U")
        await until(pilot, lambda: take.edits.cuts)

        # silence part of the second track, then normalize it
        tl.cursor = 0.2
        await pilot.press(*["shift+right"] * 5)
        await pilot.press("down", "z")
        await until(pilot, lambda: take.edits.silences.get("t2"))
        await pilot.press("N")
        await until(pilot, lambda: "t2" in take.edits.clip_gain_db)

        await pilot.press("f")
        await pilot.pause(0.2)
        app.screen.query_one("#in").value = "0.25"
        app.screen.query_one("#out").value = "0.5"
        await pilot.click("#yes")
        await until(pilot, lambda: take.edits.fade_in == 0.25 and take.edits.fade_out == 0.5)

        # keep just the middle
        spc = tl.spc(tl.take())
        await pilot.press("home", *["right"] * 5, *["shift+right"] * 20, "k")
        await until(pilot, lambda: take.edits.trim_end is not None)
        kept = lambda: app.state["takes"][-1]["timeline"]["length"] / SAMPLE_RATE  # noqa: E731
        await until(pilot, lambda: abs(kept() - 20 * spc) < 0.02)
        assert tl.cursor == 0.0 and tl.selection() is None

        await pilot.press("x")
        await until(pilot, lambda: isinstance(app.screen, ExportScreen))
        app.screen.query_one("#format").value = "mp3"
        app.screen.query_one("#what").value = "both"
        app.screen.query_one("#folder").value = str(tmp_path / "out")
        await pilot.pause(0.1)
        assert app.screen.query_one("#quality").value == "v0"
        await pilot.click("#go")
        await until(pilot, lambda: any(j["state"] != "running" for j in app.export_jobs.values()), 60)
        job = list(app.export_jobs.values())[-1]
        assert job["state"] == "done", job
        assert len(job["files"]) == 3 and all(f.endswith(".mp3") for f in job["files"])
        await pilot.pause(0.2)
        assert "Exported 3 file(s)" in str(app.screen.query_one("#result").content)
        for f in job["files"]:
            assert os.path.getsize(f) > 1000
    run(engine, body)


@needs_pipewire
def test_playback_from_the_cursor(engine):
    engine.add_tracks([tone()])

    async def body(app, pilot):
        take = await record(app, pilot, 2.0)
        tl = app.query_one(Timeline)
        tl.cursor = 1.0
        await pilot.press("space")
        await until(pilot, lambda: app.state["transport"]["playing"])
        assert app.state["transport"]["play_take"] == take["id"]
        await until(pilot, lambda: tl.play_pos is not None and tl.play_pos >= 1.0)
        await pilot.press("p")
        await until(pilot, lambda: app.state["transport"]["paused"])
        await pilot.press("p")
        await until(pilot, lambda: app.state["transport"]["playing"])
        await pilot.press("space")
        await until(pilot, lambda: not app.state["transport"]["playing"] and not app.state["transport"]["paused"])

        await pilot.press("d")
        await pilot.pause(0.3)
        await pilot.press("enter")  # the system default
        await until(pilot, lambda: not app.screen.is_modal)
        assert engine.settings.get("output") is None
    run(engine, body)


def test_takes_rename_switch_and_delete(engine):
    engine.add_tracks([tone()])

    async def body(app, pilot):
        first = await record(app, pilot, 0.6)
        second = await record(app, pilot, 0.6)
        tl = app.query_one(Timeline)
        assert tl.take_id == second["id"]
        await pilot.press("left_square_bracket")
        assert tl.take_id == first["id"]
        await pilot.press("tab", "n")
        await pilot.pause(0.2)
        app.screen.query_one("#value").value = "Intro"
        await pilot.press("enter")
        await until(pilot, lambda: engine.project.takes[0].name == "Intro")
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.click("#yes")
        await until(pilot, lambda: len(engine.project.takes) == 1)
        await until(pilot, lambda: tl.take_id == second["id"])
    run(engine, body)


def test_projects(engine):
    engine.add_tracks([tone()])
    first = engine.project.path

    async def body(app, pilot):
        await pilot.press("o")
        await pilot.pause(0.3)
        app.screen.query_one("#new").value = "Episode 2"
        app.screen.query_one("#new").focus()
        await pilot.press("enter")
        await until(pilot, lambda: engine.project.name == "Episode 2")
        await until(pilot, lambda: app.state["project"]["name"] == "Episode 2")
        assert len(app.tracks) == 1  # copied the inputs
        await pilot.press("o")
        await until(pilot, lambda: app.screen.query_one("#projects").option_count == 2)
        options = app.screen.query_one("#projects")
        index = next(i for i in range(options.option_count) if options.get_option_at_index(i).id == first)
        options.highlighted = index
        await pilot.press("enter")
        await until(pilot, lambda: engine.project.path == first)
    run(engine, body)


def test_quitting_while_recording_asks(engine):
    engine.add_tracks([tone()])

    async def body(app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: app.state["transport"]["recording"])
        await pilot.press("q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert app.screen.focused.id == "no"  # "Keep recording" is the safe default
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.is_running and engine.recorder is not None and not app.screen.is_modal
        await pilot.press("q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("i", "r")  # the main keys do nothing while a dialog is open
        assert isinstance(app.screen, ConfirmScreen) and engine.recorder is not None
        await pilot.click("#yes")
        await pilot.pause(0.3)
        assert not app.is_running
    run(engine, body)
    engine.stop_recording()  # what the launcher does next (in engine.shutdown)
    assert engine.project.takes[-1].state == "done" and engine.project.takes[-1].duration > 0


def test_ctrl_c_quits(engine):
    async def body(app, pilot):
        await pilot.press("ctrl+c")
        await pilot.pause(0.3)
        assert not app.is_running
    run(engine, body)


# ------------------------------------------------------------ real terminal

def pty_session(argv, env, keys, timeout=40):
    """Runs argv on a pseudo-terminal, sending (delay, key) pairs; returns (exit code, output)."""
    import fcntl
    import struct
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
    proc = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    out = bytearray()
    start = time.time()
    keys = list(keys)
    due = start + keys[0][0]
    try:
        while time.time() - start < timeout:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    pass
            if keys and time.time() >= due:
                _, key = keys.pop(0)
                if key == "SIGTERM":
                    proc.send_signal(signal.SIGTERM)
                else:
                    os.write(master, key.encode())
                if keys:
                    due = time.time() + keys[0][0]
            if proc.poll() is not None:
                return proc.returncode, bytes(out)
        proc.kill()
        proc.wait()
        raise AssertionError("the terminal interface did not exit:\n" + out[-2000:].decode("utf-8", "replace"))
    finally:
        os.close(master)


@needs_pipewire
def test_in_a_real_terminal(tmp_path):
    """Starts `audiomagic --tui` in a pseudo-terminal: records, gets SIGTERM
    (logging out), and must save the take and hand the terminal back."""
    store = ProjectStore(str(tmp_path / "projects"))
    p = store.create("Terminal")
    from audiomagic.project import Track
    p.tracks = [Track({"id": "t1", "name": "Tone", "source": {"kind": "tone", "freq": 440}})]
    p.save()
    env = {**os.environ, "AUDIOMAGIC_ROOT": str(tmp_path / "projects"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "TERM": "xterm-256color",
           "PYTHONPATH": ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")}
    code, out = pty_session([sys.executable, "-m", "audiomagic", "--tui"], env,
                            [(5, "r"), (3, "SIGTERM")])
    assert code == 0, out[-2000:]
    assert b"Inputs" in out and b"Timeline" in out
    assert b"\x1b[?1049l" in out  # left the alternate screen
    assert b"Stopping the recording and saving it" in out
    with open(os.path.join(p.path, "project.json")) as f:
        takes = json.load(f)["takes"]
    assert len(takes) == 1 and takes[0]["state"] == "done" and takes[0]["duration"] > SAMPLE_RATE
    log = open(tmp_path / "cache" / "audiomagic" / "tui.log").read()
    assert "starting in the terminal" in log


# Runs in its own (single-threaded) process: pty.fork() makes the pseudo-terminal
# the app's controlling terminal, so closing it sends SIGHUP like a closed window.
CLOSE_TERMINAL = """
import os, pty, select, sys, time
argv, env = sys.argv[1:], dict(os.environ)
pid, fd = pty.fork()
if pid == 0:
    os.execvpe(argv[0], argv, env)
def pump(secs):
    end = time.time() + secs
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                return
pump(5)
os.write(fd, b"r")
pump(3)
os.close(fd)
end = time.time() + 30
while time.time() < end:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        print(os.waitstatus_to_exitcode(status))
        sys.exit(0)
    time.sleep(0.1)
os.kill(pid, 9)
print("hung")
"""


@needs_pipewire
def test_closing_the_terminal_saves_the_recording(tmp_path):
    store = ProjectStore(str(tmp_path / "projects"))
    p = store.create("Closed")
    from audiomagic.project import Track
    p.tracks = [Track({"id": "t1", "name": "Tone", "source": {"kind": "tone", "freq": 440}})]
    p.save()
    env = {**os.environ, "AUDIOMAGIC_ROOT": str(tmp_path / "projects"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "TERM": "xterm-256color",
           "PYTHONPATH": ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")}
    r = subprocess.run([sys.executable, "-c", CLOSE_TERMINAL, sys.executable, "-m", "audiomagic", "--tui"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "0", (r.stdout, r.stderr)
    with open(os.path.join(p.path, "project.json")) as f:
        takes = json.load(f)["takes"]
    assert len(takes) == 1 and takes[0]["state"] == "done" and takes[0]["duration"] > 2 * SAMPLE_RATE
