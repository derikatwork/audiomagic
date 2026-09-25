// AudioMagic front end: glue between the server state and the UI.

import { api, connect } from './api.js';
import { Dialogs } from './dialogs.js';
import { Timeline } from './timeline.js';
import { Meter, TrackHeader } from './tracks.js';
import { clamp, el, fmtBytes, fmtDb, fmtDuration, fmtTime, icon, throttle } from './util.js';

const $ = (sel) => document.querySelector(sel);
const RATE = 48000;

class App {
  constructor() {
    this.state = null;
    this.handlers = new Map();
    this.headers = new Map();
    this.takeId = null;
    this.selection = null;
    this.meters = null;
    this.loadingPeaks = new Set();
    this.pendingSelect = null;
    this.dialogs = new Dialogs(this);
    this.timeline = new Timeline(this, $('#lanes'), $('#lanes-inner'), $('#canvas-stack'), $('#wave'), $('#overlay'));
    this.masterMeter = new Meter('master');
    $('#master-meter').append(this.masterMeter.root);
    this.decorate();
    this.bind();
    connect((m) => this.onMessage(m), (ok) => { $('#offline').hidden = ok; });
  }

  // ---------------------------------------------------------------- events
  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(fn);
    return () => this.handlers.get(type).delete(fn);
  }

  fire(type, msg) {
    for (const fn of this.handlers.get(type) || []) {
      try { fn(msg); } catch (e) { console.error(e); }
    }
  }

  onMessage(msg) {
    switch (msg.type) {
      case 'state': this.setState(msg.state); break;
      case 'meters': this.onMeters(msg); break;
      case 'recpeaks': {
        const tk = this.state && this.state.transport.rec_take;
        if (tk) for (const [tid, p] of Object.entries(msg.tracks)) this.timeline.appendPeaks(tk, tid, p.start, p.data);
        break;
      }
      case 'recorded':
        this.timeline.dropTake(msg.take);
        this.pendingSelect = msg.take;
        if (this.state) this.setState(this.state);
        break;
      case 'ended':
        this.timeline.playPos = null;
        this.timeline.overDirty = true;
        break;
      case 'toast': this.toast(msg.text, msg.kind); break;
      default: break;
    }
    this.fire(msg.type, msg);
  }

  // ----------------------------------------------------------------- setup
  decorate() {
    $('#help-btn').append(icon('help'));
    $('#output-icon').append(icon('speaker'));
    $('#play-btn').append(icon('play'));
    $('#stop-btn').append(icon('stop'));
    $('#take-rename').append(icon('edit'));
    $('#take-delete').append(icon('trash'));
    $('#export-btn').append(icon('export'), 'Export…');
    $('#cut-btn').append(icon('cut'), 'Cut');
    $('#silence-btn').append(icon('mute'), el('span', { id: 'silence-label' }, 'Silence'));
    $('#trim-btn').append(icon('trim'), 'Keep selection');
    $('#undo-btn').append(icon('undo'));
    $('#redo-btn').append(icon('redo'));
    $('#zoom-in').append(icon('zoomin'));
    $('#zoom-out').append(icon('zoomout'));
    $('#zoom-fit').append(icon('fit'));
    $('#add-input').append(icon('plus'), 'Add input');
  }

  bind() {
    $('#project-btn').onclick = () => this.dialogs.projects();
    $('#help-btn').onclick = () => this.dialogs.help();
    $('#empty-help').onclick = () => this.dialogs.help();
    $('#add-input').onclick = () => this.dialogs.addInput();
    $('#empty-add').onclick = () => this.dialogs.addInput();
    $('#rec-btn').onclick = () => this.toggleRecord();
    $('#play-btn').onclick = () => this.togglePlay();
    $('#stop-btn').onclick = () => this.stop();
    $('#export-btn').onclick = () => this.takeId && this.dialogs.exportTake(this.takeId);
    $('#take-select').onchange = (e) => this.selectTake(e.target.value);
    $('#take-rename').onclick = () => this.renameTake();
    $('#take-delete').onclick = () => this.deleteTake();
    $('#cut-btn').onclick = () => this.edit('cut');
    $('#silence-btn').onclick = () => this.edit('silence');
    $('#trim-btn').onclick = () => this.edit('trim');
    $('#undo-btn').onclick = () => this.edit('undo');
    $('#redo-btn').onclick = () => this.edit('redo');
    $('#zoom-in').onclick = () => this.timeline.zoomBy(1.6);
    $('#zoom-out').onclick = () => this.timeline.zoomBy(1 / 1.6);
    $('#zoom-fit').onclick = () => this.timeline.fit();
    const fades = () => this.edit('fades', { fade_in: parseFloat($('#fade-in').value) || 0, fade_out: parseFloat($('#fade-out').value) || 0 });
    $('#fade-in').onchange = fades;
    $('#fade-out').onchange = fades;
    $('#output-select').onchange = (e) => api('POST', '/output', { node: e.target.value || null }).catch((err) => this.toast(err.message, 'error'));
    const sendMaster = throttle((v) => api('POST', '/master', { gain_db: v }).catch(() => {}), 80);
    const mg = $('#master-gain');
    mg.addEventListener('pointerdown', () => { this.masterDrag = true; });
    mg.addEventListener('pointerup', () => { this.masterDrag = false; });
    mg.addEventListener('input', () => { $('#master-val').textContent = fmtDb(parseFloat(mg.value)); sendMaster(parseFloat(mg.value)); });
    mg.addEventListener('dblclick', () => { mg.value = 0; mg.dispatchEvent(new Event('input')); });
    document.addEventListener('keydown', (e) => this.onKey(e));
    // Space is always play/pause: stop it from also "clicking" a focused button
    document.addEventListener('keyup', (e) => {
      if (e.key === ' ' && this.isShortcutTarget(e)) e.preventDefault();
    });
  }

  isShortcutTarget(e) {
    const t = e.target;
    const tag = (t && t.tagName) || '';
    if (tag === 'SELECT' || tag === 'TEXTAREA') return false;
    if (tag === 'INPUT' && !['range', 'checkbox'].includes(t.type)) return false;
    return !document.querySelector('#modal-root .backdrop');
  }

  // ---------------------------------------------------------------- state
  setState(st) {
    this.state = st;
    const tr = st.transport;
    document.body.classList.toggle('recording', tr.recording);
    $('#project-name').textContent = st.project.name;
    document.title = `${st.project.name} · AudioMagic`;
    this.renderDisk(st);
    this.renderOutputs(st);
    this.renderTracks(st);
    this.renderTakes(st);
    this.renderTransport(st);
    if (!this.masterDrag) {
      $('#master-gain').value = st.project.master_gain_db;
      $('#master-val').textContent = fmtDb(st.project.master_gain_db);
    }
    const status = $('#status');
    if (!st.pipewire.ok) status.textContent = `PipeWire problem: ${st.pipewire.error || 'not reachable'}`;
    else if (st.effects_overloaded) status.textContent = 'Effects are using more CPU than this computer has: meters and monitoring may stutter. Recording is not affected. Turning off noise suppression on some tracks helps.';
    else status.textContent = '';
    status.classList.toggle('warn', !!st.effects_overloaded);
    this.fire('state', st);
  }

  renderDisk(st) {
    const d = st.disk;
    if (d.free === null) { $('#disk').textContent = ''; return; }
    const hours = d.bytes_per_sec ? d.free / d.bytes_per_sec / 3600 : null;
    $('#disk').textContent = `${fmtBytes(d.free)} free${hours !== null ? ` · room for ${hours >= 100 ? Math.round(hours) : hours.toFixed(1)} h` : ''}`;
    $('#disk').classList.toggle('warn', hours !== null && hours < 1);
  }

  renderOutputs(st) {
    const sel = $('#output-select');
    const opts = [['', 'System default output'], ...st.outputs.map((o) => [o.node, o.label])];
    if (st.output && !st.outputs.some((o) => o.node === st.output)) opts.push([st.output, `${st.output} (not connected)`]);
    const sig = JSON.stringify([opts, st.output]);
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.replaceChildren(...opts.map(([v, l]) => el('option', { value: v, selected: (st.output || '') === v }, l)));
  }

  renderTracks(st) {
    const box = $('#track-headers');
    const seen = new Set();
    st.tracks.forEach((t, i) => {
      seen.add(t.id);
      let h = this.headers.get(t.id);
      if (!h) {
        h = new TrackHeader(this, t);
        this.headers.set(t.id, h);
      } else h.update(t);
      if (box.children[i] !== h.root) box.insertBefore(h.root, box.children[i] || null);
    });
    for (const [id, h] of this.headers) {
      if (!seen.has(id)) { h.root.remove(); this.headers.delete(id); }
    }
    const none = st.tracks.length === 0;
    $('#empty').hidden = !none;
    $('#workspace').classList.toggle('is-empty', none);
    $('#add-input').disabled = st.transport.recording;
  }

  renderTakes(st) {
    const tr = st.transport;
    const takes = st.takes.filter((t) => t.state !== 'recording');
    if (tr.recording) this.takeId = tr.rec_take;
    else if (this.pendingSelect && takes.some((t) => t.id === this.pendingSelect)) { this.takeId = this.pendingSelect; this.pendingSelect = null; }
    else if (!takes.some((t) => t.id === this.takeId)) this.takeId = takes.length ? takes[takes.length - 1].id : null;
    const sel = $('#take-select');
    const opts = takes.map((t) => [t.id, `${t.name} · ${fmtTime(t.timeline.length / RATE, false)}`]);
    if (tr.recording) opts.push([tr.rec_take, 'Recording…']);
    const sig = JSON.stringify([opts, this.takeId]);
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.replaceChildren(...(opts.length ? opts : [['', 'No takes yet']]).map(([v, l]) => el('option', { value: v, selected: v === this.takeId }, l)));
    }
    const take = st.takes.find((t) => t.id === this.takeId) || null;
    const editable = take && !tr.recording;
    sel.disabled = tr.recording || !takes.length;
    $('#take-rename').disabled = !editable;
    $('#take-delete').disabled = !editable;
    $('#export-btn').disabled = !editable;
    $('#undo-btn').disabled = !(editable && take.can_undo);
    $('#redo-btn').disabled = !(editable && take.can_redo);
    for (const id of ['#fade-in', '#fade-out']) $(id).disabled = !editable;
    if (take && document.activeElement !== $('#fade-in')) $('#fade-in').value = take.edits.fade_in;
    if (take && document.activeElement !== $('#fade-out')) $('#fade-out').value = take.edits.fade_out;
    this.timeline.setView({ tracks: st.tracks, take, live: tr.recording, liveSeconds: this.meters ? this.meters.rec : 0 });
    if (take && !tr.recording) {
      for (const f of take.tracks) this.loadPeaks(take.id, f.track_id);
    }
    const hint = $('#lanes-hint');
    if (st.tracks.length && !take) {
      hint.hidden = false;
      hint.replaceChildren(el('div', {}, el('strong', {}, 'Ready to record.'), ' Check the meters, then press ', el('span', { class: 'kbd-rec' }, '● Record'), ' or the R key.'));
    } else hint.hidden = true;
    this.onSelection(this.timeline.selection);
  }

  renderTransport(st) {
    const tr = st.transport;
    const rec = $('#rec-btn');
    rec.classList.toggle('active', tr.recording);
    rec.querySelector('.rec-label').textContent = tr.recording ? 'Stop recording' : 'Record';
    rec.disabled = !tr.recording && !st.tracks.some((t) => t.armed);
    rec.title = rec.disabled ? 'Arm at least one input (R button) to record' : 'Record (R)';
    const play = $('#play-btn');
    const playingThis = tr.play_take === this.takeId && tr.playing;
    play.replaceChildren(icon(playingThis ? 'pause' : 'play'));
    play.classList.toggle('active', playingThis);
    play.disabled = tr.recording || !this.takeId;
    $('#stop-btn').disabled = !tr.recording && !tr.play_take;
    if (!tr.play_take) { this.timeline.playPos = null; this.timeline.overDirty = true; }
  }

  async loadPeaks(takeId, trackId) {
    const key = `${takeId}:${trackId}`;
    if (this.timeline.hasPeaks(takeId, trackId) || this.loadingPeaks.has(key)) return;
    this.loadingPeaks.add(key);
    try {
      const buf = await api('GET', `/takes/${takeId}/peaks/${trackId}`);
      this.timeline.setPeaks(takeId, trackId, new Int8Array(buf));
    } catch (e) {
      console.warn('peaks', e);
    } finally {
      this.loadingPeaks.delete(key);
    }
  }

  onMeters(m) {
    this.meters = m;
    for (const [id, h] of this.headers) h.meter.set(m.tracks[id]);
    this.masterMeter.set(m.master);
    const st = this.state;
    if (!st) return;
    const tr = st.transport;
    let clock;
    if (tr.recording && m.rec !== null) {
      clock = m.rec;
      this.timeline.setView({ tracks: st.tracks, take: st.takes.find((t) => t.id === tr.rec_take), live: true, liveSeconds: m.rec });
      this.timeline.overDirty = true;
    } else if (m.pos !== null && tr.play_take === this.takeId) {
      clock = m.pos;
      this.timeline.playPos = m.pos;
      this.timeline.overDirty = true;
    } else {
      clock = this.timeline.cursor;
    }
    $('#clock').textContent = fmtTime(clock);
  }

  // ------------------------------------------------------------- transport
  async toggleRecord() {
    try {
      if (this.state.transport.recording) await api('POST', '/record/stop');
      else {
        this.timeline.follow = true;
        await api('POST', '/record/start');
      }
    } catch (e) { this.toast(e.message, 'error'); }
  }

  async togglePlay() {
    const tr = this.state.transport;
    if (!this.takeId || tr.recording) return;
    try {
      if (tr.play_take === this.takeId && tr.playing) await api('POST', '/pause');
      else if (tr.play_take === this.takeId && tr.paused) await api('POST', '/resume');
      else {
        const take = this.state.takes.find((t) => t.id === this.takeId);
        let pos = this.timeline.selection ? this.timeline.selection.start : this.timeline.cursor;
        if (take && pos >= take.timeline.length / RATE - 0.05) pos = 0;
        this.timeline.follow = true;
        await api('POST', '/play', { take: this.takeId, pos });
      }
    } catch (e) { this.toast(e.message, 'error'); }
  }

  async stop() {
    const tr = this.state.transport;
    try {
      if (tr.recording) await api('POST', '/record/stop');
      else if (tr.play_take) await api('POST', '/stop');
    } catch (e) { this.toast(e.message, 'error'); }
    this.timeline.playPos = null;
    this.timeline.overDirty = true;
  }

  onSeek(t) {
    this.timeline.cursor = t;
    $('#clock').textContent = fmtTime(t);
    const tr = this.state.transport;
    if (tr.play_take === this.takeId && tr.playing) {
      api('POST', '/play', { take: this.takeId, pos: t }).catch((e) => this.toast(e.message, 'error'));
    } else if (tr.play_take) {
      api('POST', '/stop').catch(() => {});
    }
  }

  onSelection(sel) {
    this.selection = sel;
    const has = !!(sel && sel.end - sel.start > 0.001) && this.state && !this.state.transport.recording;
    $('#cut-btn').disabled = !has;
    $('#trim-btn').disabled = !has;
    const trk = has && sel.track ? this.state.tracks.find((t) => t.id === sel.track) : null;
    const take = this.state && this.state.takes.find((t) => t.id === this.takeId);
    const trkHasAudio = trk && take && take.tracks.some((f) => f.track_id === trk.id);
    $('#silence-btn').disabled = !trkHasAudio;
    $('#silence-label').textContent = trk ? `Silence “${trk.name}”` : 'Silence';
    $('#sel-info').textContent = has
      ? `Selected ${fmtTime(sel.start)} – ${fmtTime(sel.end)} (${fmtDuration(sel.end - sel.start)})`
      : 'Drag across the waveform to select';
  }

  // ------------------------------------------------------------------ takes
  selectTake(id) {
    if (!id || id === this.takeId) return;
    this.takeId = id;
    if (this.state.transport.play_take) api('POST', '/stop').catch(() => {});
    this.setState(this.state);
    this.timeline.fit();
  }

  async renameTake() {
    const take = this.state.takes.find((t) => t.id === this.takeId);
    if (!take) return;
    const name = await this.dialogs.prompt('Rename take', 'Name', take.name);
    if (name) api('PATCH', `/takes/${take.id}`, { name }).catch((e) => this.toast(e.message, 'error'));
  }

  async deleteTake() {
    const take = this.state.takes.find((t) => t.id === this.takeId);
    if (!take) return;
    const ok = await this.dialogs.confirm('Delete take?', `“${take.name}” and its audio files will be moved to the trash.`, 'Delete take', true);
    if (!ok) return;
    try {
      await api('DELETE', `/takes/${take.id}`);
      this.timeline.dropTake(take.id);
    } catch (e) { this.toast(e.message, 'error'); }
  }

  async edit(op, extra = {}) {
    if (!this.takeId || this.state.transport.recording) return;
    const sel = this.timeline.selection;
    const body = { op, ...extra };
    if (['cut', 'trim', 'silence'].includes(op)) {
      if (!sel) { this.toast('Drag across the waveform to select something first', 'info'); return; }
      body.start = sel.start;
      body.end = sel.end;
      if (op === 'silence') body.track = sel.track;
    }
    try {
      await api('POST', `/takes/${this.takeId}/edit`, body);
      if (op === 'cut') { this.timeline.cursor = sel.start; this.timeline.selection = null; }
      if (op === 'trim') { this.timeline.cursor = 0; this.timeline.selection = null; this.timeline.fit(); }
      this.onSelection(this.timeline.selection);
      this.timeline.overDirty = true;
    } catch (e) { this.toast(e.message, 'error'); }
  }

  // ------------------------------------------------------------ track menu
  trackMenu(trackId, anchor) {
    const t = this.state.tracks.find((x) => x.id === trackId);
    if (!t) return;
    const take = this.state.takes.find((x) => x.id === this.takeId);
    const inTake = take && take.tracks.some((f) => f.track_id === trackId) && !this.state.transport.recording;
    const clipGain = take && take.edits.clip_gain_db[trackId];
    const idx = this.state.tracks.indexOf(t);
    const recording = this.state.transport.recording;
    const items = [
      ['Change input…', () => this.dialogs.addInput(trackId), recording],
      ['Effects…', () => this.dialogs.fx(trackId)],
      null,
      ['Normalize in this take', () => this.edit('normalize', { track: trackId }), !inTake],
      [clipGain ? `Reset level in this take (${fmtDb(clipGain)})` : 'Reset level in this take', () => this.edit('clear_gain', { track: trackId }), !clipGain || !inTake],
      null,
      ['Move up', () => api('POST', `/tracks/${trackId}/move`, { index: idx - 1 }), idx === 0],
      ['Move down', () => api('POST', `/tracks/${trackId}/move`, { index: idx + 1 }), idx === this.state.tracks.length - 1],
      null,
      ['Remove input', () => this.removeTrack(t), recording, true],
    ];
    this.menu(anchor, items);
  }

  menu(anchor, items) {
    document.querySelectorAll('.menu').forEach((m) => m.remove());
    const menu = el('div', { class: 'menu', role: 'menu' });
    for (const it of items) {
      if (!it) { menu.append(el('div', { class: 'menu-sep' })); continue; }
      const [label, fn, disabled, danger] = it;
      menu.append(el('button', { class: `menu-item${danger ? ' danger' : ''}`, role: 'menuitem', type: 'button', disabled: !!disabled,
        onclick: () => { close(); fn(); } }, label));
    }
    document.body.append(menu);
    const r = anchor.getBoundingClientRect();
    const mh = menu.offsetHeight, mw = menu.offsetWidth;
    menu.style.left = `${clamp(r.left, 8, innerWidth - mw - 8)}px`;
    menu.style.top = `${r.bottom + 4 + mh > innerHeight ? r.top - mh - 4 : r.bottom + 4}px`;
    const close = () => { menu.remove(); document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', esc, true); };
    const outside = (e) => { if (!menu.contains(e.target)) close(); };
    const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', esc, true);
    const first = menu.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  async removeTrack(t) {
    try {
      await api('DELETE', `/tracks/${t.id}`);
    } catch (e) {
      if (!e.confirm) { this.toast(e.message, 'error'); return; }
      const ok = await this.dialogs.confirm(`Remove “${t.name}”?`, e.message, 'Remove', true);
      if (ok) api('DELETE', `/tracks/${t.id}?confirm=1`).catch((err) => this.toast(err.message, 'error'));
    }
  }

  // --------------------------------------------------------------- keyboard
  onKey(e) {
    if (!this.isShortcutTarget(e) || !this.state) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const k = e.key;
    if (k === ' ') { e.preventDefault(); this.togglePlay(); }
    else if ((k === 'r' || k === 'R') && !ctrl) { e.preventDefault(); if (!$('#rec-btn').disabled) this.toggleRecord(); }
    else if ((k === 'Delete' || k === 'Backspace') && this.timeline.selection) { e.preventDefault(); this.edit('cut'); }
    else if (ctrl && (k === 'z' || k === 'Z')) { e.preventDefault(); this.edit(e.shiftKey ? 'redo' : 'undo'); }
    else if (ctrl && (k === 'y' || k === 'Y')) { e.preventDefault(); this.edit('redo'); }
    else if (k === 'Home') { e.preventDefault(); this.onSeek(0); this.timeline.scrollTo(0); this.timeline.overDirty = true; }
    else if (k === 'End') { e.preventDefault(); const t = this.timeline.lengthSec; this.onSeek(t); this.timeline.scrollTo(t); this.timeline.overDirty = true; }
    else if (k === '+' || k === '=') { e.preventDefault(); this.timeline.zoomBy(1.6); }
    else if (k === '-' || k === '_') { e.preventDefault(); this.timeline.zoomBy(1 / 1.6); }
    else if ((k === 'f' || k === 'F') && !ctrl) { e.preventDefault(); this.timeline.fit(); }
    else if (k === 'Escape') { this.timeline.selection = null; this.onSelection(null); this.timeline.overDirty = true; }
    else if (k === '?') { this.dialogs.help(); }
  }

  // ----------------------------------------------------------------- toasts
  toast(text, kind = 'info') {
    const box = $('#toasts');
    const t = el('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, text);
    box.append(t);
    while (box.children.length > 4) box.firstChild.remove();
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, kind === 'error' ? 7000 : 3500);
  }
}

window.addEventListener('DOMContentLoaded', () => { window.audiomagic = new App(); });
