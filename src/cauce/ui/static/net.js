// The API client. The token comes from a same-origin request, never from a URL.
let token = null;

async function ensureToken() {
  if (token) return token;
  const res = await fetch("/api/token", { credentials: "same-origin" });
  if (!res.ok) throw new Error("could not get the UI token");
  token = (await res.json()).token;
  return token;
}

export async function get(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function post(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Cauce-Token": await ensureToken() },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${path}: ${res.status}`);
  return data;
}

// Live updates: server-sent events, reconnecting with the last id seen; a
// stream refused (too many open) falls back to polling the same events.
export function follow(onEvent, onState) {
  let last = 0;
  let stopped = false;
  const open = () => {
    if (stopped) return;
    const source = new EventSource(`/api/stream?after=${last}`);
    source.onopen = () => onState(true);
    source.onmessage = (msg) => {
      last = Number(msg.lastEventId) || last;
      onEvent(JSON.parse(msg.data));
    };
    source.onerror = () => {
      source.close();
      onState(false);
      setTimeout(poll, 1500);
    };
  };
  const poll = async () => {
    try {
      for (const ev of await get(`/api/events?after=${last}`)) {
        last = ev.id;
        onEvent(ev);
      }
    } catch { /* the server may be restarting */ }
    open();
  };
  return { start(after) { last = after; open(); }, stop() { stopped = true; } };
}
