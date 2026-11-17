let strings = {};
const languages = ['en', 'de', 'nl', 'es', 'fr', 'id', 'pt', 'ru', 'ko'];
let languageMenuReady = false, languageRequest = 0;

export function t(key, values = {}) {
  const fallback = document.querySelector(`[data-i18n="${key}"]`)?.textContent || '';
  return (strings[key] ?? fallback).replace(/\{(\w+)\}/g, (match, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : match);
}

export function translate(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(element => {
    const values = JSON.parse(element.dataset.i18nArgs || '{}');
    element.textContent = t(element.dataset.i18n, values);
  });
  root.querySelectorAll('[data-i18n-attr]').forEach(element => {
    for (const mapping of element.dataset.i18nAttr.split(';')) {
      const [attribute, key] = mapping.split(':');
      element.setAttribute(attribute, t(key));
    }
  });
}

export async function loadI18n(language = null) {
  if (language === null) {
    try { language = localStorage.getItem('dtw-web-language'); } catch { /* Optional storage. */ }
  }
  const selected = languages.includes(language) ? language : null;
  const generation = ++languageRequest;
  const response = await fetch(`/api/i18n${selected ? `?lang=${selected}` : ''}`, { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) throw new Error('i18n_unavailable');
  const data = await response.json();
  if (generation !== languageRequest) return;
  strings = data.strings;
  document.documentElement.lang = data.lang;
  translate();
  document.querySelectorAll('template').forEach(template => translate(template.content));
  document.querySelectorAll('[data-locale]').forEach(button =>
    button.setAttribute('aria-checked', String(button.dataset.locale === data.lang)));
  document.dispatchEvent(new CustomEvent('language-changed', { detail: { lang: data.lang } }));
  const menu = document.querySelector('#languageMenu');
  if (menu && !languageMenuReady) {
    languageMenuReady = true;
    menu.addEventListener('click', async event => {
      const button = event.target.closest('[data-locale]');
      if (!button || !languages.includes(button.dataset.locale)) return;
      try {
        await loadI18n(button.dataset.locale);
        try { localStorage.setItem('dtw-web-language', document.documentElement.lang); } catch { /* Optional storage. */ }
        document.querySelector('#languageButton').focus();
      } catch { menu.querySelector('.now').textContent = t('error.generic'); }
    });
  }
}
