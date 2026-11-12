import { api } from './api.js';
import { t } from './i18n.js';
import { md, esc } from './md.js';
import { diff } from './diff.js';
import { go, openDialog, closeDialog } from './router.js';
import { notify } from './auth.js';

const names = ['PERSONA.md', 'BEHAVIOR.md', 'MAINMEMORY.md', 'SHAREDMEMORY.md'];
const files = new Map();
let active = false, epoch = 0, selection = 0, available = null, listing = null;
let current = null, mode = 'edit', review = null, tabFocus = null;
function dirty(file) { return file && file.content !== file.base; }
function backupDirectory() {
  const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const value = type => parts.find(part => part.type === type).value;
  return `webchat/backups/${value('year')}-${value('month')}-${value('day')}/`;
}
function errorNotice(error) {
  if (error.code !== 'auth_required') notify(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : error.status === 413 ? 'files.too_large' : 'error.generic');
}
function savedNotice(name) {
  const toast = document.createElement('div'); toast.className = 'toast toast-ok'; toast.setAttribute('role', 'status');
  const message = document.createElement('span'); message.textContent = t('files.saved', { name });
  const close = document.createElement('button'); close.type = 'button'; close.className = 'toast-close';
  close.setAttribute('aria-label', t('common.close')); close.textContent = '×';
  close.addEventListener('click', () => toast.remove()); toast.append(message, close);
  document.querySelector('#toasts').appendChild(toast); setTimeout(() => toast.remove(), 5000);
}
function renderFiles() {
  const file = files.get(current), input = document.querySelector('#ed');
  for (const button of document.querySelectorAll('#flist [data-file]')) {
    button.setAttribute('aria-current', String(button.dataset.file === current));
    button.toggleAttribute('data-dirty', !!dirty(files.get(button.dataset.file)));
    button.disabled = !available?.has(button.dataset.file);
  }
  document.querySelector('#fname').textContent = current || '—';
  if (input.value !== (file?.content || '')) input.value = file?.content || '';
  input.disabled = !file || !!file.saving || !!file.loading;
  input.classList.toggle('hidden', mode === 'preview');
  const preview = document.querySelector('#prev');
  preview.classList.toggle('hidden', mode !== 'preview');
  preview.innerHTML = mode === 'preview' && file ? md(file.content) : '';
  document.querySelector('#dirty').classList.toggle('hidden', !dirty(file));
  document.querySelector('#fileSharedHint').hidden = !file?.shared_block;
  document.querySelector('#fileReload').hidden = !file?.conflict;
  document.querySelector('#fileReload').disabled = !!file?.saving || !!file?.loading;
  document.querySelector('#fileSave').disabled = !file || !!file.saving || !!file.loading || !!file.conflict;
  document.querySelector('#backupTo').textContent = t('files.backup_to', { path: file?.backup ? `webchat/${file.backup}` : backupDirectory() });
  for (const [id, selected] of [['tEdit', mode !== 'preview'], ['tPrev', mode === 'preview']]) {
    const tab = document.querySelector(`#${id}`); tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
  }
}
async function loadListing() {
  if (available) return;
  if (listing) return listing;
  const generation = epoch;
  const request = api('/files').then(data => {
    if (active && generation === epoch) available = new Set(data.files.map(file => file.name).filter(name => names.includes(name)));
  });
  listing = request;
  try { await request; } finally { if (listing === request) listing = null; }
}
export async function openFile(name, { reload = false, navigate = true } = {}) {
  if (!active || !names.includes(name)) return;
  const generation = epoch, selected = ++selection;
  try {
    await loadListing();
    if (!active || generation !== epoch || selected !== selection || !available?.has(name)) return;
    if (reload && files.get(name)?.saving) return;
    current = name;
    const cached = files.get(name);
    if (reload && cached) cached.loading = true;
    renderFiles();
    if (!files.has(name) || reload) {
      try {
        const data = await api(`/files/${encodeURIComponent(name)}`);
        if (!active || generation !== epoch) return;
        if (selected === selection || !files.has(name)) files.set(name, { ...data, base: data.content, conflict: false, saving: false, loading: false });
      } finally { if (cached) cached.loading = false; }
    }
    if (active && generation === epoch && current === name && selected === selection) {
      if (navigate) { mode = 'edit'; go('edit'); }
      renderFiles();
    }
  } catch (error) { if (active && generation === epoch) { errorNotice(error); renderFiles(); } }
}
function prepareDiff() {
  const file = files.get(current);
  document.querySelector('#diffSave').disabled = true; review = null;
  if (!file || file.saving || file.loading || file.conflict) return false;
  if (new TextEncoder().encode(file.content).length > 262144) { notify('files.too_large'); return false; }
  review = { name: current, content: file.content, base_sha256: file.sha256 };
  const rows = diff(file.base, file.content), changed = rows.some(([kind]) => kind !== 'ctx');
  document.querySelector('#diffT').textContent = current;
  document.querySelector('#diffBox').innerHTML = changed ? rows.map(([kind, line]) => `<div class="${kind}">${kind === 'add' ? '+ ' : kind === 'del' ? '− ' : '  '}${esc(line) || ' '}</div>`).join('') : `<div class="ctx">${esc(t('files.no_changes'))}</div>`;
  document.querySelector('#backupPath').textContent = t('files.backup_path', { path: backupDirectory() });
  document.querySelector('#diffSave').disabled = !changed;
  return true;
}
async function save() {
  const snapshot = review, generation = epoch, file = snapshot && files.get(snapshot.name);
  if (!active || !file || file.saving || file.conflict || !dirty(file) || file.sha256 !== snapshot.base_sha256 || file.content !== snapshot.content) return;
  file.saving = true; document.querySelector('#diffSave').disabled = true; renderFiles();
  try {
    const result = await api(`/files/${encodeURIComponent(snapshot.name)}`, {
      method: 'PUT', body: { content: snapshot.content, base_sha256: snapshot.base_sha256 },
    });
    if (!active || generation !== epoch) return;
    file.base = snapshot.content; file.sha256 = result.sha256; file.backup = result.backup; file.conflict = false;
    savedNotice(snapshot.name);
    if (current === snapshot.name && location.hash === '#diff') closeDialog();
  } catch (error) {
    if (!active || generation !== epoch) return;
    if (error.status === 409 && error.code === 'conflict') {
      file.conflict = true; notify('files.conflict');
      if (current === snapshot.name && location.hash === '#diff') closeDialog();
    } else errorNotice(error);
  } finally {
    if (active && generation === epoch) {
      file.saving = false; renderFiles();
      if (location.hash === '#diff' && review === snapshot) document.querySelector('#diffSave').disabled = !dirty(file) || file.conflict || current !== snapshot.name || file.content !== snapshot.content || file.sha256 !== snapshot.base_sha256;
    }
  }
}
export function initFiles() {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-file]');
    if (!button || !names.includes(button.dataset.file)) return;
    event.preventDefault(); void openFile(button.dataset.file);
  });
  document.querySelector('#ed').addEventListener('input', event => {
    const file = files.get(current); if (!file || file.saving) return;
    file.content = event.target.value; renderFiles();
  });
  document.querySelector('#fileSave').addEventListener('click', event => {
    if (prepareDiff()) openDialog('diff', event.currentTarget);
  });
  document.querySelector('#diffSave').addEventListener('click', save);
  document.querySelector('#fileReload').addEventListener('click', async () => {
    const name = current, generation = epoch;
    await openFile(name, { reload: true, navigate: false });
    if (active && generation === epoch && current === name) document.querySelector('#ed').focus();
  });
  for (const id of ['tEdit', 'tPrev']) {
    document.querySelector(`#${id}`).addEventListener('click', () => { tabFocus = id; });
    document.querySelector(`#${id}`).addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === 'Home' ? 'tEdit' : event.key === 'End' ? 'tPrev' : id === 'tEdit' ? 'tPrev' : 'tEdit';
      tabFocus = target;
      go(target === 'tEdit' ? 'edit' : 'preview'); document.querySelector(`#${target}`).focus();
    });
  }
  document.addEventListener('route-change', async event => {
    if (!active || event.detail.view !== 'files') return;
    const generation = epoch, route = event.detail.route;
    const focusedTab = tabFocus; tabFocus = null;
    mode = route === 'preview' ? 'preview' : 'edit'; renderFiles();
    if (focusedTab && route === (focusedTab === 'tEdit' ? 'edit' : 'preview')) document.querySelector(`#${focusedTab}`).focus();
    try {
      await loadListing();
      if (!active || generation !== epoch || location.hash !== `#${route}`) return;
      const name = available.has(current) ? current : names.find(name => available.has(name));
      if (name) await openFile(name, { navigate: false });
      if (active && generation === epoch && location.hash === `#${route}` && route === 'diff' && !prepareDiff()) closeDialog();
    } catch (error) { if (active && generation === epoch) errorNotice(error); }
  });
  document.addEventListener('auth-change', event => {
    active = event.detail.authenticated; ++epoch; ++selection;
    available = null; listing = null; current = null; review = null; tabFocus = null; mode = 'edit'; files.clear();
    renderFiles(); document.querySelector('#diffBox').replaceChildren(); document.querySelector('#diffSave').disabled = true;
  });
  renderFiles();
}
