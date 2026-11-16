import { api, setCsrf } from './api.js';
import { t } from './i18n.js';
import { beginCredentialChange, finishCredentialChange } from './ws.js';

export function initAccount() {
  const button = document.querySelector('#accountButton');
  const menu = document.querySelector('#accountMenu');
  const dialog = document.querySelector('#pwDialog');
  const form = document.querySelector('#changeForm');
  const notice = document.querySelector('#pwNotice');
  const submit = document.querySelector('#pwSubmit');
  const current = document.querySelector('#currentPw');
  const next = document.querySelector('#newPw');
  const confirmation = document.querySelector('#confirmPw');
  const code = document.querySelector('#currentTotp');
  const inputs = [...form.querySelectorAll('input')];
  let busy = false, lockedUntil = 0, unlockTimer = null;

  function closeMenu() { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); }
  function clearInvalid() {
    inputs.forEach(input => { input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby'); });
  }
  function showNotice(key, invalid = [], values = {}, success = false) {
    clearInvalid();
    notice.textContent = t(key, values);
    notice.className = `pw-notice ${success ? 'success' : 'error'}`;
    notice.hidden = false;
    invalid.forEach(input => { input.setAttribute('aria-invalid', 'true'); input.setAttribute('aria-describedby', 'pwNotice'); });
    invalid[0]?.focus();
  }
  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    inputs.forEach(input => { input.disabled = value || Date.now() < lockedUntil; });
    submit.disabled = value || Date.now() < lockedUntil;
    submit.textContent = t(value ? 'account.busy' : 'account.change');
  }
  button.addEventListener('click', () => {
    menu.hidden = !menu.hidden;
    button.setAttribute('aria-expanded', String(!menu.hidden));
    if (!menu.hidden) menu.querySelector('button').focus();
  });
  document.addEventListener('click', event => { if (!event.target.closest('.account-menu')) closeMenu(); });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closeMenu(); button.focus(); }
  });
  document.querySelector('#openPw').addEventListener('click', () => {
    closeMenu();
    if (busy) return;
    form.reset(); form.hidden = false; notice.hidden = true; clearInvalid();
    document.querySelector('#pwGuide').hidden = false;
    setBusy(false);
    if (Date.now() < lockedUntil) {
      showNotice('account.locked', [], { seconds: Math.ceil((lockedUntil - Date.now()) / 1000) });
    }
    dialog.showModal();
    if (!current.disabled) current.focus();
  });
  document.querySelector('#closePw').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { form.reset(); button.focus(); });
  document.addEventListener('auth-change', event => {
    if (!event.detail.authenticated) { closeMenu(); dialog.close(); form.reset(); }
  });
  document.addEventListener('route-change', closeMenu);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || Date.now() < lockedUntil) return;
    clearInvalid(); notice.hidden = true;
    if (next.value.length < 12) { showNotice('account.password_too_short', [next]); return; }
    if (next.value !== confirmation.value) { showNotice('account.mismatch', [confirmation]); return; }
    if (!form.reportValidity()) return;
    const body = { current_password: current.value, new_password: next.value, code: code.value };
    setBusy(true);
    beginCredentialChange();
    try {
      const result = await api('/auth/password-change', { method: 'POST', body });
      setCsrf(result.csrf);
      finishCredentialChange(true);
      form.reset(); form.hidden = true;
      document.querySelector('#pwGuide').hidden = true;
      showNotice('account.success', [], {}, true);
    } catch (error) {
      finishCredentialChange(false);
      const keys = {
        invalid: 'account.invalid', password_too_short: 'account.password_too_short',
        code_reused: 'account.code_reused', locked: 'account.locked',
        totp_required: 'account.totp_required', bot_unreachable: 'error.bot_unreachable',
      };
      const invalid = error.code === 'invalid' ? [current, code]
        : error.code === 'password_too_short' ? [next] : error.code === 'code_reused' ? [code] : [];
      code.value = '';
      if (error.code === 'locked') {
        const seconds = Math.max(1, Math.ceil(error.retryAfter || 0));
        lockedUntil = Date.now() + seconds * 1000;
        clearTimeout(unlockTimer);
        unlockTimer = setTimeout(() => { lockedUntil = 0; setBusy(busy); notice.hidden = true; }, seconds * 1000);
      }
      setBusy(false);
      showNotice(keys[error.code] || 'error.generic', invalid, { seconds: Math.max(1, Math.ceil(error.retryAfter || 0)) });
    } finally { setBusy(false); }
  });
}
