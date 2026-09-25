// Small DOM and formatting helpers.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

export function fmtTime(sec, precise = true) {
  if (sec === null || sec === undefined || !isFinite(sec)) sec = 0;
  const neg = sec < 0;
  sec = Math.abs(sec);
  sec = precise ? Math.round(sec * 10) / 10 : Math.floor(sec + 1e-6);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const ss = precise ? s.toFixed(1).padStart(4, '0') : String(Math.floor(s)).padStart(2, '0');
  const body = h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${String(m).padStart(2, '0')}:${ss}`;
  return (neg ? '-' : '') + body;
}

export function fmtDuration(sec) {
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : `${Math.floor(sec / 60)} min ${Math.round(sec % 60)} s`;
}

export function fmtDb(db) {
  if (db <= -59.9) return '-∞ dB';
  return `${db > 0 ? '+' : ''}${db.toFixed(1)} dB`;
}

export function fmtPan(p) {
  if (Math.abs(p) < 0.01) return 'C';
  return `${Math.round(Math.abs(p) * 100)}${p < 0 ? 'L' : 'R'}`;
}

export function fmtBytes(b) {
  if (b === null || b === undefined) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i >= 3 ? 1 : 0)} ${u[i]}`;
}

export function throttle(fn, ms) {
  let last = 0, timer = null, pending = null;
  return (...args) => {
    pending = args;
    const now = performance.now();
    const run = () => { last = performance.now(); timer = null; fn(...pending); };
    if (now - last >= ms) run();
    else if (!timer) timer = setTimeout(run, ms - (now - last));
  };
}

export function icon(name) {
  const paths = {
    record: '<circle cx="12" cy="12" r="7"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    play: '<path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/>',
    pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3" fill="none" stroke="currentColor" stroke-width="2"/><rect x="3" y="14" width="5" height="7" rx="2"/><rect x="16" y="14" width="5" height="7" rx="2"/>',
    more: '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/>',
    plus: '<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" fill="none"/>',
    cut: '<circle cx="6" cy="18" r="3" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="18" cy="18" r="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 16 19 4M16 16 5 4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>',
    mute: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="m16 9 5 6m0-6-5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    trim: '<path d="M4 4v16M20 4v16" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/><rect x="7" y="8" width="10" height="8" rx="1.5"/>',
    undo: '<path d="M9 7 4 12l5 5M4 12h11a5 5 0 0 1 0 10h-2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    redo: '<path d="m15 7 5 5-5 5M20 12H9a5 5 0 0 0 0 10h2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    zoomin: '<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="m20 20-4.5-4.5M10.5 7.5v6M7.5 10.5h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    zoomout: '<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="m20 20-4.5-4.5M7.5 10.5h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    export: '<path d="M12 3v12m0 0-4-4m4 4 4-4M5 21h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    trash: '<path d="M5 7h14M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z" stroke="currentColor" stroke-width="2" stroke-linejoin="round" fill="none"/>',
    help: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/><circle cx="12" cy="17.2" r="1.2"/>',
    close: '<path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" fill="none"/>',
    speaker: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    app: '<rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 9h18"  stroke="currentColor" stroke-width="2"/>',
    globe: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" stroke="currentColor" stroke-width="2" fill="none"/>',
    broadcast: '<circle cx="12" cy="12" r="2.5"/><path d="M7.5 16.5a6.4 6.4 0 0 1 0-9M16.5 7.5a6.4 6.4 0 0 1 0 9M4.6 19.4a10.5 10.5 0 0 1 0-14.8M19.4 4.6a10.5 10.5 0 0 1 0 14.8" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>',
    wave: '<path d="M3 12h2l2-6 3 12 3-15 3 15 2-6h3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" stroke="currentColor" stroke-width="2" fill="none"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
  };
  const span = document.createElement('span');
  span.className = 'icon';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor">${paths[name] || ''}</svg>`;
  return span;
}
