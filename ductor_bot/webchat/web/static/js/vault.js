import { api } from './api.js';
import { t } from './i18n.js';
import { closeMenus } from './theme.js';

const lock = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/></svg>';
const unlock = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 7-2M12 14v3"/></svg>';
const durations = [900, 1800, 3600, 14400, 43200, 86400, 0];
const esc = value => String(value).replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' })[char]);
const text = (key, params) => t(`vault.${key}`, params);
const node = (tag, cls, value) => { const result = document.createElement(tag); result.className = cls; if (value !== undefined) result.textContent = value; return result; };

export function initVault() {
  const $ = id => document.getElementById(id);
  const dialog = $('vaultDlg'), content = $('vaultContent'), notice = $('vaultNotice'), badge = $('vaultBadge');
  let authenticated = false, status = null, keys = [], view = 'master', selected = '', opener = null;
  let epoch = 0, refreshSequence = 0, authEpoch = 0, busy = false, deadline = null, timer = null;
  let revealDeadline = null, revealTimer = null, noticeKey = null, noticeParams = {};
  let lockedUntil = 0;
  const isOpen = () => status?.state === 'unlocked';
  const isList = () => ['master', 'secrets'].includes(view);
  const duration = seconds => seconds === 0 ? text('unlimited') : seconds % 60 ? text('seconds', { n:seconds }) : text(seconds % 3600 ? 'minutes' : 'hours', { n:seconds / (seconds % 3600 ? 60 : 3600) });
  const button = (label, action, cls = '') => `<button class="btn ${cls}" type="button" data-vault-go="${action}">${esc(label)}</button>`;
  function clearInputs() { content.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; }); }
  function hideValue() {
    clearTimeout(revealTimer); revealTimer = null; revealDeadline = null;
    const output = $('vaultRevealed'); if (output) { output.textContent = ''; output.remove(); }
  }
  function showNotice(key = null, params = {}) {
    noticeKey = key; noticeParams = params;
    notice.textContent = key ? (key.startsWith('error.') ? t(key, params) : text(key, params)) : '';
    notice.hidden = !notice.textContent;
  }
  function changeView(next) {
    ++epoch; hideValue(); clearInputs(); busy = false; view = next; showNotice(); render();
    if (next === 'secrets') void refresh();
    const target = content.querySelector('input:not(:disabled)') || content.querySelector('select:not(:disabled)') || content.querySelector('.vault-tabs [aria-selected="true"]') || content.querySelector('.vault-form-title');
    target?.focus();
  }
  function cleanup() {
    ++epoch; hideValue(); clearInputs(); content.replaceChildren(); showNotice(); busy = false; selected = '';
  }
  function close() { cleanup(); if (dialog.open) dialog.close(); }
  function open(next = 'master') {
    if (!authenticated) return;
    opener = document.activeElement?.closest('#accountMenu') ? $('accountButton') : document.activeElement;
    closeMenus(); cleanup(); view = next; dialog.showModal(); render(); void refresh();
    $('vaultClose').focus();
  }
  function updateBadge() {
    const state = status?.state;
    badge.replaceChildren(); badge.insertAdjacentHTML('afterbegin', state === 'unlocked' ? unlock : lock);
    badge.append(node('span', '', text(state || 'loading')));
    badge.classList.toggle('vault-warn', state === 'locked');
    badge.setAttribute('aria-label', `${text('title')}: ${text(state || 'loading')}`);
    $('vaultMenuState').textContent = state ? text(state) : '';
  }
  function summary() {
    const box = node('div', 'vault-status');
    const icon = node('div', 'vault-status-icon'); icon.innerHTML = isOpen() ? unlock : lock;
    const info = node('div', 'vault-status-info');
    info.append(node('p', 'vault-status-label', text(status.state)), node('p', 'vault-remaining'));
    const stateBadge = node('span', `vault-state-badge ${isOpen() ? 'vault-ok' : status.state === 'locked' ? 'vault-warn' : ''}`, text(status.state === 'unconfigured' ? 'before_encryption' : 'encrypted'));
    box.append(icon, info, stateBadge); return box;
  }
  function row(title, detail, label, action, cls = '') {
    const result = node('div', 'vault-row'), info = node('div', '');
    info.append(node('h3', '', title)); if (detail) info.append(node('p', '', detail));
    result.append(info); if (label) result.insertAdjacentHTML('beforeend', button(label, action, `btn-sm ${cls}`));
    return result;
  }
  function renderMaster() {
    content.append(summary());
    if (status.state === 'unconfigured') content.insertAdjacentHTML('beforeend', button(text('setup'), 'setup', 'btn-primary'));
    else if (status.state === 'locked') {
      content.insertAdjacentHTML('beforeend', button(text('unlock_action'), 'unlock', 'btn-primary'));
      content.append(row(text('ttl'), duration(status.ttl_seconds)));
    } else {
      content.append(row(text('master'), null, text('change_action'), 'change'));
      content.append(row(text('ttl'), duration(status.ttl_seconds), text('edit_action'), 'ttl'));
      content.append(row(text('store_lock'), null, text('lock_now'), 'lock', 'vault-danger'));
    }
  }
  function renderSecrets() {
    const heading = node('div', 'vault-section-head'), title = node('h2', '', text('secrets'));
    title.append(node('span', 'vault-count mono', ` ${keys.length}`)); heading.append(title);
    if (status.state !== 'locked') heading.insertAdjacentHTML('beforeend', button(text('add'), 'add', 'btn-sm btn-primary'));
    content.append(heading);
    if (status.state === 'locked') {
      content.append(node('div', 'vault-note', text('locked_notice')));
      content.insertAdjacentHTML('beforeend', button(text('unlock_action'), 'unlock', 'btn-primary')); return;
    }
    if (status.state === 'unconfigured') {
      const prompt = node('div', 'vault-note vault-setup-note', text('setup_recommended'));
      prompt.insertAdjacentHTML('beforeend', button(text('setup'), 'setup', 'btn-sm')); content.append(prompt);
    }
    if (!keys.length) { content.append(node('p', 'vault-remaining', text('empty'))); return; }
    const list = node('div', 'vault-key-list');
    keys.forEach(key => {
      const row = node('div', 'vault-key-row'), actions = node('div', 'vault-key-actions');
      row.append(node('span', 'vault-key mono', key));
      [['view_value', 'verify', ''], ['edit_value', 'edit', ''], ['delete', 'delete', 'btn-ghost vault-danger']].forEach(([label, action, cls]) => {
        const control = node('button', `btn btn-sm ${cls}`, text(label)); control.type = 'button';
        control.disabled = action === 'verify' && status.state === 'unconfigured';
        if (control.disabled) control.title = text('reveal_requires_setup');
        control.addEventListener('click', () => { if (!busy) { selected = key; changeView(action); } }); actions.append(control);
      });
      row.append(actions); list.append(row);
    }); content.append(list);
    if (status.state === 'unconfigured') content.append(node('p', 'vault-remaining', text('reveal_requires_setup')));
  }
  function field(id, label, password = true) {
    return `<div class="vault-form-group"><label for="${id}">${esc(text(label))}</label><input class="field ${password ? '' : 'mono'}" id="${id}" type="${password ? 'password' : 'text'}" autocomplete="off" autocapitalize="off" spellcheck="false" required${password ? '' : ' maxlength="56" pattern="[A-Z][A-Z0-9_]{0,55}" placeholder="API_KEY"'}></div>`;
  }
  function renderForm() {
    const secrets = ['add', 'edit', 'delete', 'verify', 'revealed'].includes(view), back = secrets ? 'secrets' : 'master';
    content.insertAdjacentHTML('beforeend', button(`← ${text(secrets ? 'secrets' : 'master')}`, back, 'vault-back'));
    content.append(node('h2', 'vault-form-title', text({ setup:'setup', unlock:'unlock_action', change:'change', ttl:'ttl_change', add:'add', edit:'edit_title', delete:'delete_title', verify:'reveal_title', revealed:'reveal_title' }[view])));
    content.querySelector('.vault-form-title').tabIndex = -1;
    if (secrets && view !== 'add') {
      if (view === 'delete') content.append(node('p', 'mono', selected));
      else { const group = node('div', 'vault-form-group'); group.append(node('label', '', text('key_name')), node('p', 'mono', selected)); content.append(group); }
    }
    if (view === 'revealed') {
      const group = node('div', 'vault-form-group'), row = node('div', 'vault-reveal-row');
      const output = node('output', 'field mono'); output.id = 'vaultRevealed';
      const copy = node('button', 'btn', text('copy')); copy.id = 'vaultCopy'; copy.type = 'button';
      copy.addEventListener('click', async () => {
        const version = epoch;
        try { await navigator.clipboard.writeText(output.textContent); if (version === epoch && dialog.open) copy.textContent = text('copied'); }
        catch { if (version === epoch && dialog.open) copy.textContent = text('copy_failed'); }
      });
      row.append(output, copy); group.append(node('label', '', text('value')), row);
      const remaining = node('p', 'vault-remaining'); remaining.id = 'vaultRevealRemaining'; remaining.setAttribute('role', 'timer');
      group.append(remaining); content.append(group);
      content.prepend(notice); content.insertAdjacentHTML('beforeend', `<div class="vault-actions">${button(t('common.close'), 'secrets', 'btn-primary')}</div>`); return;
    }
    const form = node('form', ''); form.id = 'vaultForm'; form.autocomplete = 'off';
    let fields = '', submit = t('common.save');
    if (view === 'setup') { fields += `<div class="vault-note">${esc(text('migration_notice'))}</div>`; submit = text('setup_submit'); }
    if (view === 'change') fields += field('vaultCurrentKey', 'current_key');
    if (['setup', 'unlock', 'change', 'verify'].includes(view)) fields += field('vaultMasterKey', view === 'change' ? 'new_key' : 'master');
    if (['setup', 'change'].includes(view)) fields += field('vaultConfirmKey', view === 'change' ? 'confirm_new' : 'confirm_key');
    if (view === 'unlock') submit = text('unlock_action');
    if (view === 'verify') submit = text('view_value');
    if (view === 'change') submit = text('change');
    if (view === 'add') fields += field('vaultKeyName', 'key_name', false);
    if (['add', 'edit'].includes(view)) fields += field('vaultSecretValue', 'value');
    if (view === 'delete') { fields += `<p class="vault-delete-question">${esc(text('delete_confirm'))}</p>`; submit = text('delete'); }
    if (view === 'ttl') {
      const choices = durations.includes(status.ttl_seconds) ? durations : [status.ttl_seconds, ...durations];
      fields += `<div class="vault-form-group"><label for="vaultTtl">${esc(text('auto_lock'))}</label><select class="field" id="vaultTtl">${choices.map(value => `<option value="${value}"${value === status.ttl_seconds ? ' selected' : ''}>${esc(duration(value))}</option>`).join('')}</select></div><label class="vault-check"><input id="vaultUnlimited" type="checkbox"${status.ttl_seconds === 0 ? ' checked' : ''}>${esc(text('unlimited'))}</label><div id="vaultTtlAuth"></div>`;
    }
    form.innerHTML = fields + `<div class="vault-actions">${button(t('common.cancel'), back)}<button class="btn btn-primary${view === 'delete' ? ' vault-delete-submit' : ''}" id="vaultSubmit" type="submit">${esc(submit)}</button></div>`;
    form.prepend(notice); content.append(form);
    if (view === 'ttl') {
      $('vaultTtl').disabled = $('vaultUnlimited').checked;
      const updateTtlAuth = () => {
        const value = $('vaultUnlimited').checked ? 0 : Number($('vaultTtl').value);
        const needsKey = status.state !== 'unconfigured' && status.ttl_seconds !== 0 && (value === 0 || value > status.ttl_seconds);
        const group = $('vaultTtlAuth');
        if (needsKey && !group.firstChild) group.innerHTML = field('vaultTtlKey', 'current_key');
        else if (!needsKey) { group.querySelectorAll('input').forEach(input => { input.value = ''; }); group.replaceChildren(); }
      };
      $('vaultUnlimited').addEventListener('change', event => { $('vaultTtl').disabled = event.target.checked; if (event.target.checked) $('vaultTtl').value = '0'; updateTtlAuth(); });
      $('vaultTtl').addEventListener('change', updateTtlAuth);
      updateTtlAuth();
    }
    form.addEventListener('submit', submitForm);
  }
  function render() {
    if (!dialog.open) return;
    hideValue(); clearInputs(); content.replaceChildren();
    $('vaultTitle').textContent = text('title'); $('vaultClose').setAttribute('aria-label', t('common.close'));
    if (!status) { content.append(notice, node('p', 'vault-remaining', text('loading'))); return; }
    if (isList()) {
      content.append(notice);
      const tabs = node('nav', 'vault-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', text('title'));
      [['master', 'master'], ['secrets', 'secrets']].forEach(([next, label]) => {
        const control = node('button', view === next ? 'active' : '', text(label)); control.type = 'button'; control.setAttribute('role', 'tab'); control.setAttribute('aria-selected', String(view === next));
        control.addEventListener('click', () => { if (!busy) changeView(next); }); tabs.append(control);
      });
      tabs.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const controls = [...tabs.querySelectorAll('button')], index = controls.indexOf(document.activeElement);
        controls[event.key === 'Home' ? 0 : event.key === 'End' ? 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + controls.length) % controls.length].click();
      }); content.append(tabs);
      if (view === 'master') renderMaster(); else renderSecrets();
    } else renderForm();
    tick();
  }
  async function refresh() {
    if (!authenticated) return;
    const sequence = ++refreshSequence, generation = authEpoch;
    try {
      const current = await api('/masterkey/status');
      if (!authenticated || generation !== authEpoch || sequence !== refreshSequence) return;
      status = current; deadline = current.remaining_seconds === null ? null : performance.now() + current.remaining_seconds * 1000;
      updateBadge();
      if (current.state === 'locked' && !['master', 'secrets', 'unlock'].includes(view)) { changeView('unlock'); }
      if (dialog.open && view === 'secrets' && current.state !== 'locked') {
        const version = epoch, result = await api('/secrets/keys');
        if (version !== epoch || !dialog.open || !authenticated || generation !== authEpoch || sequence !== refreshSequence) return;
        keys = result.keys;
      } else if (current.state === 'locked') keys = [];
      if (dialog.open && isList()) render();
      if (current.reason === 'INVALID_VAULT_CONFIG' && dialog.open) showNotice('invalid_config');
      tick();
    } catch (error) {
      if (sequence === refreshSequence && authenticated && generation === authEpoch) {
        if (dialog.open) failure(error);
        else { status = null; updateBadge(); }
      }
    }
  }
  function failure(error) {
    const key = { MASTER_KEY_INVALID:'wrong_key', MASTER_KEY_REQUIRED:'ttl_key_required', INVALID_VAULT_CONFIG:'invalid_config', MASTER_KEY_MISMATCH:'mismatch', MASTER_KEY_TOO_SHORT:'too_short', SECRETS_RATE_LIMITED:'rate_limited', WEB_AUTH_MIGRATION_REQUIRED:'web_migration', SECRETS_FILE_SYMLINK:'file_symlink', SECRETS_LOCKED:'locked_notice', MASTER_KEY_NOT_SET:'reveal_requires_setup', KEY_INVALID:'invalid_name', SECRET_NOT_SET:'missing', VALUE_EMPTY:'invalid_value', VALUE_TOO_LONG:'invalid_value', VALUE_MULTILINE:'invalid_value', too_large:'too_large' }[error.code] || (error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    if (error.code === 'SECRETS_RATE_LIMITED') lockedUntil = performance.now() + (error.retryAfter || 1) * 1000;
    showNotice(key, { seconds: error.retryAfter || 1 }); notice.focus();
  }
  async function submitForm(event) {
    event.preventDefault();
    if (busy || !authenticated) return;
    if (performance.now() < lockedUntil) { showNotice('rate_limited', { seconds: Math.ceil((lockedUntil - performance.now()) / 1000) }); return; }
    const operation = view, version = epoch;
    let body = {}, path;
    if (['setup', 'unlock', 'verify'].includes(operation)) body.master_key = $('vaultMasterKey').value;
    if (operation === 'setup') body.confirm_key = $('vaultConfirmKey').value;
    if (operation === 'change') body = { current_key: $('vaultCurrentKey').value, new_key: $('vaultMasterKey').value, confirm_key: $('vaultConfirmKey').value };
    if (['setup', 'change'].includes(operation) && (body.master_key ?? body.new_key) !== body.confirm_key) { clearInputs(); showNotice('mismatch'); return; }
    if (operation === 'add' && keys.includes($('vaultKeyName').value)) { clearInputs(); showNotice('duplicate'); return; }
    if (operation === 'ttl') {
      body.ttl_seconds = $('vaultUnlimited').checked ? 0 : Number($('vaultTtl').value);
      if ($('vaultTtlKey')) body.master_key = $('vaultTtlKey').value;
    }
    if (['add', 'edit'].includes(operation)) body = { key: operation === 'add' ? $('vaultKeyName').value : selected, value: $('vaultSecretValue').value };
    if (['delete', 'verify'].includes(operation)) body.key = selected;
    path = ['add', 'edit', 'delete', 'verify'].includes(operation) ? `/secrets/${{ add:'set', edit:'set', delete:'delete', verify:'reveal' }[operation]}` : `/masterkey/${operation}`;
    clearInputs(); busy = true; showNotice(); content.querySelectorAll('button,input,select').forEach(control => { control.disabled = true; });
    const response = api(path, { method:'POST', body });
    Object.keys(body).forEach(key => { body[key] = ''; }); body = null;
    try {
      const result = await response;
      if (version !== epoch || !dialog.open || !authenticated) { if (Object.hasOwn(result, 'value')) result.value = ''; return; }
      busy = false;
      if (operation === 'verify') {
        changeView('revealed'); $('vaultRevealed').textContent = result.value; result.value = '';
        revealDeadline = performance.now() + 30000; revealTimer = setTimeout(() => changeView('verify'), 30000); tick();
      } else { changeView(['add', 'edit', 'delete'].includes(operation) ? 'secrets' : 'master'); await refresh(); }
    } catch (error) {
      if (version === epoch && dialog.open && authenticated) { busy = false; render(); failure(error); }
    }
  }
  function tick() {
    const remaining = content.querySelector('.vault-status-info .vault-remaining');
    if (remaining && status) remaining.textContent = isOpen() ? deadline === null ? text('unlimited') : text('remaining', { minutes: Math.floor(Math.ceil(Math.max(0, deadline - performance.now()) / 1000) / 60), seconds: Math.ceil(Math.max(0, deadline - performance.now()) / 1000) % 60 }) : text(status.state === 'locked' ? 'key_required' : 'setup_required');
    if (revealDeadline !== null) {
      if (performance.now() >= revealDeadline) { changeView('verify'); return; }
      const label = $('vaultRevealRemaining'); if (label) label.textContent = text('hide_after', { seconds: Math.ceil((revealDeadline - performance.now()) / 1000) });
    }
    if (isOpen() && deadline !== null && performance.now() >= deadline) { deadline = null; status = { ...status, state:'locked' }; hideValue(); clearInputs(); updateBadge(); if (dialog.open) changeView('master'); void refresh(); }
  }
  content.addEventListener('click', event => {
    const target = event.target.closest('[data-vault-go]'); if (!target || busy) return;
    const next = target.dataset.vaultGo;
    if (next === 'lock') {
      ++epoch; const version = epoch; busy = true; clearInputs(); hideValue();
      void api('/masterkey/lock', { method:'POST', body:{} }).then(() => { if (version === epoch && dialog.open && authenticated) { busy = false; changeView('master'); void refresh(); } }).catch(error => { if (version === epoch && dialog.open && authenticated) { busy = false; failure(error); } });
    } else changeView(next);
  });
  badge.addEventListener('click', () => open(status?.state === 'locked' ? 'unlock' : 'master'));
  $('openVault').addEventListener('click', () => open('master'));
  $('openSecrets').addEventListener('click', () => open('secrets'));
  $('vaultClose').addEventListener('click', close);
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('close', () => { cleanup(); if (authenticated && opener?.isConnected && !opener.hidden) opener.focus(); opener = null; });
  document.addEventListener('route-change', close);
  document.addEventListener('vault-refresh', () => { if (view === 'revealed') changeView('verify'); void refresh(); });
  document.addEventListener('visibilitychange', tick);
  document.addEventListener('language-changed', () => { updateBadge(); if (view === 'revealed') changeView('verify'); else if (dialog.open) render(); showNotice(noticeKey, noticeParams); });
  document.addEventListener('auth-change', event => {
    authenticated = event.detail.authenticated; ++authEpoch; ++refreshSequence; clearInterval(timer); timer = null;
    if (!authenticated) { close(); status = null; keys = []; deadline = null; lockedUntil = 0; updateBadge(); }
    else { void refresh(); timer = setInterval(tick, 1000); }
  });
  updateBadge();
}
