#!/usr/bin/env python3
"""End-to-end check of the installed Flatpak, from outside the sandbox.

Needs a running PipeWire (with pipewire-pulse), the Flatpak installed, and
python3-numpy on the host. Uses the repository's virtual test devices:

    python3 flatpak/smoke_test.py

Lists inputs, records a virtual interface, a program and a test tone,
monitors and plays back, then exports in every format, all inside the
sandbox. Exits non-zero if anything is wrong.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from audiomagic.wavio import WavReader  # noqa: E402

APP = "io.github.derikatwork.AudioMagic"
RATE = 48000
WORK = os.path.expanduser("~/audiomagic-smoke")  # inside the home folder, which the sandbox can see
failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)
    return ok


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    devices = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_devices.py")],
                               stdout=subprocess.PIPE, text=True)
    assert devices.stdout.readline().strip() == "ready"
    time.sleep(2)
    # AUDIOMAGIC_CMD runs something else instead (e.g. "python3 -m audiomagic", to test this script)
    cmd = shlex.split(os.environ.get("AUDIOMAGIC_CMD", f"flatpak run --env=AUDIOMAGIC_ROOT={WORK}/projects {APP}"))
    app = subprocess.Popen(cmd + ["--no-window"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           env=dict(os.environ, AUDIOMAGIC_ROOT=f"{WORK}/projects"))
    try:
        url = token = None
        t0 = time.time()
        while url is None:
            line = app.stdout.readline()
            if not line and app.poll() is not None:
                raise SystemExit("the app exited at start-up")
            print("app: " + line.rstrip())
            m = re.search(r"(http://\S+/)\?token=(\S+)", line)
            if m:
                url, token = m.groups()
        check(True, f"app started inside the sandbox in {time.time() - t0:.1f} s")
        run(url, token)
    finally:
        if "AUDIOMAGIC_CMD" in os.environ:
            app.terminate()
        else:
            check(stop_sandboxed_app(), "the app shut down cleanly when asked to")
        try:
            app.wait(30)
        except subprocess.TimeoutExpired:
            app.kill()
        devices.terminate()
        devices.wait(10)
    if failures:
        print(f"\n{len(failures)} problem(s):", *failures, sep="\n  ")
        sys.exit(1)
    print("\nall good")


def stop_sandboxed_app(timeout=30):
    """Ask the app inside the sandbox to quit (like logging out does); stopping
    `flatpak run` itself would leave it running."""
    def instances():
        out = subprocess.run(["flatpak", "ps", "--columns=application,child-pid"], capture_output=True, text=True).stdout
        return [int(f[1]) for f in (line.split() for line in out.splitlines()) if len(f) > 1 and f[0] == APP]
    for init in instances():  # the sandbox's init process; the app is its child
        subprocess.run(["pkill", "-TERM", "-P", str(init)])
    end = time.time() + timeout
    while time.time() < end:
        if not instances():
            return True
        time.sleep(0.5)
    subprocess.run(["flatpak", "kill", APP])
    return False


def run(url, token):
    def api(method, path, body=None):
        req = urllib.request.Request(url + "api" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-AudioMagic-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")

    def wait(cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    src = api("GET", "/sources")
    check(src.get("ok"), "PipeWire graph readable from the sandbox (pw-dump)")
    check(any(d["node"] == "am-load-iface" for d in src["devices"]), "the virtual 8-input interface is listed")
    check(any(a["app"] == "AmLoadApp" for a in src["apps"]), "the virtual program is listed")
    check(any(o["node"] == "am-load-mon" for o in src["outputs"]), "outputs are listed")

    ids = api("POST", "/tracks", {"items": [
        {"source": {"kind": "device", "node": "am-load-iface", "channels": [0], "device_channels": 8}, "name": "Mic"},
        {"source": {"kind": "app", "app": "AmLoadApp"}, "name": "Program"},
        {"source": {"kind": "tone", "freq": 1000}, "name": "Tone"},
        {"source": {"kind": "srt", "port": 9400, "passphrase": "smoke-test-secret"}, "name": "OBS"},
    ]})["ids"]
    # an OBS-style stream (H.264 + AAC in MPEG-TS over encrypted SRT) from outside the sandbox
    sender = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "sine=f=600:r=48000",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-f", "mpegts", "srt://127.0.0.1:9400?mode=caller&passphrase=smoke-test-secret&pbkeylen=16"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        record_and_export(api, wait, ids)
    finally:
        sender.terminate()
        sender.wait(10)


def record_and_export(api, wait, ids):
    api("PATCH", f"/tracks/{ids[0]}", {"preset": "voice"})
    api("POST", "/output", {"node": "am-load-mon"})
    api("PATCH", f"/tracks/{ids[0]}", {"monitor": True})  # monitoring goes through pulsesink
    check(wait(lambda: all(t["status"] == "live" for t in api("GET", "/state")["tracks"])),
          "all four inputs are live (interface, program, tone, SRT stream)")

    take = api("POST", "/record/start")["take"]
    time.sleep(5)
    api("POST", "/record/stop")
    state = api("GET", "/state")
    tk = next(t for t in state["takes"] if t["id"] == take)
    project = state["project"]["path"]
    lengths, data = set(), {}
    for f in tk["tracks"]:
        r = WavReader(os.path.join(project, f["file"]))
        lengths.add(r.frames)
        data[f["name"]] = r.read(0, r.frames)[:, 0]
    check(lengths == {tk["duration"]} and tk["duration"] > 4 * RATE,
          f"recorded {tk['duration'] / RATE:.1f} s, every file exactly the take length")
    mic = data["Mic"][RATE // 2:].astype(np.float64)
    spec = np.abs(np.fft.rfft(mic[:RATE] * np.hanning(RATE)))
    check(abs(np.fft.rfftfreq(RATE, 1 / RATE)[np.argmax(spec)] - 200) < 2, "interface input 1 recorded (200 Hz)")
    check(np.abs(data["Program"]).max() > 0.5, "the program's audio recorded")
    check(np.abs(data["Tone"]).max() > 0.1, "the test tone recorded")
    obs = data["OBS"][RATE:3 * RATE].astype(np.float64)
    spec = np.abs(np.fft.rfft(obs * np.hanning(obs.size)))
    check(obs.size and abs(np.fft.rfftfreq(obs.size, 1 / RATE)[np.argmax(spec)] - 600) < 3, "the SRT stream recorded (600 Hz)")

    check(api("POST", "/play", {"take": take, "pos": 0}) is not None, "playback started")
    time.sleep(1.5)
    tr = api("GET", "/state")["transport"]
    check(tr["playing"], "playback is running")
    api("POST", "/stop")

    for fmt in ("flac", "mp3", "opus", "vorbis", "wav"):
        folder = f"{WORK}/export-{fmt}"
        job = api("POST", "/export", {"take": take, "format": fmt, "what": "both", "normalize": "podcast", "folder": folder})["job"]
        wait(lambda: api("GET", f"/export/{job}")["state"] != "running", 120)
        info = api("GET", f"/export/{job}")
        ok = info["state"] == "done" and len(info["files"]) == 5
        for f in info.get("files", []):
            # decode it: container durations are only estimates (Ogg Opus in particular)
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", f, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                                 capture_output=True).stdout
            ok = ok and abs(len(pcm) // 2 - tk["duration"]) < RATE // 20
        check(ok, f"export {fmt}: mix + 4 tracks with loudness normalization ({info['message']})")


if __name__ == "__main__":
    main()
