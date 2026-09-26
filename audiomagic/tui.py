"""AudioMagic in a terminal: ``audiomagic --tui``.

The same engine as the app window runs in this process, driven from the
keyboard: inputs and their mixer, effects, recording, takes, playback, editing
on a text waveform, and export. Press ? inside for the keys.
"""

import bisect
import functools
import logging
import math
import os
import queue
import secrets
import sys
import threading
import time

import numpy as np
from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import (Button, Checkbox, Footer, Input, Label, OptionList, ProgressBar, Select, SelectionList,
                             Static, TabbedContent, TabPane)
from textual.widgets.option_list import Option

from . import SAMPLE_RATE, __version__
from . import export as export_mod
from .engine import NeedsConfirm, UserError
from .peaks import BIN
from .util import log

R = SAMPLE_RATE
VBLOCKS = " ▁▂▃▄▅▆▇█"
HBLOCKS = " ▏▎▍▌▋▊▉█"
UPPER_BLOCKS = {1: "▔", 4: "▀", 8: "█"}  # the upper blocks that exist as characters

# a fixed dark palette, so colours look the same in every terminal
RED, YELLOW, GREEN, BLUE, CYAN = "#ef4444", "#eab308", "#22c55e", "#3b82f6", "#22d3ee"
DIM = "#6b7280"
QUIET = "#3f4652"
METER_BG = "#262a33"
ROW_FOCUS, ROW_BLUR = "#1e3a5f", "#252a33"
SELECTION = "#1f4d7a"
CURSOR_FG, CURSOR_BG = "#111111", "#d1d5db"
PLAY_FG, PLAY_BG = "#bbf7d0", "#15803d"
WAVE_BG = "#0f1117"

METER_FLOOR = -60.0
METER_FALL = 1.2        # dB per frame (25 fps), so peaks stay readable
CLIP_HOLD = 2.0         # seconds a clip warning stays lit

SEVERITY = {"info": "information", "warn": "warning", "error": "error"}
PEAK_LOADS = threading.BoundedSemaphore(2)  # waveforms read at once (as the local server does)
FX_ORDER = (("ns", "NS"), ("gate", "GT"), ("eq", "EQ"))


@functools.lru_cache(maxsize=4096)
def style(fg=None, bg=None, bold=False, dim=False):
    return Style(color=fg, bgcolor=bg, bold=bold or None, dim=dim or None)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def clean(text):
    """Names come from programs and users: keep them on one line."""
    return "".join(c if c >= " " else " " for c in str(text or ""))


def fmt_time(sec, decimals=0):
    sec = max(0.0, float(sec or 0.0))
    if decimals:
        scale = 10 ** decimals
        total = int(round(sec * scale))
        whole, frac = divmod(total, scale)
    else:
        whole, frac = int(sec), None
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    if frac is not None:
        out += f".{frac:0{decimals}d}"
    return out


def fmt_pan(p):
    if abs(p) < 0.005:
        return "C"
    return f"{'L' if p < 0 else 'R'}{round(abs(p) * 100)}"


def fmt_bytes_time(free, per_sec):
    if free is None:
        return ""
    if per_sec:
        mins = free / per_sec / 60
        if mins < 90:
            return f"{mins:.0f} min of space"
        return f"{mins / 60:.0f} h of space"
    return f"{free / 1e9:.0f} GB free"


class Line:
    """One screen line, built from styled runs and padded or cut to a width."""

    def __init__(self, width, base):
        self.width = width
        self.base = base
        self.segments = []
        self.used = 0

    def add(self, text, st=None, width=None):
        if width is not None:
            text = set_cell_size(text, width)
        room = self.width - self.used
        if room <= 0 or not text:
            return self
        n = cell_len(text)
        if n > room:
            text = set_cell_size(text, room)
            n = room
        self.segments.append(Segment(text, self.base + st if st else self.base))
        self.used += n
        return self

    def runs(self, chars, styles):
        """Adds one character per cell, merging neighbours that share a style."""
        start = 0
        for i in range(1, len(chars) + 1):
            if i == len(chars) or styles[i] is not styles[start]:
                self.add("".join(chars[start:i]), styles[start])
                start = i
        return self

    def strip(self):
        if self.used < self.width:
            self.segments.append(Segment(" " * (self.width - self.used), self.base))
        return Strip(self.segments, self.width)


def meter_runs(db, width, bg=METER_BG):
    """A horizontal level meter from -60 to 0 dB, one character per cell."""
    cells = clamp((db - METER_FLOOR) / -METER_FLOOR, 0.0, 1.0) * width
    full = int(cells)
    part = int((cells - full) * 8)
    chars, styles = [], []
    for i in range(width):
        cell_db = METER_FLOOR + (i + 0.5) / width * -METER_FLOOR
        color = GREEN if cell_db < -18 else YELLOW if cell_db < -6 else RED
        chars.append("█" if i < full else HBLOCKS[part] if i == full else " ")
        styles.append(style(color, bg))
    return chars, styles


def amplitude(data):
    """Peak files hold (min, max) int8 pairs; the waveform shows the larger of the two."""
    arr = np.frombuffer(data, np.int8) if isinstance(data, (bytes, bytearray)) else np.asarray(data, np.int8)
    arr = arr[:arr.size // 2 * 2].reshape(-1, 2).astype(np.int16)
    return np.maximum(np.abs(arr[:, 0]), np.abs(arr[:, 1])).astype(np.uint8)


def envelope(amp, segments, t0, spc, ncols):
    """The waveform as it plays after edits, one value per screen column.

    Returns (level 0..127, source frame at the column's middle or -1 past the end).
    """
    levels = np.zeros(ncols, np.float32)
    src_mid = np.full(ncols, -1.0)
    if not segments or amp is None:
        return levels, src_mid
    starts = [s[2] for s in segments]
    nb = amp.shape[0]
    for col in range(ncols):
        oa = (t0 + col * spc) * R
        ob = oa + spc * R
        i = max(0, bisect.bisect_right(starts, oa) - 1)
        best = 0
        while i < len(segments):
            a, b, o = segments[i]
            if o >= ob:
                break
            eo = o + (b - a)
            if eo > oa:
                lo, hi = max(oa, o), min(ob, eo)
                sa, sb = a + lo - o, a + hi - o
                if src_mid[col] < 0:
                    src_mid[col] = (sa + sb) / 2
                i0 = int(sa // BIN)
                i1 = min(nb, max(i0 + 1, int(math.ceil(sb / BIN))))
                if i1 > i0:
                    best = max(best, int(amp[i0:i1].max()))
            i += 1
        levels[col] = best
    return levels, src_mid


class Deliver(Message):
    """Runs a function on the interface's thread (posted from other threads)."""

    def __init__(self, fn, args, kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs


class Bridge:
    """Runs engine calls one at a time on their own thread, as the local server
    does, so a slow one (stopping a recording, normalizing) never freezes the
    screen. Also does the engine's housekeeping ten times a second."""

    def __init__(self, app, engine):
        self.app = app
        self.engine = engine
        self.q = queue.Queue()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)
        # calls are numbered, and every state says which calls it includes
        self.submitted = 0   # interface thread
        self.done = 0        # engine thread

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)
        if self.thread.is_alive():
            self.thread.join(timeout=15)

    def call(self, fn, *args, done=None, failed=None, retry=None, retry_label="Continue"):
        """Run fn(*args) on the engine thread. done(result) runs on the interface
        afterwards, or failed() if it went wrong (the user is told why). If the
        engine asks for confirmation, the user is asked and retry() runs if they agree."""
        self.submitted += 1
        self.q.put((self.submitted, fn, args, done, failed, retry, retry_label))

    def post(self, fn, *args, **kwargs):
        try:
            self.app.post_message(Deliver(fn, args, kwargs))
        except RuntimeError:
            pass  # the interface has closed

    def _run(self):
        next_poll = 0.0
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=0.05)
            except queue.Empty:
                item = None
            if item is not None:
                self._do(*item)
            now = time.monotonic()
            if now >= next_poll and not self._stop.is_set():
                next_poll = now + 0.1
                try:
                    self.engine.poll()
                    if self.engine.take_dirty():
                        self.post(self.app.apply_state, self.engine.state(), self.done)
                except Exception:
                    log.exception("housekeeping failed")

    def _do(self, seq, fn, args, done, failed, retry, retry_label):
        try:
            result = fn(*args)
        except NeedsConfirm as e:
            if retry is not None:
                self.post(self.app.ask, str(e), retry, yes=retry_label, danger=True)
        except Exception as e:
            if isinstance(e, UserError):
                self.post(self.app.notify, str(e), severity="error", timeout=6)
            else:
                log.exception("%s failed", getattr(fn, "__name__", fn))
                self.post(self.app.notify, f"Something went wrong: {e}", severity="error", timeout=8)
            if failed is not None:
                self.post(failed)
        else:
            if done is not None:
                self.post(done, result)
        # always follow up with the engine's view, which also undoes an
        # optimistic change the engine refused
        self.done = seq
        try:
            self.post(self.app.apply_state, self.engine.state(), seq)
        except Exception:
            log.exception("reading the state failed")


# ------------------------------------------------------------------ main view

class TopBar(Widget):
    """Project, transport clock, output and disk space."""

    DEFAULT_CSS = "TopBar { height: 1; background: #1f2430; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._clock = ""

    def set_clock(self, meters, state):
        text = self._clock_text(meters, state)
        if text != self._clock:
            self._clock = text
            self.refresh()

    @staticmethod
    def _clock_text(m, st):
        if not st:
            return ""
        tr = st["transport"]
        if tr["recording"]:
            return f"● REC {fmt_time((m or {}).get('rec') or 0)}"
        if tr["playing"] or tr["paused"]:
            pos = (m or {}).get("pos")
            take = next((t for t in st["takes"] if t["id"] == tr["play_take"]), None)
            length = take["timeline"]["length"] / R if take else 0
            icon = "▶" if tr["playing"] else "❚❚"
            return f"{icon} {fmt_time(pos if pos is not None else tr['stop_pos'], 1)} / {fmt_time(length)}"
        return "■ Stopped"

    def render(self):
        st = self.app.state
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" AudioMagic ", "bold #ffffff on #6d28d9")
        if not st:
            t.append("  starting…", DIM)
            return t
        t.append(f"  {clean(st['project']['name'])}  ", "bold")
        tr = st["transport"]
        clock = self._clock or self._clock_text(None, st)
        if tr["recording"]:
            t.append(f" {clock} ", "bold #ffffff on #b91c1c")
        elif tr["playing"]:
            t.append(f" {clock} ", f"bold {GREEN}")
        elif tr["paused"]:
            t.append(f" {clock} ", f"bold {YELLOW}")
        else:
            t.append(f" {clock} ", DIM)
        out = st.get("output")
        label = next((o["label"] for o in st.get("outputs", []) if o["node"] == out), None) if out else None
        t.append(f"  Output: {clean(label or 'system default')}", DIM)
        disk = st.get("disk") or {}
        space = fmt_bytes_time(disk.get("free"), disk.get("bytes_per_sec"))
        if space:
            low = disk.get("bytes_per_sec") and disk.get("free") is not None and \
                disk["free"] < disk["bytes_per_sec"] * 1800
            t.append(f"  ·  {space}", f"bold {RED}" if low else DIM)
        if not st["pipewire"]["ok"]:
            t.append(f"  PipeWire: {st['pipewire']['error'] or 'not available'}", f"bold {RED}")
        if st.get("effects_overloaded"):
            t.append("  Effects can't keep up", f"bold {YELLOW}")
        t.append("   ? help", DIM)
        return t


class TrackList(Widget, can_focus=True):
    """The inputs with their mixer controls and meters; the last row is the master."""

    BINDINGS = [
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("a", "toggle('armed')", "Arm"),
        Binding("m", "toggle('mute')", "Mute"),
        Binding("s", "toggle('solo')", "Solo"),
        Binding("h", "toggle('monitor')", "Hear"),
        Binding("plus,equals_sign", "gain(1)", "Level +"),
        Binding("minus", "gain(-1)", "Level −"),
        Binding("0", "gain_reset", "0 dB", show=False),
        Binding("left", "pan(-0.1)", "Pan", show=False),
        Binding("right", "pan(0.1)", "Pan", show=False),
        Binding("e", "effects", "Effects"),
        Binding("c", "change", "Change input", show=False),
        Binding("n", "rename", "Rename", show=False),
        Binding("delete", "remove", "Remove", show=False),
        Binding("shift+up", "move(-1)", "Move up", show=False),
        Binding("shift+down", "move(1)", "Move down", show=False),
    ]

    DEFAULT_CSS = """
    TrackList { height: auto; max-height: 45%; border: round #3a4152; padding: 0 0; }
    TrackList:focus { border: round #60a5fa; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.index = 0
        self.top = 0
        self.levels = {}      # track id (or "master") -> displayed dB
        self.clips = {}       # track id -> time the clip light goes out
        self._sig = None

    def on_mount(self):
        self.border_title = "Inputs"

    # ---- data
    @property
    def tracks(self):
        return self.app.tracks

    def selected(self):
        tracks = self.tracks
        return tracks[self.index] if 0 <= self.index < len(tracks) else None

    def on_state(self):
        self.index = int(clamp(self.index, 0, len(self.tracks)))
        self._scroll_into_view()
        self.refresh(layout=True)

    def on_resize(self):
        self._scroll_into_view()

    def set_meters(self, m):
        now = time.monotonic()
        values = dict(m.get("tracks") or {})
        values["master"] = m.get("master")
        for key in set(values) | set(self.levels):
            v = values.get(key)
            peak = v[0] if v else -120.0
            if v and v[2]:
                self.clips[key] = now + CLIP_HOLD
            old = self.levels.get(key, -120.0)
            self.levels[key] = max(peak, old - METER_FALL) if old > METER_FLOOR else peak
        sig = tuple(sorted((k, round(v * 2), self.clips.get(k, 0) > now) for k, v in self.levels.items()))
        if sig != self._sig:
            self._sig = sig
            self.refresh()

    # ---- layout
    def get_content_height(self, container, viewport, width):
        return max(2, len(self.tracks) + 1)

    def _widths(self, width):
        fixed = 2 + 8 + 18 + 1 + 1 + 5 + 9 + 5 + 9
        meter = int(clamp(width - fixed - 14, 8, 36))
        return meter

    def _scroll_into_view(self):
        h = max(1, self.size.height)
        rows = len(self.tracks) + 1
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + h:
            self.top = self.index - h + 1
        self.top = int(clamp(self.top, 0, max(0, rows - h)))
        below = max(0, rows - self.top - h)
        hint = " ".join(x for x in (f"↑ {self.top} more" if self.top else "",
                                     f"↓ {below} more" if below else "") if x)
        if hint != (self.border_subtitle or ""):
            self.border_subtitle = hint or None

    def render_line(self, y):
        width = self.size.width
        tracks = self.tracks
        idx = y + self.top
        base = self.rich_style
        if self.app.state is None:
            return Line(width, base).add("  Starting…", style(DIM)).strip()
        if idx > len(tracks):
            return Line(width, base).strip()
        if idx == self.index:
            base = base + style(bg=ROW_FOCUS if self.has_focus else ROW_BLUR)
        line = Line(width, base)
        line.add("› " if idx == self.index else "  ", style(CYAN, bold=True))
        meter_w = self._widths(width)
        now = time.monotonic()
        if idx == len(tracks):
            return self._master_line(line, meter_w, now, empty=not tracks)
        t = tracks[idx]
        for key, letter, color in (("armed", "R", RED), ("mute", "M", YELLOW), ("solo", "S", GREEN),
                                   ("monitor", "H", BLUE)):
            if t[key]:
                line.add(letter, style("#ffffff" if key != "mute" else "#111111", color, bold=True))
            else:
                line.add(letter, style(QUIET))
            line.add(" ")
        line.add(clean(t["name"]), style(t.get("color"), bold=True), width=18)
        line.add(" ")
        db = self.levels.get(t["id"], -120.0)
        line.runs(*meter_runs(db, meter_w))
        line.add(" ")
        if self.clips.get(t["id"], 0) > now:
            line.add("CLIP ", style(RED, bold=True))
        else:
            line.add(f"{db:4.0f} " if db > METER_FLOOR else "   - ", style(DIM))
        line.add(f"{t['gain_db']:+5.1f} dB ", style(None if abs(t["gain_db"]) > 0.05 else DIM), width=9)
        line.add(fmt_pan(t["pan"]), style(None if abs(t["pan"]) > 0.005 else DIM), width=5)
        for group, label in FX_ORDER:
            on = t["fx"][group]["on"]
            line.add(label, style(CYAN if on else QUIET, bold=on))
            line.add(" ")
        if t.get("recording"):
            line.add("● rec  ", style(RED, bold=True))
        status, msg = t.get("status"), t.get("status_message") or ""
        if status == "live":
            line.add(msg or "live", style(GREEN))
        elif status in ("waiting", "starting"):
            line.add(msg or status, style(YELLOW))
        else:
            line.add(msg or status or "", style(RED))
        return line.strip()

    def _master_line(self, line, meter_w, now, empty):
        st = self.app.state
        line.add(" " * 8)
        line.add("Master", style(bold=True), width=18)
        line.add(" ")
        db = self.levels.get("master", -120.0)
        line.runs(*meter_runs(db, meter_w))
        line.add(" ")
        if self.clips.get("master", 0) > now:
            line.add("CLIP ", style(RED, bold=True))
        else:
            line.add(f"{db:4.0f} " if db > METER_FLOOR else "   - ", style(DIM))
        g = st["project"]["master_gain_db"]
        line.add(f"{g:+5.1f} dB ", style(None if abs(g) > 0.05 else DIM), width=9)
        if empty:
            line.add("  No inputs yet: press i to add one", style(YELLOW))
        else:
            line.add("  playback level", style(DIM))
        return line.strip()

    def on_mouse_down(self, event):
        off = event.get_content_offset(self)
        if off is not None:
            self.index = int(clamp(off.y + self.top, 0, len(self.tracks)))
            self.refresh()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    # ---- actions
    def _update(self, t, changes):
        t.update(changes)  # shown straight away; the engine's state follows
        self.refresh()
        self.app.bridge.call(self.app.engine.update_track, t["id"], changes)

    def action_cursor(self, step):
        self.index = int(clamp(self.index + step, 0, len(self.tracks)))
        self._scroll_into_view()
        self.refresh()

    def action_toggle(self, key):
        t = self.selected()
        if t is not None:
            self._update(t, {key: not t[key]})

    def action_gain(self, step):
        t = self.selected()
        st = self.app.state
        if t is not None:
            self._update(t, {"gain_db": round(clamp(t["gain_db"] + step, -60.0, 24.0), 1)})
        elif st is not None:
            g = round(clamp(st["project"]["master_gain_db"] + step, -60.0, 12.0), 1)
            st["project"]["master_gain_db"] = g
            self.refresh()
            self.app.bridge.call(self.app.engine.set_master, g)

    def action_gain_reset(self):
        t = self.selected()
        if t is not None:
            self._update(t, {"gain_db": 0.0})
        elif self.app.state is not None:
            self.app.state["project"]["master_gain_db"] = 0.0
            self.refresh()
            self.app.bridge.call(self.app.engine.set_master, 0.0)

    def action_pan(self, step):
        t = self.selected()
        if t is not None:
            self._update(t, {"pan": round(clamp(t["pan"] + step, -1.0, 1.0), 2)})

    def action_effects(self):
        t = self.selected()
        if t is not None:
            self.app.push_screen(EffectsScreen(t["id"]))

    def action_change(self):
        t = self.selected()
        if t is not None:
            self.app.push_screen(AddInputScreen(replace=t))

    def action_rename(self):
        t = self.selected()
        if t is None:
            return
        tid = t["id"]
        self.app.prompt("Rename input", t["name"],
                        lambda name: self.app.bridge.call(self.app.engine.update_track, tid, {"name": name}))

    def action_remove(self):
        t = self.selected()
        if t is None:
            return
        eng, tid, name = self.app.engine, t["id"], t["name"]
        self.app.bridge.call(eng.remove_track, tid, False,
                             done=lambda _: self.app.notify(f"Removed {clean(name)}"),
                             retry=lambda: self.app.bridge.call(eng.remove_track, tid, True),
                             retry_label="Remove")

    def action_move(self, step):
        t = self.selected()
        if t is None:
            return
        new = int(clamp(self.index + step, 0, len(self.tracks) - 1))
        if new != self.index:
            self.index = new
            self._scroll_into_view()
            self.app.bridge.call(self.app.engine.move_track, t["id"], new)


class Timeline(Widget, can_focus=True):
    """The selected take as a text waveform, with a cursor and a selection for editing."""

    BINDINGS = [
        Binding("left", "move(-1)", "Cursor", show=False),
        Binding("right", "move(1)", "Cursor", show=False),
        Binding("shift+left", "move(-1, True)", "Select", show=False),
        Binding("shift+right", "move(1, True)", "Select", show=False),
        Binding("pageup", "move_page(-1)", "Page", show=False),
        Binding("pagedown", "move_page(1)", "Page", show=False),
        Binding("shift+pageup", "move_page(-1, True)", "Select", show=False),
        Binding("shift+pagedown", "move_page(1, True)", "Select", show=False),
        Binding("home", "jump(0)", "Start", show=False),
        Binding("end", "jump(1)", "End", show=False),
        Binding("shift+home", "jump(0, True)", "Select", show=False),
        Binding("shift+end", "jump(1, True)", "Select", show=False),
        Binding("up", "row(-1)", "Track", show=False),
        Binding("down", "row(1)", "Track", show=False),
        Binding("a", "select_all", "Select all", show=False),
        Binding("escape", "clear", "Clear selection", show=False),
        Binding("c,delete", "edit('cut')", "Cut"),
        Binding("k", "edit('trim')", "Keep"),
        Binding("z", "edit('silence')", "Silence"),
        Binding("Z,shift+z", "edit('unsilence')", "Unsilence", show=False),
        Binding("N,shift+n", "normalize", "Normalize"),
        Binding("f", "fades", "Fades"),
        Binding("u", "undo", "Undo"),
        Binding("U,shift+u", "redo", "Redo"),
        Binding("plus,equals_sign", "zoom(0.5)", "Zoom in"),
        Binding("minus", "zoom(2)", "Zoom out"),
        Binding("0", "zoom_fit", "Fit", show=False),
        Binding("n", "rename_take", "Rename take", show=False),
        Binding("D,shift+d", "delete_take", "Delete take", show=False),
    ]

    DEFAULT_CSS = """
    Timeline { height: 1fr; min-height: 6; border: round #3a4152; background: #0f1117; }
    Timeline:focus { border: round #60a5fa; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.take_id = None
        self.view_start = 0.0
        self.zoom = None          # seconds per column; None fits the whole take
        self.cursor = 0.0
        self.anchor = None        # the other end of the selection
        self.row = 0              # which track row is picked (for silence and normalize)
        self.top_row = 0
        self.play_pos = None
        self.rec_pos = None
        self.peaks = {}           # (take, track) -> amplitude per bin
        self.loading = set()
        self.live = {}            # track -> [buffer, used] for the take being recorded
        self.live_take = None
        self._edit_keys = {}
        self._env = {}
        self._drag = False
        self._marks = None
        self._rec_shown = 0.0

    def on_mount(self):
        self.border_title = "Timeline"

    # ---- data
    def take(self):
        st = self.app.state
        if not st or self.take_id is None:
            return None
        return next((t for t in st["takes"] if t["id"] == self.take_id), None)

    def takes(self):
        st = self.app.state
        return st["takes"] if st else []

    def rows(self, take):
        if take is None:
            return []
        have = {f["track_id"] for f in take["tracks"]}
        return [t for t in self.app.tracks if t["id"] in have]

    def recording(self, take):
        return take is not None and take["state"] == "recording"

    def length(self, take):
        if take is None:
            return 0.0
        if self.recording(take):
            return float(self.rec_pos or 0.0)
        return take["timeline"]["length"] / R

    def show_take(self, take_id):
        if take_id != self.take_id:
            self.take_id = take_id
            self.zoom = None
            self.view_start = 0.0
            self.cursor = 0.0
            self.anchor = None
            self.top_row = 0
        self.refresh()

    def step_take(self, step):
        takes = self.takes()
        if not takes:
            return
        ids = [t["id"] for t in takes]
        i = ids.index(self.take_id) if self.take_id in ids else len(ids) - 1
        self.show_take(ids[int(clamp(i + step, 0, len(ids) - 1))])

    def on_state(self):
        st = self.app.state
        self._edit_keys = {t["id"]: repr((t["edits"], t["timeline"])) for t in st["takes"]}
        take = self.take()
        if take is not None:
            length = self.length(take)
            self.cursor = clamp(self.cursor, 0.0, length)
            if self.anchor is not None:
                self.anchor = clamp(self.anchor, 0.0, length)
            self.row = int(clamp(self.row, 0, max(0, len(self.rows(take)) - 1)))
        self.refresh()

    def selection(self):
        if self.anchor is None or abs(self.anchor - self.cursor) < 1e-6:
            return None
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def set_positions(self, pos, rec):
        st = self.app.state
        take = self.take()
        playing_here = bool(st and take and st["transport"]["play_take"] == take["id"])
        self.play_pos = pos if playing_here else None
        self.rec_pos = rec
        if self.recording(take):
            # the waveform redraws when new peaks arrive; this keeps the clock going
            if int(rec or 0) != int(self._rec_shown):
                self._rec_shown = rec or 0
                self.refresh()
            return
        if self.play_pos is not None and self.size.width:
            spc = self.spc(take)
            ww = self._wave_width()
            if not self.view_start <= self.play_pos < self.view_start + ww * spc:
                self.view_start = self._clamp_view(take, self.play_pos - ww * spc * 0.1)
        marks = (self._col(self.play_pos), self._col(self.cursor))
        if marks != self._marks:
            self._marks = marks
            self.refresh()

    def add_live(self, take_id, peaks):
        if take_id != self.live_take:
            self.live_take = take_id
            self.live = {}
        for tid, d in peaks.items():
            amp = amplitude(d["data"])
            start = int(d["start"])
            buf = self.live.get(tid)
            if buf is None:
                buf = self.live[tid] = [np.zeros(max(4096, start + amp.size), np.uint8), 0]
            end = start + amp.size
            if end > buf[0].size:
                grown = np.zeros(max(end, buf[0].size * 2), np.uint8)
                grown[:buf[1]] = buf[0][:buf[1]]
                buf[0] = grown
            buf[0][start:end] = amp
            buf[1] = max(buf[1], end)
        if self.take_id == take_id:
            self.refresh()

    def _amp(self, take, track_id):
        if self.recording(take):
            buf = self.live.get(track_id) if self.live_take == take["id"] else None
            return buf[0][:buf[1]] if buf else np.zeros(0, np.uint8)
        key = (take["id"], track_id)
        amp = self.peaks.get(key)
        if amp is None and key not in self.loading:
            self.loading.add(key)
            engine, app = self.app.engine, self.app

            def load():
                try:
                    with PEAK_LOADS:
                        data = engine.peaks(*key)
                except Exception as e:
                    log.warning("could not read the waveform of %s: %s", key, e)
                    data = b""
                app.post_message(Deliver(self._loaded, (key, amplitude(data)), {}))
            threading.Thread(target=load, name="peaks", daemon=True).start()
        return amp

    def _loaded(self, key, amp):
        self.loading.discard(key)
        self.peaks[key] = amp
        self.refresh()

    # ---- geometry
    def _gutter(self):
        return int(clamp(self.size.width // 6, 10, 18))

    def _wave_width(self):
        return max(1, self.size.width - self._gutter())

    def spc(self, take):
        """Seconds per screen column."""
        ww = self._wave_width()
        length = self.length(take)
        if self.recording(take):
            return max(length, 10.0) / ww
        fit = max(length, 0.5) / ww
        if self.zoom is None or self.zoom >= fit:
            return fit
        return max(self.zoom, BIN / R)

    def _clamp_view(self, take, start):
        span = self._wave_width() * self.spc(take)
        return clamp(start, 0.0, max(0.0, self.length(take) - span * 0.9))

    def _col(self, t):
        take = self.take()
        if t is None or take is None:
            return None
        spc = self.spc(take)
        if self.recording(take):
            return None
        return int(math.floor((t - self.view_start) / spc + 1e-9))

    def _time_at(self, col):
        take = self.take()
        return clamp(self.view_start + col * self.spc(take), 0.0, self.length(take))

    def _scroll_to(self, take, t):
        spc = self.spc(take)
        span = self._wave_width() * spc
        if t < self.view_start:
            self.view_start = self._clamp_view(take, t - span * 0.25)
        elif t >= self.view_start + span - spc:
            self.view_start = self._clamp_view(take, t - span * 0.75)

    def _envelope(self, take, track, ww, spc):
        amp = self._amp(take, track["id"])
        if amp is None:
            return None
        if self.recording(take):
            segs = [[0, amp.size * BIN, 0]]
        else:
            segs = take["timeline"]["segments"]
        key = (take["id"], track["id"], self._edit_keys.get(take["id"]), round(self.view_start, 9), spc, ww, amp.size)
        hit = self._env.get(key)
        if hit is not None:
            return hit
        levels, src_mid = envelope(amp, segs, self.view_start, spc, ww)
        has = src_mid >= 0
        quiet = np.zeros(ww, bool)
        lvl = levels / 127.0
        if not self.recording(take):
            edits = take["edits"]
            for s0, s1 in edits["silences"].get(track["id"], []):
                quiet |= (src_mid >= s0) & (src_mid < s1)
            gain_db = edits["clip_gain_db"].get(track["id"], 0.0)
            lvl = lvl * math.sqrt(10 ** (gain_db / 20))
            length = self.length(take)
            mid = self.view_start + (np.arange(ww) + 0.5) * spc
            fade = np.ones(ww)
            fi, fo = edits["fade_in"], edits["fade_out"]
            if fi > 0:
                fade = np.minimum(fade, np.sin(np.pi / 2 * np.clip(mid / fi, 0, 1)))
            if fo > 0:
                fade = np.minimum(fade, np.sin(np.pi / 2 * np.clip((length - mid) / fo, 0, 1)))
            lvl = lvl * np.sqrt(fade)
        lvl = np.where(has, np.clip(lvl, 0.0, 1.0), -1.0)
        if len(self._env) > 256:
            self._env.clear()
        self._env[key] = (lvl, quiet)
        return self._env[key]

    def get_content_height(self, container, viewport, width):
        take = self.take()
        return 3 + 4 * max(1, len(self.rows(take)))

    # ---- drawing
    def render_line(self, y):
        width, height = self.size.width, self.size.height
        base = self.rich_style
        line = Line(width, base)
        st = self.app.state
        take = self.take()
        if st is None:
            return line.strip()
        if take is None:
            if y == 1:
                if st["tracks"]:
                    line.add("  No takes yet. Arm inputs (a) and press r to record.", style(DIM))
                else:
                    line.add("  Add an input (i), then press r to record.", style(DIM))
            return line.strip()
        if y == 0:
            return self._title_line(line, take)
        if y == height - 1:
            return self._status_line(line, take)
        if y == 1:
            return self._ruler_line(line, take)
        rows = self.rows(take)
        h = self._track_height(len(rows))
        per_screen = max(1, (height - 3) // h)
        if self.row < self.top_row:
            self.top_row = self.row
        elif self.row >= self.top_row + per_screen:
            self.top_row = self.row - per_screen + 1
        i, sub = divmod(y - 2, h)
        i += self.top_row
        if i >= len(rows) or i - self.top_row >= per_screen:
            return line.strip()
        return self._wave_line(line, take, rows[i], i, sub, h)

    def _track_height(self, n):
        """Rows per track: as many as fit (an even number, 2 to 8)."""
        area = max(2, self.size.height - 3)
        return int(clamp(area // max(1, n) // 2 * 2, 2, 8))

    def _title_line(self, line, take):
        takes = self.takes()
        pos = next((n for n, t in enumerate(takes, 1) if t["id"] == take["id"]), 0)
        line.add(f" {clean(take['name'])}", style(CYAN, bold=True))
        line.add(f"  {pos} of {len(takes)}   ", style(DIM))
        if self.recording(take):
            line.add("● recording  ", style(RED, bold=True))
            line.add(fmt_time(self.length(take)), style(bold=True))
            return line.strip()
        line.add(fmt_time(self.length(take), 1), style(bold=True))
        e = take["edits"]
        if e["trim_start"] or e["trim_end"] or e["cuts"]:
            line.add(f"  (recorded {fmt_time(take['duration'] / R)})", style(DIM))
        if e["fade_in"] or e["fade_out"]:
            line.add(f"  fades {e['fade_in']:g} s / {e['fade_out']:g} s", style(DIM))
        if len(takes) > 1:
            line.add("   [ ] other takes", style(DIM))
        return line.strip()

    def _ruler_line(self, line, take):
        g, ww = self._gutter(), self._wave_width()
        spc = self.spc(take)
        line.add(" " * g)
        chars = [" "] * ww
        styles = [style(DIM)] * ww
        step = 1.0
        for step in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
            if step / spc >= 10:
                break
        decimals = 2 if step < 0.1 else 1 if step < 1 else 0
        k = math.ceil(self.view_start / step - 1e-9)
        while True:
            t = k * step
            col = int(round((t - self.view_start) / spc))
            if col >= ww:
                break
            label = "╵" + fmt_time(t, decimals)
            if col >= 0 and col + len(label) <= ww:
                chars[col:col + len(label)] = list(label)
            k += 1
        for col, fg in ((self._col(self.cursor), CURSOR_BG), (self._col(self.play_pos), GREEN)):
            if col is not None and 0 <= col < ww:
                chars[col] = "▼"
                styles[col] = style(fg, bold=True)
        return line.runs(chars, styles).strip()

    def _wave_line(self, line, take, track, i, sub, h):
        """One row of a track's waveform, mirrored around a centre line: the
        upper half grows up from the centre, the lower half grows down."""
        g, ww = self._gutter(), self._wave_width()
        picked = i == self.row
        if sub == 0:
            name_style = style(track.get("color"), ROW_FOCUS if picked and self.has_focus else None, bold=True)
            line.add(("›" if picked else " ") + clean(track["name"]), name_style, width=g - 1)
        elif sub == 1 and not self.recording(take) and take["edits"]["clip_gain_db"].get(track["id"]):
            line.add(f" {take['edits']['clip_gain_db'][track['id']]:+.1f} dB", style(DIM), width=g - 1)
        else:
            line.add("", width=g - 1)
        line.add("│", style(QUIET))
        spc = self.spc(take)
        env = self._envelope(take, track, ww, spc)
        if env is None:
            return line.add(" loading…" if sub == 0 else "", style(DIM)).strip()
        levels, quiet = env
        half = h // 2
        upper = sub < half
        dist = (half - 1 - sub) if upper else (sub - half)  # rows away from the centre
        # eighths of a cell filled on each side of the centre (at least a hairline where there's audio)
        eighths = np.where(levels < 0, -1, np.maximum(1, np.ceil(levels * half * 8))).astype(int)
        sel = self.selection()
        sel_cols = (self._col(sel[0]), self._col(sel[1])) if sel is not None else None
        cur = self._col(self.cursor) if self.anchor is None else None
        play = self._col(self.play_pos)
        color = track.get("color") or CYAN
        cursor_st, play_st = style(None, CURSOR_BG), style(None, PLAY_BG)
        chars, styles = [], []
        for col in range(ww):
            if col == play:
                chars.append(" ")
                styles.append(play_st)
                continue
            if col == cur:
                chars.append(" ")
                styles.append(cursor_st)
                continue
            in_sel = sel_cols is not None and sel_cols[0] <= col < max(sel_cols[1], sel_cols[0] + 1)
            bg = SELECTION if in_sel else None
            fg = QUIET if quiet[col] else color
            e = eighths[col]
            # the hairline for quiet audio sits just above the centre, so the lower half starts one eighth later
            fill = 0 if e < 0 else int(clamp(e - dist * 8 - (0 if upper else 1), 0, 8))
            if fill == 0:
                chars.append(" ")
                styles.append(style(None, bg))
            elif upper or fill in UPPER_BLOCKS:
                chars.append(VBLOCKS[fill] if upper else UPPER_BLOCKS[fill])
                styles.append(style(fg, bg))
            else:
                # the top `fill` eighths of a cell: a lower block in the background colour over the wave colour
                chars.append(VBLOCKS[8 - fill])
                styles.append(style(bg or WAVE_BG, fg))
        return line.runs(chars, styles).strip()

    def _status_line(self, line, take):
        if self.recording(take):
            return line.add(" Recording. Press r to stop.", style(DIM)).strip()
        line.add(f" Cursor {fmt_time(self.cursor, 1)}", style(bold=True))
        sel = self.selection()
        if sel:
            a, b = sel
            line.add(f"   Selection {fmt_time(a, 1)} – {fmt_time(b, 1)} ({b - a:.1f} s)", style(CYAN, bold=True))
        else:
            line.add("   Shift+← → selects", style(DIM))
        spc = self.spc(take)
        line.add(f"   {spc:.3g} s per column", style(DIM))
        rows = self.rows(take)
        if rows:
            line.add(f"   track: {clean(rows[min(self.row, len(rows) - 1)]['name'])}", style(DIM))
        if take.get("can_undo"):
            line.add("   u undo", style(DIM))
        return line.strip()

    # ---- mouse
    def _mouse_time(self, event):
        off = event.get_content_offset(self)
        if off is None:
            return None, None
        col = off.x - self._gutter()
        return (self._time_at(col) if col >= 0 else None), off.y

    def on_mouse_down(self, event):
        take = self.take()
        if take is None or self.recording(take):
            return
        t, y = self._mouse_time(event)
        if y is not None and y >= 2:
            i = (y - 2) // 2 + self.top_row
            if i < len(self.rows(take)):
                self.row = i
        if t is not None:
            if event.shift:
                if self.anchor is None:
                    self.anchor = self.cursor
            else:
                self.anchor = t
            self.cursor = t
            self._drag = True
            self.capture_mouse()
        self.refresh()

    def on_mouse_move(self, event):
        if self._drag:
            t, _ = self._mouse_time(event)
            if t is None:
                off = event.get_content_offset(self)
                t = 0.0 if off is not None and off.x < self._gutter() else None
            if t is not None:
                self.cursor = t
                self.refresh()

    def on_mouse_up(self, event):
        if self._drag:
            self._drag = False
            self.release_mouse()
            if self.anchor is not None and abs(self.anchor - self.cursor) < 1e-6:
                self.anchor = None
            self.refresh()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    # ---- keys
    def _editable(self):
        take = self.take()
        if take is None:
            return None
        if self.recording(take):
            self.app.notify("Stop recording first")
            return None
        return take

    def _set_cursor(self, take, t, select):
        if select and self.anchor is None:
            self.anchor = self.cursor
        elif not select:
            self.anchor = None
        self.cursor = clamp(t, 0.0, self.length(take))
        self._scroll_to(take, self.cursor)
        self.refresh()

    def action_move(self, cols, select=False):
        take = self._editable()
        if take is not None:
            self._set_cursor(take, self.cursor + cols * self.spc(take), select)

    def action_move_page(self, pages, select=False):
        take = self._editable()
        if take is not None:
            self._set_cursor(take, self.cursor + pages * self._wave_width() * self.spc(take) * 0.9, select)

    def action_jump(self, where, select=False):
        take = self._editable()
        if take is not None:
            self._set_cursor(take, where * self.length(take), select)

    def action_row(self, step):
        rows = self.rows(self.take())
        if rows:
            self.row = int(clamp(self.row + step, 0, len(rows) - 1))
            self.refresh()

    def action_select_all(self):
        take = self._editable()
        if take is not None:
            self.anchor = 0.0
            self.cursor = self.length(take)
            self.refresh()

    def action_clear(self):
        self.anchor = None
        self.refresh()

    def action_zoom(self, factor):
        take = self._editable()
        if take is None:
            return
        old = self.spc(take)
        col = (self.cursor - self.view_start) / old
        self.zoom = old * factor
        new = self.spc(take)
        self.view_start = self._clamp_view(take, self.cursor - col * new)
        if self.zoom >= max(self.length(take), 0.5) / self._wave_width():
            self.zoom = None
            self.view_start = 0.0
        self.refresh()

    def action_zoom_fit(self):
        self.zoom = None
        self.view_start = 0.0
        self.refresh()

    def _picked(self, take):
        rows = self.rows(take)
        return rows[min(self.row, len(rows) - 1)] if rows else None

    def action_edit(self, op):
        take = self._editable()
        if take is None:
            return
        sel = self.selection()
        if sel is None:
            self.app.notify("Select part of the take first: hold Shift and press ← or →", severity="warning")
            return
        args = {"start": sel[0], "end": sel[1]}
        if op in ("silence", "unsilence"):
            tr = self._picked(take)
            if tr is None:
                return
            args["track"] = tr["id"]
        if op == "cut":
            self.cursor, self.anchor = sel[0], None
        elif op == "trim":
            self.cursor, self.anchor = 0.0, None
            self.zoom, self.view_start = None, 0.0
        self.app.bridge.call(self.app.engine.edit_take, take["id"], op, args)
        self.refresh()

    def action_normalize(self):
        take = self._editable()
        tr = self._picked(take) if take else None
        if tr is not None:
            name = clean(tr["name"])
            self.app.bridge.call(self.app.engine.edit_take, take["id"], "normalize", {"track": tr["id"]},
                                 done=lambda _: self.app.notify(f"Normalized {name} to -1 dB peak"))

    def action_undo(self):
        take = self._editable()
        if take is not None:
            self.app.bridge.call(self.app.engine.edit_take, take["id"], "undo", {})

    def action_redo(self):
        take = self._editable()
        if take is not None:
            self.app.bridge.call(self.app.engine.edit_take, take["id"], "redo", {})

    def action_fades(self):
        take = self._editable()
        if take is None:
            return
        tid = take["id"]

        def apply(values):
            if values is not None:
                self.app.bridge.call(self.app.engine.edit_take, tid, "fades",
                                     {"fade_in": values[0], "fade_out": values[1]})
        self.app.push_screen(FadesScreen(take["edits"]["fade_in"], take["edits"]["fade_out"]), apply)

    def action_rename_take(self):
        take = self._editable()
        if take is not None:
            tid = take["id"]
            self.app.prompt("Rename take", take["name"],
                            lambda name: self.app.bridge.call(self.app.engine.rename_take, tid, name))

    def action_delete_take(self):
        take = self._editable()
        if take is None:
            return
        tid = take["id"]
        self.app.ask(f"Delete {clean(take['name'])}? Its audio moves to the project's trash folder.",
                     lambda: self.app.bridge.call(self.app.engine.delete_take, tid), yes="Delete", danger=True)


# ------------------------------------------------------------------- dialogs

def inp(*args, **kw):
    return Input(*args, compact=True, **kw)


def sel(*args, **kw):
    return Select(*args, compact=True, **kw)


DIALOG_CSS = """
ModalScreen { align: center middle; background: #000000 55%; }
.dialog { width: 76; max-width: 96%; height: auto; max-height: 94%; border: round #60a5fa;
          background: #161a22; padding: 1 2; overflow-y: auto; }
.dialog.wide { width: 100; }
.title { text-style: bold; color: #e5e7eb; margin-bottom: 1; }
.hint { color: #9ca3af; margin-bottom: 1; }
.buttons { height: auto; align-horizontal: right; margin-top: 1; }
.buttons Button { margin-left: 2; }
.row { height: auto; margin-bottom: 1; }
.row > Label { width: 16; color: #9ca3af; }
.row > Input, .row > Select, .row > Checkbox { width: 1fr; }
.row > Button { margin-left: 2; min-width: 10; }
#export-form .row { margin-bottom: 0; }
OptionList { height: auto; max-height: 16; }
"""


class ConfirmScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message, yes="OK", no="Cancel", danger=False, focus_yes=True):
        super().__init__()
        self.message, self.yes, self.no, self.danger, self.focus_yes = message, yes, no, danger, focus_yes

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.message, classes="title", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self.no, id="no")
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary")

    def on_mount(self):
        self.query_one("#yes" if self.focus_yes else "#no").focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_cancel(self):
        self.dismiss(False)


class PromptScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, value=""):
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="title")
            yield inp(value=self.value, id="value")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="no")
                yield Button("OK", id="yes", variant="primary")

    def on_input_submitted(self, event):
        self._finish()

    def on_button_pressed(self, event):
        if event.button.id == "yes":
            self._finish()
        else:
            self.dismiss(None)

    def _finish(self):
        value = self.query_one("#value", Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self):
        self.dismiss(None)


class FadesScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, fade_in, fade_out):
        super().__init__()
        self.fade_in, self.fade_out = fade_in, fade_out

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("Fades", classes="title")
            yield Static("Seconds of fade at the start and end of the take (0 for none).", classes="hint")
            with Horizontal(classes="row"):
                yield Label("Fade in (s)")
                yield inp(f"{self.fade_in:g}", type="number", id="in")
            with Horizontal(classes="row"):
                yield Label("Fade out (s)")
                yield inp(f"{self.fade_out:g}", type="number", id="out")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="no")
                yield Button("Apply", id="yes", variant="primary")

    def _finish(self):
        try:
            values = (float(self.query_one("#in", Input).value or 0), float(self.query_one("#out", Input).value or 0))
        except ValueError:
            self.app.notify("Enter the fades in seconds, like 2 or 0.5", severity="error")
            return
        self.dismiss(values)

    def on_input_submitted(self, event):
        self._finish()

    def on_button_pressed(self, event):
        if event.button.id == "yes":
            self._finish()
        else:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class OutputScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def compose(self):
        st = self.app.state
        current = st.get("output")
        default = "System default (follows your sound settings)"
        options = [Option(("● " if not current else "  ") + default, id="@default")]
        for o in st.get("outputs", []):
            options.append(Option(("● " if o["node"] == current else "  ") + clean(o["label"]), id=o["node"]))
        with Vertical(classes="dialog"):
            yield Static("Output for monitoring and playback", classes="title")
            yield OptionList(*options, id="outputs")
            yield Static("Enter chooses · Esc closes", classes="hint")

    def on_option_list_option_selected(self, event):
        node = event.option.id
        self.app.bridge.call(self.app.engine.set_output, None if node == "@default" else node)
        self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "cancel", "Close")]

    def compose(self):
        keys = [
            ("Anywhere", [
                ("r", "record / stop recording"), ("space", "play from the cursor / stop"),
                ("p", "pause / resume"), ("[  ]", "previous / next take"), ("i", "add an input"),
                ("x", "export this take"), ("o", "projects: open, new, rename"),
                ("d", "output device for monitoring and playback"), ("tab", "switch between inputs and timeline"),
                ("q, ctrl+c", "quit (asks first while recording)")]),
            ("Inputs", [
                ("↑ ↓", "pick an input (the last row is the master level)"),
                ("a m s h", "arm for recording, mute, solo, hear (monitor)"),
                ("+ −  0", "level up / down 1 dB, back to 0 dB"), ("← →", "pan"),
                ("e", "effects: noise suppression, gate, EQ"), ("c", "change what the input records"),
                ("n", "rename"), ("del", "remove"), ("shift+↑ ↓", "move up / down")]),
            ("Timeline", [
                ("← →", "move the cursor (pgup/pgdn a screen, home/end)"),
                ("shift+← →", "select (also shift+home/end; a all; esc clears)"),
                ("click, drag", "move the cursor, select"),
                ("↑ ↓", "pick a track, for silence and normalize"), ("c or del", "cut the selection"),
                ("k", "keep only the selection"), ("z  Z", "silence / unsilence the selection on the picked track"),
                ("N", "normalize the picked track (-1 dB peak)"), ("f", "fade in and out"),
                ("u  U", "undo / redo"), ("+ −  0", "zoom in / out / fit"), ("n  D", "rename / delete the take")]),
        ]
        t = Text()
        for head, rows in keys:
            t.append(f"{head}\n", "bold #60a5fa")
            for k, desc in rows:
                t.append(f"  {k:<12}", "bold")
                t.append(f"{desc}\n")
            t.append("\n")
        t.append("Edits never change the recorded files; they're applied when you play and export.\n", DIM)
        path = getattr(self.app, "log_path", None)
        if path:
            t.append(f"Log: {path}\n", DIM)
        with Vertical(classes="dialog wide"):
            yield Static(f"AudioMagic {__version__} · keys", classes="title")
            with VerticalScroll():
                yield Static(t)

    def action_cancel(self):
        self.dismiss(None)


class Knob(Widget, can_focus=True):
    """One effects setting: a switch (space) or a value (← →)."""

    BINDINGS = [
        Binding("left", "nudge(-1)", "Less", show=False),
        Binding("right", "nudge(1)", "More", show=False),
        Binding("shift+left", "nudge(-5)", "Less", show=False),
        Binding("shift+right", "nudge(5)", "More", show=False),
        Binding("space,enter", "toggle", "On/off", show=False),
    ]
    DEFAULT_CSS = """
    Knob { height: 1; }
    Knob:focus { background: #1e3a5f; }
    """

    class Changed(Message):
        def __init__(self, knob):
            super().__init__()
            self.knob = knob

    def __init__(self, path, label, value, lo=None, hi=None, step=None, fmt=None, **kw):
        super().__init__(**kw)
        self.path, self.label, self.value = path, label, value
        self.lo, self.hi, self.step, self.fmt = lo, hi, step, fmt

    @property
    def switch(self):
        return self.step is None

    def set_value(self, value):
        if value != self.value:
            self.value = value
            self.refresh()

    def render(self):
        t = Text(no_wrap=True)
        t.append(f"  {self.label:<22}", "bold" if self.switch else "")
        if self.switch:
            t.append(" ON  " if self.value else " off ", f"bold #111111 on {GREEN}" if self.value else DIM)
            return t
        width = 24
        frac = (self.value - self.lo) / (self.hi - self.lo)
        pos = int(round(frac * (width - 1)))
        t.append("◀ ", DIM)
        t.append("━" * pos, CYAN)
        t.append("●", "bold #ffffff")
        t.append("─" * (width - 1 - pos), QUIET)
        t.append(" ▶ ", DIM)
        t.append(self.fmt(self.value), "bold")
        return t

    def action_nudge(self, n):
        if self.switch:
            return
        v = clamp(round((self.value + n * self.step) / self.step) * self.step, self.lo, self.hi)
        v = round(v, 4)
        if v != self.value:
            self.value = v
            self.refresh()
            self.post_message(self.Changed(self))

    def action_toggle(self):
        if self.switch:
            self.value = not self.value
            self.refresh()
            self.post_message(self.Changed(self))

    def on_click(self):
        self.focus()
        self.action_toggle()


def _db(v):
    return f"{v:+.1f} dB"


def _strength(v):
    return "Gentle" if v < 0.34 else "Medium" if v < 0.67 else "Strong"


EFFECT_ROWS = [
    ("ns.on", "Noise suppression", None),
    ("ns.strength", "Strength", (0.0, 1.0, 0.05, lambda v: f"{_strength(v)} {v * 100:.0f}%")),
    ("gate.on", "Noise gate", None),
    ("gate.threshold", "Threshold", (-80.0, -10.0, 1.0, lambda v: f"{v:.0f} dB")),
    ("gate.release", "Release", (20.0, 1000.0, 10.0, lambda v: f"{v:.0f} ms")),
    ("eq.on", "EQ", None),
    ("eq.lowcut", "Low cut (rumble)", None),
    ("eq.low", "Low (150 Hz)", (-12.0, 12.0, 0.5, _db)),
    ("eq.mid", "Mid (2.5 kHz)", (-12.0, 12.0, 0.5, _db)),
    ("eq.high", "High (8 kHz)", (-12.0, 12.0, 0.5, _db)),
]


class EffectsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("up", "app.focus_previous", "Up", show=False),
        Binding("down", "app.focus_next", "Down", show=False),
        Binding("v", "preset('voice')", "Voice preset"),
        Binding("o", "preset('off')", "All off"),
    ]

    def __init__(self, track_id):
        super().__init__()
        self.track_id = track_id
        self._local = 0.0

    def track(self):
        return next((t for t in self.app.tracks if t["id"] == self.track_id), None)

    def compose(self):
        t = self.track()
        fx = t["fx"] if t else {}
        with Vertical(classes="dialog"):
            yield Static(f"Effects · {clean(t['name']) if t else ''}", classes="title")
            yield Static(id="fx-meter")
            for path, label, spec in EFFECT_ROWS:
                group, key = path.split(".")
                value = fx.get(group, {}).get(key)
                if spec is None:
                    yield Knob(path, label, bool(value))
                else:
                    lo, hi, step, fmt = spec
                    yield Knob(path, "  " + label, float(clamp(value, lo, hi)), lo, hi, step, fmt)
            yield Static("↑ ↓ pick · ← → change (shift: faster) · space on/off · v voice preset · o all off",
                         classes="hint")
            yield Static("Effects never change the raw recording. You hear them while monitoring and playing "
                         "back, and they're applied when you export.", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Done", id="done", variant="primary")

    def on_mount(self):
        self.query(Knob).first().focus()

    def set_level(self, db):
        chars, styles = meter_runs(db, 40)
        t = Text("  Level  ", style=DIM)
        for c, s in zip(chars, styles):
            t.append(c, s)
        for meter in self.query("#fx-meter"):  # gone while the dialog closes
            meter.update(t)

    def on_knob_changed(self, event):
        group, key = event.knob.path.split(".")
        self._local = time.monotonic()
        self.app.bridge.call(self.app.engine.update_track, self.track_id, {"fx": {group: {key: event.knob.value}}})

    def on_state(self, st):
        t = self.track()
        if t is None:
            self.dismiss(None)
            return
        if time.monotonic() - self._local < 1.0:
            return
        for knob in self.query(Knob):
            group, key = knob.path.split(".")
            v = t["fx"][group][key]
            knob.set_value(bool(v) if knob.switch else float(v))

    def action_preset(self, name):
        self._local = 0.0
        self.app.bridge.call(self.app.engine.update_track, self.track_id, {"preset": name})

    def on_button_pressed(self, event):
        self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class AddInputScreen(ModalScreen):
    """Microphones and interfaces, programs, desktop audio, streams, SRT and a test tone."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("f5,ctrl+r", "refresh", "Refresh"),
    ] + [Binding(str(n), f"tab('{tab}')", show=False) for n, tab in enumerate(
        ("devices", "apps", "monitor", "url", "srt", "tone"), 1)]

    def __init__(self, replace=None):
        super().__init__()
        self.replace = replace
        self.choices = {}
        self.sources = None

    def compose(self):
        title = f"Change input · {clean(self.replace['name'])}" if self.replace else "Add input"
        used = {t["source"].get("port") for t in self.app.tracks if t["source"].get("kind") == "srt"}
        port = 9000
        while port in used:
            port += 1
        with Vertical(classes="dialog wide"):
            yield Static(title, classes="title")
            with TabbedContent(initial="devices"):
                with TabPane("1 Mics & interfaces", id="devices"):
                    yield OptionList(Option("Looking for devices…", disabled=True), id="dev-list")
                with TabPane("2 Programs", id="apps"):
                    yield Static("Records what one program plays: a Discord, Zoom or Jitsi call, a browser, a game. "
                                 "Programs only show up while they're playing sound (F5 refreshes).", classes="hint")
                    yield OptionList(id="app-list")
                with TabPane("3 Desktop audio", id="monitor"):
                    yield Static("Records everything that plays through an output: every program at once.",
                                 classes="hint")
                    yield OptionList(id="mon-list")
                with TabPane("4 Stream", id="url"):
                    yield Static("Internet radio (Icecast/Shoutcast), HLS (.m3u8), RTSP or any direct audio link.",
                                 classes="hint")
                    with Horizontal(classes="row"):
                        yield Label("Address")
                        yield inp(placeholder="https://example.com/live.mp3", id="url-url")
                    with Horizontal(classes="row"):
                        yield Label("Name")
                        yield inp(placeholder="Radio", id="url-name", max_length=60)
                    with Horizontal(classes="buttons"):
                        yield Button("Add stream", id="add-url", variant="primary")
                with TabPane("5 SRT (OBS)", id="srt"):
                    yield Static("Receives a stream from OBS, ffmpeg or another SRT sender. The passphrase encrypts "
                                 "it (AES-128); leave it empty to turn encryption off.", classes="hint")
                    with Horizontal(classes="row"):
                        yield Label("Name")
                        yield inp("OBS", id="srt-name", max_length=60)
                    with Horizontal(classes="row"):
                        yield Label("Port")
                        yield inp(str(port), type="integer", id="srt-port")
                    with Horizontal(classes="row"):
                        yield Label("Passphrase")
                        yield inp(random_pass(), id="srt-pass", max_length=79)
                    with Horizontal(classes="row"):
                        yield Label("Latency (ms)")
                        yield inp("200", type="integer", id="srt-latency")
                    with Horizontal(classes="row"):
                        yield Label("")
                        yield Checkbox("Allow other computers on my network to send", False, id="srt-lan",
                                       compact=True)
                    yield Static(id="srt-url", classes="hint")
                    with Horizontal(classes="buttons"):
                        yield Button("Add SRT input", id="add-srt", variant="primary")
                with TabPane("6 Test tone", id="tone"):
                    yield Static("A steady tone for checking levels, monitoring and export without a microphone.",
                                 classes="hint")
                    with Horizontal(classes="row"):
                        yield Label("Frequency (Hz)")
                        yield inp("440", type="number", id="tone-freq")
                    with Horizontal(classes="buttons"):
                        yield Button("Add test tone", id="add-tone", variant="primary")
            yield Static("1–6 switch tabs · Enter adds · F5 refreshes the lists · Esc closes", classes="hint")

    def on_mount(self):
        self._srt_url()
        self.action_refresh()
        self.query_one("#dev-list").focus()

    def action_refresh(self):
        self.app.bridge.call(self.app.engine.list_sources, done=self.show_sources)

    def action_tab(self, tab):
        self.query_one(TabbedContent).active = tab
        self.query_one(f"#{tab}").query("OptionList, Input").first().focus()

    def show_sources(self, sources):
        self.sources = sources
        self.choices = {}

        def opt(label, items, **kw):
            # the id is the input it adds, so the highlight survives a refresh
            key = repr([it["source"] for it in items])
            if key in self.choices:
                key += f"#{len(self.choices)}"
            self.choices[key] = items
            return Option(label, id=key, **kw)

        dev, apps, mon = [], [], []
        if not sources.get("ok"):
            msg = f"Can't see PipeWire: {sources.get('error')}"
            dev = apps = [Option(msg, disabled=True)]
        else:
            for d in sources["devices"]:
                chans = d["channels"]
                label = clean(d["label"])
                base = {"kind": "device", "node": d["node"], "label": d["label"], "device_channels": len(chans)}
                head = f"{label}  ({len(chans)} input{'s' if len(chans) != 1 else ''}" + \
                       (", system default)" if d["default"] else ")")
                if len(chans) <= 1:
                    dev.append(opt(Text(head, style="bold"), [{"name": d["label"], "source": {**base, "channels": [0]}}]))
                    continue
                dev.append(Option(Text(head, style="bold"), disabled=True))
                first = d["label"].split(" ")[0]

                def chan_item(i, d=d, base=base, first=first):
                    return {"name": f"{first} {i + 1}",
                            "source": {**base, "channels": [i], "label": f"{d['label']} · {d['channel_labels'][i]}"}}
                for i, lbl in enumerate(d["channel_labels"]):
                    dev.append(opt(f"    {clean(lbl)}", [chan_item(i)]))
                if not self.replace:
                    dev.append(opt("    Each input as its own track", [chan_item(i) for i in range(len(chans))]))
                dev.append(opt("    Stereo (inputs 1 and 2 together)",
                               [{"name": d["label"], "source": {**base, "channels": [0, 1],
                                                                "label": f"{d['label']} · stereo"}}]))
            if not dev:
                dev = [Option("No microphones or audio interfaces found. Plug one in and press F5.", disabled=True)]
            for a in sources["apps"]:
                label = clean(a["label"]) + (f"  ({clean(a['detail'])})" if a.get("detail") else "")
                apps.append(opt(label, [{"name": a["label"], "source": {"kind": "app", "app": a["app"],
                                                                        "binary": a["binary"], "label": a["label"]}}]))
            if not apps:
                apps = [Option("No programs are playing sound right now. Start the call or playback, then press F5.",
                               disabled=True)]
        mon.append(opt("Default output (follows your sound settings)",
                       [{"name": "Desktop audio", "source": {"kind": "monitor", "node": "@default",
                                                              "label": "Everything you hear"}}]))
        for o in sources.get("outputs", []):
            mon.append(opt(clean(o["label"]), [{"name": o["label"], "source": {
                "kind": "monitor", "node": o["node"], "label": f"Everything on {o['label']}"}}]))
        for list_id, options in (("#dev-list", dev), ("#app-list", apps), ("#mon-list", mon)):
            ol = self.query_one(list_id, OptionList)
            before = ol.highlighted_option.id if ol.highlighted_option is not None else None
            ol.clear_options()
            ol.add_options(options)
            ids = [o.id for o in options]
            if before in ids:
                ol.highlighted = ids.index(before)

    def on_option_list_option_selected(self, event):
        items = self.choices.get(event.option.id)
        if items:
            self.add(items)

    def add(self, items):
        app = self.app
        if self.replace:
            app.bridge.call(app.engine.update_track, self.replace["id"], {"source": items[0]["source"]},
                            done=lambda _: (app.notify("Input changed"), app.close(self)))
        else:
            name = items[0].get("name") or items[0]["source"].get("label") or "input"
            msg = f"Added {len(items)} inputs" if len(items) > 1 else f"Added {clean(name)}"
            app.bridge.call(app.engine.add_tracks, items, done=lambda _: (app.notify(msg), app.close(self)))

    def _srt_url(self):
        try:
            port = self.query_one("#srt-port", Input).value
            pw = self.query_one("#srt-pass", Input).value
            lan = self.query_one("#srt-lan", Checkbox).value
        except Exception:
            return
        host = "<this computer's IP>" if lan else "127.0.0.1"
        url = f"srt://{host}:{port}?mode=caller"
        if pw:
            url += f"&passphrase={pw}&pbkeylen=16"
        self.query_one("#srt-url", Static).update(
            Text.assemble(("In OBS: Settings → Stream → Service \"Custom…\", Server:\n", DIM), (url, f"bold {CYAN}")))

    def on_input_changed(self, event):
        if event.input.id in ("srt-port", "srt-pass"):
            self._srt_url()

    def on_checkbox_changed(self, event):
        self._srt_url()

    def on_input_submitted(self, event):
        prefix = (event.input.id or "").split("-")[0]
        if prefix in ("url", "srt", "tone"):
            self._submit(prefix)

    def on_button_pressed(self, event):
        if event.button.id and event.button.id.startswith("add-"):
            self._submit(event.button.id[4:])

    def _value(self, wid):
        return self.query_one(f"#{wid}", Input).value.strip()

    def _submit(self, kind):
        try:
            if kind == "url":
                self.add([{"name": self._value("url-name") or None,
                           "source": {"kind": "url", "url": self._value("url-url")}}])
            elif kind == "srt":
                self.add([{"name": self._value("srt-name") or "OBS", "source": {
                    "kind": "srt", "port": int(self._value("srt-port") or 9000),
                    "passphrase": self._value("srt-pass"), "lan": self.query_one("#srt-lan", Checkbox).value,
                    "latency": int(self._value("srt-latency") or 200)}}])
            elif kind == "tone":
                freq = float(self._value("tone-freq") or 440)
                self.add([{"name": f"Tone {freq:g} Hz", "source": {"kind": "tone", "freq": freq}}])
        except ValueError:
            self.app.notify("Check the numbers", severity="error")

    def action_close(self):
        self.dismiss(None)


def random_pass():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


class ProjectsScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]

    def compose(self):
        st = self.app.state
        recording = st["transport"]["recording"]
        with Vertical(classes="dialog wide"):
            yield Static("Projects", classes="title")
            with Horizontal(classes="row"):
                yield Label("This project")
                yield inp(st["project"]["name"], id="name", max_length=80)
                yield Button("Rename", id="rename", compact=True)
            with Horizontal(classes="row"):
                yield Label("New project")
                yield inp(placeholder="Episode 12", id="new", max_length=80)
                yield Button("Create", id="create", variant="primary", disabled=recording, compact=True)
            with Horizontal(classes="row"):
                yield Label("")
                yield Checkbox("Use the same inputs and settings as this project", True, id="copy", compact=True)
            yield Static("Open a project", classes="title")
            yield OptionList(Option("Loading…", disabled=True), id="projects")
            yield Static(id="root", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")

    def on_mount(self):
        self.app.bridge.call(self.app.engine.list_projects, done=self.show)
        self.query_one("#projects").focus()

    def show(self, data):
        ol = self.query_one("#projects", OptionList)
        ol.clear_options()
        for p in sorted(data["projects"], key=lambda p: -p.get("modified", 0)):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("modified", 0)))
            label = Text.assemble(("● " if p.get("current") else "  ", GREEN), (clean(p["name"]), "bold"),
                                  (f"   {p['tracks']} inputs · {p['takes']} takes · {when}", DIM))
            ol.add_option(Option(label, id=p["path"]))
        self.query_one("#root", Static).update(f"Projects are in {data['root']}")

    def on_option_list_option_selected(self, event):
        path = event.option.id
        app = self.app
        app.bridge.call(app.engine.open_project, path, done=lambda _: (app.opened_project(), app.close(self)))

    def _create(self):
        app = self.app
        name = self.query_one("#new", Input).value.strip()
        copy = self.query_one("#copy", Checkbox).value
        app.bridge.call(app.engine.create_project, name, copy,
                        done=lambda _: (app.opened_project(), app.notify(f"Created {clean(name)}"), app.close(self)))

    def _rename(self):
        app = self.app
        app.bridge.call(app.engine.rename_project, self.query_one("#name", Input).value,
                        done=lambda _: app.notify("Renamed"))

    def on_input_submitted(self, event):
        if event.input.id == "new":
            self._create()
        elif event.input.id == "name":
            self._rename()

    def on_button_pressed(self, event):
        if event.button.id == "create":
            self._create()
        elif event.button.id == "rename":
            self._rename()
        else:
            self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class ExportScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]

    WHAT = [("The mix (one file)", "mix"), ("Each track separately", "stems"), ("Both", "both")]

    def __init__(self, take_id):
        super().__init__()
        self.take_id = take_id
        self.job = None
        self.options = export_mod.describe()

    def compose(self):
        st = self.app.state
        take = next(t for t in st["takes"] if t["id"] == self.take_id)
        prev = st["project"].get("export") or {}
        formats = {f["id"]: f for f in self.options["formats"]}
        fmt = prev.get("format") if prev.get("format") in formats else "flac"
        tags = prev.get("tags") or {}
        any_solo = any(t["solo"] for t in st["tracks"])
        have = {f["track_id"] for f in take["tracks"]}
        picks = [(clean(t["name"]) + ("" if not t["mute"] else " (muted)") +
                  ("" if not any_solo or t["solo"] else " (not soloed)"),
                  t["id"], not t["mute"] and (not any_solo or t["solo"]))
                 for t in st["tracks"] if t["id"] in have]
        with Vertical(classes="dialog wide"):
            yield Static(f"Export · {clean(take['name'])} · {fmt_time(take['timeline']['length'] / R, 1)} after edits",
                         classes="title")
            with VerticalScroll(id="export-form"):
                with Horizontal(classes="row"):
                    yield Label("Format")
                    yield sel([(f["label"], f["id"]) for f in self.options["formats"]], value=fmt,
                                 allow_blank=False, id="format")
                with Horizontal(classes="row"):
                    yield Label("Quality")
                    yield sel(self._qualities(fmt), value=self._quality(fmt, prev), allow_blank=False, id="quality")
                with Horizontal(classes="row"):
                    yield Label("Export")
                    yield sel(self.WHAT, value=prev.get("what") if prev.get("what") in ("mix", "stems", "both")
                                 else "mix", allow_blank=False, id="what")
                with Horizontal(classes="row"):
                    yield Label("Mix channels")
                    yield sel([("Stereo", 2), ("Mono", 1)], value=2 if int(prev.get("channels") or 2) == 2 else 1,
                                 allow_blank=False, id="channels")
                with Horizontal(classes="row"):
                    yield Label("Normalize")
                    norms = [(n["label"], n["id"]) for n in self.options["normalize"]]
                    yield sel(norms, value=prev.get("normalize") if prev.get("normalize") in {i for _, i in norms}
                                 else "off", allow_blank=False, id="normalize")
                with Horizontal(classes="row"):
                    yield Label("Folder")
                    yield inp(prev.get("folder") or os.path.join(st["project"]["path"], "exports"), id="folder")
                yield Static("Tracks (space toggles)", classes="hint")
                yield SelectionList(*picks, id="tracks")
                with Horizontal(classes="row"):
                    yield Label("Title")
                    yield inp(take["name"], id="t-title")
                with Horizontal(classes="row"):
                    yield Label("Artist")
                    yield inp(tags.get("artist", ""), id="t-artist")
                with Horizontal(classes="row"):
                    yield Label("Album / show")
                    yield inp(tags.get("album") or st["project"]["name"], id="t-album")
                with Horizontal(classes="row"):
                    yield Label("Year")
                    yield inp(str(time.localtime().tm_year), id="t-date")
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Static(id="result")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")
                yield Button("Export", id="go", variant="primary")

    def _qualities(self, fmt):
        f = next(x for x in self.options["formats"] if x["id"] == fmt)
        return [(label, value) for value, label in f["qualities"]]

    def _quality(self, fmt, prev):
        f = next(x for x in self.options["formats"] if x["id"] == fmt)
        values = [v for v, _ in f["qualities"]]
        return prev.get("quality") if prev.get("format") == fmt and prev.get("quality") in values else f["default"]

    def on_mount(self):
        self.query_one("#progress").display = False
        if not self.options["ffmpeg"]:
            self.query_one("#result", Static).update(Text("ffmpeg is not installed, so exporting won't work. "
                                                          "Run: sudo apt install ffmpeg", style=f"bold {RED}"))
        self.query_one("#go").focus()

    def on_select_changed(self, event):
        if event.select.id == "format":
            q = self.query_one("#quality", Select)
            q.set_options(self._qualities(event.value))
            q.value = self._quality(event.value, {})

    def on_button_pressed(self, event):
        bid = event.button.id
        if bid == "go":
            self.start()
        elif bid == "close":
            if self.job:
                self.app.bridge.call(self.app.engine.cancel_export, self.job)
            else:
                self.dismiss(None)

    def start(self):
        ids = list(self.query_one("#tracks", SelectionList).selected)
        if not ids:
            self.app.notify("Pick at least one track", severity="error")
            return
        v = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        opts = {
            "format": self.query_one("#format", Select).value,
            "quality": self.query_one("#quality", Select).value,
            "what": self.query_one("#what", Select).value,
            "channels": int(self.query_one("#channels", Select).value),
            "normalize": self.query_one("#normalize", Select).value,
            "folder": v("folder"),
            "track_ids": ids,
            "tags": {"title": v("t-title"), "artist": v("t-artist"), "album": v("t-album"), "date": v("t-date")},
        }
        self.query_one("#go", Button).disabled = True
        self.query_one("#close", Button).label = "Cancel export"
        bar = self.query_one("#progress", ProgressBar)
        bar.display = True
        bar.update(progress=0)
        self.query_one("#result", Static).update(Text("Starting", style=DIM))
        self.job = "starting"
        self.app.bridge.call(self.app.engine.start_export, self.take_id, opts, done=self._started,
                             failed=self._not_started)

    def _not_started(self):
        self.job = None
        self.query_one("#go", Button).disabled = False
        self.query_one("#close", Button).label = "Close"
        self.query_one("#progress", ProgressBar).display = False
        self.query_one("#result", Static).update("")

    def _started(self, job_id):
        self.job = job_id
        info = self.app.export_jobs.get(job_id)
        if info:
            self.on_job(info)

    def on_job(self, job):
        if self.job != job["id"]:
            return
        self.query_one("#progress", ProgressBar).update(progress=job["progress"] * 100)
        result = self.query_one("#result", Static)
        if job["state"] == "running":
            result.update(Text(job["message"], style=DIM))
            return
        self.job = None
        self.query_one("#go", Button).disabled = False
        self.query_one("#close", Button).label = "Close"
        if job["state"] == "done":
            t = Text(job["message"] + "\n", style=f"bold {GREEN}")
            for f in job["files"]:
                t.append(f"  {os.path.basename(f)}\n")
            t.append(f"in {job['folder']}", style=DIM)
            result.update(t)
        else:
            result.update(Text(job["message"], style=f"bold {RED}"))

    def action_close(self):
        if self.job:
            self.app.notify("The export keeps going; you'll see when it's done")
        self.dismiss(None)


# ------------------------------------------------------------------------ app

MAIN_ACTIONS = {"record", "play", "pause", "take", "add_input", "export", "projects", "output", "help", "quit_app"}


class AudioMagicTUI(App):
    TITLE = "AudioMagic"
    CSS = DIALOG_CSS + """
    Screen { background: #0f1117; }
    """
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("r", "record", "Record"),
        Binding("space", "play", "Play/Stop"),
        Binding("p", "pause", "Pause", show=False),
        Binding("left_square_bracket", "take(-1)", "Previous take", show=False),
        Binding("right_square_bracket", "take(1)", "Next take", show=False),
        Binding("i", "add_input", "Add input"),
        Binding("x", "export", "Export"),
        Binding("o", "projects", "Projects"),
        Binding("d", "output", "Output", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("ctrl+q", "quit_app", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit_app", "Quit", show=False),  # what terminal users reach for
    ]

    def __init__(self, engine, quit_event=None, warnings=(), log_path=None):
        super().__init__()
        self.engine = engine
        self.quit_event = quit_event
        self.warnings = list(warnings)
        self.log_path = log_path
        self.state = None
        self.meters = None
        self.export_jobs = {}
        self.bridge = Bridge(self, engine)
        self._ticks = 0
        self._quitting = False

    # ---- plumbing
    @property
    def tracks(self):
        return self.state["tracks"] if self.state else []

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        yield TrackList(id="tracks")
        yield Timeline(id="timeline")
        yield Footer()

    def on_mount(self):
        self.tracklist = self.query_one(TrackList)
        self.timeline = self.query_one(Timeline)
        self.topbar = self.query_one(TopBar)
        self.bridge.start()
        self.set_interval(1 / 25, self.tick)
        for w in self.warnings:
            self.notify(w, severity="warning", timeout=12)
        self.tracklist.focus()

    def on_unmount(self):
        self.bridge.stop()

    def on_deliver(self, msg):
        msg.fn(*msg.args, **msg.kwargs)

    def check_action(self, action, parameters):
        if action in MAIN_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        return True

    def apply_state(self, st, seq=None):
        if seq is not None and seq < self.bridge.submitted:
            # older than a change already shown on screen; a newer state follows
            return
        prev = self.state
        self.state = st
        ids = [t["id"] for t in st["takes"]]
        rec = st["transport"]["rec_take"]
        tl = self.timeline
        if rec and (prev is None or prev["transport"]["rec_take"] != rec):
            tl.show_take(rec)  # and it stays shown after the recording stops
        elif tl.take_id not in ids:
            tl.show_take(ids[-1] if ids else None)
        self.tracklist.on_state()
        tl.on_state()
        self.topbar.refresh()
        for screen in self.screen_stack:
            if hasattr(screen, "on_state"):
                screen.on_state(st)

    def tick(self):
        if self.quit_event is not None and self.quit_event.is_set() and not self._quitting:
            self._quitting = True
            self.exit()
            return
        try:
            m = self.engine.meters()
        except Exception:
            log.exception("reading the meters failed")
            return
        self.meters = m
        self.tracklist.set_meters(m)
        self.timeline.set_positions(m.get("pos"), m.get("rec"))
        self.topbar.set_clock(m, self.state)
        if isinstance(self.screen, EffectsScreen):
            self.screen.set_level(self.tracklist.levels.get(self.screen.track_id, -120.0))
        self._ticks += 1
        if self._ticks % 3 == 0:
            peaks = self.engine.rec_peaks()
            rec = self.engine.rec_take
            if peaks and rec is not None:
                self.timeline.add_live(rec.id, peaks)
        for ev in self.engine.pop_events():
            self.on_engine_event(ev)

    def on_engine_event(self, ev):
        kind = ev.get("type")
        if kind == "toast":
            self.notify(ev.get("text", ""), severity=SEVERITY.get(ev.get("kind"), "information"),
                        timeout=8 if ev.get("kind") in ("warn", "error") else 5)
        elif kind == "export":
            job = ev["job"]
            self.export_jobs[job["id"]] = job
            screens = [s for s in self.screen_stack if isinstance(s, ExportScreen) and s.job == job["id"]]
            for s in screens:
                s.on_job(job)
            if not screens and job["state"] != "running":
                if job["state"] == "done":
                    self.notify(f"{job['message']} in {job['folder']}", timeout=10)
                else:
                    self.notify(job["message"], severity="error", timeout=10)
        elif kind == "sources":
            for s in self.screen_stack:
                if isinstance(s, AddInputScreen):
                    s.action_refresh()

    # ---- helpers for widgets
    def ask(self, message, then, yes="OK", no="Cancel", danger=False, focus_yes=True):
        def done(ok):
            if ok:
                then()
        self.push_screen(ConfirmScreen(message, yes, no, danger, focus_yes), done)

    def prompt(self, title, value, then):
        def done(v):
            if v:
                then(v)
        self.push_screen(PromptScreen(title, value), done)

    def close(self, screen):
        """Closes a dialog when an engine call it started finishes, unless the
        user has already closed it or opened another on top."""
        if self.screen is screen:
            screen.dismiss(None)

    def opened_project(self):
        # take ids repeat between projects, so forget everything drawn for the old one
        tl = self.timeline
        tl.take_id = None
        tl.peaks.clear()
        tl.loading.clear()
        tl._env.clear()
        self.tracklist.index = 0

    # ---- actions
    def action_record(self):
        st = self.state
        if st is None:
            return
        if st["transport"]["recording"]:
            self.notify("Saving the recording…", timeout=3)
            self.bridge.call(self.engine.stop_recording)
        else:
            self.bridge.call(self.engine.start_recording)

    def action_play(self):
        st = self.state
        if st is None:
            return
        tr = st["transport"]
        if tr["recording"]:
            self.notify("Stop recording first", severity="warning")
            return
        if tr["playing"] or tr["paused"]:
            self.bridge.call(self.engine.stop_playback)
            return
        tl = self.timeline
        take = tl.take()
        if take is None:
            self.notify("Nothing to play yet: record a take first", severity="warning")
            return
        sel = tl.selection()
        pos = sel[0] if sel else tl.cursor
        if pos >= tl.length(take) - 0.05:
            pos = 0.0
        self.bridge.call(self.engine.play, take["id"], pos)

    def action_pause(self):
        tr = self.state["transport"] if self.state else None
        if tr and tr["playing"]:
            self.bridge.call(self.engine.pause)
        elif tr and tr["paused"]:
            self.bridge.call(self.engine.resume)

    def action_take(self, step):
        self.timeline.step_take(step)

    def action_add_input(self):
        if self.state is not None:
            self.push_screen(AddInputScreen())

    def action_export(self):
        take = self.timeline.take()
        if take is None or take["state"] == "recording":
            self.notify("Record a take first, then export it", severity="warning")
            return
        self.push_screen(ExportScreen(take["id"]))

    def action_projects(self):
        if self.state is not None:
            self.push_screen(ProjectsScreen())

    def action_output(self):
        if self.state is not None:
            self.push_screen(OutputScreen())

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_quit_app(self):
        if self.state and self.state["transport"]["recording"]:
            self.ask("You are still recording. Quitting stops the recording and saves it.", self.exit,
                     yes="Stop and quit", no="Keep recording", focus_yes=False)
        else:
            self.exit()


# --------------------------------------------------------------------- start

def run(quit_event, warnings=(), log_path=None, debug=False):
    """Runs the terminal interface until the user quits. Called by app.main()
    after the checks and the single-instance lock."""
    if not (os.isatty(0) and os.isatty(1)):
        print("The terminal interface needs a terminal. Run it as: audiomagic --tui", file=sys.stderr)
        return 1
    from .engine import Engine

    # Textual draws on sys.__stderr__. Point that at the terminal and send the
    # real stderr (PipeWire and GStreamer write there) to the log instead, so
    # stray messages can't scribble over the screen.
    saved_err = os.dup(2)
    term = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace")
    old_stderr = sys.__stderr__
    log_fd = None
    if log_path:
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 2)
    sys.__stderr__ = term
    log.info("AudioMagic %s starting in the terminal", __version__)
    engine = app = None
    try:
        engine = Engine()
        engine.start()
        app = AudioMagicTUI(engine, quit_event, warnings, log_path)
        app.run()
    finally:
        if app is not None:
            app.bridge.stop()
        if engine is not None:
            if engine.recorder is not None:
                try:
                    print("Stopping the recording and saving it…", file=term, flush=True)
                except OSError:
                    pass  # the terminal is gone (it was closed); save anyway
            engine.shutdown()
        sys.__stderr__ = old_stderr
        os.dup2(saved_err, 2)
        os.close(saved_err)
        if log_fd is not None:
            os.close(log_fd)
        try:
            term.close()
        except OSError:
            pass
    if app is not None and app.return_code:
        return app.return_code
    return 0


def log_file(cache_dir):
    path = os.path.join(cache_dir, "tui.log")
    # a fresh log each time, so it never grows without bound
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def setup_logging(path, debug):
    logging.basicConfig(filename=path, level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
