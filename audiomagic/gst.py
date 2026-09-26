"""GStreamer setup and small helpers."""

import ctypes
import os

# libgstreamer pulls in libunwind, whose _Unwind_* symbols would otherwise
# win over libgcc's for C++ plugins loaded later (libsrt). Exceptions that
# libsrt throws and catches internally then abort the whole process. Loading
# libgcc_s globally first keeps C++ exception handling on the normal path.
try:
    ctypes.CDLL("libgcc_s.so.1", mode=os.RTLD_NOW | os.RTLD_GLOBAL)
except OSError:
    pass

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstAudio", "1.0")
from gi.repository import Gst, GstAudio  # noqa: E402

Gst.init(None)

from . import SAMPLE_RATE  # noqa: E402


def make(factory, name=None, **props):
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(f"GStreamer element '{factory}' is missing (check the gstreamer1.0 plugin packages)")
    for k, v in props.items():
        el.set_property(k.replace("_", "-"), v)
    return el


def has_element(factory):
    return Gst.ElementFactory.find(factory) is not None


def raw_caps(channels, rate=SAMPLE_RATE):
    s = f"audio/x-raw,format=F32LE,layout=interleaved,rate={rate},channels={channels}"
    if channels > 2:
        s += ",channel-mask=(bitmask)0x0"
    return Gst.Caps.from_string(s)


def pw_props(values):
    """A GstStructure for pipewiresrc/pipewiresink ``stream-properties``."""
    st = Gst.Structure.new_empty("props")
    for k, v in values.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        st.set_value(k, str(v))
    return st


def link_many(*els):
    for a, b in zip(els, els[1:]):
        if not a.link(b):
            raise RuntimeError(f"could not link {a.get_name()} -> {b.get_name()}")


def drain_bus(pipeline):
    """Pop every pending message from a pipeline's bus."""
    bus = pipeline.get_bus()
    out = []
    while True:
        m = bus.pop()
        if m is None:
            return out
        out.append(m)
