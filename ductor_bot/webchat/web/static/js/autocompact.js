import { api } from './api.js';
import { t } from './i18n.js';

export function initAutocompact(getTopic, onSaved) {
  const dialog = document.querySelector('#autocompactDlg');
  const close = document.querySelector('#autocompactClose');
  const cancel = document.querySelector('#autocompactCancel');
  const save = document.querySelector('#autocompactSave');
  const notice = document.querySelector('#autocompactError');
  const subtitle = document.querySelector('#autocompactContext');
  const follow = document.querySelector('#autocompactFollow');
  const effective = document.querySelector('#autocompactEffective');
  const warning = document.querySelector('#autocompactUnsupported');
  const automationButton = document.querySelector('#autocompactAutomations');
  const automationList = document.querySelector('#acAutomationList');
  const automationRetry = document.querySelector('#acAutomationRetry');
  const automationNumber = document.querySelector('#ac-automation-number');
  const scopes = ['topic', 'global'];
  const fields = Object.fromEntries(scopes.map(scope => [scope, document.querySelector(`#ac-${scope}-number`)]));
  let snapshot = null, topic = null, opener = null, authenticated = false, busy = false, version = 0;
  let modes = {}, errorKey = null;
  let view = 'settings', automations = null, item = null, itemMode = null, itemFocus = null;
  const mode = value => value === null ? 'inherit' : typeof value === 'number' ? 'tokens' : value;
  const value = scope => modes[scope] === 'inherit' ? null : modes[scope] === 'tokens'
    ? Number(fields[scope].value) : modes[scope];
  const valid = scope => modes[scope] !== 'tokens' || (/^\d+$/.test(fields[scope].value)
    && Number.isInteger(value(scope)) && value(scope) >= 100000 && value(scope) <= 1000000);
  const path = () => `/topics/${encodeURIComponent(topic.tid)}/autocompact`;
  function format(current) {
    return current === null ? t('autocompact.inherit') : typeof current === 'number'
      ? t('autocompact.tokens', { count: new Intl.NumberFormat(document.documentElement.lang).format(current) })
      : t(`autocompact.${current}`);
  }
  function render() {
    dialog.dataset.ready = String(!!snapshot);
    const loading = errorKey === 'autocompact.loading';
    notice.setAttribute('role', loading ? 'status' : 'alert');
    notice.classList.toggle('ac-error', !loading); notice.classList.toggle('hint', loading);
    notice.hidden = !errorKey;
    notice.textContent = errorKey ? t(errorKey) : '';
    document.querySelector('#acSettings').hidden = view !== 'settings';
    document.querySelector('#acAutomations').hidden = view !== 'list';
    document.querySelector('#acAutomationEditor').hidden = view !== 'editor';
    const cancelKey = view === 'list' ? 'autocompact.back' : 'common.cancel';
    cancel.dataset.i18n = cancelKey; cancel.textContent = t(cancelKey);
    save.hidden = view === 'list';
    document.querySelector('#autocompactTitle').textContent = t(view === 'settings' ? 'autocompact.title' : 'autocompact.automation');
    document.querySelector('#autocompactTitle').dataset.i18n = view === 'settings' ? 'autocompact.title' : 'autocompact.automation';
    if (!snapshot) return;
    if (view === 'list') renderAutomationList();
    if (view === 'editor') renderAutomationEditor();
    const provider = snapshot.provider;
    subtitle.textContent = `${topic.name} · ${provider === 'claude' ? 'Claude' : provider === 'codex' ? 'Codex' : provider === 'grok' ? 'Grok' : 'Gemini'}`;
    for (const scope of scopes) {
      dialog.querySelectorAll(`[data-ac-scope="${scope}"]`).forEach(button => {
        button.setAttribute('aria-pressed', String(modes[scope] === button.dataset.acMode));
      });
      document.querySelector(`#ac-${scope}-custom`).hidden = modes[scope] !== 'tokens';
      const invalid = !valid(scope);
      document.querySelector(`#ac-${scope}-error`).hidden = !invalid;
      fields[scope].setAttribute('aria-invalid', String(invalid));
    }
    follow.hidden = modes.topic !== 'inherit';
    follow.textContent = `${t('autocompact.inherit')} → ${valid('global') ? format(value('global')) : '—'}`;
    const selected = value('topic') === null ? 'global' : 'topic';
    effective.textContent = valid(selected) ? format(value(selected)) : '—';
    const unsupported = ['grok', 'gemini'].includes(provider) ? 'unsupported'
      : provider === 'codex' && scopes.some(scope => modes[scope] === 'off') ? 'off_unsupported' : null;
    warning.hidden = !unsupported;
    warning.textContent = unsupported ? t(`autocompact.${unsupported}`, { provider: subtitle.textContent.split(' · ').at(-1) }) : '';
  }
  function setBusy(next) {
    busy = next;
    dialog.setAttribute('aria-busy', String(next));
    scopes.forEach(scope => { document.querySelector(`#ac-${scope}-scope`).disabled = next || !snapshot; });
    save.disabled = next || !snapshot || (view === 'editor' && !item);
    automationButton.disabled = next || !snapshot;
    automationRetry.disabled = next;
    document.querySelector('#ac-automation-scope').disabled = next || !item;
    automationList.querySelectorAll('button').forEach(button => { button.disabled = next; });
    close.disabled = false; cancel.disabled = next;
    if (next && dialog.open) close.focus({ preventScroll: true });
  }
  function apply(state) {
    snapshot = state;
    scopes.forEach(scope => {
      const current = state[`${scope}_value`];
      modes[scope] = mode(current);
      fields[scope].value = typeof current === 'number' ? String(current) : '200000';
    });
    render();
  }
  async function show(selected, source = document.activeElement) {
    if (!authenticated || !selected || selected.read_only || selected.tid === 'unknown') return;
    const generation = ++version;
    topic = selected; opener = source; snapshot = null; view = 'settings'; automations = null; item = null; itemFocus = null; errorKey = 'autocompact.loading';
    render(); setBusy(true);
    if (!dialog.open) dialog.showModal();
    try {
      const state = await api(path());
      if (generation !== version || !authenticated || !dialog.open) return;
      errorKey = null; apply(state);
    } catch (error) {
      if (generation !== version || !authenticated || !dialog.open) return;
      errorKey = error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic'; render();
    } finally {
      if (generation === version) { setBusy(false); if (dialog.open) close.focus(); }
    }
  }
  async function submit() {
    if (busy || !snapshot) return;
    const invalid = scopes.find(scope => !valid(scope));
    if (invalid) { render(); fields[invalid].focus(); return; }
    const body = {};
    scopes.forEach(scope => { if (value(scope) !== snapshot[`${scope}_value`]) body[scope] = value(scope); });
    if (!Object.keys(body).length) { dialog.close(); return; }
    const generation = version;
    errorKey = null; render(); setBusy(true);
    try {
      const state = await api(path(), { method: 'PUT', body });
      if (generation !== version || !authenticated || !dialog.open) return;
      apply(state); setBusy(false); dialog.close(); onSaved(state);
    } catch (error) {
      if (generation !== version || !authenticated || !dialog.open) return;
      // A failed rollback can leave a persisted value: compare the next attempt
      // with the server's actual state while preserving the user's draft.
      try {
        const current = await api(path());
        if (generation === version && authenticated && dialog.open) snapshot = current;
      } catch { /* Keep the last known state. */ }
      if (generation !== version || !authenticated || !dialog.open) return;
      errorKey = error.status === 400 ? 'autocompact.invalid' : error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic';
      render(); notice.focus();
    } finally { if (generation === version) setBusy(false); }
  }
  function automationValue(entry) {
    return entry.value === null ? `${format(null)} → ${format(entry.effective_value)}` : format(entry.value);
  }
  function automationMeta(entry) {
    return [entry.schedule, t(entry.enabled ? 'autocompact.enabled' : 'autocompact.disabled')].filter(Boolean).join(' · ');
  }
  function renderAutomationList() {
    const focused = document.activeElement;
    const restore = automationList.contains(focused) ? { kind: focused.dataset.kind, id: focused.dataset.id } : null;
    automationList.replaceChildren();
    automationRetry.hidden = !errorKey || errorKey === 'autocompact.loading';
    if (!automations) return;
    if (!automations.items.length) {
      const empty = document.createElement('p'); empty.className = 'hint';
      empty.textContent = t('autocompact.automation_empty'); automationList.append(empty);
    }
    for (const entry of automations.items) {
      const card = document.createElement('article'); card.className = 'ac-automation-card';
      const kind = document.createElement('h3'); kind.textContent = t(`autocompact.${entry.kind}`);
      const body = document.createElement('div'); body.className = 'ac-automation-card-body';
      const heading = document.createElement('div');
      const name = document.createElement('h4'); name.textContent = entry.name;
      const meta = document.createElement('p'); meta.className = 'hint'; meta.textContent = automationMeta(entry);
      heading.append(name, meta);
      const current = document.createElement('div'); current.className = 'ac-effective';
      const label = document.createElement('span'); label.textContent = t('autocompact.current_value');
      const selected = document.createElement('b'); selected.textContent = automationValue(entry);
      current.append(label, selected);
      const change = document.createElement('button'); change.type = 'button'; change.className = 'btn btn-sm';
      change.textContent = t('autocompact.change_automation');
      change.setAttribute('aria-label', t('autocompact.change_named', { name: entry.name }));
      change.dataset.kind = entry.kind; change.dataset.id = entry.id;
      change.addEventListener('click', () => { if (!busy) editAutomation(entry); });
      body.append(heading, current, change); card.append(kind, body); automationList.append(card);
      if (restore?.kind === entry.kind && restore.id === entry.id) change.focus({ preventScroll: true });
    }
  }
  function editorValue() {
    return itemMode === 'inherit' ? null : itemMode === 'tokens' ? Number(automationNumber.value) : itemMode;
  }
  function validEditor() {
    return itemMode !== 'tokens' || (/^\d+$/.test(automationNumber.value) && Number.isInteger(editorValue())
      && editorValue() >= 100000 && editorValue() <= 1000000);
  }
  function renderAutomationEditor() {
    document.querySelector('#acAutomationName').textContent = item.name;
    document.querySelector('#acAutomationMeta').textContent = `${t(`autocompact.${item.kind}`)} · ${automationMeta(item)}`;
    document.querySelector('#acAutomationCurrent').textContent = automationValue(item);
    dialog.querySelectorAll('[data-ac-automation-mode]').forEach(button => {
      button.hidden = item.kind === 'cron' && button.dataset.acAutomationMode === 'inherit';
      button.setAttribute('aria-pressed', String(itemMode === button.dataset.acAutomationMode));
    });
    document.querySelector('#ac-automation-custom').hidden = itemMode !== 'tokens';
    const invalid = !validEditor();
    automationNumber.setAttribute('aria-invalid', String(invalid));
    document.querySelector('#ac-automation-error').hidden = !invalid;
  }
  function editAutomation(entry) {
    item = entry; itemFocus = { kind: entry.kind, id: entry.id }; itemMode = mode(entry.value);
    automationNumber.value = typeof entry.value === 'number' ? String(entry.value) : '200000';
    view = 'editor'; errorKey = null; render(); setBusy(false);
    dialog.querySelector('.ac-body').scrollTop = 0;
    dialog.querySelector(`[data-ac-automation-mode="${itemMode}"]`).focus();
  }
  function showAutomationList() {
    view = 'list'; errorKey = null; item = null; render(); setBusy(false);
    const target = [...automationList.querySelectorAll('button')].find(button =>
      button.dataset.kind === itemFocus?.kind && button.dataset.id === itemFocus?.id);
    (target || cancel).focus();
  }
  async function loadAutomations() {
    const generation = version;
    view = 'list'; item = null; errorKey = 'autocompact.loading'; automations = null; render(); setBusy(true);
    dialog.querySelector('.ac-body').scrollTop = 0;
    try {
      const state = await api(`${path()}/automations`);
      if (generation !== version || !authenticated || !dialog.open) return;
      automations = state; showAutomationList();
    } catch (error) {
      if (generation !== version || !authenticated || !dialog.open) return;
      errorKey = error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic';
      render();
    } finally {
      if (generation === version) { setBusy(false); if (dialog.open && errorKey) automationRetry.focus(); }
    }
  }
  async function submitAutomation() {
    if (busy || !item) return;
    if (!validEditor()) { render(); automationNumber.focus(); return; }
    if (editorValue() === item.value) { showAutomationList(); return; }
    const generation = version;
    errorKey = null; render(); setBusy(true);
    try {
      const state = await api(`${path()}/automations/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}`,
        { method: 'PUT', body: { value: editorValue() } });
      if (generation !== version || !authenticated || !dialog.open) return;
      automations = state; showAutomationList();
    } catch (error) {
      if (generation !== version || !authenticated || !dialog.open) return;
      errorKey = error.status === 400 ? 'autocompact.invalid' : error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic';
      render(); notice.focus();
    } finally { if (generation === version) setBusy(false); }
  }
  dialog.querySelectorAll('[data-ac-mode]').forEach(button => button.addEventListener('click', () => {
    if (busy || !snapshot) return;
    modes[button.dataset.acScope] = button.dataset.acMode;
    render();
  }));
  scopes.forEach(scope => fields[scope].addEventListener('input', render));
  save.addEventListener('click', () => { void (view === 'editor' ? submitAutomation() : submit()); });
  automationButton.addEventListener('click', () => { if (!busy) void loadAutomations(); });
  automationRetry.addEventListener('click', () => { if (!busy) void loadAutomations(); });
  dialog.querySelectorAll('[data-ac-automation-mode]').forEach(button => button.addEventListener('click', () => {
    if (busy || !item) return;
    itemMode = button.dataset.acAutomationMode; render();
  }));
  automationNumber.addEventListener('input', render);
  close.addEventListener('click', () => { dialog.close(); });
  cancel.addEventListener('click', () => {
    if (busy) return;
    if (view === 'settings') dialog.close();
    else if (view === 'list') {
      view = 'settings'; errorKey = null; render(); setBusy(false); automationButton.focus();
    } else showAutomationList();
  });
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...dialog.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')]
      .filter(element => element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    const current = document.activeElement;
    if (event.shiftKey && (current === first || !controls.includes(current))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (current === last || !controls.includes(current))) {
      event.preventDefault(); first.focus();
    }
  });
  dialog.addEventListener('close', () => {
    // Native close events are queued: a newly reopened modal owns its new state.
    if (dialog.open) return;
    ++version; snapshot = null; topic = null; errorKey = null; modes = {}; view = 'settings'; automations = null; item = null; itemFocus = null; setBusy(false);
    if (authenticated && opener?.isConnected !== false) opener?.focus();
    opener = null;
  });
  document.addEventListener('language-changed', () => { if (dialog.open) render(); });
  document.addEventListener('topic-selected', () => { if (dialog.open) dialog.close(); });
  document.addEventListener('route-change', event => { if (event.detail.view !== 'chat' && dialog.open) dialog.close(); });
  document.addEventListener('auth-change', event => {
    authenticated = event.detail.authenticated;
    if (!authenticated) { ++version; if (dialog.open) dialog.close(); }
  });
  document.querySelector('#headerAutocompact').addEventListener('click', event => { void show(getTopic(), event.currentTarget); });
  return { show };
}
