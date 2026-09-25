"""Playing a take through PipeWire, with meters that follow what you hear."""

import threading
from collections import deque

from .dsp import Meter
from .output import OutputStream
from .render import TakeRenderer
from .util import log


class Player:
    BLOCK = 2048

    def __init__(self):
        self.lock = threading.RLock()
        self.state = "stopped"      # playing | paused | stopped
        self.take_id = None
        self.start_pos = 0
        self.stop_pos = 0
        self.renderer = None
        self.out = None
        self.thread = None
        self._halt = threading.Event()
        self._meters = deque()
        self._last = ({}, None)

    # ---- control
    def play(self, project, take, pos, include, master, target=None):
        self.stop()
        with self.lock:
            renderer = TakeRenderer(project, take, start=pos, include=include, master=master)
            if renderer.done:
                return False
            self.renderer = renderer
            self.take_id = take.id
            self.start_pos = renderer.pos
            self.stop_pos = renderer.pos
            self.out = OutputStream("Playback", 2, live=False, target=target)
            self._halt.clear()
            self._meters.clear()
            self.state = "playing"
            self.thread = threading.Thread(target=self._run, args=(renderer, self.out), name="playback", daemon=True)
            self.out.start()
            self.thread.start()
            return True

    def _run(self, renderer, out):
        master_meter = Meter()
        try:
            while not self._halt.is_set():
                r = renderer.next(self.BLOCK)
                if r is None:
                    out.end()
                    break
                mix, _ = r
                master_meter.update(mix)
                tracks = {it.track.id: it.meter.read() for it in renderer.items}
                self._meters.append((renderer.pos, tracks, master_meter.read()))
                if not out.push(mix):
                    break
        except Exception:
            log.exception("playback failed")
            out.end()

    def pause(self):
        with self.lock:
            if self.state == "playing":
                self.out.pause()
                self.state = "paused"

    def resume(self):
        with self.lock:
            if self.state == "paused":
                self.out.resume()
                self.state = "playing"

    def stop(self):
        with self.lock:
            if self.state == "stopped" and self.out is None:
                return
            self.stop_pos = self.position() or self.start_pos
            self._halt.set()
            if self.out is not None:
                self.out.stop()
            if self.thread is not None:
                self.thread.join(timeout=2)
            self.out = None
            self.thread = None
            self.renderer = None
            self.state = "stopped"

    # ---- status
    def position(self):
        """Current playback position on the edited timeline (frames)."""
        out = self.out
        if out is None:
            return None
        played = out.position_frames()
        if played is None:
            return self.start_pos
        pos = self.start_pos + played
        if self.renderer is not None:
            pos = min(pos, self.renderer.length)
        return pos

    def poll(self):
        """Returns 'ended' when playback reached the end."""
        with self.lock:
            if self.out is None:
                return None
            r = self.out.poll()
            if r in ("eos", "error"):
                end = self.renderer.length if (self.renderer and r == "eos") else self.position()
                self.stop()
                self.stop_pos = end or 0
                return "ended"
        return None

    def meters(self):
        """Meter values for the audio being heard right now."""
        pos = self.position()
        if pos is None:
            return self._last
        tracks, master = {}, None
        while self._meters and self._meters[0][0] <= pos + 1024:
            _, t, m = self._meters.popleft()
            for k, v in t.items():
                old = tracks.get(k)
                tracks[k] = v if old is None else (max(old[0], v[0]), max(old[1], v[1]), old[2] or v[2])
            master = m if master is None else (max(master[0], m[0]), max(master[1], m[1]), master[2] or m[2])
        if tracks or master:
            self._last = (tracks, master)
        return self._last

