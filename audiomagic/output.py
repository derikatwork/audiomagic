"""Audio output to PipeWire (live monitoring and playback)."""

import threading

import numpy as np

from . import SAMPLE_RATE
from .gst import Gst, drain_bus, link_many, make, pw_props, raw_caps
from .util import log


class OutputStream:
    """appsrc -> pipewiresink. ``live`` streams push as data arrives (monitoring);
    non-live streams are paced by the sink clock (playback)."""

    _counter = 0

    def __init__(self, label, channels=2, live=True, target=None, latency_frames=None):
        OutputStream._counter += 1
        self.label = label
        self.channels = channels
        self.live = live
        self.frames_pushed = 0
        self.error = None
        self._lock = threading.Lock()
        name = f"audiomagic.out.{OutputStream._counter}"
        p = Gst.Pipeline.new(name)
        src = make("appsrc", "src", format=Gst.Format.TIME, is_live=live, caps=raw_caps(channels))
        bytes_per_sec = SAMPLE_RATE * channels * 4
        if live:
            src.set_property("do-timestamp", True)
            src.set_property("max-bytes", bytes_per_sec // 8)
            src.set_property("leaky-type", 2)  # drop the oldest data if the sink falls behind
        else:
            src.set_property("block", True)
            src.set_property("max-bytes", bytes_per_sec // 4)
        conv = make("audioconvert")
        res = make("audioresample")
        sink = make("pipewiresink", "sink", sync=not live, client_name="AudioMagic")
        props = {
            "media.type": "Audio",
            "media.category": "Playback",
            "media.role": "Production",
            "node.name": name,
            "node.description": f"AudioMagic: {label}",
        }
        if latency_frames:
            props["node.latency"] = f"{latency_frames}/{SAMPLE_RATE}"
        sink.set_property("stream-properties", pw_props(props))
        if target:
            sink.set_property("target-object", target)
        for e in (src, conv, res, sink):
            p.add(e)
        link_many(src, conv, res, sink)
        self.pipeline = p
        self.src = src

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)

    def resume(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def push(self, block):
        block = np.ascontiguousarray(block, dtype=np.float32)
        n = block.shape[0]
        if n == 0:
            return True
        buf = Gst.Buffer.new_wrapped(block.tobytes())
        if not self.live:
            buf.pts = self.frames_pushed * Gst.SECOND // SAMPLE_RATE
            buf.duration = n * Gst.SECOND // SAMPLE_RATE
        self.frames_pushed += n
        return self.src.emit("push-buffer", buf) == Gst.FlowReturn.OK

    def end(self):
        self.src.emit("end-of-stream")

    def position_frames(self):
        """Frames actually played so far (non-live streams)."""
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if not ok or pos < 0:
            return None
        return int(pos * SAMPLE_RATE // Gst.SECOND)

    def poll(self):
        """Returns 'eos', 'error' or None."""
        result = None
        for m in drain_bus(self.pipeline):
            if m.type == Gst.MessageType.EOS:
                result = "eos"
            elif m.type == Gst.MessageType.ERROR:
                err, dbg = m.parse_error()
                log.warning("output %s: %s (%s)", self.label, err.message, dbg)
                self.error = err.message
                result = "error"
        return result

    def stop(self):
        with self._lock:
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
