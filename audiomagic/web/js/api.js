// Talking to the local AudioMagic server.

const TOKEN = (() => {
  const url = new URL(location.href);
  let t = url.searchParams.get('token');
  if (t) {
    try { sessionStorage.setItem('am-token', t); } catch { /* private mode */ }
    history.replaceState(null, '', url.pathname);
  } else {
    try { t = sessionStorage.getItem('am-token'); } catch { t = null; }
  }
  return t;
})();

export class ApiError extends Error {
  constructor(message, { confirm = false, status = 0 } = {}) {
    super(message);
    this.confirm = confirm;
    this.status = status;
  }
}

export async function api(method, path, body) {
  const opts = { method, headers: { 'X-AudioMagic-Token': TOKEN || '' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch('/api' + path, opts);
  } catch {
    throw new ApiError('AudioMagic is not responding. Is it still running?');
  }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.arrayBuffer();
  if (r.status === 409) throw new ApiError(data.confirm, { confirm: true, status: 409 });
  if (r.status === 401) throw new ApiError('This page lost its connection key. Close it and open AudioMagic again.', { status: 401 });
  if (!r.ok) throw new ApiError((data && data.error) || `Error ${r.status}`, { status: r.status });
  return data;
}

export function connect(onMessage, onStatus) {
  let ws = null, delay = 500, closed = false;
  const open = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN || '')}`);
    ws.onopen = () => { delay = 500; onStatus(true); };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      onMessage(msg);
    };
    ws.onclose = () => {
      onStatus(false);
      if (!closed) setTimeout(open, delay);
      delay = Math.min(delay * 2, 5000);
    };
  };
  open();
  return { close() { closed = true; if (ws) ws.close(); } };
}
