import { api } from './api.js';
import { t } from './i18n.js';
import { go, renderRoute } from './router.js';
import { notify } from './auth.js';
import { esc } from './md.js';

let topics = [], selected = null, hits = [], query = '', searchTimer, revision = 0;
let active = false, request = 0, readEpoch = 0, markingAll = false, migrationEpoch = null;
let renaming = null, renameBusy = false, renameEpoch = 0;
const serverUnread = new Map(), pendingRead = new Map(), awaitingUnread = new Set();
const seen = new Map(), confirmed = new Map(), migrated = new Set();
const BADGE_DELAY = 150, BADGE_MAX_WAIT = 1000, BADGE_RETRIES = [1000, 2000, 4000, 8000];
let badgeEpoch = 0, badgeTimer, badgeDue = null, badgeDeadline = null, badgeRetries = 0;
let topicsInFlight = 0, readWrites = 0, badgeNotified = false, badgeWaiters = [];
export function getTopics() { return topics; }
export function getTopic() { return topics.find(topic => topic.tid === selected) || null; }
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
function syncSeen(cursors) {
  for (const topic of topics) mergeSeen(topic.tid, cursors[topic.tid]);
}
function armBadgeRefresh() {
  clearTimeout(badgeTimer);
  if (!active || readWrites || topicsInFlight || badgeDue === null) return;
  badgeTimer = setTimeout(async () => {
    badgeDue = badgeDeadline = null;
    const waiters = badgeWaiters;
    badgeWaiters = [];
    let refreshed = false;
    try { await refreshTopics({ badgeRequest: true }); refreshed = true; }
    catch { /* refreshTopics handles badge failures and retries. */ }
    for (const resolve of waiters) resolve(refreshed);
  }, Math.max(0, badgeDue - performance.now()));
}
export function scheduleBadgeRefresh() {
  if (!active) return;
  ++badgeEpoch;
  badgeRetries = 0;
  const now = performance.now();
  badgeDeadline ??= now + BADGE_MAX_WAIT;
  badgeDue = Math.min(now + BADGE_DELAY, badgeDeadline);
  armBadgeRefresh();
}
export function resyncTopics() {
  if (!active) return Promise.resolve(false);
  return new Promise(resolve => {
    badgeWaiters.push(resolve);
    scheduleBadgeRefresh();
  });
}
function badgeRefreshFailed(error, retry = true) {
  if (!badgeNotified && error?.code !== 'auth_required') {
    notify(error?.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    badgeNotified = true;
  }
  if (!retry) return;
  if (badgeRetries < BADGE_RETRIES.length) {
    badgeDeadline = null;
    badgeDue = performance.now() + BADGE_RETRIES[badgeRetries++];
  } else {
    for (const [tid, write] of pendingRead) {
      if (!write.done) continue;
      pendingRead.delete(tid);
      awaitingUnread.add(tid);
    }
    renderTopics();
  }
}
function beginRead(tids = []) {
  const write = { tids, done: false };
  if (!readWrites++) badgeDeadline = null;
  for (const tid of tids) pendingRead.set(tid, write);
  armBadgeRefresh();
  if (tids.length) renderTopics();
  return write;
}
function finishRead(write, succeeded) {
  --readWrites;
  write.done = true;
  if (!succeeded) {
    for (const tid of write.tids) if (pendingRead.get(tid) === write) pendingRead.delete(tid);
    if (write.tids.length) renderTopics();
  }
  if (!readWrites) badgeDeadline = null;
  scheduleBadgeRefresh();
}
function applyBadges() {
  const listed = new Set();
  for (const topic of topics) {
    const { tid, unread } = topic;
    listed.add(tid);
    const pending = pendingRead.get(tid);
    if (pending && !pending.done) continue;
    pendingRead.delete(tid);
    if (Number.isInteger(unread)) {
      serverUnread.set(tid, unread);
      awaitingUnread.delete(tid);
    } else if (pending) {
      awaitingUnread.add(tid);
    }
  }
  for (const [tid, write] of pendingRead) if (write.done && !listed.has(tid)) pendingRead.delete(tid);
  for (const tid of awaitingUnread) if (!listed.has(tid)) awaitingUnread.delete(tid);
  if (awaitingUnread.size) badgeRefreshFailed();
  else {
    badgeRetries = 0;
    badgeNotified = false;
  }
}
export function markSeen(tid, seq) {
  if (!active || !Number.isInteger(seq)) return;
  const previous = storedSeen(tid), epoch = readEpoch;
  const value = previous === null ? seq : Math.max(previous, seq);
  if (previous === value) return;
  seen.set(tid, value);
  const write = beginRead([tid]);
  void api(`/topics/${encodeURIComponent(tid)}/seen`, { method: 'PUT', body: { seq: value } }).then(data => {
    if (!active || epoch !== readEpoch) return;
    mergeSeen(tid, data.seen);
    finishRead(write, true);
  }, error => {
    if (!active || epoch !== readEpoch) return;
    if (storedSeen(tid) === value) seen.set(tid, confirmed.get(tid) ?? null);
    if (error.code !== 'auth_required') notify('error.generic');
    finishRead(write, false);
  });
}
async function markAllSeen() {
  if (!active || markingAll) return;
  const epoch = readEpoch, button = document.querySelector('#markAllRead');
  markingAll = true; button.disabled = true;
  const write = beginRead(topics.map(topic => topic.tid));
  let succeeded = false;
  try {
    const data = await api('/topics/seen-all', { method: 'POST' });
    if (active && epoch === readEpoch) { syncSeen(data.seen); succeeded = true; }
  } catch (error) {
    if (active && epoch === readEpoch && error.code !== 'auth_required') notify('error.generic');
  } finally {
    if (epoch === readEpoch) {
      markingAll = false; button.disabled = false;
      finishRead(write, succeeded);
    }
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
      const count = pendingRead.has(topic.tid) ? 0 : serverUnread.get(topic.tid) || 0;
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
        const write = beginRead();
        let succeeded = false;
        try {
          const result = await api(`/topics/${encodeURIComponent(tid)}/seen`, { method: 'PUT', body: { seq: legacy } });
          if (!active || epoch !== readEpoch) return;
          mergeSeen(tid, result.seen);
          succeeded = true;
          try { localStorage.removeItem(`dtw-seen:${tid}`); } catch { /* Optional legacy cleanup. */ }
        } catch (error) {
          if (active && epoch === readEpoch && error.code !== 'auth_required') notify('error.generic');
        } finally {
          if (active && epoch === readEpoch) finishRead(write, succeeded);
        }
      }
    }));
  } finally {
    if (migrationEpoch === epoch) migrationEpoch = null;
  }
}
export async function refreshTopics({ badgeRequest = false } = {}) {
  const generation = ++revision, authEpoch = readEpoch, requestedBadgeEpoch = badgeEpoch;
  ++topicsInFlight;
  try {
    const data = await api('/topics');
    if (!active || generation !== revision) return;
    topics = data.topics;
    for (const topic of topics) mergeSeen(topic.tid, topic.seen);
    for (const topic of topics) {
      if (topic.name_key) topic.name = t(topic.name_key);
      topic.name ||= topic.tid === 'unknown' ? t('topic.unknown_name') : topic.topic_id === null
        ? t('topic.general') : `Topic #${topic.topic_id}`;
      if (topic.group === 'imported') topic.preview = t('topic.imported_preview', { n: topic.count });
    }
    if (requestedBadgeEpoch === badgeEpoch) applyBadges();
    if (!getTopic()) selectTopic(topics[0]?.tid, null, false);
    else document.dispatchEvent(new CustomEvent('topic-updated', { detail: getTopic() }));
    renderTopics();
    document.dispatchEvent(new CustomEvent('topics-updated', { detail: topics }));
    void migrateSeen();
  } catch (error) {
    if (badgeRequest && active && generation === revision) {
      badgeRefreshFailed(error, requestedBadgeEpoch === badgeEpoch);
    }
    throw error;
  } finally {
    if (authEpoch === readEpoch) {
      --topicsInFlight;
      armBadgeRefresh();
    }
  }
}
export function noteEntry(tid, entry) {
  const topic = topics.find(item => item.tid === tid);
  if (!topic) return;
  topic.last_ts = entry.ts; topic.preview = entry.text; topic.count++;
  if (entry.imported) topic.imported_count++;
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
    syncSeen(event.detail.seen);
    scheduleBadgeRefresh();
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
    ++revision; ++badgeEpoch;
    clearTimeout(badgeTimer);
    badgeDue = badgeDeadline = null;
    topicsInFlight = readWrites = badgeRetries = 0;
    badgeNotified = false;
    pendingRead.clear(); awaitingUnread.clear();
    for (const resolve of badgeWaiters) resolve(false);
    badgeWaiters = [];
    document.querySelector('#markAllRead').disabled = false;
    if (!active) {
      ++renameEpoch; renaming = null; renameBusy = false; dialog.close(); clearRenameError();
      form.setAttribute('aria-busy', 'false');
      form.querySelectorAll('input, button').forEach(element => { element.disabled = false; });
      document.querySelector('#renameClose').disabled = false;
      ++request; clearTimeout(searchTimer); topics = []; selected = null; hits = [];
      query = ''; serverUnread.clear(); seen.clear(); confirmed.clear(); migrated.clear(); document.querySelector('#q').value = ''; renderTopics();
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
