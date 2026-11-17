import { api } from './api.js';
import { t } from './i18n.js';

export function initSecret() {
  const dialog = document.querySelector('#secretDlg');
  const form = document.querySelector('#secretForm');
  const field = document.querySelector('#secretValue');
  const message = document.querySelector('#secretMessage');
  const notice = document.querySelector('#secretError');
  const countdown = document.querySelector('#secretRemaining');
  const submit = document.querySelector('#secretSubmit');
  const confirm = document.querySelector('#secretConfirm');
  const cancel = document.querySelector('#secretCancel');
  const retry = document.querySelector('#secretRetry');
  const done = document.querySelector('#secretDone');
  let current = null, tid = null, operation = 'set', timer = null, opener = null;
  let authenticated = false, busy = false, version = 0;
  const pending = () => current && ['value', 'confirm_overwrite', 'confirm_delete'].includes(current.stage);

  function clearValue() { field.value = ''; }
  function cleanup() {
    clearValue(); clearTimeout(timer); timer = null;
    current = null; tid = null; busy = false; ++version;
  }
  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    [field, submit, confirm, cancel, retry].forEach(control => { control.disabled = value; });
    submit.textContent = t(value ? 'secret.busy' : 'chat.send');
  }
  function tick() {
    clearTimeout(timer); timer = null;
    if (!pending() || !Number.isFinite(current.deadline)) { countdown.hidden = true; return; }
    const seconds = Math.max(0, Math.ceil((current.deadline - performance.now()) / 1000));
    countdown.textContent = t('secret.remaining', { seconds }); countdown.hidden = false;
    if (!seconds && !busy) {
      apply({ text: t('secret.expired'), secret: { stage: 'expired' } });
      return;
    }
    if (seconds) timer = setTimeout(tick, 1000);
  }
  function apply(result) {
    clearValue(); clearTimeout(timer);
    const meta = result.secret;
    current = { stage: meta.stage, key: meta.key || current?.key || meta.params?.key,
      token: meta.token, deadline: performance.now() + meta.expires_in * 1000 };
    const valueStage = current.stage === 'value';
    const confirmation = ['confirm_overwrite', 'confirm_delete'].includes(current.stage);
    const modal = ['secret.prompt', 'secret.extra_args'].includes(meta.message_key);
    message.textContent = valueStage ? t('secret.prompt', { key: current.key })
      : modal ? t(meta.message_key, meta.params) : result.text || '';
    notice.textContent = valueStage ? result.text || '' : '';
    notice.hidden = !notice.textContent;
    field.removeAttribute('aria-invalid');
    if (notice.textContent) field.setAttribute('aria-invalid', 'true');
    form.hidden = !valueStage; submit.hidden = !valueStage;
    confirm.hidden = !confirmation;
    confirm.textContent = t(current.stage === 'confirm_delete' ? 'common.delete' : 'secret.btn_overwrite');
    confirm.classList.toggle('btn-danger', current.stage === 'confirm_delete');
    cancel.hidden = !(valueStage || confirmation);
    retry.hidden = !(['expired', 'error'].includes(current.stage) && current.key);
    done.hidden = valueStage || confirmation;
    tick();
    if (valueStage) field.focus();
    else if (confirmation) cancel.focus();
    else message.focus();
  }
  function failure(error) {
    clearValue(); clearTimeout(timer);
    const key = error.status === 403 ? 'secret.forbidden'
      : error.code === 'wrong_stage' ? 'secret.wrong_stage'
      : error.status === 409 ? 'secret.expired'
      : error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic';
    apply({ text: '', secret: { stage: key === 'secret.expired' ? 'expired' : 'error' } });
    notice.textContent = t(key); notice.hidden = false; notice.focus();
  }
  async function request(path, body) {
    if (busy) return;
    const generation = version;
    clearValue(); setBusy(true);
    try {
      // api serializes synchronously before awaiting fetch. Clear transient
      // request references immediately; values never enter modal state or drafts.
      const response = api(path, { method: 'POST', body });
      if (Object.hasOwn(body, 'value')) body.value = '';
      if (Object.hasOwn(body, 'token')) body.token = '';
      const result = await response;
      if (!authenticated || generation !== version || !dialog.open) return;
      apply(result);
    } catch (error) {
      if (authenticated && generation === version && dialog.open) failure(error);
    } finally {
      if (generation === version) {
        clearValue(); setBusy(false); tick();
        if (dialog.open && current?.stage === 'value') field.focus();
      }
    }
  }
  function command(text) {
    return request(`/topics/${encodeURIComponent(tid)}/messages`, { text, client_id: crypto.randomUUID() });
  }
  function confirmChoice(value) {
    if (!current?.token || busy) return;
    const body = { token: current.token, confirm: value };
    current.token = null;
    return request(`/topics/${encodeURIComponent(tid)}/secret/confirm`, body);
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (busy || current?.stage !== 'value' || !current.token) return;
    if (current.deadline <= performance.now()) { tick(); return; }
    const body = { token: current.token, value: field.value };
    current.token = null;
    void request(`/topics/${encodeURIComponent(tid)}/secret/value`, body);
  });
  confirm.addEventListener('click', () => { void confirmChoice(true); });
  cancel.addEventListener('click', () => {
    if (busy) return;
    if (current?.stage === 'value') void command('/secret cancel');
    else void confirmChoice(false);
  });
  retry.addEventListener('click', () => {
    if (current?.key && !busy) void command(`/secret ${operation} ${current.key}`);
  });
  done.addEventListener('click', () => dialog.close());
  document.querySelector('#secretClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { event.preventDefault(); dialog.close(); });
  dialog.addEventListener('close', () => {
    // Closing by Esc, navigation or the close button also cancels the server flow.
    if (authenticated && pending()) {
      const confirmation = current.stage !== 'value' && current.token;
      const path = `/topics/${encodeURIComponent(tid)}/${confirmation ? 'secret/confirm' : 'messages'}`;
      const body = confirmation ? { token: current.token, confirm: false }
        : { text: '/secret cancel', client_id: crypto.randomUUID() };
      void api(path, { method: 'POST', body }).catch(() => {});
      if (confirmation) body.token = '';
    }
    cleanup();
    if (authenticated && opener?.isConnected !== false) opener?.focus();
    opener = null;
  });
  document.addEventListener('topic-selected', () => { if (dialog.open) dialog.close(); });
  document.addEventListener('auth-change', event => {
    authenticated = event.detail.authenticated;
    if (!authenticated) { if (dialog.open) dialog.close(); else cleanup(); }
  });
  return {
    show(result, topic) {
      if (!authenticated || !result.secret) return;
      cleanup(); tid = topic;
      operation = result.secret.stage === 'confirm_delete' ? 'delete' : 'set';
      opener = document.activeElement;
      if (!dialog.open) dialog.showModal();
      setBusy(false); apply(result);
    },
  };
}
