"""Exporting a take to FLAC, Ogg (Opus/Vorbis), MP3 or WAV with ffmpeg."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import SAMPLE_RATE
from .render import TakeRenderer
from .util import db_to_lin, lin_to_db, safe_dirname

FORMATS = {
    "flac": {"label": "FLAC (lossless)", "ext": "flac", "default": "24",
             "qualities": [["24", "24-bit"], ["16", "16-bit (CD)"]]},
    "mp3": {"label": "MP3", "ext": "mp3", "default": "v0",
            "qualities": [["v0", "High (VBR ~245 kbps)"], ["320", "320 kbps"], ["192", "192 kbps"], ["128", "128 kbps"]]},
    "opus": {"label": "Ogg Opus", "ext": "opus", "default": "128",
             "qualities": [["192", "192 kbps"], ["128", "128 kbps"], ["96", "96 kbps"], ["64", "64 kbps (voice)"]]},
    "vorbis": {"label": "Ogg Vorbis", "ext": "ogg", "default": "6",
               "qualities": [["8", "High (~256 kbps)"], ["6", "Good (~192 kbps)"], ["4", "Smaller (~128 kbps)"]]},
    "wav": {"label": "WAV", "ext": "wav", "default": "24",
            "qualities": [["24", "24-bit"], ["16", "16-bit"]]},
}

NORMALIZE = {
    "off": {"label": "Off"},
    "peak": {"label": "Peak -1 dB", "peak": -1.0},
    "podcast": {"label": "Podcast loudness (-16 LUFS)", "lufs": -16.0, "tp": -1.5},
    "streaming": {"label": "Music streaming (-14 LUFS)", "lufs": -14.0, "tp": -1.0},
    "broadcast": {"label": "Broadcast (-23 LUFS)", "lufs": -23.0, "tp": -1.0},
}


class ExportError(Exception):
    pass


def ffmpeg_path():
    return shutil.which("ffmpeg")


def codec_args(fmt, quality):
    if fmt == "flac":
        return ["-c:a", "flac", "-sample_fmt", "s32" if quality == "24" else "s16"] + (
            ["-bits_per_raw_sample", "24"] if quality == "24" else [])
    if fmt == "mp3":
        if quality == "v0":
            return ["-c:a", "libmp3lame", "-q:a", "0"]
        return ["-c:a", "libmp3lame", "-b:a", f"{int(quality)}k"]
    if fmt == "opus":
        return ["-c:a", "libopus", "-b:a", f"{int(quality)}k", "-vbr", "on"]
    if fmt == "vorbis":
        return ["-c:a", "libvorbis", "-q:a", str(int(quality))]
    if fmt == "wav":
        return ["-c:a", "pcm_s24le" if quality == "24" else "pcm_s16le"]
    raise ExportError(f"unknown format {fmt}")


def unique_path(folder, name, ext, taken=()):
    base = safe_dirname(name)
    path = os.path.join(folder, f"{base}.{ext}")
    n = 2
    while os.path.exists(path) or path in taken:
        path = os.path.join(folder, f"{base} ({n}).{ext}")
        n += 1
    return path


def _raw_input(raw, channels):
    return ["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(channels), "-i", raw]


def loudness_cmd(raw, channels, lufs, tp):
    return [ffmpeg_path(), "-hide_banner", "-nostats", "-y"] + _raw_input(raw, channels) + [
        "-af", f"loudnorm=I={lufs}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"]


def parse_loudness(stderr):
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not m:
        raise ExportError("could not measure loudness")
    return json.loads(m.group(0))


def bytes_needed(frames, channels, fmt):
    """Rough disk space for an export: float temp files plus the finished files."""
    temp = frames * 4 * channels
    return int(temp * (1.8 if fmt in ("flac", "wav") else 1.1)) + 64 * 1024 * 1024


class ExportJob:
    def __init__(self, project, take, tracks, opts, on_progress=None):
        self.project = project
        self.take = take
        self.tracks = tracks
        self.opts = opts
        self.on_progress = on_progress or (lambda *a: None)
        self.cancelled = threading.Event()
        self._stop = threading.Event()  # cancelled, or another file failed
        self._lock = threading.Lock()
        self._procs = set()
        self.files = []

    def cancel(self):
        self.cancelled.set()
        self._halt()

    def _halt(self):
        self._stop.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                p.kill()

    def _ffmpeg(self, cmd):
        """Run ffmpeg at low priority (recording and the UI come first); returns its stderr."""
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        with self._lock:
            self._procs.add(p)
        try:
            try:
                os.setpriority(os.PRIO_PROCESS, p.pid, 10)
            except OSError:
                pass
            if self._stop.is_set():
                p.kill()
            _, err = p.communicate()
        finally:
            with self._lock:
                self._procs.discard(p)
        if self._stop.is_set():
            raise ExportError("cancelled")
        if p.returncode != 0:
            raise ExportError(f"ffmpeg failed: {err.strip()[-400:]}")
        return err

    def _progress(self, frac, msg):
        self.on_progress(max(0.0, min(1.0, frac)), msg)

    def run(self):
        if not ffmpeg_path():
            raise ExportError("ffmpeg is not installed (sudo apt install ffmpeg)")
        o = self.opts
        fmt = o.get("format", "flac")
        if fmt not in FORMATS:
            raise ExportError(f"unknown format {fmt}")
        quality = str(o.get("quality") or FORMATS[fmt]["default"])
        what = o.get("what", "mix")
        mix_ch = 1 if int(o.get("channels", 2)) == 1 else 2
        norm = NORMALIZE.get(o.get("normalize", "off"), NORMALIZE["off"])
        folder = os.path.expanduser(o.get("folder") or os.path.join(self.project.path, "exports"))
        os.makedirs(folder, exist_ok=True)
        wanted = o.get("track_ids")
        if wanted is None:
            wanted = [t.id for t in self.tracks]
        included = [t for t in self.tracks if t.id in set(wanted)]
        if not included:
            raise ExportError("no tracks to export (are they all muted?)")
        inc_ids = {t.id for t in included}
        want_mix = what in ("mix", "both")
        want_stems = what in ("stems", "both")
        tmp = tempfile.mkdtemp(prefix=".audiomagic-export-", dir=folder)
        try:
            renderer = TakeRenderer(self.project, self.take, tracks=self.tracks, stems=want_stems,
                                    mix_channels=mix_ch, include=lambda tr: tr.id in inc_ids,
                                    master=lambda: db_to_lin(self.project.master_gain_db))
            if renderer.length == 0:
                raise ExportError("this take is empty")
            names = {it.track.id: it.track.name for it in renderer.items}
            chans = {it.track.id: it.src.channels for it in renderer.items}
            n_ch = (mix_ch if want_mix else 0) + (sum(chans[t] for t in inc_ids if t in chans) if want_stems else 0)
            need = bytes_needed(renderer.length, n_ch, fmt)
            free = shutil.disk_usage(folder).free
            if free < need:
                raise ExportError(f"Not enough free space in {folder}: this export needs about "
                                  f"{need / 1e9:.1f} GB while working, {free / 1e9:.1f} GB is free")
            outputs = []
            mix_f = None
            if want_mix:
                mix_raw = os.path.join(tmp, "mix.f32")
                mix_f = open(mix_raw, "wb")
            stem_f = {}
            if want_stems:
                for tid in inc_ids:
                    if tid in names:
                        stem_f[tid] = open(os.path.join(tmp, f"{tid}.f32"), "wb")
            render_share = 0.5 if norm.get("lufs") else 0.7
            while not self.cancelled.is_set():
                r = renderer.next(65536)
                if r is None:
                    break
                mix, stems = r
                if mix_f:
                    mix_f.write(np.ascontiguousarray(mix, np.float32).tobytes())
                for tid, f in stem_f.items():
                    f.write(np.ascontiguousarray(stems[tid], np.float32).tobytes())
                self._progress(render_share * renderer.pos / renderer.length, "Mixing")
            if mix_f:
                mix_f.close()
            for f in stem_f.values():
                f.close()
            if self.cancelled.is_set():
                raise ExportError("cancelled")

            base = f"{self.project.name} - {self.take.name}"
            ext = FORMATS[fmt]["ext"]
            if want_mix:
                outputs.append((os.path.join(tmp, "mix.f32"), mix_ch, base, renderer.mix_peak, None))
            for it in renderer.items:
                tid = it.track.id
                if tid in stem_f:
                    outputs.append((os.path.join(tmp, f"{tid}.f32"), chans[tid], f"{base} - {names[tid]}", it.peak, names[tid]))

            taken = set()
            jobs = []
            for raw, ch, name, peak, stem_name in outputs:
                out_path = unique_path(folder, name, ext, taken)
                taken.add(out_path)
                jobs.append((raw, ch, out_path, peak, stem_name))
            done = []
            first_error = None
            self._progress(render_share, "Encoding" if len(jobs) == 1 else f"Encoding {len(jobs)} files")
            workers = max(1, min(len(jobs), (os.cpu_count() or 2) - 1))
            with ThreadPoolExecutor(workers, thread_name_prefix="export-encode") as pool:
                futures = [pool.submit(self._encode, *job, fmt, quality, norm) for job in jobs]
                for fut in as_completed(futures):
                    try:
                        done.append(fut.result())
                    except Exception as e:
                        if first_error is None:
                            first_error = e
                            self._halt()  # no point finishing the others
                        continue
                    self._progress(render_share + (1 - render_share) * len(done) / len(jobs),
                                   f"Encoded {len(done)} of {len(jobs)}")
            self.files = [j[2] for j in jobs if j[2] in done]
            if self.cancelled.is_set():
                raise ExportError("cancelled")
            if first_error is not None:
                raise first_error
            self._progress(1.0, "Done")
            return self.files
        except ExportError:
            for f in self.files:
                if self.cancelled.is_set():
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _encode(self, raw, ch, out_path, peak, stem_name, fmt, quality, norm):
        if self._stop.is_set():
            raise ExportError("cancelled")
        filters = []
        if norm.get("peak") is not None and peak > 0:
            filters.append(f"volume={norm['peak'] - lin_to_db(peak):.3f}dB")
        elif norm.get("lufs") is not None:
            m = parse_loudness(self._ffmpeg(loudness_cmd(raw, ch, norm["lufs"], norm["tp"])))
            if m.get("input_i") not in ("-inf", "inf") and float(m["input_i"]) > -70:
                def v(key, lo, hi):
                    try:
                        return f"{min(hi, max(lo, float(m[key]))):.2f}"
                    except (KeyError, ValueError):
                        return f"{lo:.2f}"
                filters.append(
                    "loudnorm=I={I}:TP={TP}:LRA=11:measured_I={mi}:measured_TP={mtp}:measured_LRA={mlra}:"
                    "measured_thresh={mth}:offset={off}:linear=true".format(
                        I=norm["lufs"], TP=norm["tp"], mi=v("input_i", -99, 0), mtp=v("input_tp", -99, 99),
                        mlra=v("input_lra", 0, 99), mth=v("input_thresh", -99, 0),
                        off=v("target_offset", -99, 99)))
        if fmt in ("flac", "wav") and quality == "16":
            filters.append(f"aresample={SAMPLE_RATE}:osf=s16:dither_method=triangular")
        else:
            filters.append(f"aresample={SAMPLE_RATE}")
        cmd = [ffmpeg_path(), "-hide_banner", "-nostats", "-loglevel", "error", "-y"] + _raw_input(raw, ch)
        cmd += ["-af", ",".join(filters)] + codec_args(fmt, quality)
        cmd += ["-ar", str(SAMPLE_RATE)]
        cmd += self._tag_args(stem_name) + [out_path]
        try:
            self._ffmpeg(cmd)
        except ExportError:
            try:
                os.remove(out_path)  # never leave a half-written file behind
            except OSError:
                pass
            raise
        return out_path

    def _tag_args(self, stem_name):
        tags = dict(self.opts.get("tags") or {})
        tags.setdefault("comment", "Recorded with AudioMagic")
        args = []
        for key in ("title", "artist", "album", "date", "genre", "comment"):
            val = str(tags.get(key) or "").strip()
            if key == "title" and stem_name and val:
                val = f"{val} ({stem_name})"
            if val:
                args += ["-metadata", f"{key}={val}"]
        return args


def describe():
    return {
        "formats": [{"id": k, **v} for k, v in FORMATS.items()],
        "normalize": [{"id": k, "label": v["label"]} for k, v in NORMALIZE.items()],
        "ffmpeg": bool(ffmpeg_path()),
    }

