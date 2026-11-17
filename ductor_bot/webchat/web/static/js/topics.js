import { api } from './api.js';
import { t } from './i18n.js';
import { go } from './router.js';
import { notify } from './auth.js';
import { esc } from './md.js';

let topics = [], selected = null, hits = [], query = '', searchTimer, revision = 0;
let active = false, request = 0, readEpoch = 0, markingAll = false;
let renaming = null, renameBusy = false, renameEpoch = 0;
const unread = new Map(), seen = new Map();
export function getTopics() { return topics; }
export function getTopic() { return topics.find(topic => topic.tid === selected) || null; }
export function formatTime(value, date = false) {
  if (!value || Number.isNaN(Date.parse(value))) return '—';
  return new Date(value).toLocaleString(document.documentElement.lang,
    date ? { dateStyle: 'medium', timeStyle: 'short' } : { hour: '2-digit', minute: '2-digit' });
}
function storedSeen(tid) {
  let value = seen.get(tid) ?? null;
  try {
    const raw = localStorage.getItem(`dtw-seen:${tid}`);
    if (raw !== null && Number.isFinite(Number(raw))) {
      value = value === null ? Number(raw) : Math.max(value, Number(raw));
    }
  } catch { /* In-memory seen state still works when storage is blocked. */ }
  seen.set(tid, value);
  return value;
}
export function markSeen(tid, seq) {
  if (!Number.isFinite(seq)) return;
  const previous = storedSeen(tid);
  const value = previous === null ? seq : Math.max(previous, seq);
  if (previous === value && unread.get(tid) === 0) return;
  seen.set(tid, value); unread.set(tid, 0);
  if (previous === null || value > previous) {
    try { localStorage.setItem(`dtw-seen:${tid}`, String(value)); } catch { /* Optional storage. */ }
  }
  renderTopics();
}
async function markAllSeen() {
  if (!active || markingAll) return;
  const epoch = readEpoch, pending = topics.map(topic => topic.tid);
  const button = document.querySelector('#markAllRead');
  markingAll = true; button.disabled = true;
  let failed = false;
  // Latest sequences are not part of /topics. Bound requests for large lists.
  await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (active && epoch === readEpoch && pending.length) {
      const tid = pending.shift();
      try {
        const data = await api(`/topics/${encodeURIComponent(tid)}/messages?limit=1`);
        if (!active || epoch !== readEpoch) return;
        if (data.entries.length) markSeen(tid, data.entries.at(-1).seq);
      } catch (error) { if (error.code !== 'auth_required') failed = true; }
    }
  }));
  if (epoch === readEpoch) {
    markingAll = false; button.disabled = false;
    if (failed && active) notify('error.generic');
  }
}
export function selectTopic(tid, seq = null, navigate = true) {
  if (!topics.some(topic => topic.tid === tid)) return;
  selected = tid;
  renderTopics();
  if (navigate) go('chat');
  document.dispatchEvent(new CustomEvent('topic-selected', { detail: { topic: getTopic(), seq } }));
}
export function renderTopics() {
  let html = '';
  for (const group of ['telegram', 'imported', 'unknown']) {
    const rows = topics.filter(topic => topic.group === group && topic.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    if (!rows.length) continue;
    html += `<div class="grp"><h3>${esc(t(`topic.group_${group}`))}</h3><span class="count">${rows.length}</span></div>`;
    for (const topic of rows) {
      const count = unread.get(topic.tid) || 0;
      html += `<button class="cell" type="button" data-tid="${esc(topic.tid)}" aria-current="${topic.tid === selected}"><span class="ic">${esc(topic.name.slice(0, 1))}</span><span class="nm">${esc(topic.name)}</span><span class="tm">${esc(formatTime(topic.last_ts))}</span><span class="pv"><span>${esc(topic.preview)}</span>${count ? `<b class="unread" aria-label="${esc(t('topic.unread', { n: count }))}">${count}</b>` : ''}</span></button>`;
    }
  }
  if (query && hits.length) {
    html += `<div class="grp"><h3>${esc(t('topic.group_search'))}</h3><span class="count">${hits.length}</span></div>`;
    for (const hit of hits) {
      const topic = topics.find(item => item.tid === hit.tid);
      html += `<button class="cell" type="button" data-tid="${esc(hit.tid)}" data-seq="${hit.seq}"><span class="ic">#</span><span class="nm">${esc(topic?.name || t('topic.unknown_name'))}</span><span class="tm">${esc(formatTime(hit.ts))}</span><span class="pv"><span>${esc(hit.snippet)}</span></span></button>`;
    }
  }
  // Restore a focused list item across incoming events without moving focus from the composer.
  const focus = document.activeElement;
  const focusedTid = focus?.closest?.('#tlist .cell')?.dataset.tid;
  const focusedSeq = focus?.dataset.seq;
  const list = document.querySelector('#tlist');
  list.innerHTML = html;
  if (focusedTid) [...list.querySelectorAll('.cell')].find(cell =>
    cell.dataset.tid === focusedTid && cell.dataset.seq === focusedSeq)?.focus();
}
async function countUnread(topic, generation) {
  const last = storedSeen(topic.tid);
  if (last === null) { unread.set(topic.tid, topic.count); return; }
  let before = '', count = 0;
  while (active && generation === revision) {
    const data = await api(`/topics/${encodeURIComponent(topic.tid)}/messages?limit=100${before}`);
    if (!active || generation !== revision) return;
    count += data.entries.filter(entry => entry.seq > last).length;
    if (!data.has_more || !data.entries.length || data.entries[0].seq <= last) break;
    before = `&before=${data.entries[0].seq}`;
  }
  if (storedSeen(topic.tid) === last) unread.set(topic.tid, count);
}
export async function refreshTopics() {
  const generation = ++revision;
  const data = await api('/topics');
  if (!active || generation !== revision) return;
  topics = data.topics;
  for (const topic of topics) {
    if (topic.name_key) topic.name = t(topic.name_key);
    topic.name ||= topic.tid === 'unknown' ? t('topic.unknown_name') : topic.topic_id === null
      ? t('topic.general') : `Topic #${topic.topic_id}`;
    if (topic.group === 'imported') topic.preview = t('topic.imported_preview', { n: topic.count });
  }
  if (!getTopic()) selectTopic(topics[0]?.tid, null, false);
  else document.dispatchEvent(new CustomEvent('topic-updated', { detail: getTopic() }));
  renderTopics();
  document.dispatchEvent(new CustomEvent('topics-updated', { detail: topics }));
  await Promise.all(topics.map(topic => countUnread(topic, generation)));
  if (active && generation === revision) renderTopics();
}
export function noteEntry(tid, entry) {
  const topic = topics.find(item => item.tid === tid);
  if (!topic) return;
  topic.last_ts = entry.ts; topic.preview = entry.text; topic.count++;
  if (entry.imported) topic.imported_count++;
  if (storedSeen(tid) === null || entry.seq > storedSeen(tid)) unread.set(tid, (unread.get(tid) || 0) + 1);
  topics.sort((a, b) => (b.last_ts || '').localeCompare(a.last_ts || ''));
  renderTopics();
}
function clearRenameError() {
  const error = document.querySelector('#renameError'), input = document.querySelector('#renameName');
  error.hidden = true; error.textContent = ''; delete error.dataset.i18n;
  input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby');
}
function renameError(key) {
  const error = document.querySelector('#renameError'), input = document.querySelector('#renameName');
  error.dataset.i18n = key; error.textContent = t(key); error.hidden = false;
  input.setAttribute('aria-invalid', 'true'); input.setAttribute('aria-describedby', 'renameError'); input.focus();
}
async function renameCurrentTopic() {
  const topic = getTopic(), form = document.querySelector('#renameForm');
  const input = document.querySelector('#renameName'), name = input.value.trim(), generation = renameEpoch;
  if (!active || renameBusy) return;
  if (!topic?.renamable || renaming !== topic.tid) {
    renameError('error.generic');
    input.disabled = true; form.querySelector('button[type="submit"]').disabled = true;
    return;
  }
  clearRenameError();
  if (!name) { renameError('rename.empty'); return; }
  if (!form.reportValidity()) return;
  renameBusy = true; form.setAttribute('aria-busy', 'true');
  form.querySelectorAll('input, button').forEach(element => { element.disabled = true; });
  document.querySelector('#renameClose').disabled = true;
  try {
    await api(`/topics/${encodeURIComponent(topic.tid)}`, { method: 'PATCH', body: { name } });
    if (!active || generation !== renameEpoch) return;
    await refreshTopics();
    if (active && generation === renameEpoch) document.querySelector('#renameDlg').close();
  } catch (error) {
    if (active && generation === renameEpoch && error.code !== 'auth_required') renameError(
      ['telegram_unavailable', 'bot_unreachable', 'read_only'].includes(error.code) ? `error.${error.code}` : 'error.generic');
  } finally {
    if (generation === renameEpoch) {
      renameBusy = false; form.setAttribute('aria-busy', 'false');
      form.querySelectorAll('input, button').forEach(element => { element.disabled = false; });
      document.querySelector('#renameClose').disabled = false;
      if (document.querySelector('#renameDlg').open) input.focus();
    }
  }
}
export function initTopics() {
  document.querySelector('#markAllRead').addEventListener('click', () => { void markAllSeen(); });
  window.addEventListener('storage', event => {
    if (event.storageArea !== localStorage || !event.key?.startsWith('dtw-seen:')) return;
    const topic = topics.find(item => item.tid === event.key.slice('dtw-seen:'.length));
    if (!active || !topic) return;
    const generation = revision;
    void countUnread(topic, generation).then(() => {
      if (active && generation === revision) renderTopics();
    }).catch(error => { if (active && error.code !== 'auth_required') notify('error.generic'); });
  });
  const dialog = document.querySelector('#renameDlg'), form = document.querySelector('#renameForm');
  const input = document.querySelector('#renameName'), opener = document.querySelector('#headerRename');
  opener.addEventListener('click', () => {
    const topic = getTopic();
    if (!active || !topic?.renamable || renameBusy) return;
    renaming = topic.tid; input.value = topic.name; clearRenameError();
    input.disabled = false; form.querySelector('button[type="submit"]').disabled = false;
    document.dispatchEvent(new CustomEvent('close-header-menus'));
    dialog.showModal(); input.focus(); input.select();
  });
  for (const id of ['renameClose', 'renameCancel']) document.querySelector(`#${id}`).addEventListener('click', () => {
    if (!renameBusy) dialog.close();
  });
  dialog.addEventListener('cancel', event => { if (renameBusy) event.preventDefault(); });
  dialog.addEventListener('close', () => { renaming = null; clearRenameError(); opener.focus(); });
  input.addEventListener('input', clearRenameError);
  form.addEventListener('submit', event => { event.preventDefault(); void renameCurrentTopic(); });
  document.addEventListener('language-changed', () => {
    for (const topic of topics) {
      if (topic.name_key) topic.name = t(topic.name_key);
      if (topic.group === 'imported') topic.preview = t('topic.imported_preview', { n: topic.count });
    }
    renderTopics();
  });
  document.addEventListener('auth-change', event => {
    active = event.detail.authenticated; ++readEpoch; markingAll = false;
    document.querySelector('#markAllRead').disabled = false;
    if (!active) {
      ++renameEpoch; renaming = null; renameBusy = false; dialog.close(); clearRenameError();
      form.setAttribute('aria-busy', 'false');
      form.querySelectorAll('input, button').forEach(element => { element.disabled = false; });
      document.querySelector('#renameClose').disabled = false;
      ++revision; ++request; clearTimeout(searchTimer); topics = []; selected = null; hits = [];
      query = ''; unread.clear(); seen.clear(); document.querySelector('#q').value = ''; renderTopics();
    }
  });
  document.querySelector('#tlist').addEventListener('click', event => {
    const button = event.target.closest('[data-tid]');
    if (button) selectTopic(button.dataset.tid, button.dataset.seq === undefined ? null : Number(button.dataset.seq));
  });
  document.querySelector('#q').addEventListener('input', event => {
    query = event.target.value.trim(); hits = []; const generation = ++request;
    clearTimeout(searchTimer); renderTopics();
    if (!query) return;
    searchTimer = setTimeout(async () => {
      try {
        const data = await api(`/search?q=${encodeURIComponent(query.slice(0, 200))}`);
        if (active && generation === request) { hits = data.hits; renderTopics(); }
      } catch (error) { if (active && generation === request && error.code !== 'auth_required') notify('error.generic'); }
    }, 300);
  });
  const create = document.querySelector('.tfoot button');
  create.addEventListener('click', async () => {
    create.disabled = true;
    try {
      const result = await api('/topics', { method: 'POST', body: { name: t('topic.new_default') } });
      await refreshTopics(); selectTopic(result.topic.tid, null, false); go('manage');
      document.dispatchEvent(new CustomEvent('topic-created', { detail: result.topic }));
    } catch (error) { if (error.code !== 'auth_required') notify(error.code === 'telegram_unavailable' ? 'error.telegram_unavailable' : 'error.generic'); }
    finally { create.disabled = false; }
  });
}
