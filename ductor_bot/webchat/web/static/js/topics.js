import { api } from './api.js';
import { t } from './i18n.js';
import { go, renderRoute } from './router.js';
import { notify } from './auth.js';
import { esc } from './md.js';

let topics = [], selected = null, hits = [], query = '', searchTimer, revision = 0;
let active = false, request = 0, readEpoch = 0, markingAll = false, migrationEpoch = null;
let renaming = null, renameBusy = false, renameEpoch = 0;
const unread = new Map(), seen = new Map(), confirmed = new Map(), latest = new Map(), migrated = new Set();
const unreadReaders = new WeakMap(), busyEvents = new Map();
let pendingSnapshot = null;
export function getTopics() { return topics; }
export function getTopic() { return topics.find(topic => topic.tid === selected) || null; }
export function noteTopicBusy(tid, value) {
  const event = { value };
  busyEvents.set(tid, event);
  const topic = topics.find(item => item.tid === tid);
  if (!topic) return false;
  const changed = topic.busy !== value || topic.busyFromSnapshot;
  topic.busy = value;
  topic.busyFromSnapshot = false;
  return changed;
}
export function formatTime(value, date = false) {
  if (!value || Number.isNaN(Date.parse(value))) return '—';
  return new Date(value).toLocaleString(document.documentElement.lang,
    date ? { dateStyle: 'medium', timeStyle: 'short' } : { hour: '2-digit', minute: '2-digit' });
}
function storedSeen(tid) { return seen.get(tid) ?? null; }
function mergeSeen(tid, seq) {
  if (!Number.isInteger(seq)) return;
  const saved = confirmed.get(tid) ?? null;
  confirmed.set(tid, saved === null ? seq : Math.max(saved, seq));
  const previous = storedSeen(tid);
  seen.set(tid, previous === null ? seq : Math.max(previous, seq));
}
async function syncSeen(cursors, all = false) {
  const generation = revision;
  await Promise.all(topics.map(async topic => {
    const seq = cursors[topic.tid];
    if (!Number.isInteger(seq)) return;
    mergeSeen(topic.tid, seq);
    if ((all && !latest.has(topic.tid)) || latest.get(topic.tid) <= storedSeen(topic.tid)) {
      unread.set(topic.tid, 0);
    } else await countUnread(topic, generation);
  }));
  if (active && generation === revision) renderTopics();
}
export function markSeen(tid, seq) {
  if (!active || !Number.isInteger(seq)) return;
  const previous = storedSeen(tid), epoch = readEpoch;
  const value = previous === null ? seq : Math.max(previous, seq);
  if (previous === value && unread.get(tid) === 0) return;
  seen.set(tid, value); unread.set(tid, 0); renderTopics();
  if (previous === value) return;
  void api(`/topics/${encodeURIComponent(tid)}/seen`, { method: 'PUT', body: { seq: value } }).then(data => {
    if (active && epoch === readEpoch) mergeSeen(tid, data.seen);
  }).catch(async error => {
    if (!active || epoch !== readEpoch) return;
    if (storedSeen(tid) === value) seen.set(tid, confirmed.get(tid) ?? null);
    if (error.code !== 'auth_required') notify('error.generic');
    const topic = topics.find(item => item.tid === tid);
    if (topic) {
      try { await countUnread(topic, revision); renderTopics(); } catch { /* Retry on the next refresh. */ }
    }
  });
}
async function markAllSeen() {
  if (!active || markingAll) return;
  const epoch = readEpoch, button = document.querySelector('#markAllRead');
  markingAll = true; button.disabled = true;
  try {
    const data = await api('/topics/seen-all', { method: 'POST' });
    if (active && epoch === readEpoch) await syncSeen(data.seen, true);
  } catch (error) {
    if (active && epoch === readEpoch && error.code !== 'auth_required') notify('error.generic');
  } finally {
    if (epoch === readEpoch) { markingAll = false; button.disabled = false; }
  }
}
export function selectTopic(tid, seq = null, navigate = true, focusComposer = false) {
  if (!topics.some(topic => topic.tid === tid)) return;
  selected = tid;
  renderTopics();
  if (navigate) go('chat');
  document.dispatchEvent(new CustomEvent('topic-selected', { detail: { topic: getTopic(), seq, focusComposer, navigate } }));
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
  const { tid } = topic, last = storedSeen(tid);
  if (last === null) { unread.set(tid, topic.count); return; }
  const readers = unreadReaders.get(topic) ?? new Set(), arrived = new Set();
  unreadReaders.set(topic, readers);
  readers.add(arrived);
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const sequences = new Set();
      let before = '';
      while (active && generation === revision) {
        const data = await api(`/topics/${encodeURIComponent(tid)}/messages?limit=100${before}`);
        if (!active || generation !== revision) return;
        if (data.entries.length) latest.set(tid, Math.max(latest.get(tid) ?? -Infinity, data.entries.at(-1).seq));
        for (const entry of data.entries) if (entry.seq > last) sequences.add(entry.seq);
        if (!data.has_more || !data.entries.length || data.entries[0].seq <= last) break;
        before = `&before=${data.entries[0].seq}`;
      }
      if (!active || generation !== revision || storedSeen(tid) !== last) return;
      // Recount once, then merge live entries by sequence so busy topics can finish.
      if (attempt === 0 && arrived.size) continue;
      for (const seq of arrived) if (seq > last) sequences.add(seq);
      unread.set(tid, sequences.size);
      return;
    }
  } finally {
    readers.delete(arrived);
    if (!readers.size) unreadReaders.delete(topic);
  }
}
async function migrateSeen() {
  const epoch = readEpoch;
  if (!active || migrationEpoch === epoch) return;
  migrationEpoch = epoch;
  const pending = topics.filter(topic => !migrated.has(topic.tid)).map(topic => topic.tid);
  try {
    await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
      while (active && epoch === readEpoch && pending.length) {
        const tid = pending.shift();
        migrated.add(tid);
        let legacy = null;
        try {
          const raw = localStorage.getItem(`dtw-seen:${tid}`);
          if (raw !== null && Number.isInteger(Number(raw))) legacy = Number(raw);
        } catch { /* Server cursors work when browser storage is blocked. */ }
        if (legacy === null || (storedSeen(tid) !== null && legacy <= storedSeen(tid))) continue;
        try {
          const result = await api(`/topics/${encodeURIComponent(tid)}/seen`, { method: 'PUT', body: { seq: legacy } });
          if (!active || epoch !== readEpoch) return;
          await syncSeen({ [tid]: result.seen });
          if (!active || epoch !== readEpoch) return;
          try { localStorage.removeItem(`dtw-seen:${tid}`); } catch { /* Optional legacy cleanup. */ }
        } catch (error) {
          if (active && epoch === readEpoch && error.code !== 'auth_required') notify('error.generic');
        }
      }
    }));
  } finally {
    if (migrationEpoch === epoch) migrationEpoch = null;
  }
}
export async function refreshTopics(onSnapshot) {
  if (onSnapshot) pendingSnapshot = onSnapshot;
  // Each busy event has a new identity, even if its value did not change.
  const generation = ++revision, observedBusy = new Map(busyEvents);
  const data = await api('/topics').catch(error => {
    // The newest request owns any pending turn recovery even if its first fetch fails.
    if (!active || generation !== revision || !pendingSnapshot) throw error;
    return api('/topics');
  });
  if (!active || generation !== revision) return;
  const previous = getTopic();
  topics = data.topics;
  for (const topic of topics) {
    const event = busyEvents.get(topic.tid);
    topic.busyFromSnapshot = event === observedBusy.get(topic.tid);
    if (!topic.busyFromSnapshot) topic.busy = event.value;
    mergeSeen(topic.tid, topic.seen);

    if (topic.name_key) topic.name = t(topic.name_key);
    topic.name ||= topic.tid === 'unknown' ? t('topic.unknown_name') : topic.topic_id === null
      ? t('topic.general') : `Topic #${topic.topic_id}`;
    if (topic.group === 'imported') topic.preview = t('topic.imported_preview', { n: topic.count });
  }
  // A newer topic request may supersede the request that started turn recovery.
  const applySnapshot = pendingSnapshot;
  pendingSnapshot = null;
  applySnapshot?.(topics);
  const current = getTopic();
  if (!current) selectTopic(topics[0]?.tid, null, false);
  else {
    document.dispatchEvent(new CustomEvent('topic-updated', { detail: current }));
    if (current.busy !== previous?.busy || (current.busy && current.busyFromSnapshot !== previous?.busyFromSnapshot)) {
      document.dispatchEvent(new CustomEvent('topic-busy-updated'));
    }
  }
  renderTopics();
  document.dispatchEvent(new CustomEvent('topics-updated', { detail: topics }));
  const counts = await Promise.allSettled(topics.map(topic => countUnread(topic, generation)));
  if (active && generation === revision) {
    renderTopics();
    // Apply the successful counts before forwarding an error to the caller.
    const failure = counts.find(result => result.status === 'rejected');
    if (failure) throw failure.reason;
    void migrateSeen();
  }
}
export function noteEntry(tid, entry) {
  const topic = topics.find(item => item.tid === tid);
  if (!topic) return;
  for (const arrived of unreadReaders.get(topic) ?? []) arrived.add(entry.seq);
  topic.last_ts = entry.ts; topic.preview = entry.text; topic.count++;
  latest.set(tid, Math.max(latest.get(tid) ?? -Infinity, entry.seq));
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
  document.addEventListener('read-state', event => {
    if (!active) return;
    void syncSeen(event.detail.seen, event.detail.all).catch(error => {
      if (active && error.code !== 'auth_required') notify('error.generic');
    });
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
      pendingSnapshot = null;
      query = ''; unread.clear(); seen.clear(); confirmed.clear(); latest.clear(); migrated.clear(); busyEvents.clear(); document.querySelector('#q').value = ''; renderTopics();
    }
  });
  document.querySelector('#tlist').addEventListener('click', event => {
    const button = event.target.closest('[data-tid]');
    if (!button) return;
    const topic = topics.find(item => item.tid === button.dataset.tid);
    const seq = button.dataset.seq === undefined ? null : Number(button.dataset.seq);
    const focusComposer = !!(event.isTrusted && seq === null && topic && !topic.read_only && topic.tid !== 'unknown');
    selectTopic(button.dataset.tid, seq, true, focusComposer);
    if (focusComposer) {
      renderRoute();
      document.querySelector('#msgIn').focus();
    }
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
