// Same HTTP(S) origin only. No drag, pointer capture or prevented default actions.
const frame = document.querySelector('#starflow-background');
const target = new URL(frame.getAttribute('src'), location.href);
if (/^https?:$/.test(location.protocol) && target.origin === location.origin) {
  let latest = null, scheduled = 0, hovering = false;
  const send = message => frame.contentWindow?.postMessage({ namespace: 'starflow-base-stars-v1', ...message }, target.origin);
  function leave() {
    cancelAnimationFrame(scheduled); scheduled = 0; latest = null;
    if (hovering) send({ type: 'leave' });
    hovering = false;
  }
  document.addEventListener('pointermove', event => {
    if (event.defaultPrevented || !['mouse', 'pen'].includes(event.pointerType) || event.buttons || !getSelection()?.isCollapsed) { leave(); return; }
    const rect = frame.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width, y = (event.clientY - rect.top) / rect.height;
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > 1 || y < 0 || y > 1) { leave(); return; }
    hovering = true; latest = { type: 'move', x, y };
    if (!scheduled) scheduled = requestAnimationFrame(() => {
      scheduled = 0; const message = latest; latest = null; if (message) send(message);
    });
  }, { passive: true });
  document.addEventListener('pointerdown', leave, { passive: true });
  document.addEventListener('pointerleave', leave);
  document.addEventListener('pointercancel', leave);
  window.addEventListener('blur', leave);
  window.addEventListener('resize', leave);
  window.addEventListener('pagehide', leave);
  document.addEventListener('visibilitychange', leave);
  frame.addEventListener('load', leave);
}
