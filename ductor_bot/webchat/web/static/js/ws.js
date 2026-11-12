import { api } from './api.js';

export function connectEvents(onEvent, onConnection) {
  let socket = null, timer = null, delay = 2000, stopped = false;
  function connect() {
    if (stopped) return;
    const url = new URL('/api/ws', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(url);
    socket.addEventListener('open', () => { delay = 2000; onConnection(true); });
    socket.addEventListener('message', event => {
      try { onEvent(JSON.parse(event.data)); } catch { /* Ignore malformed frames. */ }
    });
    socket.addEventListener('error', () => socket.close());
    socket.addEventListener('close', () => {
      if (stopped) return;
      onConnection(false);
      // Browsers do not expose upgrade rejection bodies. Check session via REST.
      api('/auth/state').then(state => {
        if (!stopped && !state.authenticated) {
          document.dispatchEvent(new CustomEvent('auth-required'));
        }
      }).catch(() => {});
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    });
  }
  connect();
  return () => { stopped = true; clearTimeout(timer); socket?.close(); };
}
