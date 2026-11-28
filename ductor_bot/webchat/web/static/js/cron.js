import { api } from './api.js';
import { t } from './i18n.js';
import { esc } from './md.js';
import { parseAutocompactTokens } from './autocompact.js';

export function presetCron({ type, time = '09:00', days = [1], interval = 4 }) {
  if (type === 'hours') return Number.isInteger(+interval) && +interval >= 1 && +interval <= 23 ? `0 */${+interval} * * *` : null;
  const match = /^(\d{2}):(\d{2})$/.exec(time);
  if (!match || +match[1] > 23 || +match[2] > 59) return null;
  const weekdays = [...new Set(days.map(Number))].sort((a, b) => a - b);
  if (type === 'weekly' && (!weekdays.length || weekdays.some(day => !Number.isInteger(day) || day < 0 || day > 6))) return null;
  if (!['daily', 'weekdays', 'weekly'].includes(type)) return null;
  return `${+match[2]} ${+match[1]} * * ${type === 'daily' ? '*' : type === 'weekdays' ? '1-5' : weekdays.join(',')}`;
}

export function cronPreset(expression) {
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5 || fields[2] !== '*' || fields[3] !== '*') return null;
  const [minute, hour, , , day] = fields;
  if (minute === '0' && /^\*\/(?:[1-9]|1\d|2[0-3])$/.test(hour) && day === '*') return { type: 'hours', interval: +hour.slice(2), time: '09:00', days: [1] };
  if (!/^\d{1,2}$/.test(minute) || +minute > 59 || !/^\d{1,2}$/.test(hour) || +hour > 23) return null;
  const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;
  if (day === '*' || day === '1-5') return { type: day === '*' ? 'daily' : 'weekdays', time, days: [1], interval: 4 };
  if (!/^[0-7](?:,[0-7])*$/.test(day)) return null;
  return { type: 'weekly', time, days: [...new Set(day.split(',').map(value => +value % 7))].sort((a, b) => a - b), interval: 4 };
}

export function initCron(getTopic) {
  const dialog = document.querySelector('#cronDlg'), content = document.querySelector('#cronContent');
  const notice = document.querySelector('#cronError'), close = document.querySelector('#cronClose');
  let topic = null, jobs = [], defaults = {}, selected = null, draft = null, creating = false;
  let view = 'list', busy = false, authenticated = false, version = 0, detailVersion = 0, previewVersion = 0;
  let previewTimer = null, preview = null, warning = null, opener = null, errorKey = null;
  const path = () => `/topics/${encodeURIComponent(topic.tid)}/cron`;
  const jobPath = id => `${path()}/${encodeURIComponent(id)}`;
  const alive = generation => generation === version && dialog.open && authenticated;
  const button = (label, action, extra = '') => `<button type="button" class="btn ${extra}" data-cron-action="${action}">${esc(t(label))}</button>`;
  const field = (label, id, value, attributes = '') => `<label class="cron-label" for="${id}">${esc(t(label))}</label><input class="field" id="${id}" value="${esc(value ?? '')}" ${attributes}>`;
  const delivery = value => t(`cron.delivery_${value}`);
  const acLabel = value => value === null ? t('autocompact.inherit') : typeof value === 'number' ? t('autocompact.tokens', { count: new Intl.NumberFormat(document.documentElement.lang).format(value) }) : t(`autocompact.${value}`);
  const target = job => job.target?.chat_id ? job.target.name : t('cron.broadcast');
  const sizeLabel = size => size < 1024 ? `${size} B` : size < 1024 ** 2 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 ** 2).toFixed(1)} MiB`;
  const limitLabel = () => sizeLabel(defaults.content_limit);
  function error(key = null) {
    errorKey = key; notice.hidden = !key;
    notice.textContent = key ? t(key, key === 'cron.too_large' ? { limit: limitLabel() } : {}) : '';
  }
  function setBusy(value) {
    busy = value; dialog.setAttribute('aria-busy', String(value));
    dialog.querySelectorAll('button,input,select,textarea').forEach(control => { if (control !== close) control.disabled = value || control.dataset.cronReadonly === 'true'; });
  }
  function secret(evidence) {
    return `${evidence?.requires_secrets ? `<div class="cron-warning" role="status">${esc(t('cron.secret_warning', { keys: evidence.keys.join(', ') || '—' }))}</div>` : ''}<p class="hint cron-limit">${esc(t('cron.secret_limitations'))}</p>`;
  }
  const scheduleText = job => esc(job.schedule_summary ?? t('cron.invalid_schedule'));
  const scheduleClass = job => job.schedule_summary === null ? 'cron-schedule cron-invalid' : 'cron-schedule';
  function card(job) {
    return `<article class="cron-job ${selected?.id === job.id ? 'is-selected' : ''}"><div class="cron-row"><button class="cron-job-title grow" type="button" data-cron-id="${esc(job.id)}">${esc(job.title)}</button><div class="cron-switch-wrap"><span>${esc(t(job.enabled ? 'cron.enabled' : 'cron.disabled'))}</span><button class="cron-switch" type="button" role="switch" aria-checked="${!!job.enabled}" aria-label="${esc(t('cron.toggle_named', { name: job.title }))}" data-cron-toggle="${esc(job.id)}"></button></div></div><p class="${scheduleClass(job)}">${scheduleText(job)} <span class="hint">· ${esc(job.timezone)}</span></p><dl class="cron-meta"><div><dt>${esc(t('cron.last_run'))}</dt><dd>${esc(job.last_run_at ? `${new Date(job.last_run_at).toLocaleString(document.documentElement.lang)} · ${job.last_run_status?.startsWith('success') ? t('cron.run_success') : job.last_run_status?.startsWith('error') ? t('cron.run_failed') : job.last_run_status || '—'}` : t('cron.never'))}</dd></div><div><dt>${esc(t('cron.target'))}</dt><dd>${esc(target(job))}</dd></div><div><dt>${esc(t('autocompact.title'))}</dt><dd>${esc(acLabel(job.autocompact))}</dd></div><div><dt>${esc(t('cron.delivery'))}</dt><dd>${esc(delivery(job.delivery_mode))}</dd></div></dl></article>`;
  }
  const hasTaskDetails = job => 'content_size' in job;
  const canEditContent = job => 'content' in job && job.content_editable !== false;
  function instructionNotice(job) {
    if (canEditContent(job)) return '';
    const size = sizeLabel(job.content_size);
    const message = 'content' in job
      ? t('cron.readonly_notice', { size, limit: limitLabel() })
      : t('cron.hidden_notice', { size });
    return `<p class="hint cron-limit">${esc(message)}</p>`;
  }
  function detail() {
    const job = selected, complete = hasTaskDetails(job);
    const instruction = complete ? `<h3>${esc(t('cron.instruction'))}</h3>${instructionNotice(job)}${'content' in job ? `<div class="cron-instruction">${esc(job.content)}</div>` : ''}` : '';
    const scripts = complete ? `<h3>${esc(t('cron.scripts'))}</h3><div class="cron-files mono">${job.scripts.length ? job.scripts.map(esc).join('<br>') : esc(t('cron.none'))}</div>${secret(job.secret_warning)}` : '';
    return `<section class="cron-detail"><div class="cron-body"><button class="cron-back" type="button" data-cron-action="list">← ${esc(t('cron.list'))}</button><div class="cron-detail-title"><div class="cron-row"><h3 class="grow">${esc(job.title)}</h3><span class="tag">${esc(t(job.enabled ? 'cron.enabled' : 'cron.disabled'))}</span></div><p class="${scheduleClass(job)}">${scheduleText(job)}</p><p class="hint mono">${esc(job.schedule)}</p></div>${instruction}<dl class="cron-summary"><div><dt>${esc(t('cron.timezone'))}</dt><dd>${esc(job.timezone)}</dd></div><div><dt>${esc(t('cron.target'))}</dt><dd>${esc(target(job))}</dd></div><div><dt>${esc(t('cron.delivery'))}</dt><dd>${esc(delivery(job.delivery_mode))}</dd></div><div><dt>${esc(t('autocompact.title'))}</dt><dd>${esc(acLabel(job.autocompact))}</dd></div></dl>${scripts}</div><footer class="cron-foot">${button('common.delete', 'delete', 'btn-danger')}${button('cron.list', 'list')}${complete ? button('cron.edit', 'edit', 'btn-primary') : ''}</footer></section>`;
  }
  function form() {
    const preset = draft.preset;
    const instruction = canEditContent(draft)
      ? `<label class="cron-label" for="cronInstruction">${esc(t('cron.instruction'))}</label><textarea id="cronInstruction" class="field" rows="6" required placeholder="${esc(t('cron.instruction_placeholder'))}">${esc(draft.content)}</textarea>`
      : `<p class="cron-label">${esc(t('cron.instruction'))}</p>${instructionNotice(draft)}`;
    return `<section class="cron-detail cron-edit"><form id="cronForm" class="cron-form"><div class="cron-body"><button class="cron-back" type="button" data-cron-action="list">← ${esc(t('cron.list'))}</button><div class="cron-formgrid"><div><div class="cron-group">${field('cron.name', 'cronName', draft.title, 'required maxlength="128"')}</div><fieldset class="cron-group"><legend>${esc(t('cron.schedule'))}</legend><div class="cron-schedulebox"><div class="cron-seg">${['preset', 'direct'].map(mode => `<button class="btn" type="button" data-cron-mode="${mode}" aria-pressed="${draft.mode === mode}">${esc(t(`cron.${mode}`))}</button>`).join('')}</div><div ${draft.mode !== 'preset' ? 'hidden' : ''}><label class="cron-label" for="cronPreset">${esc(t('cron.repeat'))}</label><select class="field" id="cronPreset">${['daily', 'weekdays', 'weekly', 'hours'].map(type => `<option value="${type}" ${preset.type === type ? 'selected' : ''}>${esc(t(`cron.${type}`))}</option>`).join('')}</select><div class="cron-days" ${preset.type !== 'weekly' ? 'hidden' : ''} role="group" aria-label="${esc(t('cron.days'))}">${Array.from({ length: 7 }, (_, day) => `<label><input type="checkbox" data-cron-day="${day}" ${preset.days.includes(day) ? 'checked' : ''}><span>${esc(t(`cron.day_${day}`))}</span></label>`).join('')}</div><div class="cron-control-space" ${preset.type === 'hours' ? 'hidden' : ''}>${field('cron.time', 'cronTime', preset.time, 'type="time"')}</div><div class="cron-control-space" ${preset.type !== 'hours' ? 'hidden' : ''}>${field('cron.interval', 'cronInterval', preset.interval, `type="number" min="1" max="23" data-cron-readonly="${draft.mode !== 'preset' || preset.type !== 'hours'}"`)}</div><p id="cronRaw" class="hint mono">${esc(draft.schedule)}</p></div><div ${draft.mode !== 'direct' ? 'hidden' : ''}>${field('cron.expression', 'cronExpression', draft.schedule, 'spellcheck="false"')}<p class="hint">0 9 * * 1-5</p></div><div class="cron-preview" aria-live="polite"><b>${esc(t('cron.preview'))}</b><div id="cronPreview"></div></div></div></fieldset><div class="cron-group">${field('cron.timezone', 'cronTimezone', draft.timezone, 'required spellcheck="false"')}</div></div><div><div class="cron-group">${instruction}<div id="cronWarning">${secret(warning)}</div></div><div class="cron-group">${field('cron.target', 'cronTarget', creating ? topic.name : target(selected), 'data-cron-readonly="true" disabled')}</div><div class="cron-group"><label class="cron-label" for="cronDelivery">${esc(t('cron.delivery'))}</label><select class="field" id="cronDelivery">${['inject', 'separate', 'in_session'].map(mode => `<option value="${mode}" ${draft.delivery_mode === mode ? 'selected' : ''}>${esc(delivery(mode))}</option>`).join('')}</select></div><fieldset class="cron-group"><legend>${esc(t('autocompact.title'))}</legend><div class="cron-chips">${['inherit', 'default', 'off', 'tokens'].map(mode => `<button class="btn cron-chip" type="button" data-cron-ac="${mode}" aria-pressed="${draft.acMode === mode}">${esc(t(`autocompact.${mode === 'tokens' ? 'input' : mode}`))}</button>`).join('')}</div><div class="cron-control-space" ${draft.acMode !== 'tokens' ? 'hidden' : ''}>${field('autocompact.token_count', 'cronTokens', draft.tokens, 'inputmode="numeric" placeholder="500k"')}<p class="hint">100k–1M</p></div></fieldset></div></div></div><footer class="cron-foot">${button('common.cancel', 'cancel')}<button class="btn btn-primary" type="submit">${esc(t(creating ? 'cron.create' : 'common.save'))}</button></footer></form></section>`;
  }
  function render() {
    document.querySelector('#cronTitle').textContent = t(view === 'edit' ? creating ? 'cron.create' : 'cron.edit' : view === 'delete' ? 'cron.delete' : 'cron.title');
    document.querySelector('#cronContext').textContent = topic?.name || '';
    if (view === 'delete') {
      content.innerHTML = `<section class="cron-detail"><div class="cron-body cron-confirm"><h3>${esc(t('cron.confirm_delete', { name: selected.title }))}</h3>${hasTaskDetails(selected) ? `<p>${esc(t('cron.archive_notice'))}</p>` : ''}</div><footer class="cron-foot">${button('common.cancel', 'cancel-delete')}${button('common.delete', 'confirm-delete', 'btn-danger')}</footer></section>`;
    } else {
      content.innerHTML = `<div class="cron-listbar"><b>${esc(t('cron.title'))} <span class="hint">${jobs.length}</span></b>${button('cron.create', 'create', 'btn-primary')}</div><div class="cron-split ${view !== 'list' ? 'has-detail' : ''}"><div class="cron-list scroll">${jobs.map(card).join('') || `<p class="cron-empty">${esc(t('cron.empty'))}</p>`}</div>${view === 'edit' ? form() : view === 'detail' && selected ? detail() : ''}</div>`;
    }
    renderPreview(); setBusy(busy);
  }
  function renderPreview() {
    const node = document.querySelector('#cronPreview');
    if (!node || view !== 'edit') return;
    node.replaceChildren();
    if (!preview) { node.textContent = t('cron.loading'); return; }
    if (!preview.valid) { node.textContent = t('cron.invalid_schedule'); node.classList.add('cron-invalid'); return; }
    node.classList.remove('cron-invalid');
    const summary = document.createElement('p'); summary.textContent = preview.schedule_summary; node.append(summary);
    const list = document.createElement('ol');
    for (const date of preview.next_runs) {
      const row = document.createElement('li');
      try { row.textContent = new Date(date).toLocaleString(document.documentElement.lang, { timeZone: preview.timezone }); }
      catch { row.textContent = date; }
      list.append(row);
    }
    node.append(list);
  }
  function readDraft() {
    if (view !== 'edit') return;
    const value = id => document.querySelector(`#${id}`).value;
    draft.title = value('cronName'); draft.timezone = value('cronTimezone').trim();
    if (canEditContent(draft)) draft.content = value('cronInstruction');
    draft.delivery_mode = value('cronDelivery'); draft.tokens = value('cronTokens');
    draft.preset = { type: value('cronPreset'), time: value('cronTime'), interval: value('cronInterval'), days: [...dialog.querySelectorAll('[data-cron-day]:checked')].map(input => +input.dataset.cronDay) };
    draft.schedule = draft.mode === 'direct' ? value('cronExpression') : presetCron(draft.preset) || '';
  }
  async function runPreview() {
    clearTimeout(previewTimer);
    if (!dialog.open || view !== 'edit' || !draft) return false;
    const request = ++previewVersion, generation = version;
    const schedule = draft.schedule, timezone = draft.timezone;
    try {
      const result = await api(`${path()}/preview`, { method: 'POST', body: { schedule, timezone } });
      if (!alive(generation) || request !== previewVersion || view !== 'edit') return false;
      preview = result; renderPreview();
      if (errorKey === 'error.bot_unreachable') error();
      return true;
    } catch (failure) {
      if (alive(generation) && request === previewVersion && view === 'edit') {
        preview = { valid: false }; renderPreview();
        if (failure.status !== 400) error('error.bot_unreachable');
        else if (errorKey === 'error.bot_unreachable') error();
      }
      return false;
    }
  }
  function queuePreview() { preview = null; ++previewVersion; renderPreview(); clearTimeout(previewTimer); previewTimer = setTimeout(() => { void runPreview(); }, 300); }
  async function select(id) {
    const generation = version, request = ++detailVersion;
    error(); setBusy(true);
    try {
      const job = await api(jobPath(id)).then(state => state.job, failure => {
        if (failure.status !== 404 || failure.code !== 'not_found') throw failure;
        const listed = jobs.find(entry => entry.id === id);
        if (!listed) throw failure;
        return listed;
      });
      if (!alive(generation) || request !== detailVersion) return;
      selected = job; view = 'detail'; draft = null; render();
    } catch { if (alive(generation) && request === detailVersion) error('error.generic'); }
    finally { if (alive(generation) && request === detailVersion) setBusy(false); }
  }
  function edit(fresh = false) {
    creating = fresh; warning = fresh ? null : selected.secret_warning;
    const value = fresh ? { title: '', content: '', schedule: '0 9 * * *', timezone: defaults.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone, delivery_mode: defaults.delivery_mode, autocompact: defaults.autocompact } : selected;
    const preset = cronPreset(value.schedule);
    draft = { ...value, id: fresh ? `cron-${crypto.randomUUID().slice(0, 12)}` : value.id, preset: preset || { type: 'daily', time: '09:00', days: [1], interval: 4 }, mode: preset ? 'preset' : 'direct', acMode: value.autocompact === null ? 'inherit' : typeof value.autocompact === 'number' ? 'tokens' : value.autocompact, tokens: typeof value.autocompact === 'number' ? String(value.autocompact) : '' };
    view = 'edit'; preview = null; error(); render(); queuePreview(); document.querySelector('#cronName').focus();
  }
  async function refresh(id = null) {
    const generation = version;
    const data = await api(path());
    if (!alive(generation)) return;
    jobs = data.jobs; defaults = data.defaults;
    if (id) { await select(id); return; }
    selected = null; view = 'list'; render();
    if (jobs.length && document.documentElement.clientWidth > 768) await select(jobs[0].id);
  }
  async function save() {
    if (busy) return;
    readDraft();
    const formNode = document.querySelector('#cronForm');
    const tokens = draft.acMode === 'tokens' ? parseAutocompactTokens(draft.tokens.trim() || '500k') : null;
    const tokenNode = document.querySelector('#cronTokens');
    tokenNode.setCustomValidity(draft.acMode === 'tokens' && tokens === null ? t('autocompact.invalid') : '');
    if (!formNode.reportValidity()) return;
    const editContent = canEditContent(draft);
    if (editContent && new TextEncoder().encode(draft.content).length > defaults.content_limit) { error('cron.too_large'); return; }
    error(); setBusy(true);
    const generation = version;
    try {
      if (!await runPreview()) return;
      const body = { title: draft.title, schedule: draft.schedule, timezone: draft.timezone, delivery_mode: draft.delivery_mode, autocompact: draft.acMode === 'inherit' ? null : draft.acMode === 'tokens' ? tokens : draft.acMode };
      if (editContent) {
        body.content = draft.content;
        if (!creating) body.base_sha256 = selected.sha256;
      }
      if (creating) body.id = draft.id;
      const state = await api(creating ? path() : jobPath(selected.id), { method: creating ? 'POST' : 'PUT', body });
      if (alive(generation)) await refresh(state.job.id);
    } catch (failure) { if (alive(generation)) error(failure.status === 409 ? 'cron.conflict' : failure.status === 413 ? 'cron.too_large' : failure.status === 400 ? 'cron.invalid' : 'error.generic'); }
    finally { if (alive(generation)) setBusy(false); }
  }
  async function toggle(id) {
    const generation = version, job = jobs.find(entry => entry.id === id);
    setBusy(true); error();
    try {
      const state = await api(`${jobPath(id)}/toggle`, { method: 'POST', body: { enabled: !job.enabled } });
      if (!alive(generation)) return;
      jobs = jobs.map(entry => entry.id === id ? state.job : entry);
      if (selected?.id === id) selected = { ...selected, ...state.job, secret_warning: state.secret_warning };
      render();
    } catch { if (alive(generation)) error('error.generic'); }
    finally { if (alive(generation)) setBusy(false); }
  }
  async function remove() {
    const generation = version; setBusy(true); error();
    try { await api(jobPath(selected.id), { method: 'DELETE' }); if (alive(generation)) await refresh(); }
    catch { if (alive(generation)) error('error.generic'); }
    finally { if (alive(generation)) setBusy(false); }
  }
  async function show(current = getTopic(), source = document.querySelector('#headerCron')) {
    if (!authenticated || !current || current.read_only || current.tid === 'unknown') return;
    clearTimeout(previewTimer); ++previewVersion;
    ++version; ++detailVersion; topic = { ...current }; selected = null; draft = null; view = 'list'; jobs = []; opener = source;
    error(); render(); if (!dialog.open) dialog.showModal(); setBusy(true);
    const generation = version;
    try { await refresh(); } catch { if (alive(generation)) error('error.generic'); }
    finally { if (alive(generation)) setBusy(false); }
  }
  content.addEventListener('submit', event => { event.preventDefault(); void save(); });
  content.addEventListener('click', event => {
    const control = event.target.closest('button'); if (!control || busy) return;
    if (control.dataset.cronId) { void select(control.dataset.cronId); return; }
    if (control.dataset.cronToggle) { void toggle(control.dataset.cronToggle); return; }
    const action = control.dataset.cronAction;
    if (action === 'create') edit(true);
    if (action === 'edit') edit();
    if (action === 'list' || action === 'cancel') { view = action === 'cancel' && !creating ? 'detail' : 'list'; render(); error(); }
    if (action === 'delete') { view = 'delete'; render(); }
    if (action === 'cancel-delete') { view = 'detail'; render(); }
    if (action === 'confirm-delete') void remove();
    if (control.dataset.cronMode) { readDraft(); draft.mode = control.dataset.cronMode; if (draft.mode === 'preset') { draft.preset = cronPreset(draft.schedule) || draft.preset; draft.schedule = presetCron(draft.preset) || ''; } render(); queuePreview(); }
    if (control.dataset.cronAc) { readDraft(); draft.acMode = control.dataset.cronAc; render(); }
  });
  function input(event) {
    if (view !== 'edit' || busy) return;
    readDraft();
    if (event.target.id === 'cronTokens') event.target.setCustomValidity('');
    if (event.target.id === 'cronPreset') { render(); queuePreview(); }
    else if (['cronExpression', 'cronTime', 'cronInterval', 'cronTimezone'].includes(event.target.id) || event.target.dataset.cronDay !== undefined) {
      document.querySelector('#cronRaw').textContent = draft.schedule; queuePreview();
    }
    if (event.target.id === 'cronInstruction') { warning = null; document.querySelector('#cronWarning').innerHTML = secret(null); }
  }
  content.addEventListener('input', input);
  content.addEventListener('change', event => { if (event.target.id === 'cronPreset' || event.target.dataset.cronDay !== undefined) input(event); });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    ++version; ++detailVersion; ++previewVersion; clearTimeout(previewTimer); busy = false;
    if (authenticated) opener?.focus();
  });
  document.querySelector('#headerCron').addEventListener('click', event => { void show(getTopic(), event.currentTarget); });
  document.addEventListener('language-changed', () => { if (dialog.open) { readDraft(); render(); } });
  document.addEventListener('topic-selected', () => { if (dialog.open) dialog.close(); });
  document.addEventListener('route-change', event => { if (event.detail.view !== 'chat' && dialog.open) dialog.close(); });
  document.addEventListener('auth-change', event => { authenticated = event.detail.authenticated; if (!authenticated) { ++version; if (dialog.open) dialog.close(); } });
  return { show };
}
