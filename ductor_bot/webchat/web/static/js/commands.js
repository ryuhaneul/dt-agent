import { api } from './api.js';
import { t } from './i18n.js';
import { esc } from './md.js';

export function initCommands(input, onChoose) {
  const menu = document.querySelector('#commandMenu'), list = document.querySelector('#commandList');
  let catalog = [], filtered = [], selected = 0, enabled = false, generation = 0;
  function close() {
    menu.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  function fit() {
    const rect = input.closest?.('.composer')?.getBoundingClientRect?.();
    if (!rect || !menu.style) return;
    const header = document.querySelector('[data-view="chat"] .pane-head')?.getBoundingClientRect?.();
    const top = Math.max(globalThis.visualViewport?.offsetTop || 0, header?.bottom || 0);
    menu.style.maxHeight = `${Math.max(0, rect.top - top - 8)}px`;
  }
  function render(reset = true) {
    const query = input.value.match(/^\s*\/([a-z0-9_]*)$/i);
    if (!enabled || !catalog.length || !query) { close(); return; }
    filtered = catalog.filter(command => command.name.startsWith(query[1].toLowerCase()));
    if (reset) selected = 0;
    selected = Math.min(selected, filtered.length - 1);
    document.querySelector('#commandCount').textContent = `${filtered.length} / ${catalog.length}`;
    list.innerHTML = filtered.length ? filtered.map((command, index) =>
      `<div class="command-option" id="command-option-${index}" role="option" aria-selected="${index === selected}" data-index="${index}" title="${esc(command.description)}"><code>/${esc(command.name)}</code><span>${esc(command.description)}</span></div>`
    ).join('') : `<div class="command-empty">${esc(t('commands.empty'))}</div>`;
    menu.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    fit();
    if (filtered.length) {
      input.setAttribute('aria-activedescendant', `command-option-${selected}`);
      list.querySelector(`#command-option-${selected}`)?.scrollIntoView?.({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  }
  function choose() {
    const command = filtered[selected];
    if (!command) return;
    input.value = `/${command.name} `;
    close();
    input.focus();
    input.setSelectionRange?.(input.value.length, input.value.length);
    onChoose(input.value);
  }
  function keydown(event) {
    if (menu.hidden || event.isComposing || event.shiftKey) return false;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (filtered.length) {
        selected = (selected + (event.key === 'ArrowDown' ? 1 : filtered.length - 1)) % filtered.length;
        render(false);
      }
      return true;
    }
    if (['Enter', 'Tab'].includes(event.key) && filtered.length) {
      event.preventDefault(); choose(); return true;
    }
    if (event.key === 'Escape') { event.preventDefault(); close(); return true; }
    if (event.key === 'Tab') close();
    return false;
  }
  input.addEventListener('input', () => render());
  input.addEventListener('focus', () => render());
  list.addEventListener('pointerdown', event => {
    const option = event.target.closest?.('[data-index]');
    if (!option) return;
    event.preventDefault(); selected = Number(option.dataset.index); choose();
  });
  document.addEventListener('click', event => { if (!event.target.closest?.('.composer')) close(); });
  document.addEventListener('topic-selected', close);
  document.addEventListener('route-change', close);
  function refreshCatalog() {
    const current = ++generation;
    catalog = []; filtered = []; close();
    if (enabled) {
      void api(`/commands?lang=${encodeURIComponent(document.documentElement.lang || 'en')}`).then(data => {
        if (!enabled || current !== generation) return;
        catalog = data.commands;
        if (document.activeElement === input) render();
      }).catch(() => { if (current === generation) close(); });
    }
  }
  document.addEventListener('auth-change', event => {
    enabled = event.detail.authenticated;
    refreshCatalog();
  });
  document.addEventListener('language-changed', () => { if (enabled) refreshCatalog(); });
  globalThis.addEventListener?.('resize', () => { if (!menu.hidden) fit(); });
  globalThis.visualViewport?.addEventListener('resize', () => { if (!menu.hidden) fit(); });
  return { close, keydown };
}
