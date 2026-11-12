let strings = {};

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

export async function loadI18n() {
  const response = await fetch('/api/i18n', { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) throw new Error('i18n_unavailable');
  const data = await response.json();
  strings = data.strings;
  document.documentElement.lang = data.lang;
  translate();
  document.querySelectorAll('template').forEach(template => translate(template.content));
}
