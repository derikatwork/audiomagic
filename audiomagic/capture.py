"""Audio inputs. Each Capture runs one GStreamer pipeline that ends in an
appsink and hands float32 blocks, shaped (frames, channels), to the engine.

* PipeWireCapture: a microphone/interface, an app's audio, or everything
  playing on an output (sink monitor). Uses pipewiresrc directly.
* UrlCapture: an internet stream (Icecast/HTTP radio, HLS, RTSP...).
* SrtCapture: a stream sent from OBS or ffmpeg over SRT (optionally encrypted).
* ToneCapture: a test tone for checking the setup.
"""

import threading
import time
from collections import deque

import numpy as np

from . import SAMPLE_RATE
from .gst import Gst, drain_bus, link_many, make, pw_props, raw_caps
from .util import log


class BlockClock:
    """Estimates when the first sample of each block was captured.

    Buffers reach us with scheduling jitter (always late, never early), so the
    capture time is the lower envelope of (arrival time - sample position),
    taken over a sliding two-second window. The window lets the estimate
    follow clock drift and recover from bursts of queued data.
    """

    WINDOW_NS = 2_000_000_000

    def __init__(self, rate=SAMPLE_RATE):
        self.rate = rate
        self.n = 0
        self._q = deque()
        self.origin = None

    def update(self, frames, arrival_ns):
        self.n += frames
        off = arrival_ns - self.n * 1e9 / self.rate
        q = self._q
        while q and q[-1][1] >= off:
            q.pop()
        q.append((arrival_ns, off))
        while q[0][0] < arrival_ns - self.WINDOW_NS:
            q.popleft()
        self.origin = q[0][1]
        return int(self.origin + (self.n - frames) * 1e9 / self.rate)


class Capture:
    graph_synced = False   # True when PipeWire keeps us sample-locked to the other inputs
    waiting_status = "offline"

    def __init__(self, key, channels, label, on_block, on_status):
        self.key = key
        self.channels = channels
        self.label = label
        self.on_block = on_block
        self.on_status = on_status
        self.pipeline = None
        self.status = "stopped"
        self.message = ""
        self.generation = 0
        self.clock = BlockClock()
        self.last_data_ns = 0
        self.retry_at = None
        self.lock = threading.RLock()
        self._stopped = False

    # ---- status
    def _set_status(self, status, message=""):
        if (status, message) != (self.status, self.message):
            self.status, self.message = status, message
            try:
                self.on_status(self)
            except Exception:
                log.exception("status callback failed")

    def info(self):
        return {"status": self.status, "message": self.message}

    # ---- lifecycle
    def build(self):
        raise NotImplementedError

    def should_run(self):
        return not self._stopped

    def start(self):
        with self.lock:
            self._stopped = False
            self._stop_pipeline()
            try:
                p = self.build()
            except Exception as e:
                log.warning("could not build input %s: %s", self.label, e)
                self._set_status("error", str(e))
                self.retry_at = time.monotonic() + 5
                return
            sink = p.get_by_name("sink")
            sink.connect("new-sample", self._on_sample)
            self.generation += 1
            self.clock = BlockClock()
            self.pipeline = p
            self.retry_at = None
            self._set_status("starting", self.start_message())
            if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._stop_pipeline()
                self._set_status("error", "could not start")
                self.retry_at = time.monotonic() + 3

    def start_message(self):
        return ""

    def _stop_pipeline(self):
        p, self.pipeline = self.pipeline, None
        if p is not None:
            p.set_state(Gst.State.NULL)

    def stop(self):
        with self.lock:
            self._stopped = True
            self.retry_at = None
            self._stop_pipeline()
            self._set_status("stopped")

    # ---- data
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = np.frombuffer(buf.extract_dup(0, buf.get_size()), dtype=np.float32)
        ch = self.channels
        n = data.size // ch
        if n == 0:
            return Gst.FlowReturn.OK
        data = data[:n * ch].reshape(n, ch)
        now = time.monotonic_ns()
        t0 = self.clock.update(n, now)
        self.last_data_ns = now
        if self.status != "live":
            self._set_status("live")
        try:
            self.on_block(self, data, t0)
        except Exception:
            log.exception("input block handler failed")
        return Gst.FlowReturn.OK

    # ---- housekeeping (called regularly from the engine loop)
    def poll(self):
        with self.lock:
            p = self.pipeline
            if p is not None:
                for m in drain_bus(p):
                    self.handle_message(m)
            if self.retry_at is not None and time.monotonic() >= self.retry_at and self.should_run():
                self.retry_at = None
                self.start()

    def handle_message(self, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            log.warning("input %s: %s (%s)", self.label, err.message, dbg)
            self._stop_pipeline()
            self._set_status("error", err.message)
            self.retry_at = time.monotonic() + 3
        elif m.type == Gst.MessageType.EOS:
            self._stop_pipeline()
            self._set_status(self.waiting_status, "stream ended, reconnecting")
            self.retry_at = time.monotonic() + 2

    def refresh(self, graph):
        """React to a PipeWire graph change."""

    def _appsink_tail(self, pipeline, sync=False):
        conv = make("audioconvert")
        res = make("audioresample")
        caps = make("capsfilter", caps=raw_caps(self.channels))
        sink = make("appsink", "sink", emit_signals=True, sync=sync, max_buffers=64, drop=False,
                    caps=raw_caps(self.channels))
        for e in (conv, res, caps, sink):
            pipeline.add(e)
        link_many(conv, res, caps, sink)
        return conv


class PipeWireCapture(Capture):
    graph_synced = True
    _counter = 0

    def __init__(self, key, channels, label, on_block, on_status, resolver, capture_sink=False,
                 waiting_status="offline", waiting_message="not connected"):
        super().__init__(key, channels, label, on_block, on_status)
        self.resolver = resolver
        self.capture_sink = capture_sink
        self.target = None
        self.waiting_status = waiting_status
        self.waiting_message = waiting_message
        PipeWireCapture._counter += 1
        self.node_name = f"audiomagic.in.{PipeWireCapture._counter}"

    def should_run(self):
        return super().should_run() and self.target is not None

    def refresh(self, graph):
        if not graph.ok:
            return
        target = self.resolver(graph)
        with self.lock:
            if self._stopped:
                return
            if target is None:
                self.target = None
                self._stop_pipeline()
                self.retry_at = None
                self._set_status(self.waiting_status, self.waiting_message)
            elif target != self.target or (self.pipeline is None and self.retry_at is None):
                self.target = target
                self.start()

    def start(self):
        if self.target is None:
            self._set_status(self.waiting_status, self.waiting_message)
            return
        super().start()

    def build(self):
        p = Gst.Pipeline.new(self.node_name)
        props = {
            "media.type": "Audio",
            "media.category": "Capture",
            "media.role": "Production",
            "node.name": self.node_name,
            "node.description": f"AudioMagic: {self.label}",
            "node.dont-reconnect": True,
            "stream.dont-remix": True,
        }
        if self.capture_sink:
            props["stream.capture.sink"] = True
        src = make("pipewiresrc", target_object=str(self.target), client_name="AudioMagic")
        src.set_property("stream-properties", pw_props(props))
        caps = make("capsfilter", caps=raw_caps(self.channels))
        p.add(src)
        p.add(caps)
        link_many(src, caps, self._appsink_tail(p))
        return p


class _DecodingCapture(Capture):
    """Shared plumbing for inputs that go through a decodebin."""

    graph_synced = False
    waiting_status = "waiting"

    def _link_decoded(self, pad, conv):
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if not name.startswith("audio/"):
            return
        sinkpad = conv.get_static_pad("sink")
        if not sinkpad.is_linked():
            pad.link(sinkpad)


class UrlCapture(_DecodingCapture):
    SCHEMES = ("http", "https", "rtsp", "rtmp", "rtmps", "file")

    def __init__(self, key, url, label, on_block, on_status):
        super().__init__(key, 2, label, on_block, on_status)
        self.url = url
        self._buffering = False

    def start_message(self):
        return "connecting"

    def build(self):
        p = Gst.Pipeline.new("url-input")
        p.use_clock(Gst.SystemClock.obtain())
        src = make("uridecodebin", uri=self.url, use_buffering=True)
        p.add(src)
        conv = self._appsink_tail(p, sync=True)
        src.connect("pad-added", lambda el, pad: self._link_decoded(pad, conv))
        return p

    def handle_message(self, m):
        if m.type == Gst.MessageType.BUFFERING and self.pipeline is not None:
            pct = m.parse_buffering()
            if pct < 100 and not self._buffering:
                self._buffering = True
                self.pipeline.set_state(Gst.State.PAUSED)
                self._set_status("waiting", "buffering")
            elif pct >= 100 and self._buffering:
                self._buffering = False
                self.pipeline.set_state(Gst.State.PLAYING)
            return
        super().handle_message(m)


class SrtCapture(_DecodingCapture):
    def __init__(self, key, port, passphrase, lan, latency_ms, label, on_block, on_status):
        super().__init__(key, 2, label, on_block, on_status)
        self.port = int(port)
        self.passphrase = passphrase or ""
        self.lan = bool(lan)
        self.latency_ms = int(latency_ms or 200)

    def start_message(self):
        return "waiting for a sender"

    def build(self):
        p = Gst.Pipeline.new("srt-input")
        addr = "0.0.0.0" if self.lan else "127.0.0.1"
        src = make("srtsrc", uri=f"srt://{addr}:{self.port}?mode=listener", latency=self.latency_ms,
                   keep_listening=True)
        if self.passphrase:
            src.set_property("passphrase", self.passphrase)
            src.set_property("pbkeylen", 16)
        dec = make("decodebin")
        p.add(src)
        p.add(dec)
        src.link(dec)
        conv = self._appsink_tail(p, sync=False)
        dec.connect("pad-added", lambda el, pad: self._link_decoded(pad, conv))
        return p


class ToneCapture(Capture):
    def __init__(self, key, freq, label, on_block, on_status):
        super().__init__(key, 1, label, on_block, on_status)
        self.freq = float(freq or 440)

    def build(self):
        p = Gst.Pipeline.new("tone")
        src = make("audiotestsrc", is_live=True, freq=self.freq, volume=0.25, samplesperbuffer=512)
        p.add(src)
        link_many(src, self._appsink_tail(p))
        return p
