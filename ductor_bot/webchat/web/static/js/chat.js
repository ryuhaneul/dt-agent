import { api } from './api.js';
import { initCommands } from './commands.js';
import { initAttachments } from './attachments.js';
import { initAttachmentView } from './attachment-view.js';
import { initSecret } from './secret.js';
import { initAutocompact } from './autocompact.js';
import { t } from './i18n.js';
import { notify } from './auth.js';
import { connectEvents } from './ws.js';
import { md, esc, createMarkdownCache, createMarkdownStream, initMarkdownLinks } from './md.js';
import { getTopic, getTopics, selectTopic, refreshTopics, noteEntry, markSeen, formatTime } from './topics.js';

const histories = new Map(), turns = new Map(), busy = new Map(), drafts = new Map(), topicVersions = new Map(), busyVersions = new Map();
let active = false, epoch = 0, stopEvents = null, telegram = { state: 'unknown' };
let botUp = false, webSessions = null, pending = 0, outbox = [], sending = false;
let view = 'chat', selection = 0, commandMenu = null, secretDialog = null, autocompactDialog = null;
let streamFrame = null, historyRender = null, composerFiles = null, attachmentView = null;
const streamUpdates = new Set();
const HISTORY_PAGE_SIZE = 100, MESSAGE_WINDOW_SIZE = 100, WINDOW_STEP = 50;
let renderedTopic = null;
function history(tid) {
  if (!histories.has(tid)) histories.set(tid, { entries: new Map(), meta: new Map(), choices: new Map(), mirrorReaders: new Set(), hasMore: false, loaded: false, rendered: false, renderStart: null, range: null, markdown: createMarkdownCache(`entry:${tid}`) });
  return histories.get(tid);
}
export function mergeEntries(entries, incoming) {
  for (const entry of incoming) entries.set(entry.id, entry);
  return [...entries.values()].sort((a, b) => a.seq - b.seq);
}
function sorted(tid) { return mergeEntries(history(tid).entries, []); }
function hasTurnEntry(turn, role) {
  return [...history(turn.tid).entries.values()].some(entry => entry.role === role && entry.turn_id === turn.id);
}
function started(tid) {
  return [...turns.values()].find(turn => turn.tid === tid && turn.phase === 'started');
}
function visibleChat() {
  return view === 'chat' && ['#chat', ''].includes(location.hash) && !document.hidden;
}
function errorNotice(error) {
  if (error.silent) return;
  if (error.attachmentKey) { notify(error.attachmentKey, error.attachmentValues || {}); return; }
  if (error.code === 'auth_required') return;
  notify(['bot_unreachable', 'telegram_unavailable', 'web_turn_running', 'topic_busy', 'read_only'].includes(error.code)
    ? `error.${error.code}` : 'error.generic');
}
function mirror(entry) {
  if (!entry.mirror || !(entry.source === 'web' || entry.role === 'event' || entry.source === 'system')) return '';
  const state = entry.mirror.state;
  const key = state === 'sent' ? 'delivered' : state === 'pending' ? 'pending_mirror' : 'mirror_failed';
  return `<div data-mirror class="${state === 'sent' ? 'deliv' : state === 'pending' ? 'dirty' : 'stopped'}">${esc(t(`chat.${key}`))}</div>`;
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
  state.choices.set(entryId, value); updateEntries(topic.tid, [entryId]);
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
    if (active && generation === epoch) updateEntries(topic.tid, [entryId]);
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
  turn.cancelling = true; updateTurn(turn);
  try {
    await api(`/topics/${encodeURIComponent(turn.tid)}/turns/${encodeURIComponent(turnId)}/cancel`, { method: 'POST' });
    if (!active || generation !== epoch) return;
    turn.phase = 'cancelled';
    if (!hasTurnEntry(turn, 'user')) await loadHistory(turn.tid, { turnId });
  } catch (error) {
    if (!active || generation !== epoch) return;
    if (error.status === 409 && error.code === 'turn_started') {
      // The start won the server's atomic race; this message can no longer be cancelled.
      if (turn.phase === 'queued') turn.phase = 'started';
      await resync();
    } else errorNotice(error);
  } finally {
    turn.cancelling = false;
    if (active && generation === epoch) updateTurn(turn);
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
  const displayText=attachmentView?.text(getTopic().tid,entry)??entry.text;
  const attachmentCards=attachmentView?.markup(getTopic().tid,entry)||'';
  const time = esc(formatTime(entry.ts));
  const delegated = entry.role === 'event' && entry.kind === 'interagent';
  if (entry.role === 'event' && !delegated) {
    const failed = ['failed', 'error', 'cancelled'].includes(entry.status);
    const key = entry.kind === 'task_result' && failed ? 'task_failed' : entry.kind;
    const kind = ['task_result', 'task_failed', 'task_question', 'heartbeat', 'interagent', 'cron', 'webhook', 'background'].includes(key) ? key : 'background';
    return `<div class="event${entry.imported ? ' old' : ''}" data-entry-seq="${entry.seq}"><span class="badge ${kind === 'task_result' ? 'badge-ok' : kind === 'task_failed' ? 'badge-down' : 'badge-off'}">${esc(t(`event.${kind}`))}</span><div><div class="meta">${sourceTag(entry)}<span class="tm">${time}</span>${entry.title ? `<b>${esc(entry.title)}</b>` : ''}</div><div class="md">${history(getTopic().tid).markdown({ ...entry, text: displayText })}</div>${attachmentCards}${responseButtons(entry)}${mirror(entry)}</div></div>`;
  }
  const turn = turns.get(entry.turn_id);
  const stopped = entry.status === 'aborted' || (entry.role === 'bot' && turn?.phase === 'aborted');
  return `<div class="msg ${entry.role === 'user' ? 'me' : 'bot'}${entry.imported ? ' old' : ''}${entry.kind === 'command' ? ' command-message' : ''}" data-entry-seq="${entry.seq}"><div class="meta">${sourceTag(entry)}${delegated ? `<span class="tag">${esc(t('chat.delegated_response'))}</span>` : ''}<span class="tm">${time}</span>${delegated && entry.title ? `<span>${esc(entry.title)}</span>` : ''}</div><div class="bubble md">${entry.role === 'bot' ? activitySummary(turn) : ''}${entry.kind === 'command' ? esc(entry.text || '') : history(getTopic().tid).markdown({ ...entry, text: displayText })}${attachmentCards}${responseButtons(entry)}</div>${entry.role === 'user' ? queueStatus(entry.turn_id, entry.status) : ''}${stopped ? `<div class="stopped">${esc(t('chat.stopped'))}</div>` : ''}${mirror(entry)}</div>`;
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
    const nearBottom = !history(topic.tid).range?.newer && thread.scrollHeight - thread.clientHeight - previousTop < 80;
    for (const id of updates) {
      const turn = turns.get(id);
      if (!turn || turn.tid !== topic.tid) continue;
      const element = thread.querySelector(`[data-turn="${CSS.escape(id)}"]`);
      if (element) {
        if (turn.renderedActivityRevision !== turn.activityRevision || !element.querySelector('[data-stream-complete]')) {
          updateTurn(turn);
          continue;
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
      } else if (!history(topic.tid).range?.newer && !hasTurnEntry(turn, 'bot')) {
        updateTurn(turn);
      }
    }
    updateStopButton(topic);
    if (nearBottom) thread.scrollTop = thread.scrollHeight;
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
  document.querySelector('#headerAutocompact').disabled = !active || !topic || topic.read_only || topic.tid === 'unknown';
  document.querySelector('#topicDelete').hidden = !topic?.deletable;
  document.querySelector('.composer').hidden = !topic || topic.read_only || topic.tid === 'unknown';
  updateStopButton(topic);
  updateSendButton();
}
function updateStopButton(topic) {
  const stop = document.querySelector('#stopBtn'), unavailable = !topic || !started(topic.tid);
  stop.hidden = unavailable;
  stop.disabled = unavailable;
}
function updateSendButton() {
  const input = document.querySelector('#msgIn');
  document.querySelector('#sendBtn').disabled = !getTopic() || (sending && !isControlCommand(input.value));
}
function updateThreadNavigation() {
  const thread = document.querySelector('#thread');
  const top = document.querySelector('#threadTop'), bottom = document.querySelector('#threadBottom');
  const topic = getTopic(), state = topic ? history(topic.tid) : null;
  top.hidden = !state || (!state.range?.older && !state.hasMore && thread.scrollTop < 80);
  bottom.hidden = !state || (!state.range?.newer && thread.scrollHeight - thread.clientHeight - thread.scrollTop < 80);
}
function localDay(date) {
  return Number.isNaN(+date) ? '' : `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}
function entryDayKey(entry) {
  const day = entry.ts ? localDay(new Date(entry.ts)) : '';
  return `${day}:${!!entry.imported}`;
}
function threadItems(rows) {
  const items = [], todayPrefix = `${localDay(new Date())}:`;
  let previousDay;
  for (const row of rows) {
    if (row.kind === 'entry' && !row.waiting) {
      const entry = row.entry, day = entryDayKey(entry);
      if (day !== previousDay) {
        const today = day.startsWith(todayPrefix);
        items.push({ kind: 'day', entry, today, key: `day:${entry.seq}:${day}:${today}` });
      }
      previousDay = day;
    }
    items.push(row);
  }
  return items;
}
function itemMarkup(item) {
  const { kind, key, entry, turn } = item;
  if (kind === 'entry') return entryMarkup(entry);
  if (kind === 'queue') return queuedMarkup(turn);
  if (kind === 'stream') return streamMarkup(turn);
  if (kind === 'busy') return `<div class="meta" data-thread-key="busy">${esc(t('chat.answering'))}<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span></div>`;
  const date = entry.ts && !Number.isNaN(Date.parse(entry.ts)) ? new Date(entry.ts) : null;
  const label = item.today ? t('chat.today') : date ? date.toLocaleDateString(document.documentElement.lang) : '—';
  return `<div class="day" data-thread-key="${key}">${esc(label)}${entry.imported ? ` · ${esc(t('chat.imported_day'))}` : ''}</div>`;
}
function nodeKey(node) {
  const { entrySeq, queueTurn, turn, threadKey } = node.dataset;
  if (entrySeq !== undefined) return `entry:${entrySeq}`;
  if (queueTurn !== undefined) return `queue:${queueTurn}`;
  if (turn !== undefined) return `stream:${turn}`;
  return threadKey;
}
function rememberActivity(node) {
  for (const element of node.querySelectorAll('details[data-activity-turn]')) {
    const turn = turns.get(element.dataset.activityTurn);
    if (turn) turn.activityOpen = element.open;
  }
}
function patchThread(thread, items, changes) {
  // History and queued turns share one order. Keep unchanged nodes in place as that order evolves.
  const wanted = new Set(items.map(item => item.key)), existing = new Map();
  for (const node of thread.children) existing.set(nodeKey(node), node);
  for (const [key, node] of existing) {
    if (!wanted.has(key)) { rememberActivity(node); node.remove(); }
  }
  const template = document.createElement('template');
  let next = null;
  for (let index = items.length - 1; index >= 0; index--) {
    const item = items[index], { entry, turn } = item;
    let node = existing.get(item.key);
    const dirty = item.kind === 'entry'
      ? changes.entries?.has(entry.id) || changes.turns?.has(entry.turn_id)
      : turn && changes.turns?.has(turn.id);
    if (!node || dirty) {
      if (node) rememberActivity(node);
      template.innerHTML = itemMarkup(item);
      const replacement = template.content.firstElementChild;
      if (node) node.replaceWith(replacement);
      node = replacement;
    }
    if (node.parentNode !== thread || node.nextElementSibling !== next) thread.insertBefore(node, next);
    next = node;
  }
}
function updateEntries(tid, entries) {
  if (!active || tid !== getTopic()?.tid) return;
  renderThread({ changes: { entries: new Set(entries) } });
}
function updateTurn(turn, bottom = false) {
  if (!active || turn.tid !== getTopic()?.tid) return;
  renderThread({ bottom, changes: { turns: new Set([turn.id]) } });
}
function cancelHistoryRender() {
  if (!historyRender) return;
  if (historyRender.idle) cancelIdleCallback(historyRender.id);
  else cancelAnimationFrame(historyRender.id);
  historyRender = null;
}
function loadWindowAttachments(tid, rows) {
  attachmentView?.load(tid, rows.filter(row => row.kind === 'entry').map(row => row.entry));
}
function scheduleHistoryRender(topic, rows, start, limit) {
  const state = history(topic.tid), thread = document.querySelector('#thread');
  const job = { tid: topic.tid, start, idle: typeof requestIdleCallback === 'function', id: null };
  historyRender = job;
  function schedule() {
    job.id = job.idle ? requestIdleCallback(appendOlder) : requestAnimationFrame(appendOlder);
  }
  function appendOlder() {
    if (historyRender !== job || !active || getTopic()?.tid !== job.tid) return;
    const next = Math.max(limit, job.start - 12), batch = rows.slice(next, job.start);
    const top = thread.scrollTop, height = thread.scrollHeight;
    const pinned = !state.range.newer && height - thread.clientHeight - top < 80;
    const previous = rows[job.start];
    if (previous.kind === 'entry' && !previous.waiting && batch.at(-1).kind === 'entry' &&
        !batch.at(-1).waiting && entryDayKey(batch.at(-1).entry) === entryDayKey(previous.entry)) {
      thread.firstElementChild?.remove();
    }
    thread.insertAdjacentHTML('afterbegin', threadItems(batch).map(itemMarkup).join(''));
    const newHeight = thread.scrollHeight;
    thread.scrollTop = pinned ? newHeight : top + newHeight - height;
    loadWindowAttachments(topic.tid, batch);
    updateThreadNavigation();
    job.start = next; state.renderStart = rows[next].key;
    if (next > limit) schedule();
    else { state.rendered = true; state.renderStart = null; historyRender = null; }
  }
  schedule();
}
function orderedMessages(entries, liveTurns, topicTurns) {
  const received = liveTurns.map(turn => Date.parse(turn.received));
  const boundary = received.length && received.every(Number.isFinite) ? Math.min(...received) : null;
  const rows = [], waiting = [], userTurns = new Set(), botTurns = new Set();
  for (const entry of entries) {
    if (entry.role === 'user') userTurns.add(entry.turn_id);
    if (entry.role === 'bot') botTurns.add(entry.turn_id);
    const row = { kind: 'entry', entry, key: `entry:${entry.seq}` };
    const turn = turns.get(entry.turn_id);
    if (entry.role === 'user' && boundary !== null && (
      turn?.phase === 'queued' || (entry.status === 'cancelled' && Date.parse(entry.ts) >= boundary)
    )) {
      waiting.push({ ...row, waiting: true, received: turn?.received || entry.ts });
    } else rows.push(row);
  }
  for (const turn of topicTurns) {
    const userExists = userTurns.has(turn.id);
    const queued = { kind: 'queue', turn, key: `queue:${turn.id}`, received: turn.received };
    if (['queued', 'cancelled'].includes(turn.phase)) {
      if (!userExists && turn.userText !== undefined) waiting.push(queued);
      continue;
    }
    if (!userExists && turn.userText !== undefined) rows.push(queued);
    if (!botTurns.has(turn.id)) rows.push({ kind: 'stream', turn, key: `stream:${turn.id}` });
  }
  // Receipt times preserve the position of cancellations across reconnects.
  if (boundary !== null) waiting.sort((a, b) => Date.parse(a.received) - Date.parse(b.received));
  rows.push(...waiting);
  return rows;
}
function viewportMessages(thread) {
  const bounds = thread.getBoundingClientRect();
  let first = null, last = null;
  for (const node of thread.children) {
    if (node.dataset.threadKey !== undefined) continue;
    const key = nodeKey(node), rect = node.getBoundingClientRect();
    if (rect.top >= bounds.bottom) break;
    if (rect.bottom <= bounds.top) continue;
    if (!first) first = { key, top: rect.top };
    last = key;
  }
  return { first, last };
}
function restoreViewport(thread, viewport, previousTop, changes) {
  const anchor = viewport.first && [...thread.children].find(node => nodeKey(node) === viewport.first.key);
  if (!anchor) {
    if (!changes) thread.scrollTop = previousTop;
    return;
  }
  // Read scrollTop after layout so native scroll anchoring is included in the correction.
  const shift = anchor.getBoundingClientRect().top - viewport.first.top;
  if (!shift) return;
  thread.scrollTop += shift;
  // Chromium can quantize scrollTop even when row positions use fractional CSS pixels.
  const remainder = anchor.getBoundingClientRect().top - viewport.first.top;
  if (!remainder || Math.abs(remainder) >= 1) return;
  const previous = parseFloat(thread.style.getPropertyValue('--thread-anchor-offset')) || 0;
  const padding = previous - remainder, whole = Math.trunc(padding);
  const top = thread.scrollTop;
  thread.style.setProperty('--thread-anchor-offset', `${padding - whole}px`);
  if (whole) thread.scrollTop = top - whole;
}
function messageRange(state, rows, { seq, bottom, move, viewport, keepReading }) {
  const length = rows.length;
  const indexOf = key => rows.findIndex(row => row.key === key);
  const previousFirst = indexOf(state.range?.first);
  const previousLast = indexOf(state.range?.last);
  // If a queued/streaming row was replaced, keep the window around its surviving first message.
  const previousEnd = previousLast >= 0 ? previousLast + 1
    : previousFirst >= 0 ? Math.min(length, previousFirst + MESSAGE_WINDOW_SIZE) : length;
  let end = state.range?.newer ? previousEnd : length;
  const target = seq !== null ? indexOf(`entry:${seq}`) : -1;
  if (bottom) end = length;
  else if (target >= 0) end = Math.min(length, Math.max(MESSAGE_WINDOW_SIZE, target + WINDOW_STEP));
  else if (move === 'top') end = Math.min(length, MESSAGE_WINDOW_SIZE);
  else if (move === 'older') {
    const lastVisible = indexOf(viewport.last);
    end = Math.max(Math.min(length, MESSAGE_WINDOW_SIZE), previousEnd - WINDOW_STEP,
      Math.min(lastVisible + 1, previousEnd - 1));
  } else if (move === 'newer') {
    const firstVisible = indexOf(viewport.first?.key);
    end = Math.min(length, previousEnd + WINDOW_STEP,
      firstVisible < 0 ? length : Math.max(previousEnd + 1, firstVisible + MESSAGE_WINDOW_SIZE));
  } else if (keepReading && state.range && previousFirst >= 0 && length - MESSAGE_WINDOW_SIZE > previousFirst) {
    end = previousEnd;
  }
  const start = Math.max(0, end - MESSAGE_WINDOW_SIZE);
  state.range = { first: rows[start]?.key, last: rows[end - 1]?.key, older: start > 0, newer: end < length };
  return { start, end };
}
export function renderThread({ bottom = false, seq = null, changes = null, move = null } = {}) {
  if (!changes) cancelStreamRender();
  cancelHistoryRender();
  const topic = getTopic();
  if (!changes || changes.header) renderHeader();
  else if (changes.turns) updateStopButton(topic);
  const thread = document.querySelector('#thread');
  if (!changes) rememberActivity(thread);
  const sameTopic = renderedTopic === topic?.tid;
  renderedTopic = topic?.tid;
  if (!topic) { thread.replaceChildren(); updateThreadNavigation(); return; }
  const state = history(topic.tid);
  const previousTop = thread.scrollTop;
  const nearBottom = !state.range?.newer && thread.scrollHeight - thread.clientHeight - previousTop < 80;
  const viewport = sameTopic && !bottom && seq === null && (move || !nearBottom)
    ? viewportMessages(thread) : {};
  const entries = sorted(topic.tid);
  const topicTurns = [...turns.values()].filter(turn => turn.tid === topic.tid);
  const liveTurns = topicTurns.filter(turn => ['queued', 'started'].includes(turn.phase));
  const rows = orderedMessages(entries, liveTurns, topicTurns);
  const { start: limit, end } = messageRange(state, rows, {
    seq, bottom, move, viewport, keepReading: sameTopic && !nearBottom,
  });
  let start = limit;
  if (!state.rendered && seq === null && !move && end - limit > 12) {
    const resumed = rows.findIndex(row => row.key === state.renderStart);
    start = resumed >= limit && resumed < end ? resumed : end - 12;
  }
  if (!sameTopic || bottom || seq !== null || move === 'top') thread.style.removeProperty('--thread-anchor-offset');
  const mounted = rows.slice(start, end), items = threadItems(mounted);
  if (!state.range.newer && (busy.get(topic.tid) ?? topic.busy) && !liveTurns.length) {
    items.push({ kind: 'busy', key: 'busy' });
  }
  if (changes) patchThread(thread, items, changes);
  else thread.innerHTML = items.map(itemMarkup).join('');
  loadWindowAttachments(topic.tid, mounted);
  const pinned = !move && (bottom || nearBottom) && !state.range.newer;
  if (seq !== null) {
    thread.querySelector(`[data-entry-seq="${Number(seq)}"]`)?.scrollIntoView({ block: 'center' });
  } else if (move === 'top') thread.scrollTop = 0;
  else if (pinned) thread.scrollTop = thread.scrollHeight;
  else restoreViewport(thread, viewport, previousTop, changes);
  updateThreadNavigation();
  if (visibleChat() && pinned && entries.length) markSeen(topic.tid, entries.at(-1).seq);
  if (start > limit) {
    state.renderStart = rows[start].key;
    scheduleHistoryRender(topic, rows, start, limit);
  } else if (rows.length) { state.rendered = true; state.renderStart = null; }
}
async function loadHistory(tid, { older = false, target = null, turnId = null } = {}) {
  const generation = epoch, state = history(tid), entries = sorted(tid);
  let before = older && entries.length ? `&before=${entries[0].seq}` : '';
  const lower = target ?? (!older && entries.length ? entries[0].seq : null);
  let more;
  do {
    const liveMirrors = new Map();
    state.mirrorReaders.add(liveMirrors);
    let data;
    try {
      data = await api(`/topics/${encodeURIComponent(tid)}/messages?limit=${HISTORY_PAGE_SIZE}${before}`);
    } finally {
      state.mirrorReaders.delete(liveMirrors);
    }
    if (!active || generation !== epoch) return;
    const incoming = turnId === null ? data.entries : data.entries.filter(entry => entry.turn_id === turnId);
    for (const entry of incoming) {
      if (liveMirrors.has(entry.id)) entry.mirror = liveMirrors.get(entry.id);
      if (turnId !== null && !state.entries.has(entry.id)) noteEntry(tid, entry);
    }
    mergeHistory(state, incoming);
    for (const entry of incoming) {
      const turn = turns.get(entry.turn_id);
      if (turn && entry.role === 'user' && entry.status === 'cancelled') turn.phase = 'cancelled';
      if (turn && entry.role === 'bot') {
        turn.phase = entry.status === 'aborted' ? 'aborted' : entry.status === 'error' ? 'error' : 'done';
        turn.finished = entry.ts;
      }
    }
    if (turnId !== null) {
      if (tid === getTopic()?.tid) attachmentView?.load(tid, incoming);
      return incoming.length;
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
  renderOutboxStatus();
}
function renderOutboxStatus() {
  document.querySelector('#pendingBadge').hidden = pending === 0;
  badge('#pendingBadge', 'status.pending', 'badge-warn', { n: pending });
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
  outbox = data.items; renderOutboxStatus();
}
async function resync() {
  attachmentView?.reset?.();
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
    for (const [tid, state] of histories) if (tid !== topic?.tid) state.loaded = false;
    if (topic) {
      await loadHistory(topic.tid);
      if (active && generation === epoch && getTopic()?.tid === topic.tid) renderThread();
    }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
}
function updateMirror(item, historyId, status) {
  if (!item || !historyId) return;
  const state = history(item.tid), entry = state.entries.get(historyId);
  const receipt = status ? { state: status } : null;
  // A history response already in flight can contain an older receipt.
  for (const reader of state.mirrorReaders) reader.set(historyId, receipt);
  if (!entry || (entry.mirror?.state ?? null) === status) return;
  entry.mirror = receipt;
  if (getTopic()?.tid !== item.tid) return;
  const thread = document.querySelector('#thread');
  const node = thread.querySelector(`[data-entry-seq="${Number(entry.seq)}"]`);
  if (!node) return;
  const existing = node.querySelector('[data-mirror]'), html = mirror(entry);
  if (!existing && !html) return;
  const pinned = !state.range?.newer && thread.scrollHeight - thread.clientHeight - thread.scrollTop < 80;
  if (existing) existing.remove();
  if (html) (node.classList.contains('event') ? node.lastElementChild : node).insertAdjacentHTML('beforeend', html);
  if (pinned) thread.scrollTop = thread.scrollHeight;
  updateThreadNavigation();
}
export async function handleEvent(event) {
  if (!active) return;
  const tid = event.tid, generation = epoch;
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
      if (!active || generation !== epoch || tid !== getTopic()?.tid) return;
      attachmentView?.load(tid, [state.entries.get(entry.id)]);
      const entries = new Set([entry.id]);
      const changedTurns = turn && entry.role === 'user' && entry.status === 'cancelled' ? new Set([turn.id]) : null;
      renderThread({ changes: { entries, turns: changedTurns, header: true } });
      return;
    }
    case 'attachments_changed': {
      const entry = history(tid).entries.get(event.entry_id);
      attachmentView?.invalidate(tid, event.entry_id);
      if (entry) {
        entry.has_attachments = true;
        if (tid === getTopic()?.tid) attachmentView?.load(tid, [entry]);
      }
      return;
    }
    case 'entry_meta': {
      const state = history(tid);
      mergeButtonMeta(state, event.entry_id, event.meta || {});
      const entry = state.entries.get(event.entry_id);
      if (entry) {
        mergeHistory(state, [entry]);
        updateEntries(tid, [entry.id]);
      }
      return;
    }
    case 'busy':
      busyVersions.set(tid, (busyVersions.get(tid) || 0) + 1);
      if (busy.get(tid) === event.busy) return;
      busy.set(tid, event.busy);
      if (tid === getTopic()?.tid) renderThread({ changes: {} });
      return;
    case 'turn': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, text: '' };
      turn.phase = event.phase;
      turn.startedAt = event.started_at ?? turn.startedAt;
      if (event.phase === 'started') turn.startedAt ??= new Date().toISOString();
      turns.set(turn.id, turn);
      if (event.phase === 'queued' && turn.userText === undefined) {
        const data = await api('/topics');
        if (!active || generation !== epoch) return;
        const snapshot = data.topics.find(item => item.tid === tid)?.turns?.find(item => item.turn_id === turn.id);
        if (snapshot) {
          turn.userText ??= snapshot.text;
          turn.received ??= snapshot.received;
          if (turn.phase === 'queued' && snapshot.activity) addActivity(turn, snapshot.activity);
        }
      }
      const terminal = !['queued', 'started'].includes(event.phase);
      if (terminal) turn.finished = new Date().toISOString();
      updateTurn(turn);
      if (terminal && tid === getTopic()?.tid && !hasTurnEntry(turn, event.phase === 'cancelled' ? 'user' : 'bot')) {
        const recovered = await loadHistory(tid, { turnId: turn.id });
        if (active && generation === epoch && recovered) updateTurn(turn);
      }
      return;
    }
    case 'delta': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, phase: 'started', text: '' };
      if (turn.phase === 'queued') turn.phase = 'started';
      turn.text += event.text; turns.set(turn.id, turn);
      if (tid === getTopic()?.tid) scheduleStreamRender(turn.id);
      return;
    }
    case 'tool':
    case 'system_status': {
      const turn = turns.get(event.turn_id) || { id: event.turn_id, tid, phase: 'started', text: '' };
      addActivity(turn, event); turns.set(turn.id, turn);
      if (tid === getTopic()?.tid) scheduleStreamRender(turn.id);
      return;
    }
    case 'telegram': telegram = event; renderStatus(); return;
    case 'secrets_changed':
      document.dispatchEvent(new CustomEvent('vault-refresh')); return;
    case 'bridge':
      if (event.up) document.dispatchEvent(new CustomEvent('vault-refresh'));
      botUp = event.up; renderStatus();
      if (event.up) await resync();
      return;
    case 'topics': await refreshTopics(); return;
    case 'outbox': {
      pending = event.pending;
      if (event.item) {
        const index = outbox.findIndex(item => item.id === event.item.id);
        if (index < 0) outbox.push(event.item);
        else outbox[index] = event.item;
        outbox.sort((a, b) => (a.created || '').localeCompare(b.created || ''));
      }
      renderOutboxStatus();
      if (Object.hasOwn(event, 'history_id') && Object.hasOwn(event, 'mirror')) {
        updateMirror(event.item, event.history_id, event.mirror);
      } else {
        // Older bridges only publish queue items without the aggregate receipt.
        const topic = getTopic();
        if (!topic || (event.item?.tid && event.item.tid !== topic.tid)) return;
        await loadHistory(topic.tid);
        if (active && generation === epoch && getTopic()?.tid === topic.tid) renderThread();
      }
      return;
    }
  }
}
function resizeComposer() {
  if (composerFiles) { composerFiles.resize(); return; }
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
  if (/^\/autocompact(?:@[a-z0-9_]+)?\s*$/i.test(input.value.trim())) {
    drafts.delete(topic.tid); input.value = ''; resizeComposer(); renderHeader();
    void autocompactDialog.show(topic); return;
  }
  const text = input.value, control = isControlCommand(text);
  if (sending && !control) return;
  const hasFiles = composerFiles?.has(topic.tid);
  if ((!text.trim() && !hasFiles) || text.length > 32000) return;
  if (hasFiles && text.trimStart().startsWith('/')) { notify('attach.command'); return; }
  if (!control) sending = true;
  renderHeader();
  const generation = epoch;
  let uploaded = null, attachmentError = null, sentTurn = null;
  try {
    uploaded = hasFiles ? await composerFiles.upload(topic.tid) : null;
    if (!active || generation !== epoch) return;
    const result = await api(`/topics/${encodeURIComponent(topic.tid)}/messages`, {
      method: 'POST', body: { text, client_id: crypto.randomUUID(), ...(uploaded ? { attachment_ids: uploaded.ids } : {}) },
    });
    if (uploaded && active && generation === epoch) composerFiles.sent(topic.tid, uploaded.batch);
    if (!active || generation !== epoch) return;
    if (result.turn_id) {
      const turn = turns.get(result.turn_id) || { id: result.turn_id, tid: topic.tid, phase: 'queued', text: '' };
      turn.userText = text; turn.received ??= new Date().toISOString(); turns.set(turn.id, turn);
      sentTurn = turn;
    }
    if (result.secret && getTopic()?.tid === topic.tid) secretDialog.show(result, topic.tid);
    drafts.delete(topic.tid);
    if (getTopic()?.tid === topic.tid && input.value === text) { input.value = ''; resizeComposer(); }
  } catch (error) { attachmentError = error; if (active && generation === epoch) errorNotice(error); }
  finally {
    composerFiles?.finish?.(uploaded?.run || attachmentError?.attachmentRun, attachmentError);
    if (!control) sending = false;
    if (active && generation === epoch) {
      updateSendButton();
      if (sentTurn) updateTurn(sentTurn, true);
      else if (getTopic()?.tid === topic.tid) {
        renderThread({ bottom: true, changes: {} });
      }
    }
  }
}
export function initChat() {
  secretDialog = initSecret();
  attachmentView = initAttachmentView({ changed: (tid, entryId) => updateEntries(tid, [entryId]) });
  autocompactDialog = initAutocompact(getTopic, () => {
    notify('autocompact.saved');
    const toast = document.querySelector('#toasts').lastElementChild;
    toast?.classList.replace('toast-error', 'toast-ok'); toast?.setAttribute('role', 'status');
  });
  document.addEventListener('language-changed', () => { renderThread(); renderStatus(); resizeComposer(); });
  const thread = document.querySelector('#thread'), input = document.querySelector('#msgIn');
  let pagination = null, topRequest = null;
  function cancelTop() { topRequest = null; }
  function fetchOlder(tid) {
    if (pagination?.tid === tid) return pagination.promise;
    const request = { tid, promise: loadHistory(tid, { older: true }) };
    pagination = request;
    return request.promise.finally(() => { if (pagination === request) pagination = null; });
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
      if (toBottom) { renderThread({ bottom: true, changes: {} }); return; }
      const request = { tid: topic.tid }, generation = epoch, selected = selection;
      topRequest = request;
      const current = () => topRequest === request && active && generation === epoch &&
        selected === selection && getTopic()?.tid === topic.tid;
      try {
        while (current() && history(topic.tid).hasMore) await fetchOlder(topic.tid);
        if (current()) renderThread({ move: 'top', changes: {} });
      } catch (error) { if (active && generation === epoch) errorNotice(error); }
      finally { if (topRequest === request) cancelTop(); updateThreadNavigation(); }
    });
  }
  new ResizeObserver(updateThreadNavigation).observe(thread);
  initMarkdownLinks(thread);
  input.maxLength = 32000;
  composerFiles = initAttachments({ getTopic, changed: updateSendButton, resume: () => void sendMessage() });
  function updateDraft() {
    const topic = getTopic();
    if (topic) drafts.set(topic.tid, input.value);
    updateSendButton();
    resizeComposer();
  }
  input.addEventListener('input', updateDraft);
  commandMenu = initCommands(input, updateDraft);
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
    if (!topic || topRequest || pagination?.tid === topic.tid) return;
    const state = history(topic.tid);
    if (!state.rendered || !state.range) return;
    const gap = thread.scrollHeight - thread.clientHeight - thread.scrollTop;
    if (state.range.newer && gap < 1 && thread.scrollTop > 0) {
      renderThread({ move: 'newer', changes: {} });
    } else if (thread.scrollTop <= 0 && (state.range.older || state.hasMore)) {
      const generation = epoch, selected = selection;
      try {
        if (!state.range.older) await fetchOlder(topic.tid);
        if (active && generation === epoch && selected === selection && !topRequest && thread.scrollTop <= 0) {
          renderThread({ move: 'older', changes: {} });
        }
      } catch (error) { if (active && generation === epoch) errorNotice(error); }
    } else if (gap < 80 && !state.range.newer) {
      const entries = sorted(topic.tid);
      if (entries.length && visibleChat()) markSeen(topic.tid, entries.at(-1).seq);
    }
  });
  document.addEventListener('topic-selected', async event => {
    cancelTop();
    const { topic, seq } = event.detail, generation = epoch, selected = ++selection;
    const state = history(topic.tid);
    state.range = null; state.renderStart = null;
    input.value = drafts.get(topic.tid) || ''; renderThread({ bottom: true }); resizeComposer();
    try {
      const cached = seq === null ? [] : sorted(topic.tid);
      if (!state.loaded || !cached.some(entry => entry.seq === seq)) {
        await loadHistory(topic.tid, { target: seq, older: seq !== null && seq < cached[0]?.seq });
      }
      if (active && generation === epoch && selected === selection && getTopic()?.tid === topic.tid) {
        renderThread({ bottom: seq === null, seq });
        if (location.hash === '#chat' && !event.detail.focusComposer) {
          if (seq === null && !topic.read_only && topic.tid !== 'unknown') {
            if (matchMedia('(pointer: fine)').matches) input.focus();
          } else if (seq !== null) document.querySelector('[data-view="chat"] h2').focus();
        }
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
    stopEvents?.(); stopEvents = null; renderedTopic = null; pagination = null;
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
