import { api } from './api.js';
import { t } from './i18n.js';
import { esc } from './md.js';
import { getTopics, getTopic, refreshTopics, formatTime } from './topics.js';
import { go, openDialog, closeDialog } from './router.js';
import { notify } from './auth.js';

let active = false, epoch = 0, editing = null, draft = '', deleting = null, working = false;
function find(tid) { return getTopics().find(topic => topic.tid === tid); }
function errorNotice(error) {
  if (error.code !== 'auth_required') notify(['telegram_unavailable', 'bot_unreachable', 'topic_busy', 'read_only'].includes(error.code) ? `error.${error.code}` : 'error.generic');
}
function focusRename(tid) {
  [...document.querySelectorAll('#mlist [data-rename]')].find(button => button.dataset.rename === tid)?.focus();
}
export function renderManage() {
  const list = document.querySelector('#mlist');
  const focus = document.activeElement;
  const focusedAction = focus?.dataset.rename ? ['rename', focus.dataset.rename]
    : focus?.dataset.deleteTid ? ['deleteTid', focus.dataset.deleteTid] : null;
  const hadInput = focus?.id === 'renameInput';
  const selection = hadInput ? [focus.selectionStart, focus.selectionEnd] : null;
  let html = '';
  for (const group of ['telegram', 'imported']) {
    const topics = getTopics().filter(topic => topic.group === group && topic.tid !== 'unknown');
    if (!topics.length) continue;
    html += `<div class="grp"><h3>${esc(t(`topic.group_${group}`))}</h3><span class="count">${topics.length}</span></div>`;
    for (const topic of topics) {
      if (editing === topic.tid && topic.renamable) {
        html += `<form class="mrow editing" data-rename-form="${esc(topic.tid)}"><div class="tx"><label class="lb" for="renameInput">${esc(t('topic.new_name'))}</label><input id="renameInput" class="field" maxlength="128" required value="${esc(draft)}" ${working ? 'disabled' : ''}><p class="hint">${esc(t('topic.current', { name: topic.name, id: topic.topic_id ?? topic.chat_id }))}</p></div><div class="acts"><button class="btn" type="button" data-rename-cancel ${working ? 'disabled' : ''}>${esc(t('common.cancel'))}</button><button class="btn btn-primary" type="submit" ${working || !draft.trim() ? 'disabled' : ''}>${esc(t('common.save'))}</button></div></form>`;
      } else {
        html += `<div class="mrow"><div class="tx"><div class="nm">${esc(topic.name)}</div><div class="tm">${esc(t('topic.last_active', { time: formatTime(topic.last_ts, true) }))}</div></div><div class="acts">${topic.renamable ? `<button class="btn btn-sm" type="button" data-rename="${esc(topic.tid)}">${esc(t('topic.rename'))}</button>` : ''}${topic.deletable ? `<button class="btn btn-sm btn-danger" type="button" data-delete-tid="${esc(topic.tid)}">${esc(t('topic.delete'))}</button>` : ''}</div></div>`;
      }
    }
  }
  list.innerHTML = html;
  if (hadInput && editing) {
    const input = document.querySelector('#renameInput');
    input?.focus(); if (input && selection) input.setSelectionRange(...selection);
  } else if (focusedAction) [...list.querySelectorAll('button')].find(button => button.dataset[focusedAction[0]] === focusedAction[1])?.focus();
}
function beginRename(tid) {
  const topic = find(tid);
  if (!topic?.renamable || working) return;
  editing = tid; draft = topic.name; renderManage();
  if (location.hash === '#manage') { document.querySelector('#renameInput')?.focus(); document.querySelector('#renameInput')?.select(); }
}
function updateDelete() {
  const topic = find(deleting);
  const button = document.querySelector('#delGo');
  button.disabled = working || !topic?.deletable || document.querySelector('#delIn').value !== topic.name;
  if (!topic) return;
  document.querySelector('#delT span').textContent = t('del.title', { name: topic.name });
  document.querySelector('[data-i18n="del.item_history"]').textContent = t('del.item_history', { n: topic.count });
  document.querySelector('[data-i18n="del.confirm"]').textContent = t('del.confirm', { name: topic.name });
}
function requestDelete(tid, opener) {
  if (!find(tid)?.deletable || working) return;
  deleting = tid; document.querySelector('#delIn').value = ''; updateDelete();
  openDialog('delete', opener);
}
async function rename() {
  const topic = find(editing), name = draft.trim(), generation = epoch;
  if (!active || working || !topic?.renamable || !name || name.length > 128) return;
  working = true; renderManage();
  try {
    await api(`/topics/${encodeURIComponent(topic.tid)}`, { method: 'PATCH', body: { name } });
    if (!active || generation !== epoch) return;
    editing = null; await refreshTopics();
    if (active && generation === epoch) { renderManage(); focusRename(topic.tid); }
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
  finally { if (generation === epoch) {
    working = false; renderManage();
    if (editing && location.hash === '#manage') document.querySelector('#renameInput')?.focus();
  } }
}
async function remove() {
  const topic = find(deleting), confirmation = document.querySelector('#delIn').value, generation = epoch;
  if (!active || working || !topic?.deletable || confirmation !== topic.name) return;
  working = true; updateDelete();
  try {
    await api(`/topics/${encodeURIComponent(topic.tid)}`, { method: 'DELETE', body: { confirm_name: confirmation } });
    if (!active || generation !== epoch) return;
    deleting = null; editing = null; await refreshTopics();
    if (active && generation === epoch && location.hash === '#delete') closeDialog();
  } catch (error) { if (active && generation === epoch) errorNotice(error); }
  finally { if (generation === epoch) { working = false; updateDelete(); } }
}
export function initManage() {
  const list = document.querySelector('#mlist');
  list.addEventListener('input', event => {
    if (event.target.id !== 'renameInput') return;
    draft = event.target.value;
    event.target.closest('form').querySelector('[type="submit"]').disabled = working || !draft.trim();
  });
  list.addEventListener('submit', event => { event.preventDefault(); void rename(); });
  list.addEventListener('keydown', event => {
    if (event.key === 'Escape' && editing && !working) {
      event.preventDefault(); const tid = editing; editing = null; renderManage(); focusRename(tid);
    }
  });
  list.addEventListener('click', event => {
    const renameButton = event.target.closest('[data-rename]');
    const deleteButton = event.target.closest('[data-delete-tid]');
    if (renameButton) beginRename(renameButton.dataset.rename);
    else if (deleteButton) requestDelete(deleteButton.dataset.deleteTid, deleteButton);
    else if (event.target.closest('[data-rename-cancel]') && !working) {
      const tid = editing; editing = null; renderManage(); focusRename(tid);
    }
  });
  document.querySelector('#topicRename').addEventListener('click', () => beginRename(getTopic()?.tid));
  document.querySelector('#topicDelete').addEventListener('click', event => {
    event.preventDefault(); event.stopPropagation(); requestDelete(getTopic()?.tid, event.currentTarget);
  });
  document.querySelector('#delIn').addEventListener('input', updateDelete);
  document.querySelector('#delIn').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); void remove(); }
  });
  document.querySelector('#delGo').addEventListener('click', remove);
  document.addEventListener('topic-created', event => beginRename(event.detail.tid));
  document.addEventListener('topics-updated', () => {
    if (!active) return;
    if (editing && !find(editing)?.renamable) editing = null;
    renderManage(); updateDelete();
    if (deleting && !find(deleting) && location.hash === '#delete') { deleting = null; closeDialog(); }
  });
  document.addEventListener('route-change', event => {
    if (!active) return;
    if (event.detail.route !== 'delete') deleting = null;
    if (event.detail.route === 'manage') {
      renderManage(); if (editing) document.querySelector('#renameInput')?.focus();
    } else if (event.detail.route === 'delete') {
      if (!deleting) { deleting = getTopic()?.tid; document.querySelector('#delIn').value = ''; }
      updateDelete();
      if (!find(deleting)?.deletable) closeDialog();
    } else if (event.detail.route !== 'status') deleting = null;
  });
  document.addEventListener('auth-change', event => {
    active = event.detail.authenticated; ++epoch;
    editing = null; deleting = null; draft = ''; working = false;
    if (!active) { list.replaceChildren(); document.querySelector('#delIn').value = ''; updateDelete(); }
  });
}
