// Track headers (the controls on the left of each lane) and level meters.

import { api } from './api.js';
import { clamp, el, fmtDb, fmtPan, icon, throttle } from './util.js';

const FLOOR = -60;
const pos = (db) => clamp((db - FLOOR) / -FLOOR, 0, 1);

export class Meter {
  constructor(extraClass = '') {
    this.mask = el('div', { class: 'meter-mask' });
    this.peak = el('div', { class: 'meter-peak' });
    this.clip = el('div', { class: 'meter-clip', title: 'Clipping: the input is too loud. Turn it down at the source.' });
    this.root = el('div', { class: `meter ${extraClass}` }, el('div', { class: 'meter-bar' }, this.mask, this.peak), this.clip);
    this.level = FLOOR;
    this.hold = FLOOR;
    this.holdAt = 0;
    this.clipAt = -1e9;
    this.last = performance.now();
    this.shown = null;
  }

  set(value) {
    const now = performance.now();
    const dt = (now - this.last) / 1000;
    this.last = now;
    const p = value ? Math.max(value[0], FLOOR) : FLOOR;
    this.level = p >= this.level ? p : Math.max(p, this.level - 24 * dt);
    if (p >= this.hold) { this.hold = p; this.holdAt = now; }
    else if (now - this.holdAt > 1500) this.hold = Math.max(this.level, this.hold - 30 * dt);
    if (value && value[2]) this.clipAt = now;
    const shown = [Math.round(pos(this.level) * 400), Math.round(pos(this.hold) * 400), now - this.clipAt < 2000];
    if (this.shown && shown[0] === this.shown[0] && shown[1] === this.shown[1] && shown[2] === this.shown[2]) return;
    this.shown = shown;
    this.mask.style.transform = `scaleX(${1 - shown[0] / 400})`;
    this.peak.style.left = `${(shown[1] / 4).toFixed(2)}%`;
    this.peak.style.opacity = this.hold > FLOOR + 1 ? '1' : '0';
    this.root.classList.toggle('clipping', shown[2]);
  }
}

function toggle(label, title, cls) {
  return el('button', { class: `tbtn ${cls}`, title, type: 'button', 'aria-pressed': 'false' }, label);
}

export class TrackHeader {
  constructor(app, track) {
    this.app = app;
    this.id = track.id;
    this.dragging = new Set();
    this.dot = el('span', { class: 'status-dot' });
    this.nameInput = el('input', { class: 'track-name', value: track.name, maxlength: 60, 'aria-label': 'Track name', spellcheck: 'false' });
    this.moreBtn = el('button', { class: 'icon-btn more', title: 'More options', type: 'button' }, icon('more'));
    this.source = el('div', { class: 'track-source' });
    this.armBtn = toggle('R', 'Record this input (arm)', 'arm');
    this.muteBtn = toggle('M', 'Mute', 'mute');
    this.soloBtn = toggle('S', 'Solo: hear only soloed inputs', 'solo');
    this.monBtn = toggle(icon('headphones'), 'Monitor: hear this input live (use headphones)', 'mon');
    this.fxBtn = el('button', { class: 'tbtn fx', type: 'button', title: 'Noise suppression, gate and EQ' }, 'FX');
    this.gain = el('input', { type: 'range', min: -48, max: 12, step: 0.5, class: 'slider gain', 'aria-label': 'Volume' });
    this.gainVal = el('span', { class: 'val' });
    this.pan = el('input', { type: 'range', min: -100, max: 100, step: 1, class: 'slider pan', 'aria-label': 'Pan' });
    this.panVal = el('span', { class: 'val' });
    this.meter = new Meter();
    this.root = el('div', { class: 'track', dataset: { id: track.id } },
      el('div', { class: 'track-top' }, this.dot, this.nameInput, this.moreBtn),
      this.source,
      el('div', { class: 'track-buttons' }, this.armBtn, this.muteBtn, this.soloBtn, this.monBtn, this.fxBtn),
      el('label', { class: 'track-slider' }, el('span', { class: 'lbl' }, 'Vol'), this.gain, this.gainVal),
      el('label', { class: 'track-slider' }, el('span', { class: 'lbl' }, 'Pan'), this.pan, this.panVal),
      this.meter.root,
    );
    this.bind();
    this.update(track);
  }

  patch(changes) {
    return api('PATCH', `/tracks/${this.id}`, changes).catch((e) => this.app.toast(e.message, 'error'));
  }

  bind() {
    const flip = (key) => () => this.patch({ [key]: !this.track[key] });
    this.armBtn.onclick = flip('armed');
    this.muteBtn.onclick = flip('mute');
    this.soloBtn.onclick = flip('solo');
    this.monBtn.onclick = flip('monitor');
    this.fxBtn.onclick = () => this.app.dialogs.fx(this.id);
    this.moreBtn.onclick = () => this.app.trackMenu(this.id, this.moreBtn);
    const commitName = () => {
      const v = this.nameInput.value.trim();
      if (v && v !== this.track.name) this.patch({ name: v });
      else this.nameInput.value = this.track.name;
    };
    this.nameInput.addEventListener('change', commitName);
    this.nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); this.nameInput.blur(); }
      if (e.key === 'Escape') { this.nameInput.value = this.track.name; this.nameInput.blur(); }
    });
    const sendGain = throttle((v) => this.patch({ gain_db: v }), 60);
    const sendPan = throttle((v) => this.patch({ pan: v }), 60);
    this.slider(this.gain, 'gain', () => {
      const v = parseFloat(this.gain.value);
      this.gainVal.textContent = fmtDb(v);
      sendGain(v);
    }, 0);
    this.slider(this.pan, 'pan', () => {
      const v = parseInt(this.pan.value, 10) / 100;
      this.panVal.textContent = fmtPan(v);
      sendPan(v);
    }, 0);
  }

  slider(input, key, onInput, reset) {
    input.addEventListener('pointerdown', () => this.dragging.add(key));
    input.addEventListener('pointerup', () => this.dragging.delete(key));
    input.addEventListener('pointercancel', () => this.dragging.delete(key));
    input.addEventListener('input', onInput);
    input.addEventListener('dblclick', () => { input.value = reset; onInput(); });
  }

  update(t) {
    this.track = t;
    const st = this.app.state;
    const recording = st.transport.recording;
    this.root.style.setProperty('--track-color', t.color);
    this.root.classList.toggle('is-recording', t.recording);
    this.root.classList.toggle('is-muted', t.mute || (st.tracks.some((x) => x.solo) && !t.solo));
    if (document.activeElement !== this.nameInput) this.nameInput.value = t.name;
    const live = t.status === 'live';
    this.dot.className = `status-dot st-${t.status}`;
    this.dot.title = live ? 'Receiving audio' : (t.status_message || t.status);
    const label = t.source.label || '';
    this.source.textContent = live || !t.status_message ? label : `${label} · ${t.status_message}`;
    this.source.title = this.source.textContent;
    this.source.classList.toggle('warn', !live && t.status !== 'starting');
    for (const [btn, on] of [[this.armBtn, t.armed], [this.muteBtn, t.mute], [this.soloBtn, t.solo], [this.monBtn, t.monitor]]) {
      btn.classList.toggle('on', !!on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    this.armBtn.disabled = recording;
    const fx = t.fx;
    const fxOn = fx.ns.on || fx.gate.on || fx.eq.on;
    this.fxBtn.classList.toggle('on', fxOn);
    this.fxBtn.title = fxOn
      ? 'Effects on: ' + [fx.ns.on && 'noise suppression', fx.gate.on && 'gate', fx.eq.on && 'EQ'].filter(Boolean).join(', ')
      : 'Noise suppression, gate and EQ';
    if (!this.dragging.has('gain')) { this.gain.value = t.gain_db; this.gainVal.textContent = fmtDb(t.gain_db); }
    if (!this.dragging.has('pan')) { this.pan.value = Math.round(t.pan * 100); this.panVal.textContent = fmtPan(t.pan); }
  }
}
