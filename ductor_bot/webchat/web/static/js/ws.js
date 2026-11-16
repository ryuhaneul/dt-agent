import {
  beginCredentialChange as beginChange, finishCredentialChange as finishChange,
  isCredentialChanging, checkSession,
} from './api.js';

let activeConnection = null;
export function beginCredentialChange() { beginChange(); }
export function finishCredentialChange(success) {
  finishChange(success);
  if (success) activeConnection?.reconnect();
}

export function connectEvents(onEvent, onConnection) {
  let socket = null, timer = null, delay = 2000, stopped = false;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(connect, delay);
  }
  function connect() {
    if (stopped) return;
    if (isCredentialChanging()) { schedule(); return; }
    const url = new URL('/api/ws', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const current = new WebSocket(url);
    socket = current;
    current.addEventListener('open', () => { delay = 2000; onConnection(true); });
    current.addEventListener('message', event => {
      try { onEvent(JSON.parse(event.data)); } catch { /* Ignore malformed frames. */ }
    });
    current.addEventListener('error', () => current.close());
    current.addEventListener('close', () => {
      if (stopped || socket !== current) return;
      onConnection(false);
      if (!isCredentialChanging()) checkSession(() => !stopped);
      schedule();
      delay = Math.min(delay * 2, 30000);
    });
  }
  const connection = {
    reconnect() {
      if (stopped) return;
      clearTimeout(timer);
      const previous = socket;
      socket = null;
      previous?.close();
      connect();
    },
  };
  activeConnection = connection;
  connect();
  return () => {
    stopped = true; clearTimeout(timer); socket?.close();
    if (activeConnection === connection) activeConnection = null;
  };
}
