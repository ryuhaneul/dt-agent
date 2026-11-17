import { api } from './api.js';
import { initCommands } from './commands.js';
import { initSecret } from './secret.js';
import { t } from './i18n.js';
import { notify } from './auth.js';
import { connectEvents } from './ws.js';
import { esc, md } from './md.js';
import { getTopic, getTopics, selectTopic, refreshTopics, noteEntry, markSeen, formatTime } from './topics.js';

const histories = new Map(), turns = new Map(), busy = new Map(), drafts = new Map(), topicVersions = new Map(), busyVersions = new Map();
let active = false, epoch = 0, stopEvents = null, telegram = { state: 'unknown' };
let botUp = false, webSessions = null, pending = 0, outbox = [], sending = false;
let view = 'chat', loading = false, selection = 0, commandMenu = null, secretDialog = null;
function history(tid) {
  if (!histories.has(tid)) histories.set(tid, { entries: new Map(), meta: new Map(), choices: new Map(), hasMore: false, loaded: false });
  return histories.get(tid);
}
export function mergeEntries(entries, incoming) {
  for (const entry of incoming) entries.set(entry.id, entry);
  return [...entries.values()].sort((a, b) => a.seq - b.seq);
}
function sorted(tid) { return mergeEntries(history(tid).entries, []); }
function running(tid) {
  return [...turns.values()].find(turn => turn.tid === tid && ['queued', 'started'].includes(turn.phase));
}
function visibleChat() {
  return view === 'chat' && ['#chat', ''].includes(location.hash) && !document.hidden;
}
function errorNotice(error) {
  if (error.code === 'auth_required') return;
  notify(['bot_unreachable', 'telegram_unavailable', 'web_turn_running', 'topic_busy', 'read_only'].includes(error.code)
    ? `error.${error.code}` : 'error.generic');
}
function mirror(entry) {
  if (!entry.mirror || !(entry.source === 'web' || entry.role === 'event' || entry.source === 'system')) return '';
  const state = entry.mirror.state;
  const key = state === 'sent' ? 'delivered' : state === 'pending' ? 'pending_mirror' : 'mirror_failed';
  return `<div class="${state === 'sent' ? 'deliv' : state === 'pending' ? 'dirty' : 'stopped'}">${esc(t(`chat.${key}`))}</div>`;
}
function sourceTag(entry) {
  if (entry.kind === 'command') return `<span class="tag">${esc(t('commands.run'))}</span>`;
  const source = entry.imported || entry.source === 'import' ? 'imported' : entry.source;
  return ['telegram', 'web', 'api', 'imported'].includes(source)
    ? `<span class="tag">${esc(t(`tag.${source}`))}</span>` : '';
}
function mergeButtonMeta(state, entryId, incoming) {
  const meta = { ...state.meta.get(entryId) };
  if ((incoming.selector_revision ?? 0) >= (meta.selector_revision ?? 0)) {
    for (const field of ['text', 'buttons', 'button_kinds', 'selector_revision']) {
      if (Object.hasOwn(incoming, field)) meta[field] = incoming[field];
    }
  }
  if (typeof incoming.answered === 'string') meta.answered = incoming.answered;
  state.meta.set(entryId, meta);
  return meta;
}
function mergeHistory(state, incoming) {
  return mergeEntries(state.entries, incoming.map(entry => ({
    ...entry, ...mergeButtonMeta(state, entry.id, entry),
  })));
}
function responseButtons(entry) {
  const buttons = entry.buttons;
  if (!Array.isArray(buttons) || !buttons.length) return '';
  const state = history(getTopic().tid), waiting = state.choices.has(entry.id);
  const answered = typeof entry.answered === 'string', locked = answered || waiting || getTopic().read_only;
  const rows = buttons.map((row, rowIndex) => '<div class="response-button-row">' + row.map((button, buttonIndex) => {
    let url = null;
    if (button.url) {
      try { const parsed = new URL(button.url); if (['http:', 'https:'].includes(parsed.protocol)) url = parsed.href; }
      catch { /* Invalid URLs remain non-interactive labels. */ }
    }
    const kind = entry.role !== 'user' && entry.button_kinds?.[rowIndex]?.[buttonIndex];
    const ordinary = kind === 'reply', selector = kind === 'selector';
    const selected = answered && entry.answered === button.text;
    const disabled = locked || !(ordinary || selector);
    const attrs = ordinary ? ` data-response-entry="${esc(entry.id)}" data-response-label="${esc(button.text)}"`
      : selector ? ` data-callback-entry="${esc(entry.id)}" data-callback-data="${esc(button.callback_data)}"` : '';
    return url && !answered && !waiting ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">${esc(button.text)}</a>`
      : `<button class="btn${selected ? ' response-selected' : ''}" type="button"${attrs} aria-pressed="${selected}"${disabled ? ' disabled' : ''} aria-disabled="${!!disabled}">${esc(button.text)}</button>`;
  }).join('') + '</div>').join('');
  const status = answered ? t('buttons.answered', { label: entry.answered }) : waiting ? t('buttons.sending') : '';
  return `<div class="response-buttons" aria-busy="${waiting}">${rows}${status ? `<div class="response-status" role="status">${esc(status)}</div>` : ''}</div>`;
}
async function chooseButton(entryId, value, kind) {
  const topic = getTopic();
  if (!active || !topic || topic.read_only) return;
  const state = history(topic.tid), entry = state.entries.get(entryId);
  if (!entry || entry.role === 'user' || typeof entry.answered === 'string' || state.choices.has(entryId)
      || !entry.buttons?.some((row, rowIndex) => row.some((button, buttonIndex) =>
        entry.button_kinds?.[rowIndex]?.[buttonIndex] === kind
        && (kind === 'selector' ? button.callback_data : button.text) === value))) return;
  const generation = epoch;
  state.choices.set(entryId, value); renderThread();
  try {
    const selector = kind === 'selector';
    const result = await api(`/topics/${encodeURIComponent(topic.tid)}/${selector ? 'callbacks' : 'buttons'}`, {
      method: 'POST', body: selector
        ? { entry_id: entryId, data: value, revision: entry.selector_revision ?? 0 }
        : { entry_id: entryId, label: value },
    });
    if (!active || generation !== epoch) return;
    mergeButtonMeta(state, entryId, kind === 'selector' ? result : { answered: result.answered });
    mergeHistory(state, [state.entries.get(entryId)]);
    if (result.secret && getTopic()?.tid === topic.tid) secretDialog.show(result, topic.tid);
    if (result.turn_id && !turns.has(result.turn_id)) turns.set(result.turn_id, { id: result.turn_id, tid: topic.tid, phase: 'queued', text: '' });
  } catch (error) {
    if (!active || generation !== epoch) return;
    if (['already_answered', 'stale_selector'].includes(error.code)) {
      notify(`buttons.${error.code}`);
      try { await loadHistory(topic.tid, { target: entry.seq }); }
      catch (refreshError) { if (active && generation === epoch) errorNotice(refreshError); }
    } else if (error.status === 400) notify('buttons.invalid');
    else errorNotice(error);
  } finally {
    state.choices.delete(entryId);
    if (active && generation === epoch) renderThread();
  }
}
function entryMarkup(entry) {
  const time = esc(formatTime(entry.ts));
  if (entry.role === 'event') {
    const failed = ['failed', 'error', 'cancelled'].includes(entry.status);
    const key = entry.kind === 'task_result' && failed ? 'task_failed' : entry.kind;
    const kind = ['task_result', 'task_failed', 'task_question', 'heartbeat', 'interagent', 'cron', 'webhook', 'background'].includes(key) ? key : 'background';
    return `<div class="event${entry.imported ? ' old' : ''}" data-entry-seq="${entry.seq}"><span class="badge ${kind === 'task_result' ? 'badge-ok' : kind === 'task_failed' ? 'badge-down' : 'badge-off'}">${esc(t(`event.${kind}`))}</span><div><div class="meta">${sourceTag(entry)}<span class="tm">${time}</span>${entry.title ? `<b>${esc(entry.title)}</b>` : ''}</div><div class="md">${md(entry.text || '')}</div>${responseButtons(entry)}${mirror(entry)}</div></div>`;
  }
  const turn = turns.get(entry.turn_id);
  const stopped = entry.status === 'aborted' || (entry.role === 'bot' && turn?.phase === 'aborted');
  return `<div class="msg ${entry.role === 'user' ? 'me' : 'bot'}${entry.imported ? ' old' : ''}${entry.kind === 'command' ? ' command-message' : ''}" data-entry-seq="${entry.seq}"><div class="meta">${sourceTag(entry)}<span class="tm">${time}</span></div><div class="bubble md">${entry.kind === 'command' ? esc(entry.text || '') : md(entry.text || '')}${responseButtons(entry)}</div>${stopped ? `<div class="stopped">${esc(t('chat.stopped'))}</div>` : ''}${mirror(entry)}</div>`;
}
function streamMarkup(turn) {
  const live = ['queued', 'started'].includes(turn.phase);
  const dots = live ? '<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span>' : '';
  return `<div class="msg bot" ${live ? 'id="streamMsg"' : ''} data-turn="${esc(turn.id)}"><div class="meta"><span class="tag">${esc(t('tag.web'))}</span><span>${esc(live ? t('chat.answering') : formatTime(turn.finished))}</span>${dots}${live && turn.tool ? `<span>${esc(turn.tool)}</span>` : ''}</div><div class="bubble md">${md(turn.text)}${live ? '<span class="caret" aria-hidden="true"></span>' : ''}</div>${turn.phase === 'aborted' ? `<div class="stopped">${esc(t('chat.stopped'))}</div>` : turn.phase === 'error' ? `<div class="stopped">${esc(t('error.generic'))}</div>` : ''}</div>`;
}
function isControlCommand(text) {
  const name = text.trim().split(/\s/, 1)[0].split('@', 1)[0].toLowerCase();
  return ['/stop', '/interrupt', '/stop_all'].includes(name);
}
function renderHeader() {
  const topic = getTopic();
  const root = document.querySelector('[data-view="chat"]');
  root.querySelector('h2').textContent = topic?.name || '—';
  root.querySelector('.sub').textContent = topic ? t('topic.meta', {
    id: topic.topic_id ?? topic.chat_id ?? '—', time: formatTime(topic.last_ts, true),
  }) : '—';
  const values = document.querySelectorAll('#topicPanel dd');
  [topic?.count, topic?.imported_count, formatTime(topic?.created_at, true)].forEach((value, index) => {
    values[index].textContent = value ?? '—';
  });
  document.querySelector('#topicRename').hidden = !topic?.renamable;
  document.querySelector('#topicDelete').hidden = !topic?.deletable;
  document.querySelector('.composer').hidden = !topic || topic.read_only || topic.tid === 'unknown';
  const turn = topic && running(topic.tid) && !document.querySelector('#msgIn').value.trimStart().startsWith('/');
  const button = document.querySelector('#stopBtn');
  button.className = `btn ${turn ? 'btn-danger' : 'btn-primary'}`;
  button.querySelector('span').textContent = t(turn ? 'chat.stop' : 'chat.send');
  button.querySelector('span').dataset.i18n = turn ? 'chat.stop' : 'chat.send';
  button.querySelector('path').setAttribute('d', turn ? 'M7 7h10v10H7z' : 'M5 12h14M13 6l6 6-6 6');
  button.disabled = !topic || (sending && !turn && !isControlCommand(document.querySelector('#msgIn').value));
}
export function renderThread({ bottom = false, seq = null } = {}) {
  const topic = getTopic(); renderHeader();
  const thread = document.querySelector('#thread');
  const previousTop = thread.scrollTop;
  const nearBottom = thread.scrollHeight - thread.clientHeight - previousTop < 80;
  if (!topic) { thread.replaceChildren(); return; }
  const entries = sorted(topic.tid);
  let html = '', previousDay = '';
  for (const entry of entries) {
    const date = entry.ts && !Number.isNaN(Date.parse(entry.ts)) ? new Date(entry.ts) : null;
    const dayKey = `${date?.toLocaleDateString() || '—'}:${!!entry.imported}`;
    if (dayKey !== previousDay) {
      const today = date && date.toLocaleDateString() === new Date().toLocaleDateString();
      const label = today ? t('chat.today') : date ? date.toLocaleDateString(document.documentElement.lang) : '—';
      html += `<div class="day">${esc(label)}${entry.imported ? ` · ${esc(t('chat.imported_day'))}` : ''}</div>`;
      previousDay = dayKey;
    }
    html += entryMarkup(entry);
  }
  for (const turn of turns.values()) {
    if (turn.tid === topic.tid && !entries.some(entry => entry.role === 'bot' && entry.turn_id === turn.id)) html += streamMarkup(turn);
  }
  if ((busy.get(topic.tid) ?? topic.busy) && !running(topic.tid)) html += `<div class="meta">${esc(t('chat.answering'))}<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span></div>`;
  thread.innerHTML = html;
  if (seq !== null) {
    thread.querySelector(`[data-entry-seq="${Number(seq)}"]`)?.scrollIntoView({ block: 'center' });
  } else if (bottom || nearBottom) thread.scrollTop = thread.scrollHeight;
  else thread.scrollTop = previousTop;
  if (visibleChat() && (bottom || nearBottom) && entries.length) markSeen(topic.tid, entries.at(-1).seq);
}
async function loadHistory(tid, { older = false, target = null } = {}) {
  const generation = epoch, state = history(tid), entries = sorted(tid);
  let before = older && entries.length ? `&before=${entries[0].seq}` : '';
  const lower = target ?? (!older && entries.length ? entries[0].seq : null);
  let more;
  do {
    const data = await api(`/topics/${encodeURIComponent(tid)}/messages?limit=100${before}`);
    if (!active || generation !== epoch) return;
    mergeHistory(state, data.entries);
    for (const entry of data.entries) {
      const turn = entry.role === 'bot' && turns.get(entry.turn_id);
      if (turn) {
        turn.phase = entry.status === 'aborted' ? 'aborted' : entry.status === 'error' ? 'error' : 'done';
        turn.finished = entry.ts;
      }
    }
    more = data.has_more;
    state.hasMore = more; state.loaded = true;
    if (!more || !data.entries.length || lower === null || data.entries[0].seq <= lower) break;
    before = `&before=${data.entries[0].seq}`;
  } while (active);
}
function badge(selector, key, className, values = {}) {
  const element = document.querySelector(selector);
  element.className = `badge ${className}`;
  element.querySelector('span').textContent = t(key, values);
  element.querySelector('span').removeAttribute('data-i18n');
}
function renderStatus() {
  const key = telegram.state === 'up' ? 'status.telegram_up' : telegram.state === 'down'
    ? telegram.down_since ? 'status.telegram_down_for' : 'status.telegram_down' : 'status.telegram_unknown';
  const minutes = telegram.down_since ? Math.max(0, Math.floor((Date.now() - Date.parse(telegram.down_since)) / 60000)) : 0;
  const className = telegram.state === 'up' ? 'badge-ok' : telegram.state === 'down' ? 'badge-down' : 'badge-off';
  badge('#telegramBadge', key, className, { minutes });
  badge('#telegramDetail', key, className, { minutes });
  badge('#botBadge', botUp ? 'status.bot_up' : 'status.bot_down', botUp ? 'badge-ok' : 'badge-down');
  badge('#botDetail', botUp ? 'status.bot_up' : 'status.bot_down', botUp ? 'badge-ok' : 'badge-down');
  document.querySelector('#pendingBadge').hidden = pending === 0;
  badge('#pendingBadge', 'status.pending', 'badge-warn', { n: pending });
  document.querySelector('#downSince').textContent = formatTime(telegram.down_since, true);
  document.querySelector('#lastRetry').textContent = formatTime(telegram.last_check, true);
  document.querySelector('#retryInterval').textContent = t('status.interval', { s: telegram.interval_s || 30 });
  document.querySelector('#webSessions').textContent = webSessions ?? '—';
  const strip = telegram.strip || Array(30).fill('none');
  const bars = document.querySelector('#hb');
  bars.innerHTML = strip.map(state => `<i data-s="${['ok', 'down', 'none'].includes(state) ? state : state === 'mixed' ? 'pending' : 'none'}"></i>`).join('');
  bars.setAttribute('aria-label', t('status.strip_aria', { down: strip.filter(state => state === 'down' || state === 'mixed').length }));
  bars.title = formatTime(telegram.strip_end, true);
  document.querySelector('#stripEnd').textContent = formatTime(telegram.strip_end);
  document.querySelector('.composer .line').hidden = telegram.state !== 'down';
  document.querySelector('#outboxCount').textContent = pending;
  document.querySelector('#outboxRows').innerHTML = outbox.filter(item => item.state === 'pending').map(item => {
    const key = item.kind === 'user_mirror' ? 'web' : item.kind === 'reply_mirror' ? 'reply' : 'result';
    return `<div class="qrow"><span class="tag">${esc(t(`outbox.tag_${key}`))}</span><span class="q">${esc(item.kind === 'bus' ? item.title || item.preview : item.preview)} · ${esc(t('chat.pending_mirror'))}</span><span class="t">${esc(formatTime(item.created))}</span></div>`;
  }).join('');
}
async function refreshState() {
  const generation = epoch;
  try {
    const state = await api('/state');
    if (!active || epoch !== generation) return;
    telegram = state.telegram; pending = state.outbox.pending;
    botUp = state.bot.up; webSessions = state.web_sessions; renderStatus();
  } catch (error) { if (active && generation === epoch && error.code !== 'auth_required') { botUp = false; renderStatus(); } }
}
async function refreshOutbox() {
  const generation = epoch;
  const data = await api('/outbox');
  if (!active || epoch !== generation) return;
  outbox = data.items; renderStatus();
}
async function resync() {
  const generation = epoch, versions = new Map(topicVersions), busySnapshot = new Map(busyVersions);
  try {
    await Promise.all([refreshTopics(), refreshState(), refreshOutbox()]);
    if (!active || generation !== epoch) return;
    for (const item of getTopics()) {
      if (busySnapshot.get(item.tid) === busyVersions.get(item.tid)) busy.set(item.tid, item.busy);
      if (versions.get(item.tid) === topicVersions.get(item.tid)) {
        const turn = running(item.tid);
        if (turn && turn.id !== item.turn_id) { turn.phase = 'done'; turn.finished = null; }
        if (item.turn_id && !turns.has(item.turn_id)) {
          turns.set(item.turn_id, { id: item.turn_id, tid: item.tid, phase: 'started', text: '' });
        }
      }
    }
    const topic = getTopic();
    if (topic) { await loadHistory(topic.tid); if (active && generation === epoch) renderThread(); }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
}
export async function handleEvent(event) {
  if (!active) return;
  const topic = getTopic(), tid = event.tid;
  if (tid && ['turn', 'delta'].includes(event.type)) {
    topicVersions.set(tid, (topicVersions.get(tid) || 0) + 1);
  }
  switch (event.type) {
    case 'entry': {
      const state = history(tid), entry = event.entry;
      if (!state.entries.has(entry.id)) noteEntry(tid, entry);
      mergeHistory(state, [entry]);
      if (!getTopics().some(item => item.tid === tid)) await refreshTopics();
      break;
    }
    case 'entry_meta': {
      const state = history(tid);
      mergeButtonMeta(state, event.entry_id, event.meta || {});
      const entry = state.entries.get(event.entry_id);
      if (entry) mergeHistory(state, [entry]);
      break;
    }
    case 'busy':
      busyVersions.set(tid, (busyVersions.get(tid) || 0) + 1);
      busy.set(tid, event.busy); break;
    case 'turn': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, text: '' };
      turn.phase = event.phase; turns.set(turn.id, turn);
      if (!['queued', 'started'].includes(event.phase)) {
        turn.finished = new Date().toISOString();
        if (topic?.tid === tid) await loadHistory(tid);
        await refreshTopics();
      }
      break;
    }
    case 'delta': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, phase: 'started', text: '' };
      turn.text += event.text; turns.set(turn.id, turn); break;
    }
    case 'tool': {
      const turn = turns.get(event.turn_id);
      if (turn) turn.tool = event.name;
      break;
    }
    case 'telegram': telegram = event; renderStatus(); break;
    case 'bridge':
      botUp = event.up; renderStatus();
      if (event.up) await resync();
      break;
    case 'topics': await refreshTopics(); break;
    case 'outbox':
      pending = event.pending;
      if (event.item) {
        const index = outbox.findIndex(item => item.id === event.item.id);
        if (index < 0) outbox.push(event.item);
        else outbox[index] = event.item;
        outbox.sort((a, b) => (a.created || '').localeCompare(b.created || ''));
      }
      renderStatus();
      // Entries join all queue parts by history_id on the server, never by turn_id here.
      if (topic) await loadHistory(topic.tid);
      break;
    default: return;
  }
  if (active) renderThread();
}
async function sendOrStop() {
  commandMenu?.close();
  const topic = getTopic(), input = document.querySelector('#msgIn');
  if (!topic || topic.read_only) return;
  const text = input.value, turn = running(topic.tid) && !text.trimStart().startsWith('/');
  const control = turn || isControlCommand(text);
  if (sending && !control) return;
  if (!turn && (!text.trim() || text.length > 32000)) return;
  if (!control) sending = true;
  renderHeader();
  const generation = epoch;
  try {
    if (turn) await api(`/topics/${encodeURIComponent(topic.tid)}/abort`, { method: 'POST' });
    else {
      const result = await api(`/topics/${encodeURIComponent(topic.tid)}/messages`, {
        method: 'POST', body: { text, client_id: crypto.randomUUID() },
      });
      if (!active || generation !== epoch) return;
      if (result.turn_id && !turns.has(result.turn_id)) turns.set(result.turn_id, { id: result.turn_id, tid: topic.tid, phase: 'queued', text: '' });
      if (result.secret && getTopic()?.tid === topic.tid) secretDialog.show(result, topic.tid);
      drafts.delete(topic.tid);
      if (getTopic()?.tid === topic.tid && input.value === text) input.value = '';
    }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
  finally { if (!control) sending = false; if (active) { renderHeader(); renderThread({ bottom: true }); } }
}
export function initChat() {
  secretDialog = initSecret();
  const thread = document.querySelector('#thread'), input = document.querySelector('#msgIn');
  input.maxLength = 32000;
  commandMenu = initCommands(input, value => { if (getTopic()) drafts.set(getTopic().tid, value); });
  input.addEventListener('input', () => { if (getTopic()) drafts.set(getTopic().tid, input.value); renderHeader(); });
  input.addEventListener('keydown', event => {
    if (commandMenu.keydown(event)) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && matchMedia('(pointer: fine)').matches) {
      event.preventDefault(); if (!running(getTopic()?.tid) || input.value.trimStart().startsWith('/')) void sendOrStop();
    }
  });
  document.querySelector('#stopBtn').addEventListener('click', sendOrStop);
  thread.addEventListener('click', event => {
    const button = event.target.closest('[data-response-entry]');
    if (button && !button.disabled) void chooseButton(button.dataset.responseEntry, button.dataset.responseLabel, 'reply');
    const selector = event.target.closest('[data-callback-entry]');
    if (selector && !selector.disabled) void chooseButton(selector.dataset.callbackEntry, selector.dataset.callbackData, 'selector');
  });
  thread.addEventListener('scroll', async () => {
    const topic = getTopic();
    if (!topic || loading) return;
    if (thread.scrollHeight - thread.clientHeight - thread.scrollTop < 80) {
      const entries = sorted(topic.tid); if (entries.length && visibleChat()) markSeen(topic.tid, entries.at(-1).seq);
    }
    if (thread.scrollTop > 0 || !history(topic.tid).hasMore) return;
    const height = thread.scrollHeight; loading = true;
    try {
      await loadHistory(topic.tid, { older: true });
      if (getTopic()?.tid === topic.tid) { renderThread(); thread.scrollTop = thread.scrollHeight - height; }
    } catch (error) { errorNotice(error); } finally { loading = false; }
  });
  document.addEventListener('topic-selected', async event => {
    const { topic, seq } = event.detail, generation = epoch, selected = ++selection;
    input.value = drafts.get(topic.tid) || ''; renderThread();
    try {
      await loadHistory(topic.tid, { target: seq });
      if (active && generation === epoch && selected === selection && getTopic()?.tid === topic.tid) {
        renderThread({ bottom: seq === null, seq });
        if (location.hash === '#chat') document.querySelector('[data-view="chat"] h2').focus();
      }
    } catch (error) { if (active && generation === epoch) errorNotice(error); }
  });
  document.addEventListener('topic-updated', () => renderHeader());
  document.addEventListener('route-change', event => {
    view = event.detail.view;
    if (active && event.detail.route === 'chat') renderThread();
  });
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) void resync(); });
  document.addEventListener('auth-change', event => {
    active = event.detail.authenticated; ++epoch;
    stopEvents?.(); stopEvents = null;
    histories.clear(); turns.clear(); busy.clear(); drafts.clear(); topicVersions.clear(); busyVersions.clear(); input.value = '';
    telegram = { state: 'unknown' }; botUp = false; webSessions = null;
    pending = 0; outbox = []; renderStatus(); renderHeader();
    if (!active) { thread.replaceChildren(); return; }
    stopEvents = connectEvents(event => { void handleEvent(event).catch(errorNotice); }, up => {
      if (!up) { botUp = false; renderStatus(); }
      if (up) void resync();
    });
    void resync();
  });
}
