import { t } from './i18n.js';
const root = document.documentElement;
const media = matchMedia('(prefers-color-scheme: dark)');

export function closeMenus(except = null) {
  document.querySelectorAll('.tpick').forEach(picker => {
    if (picker === except) return;
    picker.querySelector('.menu').hidden = true;
    picker.querySelector('.theme-btn').setAttribute('aria-expanded', 'false');
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
    const button = picker.querySelector('.theme-btn');
    const menu = picker.querySelector('.menu');
    button.addEventListener('click', event => {
      event.stopPropagation();
      const open = menu.hidden;
      closeMenus();
      menu.hidden = !open;
      button.setAttribute('aria-expanded', String(open));
      if (open) menu.querySelector('[aria-checked="true"]').focus();
    });
    menu.addEventListener('click', event => {
      const item = event.target.closest('[data-pref]');
      if (!item) return;
      setPreference(item.dataset.pref);
      closeMenus();
      button.focus();
    });
    menu.addEventListener('keydown', event => {
      const items = [...menu.querySelectorAll('[data-pref]')];
      const current = items.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        items[(current + (event.key === 'ArrowDown' ? 1 : 2)) % 3].focus();
      }
      if (event.key === 'Escape') { event.preventDefault(); closeMenus(); button.focus(); }
    });
  });
  media.addEventListener('change', () => {
    if (root.dataset.themePref === 'system') applyTheme();
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.tpick')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const open = [...document.querySelectorAll('.tpick')].find(p => !p.querySelector('.menu').hidden);
    if (open) { closeMenus(); open.querySelector('.theme-btn').focus(); }
  });
  applyTheme();
}
