// Modal dialogs: add input, effects, export, projects, confirm/prompt, help.

import { api } from './api.js';
import { el, fmtDb, fmtTime, icon, throttle } from './util.js';

function modal({ title, body, footer, wide = false, onClose }) {
  const root = document.getElementById('modal-root');
  const closeBtn = el('button', { class: 'icon-btn modal-close', type: 'button', title: 'Close (Esc)' }, icon('close'));
  const box = el('div', { class: `modal${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    el('div', { class: 'modal-head' }, el('h2', {}, title), closeBtn),
    el('div', { class: 'modal-body' }, body),
    footer ? el('div', { class: 'modal-foot' }, footer) : null);
  const backdrop = el('div', { class: 'backdrop' }, box);
  const prevFocus = document.activeElement;
  const close = () => {
    backdrop.remove();
    document.removeEventListener('keydown', onKey, true);
    if (onClose) onClose();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  };
  const onKey = (e) => {
    if (e.key === 'Escape' && root.lastElementChild === backdrop) { e.stopPropagation(); close(); }
  };
  closeBtn.onclick = close;
  backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) close(); });
  document.addEventListener('keydown', onKey, true);
  root.append(backdrop);
  const first = box.querySelector('[autofocus], .modal-body input, .modal-body select, .modal-body button');
  if (first) setTimeout(() => first.focus(), 0);
  return { close, box };
}

function field(label, control, hint) {
  return el('label', { class: 'field' }, el('span', { class: 'field-label' }, label), control, hint ? el('span', { class: 'hint' }, hint) : null);
}

function select(options, value) {
  const s = el('select', {});
  for (const [v, label] of options) s.append(el('option', { value: v, selected: String(v) === String(value) }, label));
  return s;
}

function switchInput(checked, label) {
  const input = el('input', { type: 'checkbox', class: 'switch-input', checked });
  return { input, node: el('label', { class: 'switch' }, input, el('span', { class: 'switch-track' }), el('span', {}, label)) };
}

function randomPass() {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => 'abcdefghjkmnpqrstuvwxyz23456789'[b % 31]).join('');
}

export class Dialogs {
  constructor(app) {
    this.app = app;
  }

  // ------------------------------------------------------ confirm / prompt
  confirm(title, text, okLabel = 'OK', danger = false) {
    return new Promise((resolve) => {
      let answered = false;
      const ok = el('button', { class: `btn ${danger ? 'danger' : 'primary'}`, type: 'button', autofocus: true }, okLabel);
      const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
      const m = modal({ title, body: el('p', { class: 'confirm-text' }, text), footer: [cancel, ok], onClose: () => { if (!answered) resolve(false); } });
      ok.onclick = () => { answered = true; m.close(); resolve(true); };
      cancel.onclick = () => { answered = true; m.close(); resolve(false); };
    });
  }

  prompt(title, label, value = '', okLabel = 'Save') {
    return new Promise((resolve) => {
      let answered = false;
      const input = el('input', { type: 'text', value, maxlength: 80, autofocus: true });
      const ok = el('button', { class: 'btn primary', type: 'submit' }, okLabel);
      const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
      const form = el('form', {}, field(label, input));
      const m = modal({ title, body: form, footer: [cancel, ok], onClose: () => { if (!answered) resolve(null); } });
      const submit = (e) => { e && e.preventDefault(); answered = true; m.close(); resolve(input.value.trim()); };
      form.onsubmit = submit;
      ok.onclick = submit;
      cancel.onclick = () => { answered = true; m.close(); resolve(null); };
      setTimeout(() => input.select(), 0);
    });
  }

  // ------------------------------------------------------------ add input
  async addInput(replaceTrackId = null) {
    const app = this.app;
    const tabs = [
      ['devices', 'Microphones & interfaces', 'mic'],
      ['apps', 'Programs', 'app'],
      ['monitor', 'Everything you hear', 'speaker'],
      ['url', 'Internet stream', 'globe'],
      ['srt', 'OBS / network (SRT)', 'broadcast'],
      ['tone', 'Test tone', 'wave'],
    ];
    const nav = el('nav', { class: 'tabs', role: 'tablist' });
    const pane = el('div', { class: 'tab-pane' });
    const title = replaceTrackId ? 'Change input' : 'Add input';
    const m = modal({ title, body: el('div', { class: 'add-input' }, nav, pane), wide: true });
    let sources = null;
    const add = async (items) => {
      try {
        if (replaceTrackId) {
          await api('PATCH', `/tracks/${replaceTrackId}`, { source: items[0].source });
        } else {
          await api('POST', '/tracks', { items });
          app.toast(items.length > 1 ? `Added ${items.length} inputs` : `Added ${items[0].name || items[0].source.label}`);
        }
        m.close();
      } catch (e) { app.toast(e.message, 'error'); }
    };
    const load = async () => {
      try { sources = await api('GET', '/sources'); } catch (e) { sources = { ok: false, error: e.message, devices: [], apps: [], outputs: [] }; }
    };
    const refreshBtn = (then) => el('button', { class: 'btn small', type: 'button', onclick: async () => { await load(); then(); } }, icon('refresh'), 'Refresh');
    const empty = (text, then) => el('div', { class: 'empty' }, el('p', {}, text), then ? refreshBtn(then) : null);

    const views = {
      devices: () => {
        if (!sources.ok) return empty(`Can't see PipeWire: ${sources.error}`, show.bind(null, 'devices'));
        if (!sources.devices.length) return empty('No microphones or audio interfaces found. Plug one in and refresh.', () => show('devices'));
        const list = el('div', { class: 'source-list' });
        for (const d of sources.devices) {
          const base = { kind: 'device', node: d.node, label: d.label, device_channels: d.channels.length };
          const btns = el('div', { class: 'source-actions' });
          const chanItem = (i) => ({ name: d.channels.length > 1 ? `${d.label.split(' ')[0]} ${i + 1}` : d.label, source: { ...base, channels: [i], label: `${d.label} · ${d.channel_labels[i]}` } });
          if (d.channels.length === 1) {
            btns.append(el('button', { class: 'btn primary', type: 'button', onclick: () => add([{ name: d.label, source: { ...base, channels: [0] } }]) }, 'Add'));
          } else {
            d.channel_labels.forEach((lbl, i) => btns.append(el('button', { class: 'btn', type: 'button', onclick: () => add([chanItem(i)]) }, lbl)));
            if (!replaceTrackId) {
              btns.append(el('button', { class: 'btn primary', type: 'button', title: 'One track per input jack', onclick: () => add(d.channels.map((_, i) => chanItem(i))) }, `Each input as its own track`));
            }
            btns.append(el('button', { class: 'btn', type: 'button', title: 'Inputs 1 and 2 together as a stereo pair', onclick: () => add([{ name: d.label, source: { ...base, channels: [0, 1], label: `${d.label} · stereo` } }]) }, 'Stereo (1+2)'));
          }
          list.append(el('div', { class: 'source-card' },
            el('div', { class: 'source-head' }, icon('mic'), el('div', {}, el('strong', {}, d.label),
              el('span', { class: 'muted' }, ` ${d.channels.length} input${d.channels.length > 1 ? 's' : ''}${d.default ? ' · system default' : ''}`))),
            btns));
        }
        return el('div', {}, list, el('div', { class: 'pane-foot' }, refreshBtn(() => show('devices'))));
      },
      apps: () => {
        const hint = el('p', { class: 'hint block' }, 'Records what a program plays, such as a Discord, Zoom or Jitsi call with remote guests, a browser, or a game. ',
          'Programs only show up while they are playing sound.');
        if (!sources.ok) return empty(`Can't see PipeWire: ${sources.error}`);
        if (!sources.apps.length) return el('div', {}, hint, empty('No programs are playing sound right now. Start the call or playback, then refresh.', () => show('apps')));
        const list = el('div', { class: 'source-list' });
        for (const a of sources.apps) {
          list.append(el('div', { class: 'source-card row' },
            el('div', { class: 'source-head' }, icon('app'), el('div', {}, el('strong', {}, a.label), a.detail ? el('span', { class: 'muted' }, ` ${a.detail}`) : null)),
            el('button', { class: 'btn primary', type: 'button', onclick: () => add([{ name: a.label, source: { kind: 'app', app: a.app, binary: a.binary, label: a.label } }]) }, 'Add')));
        }
        return el('div', {}, hint, list, el('div', { class: 'pane-foot' }, refreshBtn(() => show('apps'))));
      },
      monitor: () => {
        const list = el('div', { class: 'source-list' });
        list.append(el('div', { class: 'source-card row' },
          el('div', { class: 'source-head' }, icon('speaker'), el('div', {}, el('strong', {}, 'Default output'), el('span', { class: 'muted' }, ' follows your sound settings'))),
          el('button', { class: 'btn primary', type: 'button', onclick: () => add([{ name: 'Desktop audio', source: { kind: 'monitor', node: '@default', label: 'Everything you hear' } }]) }, 'Add')));
        for (const o of (sources.outputs || [])) {
          list.append(el('div', { class: 'source-card row' },
            el('div', { class: 'source-head' }, icon('speaker'), el('div', {}, el('strong', {}, o.label))),
            el('button', { class: 'btn', type: 'button', onclick: () => add([{ name: o.label, source: { kind: 'monitor', node: o.node, label: `Everything on ${o.label}` } }]) }, 'Add')));
        }
        return el('div', {}, el('p', { class: 'hint block' }, 'Records everything that plays through an output: every program at once. Use "Programs" to record just one.'), list);
      },
      url: () => {
        const url = el('input', { type: 'url', placeholder: 'https://example.com/live.mp3', autofocus: true });
        const name = el('input', { type: 'text', placeholder: 'Radio', maxlength: 60 });
        const form = el('form', { class: 'form' },
          field('Stream address', url, 'Internet radio (Icecast/Shoutcast), HLS (.m3u8), RTSP or any direct audio link.'),
          field('Name', name),
          el('div', { class: 'form-actions' }, el('button', { class: 'btn primary', type: 'submit' }, 'Add stream')));
        form.onsubmit = (e) => { e.preventDefault(); add([{ name: name.value.trim() || undefined, source: { kind: 'url', url: url.value.trim() } }]); };
        return form;
      },
      srt: () => {
        const used = new Set(app.state.tracks.filter((t) => t.source.kind === 'srt').map((t) => t.source.port));
        let p = 9000;
        while (used.has(p)) p++;
        const port = el('input', { type: 'number', min: 1024, max: 65535, value: p });
        const pass = el('input', { type: 'text', value: randomPass(), maxlength: 79, spellcheck: 'false' });
        const lan = switchInput(false, 'Allow other computers on my network to send');
        const latency = el('input', { type: 'number', min: 20, max: 8000, value: 200 });
        const name = el('input', { type: 'text', value: 'OBS', maxlength: 60 });
        const obsUrl = el('code', { class: 'copyable' });
        const copy = el('button', { class: 'btn small', type: 'button', title: 'Copy' }, icon('copy'), 'Copy');
        const update = () => {
          const host = lan.input.checked ? '<this computer\'s IP>' : '127.0.0.1';
          let u = `srt://${host}:${port.value}?mode=caller`;
          if (pass.value) u += `&passphrase=${pass.value}&pbkeylen=16`;
          obsUrl.textContent = u;
        };
        for (const i of [port, pass, lan.input]) i.addEventListener('input', update);
        update();
        copy.onclick = () => navigator.clipboard && navigator.clipboard.writeText(obsUrl.textContent).then(() => app.toast('Copied'));
        const form = el('form', { class: 'form' },
          el('p', { class: 'hint block' }, 'Receives a stream sent from OBS, ffmpeg or another SRT sender. A passphrase encrypts the stream (AES-128); leave it empty to turn encryption off.'),
          el('div', { class: 'form-grid' }, field('Name', name), field('Port', port), field('Passphrase', pass, '10 to 79 characters'), field('Latency (ms)', latency)),
          lan.node,
          el('div', { class: 'obs-help' },
            el('strong', {}, 'In OBS: '), 'Settings → Stream → Service "Custom…", then paste this as the Server (leave Stream Key empty):',
            el('div', { class: 'copy-row' }, obsUrl, copy)),
          el('div', { class: 'form-actions' }, el('button', { class: 'btn primary', type: 'submit' }, 'Add SRT input')));
        form.onsubmit = (e) => {
          e.preventDefault();
          add([{ name: name.value.trim() || 'OBS', source: { kind: 'srt', port: parseInt(port.value, 10), passphrase: pass.value, lan: lan.input.checked, latency: parseInt(latency.value, 10) } }]);
        };
        return form;
      },
      tone: () => {
        const freq = el('input', { type: 'number', min: 20, max: 20000, value: 440 });
        const form = el('form', { class: 'form' },
          el('p', { class: 'hint block' }, 'A steady tone for checking levels, monitoring and export without a microphone.'),
          field('Frequency (Hz)', freq),
          el('div', { class: 'form-actions' }, el('button', { class: 'btn primary', type: 'submit' }, 'Add test tone')));
        form.onsubmit = (e) => { e.preventDefault(); add([{ name: `Tone ${freq.value} Hz`, source: { kind: 'tone', freq: parseFloat(freq.value) } }]); };
        return form;
      },
    };
    const show = (id) => {
      for (const b of nav.children) b.setAttribute('aria-selected', b.dataset.id === id ? 'true' : 'false');
      pane.replaceChildren(views[id]());
    };
    for (const [id, label, ic] of tabs) {
      nav.append(el('button', { class: 'tab', type: 'button', role: 'tab', dataset: { id }, onclick: () => show(id) }, icon(ic), label));
    }
    pane.append(el('div', { class: 'empty' }, 'Looking for devices…'));
    await load();
    show('devices');
  }

  // --------------------------------------------------------------- effects
  fx(trackId) {
    const app = this.app;
    const track = () => app.state.tracks.find((t) => t.id === trackId);
    if (!track()) return;
    const send = throttle((fx) => api('PATCH', `/tracks/${trackId}`, { fx }).catch((e) => app.toast(e.message, 'error')), 80);
    const body = el('div', { class: 'fx' });
    const meter = el('div', { class: 'fx-level' });
    let dragging = false;
    body.addEventListener('pointerdown', (e) => { if (e.target.type === 'range') dragging = true; });
    const endDrag = () => { dragging = false; };
    body.addEventListener('pointerup', endDrag);
    body.addEventListener('pointercancel', endDrag);
    const build = () => {
      const t = track();
      if (!t) return;
      const fx = t.fx;
      const group = (key, title, desc, rows) => {
        const sw = switchInput(fx[key].on, title);
        sw.input.dataset.key = `${key}.on`;
        sw.input.onchange = () => send({ [key]: { on: sw.input.checked } });
        return el('section', { class: `fx-group${fx[key].on ? ' on' : ''}` },
          el('div', { class: 'fx-group-head' }, sw.node), el('p', { class: 'hint' }, desc), el('div', { class: 'fx-rows' }, rows));
      };
      const slider = (key, sub, min, max, step, fmt, label) => {
        const val = el('span', { class: 'val' }, fmt(fx[key][sub]));
        const input = el('input', { type: 'range', class: 'slider', min, max, step, value: fx[key][sub], dataset: { key: `${key}.${sub}` } });
        input.oninput = () => { val.textContent = fmt(parseFloat(input.value)); send({ [key]: { [sub]: parseFloat(input.value) } }); };
        return el('label', { class: 'fx-row' }, el('span', { class: 'lbl' }, label), input, val);
      };
      const strengthName = (v) => (v < 0.34 ? 'Gentle' : v < 0.67 ? 'Medium' : 'Strong');
      const lowcut = switchInput(fx.eq.lowcut, 'Low cut (removes rumble and handling noise)');
      lowcut.input.dataset.key = 'eq.lowcut';
      lowcut.input.onchange = () => send({ eq: { lowcut: lowcut.input.checked } });
      body.replaceChildren(
        el('div', { class: 'fx-presets' }, el('span', { class: 'muted' }, 'Start from:'),
          el('button', { class: 'btn small', type: 'button', dataset: { key: 'preset.voice' }, onclick: () => api('PATCH', `/tracks/${trackId}`, { preset: 'voice' }) }, 'Voice'),
          el('button', { class: 'btn small', type: 'button', dataset: { key: 'preset.off' }, onclick: () => api('PATCH', `/tracks/${trackId}`, { preset: 'off' }) }, 'All off'),
          meter),
        group('ns', 'Noise suppression', 'Reduces steady background noise such as fans, hiss and hum.', [
          slider('ns', 'strength', 0, 1, 0.01, strengthName, 'Strength')]),
        group('gate', 'Noise gate', 'Silences the input between phrases, when it is quieter than the threshold.', [
          slider('gate', 'threshold', -80, -10, 1, (v) => `${v} dB`, 'Threshold'),
          slider('gate', 'release', 20, 1000, 10, (v) => `${v} ms`, 'Release')]),
        group('eq', 'EQ', 'Shape the tone: low (150 Hz), mid (2.5 kHz), high (8 kHz).', [
          lowcut.node,
          slider('eq', 'low', -12, 12, 0.5, fmtDb, 'Low'),
          slider('eq', 'mid', -12, 12, 0.5, fmtDb, 'Mid'),
          slider('eq', 'high', -12, 12, 0.5, fmtDb, 'High')]),
        el('p', { class: 'hint' }, 'Effects never change the raw recording. You hear them while monitoring and playing back, and they are applied when you export.'),
      );
    };
    build();
    let lastFx = JSON.stringify(track().fx);
    const off = app.on('state', () => {
      const t = track();
      if (!t) { m.close(); return; }
      const now = JSON.stringify(t.fx);
      if (now !== lastFx && !dragging) {
        const focusKey = document.activeElement && body.contains(document.activeElement) ? document.activeElement.dataset.key : null;
        lastFx = now;
        build();
        if (focusKey) { const f = body.querySelector(`[data-key="${focusKey}"]`); if (f) f.focus(); }
      }
    });
    const offMeter = app.on('meters', (msg) => {
      const v = msg.tracks[trackId];
      meter.textContent = v ? `Level ${v[0] <= -119 ? '-∞' : v[0].toFixed(0)} dB` : '';
    });
    const m = modal({ title: `Effects · ${track().name}`, body, onClose: () => { off(); offMeter(); } });
  }

  // ---------------------------------------------------------------- export
  async exportTake(takeId) {
    const app = this.app;
    const st = app.state;
    const take = st.takes.find((t) => t.id === takeId);
    if (!take) return;
    let options;
    try { options = await api('GET', '/export/options'); } catch (e) { app.toast(e.message, 'error'); return; }
    const prev = st.project.export || {};
    const fmt = select(options.formats.map((f) => [f.id, f.label]), prev.format || 'flac');
    const quality = el('select', {});
    const fillQuality = () => {
      const f = options.formats.find((x) => x.id === fmt.value);
      quality.replaceChildren(...f.qualities.map(([v, l]) => el('option', { value: v, selected: v === (prev.format === f.id ? prev.quality : f.default) }, l)));
    };
    fmt.onchange = fillQuality;
    fillQuality();
    const what = select([['mix', 'The mix (one file)'], ['stems', 'Each track separately'], ['both', 'Both']], prev.what || 'mix');
    const channels = select([['2', 'Stereo'], ['1', 'Mono']], prev.channels || 2);
    const norm = select(options.normalize.map((n) => [n.id, n.label]), prev.normalize || 'off');
    const tags = prev.tags || {};
    const tTitle = el('input', { type: 'text', value: take.name });
    const tArtist = el('input', { type: 'text', value: tags.artist || '' });
    const tAlbum = el('input', { type: 'text', value: tags.album || st.project.name });
    const tDate = el('input', { type: 'text', value: String(new Date().getFullYear()) });
    const folder = el('input', { type: 'text', value: prev.folder || `${st.project.path}/exports` });
    const withAudio = st.tracks.filter((t) => take.tracks.some((f) => f.track_id === t.id));
    const anySolo = st.tracks.some((t) => t.solo);
    const checks = withAudio.map((t) => {
      const audible = !t.mute && (!anySolo || t.solo);
      const input = el('input', { type: 'checkbox', checked: audible, value: t.id });
      return { t, input, node: el('label', { class: 'check' }, input, el('span', { class: 'swatch', style: { background: t.color } }), t.name, !audible ? el('span', { class: 'muted' }, t.mute ? ' (muted)' : ' (not soloed)') : null) };
    });
    const progress = el('div', { class: 'progress', hidden: true }, el('div', { class: 'progress-bar' }), el('span', { class: 'progress-text' }));
    const result = el('div', { class: 'export-result' });
    const go = el('button', { class: 'btn primary', type: 'button' }, icon('export'), 'Export');
    const cancel = el('button', { class: 'btn', type: 'button' }, 'Close');
    const length = take.timeline.length / 48000;
    const body = el('div', { class: 'export' },
      el('p', { class: 'muted' }, `${take.name} · ${fmtTime(length)} after edits`),
      el('div', { class: 'form-grid' },
        field('Format', fmt), field('Quality', quality),
        field('Export', what), field('Mix channels', channels),
        field('Normalize', norm, 'Loudness targets make episodes sound equally loud on every player.'),
        field('Folder', folder)),
      el('fieldset', { class: 'tracks-pick' }, el('legend', {}, 'Tracks'), checks.map((c) => c.node)),
      el('details', { class: 'tags' }, el('summary', {}, 'Title, artist and other tags'),
        el('div', { class: 'form-grid' }, field('Title', tTitle), field('Artist', tArtist), field('Album / show', tAlbum), field('Year', tDate))),
      progress, result);
    let job = null;
    const off = app.on('export', (msg) => {
      if (!job || msg.job.id !== job) return;
      const j = msg.job;
      progress.hidden = false;
      progress.querySelector('.progress-bar').style.transform = `scaleX(${j.progress})`;
      progress.querySelector('.progress-text').textContent = j.message;
      if (j.state !== 'running') {
        go.disabled = false;
        cancel.textContent = 'Close';
        job = null;
        if (j.state === 'done') {
          result.replaceChildren(
            el('div', { class: 'ok-box' }, el('strong', {}, j.message),
              el('ul', {}, j.files.map((f) => el('li', {}, f.split('/').pop()))),
              el('button', { class: 'btn', type: 'button', onclick: () => api('POST', '/open-folder', { path: j.folder }).catch((e) => app.toast(e.message, 'error')) }, icon('folder'), 'Open folder')));
        } else {
          result.replaceChildren(el('div', { class: 'err-box' }, j.message));
        }
      }
    });
    const m = modal({ title: 'Export', body, footer: [cancel, go], onClose: () => off() });
    cancel.onclick = () => {
      if (job) api('POST', `/export/${job}/cancel`);
      else m.close();
    };
    go.onclick = async () => {
      const ids = checks.filter((c) => c.input.checked).map((c) => c.t.id);
      if (!ids.length) { app.toast('Pick at least one track', 'error'); return; }
      result.replaceChildren();
      go.disabled = true;
      cancel.textContent = 'Cancel export';
      progress.hidden = false;
      progress.querySelector('.progress-bar').style.transform = 'scaleX(0)';
      progress.querySelector('.progress-text').textContent = 'Starting';
      try {
        const r = await api('POST', '/export', {
          take: takeId, format: fmt.value, quality: quality.value, what: what.value, channels: parseInt(channels.value, 10),
          normalize: norm.value, folder: folder.value.trim(), track_ids: ids,
          tags: { title: tTitle.value, artist: tArtist.value, album: tAlbum.value, date: tDate.value },
        });
        job = r.job;
      } catch (e) {
        go.disabled = false;
        cancel.textContent = 'Close';
        progress.hidden = true;
        app.toast(e.message, 'error');
      }
    };
    if (!options.ffmpeg) result.replaceChildren(el('div', { class: 'err-box' }, 'ffmpeg is not installed. Run: sudo apt install ffmpeg'));
  }

  // -------------------------------------------------------------- projects
  async projects() {
    const app = this.app;
    let data;
    try { data = await api('GET', '/projects'); } catch (e) { app.toast(e.message, 'error'); return; }
    const recording = app.state.transport.recording;
    const name = el('input', { type: 'text', value: app.state.project.name, maxlength: 80 });
    const rename = el('button', { class: 'btn', type: 'button' }, 'Rename');
    rename.onclick = () => api('POST', '/project/rename', { name: name.value }).then(() => app.toast('Renamed')).catch((e) => app.toast(e.message, 'error'));
    const newName = el('input', { type: 'text', placeholder: 'Episode 12', maxlength: 80 });
    const copy = switchInput(true, 'Use the same inputs and settings as this project');
    const create = el('button', { class: 'btn primary', type: 'submit', disabled: recording }, icon('plus'), 'Create');
    const list = el('div', { class: 'project-list' });
    for (const p of data.projects) {
      const when = new Date(p.modified * 1000).toLocaleString();
      list.append(el('button', { class: `project-item${p.current ? ' current' : ''}`, type: 'button', disabled: p.current || recording,
        onclick: async () => {
          try { await api('POST', '/projects/open', { path: p.path }); m.close(); } catch (e) { app.toast(e.message, 'error'); }
        } },
      el('strong', {}, p.name), el('span', { class: 'muted' }, `${p.takes} take${p.takes === 1 ? '' : 's'} · ${p.tracks} input${p.tracks === 1 ? '' : 's'} · ${when}`),
      p.current ? el('span', { class: 'badge' }, 'Open') : null));
    }
    const form = el('form', { class: 'new-project' }, field('New project', newName), copy.node, el('div', { class: 'form-actions' }, create));
    form.onsubmit = async (e) => {
      e.preventDefault();
      try { await api('POST', '/projects', { name: newName.value, copy_inputs: copy.input.checked }); m.close(); } catch (err) { app.toast(err.message, 'error'); }
    };
    const body = el('div', { class: 'projects' },
      el('div', { class: 'rename-row' }, field('This project', name), rename),
      form,
      el('h3', {}, 'Open a project'),
      list,
      el('div', { class: 'pane-foot' },
        el('span', { class: 'muted' }, data.root),
        el('button', { class: 'btn small', type: 'button', onclick: () => api('POST', '/open-folder', { path: data.root }).catch((e) => app.toast(e.message, 'error')) }, icon('folder'), 'Open folder')));
    const m = modal({ title: 'Projects', body, wide: true });
  }

  // ------------------------------------------------------------------ help
  help() {
    const keys = [
      ['Space', 'Play / pause'], ['R', 'Start / stop recording'], ['Home', 'Go to the start'],
      ['Click', 'Place the cursor'], ['Drag', 'Select part of the take'], ['Shift + click', 'Extend the selection'],
      ['Delete', 'Cut the selection from all tracks'], ['Ctrl + Z / Ctrl + Shift + Z', 'Undo / redo'],
      ['Ctrl + scroll, + / -', 'Zoom'], ['F', 'Zoom to fit'], ['Esc', 'Clear the selection'],
    ];
    modal({
      title: 'How to use AudioMagic',
      wide: true,
      body: el('div', { class: 'help' },
        el('ol', { class: 'steps' },
          el('li', {}, el('strong', {}, 'Add inputs. '), 'Microphones, interface inputs, programs (like a Discord or Zoom call), internet streams or OBS.'),
          el('li', {}, el('strong', {}, 'Check levels. '), 'Speak normally and aim for the meter to peak in the yellow. A red CLIP light means the source is too loud; turn it down on the mic or interface.'),
          el('li', {}, el('strong', {}, 'Record. '), 'Every input with its red R button lit is recorded to its own file, all lined up in time.'),
          el('li', {}, el('strong', {}, 'Tidy up. '), 'Drag across the waveform to select, then cut, silence one track, or keep just the selection. Add fades and normalize from the track menu.'),
          el('li', {}, el('strong', {}, 'Export. '), 'FLAC, Ogg (Opus or Vorbis), MP3 or WAV, as one mix and/or separate tracks.')),
        el('h3', {}, 'Keyboard'),
        el('table', { class: 'keys' }, keys.map(([k, v]) => el('tr', {}, el('td', {}, el('kbd', {}, k)), el('td', {}, v)))),
        el('p', { class: 'hint' }, 'Monitoring plays inputs back live: use headphones so the speakers do not feed back into the microphone.')),
    });
  }
}
