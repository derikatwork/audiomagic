"""Fake PipeWire devices for the load test (run as its own process).

The signals are rendered to files and played with pw-cat: PipeWire pulls file
playback on demand, so these sources never underrun even on a busy machine.

* am-load-iface: an 8-input virtual interface (AUX0..AUX7, like a pro-audio
  interface). Inputs 1-7 carry steady sines (200..800 Hz); input 8 carries a
  click every 0.5 s.
* AmLoadApp: a program playing the same clicks.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audiomagic.wavio import WavWriter  # noqa: E402

RATE = 48000
SECONDS = 90
AUX = [f"AUX{k}" for k in range(8)]


def clicks(n0, n):
    k = np.arange(n0, n0 + n)
    return np.where(k % 24000 < 480, 0.8 * np.sin(2 * np.pi * 2000 * k / RATE), 0.0)


d = tempfile.mkdtemp(prefix="am-load-")
iface = WavWriter(f"{d}/iface.wav", 8, bits=16, sync_interval=600)
app = WavWriter(f"{d}/app.wav", 2, bits=16, sync_interval=600)
for n0 in range(0, SECONDS * RATE, RATE * 10):
    t = np.arange(n0, n0 + RATE * 10) / RATE
    c = clicks(n0, RATE * 10)
    iface.write(np.stack([0.3 * np.sin(2 * np.pi * (200 + 100 * k) * t) for k in range(7)] + [c], 1).astype(np.float32))
    app.write(np.stack([c, c], 1).astype(np.float32))
iface.close()
app.close()

sink_owner = subprocess.Popen(["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
sink_owner.stdin.write("create-node adapter { factory.name=support.null-audio-sink node.name=am-load-out "
                       "media.class=Audio/Sink audio.position=[ FL FR ] }\n")
sink_owner.stdin.flush()  # the sink lives as long as this pw-cli session
procs = [
    sink_owner,
    subprocess.Popen(["pw-loopback", "-c", "8", "-m", "[ " + " ".join(AUX) + " ]",
                      "--capture-props=media.class=Audio/Sink node.name=am-load-bus",
                      "--playback-props=media.class=Audio/Source node.name=am-load-iface node.description=LoadTestInterface"]),
]
time.sleep(1.5)
procs.append(subprocess.Popen(["pw-cat", "--playback", "--target", "am-load-bus", "--channels", "8",
                               "--channel-map", ",".join(AUX), f"{d}/iface.wav"]))
procs.append(subprocess.Popen(["pw-cat", "--playback", "--target", "am-load-out", "-P",
                               "{ application.name = AmLoadApp node.name = AmLoadApp }", f"{d}/app.wav"]))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the cleanup below runs
print("ready", flush=True)
try:
    procs[2].wait()
finally:
    for p in procs:
        p.terminate()
    shutil.rmtree(d, ignore_errors=True)
