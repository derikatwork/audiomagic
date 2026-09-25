import json
import os
import shutil
import subprocess

import numpy as np
import pytest

from audiomagic import SAMPLE_RATE
from audiomagic.dsp import merge_fx
from audiomagic.edits import op_cut, op_silence
from audiomagic.export import ExportJob
from audiomagic.project import ProjectStore, Take, Track
from audiomagic.render import TakeRenderer
from audiomagic.wavio import WavWriter

R = SAMPLE_RATE


def make_take(tmp_path, signal, fx_a=None, fx_b=None):
    store = ProjectStore(str(tmp_path))
    p = store.create("Render")
    os.makedirs(p.abspath("audio/take-001"))
    for name in ("a", "b"):
        w = WavWriter(p.abspath(f"audio/take-001/{name}.wav"), 1)
        w.write(signal)
        w.close()
    p.tracks = [
        Track({"id": "a", "name": "A", "source": {"kind": "tone"}, "fx": merge_fx(fx_a)}),
        Track({"id": "b", "name": "B", "source": {"kind": "tone"}, "fx": merge_fx(fx_b)}),
    ]
    take = Take({"id": "k1", "number": 1, "duration": signal.shape[0], "tracks": [
        {"track_id": "a", "file": "audio/take-001/a.wav", "channels": 1},
        {"track_id": "b", "file": "audio/take-001/b.wav", "channels": 1}]})
    p.takes = [take]
    return p, take


def render_all(p, take, **kw):
    r = TakeRenderer(p, take, stems=True, **kw)
    mixes, stems = [], {"a": [], "b": []}
    while True:
        out = r.next(3000)
        if out is None:
            break
        mixes.append(out[0])
        for k in stems:
            stems[k].append(out[1][k])
    return np.concatenate(mixes), {k: np.concatenate(v) for k, v in stems.items()}


def speech_and_noise(secs=4, seed=2):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(secs * R)).astype(np.float32) * 0.01
    t = np.arange(x.size) / R
    bursts = (np.sin(2 * np.pi * 2.5 * t) > 0.3).astype(np.float32)
    x += 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32) * bursts
    return x[:, None]


def test_noise_suppressed_track_stays_aligned(tmp_path):
    sig = speech_and_noise()
    p, take = make_take(tmp_path, sig, fx_a={"ns": {"on": True, "strength": 0.3}})
    _, stems = render_all(p, take)
    a, b = stems["a"][:, 0], stems["b"][:, 0]
    assert a.shape == b.shape == (sig.shape[0],)
    seg = slice(R, 3 * R)
    corr = np.correlate(a[seg], b[seg][1000:-1000], mode="valid")
    lag = int(np.argmax(corr)) - 1000
    assert lag == 0
    assert np.allclose(b, sig[:, 0], atol=1e-6)  # no effects -> untouched


def test_start_mid_take_matches_full_render(tmp_path):
    sig = speech_and_noise()
    p, take = make_take(tmp_path, sig)
    full, _ = render_all(p, take)
    r = TakeRenderer(p, take, start=R)
    part = np.concatenate([m for m, _ in iter(lambda: r.next(4096), None)])
    assert np.allclose(part, full[R:], atol=1e-6)


def test_cuts_silence_and_fades(tmp_path):
    sig = np.full((4 * R, 1), 0.5, np.float32)
    p, take = make_take(tmp_path, sig)
    take.edits = op_cut(take.edits, take.duration, R, 2 * R)            # 3 s left
    take.edits = op_silence(take.edits, take.duration, "a", 0, R // 2)  # track a quiet at start
    take.edits.fade_out = 0.5
    mix, stems = render_all(p, take)
    assert mix.shape[0] == 3 * R
    a = stems["a"][:, 0]
    assert np.all(a[:R // 2 - 300] == 0) and abs(a[R // 2 + 400] - 0.5) < 1e-3
    b = stems["b"][:, 0]
    assert abs(b[R - 1000] - 0.5) < 1e-3                  # before the join
    assert b[R] < 0.1                                      # the join is ramped (no click)
    assert abs(b[R + 1000] - 0.5) < 1e-3
    assert b[-1] < 0.01 and abs(b[int(2.4 * R)] - 0.5) < 1e-3  # fade-out at the very end
    # stereo mix of two centred mono tracks
    assert abs(mix[R + 1000, 0] - 1.0) < 1e-3 and abs(mix[R + 1000, 1] - 1.0) < 1e-3


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_export_loudness_normalization(tmp_path):
    sig = speech_and_noise(secs=6)
    p, take = make_take(tmp_path, sig)
    job = ExportJob(p, take, p.tracks, {"format": "flac", "what": "mix", "normalize": "podcast",
                                        "folder": str(tmp_path / "out"), "tags": {"title": "T"}})
    (path,) = job.run()
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    summary = r.stderr[r.stderr.rfind("Summary:"):]
    lufs = float(summary.split("I:")[1].split("LUFS")[0])
    assert abs(lufs - (-16.0)) < 1.0, summary
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path]))
    assert probe["streams"][0]["sample_rate"] == "48000"


def test_export_cancel_leaves_nothing(tmp_path):
    sig = speech_and_noise(secs=3)
    p, take = make_take(tmp_path, sig)
    job = ExportJob(p, take, p.tracks, {"format": "mp3", "what": "both", "folder": str(tmp_path / "out")})
    job.cancel()
    with pytest.raises(Exception):
        job.run()
    assert os.listdir(tmp_path / "out") == []
