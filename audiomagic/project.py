"""Projects, tracks and takes, stored as JSON next to the audio files.

Layout of a project folder::

    ~/Music/AudioMagic/My Podcast/
        project.json
        audio/take-001/01-host-mic.wav
        exports/
        trash/        (audio of removed tracks, kept just in case)
"""

import copy
import datetime
import json
import os
import shutil

from . import SAMPLE_RATE
from .dsp import merge_fx
from .edits import Edits
from .util import log, safe_dirname
from . import wavio

PROJECT_FILE = "project.json"
TRACK_COLORS = ["#4f9cf9", "#f59e0b", "#22c55e", "#ef4444", "#a78bfa", "#ec4899", "#14b8a6", "#eab308"]


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def music_dir():
    home = os.path.expanduser("~")
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "user-dirs.dirs")
    try:
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                if line.startswith("XDG_MUSIC_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"').replace("$HOME", home)
                    if val and os.path.isabs(val):
                        return val
    except OSError:
        pass
    return os.path.join(home, "Music")


def default_root():
    return os.environ.get("AUDIOMAGIC_ROOT") or os.path.join(music_dir(), "AudioMagic")


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def source_channels(src):
    kind = src.get("kind")
    if kind == "device":
        return max(1, min(2, len(src.get("channels") or [0])))
    if kind == "tone":
        return 1
    return 2


class Track:
    FIELDS = ("id", "name", "source", "gain_db", "pan", "mute", "solo", "armed", "monitor", "fx", "color")

    def __init__(self, data):
        self.id = data["id"]
        self.name = data.get("name") or "Input"
        self.source = dict(data.get("source") or {"kind": "tone", "freq": 440, "label": "Test tone"})
        self.gain_db = float(data.get("gain_db", 0.0))
        self.pan = float(data.get("pan", 0.0))
        self.mute = bool(data.get("mute", False))
        self.solo = bool(data.get("solo", False))
        self.armed = bool(data.get("armed", True))
        self.monitor = bool(data.get("monitor", False))
        self.fx = merge_fx(data.get("fx"))
        self.color = data.get("color") or TRACK_COLORS[0]

    @property
    def channels(self):
        return source_channels(self.source)

    def to_dict(self):
        d = {k: copy.deepcopy(getattr(self, k)) for k in self.FIELDS}
        d["channels"] = self.channels
        return d


class Take:
    def __init__(self, data):
        self.id = data["id"]
        self.number = int(data.get("number", 1))
        self.name = data.get("name") or f"Take {self.number}"
        self.created = data.get("created") or now_iso()
        self.duration = int(data.get("duration", 0))
        self.rate = int(data.get("rate", SAMPLE_RATE))
        self.state = data.get("state", "done")
        self.dir = data.get("dir") or f"audio/take-{self.number:03d}"
        self.tracks = [dict(t) for t in data.get("tracks", [])]
        self.edits = Edits(data.get("edits"))

    def file_for(self, track_id):
        for t in self.tracks:
            if t["track_id"] == track_id:
                return t
        return None

    def to_dict(self):
        return {
            "id": self.id,
            "number": self.number,
            "name": self.name,
            "created": self.created,
            "duration": self.duration,
            "rate": self.rate,
            "state": self.state,
            "dir": self.dir,
            "tracks": copy.deepcopy(self.tracks),
            "edits": self.edits.to_dict(),
        }


class Project:
    def __init__(self, path, data):
        self.path = path
        self.name = data.get("name") or os.path.basename(path)
        self.created = data.get("created") or now_iso()
        self.tracks = [Track(t) for t in data.get("tracks", [])]
        self.takes = [Take(t) for t in data.get("takes", [])]
        self.master_gain_db = float(data.get("master_gain_db", 0.0))
        self.export = dict(data.get("export") or {})
        self.counter = int(data.get("counter", 0))

    # ---- persistence
    @classmethod
    def load(cls, path):
        with open(os.path.join(path, PROJECT_FILE), encoding="utf-8") as f:
            data = json.load(f)
        p = cls(path, data)
        p.recover()
        return p

    def to_dict(self):
        return {
            "version": 1,
            "name": self.name,
            "created": self.created,
            "master_gain_db": self.master_gain_db,
            "counter": self.counter,
            "tracks": [t.to_dict() for t in self.tracks],
            "takes": [t.to_dict() for t in self.takes],
            "export": self.export,
        }

    def save(self):
        atomic_write_json(os.path.join(self.path, PROJECT_FILE), self.to_dict())

    def recover(self):
        """Fix takes left behind by a crash during recording."""
        changed = False
        for take in self.takes:
            if take.state != "recording":
                continue
            longest = 0
            for t in take.tracks:
                fp = os.path.join(self.path, t["file"])
                try:
                    longest = max(longest, wavio.repair(fp))
                except (OSError, ValueError) as e:
                    log.warning("could not recover %s: %s", fp, e)
            take.duration = longest
            take.state = "recovered"
            changed = True
            log.info("recovered take %s (%d frames)", take.name, longest)
        if changed:
            self.save()

    # ---- lookup / ids
    def next_id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def track(self, track_id):
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def take(self, take_id):
        for t in self.takes:
            if t.id == take_id:
                return t
        return None

    def next_color(self):
        used = [t.color for t in self.tracks]
        for c in TRACK_COLORS:
            if c not in used:
                return c
        return TRACK_COLORS[len(self.tracks) % len(TRACK_COLORS)]

    def next_take_number(self):
        return max([t.number for t in self.takes] + [0]) + 1

    def abspath(self, rel):
        return os.path.join(self.path, rel)

    def takes_using(self, track_id):
        return [t for t in self.takes if t.file_for(track_id)]


class ProjectStore:
    def __init__(self, root=None):
        self.root = root or default_root()

    def list(self):
        out = []
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return out
        for name in names:
            path = os.path.join(self.root, name)
            pf = os.path.join(path, PROJECT_FILE)
            if not os.path.isfile(pf):
                continue
            try:
                with open(pf, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({
                "path": path,
                "name": data.get("name") or name,
                "modified": os.path.getmtime(pf),
                "takes": len(data.get("takes", [])),
                "tracks": len(data.get("tracks", [])),
            })
        out.sort(key=lambda p: p["modified"], reverse=True)
        return out

    def create(self, name, template=None):
        base = safe_dirname(name)
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, base)
        n = 2
        while os.path.exists(path):
            path = os.path.join(self.root, f"{base} ({n})")
            n += 1
        os.makedirs(os.path.join(path, "audio"))
        os.makedirs(os.path.join(path, "exports"))
        data = {"name": name.strip() or base, "created": now_iso()}
        if template is not None:
            data["tracks"] = [t.to_dict() for t in template.tracks]
            data["counter"] = template.counter
            data["master_gain_db"] = template.master_gain_db
        p = Project(path, data)
        p.save()
        return p

    def open(self, path):
        if not os.path.isabs(path):
            path = os.path.join(self.root, path)
        return Project.load(path)


def move_to_trash(project, rel_path):
    src = project.abspath(rel_path)
    if not os.path.exists(src):
        return
    dst = os.path.join(project.path, "trash", rel_path.replace(os.sep, "_"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
