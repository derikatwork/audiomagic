"""Non-destructive edits for a take.

All ranges are stored in *source* frames (positions in the recorded files),
so the raw recordings are never modified. The UI works in *output* frames
(positions on the edited timeline); the helpers here translate between the
two.
"""

import copy


def merge_ranges(ranges):
    out = []
    for a, b in sorted((int(a), int(b)) for a, b in ranges if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def subtract_ranges(base, remove):
    """[a, b) minus a list of ranges -> list of kept ranges."""
    kept = []
    a, b = base
    for ra, rb in merge_ranges(remove):
        if rb <= a or ra >= b:
            continue
        if ra > a:
            kept.append((a, ra))
        a = max(a, rb)
        if a >= b:
            break
    if a < b:
        kept.append((a, b))
    return kept


class Edits:
    def __init__(self, data=None):
        data = data or {}
        self.trim_start = int(data.get("trim_start", 0))
        te = data.get("trim_end")
        self.trim_end = None if te is None else int(te)
        self.cuts = merge_ranges(data.get("cuts", []))
        self.silences = {k: merge_ranges(v) for k, v in data.get("silences", {}).items() if v}
        self.fade_in = float(data.get("fade_in", 0.0))
        self.fade_out = float(data.get("fade_out", 0.0))
        self.clip_gain_db = {k: float(v) for k, v in data.get("clip_gain_db", {}).items()}

    def to_dict(self):
        return {
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
            "cuts": copy.deepcopy(self.cuts),
            "silences": copy.deepcopy(self.silences),
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "clip_gain_db": dict(self.clip_gain_db),
        }

    def copy(self):
        return Edits(self.to_dict())

    def is_empty(self):
        return self.to_dict() == Edits().to_dict()


class Timeline:
    """The kept pieces of a take, in order, with output positions."""

    def __init__(self, edits, duration):
        self.edits = edits
        self.duration = int(duration)
        start = max(0, min(edits.trim_start, self.duration))
        end = self.duration if edits.trim_end is None else max(start, min(edits.trim_end, self.duration))
        self.segments = subtract_ranges((start, end), edits.cuts)
        self.out_starts = []
        pos = 0
        for a, b in self.segments:
            self.out_starts.append(pos)
            pos += b - a
        self.length = pos

    def to_dict(self):
        return {"length": self.length, "segments": [[a, b, o] for (a, b), o in zip(self.segments, self.out_starts)]}

    def src_ranges(self, out_a, out_b):
        """Source ranges covering output frames [out_a, out_b)."""
        out_a = max(0, int(out_a))
        out_b = min(self.length, int(out_b))
        res = []
        for (a, b), o in zip(self.segments, self.out_starts):
            oa, ob = o, o + (b - a)
            lo, hi = max(out_a, oa), min(out_b, ob)
            if hi > lo:
                res.append((a + lo - oa, a + hi - oa))
        return res

    def pieces(self, out_a, n):
        """Yield (dst_offset, src_start, length, seg_index) for output [out_a, out_a+n)."""
        out_b = out_a + n
        for i, ((a, b), o) in enumerate(zip(self.segments, self.out_starts)):
            oa, ob = o, o + (b - a)
            lo, hi = max(out_a, oa), min(out_b, ob)
            if hi > lo:
                yield lo - out_a, a + lo - oa, hi - lo, i


# --------------------------------------------------------------- operations

def op_cut(edits, duration, out_a, out_b):
    tl = Timeline(edits, duration)
    e = edits.copy()
    e.cuts = merge_ranges(e.cuts + [list(r) for r in tl.src_ranges(out_a, out_b)])
    return e


def op_trim(edits, duration, out_a, out_b):
    """Keep only the selected part of the timeline."""
    tl = Timeline(edits, duration)
    rs = tl.src_ranges(out_a, out_b)
    if not rs:
        return edits.copy()
    e = edits.copy()
    e.trim_start = rs[0][0]
    e.trim_end = rs[-1][1]
    e.cuts = [c for c in e.cuts if c[1] > e.trim_start and c[0] < e.trim_end]
    return e


def op_silence(edits, duration, track_id, out_a, out_b):
    tl = Timeline(edits, duration)
    e = edits.copy()
    cur = e.silences.get(track_id, [])
    e.silences[track_id] = merge_ranges(cur + [list(r) for r in tl.src_ranges(out_a, out_b)])
    return e


def op_unsilence(edits, duration, track_id, out_a, out_b):
    tl = Timeline(edits, duration)
    e = edits.copy()
    cur = e.silences.get(track_id, [])
    remove = tl.src_ranges(out_a, out_b)
    kept = []
    for a, b in cur:
        kept.extend(list(r) for r in subtract_ranges((a, b), remove))
    if kept:
        e.silences[track_id] = merge_ranges(kept)
    else:
        e.silences.pop(track_id, None)
    return e


class History:
    """Undo/redo stacks of Edits snapshots."""

    LIMIT = 200

    def __init__(self):
        self.undo_stack = []
        self.redo_stack = []

    def push(self, before):
        self.undo_stack.append(before.to_dict())
        del self.undo_stack[:-self.LIMIT]
        self.redo_stack.clear()

    def undo(self, current):
        if not self.undo_stack:
            return None
        self.redo_stack.append(current.to_dict())
        return Edits(self.undo_stack.pop())

    def redo(self, current):
        if not self.redo_stack:
            return None
        self.undo_stack.append(current.to_dict())
        return Edits(self.redo_stack.pop())
