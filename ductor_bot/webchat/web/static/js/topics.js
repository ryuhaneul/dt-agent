import { api } from './api.js';
import { t } from './i18n.js';
import { go } from './router.js';
import { notify } from './auth.js';
import { esc } from './md.js';

let topics = [], selected = null, hits = [], query = '', searchTimer, revision = 0;
let active = false, request = 0;
const unread = new Map(), seen = new Map();
export function getTopics() { return topics; }
export function getTopic() { return topics.find(topic => topic.tid === selected) || null; }
export function formatTime(value, date = false) {
  if (!value || Number.isNaN(Date.parse(value))) return '—';
  return new Date(value).toLocaleString(document.documentElement.lang,
    date ? { dateStyle: 'medium', timeStyle: 'short' } : { hour: '2-digit', minute: '2-digit' });
}
function storedSeen(tid) {
  if (seen.has(tid)) return seen.get(tid);
  let value = null;
  try {
    const raw = localStorage.getItem(`dtw-seen:${tid}`);
    if (raw !== null && Number.isFinite(Number(raw))) value = Number(raw);
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
  try { localStorage.setItem(`dtw-seen:${tid}`, String(value)); } catch { /* Optional storage. */ }
  renderTopics();
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
export function initTopics() {
  document.addEventListener('auth-change', event => {
    active = event.detail.authenticated;
    if (!active) {
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
