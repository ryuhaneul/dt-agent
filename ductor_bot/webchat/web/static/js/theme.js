import { t } from './i18n.js';
const root = document.documentElement;
const media = matchMedia('(prefers-color-scheme: dark)');

let activePicker = null, menuRevision = 0;
export function closeMenus(except = null) {
  ++menuRevision;
  document.querySelectorAll('.tpick, .lpick, .account-menu').forEach(picker => {
    if (picker === except) return;
    picker.querySelector('.menu').hidden = true;
    picker.querySelector('.theme-btn').setAttribute('aria-expanded', 'false');
  });
  activePicker = except;
}
export function positionMenu(picker) {
  const menu = picker.querySelector('.menu');
  if (menu.hidden) return;
  const button = picker.querySelector('.theme-btn'), savedScrollTop = menu.scrollTop;
  const pad = 8, gap = 6, viewportWidth = document.documentElement.clientWidth, viewportHeight = innerHeight;
  const trigger = button.getBoundingClientRect();
  const width = picker.classList.contains('lpick') ? 240 : picker.classList.contains('account-menu') ? 172 : 168;
  menu.style.width = Math.min(width, viewportWidth - pad * 2) + 'px';
  menu.style.maxHeight = Math.max(44, viewportHeight - pad * 2) + 'px';
  menu.style.right = 'auto';
  const bar = picker.closest('.bar')?.getBoundingClientRect() || trigger;
  const belowEdge = Math.max(trigger.bottom, bar.bottom), aboveEdge = Math.min(trigger.top, bar.top);
  const fullHeight = menu.scrollHeight + 2;
  const below = Math.max(0, viewportHeight - pad - belowEdge - gap), above = Math.max(0, aboveEdge - gap - pad);
  const belowTrigger = below >= Math.min(fullHeight, 160) || below >= above;
  menu.style.maxHeight = Math.max(1, belowTrigger ? below : above) + 'px';
  const rect = menu.getBoundingClientRect(), parent = menu.offsetParent.getBoundingClientRect();
  const left = Math.max(pad, Math.min(trigger.right - rect.width, viewportWidth - pad - rect.width));
  const preferredTop = belowTrigger ? belowEdge + gap : aboveEdge - gap - rect.height;
  const top = Math.max(pad, Math.min(preferredTop, viewportHeight - pad - rect.height));
  menu.style.left = (left - parent.left) + 'px'; menu.style.top = (top - parent.top) + 'px';
  menu.scrollTop = savedScrollTop;
}
function bindMenu(picker) {
  const button = picker.querySelector('.theme-btn'), menu = picker.querySelector('.menu');
  button.addEventListener('click', () => {
    const open = menu.hidden;
    closeMenus();
    if (!open) return;
    const generation = menuRevision;
    menu.style.visibility = 'hidden';
    menu.style.opacity = '0';
    activePicker = picker; menu.hidden = false; button.setAttribute('aria-expanded', 'true');
    positionMenu(picker);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (generation !== menuRevision || activePicker !== picker || menu.hidden) return;
      positionMenu(picker);
      menu.style.visibility = '';
      menu.style.opacity = '';
      const first = menu.querySelector('[aria-checked="true"]') || menu.querySelector('button');
      first?.focus({ preventScroll: true });
      if (first?.offsetTop < menu.scrollTop) menu.scrollTop = first.offsetTop;
      else if (first && first.offsetTop + first.offsetHeight > menu.scrollTop + menu.clientHeight) {
        menu.scrollTop = first.offsetTop + first.offsetHeight - menu.clientHeight;
      }
    }));
  });
  menu.addEventListener('click', event => {
    if (event.target.closest('button')) { closeMenus(); button.focus({ preventScroll: true }); }
  });
  menu.addEventListener('keydown', event => {
    const items = [...menu.querySelectorAll('button')], current = items.indexOf(document.activeElement);
    let index = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') index = (current + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
    if (event.key === 'Home') index = 0;
    if (event.key === 'End') index = items.length - 1;
    if (index !== null) { event.preventDefault(); items[index].focus({ preventScroll: true }); items[index].scrollIntoView({ block: 'nearest' }); }
    if (event.key === 'Escape') { event.preventDefault(); closeMenus(); button.focus({ preventScroll: true }); }
  });
}

export function applyTheme() {
  const preference = root.dataset.themePref;
  const dark = preference === 'system' ? media.matches : preference === 'dark';
  root.dataset.theme = dark ? 'dark' : 'light';
  const sky = document.querySelector('#starflow-background');
  if (dark && !sky.hasAttribute('src')) sky.src = sky.dataset.src;
  else if (!dark && sky.hasAttribute('src')) sky.removeAttribute('src');
  document.querySelectorAll('.tpick').forEach(picker => {
    const label = t(`theme.${preference}`) + (preference === 'system'
      ? ` (${t(dark ? 'theme.dark' : 'theme.light')})` : '');
    picker.querySelector('.theme-btn').setAttribute('aria-label', t('theme.label', { pref: label }));
    picker.querySelectorAll('[data-pref]').forEach(button =>
      button.setAttribute('aria-checked', String(button.dataset.pref === preference)));
    picker.querySelector('.now').textContent = preference === 'system'
      ? t('theme.now', { mode: t(dark ? 'theme.dark' : 'theme.light') }) : '';
  });
}

export function setPreference(preference) {
  if (!['system', 'dark', 'light'].includes(preference)) return;
  root.dataset.themePref = preference;
  try {
    if (preference === 'system') localStorage.removeItem('dtw-theme');
    else localStorage.setItem('dtw-theme', preference);
  } catch { /* Theme selection remains effective for this page. */ }
  applyTheme();
}

export function initTheme() {
  document.querySelectorAll('[data-tpick]').forEach(slot => {
    const picker = document.querySelector('#tpickTpl').content.firstElementChild.cloneNode(true);
    slot.appendChild(picker);
    const menu = picker.querySelector('.menu');
    menu.addEventListener('click', event => {
      const item = event.target.closest('[data-pref]');
      if (item) setPreference(item.dataset.pref);
    });
  });
  document.querySelectorAll('.tpick, .lpick, .account-menu').forEach(bindMenu);
  const reposition = () => { if (activePicker) positionMenu(activePicker); };
  globalThis.addEventListener?.('resize', reposition);
  globalThis.addEventListener?.('scroll', event => {
    if (activePicker && !(event.target instanceof Node && activePicker.querySelector('.menu').contains(event.target))) reposition();
  }, true);
  if (typeof ResizeObserver !== 'undefined') {
    const bar = document.querySelector('.bar');
    if (bar) new ResizeObserver(reposition).observe(bar);
  }
  document.addEventListener('language-changed', () => { applyTheme(); requestAnimationFrame(reposition); });
  document.addEventListener('route-change', () => closeMenus());
  document.addEventListener('close-header-menus', () => closeMenus());
  document.addEventListener('auth-change', () => closeMenus());
  media.addEventListener('change', () => {
    if (root.dataset.themePref === 'system') applyTheme();
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.tpick, .lpick, .account-menu')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const open = activePicker;
    if (open) { closeMenus(); open.querySelector('.theme-btn').focus(); }
  });
  applyTheme();
}
