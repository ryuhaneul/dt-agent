import { api } from './api.js';

let activeConnection = null;
export function beginCredentialChange() { activeConnection?.beginCredentialChange(); }
export function finishCredentialChange(success) { activeConnection?.finishCredentialChange(success); }

export function connectEvents(onEvent, onConnection) {
  let socket = null, timer = null, delay = 2000, stopped = false;
  let credentialChanging = false, credentialEpoch = 0;
  function checkSession() {
    const epoch = credentialEpoch;
    // Browsers do not expose upgrade rejection bodies. Check session via REST.
    api('/auth/state').then(state => {
      if (!stopped && !credentialChanging && epoch === credentialEpoch && !state.authenticated) {
        document.dispatchEvent(new CustomEvent('auth-required'));
      }
    }).catch(() => {});
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(connect, delay);
  }
  function connect() {
    if (stopped) return;
    if (credentialChanging) { schedule(); return; }
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
      if (!credentialChanging) checkSession();
      schedule();
      delay = Math.min(delay * 2, 30000);
    });
  }
  const connection = {
    beginCredentialChange() { credentialChanging = true; ++credentialEpoch; },
    finishCredentialChange(success) {
      credentialChanging = false;
      if (stopped) return;
      if (!success) { checkSession(); return; }
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
