import { closeMenus } from './theme.js';
let authenticated = false;
let preauth = false;
let current = 'pw';
let previousView = 'chat';
let opener = null;
let pendingOpener = null;
let dialogReturn = null;
const authRoutes = ['pw', 'totp', 'setup'];
const views = { chat: 'chat', list: 'chat', status: 'chat', manage: 'manage', delete: 'manage',
  files: 'files', edit: 'files', preview: 'files', diff: 'files' };

export function setAuthenticated(value) {
  if (authenticated === value) return;
  authenticated = value;
  document.dispatchEvent(new CustomEvent('auth-change', { detail: { authenticated: value } }));
}
export function setPreauth(value) { preauth = value; }
export function go(route) {
  if (location.hash === `#${route}`) renderRoute();
  else location.hash = route;
}
export function closeInfo() { go(previousView); }
export function openDialog(route, element = document.activeElement) {
  pendingOpener = element;
  const origin = (location.hash || '#chat').slice(1);
  dialogReturn = route === 'diff' ? 'edit' : ['chat', 'list', 'status', 'manage'].includes(origin) ? origin : 'manage';
  go(route);
}
export function closeDialog() {
  const route = (location.hash || '').slice(1);
  if (!['delete', 'diff'].includes(route)) return;
  go(dialogReturn || (route === 'delete' ? 'manage' : 'edit'));
}

export function renderRoute() {
  let route = (location.hash || '#pw').slice(1);
  if (!authRoutes.includes(route) && !Object.hasOwn(views, route)) route = authenticated ? 'chat' : 'pw';
  if (!authenticated && !authRoutes.includes(route)) route = 'pw';
  if (!authenticated && ['totp', 'setup'].includes(route) && !preauth) route = 'pw';
  if (authenticated && authRoutes.includes(route)) route = 'chat';
  if (location.hash !== `#${route}`) history.replaceState(null, '', `#${route}`);
  const isAuth = authRoutes.includes(route);
  document.querySelector('#auth').classList.toggle('hidden', !isAuth);
  document.querySelector('#app').classList.toggle('hidden', isAuth);
  document.querySelectorAll('[data-auth]').forEach(element =>
    element.classList.toggle('hidden', element.dataset.auth !== route));
  closeMenus();
  document.body.classList.toggle('m-list', !isAuth && route === 'list');
  document.body.classList.toggle('info-open', !isAuth && route === 'status');
  const view = views[route] || 'chat';
  document.querySelectorAll('[data-view]').forEach(element =>
    element.classList.toggle('hidden', element.dataset.view !== view));
  const dialogId = route === 'delete' ? 'delDlg' : route === 'diff' ? 'diffDlg' : null;
  let closedDialog = false;
  for (const dialog of document.querySelectorAll('dialog')) {
    if (!isAuth && dialog.id === dialogId && !dialog.open) {
      if (!pendingOpener) dialogReturn = route === 'delete' ? 'manage' : 'edit';
      opener = pendingOpener || document.activeElement;
      pendingOpener = null;
      dialog.showModal();
    } else if (dialog.id !== dialogId && dialog.open) {
      dialog.close();
      closedDialog = true;
      let target = opener;
      if (target?.isConnected === false && target.dataset.deleteTid) {
        target = [...document.querySelectorAll('#mlist [data-delete-tid]')]
          .find(button => button.dataset.deleteTid === opener.dataset.deleteTid);
      }
      if (target && target.isConnected !== false && !target.disabled && !target.hidden) target.focus();
      else document.querySelector(`[data-view="${view}"] h2`)?.focus();
    }
  }
  document.querySelector('#connPanel').classList.toggle('focus', route === 'status');
  if (route === 'status') document.querySelector('#info').scrollTop = 0;
  if (!isAuth && !['status', 'delete', 'diff'].includes(route)) previousView = view;
  if (route !== current && !dialogId && !closedDialog) {
    const heading = isAuth ? document.querySelector(`[data-auth="${route}"] h2`)
      : route === 'status' ? document.querySelector('#connPanel h2')
      : document.querySelector(`[data-view="${view}"] h2`);
    heading?.focus();
  }
  current = route;
  document.dispatchEvent(new CustomEvent('route-change', { detail: { route, view } }));
}

export function initRouter() {
  document.addEventListener('click', event => {
    if (event.target.closest('[data-close-dialog]')) { event.preventDefault(); closeDialog(); return; }
    const button = event.target.closest('[data-route]');
    if (!button) return;
    event.preventDefault();
    if (['delete', 'diff'].includes(button.dataset.route)) openDialog(button.dataset.route, button);
    else go(button.dataset.route);
  });
  document.querySelector('.info-close').addEventListener('click', closeInfo);
  document.querySelector('#delDlg').addEventListener('cancel', event => {
    event.preventDefault(); closeDialog();
  });
  document.querySelector('#diffDlg').addEventListener('cancel', event => {
    event.preventDefault(); closeDialog();
  });
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]')]
        .filter(element => !element.closest('[hidden], .hidden') && element.getClientRects().length);
      if (!controls.length) { event.preventDefault(); dialog.setAttribute('tabindex', '-1'); dialog.focus(); return; }
      const first = controls[0], last = controls.at(-1), focused = document.activeElement;
      if (!dialog.contains(focused) || (event.shiftKey && focused === first) || (!event.shiftKey && focused === last)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      }
    });
  }
  window.addEventListener('hashchange', renderRoute);
}
