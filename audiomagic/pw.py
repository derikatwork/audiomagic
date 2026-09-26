"""A view of the PipeWire graph (devices, apps, outputs) via ``pw-dump``."""

import json
import subprocess
import threading

from .util import log

OUR_PREFIX = "audiomagic"

CHANNEL_NAMES = {
    "MONO": "Mono", "FL": "Left", "FR": "Right", "FC": "Center", "LFE": "LFE",
    "RL": "Rear left", "RR": "Rear right", "SL": "Side left", "SR": "Side right",
}


def channel_label(index, position):
    """'Input 1', 'Input 2 (Right)' ..."""
    pos = CHANNEL_NAMES.get(position or "")
    if position and position.startswith("AUX"):
        pos = None
    return f"Input {index + 1}" + (f" ({pos})" if pos and position not in ("MONO",) else "")


class Node:
    __slots__ = ("id", "serial", "name", "description", "media_class", "app_name", "binary",
                 "media_name", "channels", "pid")

    def __init__(self, id, props):
        self.id = id
        self.serial = str(props.get("object.serial", id))
        self.name = props.get("node.name") or f"node-{id}"
        self.description = props.get("node.description") or props.get("node.nick") or self.name
        self.media_class = props.get("media.class") or ""
        self.app_name = props.get("application.name")
        self.binary = props.get("application.process.binary")
        self.media_name = props.get("media.name")
        self.pid = props.get("application.process.id")
        self.channels = []

    @property
    def is_source(self):
        return self.media_class in ("Audio/Source", "Audio/Source/Virtual", "Audio/Duplex")

    @property
    def is_sink(self):
        return self.media_class in ("Audio/Sink", "Audio/Duplex")

    @property
    def is_app_stream(self):
        return self.media_class == "Stream/Output/Audio"

    @property
    def app_label(self):
        name = self.app_name or self.binary or self.description
        return name

    def sig(self):
        return (self.id, self.name, self.media_class, tuple(self.channels), self.app_name, self.binary)


class Graph:
    def __init__(self, nodes=None, default_sink=None, default_source=None, ok=True, error=None, version=None):
        self.nodes = nodes or {}
        self.version = version  # of the PipeWire daemon
        self.default_sink = default_sink
        self.default_source = default_source
        self.ok = ok
        self.error = error

    def signature(self):
        return (tuple(sorted(n.sig() for n in self.nodes.values())), self.default_sink, self.default_source)

    def by_name(self, name):
        for n in self.nodes.values():
            if n.name == name:
                return n
        return None

    def sources(self):
        return sorted((n for n in self.nodes.values() if n.is_source and n.channels), key=lambda n: n.description.lower())

    def sinks(self):
        return sorted((n for n in self.nodes.values() if n.is_sink), key=lambda n: n.description.lower())

    def app_streams(self):
        return sorted((n for n in self.nodes.values() if n.is_app_stream and n.channels), key=lambda n: (n.app_label or "").lower())

    def find_app(self, app_name=None, binary=None):
        """Newest playback stream of an app. An exact name+program match wins;
        otherwise any stream from the same program (or with the same name)."""
        exact = loose = None
        for n in self.nodes.values():
            if not n.is_app_stream:
                continue
            name_ok = not app_name or n.app_name == app_name
            bin_ok = not binary or n.binary == binary
            if name_ok and bin_ok:
                if exact is None or n.id > exact.id:
                    exact = n
            elif (binary and bin_ok) or (not binary and name_ok):
                if loose is None or n.id > loose.id:
                    loose = n
        return exact or loose


def parse_dump(objs):
    nodes = {}
    ports = []
    default_sink = default_source = version = None
    for o in objs:
        t = o.get("type", "")
        info = o.get("info") or {}
        props = info.get("props") or o.get("props") or {}
        if t == "PipeWire:Interface:Core":
            version = info.get("version")
        elif t == "PipeWire:Interface:Node":
            n = Node(o["id"], props)
            if n.name.startswith(OUR_PREFIX):
                continue
            nodes[n.id] = n
        elif t == "PipeWire:Interface:Port":
            ports.append(props)
        elif t == "PipeWire:Interface:Metadata" and props.get("metadata.name") == "default":
            for entry in o.get("metadata") or []:
                val = entry.get("value")
                name = val.get("name") if isinstance(val, dict) else None
                if entry.get("key") == "default.audio.sink":
                    default_sink = name
                elif entry.get("key") == "default.audio.source":
                    default_source = name
    grouped = {}
    for p in ports:
        grouped.setdefault(p.get("node.id"), []).append(p)
    for nid, plist in grouped.items():
        n = nodes.get(nid)
        if n is None:
            continue
        if n.is_source or n.is_app_stream:
            chosen = [p for p in plist if p.get("port.direction") == "out" and not p.get("port.monitor")]
        elif n.is_sink:
            chosen = [p for p in plist if p.get("port.direction") == "out" and p.get("port.monitor")]
        else:
            chosen = []
        chosen.sort(key=lambda p: (p.get("port.id", 0), p.get("object.id", 0)))
        n.channels = [p.get("audio.channel") or "MONO" for p in chosen]
    return Graph(nodes, default_sink, default_source, version=version)


def decode_dump(text):
    """pw-dump may print several JSON arrays when the graph changes while it
    runs; later arrays update earlier objects (``info: null`` = removed)."""
    dec = json.JSONDecoder()
    objs = {}
    pos, n = 0, len(text)
    while pos < n:
        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            break
        arr, pos = dec.raw_decode(text, pos)
        for o in arr if isinstance(arr, list) else []:
            oid = o.get("id")
            if o.get("info", True) is None and "metadata" not in o:
                objs.pop(oid, None)
            elif oid in objs and "type" not in o:
                objs[oid].update({k: v for k, v in o.items() if v is not None})
            else:
                objs[oid] = o
    return list(objs.values())


def snapshot(timeout=4.0):
    try:
        out = subprocess.run(["pw-dump", "--no-colors"], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=timeout, check=True).stdout
        return parse_dump(decode_dump(out.decode("utf-8", "replace")))
    except FileNotFoundError:
        return Graph(ok=False, error="pw-dump not found (install pipewire-bin)")
    except subprocess.CalledProcessError as e:
        lines = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return Graph(ok=False, error=f"pw-dump failed: {lines[-1] if lines else f'exit code {e.returncode}'}")
    except (subprocess.SubprocessError, ValueError) as e:
        return Graph(ok=False, error=f"could not read the PipeWire graph: {e}")


class Watcher(threading.Thread):
    """Polls the graph and calls ``on_change(graph)`` whenever it changes."""

    def __init__(self, on_change, interval=1.5):
        super().__init__(name="pw-watcher", daemon=True)
        self.on_change = on_change
        self.interval = interval
        self.graph = Graph(ok=False)
        self._sig = None
        self._wake = threading.Event()
        self._stop = threading.Event()

    def refresh(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            g = snapshot()
            sig = g.signature() if g.ok else ("error", g.error)
            if sig != self._sig:
                self._sig = sig
                self.graph = g
                if not g.ok:
                    log.warning("%s", g.error)
                try:
                    self.on_change(g)
                except Exception:
                    log.exception("graph change handler failed")
            self._wake.wait(self.interval)
            self._wake.clear()
