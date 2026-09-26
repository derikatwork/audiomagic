"""The engine: owns the project, the inputs, recording, playback and export.

Threads involved:
* the server's executor thread calls the public methods (one at a time);
* GStreamer streaming threads deliver input blocks to TrackRuntime.on_block;
* the PipeWire watcher thread reports graph changes;
* playback and export run in their own threads.

Input-block handling never takes ``self.lock``, so stopping a pipeline while
holding the lock cannot deadlock.
"""

import copy
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlparse

import numpy as np

from . import SAMPLE_RATE, __version__
from . import peaks as peaks_mod
from . import pw
from .capture import PipeWireCapture, SrtCapture, ToneCapture, UrlCapture
from .config import Settings
from .dsp import DEFAULT_FX, FX_PRESETS, Meter, TrackDSP, merge_fx, to_stereo
from .edits import Edits, History, Timeline, op_cut, op_silence, op_trim, op_unsilence
from .export import ExportError, ExportJob
from .output import OutputStream
from .playback import Player
from .project import ProjectStore, Take, Track
from .recorder import Recorder
from .util import clamp, db_to_lin, lin_to_db, log, slugify
from .wavio import WavReader


class UserError(Exception):
    """Something the user can fix; the message is shown in the UI."""


class NeedsConfirm(Exception):
    """The action is destructive; the UI should ask and retry with confirm=true."""


def trash(path):
    """Move a file or folder to the desktop trash (restorable)."""
    if not os.path.exists(path):
        return
    if shutil.which("gio"):
        r = subprocess.run(["gio", "trash", path], stdin=subprocess.DEVNULL, capture_output=True)
        if r.returncode == 0:
            return
    fallback = os.path.join(os.path.dirname(path), ".trash")
    os.makedirs(fallback, exist_ok=True)
    shutil.move(path, os.path.join(fallback, f"{int(time.time())}-{os.path.basename(path)}"))


class LiveProcessor(threading.Thread):
    """Runs effects, meters and monitoring for every track, one block at a time.

    Recording never waits for this: input threads only hand blocks over. If
    effects can't keep up (too many tracks for this computer), the oldest
    blocks are skipped, so meters and monitoring stutter but recordings stay
    perfect.
    """

    MAX_AGE = 0.25  # seconds a block may wait before it is skipped

    def __init__(self):
        super().__init__(name="live-effects", daemon=True)
        self._q = deque()
        self._wake = threading.Event()
        self._stop = False
        self.skipped = 0
        self.last_skip = 0.0
        self.busy = 0.0       # fraction of time spent processing (smoothed)

    def submit(self, rt, block):
        self._q.append((time.monotonic(), rt, block))
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    @property
    def overloaded(self):
        return time.monotonic() - self.last_skip < 3.0

    def run(self):
        q = self._q
        t_idle = time.perf_counter()
        while not self._stop:
            if not q:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            t_start = time.perf_counter()
            idle = t_start - t_idle
            n = 0
            while q and not self._stop:
                t_in, rt, block = q.popleft()
                if time.monotonic() - t_in > self.MAX_AGE:
                    self.skipped += 1
                    self.last_skip = time.monotonic()
                    continue
                try:
                    rt.process_live(block)
                except Exception:
                    log.exception("live effects failed for %s", rt.track.name)
                n += 1
            t_idle = time.perf_counter()
            work = t_idle - t_start
            if work + idle > 0:
                self.busy = 0.95 * self.busy + 0.05 * (work / (work + idle))


class TrackRuntime:
    """Live state for one track: channel routing, effects, meter, monitor."""

    def __init__(self, engine, track, capture_key, channel_map):
        self.engine = engine
        self.track = track
        self.capture_key = capture_key
        self.channel_map = channel_map
        self.channels = track.channels
        self.dsp = TrackDSP(self.channels)
        self._fx_seen = None
        self.meter = Meter()
        self.monitor = None
        self.rec = None

    def on_block(self, block, t0, capture):
        """Input thread: record the raw audio, hand the rest to the live processor."""
        x = block if self.channel_map is None else block[:, self.channel_map]
        rec = self.rec
        if rec is not None:
            rec.feed(x, t0, capture)
        self.engine.live.submit(self, x)

    def process_live(self, x):
        """Live-processor thread: effects, level meter and monitoring."""
        tr = self.track
        if tr.fx is not self._fx_seen:
            self.dsp.configure(tr.fx)
            self._fx_seen = tr.fx
        raw_peak = float(np.max(np.abs(x))) if x.size else 0.0
        y = self.dsp.process(x)
        g = db_to_lin(tr.gain_db)
        if g != 1.0:
            y = y * g
        self.meter.update(y, raw_peak)
        mon = self.monitor
        if mon is not None and self.engine.audible(tr):
            mon.push(to_stereo(y, tr.pan) * self.engine.master_lin)

    def stop_monitor(self):
        mon, self.monitor = self.monitor, None
        if mon is not None:
            mon.stop()


class Engine:
    def __init__(self, store=None, settings=None, watch=True):
        self.store = store or ProjectStore()
        self.settings = settings or Settings()
        self.project = None
        self.lock = threading.RLock()
        self.captures = {}
        self.runtimes = {}
        self.routes = {}
        self.graph = pw.Graph(ok=False)
        self.watcher = pw.Watcher(self._on_graph) if watch else None
        self.recorder = None
        self.rec_take = None
        self.player = Player()
        self.histories = {}
        self.jobs = {}
        self.master_lin = 1.0
        self._any_solo = False
        self._dirty = True
        self._sources_dirty = False
        self._events = deque()
        self._closing = False
        self._save_at = None
        self._stop_lock = threading.Lock()
        self.live = LiveProcessor()
        self.live.start()
        self._overloaded = False

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.watcher is not None:
            self.graph = pw.snapshot()
            self.watcher.graph = self.graph
            self.watcher.start()
        last = self.settings.get("last_project")
        project = None
        if last and os.path.isdir(last):
            try:
                project = self.store.open(last)
            except Exception as e:
                log.warning("could not open last project %s: %s", last, e)
        if project is None:
            existing = self.store.list()
            if existing:
                try:
                    project = self.store.open(existing[0]["path"])
                except Exception:
                    project = None
            if project is None:
                project = self.store.create("My First Project")
        self._load_project(project)

    def shutdown(self):
        self._closing = True
        try:
            if self.recorder is not None:
                self.stop_recording()
        except Exception:
            log.exception("stopping the recording failed")
        self.player.stop()
        for job in list(self.jobs.values()):
            if job.get("job"):
                job["job"].cancel()
        with self.lock:
            for rt in self.runtimes.values():
                rt.stop_monitor()
            for cap in self.captures.values():
                cap.stop()
            self.captures.clear()
            if self.project is not None:
                self.project.save()
                self._save_at = None
        if self.watcher is not None:
            self.watcher.stop()
        self.live.stop()

    # --------------------------------------------------------------- events
    def _mark_dirty(self, *_):
        self._dirty = True

    def _save_soon(self):
        """Save within a second. Used for mixer tweaks (a slider drag sends
        many small changes) so the project file isn't rewritten for each one."""
        if self._save_at is None:
            self._save_at = time.monotonic() + 1.0

    def _flush_save(self):
        if self._save_at is not None and self.project is not None:
            self._save_at = None
            self.project.save()

    def take_dirty(self):
        d, self._dirty = self._dirty, False
        return d

    def emit(self, event):
        self._events.append(event)

    def pop_events(self):
        out = []
        while self._events:
            out.append(self._events.popleft())
        if self._sources_dirty:
            self._sources_dirty = False
            out.append({"type": "sources"})
        return out

    def toast(self, text, kind="info"):
        self.emit({"type": "toast", "text": text, "kind": kind})

    # ---------------------------------------------------------------- graph
    def _on_graph(self, graph):
        with self.lock:
            self.graph = graph
            rebuild = False
            for rt in self.runtimes.values():
                src = rt.track.source
                if src.get("kind") == "device" and graph.ok:
                    node = graph.by_name(src.get("node"))
                    cap = self.captures.get(rt.capture_key)
                    if node and node.channels and cap is not None and len(node.channels) != cap.channels:
                        rebuild = True
            if rebuild and self.recorder is None:
                self._rebuild_routes()
            for cap in list(self.captures.values()):
                cap.refresh(graph)
        self._sources_dirty = True
        self._dirty = True

    # ------------------------------------------------------------- projects
    def _load_project(self, project):
        with self.lock:
            self._flush_save()
            self.player.stop()
            for rt in self.runtimes.values():
                rt.stop_monitor()
            for cap in self.captures.values():
                cap.stop()
            self.captures.clear()
            self.runtimes.clear()
            self.routes = {}
            self.histories.clear()
            self.project = project
            self.master_lin = db_to_lin(project.master_gain_db)
            self.settings.set("last_project", project.path)
            self._rebuild_routes()
        self._dirty = True

    def list_projects(self):
        out = self.store.list()
        for p in out:
            try:
                p["current"] = bool(self.project and os.path.samefile(p["path"], self.project.path))
            except OSError:
                p["current"] = False
        return {"root": self.store.root, "projects": out}

    def create_project(self, name, copy_inputs=False):
        self._not_recording()
        name = (name or "").strip()
        if not name:
            raise UserError("Give the project a name")
        p = self.store.create(name, template=self.project if copy_inputs else None)
        self._load_project(p)
        return p.path

    def open_project(self, path):
        self._not_recording()
        if not os.path.isfile(os.path.join(path, "project.json")):
            raise UserError("That folder is not an AudioMagic project")
        self._load_project(self.store.open(path))

    def rename_project(self, name):
        name = (name or "").strip()[:80]
        if not name:
            raise UserError("The name can't be empty")
        with self.lock:
            self.project.name = name
            self.project.save()
        self._dirty = True

    def _not_recording(self):
        if self.recorder is not None:
            raise UserError("Stop recording first")

    # -------------------------------------------------------------- routing
    def _dispatch(self, capture, block, t0):
        for rt in self.routes.get(capture.key, ()):
            rt.on_block(block, t0, capture)

    def _capture_for(self, track):
        """(key, channels, channel_map, factory) for a track's source."""
        src = track.source
        kind = src.get("kind")
        label = src.get("label") or track.name
        cb = (self._dispatch, self._mark_dirty)
        if kind == "device":
            node = src.get("node")
            gnode = self.graph.by_name(node) if self.graph.ok else None
            chans = [int(c) for c in (src.get("channels") or [0])][:2]
            if gnode is not None and gnode.channels:
                dev_ch = len(gnode.channels)
            else:
                dev_ch = int(src.get("device_channels") or (max(chans) + 1))
            chans = [c for c in chans if c < dev_ch] or [0]
            if len(chans) == 1 and track.channels == 2:
                chans = chans * 2
            cmap = None if chans == list(range(dev_ch)) else chans
            key = ("pw", node)
            return key, dev_ch, cmap, lambda: PipeWireCapture(
                key, dev_ch, gnode.description if gnode else label, *cb,
                resolver=lambda g: node if g.by_name(node) else None,
                waiting_message="device not connected")
        if kind == "monitor":
            node = src.get("node") or "@default"
            key = ("mon", node)

            def resolve(g, node=node):
                name = g.default_sink if node == "@default" else node
                return name if name and g.by_name(name) else None
            return key, 2, None, lambda: PipeWireCapture(key, 2, label, *cb, resolver=resolve, capture_sink=True,
                                                         waiting_message="output not available")
        if kind == "app":
            app, binary = src.get("app"), src.get("binary")
            key = ("app", binary or app)

            def resolve_app(g, app=app, binary=binary):
                n = g.find_app(app, binary)
                return n.serial if n else None
            return key, 2, None, lambda: PipeWireCapture(
                key, 2, label, *cb, resolver=resolve_app, waiting_status="waiting",
                waiting_message=f"waiting for {app or binary} to play audio")
        if kind == "url":
            url = src.get("url")
            key = ("url", url)
            return key, 2, None, lambda: UrlCapture(key, url, label, *cb)
        if kind == "srt":
            port = int(src.get("port", 9000))
            key = ("srt", port)
            return key, 2, None, lambda: SrtCapture(key, port, src.get("passphrase"), src.get("lan"),
                                                    src.get("latency", 200), label, *cb)
        key = ("tone", track.id)
        return key, 1, None, lambda: ToneCapture(key, src.get("freq", 440), label, *cb)

    def _rebuild_routes(self):
        """Match inputs and runtimes to the project's tracks. Call with the lock held."""
        project = self.project
        wanted = {}
        runtimes = {}
        for tr in project.tracks:
            key, ch, cmap, factory = self._capture_for(tr)
            wanted.setdefault(key, (ch, factory))
            old = self.runtimes.get(tr.id)
            if old is not None and old.capture_key == key and old.channel_map == cmap and old.channels == tr.channels:
                old.track = tr
                runtimes[tr.id] = old
            else:
                if old is not None:
                    old.stop_monitor()
                runtimes[tr.id] = TrackRuntime(self, tr, key, cmap)
        for tid, old in self.runtimes.items():
            if tid not in runtimes:
                old.stop_monitor()
        for key, cap in list(self.captures.items()):
            if key not in wanted or cap.channels != wanted[key][0]:
                cap.stop()
                del self.captures[key]
        self.runtimes = runtimes
        routes = {}
        for rt in runtimes.values():
            routes.setdefault(rt.capture_key, []).append(rt)
        self.routes = routes
        for key, (ch, factory) in wanted.items():
            if key not in self.captures:
                cap = factory()
                self.captures[key] = cap
                if isinstance(cap, PipeWireCapture):
                    if self.graph.ok:
                        cap.refresh(self.graph)
                    else:
                        cap._set_status("offline", "PipeWire is not available")
                else:
                    cap.start()
        self._any_solo = any(t.solo for t in project.tracks)
        self._update_monitors()
        self._dirty = True

    def audible(self, track):
        return not track.mute and (track.solo or not self._any_solo)

    def _output_target(self):
        """The chosen output, or None (the system default) if it isn't connected right now."""
        target = self.settings.get("output")
        if target and self.graph.ok and not any(n.name == target for n in self.graph.sinks()):
            return None
        return target or None

    def _update_monitors(self):
        target = self._output_target()
        for rt in self.runtimes.values():
            want = rt.track.monitor and not self._closing
            if want and rt.monitor is None:
                out = OutputStream(f"Monitor {rt.track.name}", 2, live=True, target=target)
                out.start()
                rt.monitor = out
            elif not want and rt.monitor is not None:
                rt.stop_monitor()

    # ------------------------------------------------------------- sources
    def list_sources(self):
        g = pw.snapshot()
        if not g.ok:
            return {"ok": False, "error": g.error, "devices": [], "apps": [], "outputs": []}
        devices = [{
            "node": n.name, "label": n.description, "channels": n.channels,
            "channel_labels": [pw.channel_label(i, c) for i, c in enumerate(n.channels)],
            "default": n.name == g.default_source,
        } for n in g.sources()]
        apps = {}
        for n in g.app_streams():
            k = n.binary or n.app_name
            if k and k not in apps:
                apps[k] = {"app": n.app_name, "binary": n.binary, "label": n.app_label, "detail": n.media_name}
        outputs = [{"node": n.name, "label": n.description, "default": n.name == g.default_sink} for n in g.sinks()]
        return {"ok": True, "devices": devices, "apps": list(apps.values()), "outputs": outputs}

    # --------------------------------------------------------------- tracks
    def _clean_source(self, src):
        if not isinstance(src, dict):
            raise UserError("Invalid input")
        kind = src.get("kind")
        label = str(src.get("label") or "")[:120]
        if kind == "device":
            node = str(src.get("node") or "")
            chans = [int(c) for c in (src.get("channels") or [0])][:2]
            if not node or any(c < 0 or c > 63 for c in chans):
                raise UserError("Pick a device and input")
            return {"kind": "device", "node": node, "channels": chans, "label": label or node,
                    "device_channels": int(src.get("device_channels") or (max(chans) + 1))}
        if kind == "monitor":
            return {"kind": "monitor", "node": str(src.get("node") or "@default"), "label": label or "Everything you hear"}
        if kind == "app":
            app, binary = src.get("app"), src.get("binary")
            if not (app or binary):
                raise UserError("Pick a program")
            return {"kind": "app", "app": app, "binary": binary, "label": label or app or binary}
        if kind == "url":
            url = str(src.get("url") or "").strip()
            u = urlparse(url)
            if u.scheme not in UrlCapture.SCHEMES or not (u.netloc or u.scheme == "file"):
                raise UserError("Enter a full stream address, like https://example.com/stream.mp3")
            return {"kind": "url", "url": url, "label": label or u.netloc or url}
        if kind == "srt":
            port = int(src.get("port") or 9000)
            if not 1024 <= port <= 65535:
                raise UserError("Pick a port between 1024 and 65535")
            pw_ = str(src.get("passphrase") or "")
            if pw_ and not 10 <= len(pw_) <= 79:
                raise UserError("The SRT passphrase must be 10 to 79 characters (or empty for no encryption)")
            return {"kind": "srt", "port": port, "passphrase": pw_, "lan": bool(src.get("lan")),
                    "latency": int(clamp(int(src.get("latency") or 200), 20, 8000)), "label": label or f"SRT port {port}"}
        if kind == "tone":
            freq = float(clamp(float(src.get("freq") or 440), 20, 20000))
            return {"kind": "tone", "freq": freq, "label": label or f"Test tone {freq:g} Hz"}
        raise UserError("Unknown input type")

    def add_tracks(self, items):
        self._not_recording()
        added = []
        with self.lock:
            for item in items:
                src = self._clean_source(item.get("source"))
                tid = self.project.next_id("t")
                name = str(item.get("name") or src["label"])[:60]
                tr = Track({"id": tid, "name": name, "source": src, "color": self.project.next_color(),
                            "fx": copy.deepcopy(DEFAULT_FX)})
                self.project.tracks.append(tr)
                added.append(tid)
            self.project.save()
            self._rebuild_routes()
        return added

    def update_track(self, track_id, changes):
        with self.lock:
            tr = self._track(track_id)
            rebuild = False
            if "name" in changes:
                tr.name = str(changes["name"]).strip()[:60] or tr.name
            if "gain_db" in changes:
                tr.gain_db = float(clamp(float(changes["gain_db"]), -60.0, 24.0))
            if "pan" in changes:
                tr.pan = float(clamp(float(changes["pan"]), -1.0, 1.0))
            for key in ("mute", "solo", "armed", "monitor"):
                if key in changes:
                    if key == "armed" and self.recorder is not None:
                        raise UserError("Stop recording before arming or disarming inputs")
                    setattr(tr, key, bool(changes[key]))
            if "fx" in changes:
                tr.fx = self._clean_fx(tr.fx, changes["fx"])
            if "preset" in changes:
                preset = FX_PRESETS.get(changes["preset"])
                if preset is None:
                    raise UserError("Unknown preset")
                tr.fx = self._clean_fx(tr.fx, preset)
            if "source" in changes:
                self._not_recording()
                tr.source = self._clean_source(changes["source"])
                rebuild = True
            self._any_solo = any(t.solo for t in self.project.tracks)
            if rebuild:
                self.project.save()
                self._rebuild_routes()
            else:
                self._save_soon()
                self._update_monitors()
        self._dirty = True

    @staticmethod
    def _clean_fx(current, changes):
        fx = merge_fx(current)
        for group, vals in (changes or {}).items():
            if group in fx and isinstance(vals, dict):
                for k, v in vals.items():
                    if k in fx[group]:
                        fx[group][k] = v if isinstance(fx[group][k], bool) is False else bool(v)
        ns, gate, eq = fx["ns"], fx["gate"], fx["eq"]
        ns["on"] = bool(ns["on"])
        ns["strength"] = float(clamp(float(ns["strength"]), 0.0, 1.0))
        gate["on"] = bool(gate["on"])
        gate["threshold"] = float(clamp(float(gate["threshold"]), -90.0, 0.0))
        gate["range"] = float(clamp(float(gate["range"]), -90.0, 0.0))
        gate["attack"] = float(clamp(float(gate["attack"]), 0.1, 50.0))
        gate["hold"] = float(clamp(float(gate["hold"]), 0.0, 2000.0))
        gate["release"] = float(clamp(float(gate["release"]), 5.0, 2000.0))
        eq["on"] = bool(eq["on"])
        eq["lowcut"] = bool(eq["lowcut"])
        eq["lowcut_hz"] = float(clamp(float(eq["lowcut_hz"]), 20.0, 300.0))
        for band in ("low", "mid", "high"):
            eq[band] = float(clamp(float(eq[band]), -15.0, 15.0))
        return fx

    def remove_track(self, track_id, confirm=False):
        self._not_recording()
        with self.lock:
            tr = self._track(track_id)
            used = self.project.takes_using(track_id)
            if used and not confirm:
                raise NeedsConfirm(f"'{tr.name}' has audio in {len(used)} take(s). Removing it moves that audio "
                                   f"to the trash.")
            self.player.stop()
            for take in used:
                tf = take.file_for(track_id)
                path = self.project.abspath(tf["file"])
                for p in (path, peaks_mod.peaks_path(path)):
                    try:
                        trash(p)
                    except OSError as e:
                        log.warning("could not trash %s: %s", p, e)
                take.tracks = [t for t in take.tracks if t["track_id"] != track_id]
                take.edits.silences.pop(track_id, None)
                take.edits.clip_gain_db.pop(track_id, None)
            self.project.tracks = [t for t in self.project.tracks if t.id != track_id]
            self.project.save()
            self._rebuild_routes()

    def move_track(self, track_id, index):
        with self.lock:
            tr = self._track(track_id)
            tracks = [t for t in self.project.tracks if t.id != track_id]
            index = int(clamp(int(index), 0, len(tracks)))
            tracks.insert(index, tr)
            self.project.tracks = tracks
            self.project.save()
        self._dirty = True

    def _track(self, track_id):
        tr = self.project.track(track_id)
        if tr is None:
            raise UserError("That input no longer exists")
        return tr

    # --------------------------------------------------------- master/output
    def set_master(self, gain_db):
        with self.lock:
            self.project.master_gain_db = float(clamp(float(gain_db), -60.0, 12.0))
            self.master_lin = db_to_lin(self.project.master_gain_db)
            self._save_soon()
        self._dirty = True

    def set_output(self, node):
        with self.lock:
            self.settings.set("output", node or None)
            for rt in self.runtimes.values():
                rt.stop_monitor()
            self._update_monitors()
            player = self.player
            if player.state == "playing" and player.take_id:
                # move playback to the new output, carrying on from the same spot
                pos = (player.position() or 0) / SAMPLE_RATE
                self.play(player.take_id, pos)
        self._dirty = True

    # ------------------------------------------------------------ recording
    def start_recording(self):
        with self.lock:
            if self.recorder is not None:
                return self.rec_take.id
            armed = [t for t in self.project.tracks if t.armed]
            if not armed:
                raise UserError("Arm at least one input (the red R button) to record")
            self.player.stop()
            p = self.project
            num = p.next_take_number()
            take_dir = f"audio/take-{num:03d}"
            files = []
            for i, t in enumerate(armed, 1):
                files.append({"track_id": t.id, "file": f"{take_dir}/{i:02d}-{slugify(t.name, 'input')}.wav",
                              "channels": t.channels, "name": t.name})
            take = Take({"id": p.next_id("k"), "number": num, "state": "recording", "dir": take_dir, "tracks": files})
            rec = Recorder(p.path)
            offline = []
            for f in files:
                rt = self.runtimes[f["track_id"]]
                cap = self.captures.get(rt.capture_key)
                rt.rec = rec.add_track(f["track_id"], f["file"], f["channels"], bool(cap and cap.graph_synced))
                if cap is None or cap.status != "live":
                    offline.append(rt.track.name)
            p.takes.append(take)
            p.save()
            self.recorder = rec
            self.rec_take = take
        if offline:
            self.toast(f"No signal yet from {', '.join(offline)}; recording silence until it arrives", "warn")
        self._dirty = True
        return take.id

    def stop_recording(self):
        with self._stop_lock:
            return self._stop_recording()

    def _stop_recording(self):
        rec, take = self.recorder, self.rec_take
        if rec is None:
            return None
        duration = rec.finalize()
        with self.lock:
            for rt in self.runtimes.values():
                rt.rec = None
            self.recorder = None
            self.rec_take = None
            p = self.project
            if duration < SAMPLE_RATE // 5:
                p.takes = [t for t in p.takes if t.id != take.id]
                shutil.rmtree(p.abspath(take.dir), ignore_errors=True)
                p.save()
                self._dirty = True
                self.toast("That take was too short to keep")
                return None
            take.duration = duration
            take.state = "done"
            p.save()
        self._dirty = True
        self.emit({"type": "recorded", "take": take.id})
        return take.id

    # ------------------------------------------------------------- playback
    def play(self, take_id, pos=0.0):
        self._not_recording()
        take = self._take(take_id)
        ok = self.player.play(self.project, take, int(max(0.0, float(pos)) * SAMPLE_RATE),
                              include=self.audible, master=lambda: self.master_lin,
                              target=self._output_target())
        self._dirty = True
        return ok

    def pause(self):
        self.player.pause()
        self._dirty = True

    def resume(self):
        self.player.resume()
        self._dirty = True

    def stop_playback(self):
        self.player.stop()
        self._dirty = True

    # ---------------------------------------------------------------- takes
    def _take(self, take_id):
        t = self.project.take(take_id)
        if t is None:
            raise UserError("That take no longer exists")
        if t.state == "recording":
            raise UserError("That take is still recording")
        return t

    def rename_take(self, take_id, name):
        with self.lock:
            t = self._take(take_id)
            t.name = (str(name).strip() or t.name)[:80]
            self.project.save()
        self._dirty = True

    def delete_take(self, take_id):
        with self.lock:
            t = self._take(take_id)
            if self.player.take_id == take_id:
                self.player.stop()
            self.project.takes = [x for x in self.project.takes if x.id != take_id]
            self.project.save()
            try:
                trash(self.project.abspath(t.dir))
            except OSError as e:
                log.warning("could not trash take folder: %s", e)
        self._dirty = True

    def edit_take(self, take_id, op, args):
        with self.lock:
            take = self._take(take_id)
            hist = self.histories.setdefault(take_id, History())
            e = take.edits
            a = int(float(args.get("start", 0)) * SAMPLE_RATE)
            b = int(float(args.get("end", 0)) * SAMPLE_RATE)
            if op in ("cut", "trim", "silence", "unsilence") and b - a < 1:
                raise UserError("Select part of the recording first (drag across the waveform)")
            if op == "undo":
                new = hist.undo(e)
                if new is None:
                    return
            elif op == "redo":
                new = hist.redo(e)
                if new is None:
                    return
            else:
                if op == "cut":
                    new = op_cut(e, take.duration, a, b)
                elif op == "trim":
                    new = op_trim(e, take.duration, a, b)
                elif op == "silence":
                    new = op_silence(e, take.duration, self._track(args.get("track")).id, a, b)
                elif op == "unsilence":
                    new = op_unsilence(e, take.duration, self._track(args.get("track")).id, a, b)
                elif op == "fades":
                    new = e.copy()
                    new.fade_in = float(clamp(float(args.get("fade_in", e.fade_in)), 0.0, 60.0))
                    new.fade_out = float(clamp(float(args.get("fade_out", e.fade_out)), 0.0, 60.0))
                elif op == "normalize":
                    new = e.copy()
                    tid = self._track(args.get("track")).id
                    peak = self._raw_peak(take, tid)
                    if peak <= 0:
                        raise UserError("That track is silent")
                    new.clip_gain_db[tid] = round(float(clamp(-1.0 - lin_to_db(peak), -30.0, 40.0)), 2)
                elif op == "clear_gain":
                    new = e.copy()
                    new.clip_gain_db.pop(args.get("track"), None)
                elif op == "reset":
                    new = Edits()
                else:
                    raise UserError("Unknown edit")
                hist.push(e)
            if self.player.take_id == take_id and self.player.state != "stopped":
                self.player.stop()
            take.edits = new
            self.project.save()
        self._dirty = True

    def _raw_peak(self, take, track_id):
        tf = take.file_for(track_id)
        if tf is None:
            raise UserError("That track has no audio in this take")
        reader = WavReader(self.project.abspath(tf["file"]))
        tl = Timeline(take.edits, take.duration)
        silences = take.edits.silences.get(track_id, [])
        from .edits import subtract_ranges
        peak = 0.0
        for a, b in tl.segments:
            for sa, sb in subtract_ranges((a, b), silences):
                for s in range(sa, sb, SAMPLE_RATE * 10):
                    x = reader.read(s, min(SAMPLE_RATE * 10, sb - s))
                    if x.size:
                        peak = max(peak, float(np.max(np.abs(x))))
        return peak

    def peaks(self, take_id, track_id):
        with self.lock:
            take = self.project.take(take_id)
            if take is None:
                raise UserError("That take no longer exists")
            if self.recorder is not None and self.rec_take is take:
                rt = self.recorder.tracks.get(track_id)
                return rt.peaks.all().tobytes() if rt else b""
            tf = take.file_for(track_id)
            if tf is None:
                return b""
            path = self.project.abspath(tf["file"])
        if not os.path.exists(path):
            return b""
        return peaks_mod.load_or_compute(path).tobytes()

    # --------------------------------------------------------------- export
    def start_export(self, take_id, opts):
        with self.lock:
            take = self._take(take_id)
            tracks = [copy.deepcopy(t) for t in self.project.tracks]
            if "track_ids" not in opts:
                opts["track_ids"] = [t.id for t in tracks if self.audible(t) and take.file_for(t.id)]
            self.project.export = {k: v for k, v in opts.items() if k not in ("track_ids",)}
            self.project.save()
            project = self.project
        job_id = uuid.uuid4().hex[:8]
        info = {"id": job_id, "state": "running", "progress": 0.0, "message": "Starting", "files": [],
                "folder": os.path.expanduser(opts.get("folder") or os.path.join(project.path, "exports"))}

        def progress(frac, msg):
            info["progress"] = round(frac, 3)
            info["message"] = msg
            self.emit({"type": "export", "job": {k: v for k, v in info.items() if k != "job"}})

        job = ExportJob(project, take, tracks, opts, progress)
        info["job"] = job
        self.jobs[job_id] = info

        def run():
            try:
                info["files"] = job.run()
                info["state"] = "done"
                info["message"] = f"Exported {len(info['files'])} file(s)"
            except ExportError as e:
                info["state"] = "cancelled" if str(e) == "cancelled" else "error"
                info["message"] = str(e)
            except Exception as e:
                log.exception("export failed")
                info["state"] = "error"
                info["message"] = f"Export failed: {e}"
            self.emit({"type": "export", "job": {k: v for k, v in info.items() if k != "job"}})

        threading.Thread(target=run, name=f"export-{job_id}", daemon=True).start()
        return job_id

    def cancel_export(self, job_id):
        info = self.jobs.get(job_id)
        if info and info.get("job"):
            info["job"].cancel()

    # --------------------------------------------------------- housekeeping
    def poll(self):
        if self.live.overloaded != self._overloaded:
            self._overloaded = self.live.overloaded
            self._dirty = True
            if self._overloaded:
                log.warning("live effects are falling behind; monitoring may stutter (recording is unaffected)")
        if self._save_at is not None and time.monotonic() >= self._save_at:
            with self.lock:
                self._flush_save()
        rec = self.recorder
        if rec is not None:
            failed = [t for t in rec.tracks.values() if t.error]
            if failed:
                self.toast(f"Recording stopped: {failed[0].error}. Everything up to this point was saved.", "error")
                self.stop_recording()
        for cap in list(self.captures.values()):
            try:
                cap.poll()
            except Exception:
                log.exception("input poll failed")
        for rt in list(self.runtimes.values()):
            mon = rt.monitor
            if mon is not None and mon.poll() == "error":
                self.toast(f"Monitoring {rt.track.name} failed: {mon.error}", "error")
                rt.stop_monitor()
                rt.track.monitor = False
                self._dirty = True
        if self.player.poll() == "ended":
            self.emit({"type": "ended", "pos": self.player.stop_pos / SAMPLE_RATE})
            self._dirty = True

    def meters(self):
        play_tracks, master = ({}, None)
        if self.player.state != "stopped":
            play_tracks, master = self.player.meters()
        tracks = {}
        for tid, rt in list(self.runtimes.items()):
            live = rt.meter.read()
            tracks[tid] = play_tracks.get(tid) or live
        pos = self.player.position()
        rec = self.recorder
        return {
            "type": "meters",
            "tracks": tracks,
            "master": master,
            "pos": None if pos is None else pos / SAMPLE_RATE,
            "rec": None if rec is None else rec.elapsed_frames() / SAMPLE_RATE,
        }

    def rec_peaks(self):
        rec = self.recorder
        if rec is None:
            return None
        out = {}
        for tid, rt in rec.tracks.items():
            bins, start = rt.peaks.new_bins()
            if bins.shape[0]:
                out[tid] = {"start": start, "data": bins.reshape(-1).tolist()}
        return out

    def state(self):
        with self.lock:
            p = self.project
            tracks = []
            for t in p.tracks:
                d = t.to_dict()
                rt = self.runtimes.get(t.id)
                cap = self.captures.get(rt.capture_key) if rt else None
                d["status"] = cap.status if cap else "stopped"
                d["status_message"] = cap.message if cap else ""
                d["recording"] = bool(rt and rt.rec is not None)
                tracks.append(d)
            takes = []
            for tk in p.takes:
                d = tk.to_dict()
                d["timeline"] = Timeline(tk.edits, tk.duration).to_dict()
                h = self.histories.get(tk.id)
                d["can_undo"] = bool(h and h.undo_stack)
                d["can_redo"] = bool(h and h.redo_stack)
                takes.append(d)
            armed_ch = sum(t.channels for t in p.tracks if t.armed)
            try:
                free = shutil.disk_usage(p.path).free
            except OSError:
                free = None
            player = self.player
            return {
                "version": __version__,
                "project": {"name": p.name, "path": p.path, "master_gain_db": p.master_gain_db, "export": p.export},
                "tracks": tracks,
                "takes": takes,
                "transport": {
                    "recording": self.recorder is not None,
                    "rec_take": self.rec_take.id if self.rec_take else None,
                    "playing": player.state == "playing",
                    "paused": player.state == "paused",
                    "play_take": player.take_id if player.state != "stopped" else None,
                    "stop_pos": player.stop_pos / SAMPLE_RATE,
                },
                "output": self.settings.get("output"),
                "outputs": [{"node": n.name, "label": n.description} for n in self.graph.sinks()] if self.graph.ok else [],
                "disk": {"free": free, "bytes_per_sec": armed_ch * 3 * SAMPLE_RATE},
                "pipewire": {"ok": self.graph.ok, "error": self.graph.error},
                "effects_overloaded": self.live.overloaded,
                "presets": sorted(FX_PRESETS),
            }
