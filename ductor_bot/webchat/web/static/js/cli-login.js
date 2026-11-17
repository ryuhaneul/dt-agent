import { api } from './api.js';
import { t, translate } from './i18n.js';

const providers = ['claude', 'codex', 'grok'];
const terminal = ['succeeded', 'failed', 'cancelled', 'timed_out'];
let openModal = () => {};
export function openCliLogin() { openModal(); }

export function initCliLogin() {
  const $ = id => document.getElementById(id);
  const dialog = $('cliLoginDlg'), confirmation = $('cliConfirmDlg');
  let view = 'list', status = null, target = null, job = null, seq = 0;
  let timer = null, generation = 0, busy = false, loading = false, polling = false, confirmMode = 'start';
  let noticeKey = null, copied = false, cancelRequested = false, refreshingProvider = null;
  let accountsKey = null, linksKey = null;
  let codeFocusPending = false, returnToCode = false;
  // Probes can take 10s (Claude) + 15s (Grok), plus a queued completion probe.
  // Bound unknown-status polling; two snapshots skip the first cached response.
  let statusChecksLeft = 60, refreshChecksLeft = 0;
  const active = () => dialog.open && view === 'progress' && job && !terminal.includes(job.state);
  const slotName = name => ['', 'default', 'claude'].includes(name) ? 'claude'
    : name.startsWith('claude-') ? name : `claude-${name}`;
  const targetName = () => target?.provider === 'claude' && target.slot
    ? `claude · ${target.slot === 'claude' ? t('cli_login.default_slot') : target.slot}` : target?.provider || '';
  function stopPolling() { clearTimeout(timer); timer = null; }
  function showNotice(key) { noticeKey = key; $('cliNotice').textContent = key ? t(key) : ''; $('cliNotice').hidden = !key; $('cliNotice').classList.toggle('cli-notice-ok', key === 'cli_login.sent'); }
  function node(tag, className, text) {
    const element = document.createElement(tag); element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function button(key, callback, primary = false) {
    const element = node('button', `btn btn-sm${primary ? ' btn-primary' : ''}`, t(key));
    element.dataset.i18n = key;
    element.type = 'button'; element.disabled = busy || loading || Boolean(status?.running);
    element.addEventListener('click', callback); return element;
  }
  function badge(authenticated, state = null) {
    const key = state ? `cli_login.${state}` : authenticated === null ? 'cli_login.checking' : authenticated ? 'cli_login.authenticated' : 'cli_login.unauthenticated';
    const element = node('span', `badge ${state === 'expired' ? 'badge-warn' : !state && authenticated ? 'badge-ok' : 'badge-off'}`, t(key));
    element.dataset.i18n = key; return element;
  }
  function renderAccounts() {
    const root = $('cliProviders');
    const key = JSON.stringify([status?.providers, status?.claude_slots]);
    if (key === accountsKey) {
      root.querySelectorAll('button').forEach(element => { element.disabled = busy || loading || Boolean(status?.running); });
      return;
    }
    accountsKey = key; root.replaceChildren();
    providers.forEach(provider => {
      const section = node('section', 'cli-provider');
      const head = node('div', 'cli-provider-head'); head.append(node('h3', 'mono', provider));
      if (provider === 'claude') {
        head.append(button('cli_login.add_slot', () => {
          view = 'slot'; $('cliSlotForm').reset(); $('cliSlotError').hidden = true;
          $('cliSlotName').removeAttribute('aria-invalid'); render(); $('cliSlotName').focus();
        }));
        section.append(head);
        const slots = node('div', 'cli-slots');
        (status?.claude_slots || []).forEach(slot => {
          const row = node('div', 'cli-slot');
          const name = node('span', 'cli-slot-name mono', slot.name === 'claude' ? t('cli_login.default_slot') : slot.name);
          if (slot.name === 'claude') name.dataset.i18n = 'cli_login.default_slot';
          const state = slot.priority == null ? 'unused' : slot.auth_expired ? 'expired' : null;
          row.append(name, badge(slot.authenticated, state),
            button(slot.authenticated ? 'cli_login.relogin' : 'cli_login.login', () => askStart({ provider, slot: slot.name, new_slot: false }), !slot.authenticated));
          slots.append(row);
        });
        section.append(slots);
      } else {
        const authenticated = status?.providers?.[provider] == null ? null : status.providers[provider] === 'authenticated';
        head.append(badge(authenticated), button(authenticated ? 'cli_login.relogin' : 'cli_login.login',
          () => askStart({ provider }), !authenticated));
        section.append(head);
      }
      root.append(section);
    });
  }
  function renderConfirmation() {
    const cancel = confirmMode === 'cancel';
    $('cliConfirmTitle').textContent = t(cancel ? 'cli_login.cancel_title' : 'cli_login.confirm_title');
    $('cliConfirmTarget').textContent = targetName();
    $('cliConfirmWarning').textContent = t(cancel ? 'cli_login.cancel_warning'
      : 'cli_login.confirm_warning')
      + (!cancel && target?.provider === 'codex' ? ` ${t('cli_login.confirm_codex')}` : '');
    $('cliConfirmNo').textContent = t(cancel ? 'cli_login.keep_login' : 'common.cancel');
    $('cliConfirmYes').textContent = t(cancel ? 'cli_login.confirm_cancel' : 'cli_login.start');
    $('cliConfirmYes').classList.toggle('btn-danger', cancel);
    $('cliConfirmYes').classList.toggle('btn-primary', !cancel);
    $('cliConfirmYes').disabled = busy;
  }
  function focusCodeInput() {
    const input = $('cliCodeInput'), focused = document.activeElement;
    if (!codeFocusPending || !active() || !job.needs_input || input.disabled
      || confirmation.open || document.hidden) return;
    if (focused === input) { codeFocusPending = false; return; }
    // Keep explicit interaction with other controls (including raw output) intact.
    if (focused && focused !== document.body && focused !== dialog && focused !== $('cliClose')
      && focused.getClientRects().length && !$('cliLinkRows').contains(focused)) {
      codeFocusPending = false; return;
    }
    input.focus(); codeFocusPending = false;
  }
  function renderProgress() {
    $('cliTarget').textContent = targetName();
    const links = $('cliLinkRows'), nextLinksKey = JSON.stringify(job?.urls || []);
    if (nextLinksKey !== linksKey) {
      linksKey = nextLinksKey; links.replaceChildren();
      (job?.urls || []).forEach(value => {
        let url; try { url = new URL(value); } catch { return; }
        if (url.protocol !== 'https:') return;
        const row = node('div', 'cli-link-row');
        const link = node('a', 'btn btn-primary', t('cli_login.open_link'));
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.dataset.i18n = 'cli_login.open_link'; link.dataset.i18nAttr = 'aria-label:cli_login.open_link';
        link.setAttribute('aria-label', t('cli_login.open_link'));
        link.addEventListener('click', () => {
          returnToCode = true; codeFocusPending = true; focusCodeInput();
        });
        row.append(node('span', 'cli-url mono', url.host + url.pathname), link); links.append(row);
      });
    }
    $('cliLinks').hidden = !links.childElementCount;
    $('cliDevice').hidden = !job?.code; $('cliDeviceCode').textContent = job?.code || '';
    $('cliCopy').textContent = t(copied ? 'cli_login.copied' : 'cli_login.copy');
    if ($('cliCodeForm').hidden && job?.needs_input) codeFocusPending = true;
    $('cliCodeForm').hidden = !job?.needs_input;
    $('cliWaiting').hidden = Boolean(job?.needs_input);
    $('cliCancel').disabled = busy || cancelRequested;
    [$('cliCodeInput'), $('cliRawInput'), ...dialog.querySelectorAll('#cliCodeForm button, #cliRawForm button')]
      .forEach(control => { control.disabled = busy || cancelRequested; });
    focusCodeInput();
  }
  function renderResult() {
    const state = view === 'busy' ? 'busy' : job?.state || 'failed';
    $('cliResultBadge').textContent = t(`cli_login.${state}`);
    $('cliResultBadge').className = `badge ${state === 'succeeded' ? 'badge-ok' : state === 'failed' ? 'badge-down' : 'badge-warn'}`;
    $('cliResultTitle').textContent = t(`cli_login.${state}_title`);
    $('cliResultMessage').textContent = t(state === 'cancelled' && target?.provider === 'codex' ? 'cli_login.cancelled_codex' : `cli_login.${state}_message`);
    $('cliRestart').hidden = !job?.restart_required;
    $('cliBusyProvider').hidden = state !== 'busy' || !status?.provider;
    $('cliBusyProvider').textContent = status?.provider ? `${status.provider} · ${t('cli_login.running')}` : '';
    $('cliRetry').hidden = ['succeeded', 'busy'].includes(state);
  }
  function render() {
    translate(dialog); translate(confirmation);
    $('cliAccounts').hidden = !['list', 'busy'].includes(view);
    $('cliSlot').hidden = view !== 'slot'; $('cliProgress').hidden = view !== 'progress';
    $('cliResult').hidden = !['result', 'busy'].includes(view);
    if (['list', 'busy'].includes(view)) renderAccounts();
    if (view === 'slot') $('cliSlotPreview').textContent = t('cli_login.slot_preview', { name: slotName($('cliSlotName').value) });
    if (view === 'progress') renderProgress();
    if (['result', 'busy'].includes(view)) renderResult();
    showNotice(noticeKey); renderConfirmation();
    if (!$('cliSlotError').hidden) $('cliSlotError').textContent = t($('cliSlotError').dataset.key);
  }
  function schedule() {
    stopPolling();
    if (active()) timer = setTimeout(pollJob, 1000);
    else if (dialog.open && ['list', 'result'].includes(view) && !busy && statusChecksLeft > 0 && (refreshChecksLeft || Object.values(status?.providers || {}).some(value => value === null))) timer = setTimeout(loadStatus, 1000);
  }
  async function loadStatus() {
    if (!dialog.open || !['list', 'result'].includes(view) || busy || statusChecksLeft <= 0) return;
    const version = generation; statusChecksLeft--; loading = true; renderAccounts();
    try {
      const result = await api('/cli-login/status');
      if (version !== generation || !dialog.open) return;
      if (!['list', 'result'].includes(view) || busy) return;
      status = result;
      if (refreshChecksLeft && result.providers?.[refreshingProvider] != null) refreshChecksLeft--;
      if (!refreshChecksLeft) refreshingProvider = null;
      if (view === 'result') return;
      if (result.running && result.job_id) {
        seq = 0; $('cliRawOutput').textContent = ''; job = { job_id: result.job_id, state: 'running' };
        target = { provider: result.provider }; view = 'progress'; await pollJob(); return;
      }
      view = result.running ? 'busy' : 'list'; showNotice(null);
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    } finally {
      if (version === generation && dialog.open) { loading = false; render(); schedule(); }
    }
  }
  async function pollJob() {
    if (!active()) return;
    if (polling) { schedule(); return; }
    polling = true;
    const version = generation, id = job.job_id;
    try {
      const result = await api(`/cli-login/jobs/${encodeURIComponent(id)}?after=${seq}`);
      if (version !== generation || !dialog.open || job?.job_id !== id) return;
      (result.output || []).forEach(chunk => { if (chunk.seq > seq) $('cliRawOutput').textContent += chunk.text; });
      $('cliRawOutput').textContent = $('cliRawOutput').textContent.slice(-65536);
      seq = result.seq; job = result; target = { ...target, provider: result.provider, slot: result.slot };
      showNotice(null);
      if (terminal.includes(result.state)) {
        stopPolling(); view = 'result';
        refreshingProvider = result.provider; refreshChecksLeft = 2; statusChecksLeft = 60;
        loadStatus();
      }
      render();
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      if (error.status === 404) { job = { ...job, state: 'failed' }; view = 'result'; stopPolling(); }
      showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic'); render();
    } finally { polling = false; if (dialog.open) schedule(); }
  }
  function askStart(selection) {
    if (busy) return;
    target = selection; confirmMode = 'start'; renderConfirmation(); confirmation.showModal(); $('cliConfirmNo').focus();
  }
  function askCancel() {
    if (!active() || busy || cancelRequested) return;
    confirmMode = 'cancel'; renderConfirmation(); confirmation.showModal(); $('cliConfirmNo').focus();
  }
  async function startJob() {
    const version = generation; busy = true; renderConfirmation();
    try {
      const result = await api('/cli-login/jobs', { method: 'POST', body: target });
      if (version !== generation || !dialog.open) return;
      confirmation.close(); copied = false; cancelRequested = false; seq = 0;
      codeFocusPending = false; returnToCode = false;
      $('cliRawOutput').textContent = ''; $('cliCodeForm').reset(); $('cliRawForm').reset(); $('cliRaw').open = false;
      job = { job_id: result.job_id, state: 'running' }; view = 'progress'; showNotice(null); render();
      await pollJob();
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      confirmation.close();
      if (error.code === 'login_running') { view = 'busy'; job = null; status = { ...status, running: true }; }
      else { job = { state: 'failed' }; view = 'result'; }
      showNotice(error.code === 'invalid_slot' ? 'cli_login.invalid_slot' : error.code === 'bot_unreachable' ? 'error.bot_unreachable' : null);
    } finally { if (version === generation && dialog.open) { busy = false; render(); schedule(); } }
  }
  async function retryJob() {
    if (busy || !target) return;
    const selection = { ...target };
    if (selection.provider !== 'claude') { askStart(selection); return; }
    const version = generation; busy = true; $('cliRetry').disabled = true;
    try {
      // Failed authentication can leave credentials in a newly created slot.
      // Reuse that slot instead of requesting creation of the same directory.
      const result = await api('/cli-login/status');
      if (version !== generation || !dialog.open || view !== 'result') return;
      status = result;
      selection.new_slot = !result.claude_slots.some(slot => slot.name === selection.slot);
      busy = false; askStart(selection);
    } catch (error) {
      if (version === generation && dialog.open) showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    } finally {
      if (version === generation && dialog.open) {
        busy = false; $('cliRetry').disabled = false; render();
        if (view === 'list') loadStatus();
      }
    }
  }
  async function cancelJob() {
    const version = generation; busy = true; confirmation.close(); render();
    try {
      await api(`/cli-login/jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: 'POST' });
      if (version !== generation || !dialog.open) return;
      cancelRequested = true; await pollJob();
    } catch (error) { if (version === generation && dialog.open) showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic'); }
    finally { if (version === generation && dialog.open) { busy = false; render(); schedule(); } }
  }
  async function send(body, field = null) {
    if (!active() || busy || cancelRequested) return;
    if (!body.value || !body.value.replace(/[\x00-\x1f\x7f-\x9f]/g, '')) { showNotice('cli_login.invalid_input'); field?.focus(); return; }
    const version = generation; busy = true; renderProgress();
    try {
      const request = api(`/cli-login/jobs/${encodeURIComponent(job.job_id)}/input`, { method: 'POST', body });
      body.value = ''; if (field) field.value = '';
      await request;
      if (version !== generation || !dialog.open) return;
      await pollJob(); if (active()) showNotice('cli_login.sent');
    } catch (error) { if (version === generation && dialog.open) showNotice(error.code === 'invalid_input' ? 'cli_login.invalid_input' : 'error.generic'); }
    finally { if (version === generation && dialog.open) { busy = false; renderProgress(); schedule(); } }
  }
  openModal = () => {
    if (dialog.open) return;
    ++generation; status = null; target = null; job = null; view = 'list'; busy = false; loading = true; noticeKey = null;
    copied = false; cancelRequested = false; seq = 0; statusChecksLeft = 60; refreshChecksLeft = 0; refreshingProvider = null; $('cliRaw').open = false;
    dialog.showModal(); render(); $('cliClose').focus(); loadStatus();
  };
  const focusAfterReturn = () => {
    if (!returnToCode || document.hidden) return;
    codeFocusPending = true; focusCodeInput();
  };
  window.addEventListener('focus', focusAfterReturn);
  document.addEventListener('visibilitychange', focusAfterReturn);
  dialog.addEventListener('pointerdown', event => {
    if (!event.target.closest('#cliLinkRows, #cliCodeForm')) {
      codeFocusPending = false; returnToCode = false;
    }
  });
  $('cliSlotName').addEventListener('input', () => { $('cliSlotPreview').textContent = t('cli_login.slot_preview', { name: slotName($('cliSlotName').value) }); });
  $('cliSlotForm').addEventListener('submit', event => {
    event.preventDefault();
    const name = $('cliSlotName').value, canonical = slotName(name);
    const key = !name || !/^claude(?:-[a-z0-9][a-z0-9_-]*)?$/.test(canonical) || canonical === 'claude-slots'
      ? 'cli_login.invalid_slot' : status?.claude_slots?.some(slot => slot.name === canonical) ? 'cli_login.duplicate_slot' : null;
    $('cliSlotError').hidden = !key; $('cliSlotName').setAttribute('aria-invalid', String(Boolean(key)));
    if (key) { $('cliSlotError').dataset.key = key; $('cliSlotError').textContent = t(key); $('cliSlotName').focus(); return; }
    askStart({ provider: 'claude', slot: canonical, new_slot: true });
  });
  $('cliCodeForm').addEventListener('submit', event => { event.preventDefault(); send({ kind: 'line', value: $('cliCodeInput').value }, $('cliCodeInput')); });
  $('cliRawForm').addEventListener('submit', event => { event.preventDefault(); send({ kind: 'line', value: $('cliRawInput').value }, $('cliRawInput')); });
  $('cliCopy').addEventListener('click', async () => {
    if (!job?.code) return;
    try { await navigator.clipboard.writeText(job.code); copied = true; $('cliCopy').textContent = t('cli_login.copied'); }
    catch { const range = document.createRange(); range.selectNodeContents($('cliDeviceCode')); getSelection().removeAllRanges(); getSelection().addRange(range); }
  });
  $('cliCancel').addEventListener('click', askCancel);
  $('cliConfirmNo').addEventListener('click', () => { if (!busy) confirmation.close(); });
  $('cliConfirmYes').addEventListener('click', () => { if (!busy) { if (confirmMode === 'cancel') cancelJob(); else startJob(); } });
  $('cliRetry').addEventListener('click', retryJob);
  dialog.querySelectorAll('[data-cli-list]').forEach(control => control.addEventListener('click', () => { stopPolling(); view = 'list'; job = null; showNotice(null); render(); loadStatus(); }));
  function closeDialog() { if (active()) askCancel(); else dialog.close(); }
  $('cliClose').addEventListener('click', closeDialog);
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
  confirmation.addEventListener('cancel', event => { event.preventDefault(); if (!busy) confirmation.close(); });
  [dialog, confirmation].forEach(modal => modal.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...modal.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],summary,[tabindex="0"]')].filter(element => element.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }));
  dialog.addEventListener('close', () => {
    ++generation; stopPolling(); confirmation.close(); accountsKey = null; linksKey = null;
    codeFocusPending = false; returnToCode = false; $('cliSlotForm').reset(); $('cliCodeForm').reset(); $('cliRawForm').reset();
    $('cliRawOutput').textContent = ''; job = null; target = null; status = null; $('accountButton').focus();
  });
  document.addEventListener('language-changed', () => { if (dialog.open) render(); });
  document.addEventListener('auth-change', event => { if (!event.detail.authenticated) { refreshingProvider = null; confirmation.close(); dialog.close(); stopPolling(); } });
}
