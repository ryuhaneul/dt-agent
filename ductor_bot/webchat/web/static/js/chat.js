import { api } from './api.js';
import { initCommands } from './commands.js';
import { initSecret } from './secret.js';
import { t } from './i18n.js';
import { notify } from './auth.js';
import { connectEvents } from './ws.js';
import { md, esc, createMarkdownCache, createMarkdownStream, initMarkdownLinks } from './md.js';
import { getTopic, getTopics, selectTopic, refreshTopics, noteEntry, markSeen, formatTime } from './topics.js';

const histories = new Map(), turns = new Map(), busy = new Map(), drafts = new Map(), topicVersions = new Map(), busyVersions = new Map();
let active = false, epoch = 0, stopEvents = null, telegram = { state: 'unknown' };
let botUp = false, webSessions = null, pending = 0, outbox = [], sending = false;
let view = 'chat', loading = false, selection = 0, commandMenu = null, secretDialog = null;
let streamFrame = null, historyRender = null;
const streamUpdates = new Set();
function history(tid) {
  if (!histories.has(tid)) histories.set(tid, { entries: new Map(), meta: new Map(), choices: new Map(), hasMore: false, loaded: false, rendered: false, renderStart: null, markdown: createMarkdownCache(`entry:${tid}`) });
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
function started(tid) {
  return [...turns.values()].find(turn => turn.tid === tid && turn.phase === 'started');
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
function queueStatus(turnId, status = null) {
  const turn = turns.get(turnId), cancelled = status === 'cancelled' || turn?.phase === 'cancelled';
  if (!cancelled && turn?.phase !== 'queued') return '';
  const label = t(cancelled ? 'chat.cancelled' : 'chat.queued');
  const cancel = !cancelled ? `<button class="btn queue-cancel" type="button" data-cancel-turn="${esc(turnId)}" aria-label="${esc(t('chat.cancel_queued'))}"${turn.cancelling ? ' disabled' : ''}>${esc(t('common.cancel'))}</button>` : '';
  return `<div class="queue-status${cancelled ? ' stopped' : ''}" role="status"><span>${esc(label)}</span>${cancel}</div>`;
}
function queuedMarkup(turn) {
  return `<div class="msg me" data-queue-turn="${esc(turn.id)}"><div class="meta"><span class="tag">${esc(t('tag.web'))}</span><span class="tm">${esc(formatTime(turn.received))}</span></div><div class="bubble md">${md(turn.userText)}</div>${queueStatus(turn.id)}</div>`;
}
async function cancelQueued(turnId) {
  const turn = turns.get(turnId), generation = epoch;
  if (!active || !turn || turn.phase !== 'queued' || turn.cancelling || getTopic()?.read_only) return;
  turn.cancelling = true; renderThread();
  try {
    await api(`/topics/${encodeURIComponent(turn.tid)}/turns/${encodeURIComponent(turnId)}/cancel`, { method: 'POST' });
    if (!active || generation !== epoch) return;
    turn.phase = 'cancelled';
    await loadHistory(turn.tid);
  } catch (error) {
    if (!active || generation !== epoch) return;
    if (error.status === 409 && error.code === 'turn_started') {
      // The start won the server's atomic race; this message can no longer be cancelled.
      if (turn.phase === 'queued') turn.phase = 'started';
      await resync();
    } else errorNotice(error);
  } finally {
    turn.cancelling = false;
    if (active && generation === epoch) renderThread();
  }
}
const systemLabels = {
  thinking: 'stream.label_thinking', compacting: 'stream.label_compacting',
  recovering: 'stream.label_recovering', timeout_warning: 'stream.label_timeout_warning',
  timeout_extended: 'stream.label_timeout_extended',
};
function addActivity(turn, event) {
  if (event.type === 'system_status' && !Object.hasOwn(systemLabels, event.status)) return;
  if (event.type === 'tool' && (typeof event.name !== 'string' || !event.name)) return;
  const items = turn.activities ||= [], last = items.at(-1);
  // Snapshot recovery may contain the last event already received over the live socket.
  if (event.seq !== undefined && items.some(item => item.seq === event.seq)) return;
  const offset = turn.text.length, value = event.type === 'tool' ? event.name : event.status;
  if (last && last.type === event.type && last.value === value && last.offset === offset) {
    last.count++; last.seq = event.seq;
  } else items.push({ type: event.type, value, offset, count: 1, seq: event.seq });
  turn.activityRevision = (turn.activityRevision || 0) + 1;
}
function activityLine(item) {
  const text = item.type === 'tool' ? `TOOL: ${item.value}` : t(systemLabels[item.value]);
  const tag = item.type === 'tool' ? 'b' : 'i', count = item.count > 1 ? ` x${item.count}` : '';
  return `<div class="activity-line"><${tag}>[${esc(text)}]${count}</${tag}></div>`;
}
function activitySummary(turn) {
  if (!turn?.activities?.length) return '';
  const seconds = Math.max(0, ((Date.parse(turn.finished) || Date.now()) - (Date.parse(turn.startedAt) || Date.now())) / 1000).toFixed(1);
  const label = turn.phase === 'aborted' ? t('chat.stopped') : turn.phase === 'error' ? t('error.generic') : t('stream.done', { secs: seconds });
  return `<details class="activity-trace" data-activity-turn="${esc(turn.id)}"${turn.activityOpen ? ' open' : ''}><summary>${esc(label)}<span class="sr-only"> · ${esc(t('chat.activity'))}</span></summary><div class="activity-lines" role="group" aria-label="${esc(t('chat.activity'))}">${turn.activities.map(activityLine).join('')}</div></details>`;
}
function streamContent(turn, live) {
  if (!live) {
    turn.markdown ||= createMarkdownStream(`turn:${turn.id}`);
    return activitySummary(turn) + turn.markdown.render(turn.text, true);
  }
  let html = '', offset = 0;
  for (const item of turn.activities || []) {
    html += md(turn.text.slice(offset, item.offset), { scope: `turn:${turn.id}:activity:${offset}` }) + activityLine(item);
    offset = item.offset;
  }
  if (turn.markdownOffset !== offset) turn.markdown = createMarkdownStream(`turn:${turn.id}`);
  turn.markdownOffset = offset;
  turn.markdown.render(turn.text.slice(offset));
  turn.renderedActivityRevision = turn.activityRevision;
  return html + `<div data-stream-complete data-length="${turn.markdown.completeHTML.length}" data-revision="${turn.markdown.revision}">${turn.markdown.completeHTML}</div><div data-stream-tail>${turn.markdown.tailHTML}</div>`;
}

function entryMarkup(entry) {
  const time = esc(formatTime(entry.ts));
  const delegated = entry.role === 'event' && entry.kind === 'interagent';
  if (entry.role === 'event' && !delegated) {
    const failed = ['failed', 'error', 'cancelled'].includes(entry.status);
    const key = entry.kind === 'task_result' && failed ? 'task_failed' : entry.kind;
    const kind = ['task_result', 'task_failed', 'task_question', 'heartbeat', 'interagent', 'cron', 'webhook', 'background'].includes(key) ? key : 'background';
    return `<div class="event${entry.imported ? ' old' : ''}" data-entry-seq="${entry.seq}"><span class="badge ${kind === 'task_result' ? 'badge-ok' : kind === 'task_failed' ? 'badge-down' : 'badge-off'}">${esc(t(`event.${kind}`))}</span><div><div class="meta">${sourceTag(entry)}<span class="tm">${time}</span>${entry.title ? `<b>${esc(entry.title)}</b>` : ''}</div><div class="md">${history(getTopic().tid).markdown(entry)}</div>${responseButtons(entry)}${mirror(entry)}</div></div>`;
  }
  const turn = turns.get(entry.turn_id);
  const stopped = entry.status === 'aborted' || (entry.role === 'bot' && turn?.phase === 'aborted');
  return `<div class="msg ${entry.role === 'user' ? 'me' : 'bot'}${entry.imported ? ' old' : ''}${entry.kind === 'command' ? ' command-message' : ''}" data-entry-seq="${entry.seq}"><div class="meta">${sourceTag(entry)}${delegated ? `<span class="tag">${esc(t('chat.delegated_response'))}</span>` : ''}<span class="tm">${time}</span>${delegated && entry.title ? `<span>${esc(entry.title)}</span>` : ''}</div><div class="bubble md">${entry.role === 'bot' ? activitySummary(turn) : ''}${entry.kind === 'command' ? esc(entry.text || '') : history(getTopic().tid).markdown(entry)}${responseButtons(entry)}</div>${entry.role === 'user' ? queueStatus(entry.turn_id, entry.status) : ''}${stopped ? `<div class="stopped">${esc(t('chat.stopped'))}</div>` : ''}${mirror(entry)}</div>`;
}
function streamMarkup(turn) {
  const live = ['queued', 'started'].includes(turn.phase);
  const dots = live ? '<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span>' : '';
  return `<div class="msg bot" ${live ? 'id="streamMsg"' : ''} data-turn="${esc(turn.id)}"><div class="meta"><span class="tag">${esc(t('tag.web'))}</span><span>${esc(live ? t('chat.answering') : formatTime(turn.finished))}</span>${dots}</div><div class="bubble md">${streamContent(turn, live)}${live ? '<span class="caret" aria-hidden="true"></span>' : ''}</div>${turn.phase === 'aborted' ? `<div class="stopped">${esc(t('chat.stopped'))}</div>` : turn.phase === 'error' ? `<div class="stopped">${esc(t('error.generic'))}</div>` : ''}</div>`;
}
function cancelStreamRender() {
  if (streamFrame !== null) cancelAnimationFrame(streamFrame);
  streamFrame = null; streamUpdates.clear();
}
function scheduleStreamRender(turnId) {
  streamUpdates.add(turnId);
  if (streamFrame !== null) return;
  streamFrame = requestAnimationFrame(() => {
    streamFrame = null;
    const updates = [...streamUpdates]; streamUpdates.clear();
    const topic = getTopic();
    if (!active || !topic) return;
    const thread = document.querySelector('#thread'), previousTop = thread.scrollTop;
    const nearBottom = thread.scrollHeight - thread.clientHeight - previousTop < 80;
    for (const id of updates) {
      const turn = turns.get(id);
      if (!turn || turn.tid !== topic.tid) continue;
      const element = [...thread.querySelectorAll('[data-turn]')].find(node => node.dataset.turn === id);
      if (element) {
        if (turn.renderedActivityRevision !== turn.activityRevision) {
          renderThread();
          return;
        }
        turn.markdown.render(turn.text.slice(turn.markdownOffset || 0));
        const complete = element.querySelector('[data-stream-complete]');
        const tail = element.querySelector('[data-stream-tail]');
        const length = Number(complete.dataset.length || 0);
        if (Number(complete.dataset.revision) !== turn.markdown.revision || turn.markdown.completeHTML.length < length) complete.innerHTML = turn.markdown.completeHTML;
        else complete.insertAdjacentHTML('beforeend', turn.markdown.completeHTML.slice(length));
        complete.dataset.length = turn.markdown.completeHTML.length;
        complete.dataset.revision = turn.markdown.revision;
        tail.innerHTML = turn.markdown.tailHTML;
      } else if (![...history(topic.tid).entries.values()].some(entry => entry.role === 'bot' && entry.turn_id === id)) {
        renderThread();
        return;
      }
    }
    renderHeader();
    thread.scrollTop = nearBottom ? thread.scrollHeight : previousTop;
    updateThreadNavigation();
  });
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
  document.querySelector('#headerRename').hidden = !topic?.renamable;
  document.querySelector('#topicDelete').hidden = !topic?.deletable;
  document.querySelector('.composer').hidden = !topic || topic.read_only || topic.tid === 'unknown';
  const stop = document.querySelector('#stopBtn');
  stop.hidden = !topic || !started(topic.tid);
  stop.disabled = !topic || !started(topic.tid);
  document.querySelector('#sendBtn').disabled = !topic || (sending && !isControlCommand(document.querySelector('#msgIn').value));
}
function updateThreadNavigation() {
  const thread = document.querySelector('#thread');
  const top = document.querySelector('#threadTop'), bottom = document.querySelector('#threadBottom');
  const hasTopic = !!getTopic();
  top.hidden = !hasTopic || thread.scrollTop < 80;
  bottom.hidden = !hasTopic || thread.scrollHeight - thread.clientHeight - thread.scrollTop < 80;
}
function entryDay(entry) {
  const date = entry.ts && !Number.isNaN(Date.parse(entry.ts)) ? new Date(entry.ts) : null;
  const key = `${date?.toLocaleDateString() || '—'}:${!!entry.imported}`;
  const today = date && date.toLocaleDateString() === new Date().toLocaleDateString();
  const label = today ? t('chat.today') : date ? date.toLocaleDateString(document.documentElement.lang) : '—';
  return { key, html: `<div class="day">${esc(label)}${entry.imported ? ` · ${esc(t('chat.imported_day'))}` : ''}</div>` };
}
function entryRange(entries, start, end) {
  let html = '', previousDay = '';
  for (let index = start; index < end; index++) {
    const entry = entries[index], day = entryDay(entry);
    if (day.key !== previousDay) html += day.html;
    previousDay = day.key;
    html += entryMarkup(entry);
  }
  return html;
}
function cancelHistoryRender() {
  if (!historyRender) return;
  if (historyRender.idle) cancelIdleCallback(historyRender.id);
  else cancelAnimationFrame(historyRender.id);
  historyRender = null;
}
function scheduleHistoryRender(topic, entries, start) {
  const state = history(topic.tid), thread = document.querySelector('#thread');
  const job = { tid: topic.tid, start, idle: typeof requestIdleCallback === 'function', id: null };
  historyRender = job;
  function schedule() {
    job.id = job.idle ? requestIdleCallback(appendOlder) : requestAnimationFrame(appendOlder);
  }
  function appendOlder() {
    if (historyRender !== job || !active || getTopic()?.tid !== job.tid) return;
    const next = Math.max(0, job.start - 12), html = entryRange(entries, next, job.start);
    const top = thread.scrollTop, height = thread.scrollHeight;
    const pinned = height - thread.clientHeight - top < 80;
    if (entryDay(entries[job.start - 1]).key === entryDay(entries[job.start]).key) thread.firstElementChild?.remove();
    thread.insertAdjacentHTML('afterbegin', html);
    // All writes precede the single layout read; preserve the existing viewport.
    const newHeight = thread.scrollHeight;
    thread.scrollTop = pinned ? newHeight : top + newHeight - height;
    updateThreadNavigation();
    job.start = next; state.renderStart = entries[next].seq;
    if (next) schedule();
    else { state.rendered = true; state.renderStart = null; historyRender = null; }
  }
  schedule();
}
export function renderThread({ bottom = false, seq = null } = {}) {
  cancelStreamRender(); cancelHistoryRender();
  const topic = getTopic(); renderHeader();
  const thread = document.querySelector('#thread');
  for (const element of thread.querySelectorAll('details[data-activity-turn]')) {
    const turn = turns.get(element.dataset.activityTurn);
    if (turn) turn.activityOpen = element.open;
  }
  const previousTop = thread.scrollTop;
  const nearBottom = thread.scrollHeight - thread.clientHeight - previousTop < 80;
  if (!topic) { thread.replaceChildren(); updateThreadNavigation(); return; }
  const entries = sorted(topic.tid);
  const topicTurns = [...turns.values()].filter(turn => turn.tid === topic.tid);
  const liveTurns = topicTurns.filter(turn => ['queued', 'started'].includes(turn.phase));
  const received = liveTurns.map(turn => Date.parse(turn.received));
  const boundary = received.length && received.every(Number.isFinite) ? Math.min(...received) : null;
  const waiting = [], deferred = new Set();
  const visibleEntries = entries.filter(entry => {
    const turn = turns.get(entry.turn_id);
    if (entry.role === 'user' && boundary !== null && (
      turn?.phase === 'queued' || (entry.status === 'cancelled' && Date.parse(entry.ts) >= boundary)
    )) {
      waiting.push({ received: turn?.received || entry.ts, markup: entryMarkup(entry) });
      deferred.add(entry.turn_id); return false;
    }
    return true;
  });
  const state = history(topic.tid);
  let start = 0;
  if (!state.rendered && seq === null && visibleEntries.length > 12) {
    const resumed = visibleEntries.findIndex(entry => entry.seq === state.renderStart);
    start = resumed >= 0 ? resumed : visibleEntries.length - 12;
  }
  let html = entryRange(visibleEntries, start, visibleEntries.length);
  for (const turn of topicTurns) {
    const userExists = entries.some(entry => entry.role === 'user' && entry.turn_id === turn.id);
    if (['queued', 'cancelled'].includes(turn.phase)) {
      if (!deferred.has(turn.id) && !userExists && turn.userText !== undefined) {
        waiting.push({ received: turn.received, markup: queuedMarkup(turn) });
      }
      continue;
    }
    if (!userExists && turn.userText !== undefined) html += queuedMarkup(turn);
    if (!entries.some(entry => entry.role === 'bot' && entry.turn_id === turn.id)) html += streamMarkup(turn);
  }
  // Receipt times preserve the position of cancellations across reconnects. Legacy snapshots
  // without receipt times leave persisted messages in their original history sequence.
  if (boundary !== null) waiting.sort((a, b) => Date.parse(a.received) - Date.parse(b.received));
  html += waiting.map(item => item.markup).join('');
  if ((busy.get(topic.tid) ?? topic.busy) && !running(topic.tid)) html += `<div class="meta">${esc(t('chat.answering'))}<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span></div>`;
  thread.innerHTML = html;
  if (seq !== null) {
    thread.querySelector(`[data-entry-seq="${Number(seq)}"]`)?.scrollIntoView({ block: 'center' });
  } else if (bottom || nearBottom) thread.scrollTop = thread.scrollHeight;
  else thread.scrollTop = previousTop;
  updateThreadNavigation();
  if (visibleChat() && (bottom || nearBottom) && entries.length) markSeen(topic.tid, entries.at(-1).seq);
  if (start) {
    state.renderStart = visibleEntries[start].seq;
    scheduleHistoryRender(topic, visibleEntries, start);
  } else if (entries.length) { state.rendered = true; state.renderStart = null; }
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
      const turn = turns.get(entry.turn_id);
      if (turn && entry.role === 'user' && entry.status === 'cancelled') turn.phase = 'cancelled';
      if (turn && entry.role === 'bot') {
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
      const stable = versions.get(item.tid) === topicVersions.get(item.tid);
      const snapshot = item.turns ?? (item.turn_id ? [{ turn_id: item.turn_id, phase: 'started' }] : []);
      const ids = new Set(snapshot.map(turn => turn.turn_id));
      if (stable) {
        for (const turn of turns.values()) {
          if (turn.tid === item.tid && ['queued', 'started'].includes(turn.phase) && !ids.has(turn.id)) {
            turn.phase = 'done'; turn.finished = null;
          }
        }
      }
      for (const state of snapshot) {
        const existing = turns.get(state.turn_id);
        if (!existing && !stable) continue;
        const turn = existing || { id: state.turn_id, tid: item.tid, text: '' };
        if (stable) turn.phase = state.phase;
        // Admission text/time are immutable, even when newer stream events won the phase race.
        if (state.text !== undefined) turn.userText = state.text;
        turn.received = state.received ?? turn.received;
        turn.startedAt = state.started_at ?? turn.startedAt;
        if (stable && state.activity) addActivity(turn, state.activity);
        turns.set(turn.id, turn);
      }
    }
    const topic = getTopic();
    if (topic) { await loadHistory(topic.tid); if (active && generation === epoch) renderThread(); }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
}
export async function handleEvent(event) {
  if (!active) return;
  const topic = getTopic(), tid = event.tid;
  if (tid && ['turn', 'delta', 'tool', 'system_status'].includes(event.type)) {
    topicVersions.set(tid, (topicVersions.get(tid) || 0) + 1);
  }
  switch (event.type) {
    case 'entry': {
      const state = history(tid), entry = event.entry;
      if (!state.entries.has(entry.id)) noteEntry(tid, entry);
      mergeHistory(state, [entry]);
      const turn = turns.get(entry.turn_id);
      if (turn && entry.role === 'user' && entry.status === 'cancelled') turn.phase = 'cancelled';
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
      turn.phase = event.phase;
      turn.startedAt = event.started_at ?? turn.startedAt;
      if (event.phase === 'started') turn.startedAt ??= new Date().toISOString();
      turns.set(turn.id, turn);
      if (event.phase === 'queued' && turn.userText === undefined) await resync();
      if (!['queued', 'started'].includes(event.phase)) {
        turn.finished = new Date().toISOString();
        if (topic?.tid === tid) renderThread();
        if (topic?.tid === tid) await loadHistory(tid);
        await refreshTopics();
      }
      break;
    }
    case 'delta': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, phase: 'started', text: '' };
      if (turn.phase === 'queued') turn.phase = 'started';
      turn.text += event.text; turns.set(turn.id, turn);
      if (topic?.tid === tid) scheduleStreamRender(turn.id);
      return;
    }
    case 'tool':
    case 'system_status': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, phase: 'started', text: '' };
      addActivity(turn, event); turns.set(turn.id, turn);
      if (topic?.tid === tid) scheduleStreamRender(turn.id);
      return;
    }
    case 'telegram': telegram = event; renderStatus(); break;
    case 'secrets_changed':
      document.dispatchEvent(new CustomEvent('vault-refresh')); break;
    case 'bridge':
      if (event.up) document.dispatchEvent(new CustomEvent('vault-refresh'));
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
function resizeComposer() {
  const input = document.querySelector('#msgIn');
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight + input.offsetHeight - input.clientHeight}px`;
}
async function stopAnswer() {
  const topic = getTopic(), generation = epoch;
  if (!active || !topic || topic.read_only || !started(topic.tid)) return;
  try { await api(`/topics/${encodeURIComponent(topic.tid)}/abort`, { method: 'POST' }); }
  catch (error) { if (active && generation === epoch) errorNotice(error); }
}
async function sendMessage() {
  commandMenu?.close();
  const topic = getTopic(), input = document.querySelector('#msgIn');
  if (!active || !topic || topic.read_only) return;
  const text = input.value, control = isControlCommand(text);
  if (sending && !control) return;
  if (!text.trim() || text.length > 32000) return;
  if (!control) sending = true;
  renderHeader();
  const generation = epoch;
  try {
    const result = await api(`/topics/${encodeURIComponent(topic.tid)}/messages`, {
      method: 'POST', body: { text, client_id: crypto.randomUUID() },
    });
    if (!active || generation !== epoch) return;
    if (result.turn_id) {
      const turn = turns.get(result.turn_id) || { id: result.turn_id, tid: topic.tid, phase: 'queued', text: '' };
      turn.userText = text; turn.received ??= new Date().toISOString(); turns.set(turn.id, turn);
    }
    if (result.secret && getTopic()?.tid === topic.tid) secretDialog.show(result, topic.tid);
    drafts.delete(topic.tid);
    if (getTopic()?.tid === topic.tid && input.value === text) { input.value = ''; resizeComposer(); }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
  finally { if (!control) sending = false; if (active && generation === epoch) { renderHeader(); renderThread({ bottom: true }); } }
}
export function initChat() {
  secretDialog = initSecret();
  document.addEventListener('language-changed', () => { renderThread(); renderStatus(); resizeComposer(); });
  const thread = document.querySelector('#thread'), input = document.querySelector('#msgIn');
  let pagination = null, topRequest = null;
  function cancelTop() { topRequest = null; }
  function loadOlder(tid) {
    if (pagination) return pagination;
    const height = thread.scrollHeight, generation = epoch;
    loading = true;
    pagination = (async () => {
      try {
        await loadHistory(tid, { older: true });
        if (!active || generation !== epoch || getTopic()?.tid !== tid) return;
        const previousTop = thread.scrollTop;
        renderThread();
        thread.scrollTop = topRequest?.tid === tid ? 0 : thread.scrollHeight - height + previousTop;
        updateThreadNavigation();
      } finally { loading = false; pagination = null; }
    })();
    return pagination;
  }
  for (const type of ['wheel', 'touchmove', 'pointerdown']) thread.addEventListener(type, cancelTop, { passive: true });
  thread.addEventListener('keydown', event => {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) cancelTop();
  });
  for (const [id, toBottom] of [['threadTop', false], ['threadBottom', true]]) {
    document.querySelector(`#${id}`).addEventListener('click', async () => {
      cancelTop();
      const topic = getTopic();
      if (!topic) return;
      thread.focus({ preventScroll: true });
      if (toBottom) { thread.scrollTop = thread.scrollHeight; updateThreadNavigation(); return; }
      const request = { tid: topic.tid }, generation = epoch;
      topRequest = request;
      history(topic.tid).rendered = true; renderThread();
      try {
        while (topRequest === request && active && generation === epoch && getTopic()?.tid === topic.tid) {
          if (pagination) await pagination;
          else if (history(topic.tid).hasMore) await loadOlder(topic.tid);
          else { thread.scrollTop = 0; break; }
        }
      } catch (error) { if (active && generation === epoch) errorNotice(error); }
      finally { if (topRequest === request) cancelTop(); updateThreadNavigation(); }
    });
  }
  new ResizeObserver(updateThreadNavigation).observe(thread);
  initMarkdownLinks(thread);
  input.maxLength = 32000;
  commandMenu = initCommands(input, value => { if (getTopic()) drafts.set(getTopic().tid, value); resizeComposer(); });
  input.addEventListener('input', () => { if (getTopic()) drafts.set(getTopic().tid, input.value); resizeComposer(); renderHeader(); });
  window.addEventListener('resize', resizeComposer);
  globalThis.visualViewport?.addEventListener('resize', resizeComposer);
  input.addEventListener('keydown', event => {
    if (commandMenu.keydown(event)) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && matchMedia('(pointer: fine)').matches) {
      event.preventDefault(); void sendMessage();
    }
  });
  document.querySelector('#sendBtn').addEventListener('click', sendMessage);
  document.querySelector('#stopBtn').addEventListener('click', stopAnswer);
  thread.addEventListener('click', event => {
    const cancel = event.target.closest('[data-cancel-turn]');
    if (cancel && !cancel.disabled) void cancelQueued(cancel.dataset.cancelTurn);
    const button = event.target.closest('[data-response-entry]');
    if (button && !button.disabled) void chooseButton(button.dataset.responseEntry, button.dataset.responseLabel, 'reply');
    const selector = event.target.closest('[data-callback-entry]');
    if (selector && !selector.disabled) void chooseButton(selector.dataset.callbackEntry, selector.dataset.callbackData, 'selector');
  });
  thread.addEventListener('scroll', async () => {
    updateThreadNavigation();
    const topic = getTopic();
    if (!topic || loading) return;
    if (thread.scrollHeight - thread.clientHeight - thread.scrollTop < 80) {
      const entries = sorted(topic.tid); if (entries.length && visibleChat()) markSeen(topic.tid, entries.at(-1).seq);
    }
    if (thread.scrollTop > 0 || !history(topic.tid).hasMore || !history(topic.tid).rendered) return;
    if (topRequest) return;
    try { await loadOlder(topic.tid); }
    catch (error) { errorNotice(error); }
  });
  document.addEventListener('topic-selected', async event => {
    cancelTop();
    const { topic, seq } = event.detail, generation = epoch, selected = ++selection;
    input.value = drafts.get(topic.tid) || ''; renderThread(); resizeComposer();
    try {
      await loadHistory(topic.tid, { target: seq });
      if (active && generation === epoch && selected === selection && getTopic()?.tid === topic.tid) {
        renderThread({ bottom: seq === null, seq });
        if (location.hash === '#chat' && (!event.detail.focusComposer || document.activeElement !== input)) document.querySelector('[data-view="chat"] h2').focus();
      }
    } catch (error) { if (active && generation === epoch) errorNotice(error); }
  });
  document.addEventListener('topic-updated', () => renderHeader());
  document.addEventListener('route-change', event => {
    view = event.detail.view;
    if (active && event.detail.route === 'chat') { renderThread(); resizeComposer(); }
  });
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) void resync(); });
  document.addEventListener('auth-change', event => {
    cancelTop();
    active = event.detail.authenticated; ++epoch;
    cancelStreamRender(); cancelHistoryRender();
    stopEvents?.(); stopEvents = null;
    histories.clear(); turns.clear(); busy.clear(); drafts.clear(); topicVersions.clear(); busyVersions.clear(); input.value = '';
    telegram = { state: 'unknown' }; botUp = false; webSessions = null;
    pending = 0; outbox = []; renderStatus(); renderHeader(); resizeComposer();
    if (!active) { thread.replaceChildren(); updateThreadNavigation(); return; }
    stopEvents = connectEvents(event => { void handleEvent(event).catch(errorNotice); }, up => {
      if (!up) { botUp = false; renderStatus(); }
      if (up) void resync();
    });
    void resync();
  });
}
