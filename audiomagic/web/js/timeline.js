// The timeline: waveforms of the selected take, the ruler, selection,
// cursor and playhead. Waveforms are drawn onto a base canvas only when the
// view changes; the moving parts are drawn on an overlay canvas.

import { clamp, fmtTime } from './util.js';

export const ROW_H = 144;
export const RULER_H = 30;
const BIN = 256;
const RATE = 48000;
const TICKS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export class Timeline {
  constructor(app, scrollEl, innerEl, stackEl, baseCanvas, overlayCanvas) {
    this.app = app;
    this.scroll = scrollEl;
    this.inner = innerEl;
    this.stack = stackEl;
    this.base = baseCanvas;
    this.over = overlayCanvas;
    this.pps = 40;
    this.tracks = [];
    this.take = null;
    this.live = false;
    this.lengthSec = 0;
    this.segments = [];
    this.peaks = new Map();
    this.selection = null;
    this.cursor = 0;
    this.playPos = null;
    this.follow = true;
    this.baseDirty = true;
    this.overDirty = true;
    this.width = 600;
    this.drag = null;
    this.colors = {};
    this.readColors();
    this.viewW = this.scroll.clientWidth;
    this._dims = {};
    new ResizeObserver((entries) => {
      this.viewW = entries[0].contentRect.width;
      this.layout();
    }).observe(this.scroll);
    this.scroll.addEventListener('scroll', () => { this.baseDirty = true; this.overDirty = true; });
    this.scroll.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
    this.over.addEventListener('pointerdown', (e) => this.onDown(e));
    this.over.addEventListener('pointermove', (e) => this.onMove(e));
    this.over.addEventListener('pointerup', (e) => this.onUp(e));
    this.over.addEventListener('pointercancel', () => { this.drag = null; });
    matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { this.readColors(); this.baseDirty = true; });
    const loop = () => {
      if (this.baseDirty) { this.baseDirty = false; this.drawBase(); this.overDirty = true; }
      if (this.overDirty) { this.overDirty = false; this.drawOverlay(); }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  readColors() {
    this.colors = {
      fade: css('--fade') || 'rgba(0,0,0,0.42)',
      bg: css('--lane-bg') || '#16191e', bg2: css('--lane-bg-2') || '#191c22', line: css('--line') || '#2b3038',
      text: css('--muted') || '#9aa3ae', ruler: css('--panel') || '#1a1d23', accent: css('--accent') || '#4f9cf9',
      rec: css('--rec') || '#ef4444', sel: css('--sel') || 'rgba(79,156,249,0.18)', end: css('--beyond') || 'rgba(0,0,0,0.35)',
    };
  }

  // ------------------------------------------------------------------ data
  setView({ tracks, take, live, liveSeconds }) {
    const takeChanged = (take && take.id) !== (this.take && this.take.id);
    const finishedRecording = this.live && !live;
    this.tracks = tracks;
    this.take = take;
    this.live = !!live;
    if (take && live) {
      const frames = Math.round((liveSeconds || 0) * RATE);
      this.segments = [[0, frames, 0]];
      this.lengthSec = frames / RATE;
    } else if (take) {
      this.segments = take.timeline.segments;
      this.lengthSec = take.timeline.length / RATE;
    } else {
      this.segments = [];
      this.lengthSec = 0;
    }
    if (takeChanged || finishedRecording) {
      this.selection = null;
      this.cursor = 0;
      if (!live) this.fit();
    }
    if (this.selection) {
      this.selection.start = clamp(this.selection.start, 0, this.lengthSec);
      this.selection.end = clamp(this.selection.end, 0, this.lengthSec);
      if (this.selection.end - this.selection.start < 0.001) this.selection = null;
    }
    this.cursor = clamp(this.cursor, 0, this.lengthSec);
    this.layout();
    // while recording, new waveform data (appendPeaks) triggers the redraws
    if (!this.live || takeChanged || finishedRecording) this.baseDirty = true;
  }

  setPeaks(takeId, trackId, arr) {
    this.peaks.set(`${takeId}:${trackId}`, { data: arr, n: arr.length });
    this.baseDirty = true;
  }

  appendPeaks(takeId, trackId, start, values) {
    const key = `${takeId}:${trackId}`;
    let p = this.peaks.get(key);
    const need = (start * 2) + values.length;
    if (!p) { p = { data: new Int8Array(Math.max(4096, need * 2)), n: 0 }; this.peaks.set(key, p); }
    if (need > p.data.length) {
      const grown = new Int8Array(Math.max(need, p.data.length * 2));
      grown.set(p.data.subarray(0, p.n));
      p.data = grown;
    }
    p.data.set(values, start * 2);
    p.n = Math.max(p.n, need);
    this.baseDirty = true;
  }

  hasPeaks(takeId, trackId) { return this.peaks.has(`${takeId}:${trackId}`); }

  dropTake(takeId) {
    for (const k of [...this.peaks.keys()]) if (k.startsWith(`${takeId}:`)) this.peaks.delete(k);
  }

  // ---------------------------------------------------------------- layout
  layout() {
    // Only touch the page when a size really changes: this runs on every
    // meter update while recording.
    const w = Math.max(200, Math.floor(this.viewW));
    const h = RULER_H + Math.max(1, this.tracks.length) * ROW_H;
    const contentW = Math.max(w, Math.ceil(this.lengthSec * this.pps) + 160);
    const d = this._dims;
    if (d.contentW !== contentW) {
      this.inner.style.width = `${contentW}px`;
      d.contentW = contentW;
    }
    const scrollH = h + (contentW > w ? 14 : 0);
    if (d.scrollH !== scrollH) {
      this.scroll.style.height = `${scrollH}px`;
      d.scrollH = scrollH;
    }
    const dpr = window.devicePixelRatio || 1;
    if (d.w !== w || d.h !== h || d.dpr !== dpr) {
      this.inner.style.height = `${h}px`;
      this.stack.style.width = `${w}px`;
      this.stack.style.height = `${h}px`;
      for (const c of [this.base, this.over]) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
        c.style.width = `${w}px`;
        c.style.height = `${h}px`;
      }
      Object.assign(d, { w, h, dpr });
      this.baseDirty = true;
      this.overDirty = true;
    }
    this.width = w;
    this.height = h;
  }

  minPps() { return Math.min(2, (this.width - 60) / Math.max(1, this.lengthSec)); }

  setZoom(pps, anchorX = this.width / 2) {
    const t = (this.scroll.scrollLeft + anchorX) / this.pps;
    this.pps = clamp(pps, this.minPps(), 4000);
    this.layout();
    this.scroll.scrollLeft = Math.max(0, t * this.pps - anchorX);
    this.baseDirty = true;
  }

  zoomBy(f, anchorX) { this.setZoom(this.pps * f, anchorX); }

  fit() {
    const len = Math.max(this.lengthSec, 5);
    this.pps = clamp((this.width - 60) / len, 0.02, 4000);
    this.layout();
    this.baseDirty = true;
    this.scroll.scrollLeft = 0;
  }

  xOf(t) { return t * this.pps - this.scroll.scrollLeft; }
  tOf(x) { return (x + this.scroll.scrollLeft) / this.pps; }

  scrollTo(t) {
    const x = this.xOf(t);
    if (x < 0 || x > this.width - 30) this.scroll.scrollLeft = Math.max(0, t * this.pps - this.width * 0.15);
  }

  // ----------------------------------------------------------------- input
  onWheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const r = this.scroll.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * 0.0022), e.clientX - r.left);
    } else if (e.shiftKey && Math.abs(e.deltaX) < Math.abs(e.deltaY)) {
      e.preventDefault();
      this.scroll.scrollLeft += e.deltaY;
    }
  }

  local(e) {
    const r = this.over.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  laneAt(y) {
    if (y < RULER_H) return -1;
    const i = Math.floor((y - RULER_H) / ROW_H);
    return i < this.tracks.length ? i : -1;
  }

  onDown(e) {
    if (e.button !== 0 || !this.take) return;
    const { x, y } = this.local(e);
    this.drag = { x0: x, t0: clamp(this.tOf(x), 0, this.lengthSec), lane: this.laneAt(y), moved: false, shift: e.shiftKey };
    this.over.setPointerCapture(e.pointerId);
    if (this.live) this.drag = null;
  }

  onMove(e) {
    const d = this.drag;
    if (!d) return;
    const { x } = this.local(e);
    if (!d.moved && Math.abs(x - d.x0) < 4) return;
    d.moved = true;
    if (x > this.width - 24) this.scroll.scrollLeft += Math.min(40, x - (this.width - 24));
    else if (x < 24) this.scroll.scrollLeft -= Math.min(40, 24 - x);
    const t = clamp(this.tOf(x), 0, this.lengthSec);
    const track = d.lane >= 0 ? this.tracks[d.lane] : null;
    this.selection = { start: Math.min(d.t0, t), end: Math.max(d.t0, t), track: track ? track.id : null };
    this.overDirty = true;
    this.app.onSelection(this.selection);
  }

  onUp() {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.moved) { this.app.onSelection(this.selection); return; }
    if (d.shift) {
      const a = Math.min(this.cursor, d.t0), b = Math.max(this.cursor, d.t0);
      const lane = d.lane >= 0 ? this.tracks[d.lane] : null;
      this.selection = b - a > 0.001 ? { start: a, end: b, track: lane ? lane.id : null } : null;
      this.app.onSelection(this.selection);
    } else {
      this.selection = null;
      this.cursor = d.t0;
      this.app.onSelection(null);
      this.app.onSeek(d.t0);
    }
    this.overDirty = true;
  }

  // ------------------------------------------------------------------ draw
  drawBase() {
    const ctx = this.base.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = this.width, h = this.height, c = this.colors;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    this.tracks.forEach((tr, i) => {
      const y = RULER_H + i * ROW_H;
      ctx.fillStyle = i % 2 ? c.bg2 : c.bg;
      ctx.fillRect(0, y, w, ROW_H);
      ctx.fillStyle = c.line;
      ctx.fillRect(0, y + ROW_H - 1, w, 1);
    });
    const take = this.take;
    if (take) {
      const endX = this.xOf(this.lengthSec);
      this.tracks.forEach((tr, i) => {
        const y = RULER_H + i * ROW_H;
        const hasFile = this.live ? tr.recording : take.tracks.some((t) => t.track_id === tr.id);
        const pk = this.peaks.get(`${take.id}:${tr.id}`);
        if (hasFile && pk) this.drawWave(ctx, pk, tr, y + 10, ROW_H - 20);
        else if (!hasFile) {
          ctx.fillStyle = c.text;
          ctx.globalAlpha = 0.55;
          ctx.font = '12px system-ui, sans-serif';
          ctx.fillText('No audio for this input in this take', 14, y + ROW_H / 2 + 4);
          ctx.globalAlpha = 1;
        }
      });
      if (!this.live) this.drawFades(ctx);
      if (endX < w) {
        ctx.fillStyle = c.end;
        ctx.fillRect(Math.max(0, endX), RULER_H, w - Math.max(0, endX), h - RULER_H);
      }
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = c.text;
      ctx.globalAlpha = 0.7;
      for (let i = 1; i < this.segments.length; i++) {
        const x = Math.round(this.xOf(this.segments[i][2] / RATE)) + 0.5;
        if (x < 0 || x > w) continue;
        ctx.beginPath();
        ctx.moveTo(x, RULER_H);
        ctx.lineTo(x, h);
        ctx.stroke();
      }
      ctx.restore();
    }
    this.drawRuler(ctx);
  }

  drawWave(ctx, pk, tr, y, hgt) {
    const take = this.take;
    const data = pk.data, nbins = pk.n / 2;
    const mid = y + hgt / 2, half = hgt / 2;
    const gainDb = (!this.live && take.edits.clip_gain_db[tr.id]) || 0;
    const sq = Math.sqrt(Math.pow(10, gainDb / 20));
    const silences = (!this.live && take.edits.silences[tr.id]) || [];
    const normal = new Path2D(), dim = new Path2D();
    const segs = this.segments;
    const x0 = Math.max(0, Math.floor(this.xOf(0)));
    const x1 = Math.min(this.width, Math.ceil(this.xOf(this.lengthSec)));
    const sl = this.scroll.scrollLeft, pps = this.pps;
    let si = 0;
    for (let px = x0; px < x1; px++) {
      const oa = ((sl + px) / pps) * RATE;
      const ob = Math.min(((sl + px + 1) / pps), this.lengthSec) * RATE;
      let mn = 127, mx = -128, found = false, srcMid = -1;
      while (si > 0 && segs[si][2] > oa) si--;
      for (let s = si; s < segs.length; s++) {
        const [a, b, o] = segs[s];
        const eo = o + (b - a);
        if (o >= ob) break;
        if (eo <= oa) { si = s + 1 < segs.length ? s + 1 : s; continue; }
        const lo = Math.max(oa, o), hi = Math.min(ob, eo);
        const sa = a + lo - o, sb = a + hi - o;
        if (srcMid < 0) srcMid = (sa + sb) / 2;
        let i0 = Math.floor(sa / BIN);
        const i1 = Math.min(nbins, Math.max(i0 + 1, Math.ceil(sb / BIN)));
        for (; i0 < i1; i0++) {
          const lo8 = data[2 * i0], hi8 = data[2 * i0 + 1];
          if (lo8 < mn) mn = lo8;
          if (hi8 > mx) mx = hi8;
          found = true;
        }
      }
      if (!found) continue;
      const vmin = clamp((mn / 127) * sq, -1, 1), vmax = clamp((mx / 127) * sq, -1, 1);
      let quiet = false;
      for (const [s0, s1] of silences) if (srcMid >= s0 && srcMid < s1) { quiet = true; break; }
      const top = mid - vmax * half;
      (quiet ? dim : normal).rect(px, top, 1, Math.max(1, (vmax - vmin) * half));
    }
    ctx.fillStyle = tr.color;
    ctx.globalAlpha = 0.9;
    ctx.fill(normal);
    ctx.globalAlpha = 0.18;
    ctx.fill(dim);
    ctx.globalAlpha = 1;
  }

  drawFades(ctx) {
    const e = this.take.edits;
    const len = this.lengthSec;
    const shade = (t0, t1, env) => {
      const xa = Math.max(0, this.xOf(t0)), xb = Math.min(this.width, this.xOf(t1));
      if (xb <= xa) return;
      this.tracks.forEach((tr, i) => {
        if (!this.take.tracks.some((f) => f.track_id === tr.id)) return;
        const y = RULER_H + i * ROW_H + 10, hgt = ROW_H - 20, mid = y + hgt / 2;
        ctx.beginPath();
        ctx.moveTo(xa, y);
        for (let x = xa; x <= xb; x += 2) ctx.lineTo(x, mid - env(this.tOf(x)) * hgt / 2);
        ctx.lineTo(xb, y);
        ctx.closePath();
        ctx.moveTo(xa, y + hgt);
        for (let x = xa; x <= xb; x += 2) ctx.lineTo(x, mid + env(this.tOf(x)) * hgt / 2);
        ctx.lineTo(xb, y + hgt);
        ctx.closePath();
        ctx.fillStyle = this.colors.fade;
        ctx.fill();
      });
    };
    if (e.fade_in > 0) shade(0, Math.min(e.fade_in, len), (t) => Math.sin(Math.PI / 2 * clamp(t / e.fade_in, 0, 1)));
    if (e.fade_out > 0) shade(Math.max(0, len - e.fade_out), len, (t) => Math.sin(Math.PI / 2 * clamp((len - t) / e.fade_out, 0, 1)));
  }

  drawRuler(ctx) {
    const c = this.colors, w = this.width;
    ctx.fillStyle = c.ruler;
    ctx.fillRect(0, 0, w, RULER_H);
    ctx.fillStyle = c.line;
    ctx.fillRect(0, RULER_H - 1, w, 1);
    const step = TICKS.find((s) => s * this.pps >= 90) || 3600;
    const minor = step / (step >= 60 && step % 60 === 0 && step < 600 ? 4 : 5);
    const t0 = this.tOf(0), t1 = this.tOf(w);
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    const per = Math.round(step / minor);
    for (let i = Math.max(0, Math.floor(t0 / minor)); i * minor <= t1; i++) {
      const t = i * minor;
      const x = Math.round(this.xOf(t)) + 0.5;
      const major = i % per === 0;
      ctx.fillStyle = c.text;
      ctx.globalAlpha = major ? 0.9 : 0.4;
      ctx.fillRect(x, major ? RULER_H - 12 : RULER_H - 6, 1, major ? 12 : 6);
      if (major) {
        ctx.globalAlpha = 0.9;
        ctx.fillText(fmtTime(t, step < 1), x + 4, 11);
      }
    }
    ctx.globalAlpha = 1;
    if (this.take) {
      for (let i = 1; i < this.segments.length; i++) {
        const x = this.xOf(this.segments[i][2] / RATE);
        if (x < -6 || x > w + 6) continue;
        ctx.fillStyle = c.accent;
        ctx.beginPath();
        ctx.moveTo(x - 5, RULER_H - 1);
        ctx.lineTo(x + 5, RULER_H - 1);
        ctx.lineTo(x, RULER_H - 8);
        ctx.fill();
      }
    }
  }

  drawOverlay() {
    const ctx = this.over.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = this.width, h = this.height, c = this.colors;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!this.take) return;
    const s = this.selection;
    if (s) {
      const xa = this.xOf(s.start), xb = this.xOf(s.end);
      ctx.fillStyle = c.sel;
      ctx.fillRect(xa, 0, xb - xa, h);
      const lane = this.tracks.findIndex((t) => t.id === s.track);
      if (lane >= 0) {
        ctx.fillStyle = c.sel;
        ctx.fillRect(xa, RULER_H + lane * ROW_H, xb - xa, ROW_H - 1);
      }
      ctx.fillStyle = c.accent;
      ctx.fillRect(Math.round(xa), 0, 1, h);
      ctx.fillRect(Math.round(xb), 0, 1, h);
    }
    if (!this.live) {
      const xc = Math.round(this.xOf(this.cursor)) + 0.5;
      if (xc >= 0 && xc <= w) {
        ctx.save();
        ctx.strokeStyle = c.text;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(xc, 0);
        ctx.lineTo(xc, h);
        ctx.stroke();
        ctx.restore();
      }
    }
    let head = null, color = c.accent;
    if (this.live) { head = this.lengthSec; color = c.rec; }
    else if (this.playPos !== null) head = this.playPos;
    if (head !== null) {
      if (this.follow && !this.drag) {
        const x = this.xOf(head);
        if (x > w - 40 || x < 0) {
          this.scroll.scrollLeft = Math.max(0, head * this.pps - (this.live ? w - 120 : 40));
          this.baseDirty = true;
        }
      }
      const x = Math.round(this.xOf(head));
      ctx.fillStyle = color;
      ctx.fillRect(x - 1, 0, 2, h);
      ctx.beginPath();
      ctx.moveTo(x - 6, 0);
      ctx.lineTo(x + 6, 0);
      ctx.lineTo(x, 8);
      ctx.fill();
    }
  }
}
