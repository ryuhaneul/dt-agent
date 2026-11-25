import { api } from './api.js';
import { t, translate } from './i18n.js';

const providers = ['claude', 'codex', 'grok'];
const terminal = ['succeeded', 'failed', 'cancelled', 'timed_out'];
let openModal = () => {};
export function openCliLogin() { openModal(); }

export function initCliLogin() {
  const $ = id => document.getElementById(id);
  const dialog = $('cliLoginDlg'), confirmation = $('cliConfirmDlg');
  const cliBody = dialog.querySelector('.cli-body');
  const rawOutput = document.createTextNode('');
  $('cliRawOutput').append(rawOutput);
  let view = 'list', status = null, target = null, job = null, seq = 0;
  let timer = null, generation = 0, busy = false, loading = false, polling = false, confirmMode = 'start';
  let noticeKey = null, cancelNoticeKey = null, copied = false, cancelRequested = false, refreshingProvider = null;
  let accountsKey = null, linksKey = null, slotError = null, slotMenu = null, confirmReturn = null;
  let codeFocusPending = false, returnToCode = false;
  let loginTab = null, previousLoginHref = null;
  // Probes can take 10s (Claude) + 15s (Grok), plus a queued completion probe.
  // Bound unknown-status polling; two snapshots skip the first cached response.
  let statusChecksLeft = 60, refreshChecksLeft = 0;
  const active = () => dialog.open && view === 'progress' && job && !terminal.includes(job.state);
  const slotName = name => ['', 'default', 'claude'].includes(name) ? 'claude'
    : name.startsWith('claude-') ? name : `claude-${name}`;
  const targetName = () => target?.provider === 'claude' && target.slot
    ? `claude · ${target.slot === 'claude' ? t('cli_login.default_slot') : target.slot}` : target?.provider || '';
  function stopPolling() { clearTimeout(timer); timer = null; }
  function closeLoginTab() { loginTab?.close(); loginTab = null; previousLoginHref = null; }
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
    const element = node('span', `badge ${['expired', 'slot_in_use'].includes(state) ? 'badge-warn' : (!state || state === 'key_connected') && authenticated ? 'badge-ok' : 'badge-off'}`, t(key));
    element.dataset.i18n = key; return element;
  }
  function closeSlotMenu(restoreFocus = false) {
    if (!slotMenu) return;
    const { menu, trigger } = slotMenu;
    slotMenu = null;
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus || menu.contains(document.activeElement)) trigger.focus({ preventScroll: true });
    menu.hidePopover();
    schedule();
  }
  function positionSlotMenu() {
    if (!slotMenu) return;
    const { menu, trigger } = slotMenu;
    const anchor = trigger.getBoundingClientRect(), body = dialog.querySelector('.cli-body').getBoundingClientRect();
    if (anchor.top < body.top || anchor.bottom > body.bottom || !trigger.getClientRects().length) {
      closeSlotMenu();
      return;
    }
    const { width, height } = menu.getBoundingClientRect();
    const below = anchor.bottom + 6, above = anchor.top - height - 6;
    menu.style.left = `${Math.max(12, Math.min(anchor.right - width, innerWidth - width - 12))}px`;
    menu.style.top = `${below + height <= innerHeight - 12 ? below : Math.max(12, above)}px`;
  }
  function renderSlotMenu(row, slot, provider) {
    const name = slot.name;
    const trigger = node('button', 'theme-btn cli-slot-more');
    trigger.type = 'button';
    trigger.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="6" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="18" cy="12" r="1.8"/></svg>';
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    const menu = node('div', 'menu cli-slot-menu');
    menu.id = `cli-slot-menu-${name}`;
    menu.setAttribute('popover', 'auto');
    menu.setAttribute('role', 'menu');
    trigger.setAttribute('aria-controls', menu.id);
    trigger.setAttribute('popovertarget', menu.id);
    const label = t('cli_login.slot_more', { name });
    trigger.setAttribute('aria-label', label);
    menu.setAttribute('aria-label', label);

    function add(key, openConfirmation, options = {}) {
      const action = button(`cli_login.${key}`, () => {
        closeSlotMenu(true);
        confirmReturn = trigger;
        openConfirmation();
      });
      action.className = options.remove ? 'btn-danger' : '';
      action.setAttribute('role', 'menuitem');
      action.setAttribute('aria-haspopup', 'dialog');
      if (options.mutation) {
        action.dataset.slotMutation = '';
        action.disabled = busy || loading;
      }
      if (options.remove) {
        action.dataset.delete = name;
        const label = t('cli_login.delete_label', { name });
        action.setAttribute('aria-label', label);
        action.title = label;
      }
      menu.append(action);
    }
    if (slot.kind === 'api_key') {
      add('disconnect', () => askSlotChange('disconnect', name), { mutation: true });
    } else if (slot.kind !== 'api_key_detached') {
      add(slot.authenticated ? 'relogin' : 'login', () => askStart({ provider, slot: name, new_slot: false }));
      if (slot.authenticated) add('logout', () => askSlotChange('logout', name), { mutation: true });
    }
    if (name !== 'claude') add('delete', () => askSlotChange('delete', name), { mutation: true, remove: true });
    trigger.disabled = busy || loading || !menu.children.length || [...menu.children].every(item => item.disabled);
    menu.addEventListener('beforetoggle', event => {
      if (event.newState !== 'closed') return;
      trigger.setAttribute('aria-expanded', 'false');
      if (slotMenu?.menu === menu) { slotMenu = null; schedule(); }
    });
    menu.addEventListener('toggle', () => {
      if (!menu.matches(':popover-open')) return;
      slotMenu = { menu, trigger };
      trigger.setAttribute('aria-expanded', 'true');
      positionSlotMenu();
      if (slotMenu?.menu === menu) menu.querySelector('button:not(:disabled)')?.focus();
    });
    trigger.addEventListener('keydown', event => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key) || menu.matches(':popover-open')) return;
      event.preventDefault();
      menu.showPopover();
    });
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); closeSlotMenu(true);
        return;
      }
      if (!['Tab', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      const items = [...menu.querySelectorAll('button:not(:disabled)')];
      if (!items.length) return;
      const index = items.indexOf(document.activeElement);
      const backward = event.key === 'ArrowUp' || (event.key === 'Tab' && event.shiftKey);
      const next = event.key === 'Home' ? items[0] : event.key === 'End' ? items.at(-1)
        : items[(index + (backward ? -1 : 1) + items.length) % items.length];
      event.preventDefault(); event.stopPropagation(); next?.focus();
    });
    row.append(trigger, menu);
  }
  function renderAccounts() {
    const root = $('cliProviders');
    const key = JSON.stringify([status?.providers, status?.claude_slots, slotError]);
    if (key === accountsKey) {
      root.querySelectorAll('button').forEach(element => {
        const menu = element.classList.contains('cli-slot-more')
          ? document.getElementById(element.getAttribute('aria-controls')) : null;
        const canRun = menu
          ? [...menu.children].some(item => !status?.running || Object.hasOwn(item.dataset, 'slotMutation'))
          : !status?.running || Object.hasOwn(element.dataset, 'slotMutation');
        element.disabled = busy || loading || !canRun;
        if (menu) {
          const label = t('cli_login.slot_more', { name: element.closest('[data-slot]').dataset.slot });
          element.setAttribute('aria-label', label); menu.setAttribute('aria-label', label);
        }
        if (element.dataset.delete) {
          const label = t('cli_login.delete_label', { name: element.dataset.delete });
          element.setAttribute('aria-label', label); element.title = label;
        }
      });
      return;
    }
    closeSlotMenu(); accountsKey = key; root.replaceChildren();
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
          const row = node('div', 'cli-slot'); row.dataset.slot = slot.name;
          const error = slotError?.name === slot.name ? slotError : null;
          const name = node('span', 'cli-slot-name mono', slot.name === 'claude' ? t('cli_login.default_slot') : slot.name);
          if (slot.name === 'claude') name.dataset.i18n = 'cli_login.default_slot';
          const state = error?.busy ? 'slot_in_use'
            : slot.kind === 'api_key_detached' ? 'key_disconnected'
            : slot.kind === 'api_key' ? 'key_connected'
            : !slot.authenticated ? 'logged_out'
            : slot.priority == null ? 'unused' : slot.auth_expired ? 'expired' : null;
          row.append(name, badge(slot.authenticated, state));
          renderSlotMenu(row, slot, provider);
          slots.append(row);
          if (slot.kind === 'api_key_detached') {
            const hint = node('p', 'hint cli-slot-reconnect', t('cli_login.key_reconnect'));
            hint.dataset.i18n = 'cli_login.key_reconnect'; slots.append(hint);
          }
          if (error) {
            const message = node('p', 'cli-error slot-error', t(error.key, error.args));
            message.dataset.i18n = error.key;
            message.dataset.i18nArgs = JSON.stringify(error.args);
            message.setAttribute('role', 'alert');
            slots.append(message);
          }
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
    const start = confirmMode === 'start', cancel = confirmMode === 'cancel';
    const prefix = start ? 'confirm' : confirmMode;
    $('cliConfirmTitle').textContent = t(`cli_login.${prefix}_title`);
    $('cliConfirmTarget').textContent = start || cancel ? targetName() : target?.slot || '';
    const provider = target?.provider, providerStatus = status?.providers?.[provider];
    const loggedOut = start && (target?.slot
      ? status?.claude_slots?.find(slot => slot.name === target.slot)?.authenticated === false
      : refreshingProvider !== provider && ['installed', 'not_found'].includes(providerStatus));
    let warning = `cli_login.${prefix}_warning`;
    if (start && target?.new_slot === true) warning = 'cli_login.confirm_new_slot';
    else if (loggedOut) warning = 'cli_login.confirm_login';
    $('cliConfirmWarning').textContent = t(warning)
      + (start && provider === 'codex' && !loggedOut ? ` ${t('cli_login.confirm_codex')}` : '');
    $('cliConfirmNo').textContent = t(cancel ? 'cli_login.keep_login' : 'common.cancel');
    $('cliConfirmYes').textContent = t(`cli_login.${cancel ? 'confirm_cancel' : confirmMode}`);
    $('cliConfirmYes').classList.toggle('btn-danger', !start);
    $('cliConfirmYes').classList.toggle('btn-primary', start);
    $('cliConfirmYes').disabled = busy;
    $('cliConfirmNo').disabled = busy && !start && !cancel;
  }
  function focusCodeInput() {
    const input = $('cliCodeInput'), focused = document.activeElement;
    if (!codeFocusPending || !active() || !job.needs_input || input.disabled
      || confirmation.open || document.hidden) return;
    const selected = window.getSelection();
    if (selected && !selected.isCollapsed && dialog.contains(selected.anchorNode)) return;
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
          closeLoginTab();
          returnToCode = true; codeFocusPending = true; focusCodeInput();
        });
        const text = node('span', 'cli-url mono', url.href);
        const copy = node('button', 'btn', t('cli_login.copy_link'));
        copy.type = 'button'; copy.dataset.i18n = 'cli_login.copy_link';
        let copyTimer;
        copy.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(url.href);
            copy.dataset.i18n = 'common.copied'; copy.textContent = t('common.copied');
            clearTimeout(copyTimer);
            copyTimer = setTimeout(() => {
              copy.dataset.i18n = 'cli_login.copy_link'; copy.textContent = t('cli_login.copy_link');
            }, 2000);
          } catch {
            if (!copy.isConnected || !dialog.open || view !== 'progress') return;
            const selection = window.getSelection(), range = document.createRange();
            range.selectNodeContents(text); selection.removeAllRanges(); selection.addRange(range);
          }
        });
        row.append(text, link, copy); links.append(row);
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
    showNotice(noticeKey);
    if (slotError && !status?.claude_slots?.some(slot => slot.name === slotError.name) && ['list', 'busy'].includes(view)) {
      $('cliNotice').textContent = t(slotError.key, slotError.args); $('cliNotice').hidden = false;
    }
    renderConfirmation();
    if (!$('cliSlotError').hidden) $('cliSlotError').textContent = t($('cliSlotError').dataset.key);
  }
  function schedule() {
    stopPolling();
    if (active()) timer = setTimeout(pollJob, 1000);
    else if (dialog.open && !confirmation.open && !slotMenu && ['list', 'result'].includes(view) && !busy && statusChecksLeft > 0 && (refreshChecksLeft || Object.values(status?.providers || {}).some(value => value === null))) timer = setTimeout(loadStatus, 1000);
  }
  async function loadStatus() {
    if (!dialog.open || confirmation.open || slotMenu || !['list', 'result'].includes(view) || busy || statusChecksLeft <= 0) return;
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
        cancelNoticeKey = null;
        seq = 0; rawOutput.data = ''; job = { job_id: result.job_id, state: 'running' };
        target = { provider: result.provider }; view = 'progress'; await pollJob(); return;
      }
      view = result.running ? 'busy' : 'list'; showNotice(cancelNoticeKey);
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    } finally {
      if (version === generation && dialog.open) {
        loading = false; render();
        if (view === 'busy') cliBody.scrollTop = 0;
        schedule();
      }
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
      (result.output || []).forEach(chunk => { if (chunk.seq > seq) rawOutput.appendData(chunk.text); });
      if (rawOutput.length > 65536) rawOutput.deleteData(0, rawOutput.length - 65536);
      seq = result.seq; job = result; target = { ...target, provider: result.provider, slot: result.slot };
      showNotice(null);
      if (terminal.includes(result.state)) {
        const cancelledHere = result.state === 'cancelled' && cancelRequested;
        closeLoginTab();
        stopPolling(); view = cancelledHere ? 'list' : 'result';
        if (cancelledHere) {
          job = null; cancelRequested = false;
          cancelNoticeKey = result.provider === 'codex' ? 'cli_login.cancelled_codex' : null;
          showNotice(cancelNoticeKey);
        }
        refreshingProvider = result.provider; refreshChecksLeft = 2; statusChecksLeft = 60;
        render();
        if (cancelledHere) {
          cliBody.scrollTop = 0;
          $('cliClose').focus();
        }
        loadStatus();
        return;
      }
      render();
      // A PTY read can end mid-URL. Wait for the CLI's prompt or two matching polls.
      const href = loginTab ? $('cliLinkRows').querySelector('a[href]')?.href : null;
      const prompted = job.code || job.needs_input;
      const ready = href && (prompted || href === previousLoginHref);
      previousLoginHref = href;
      if (ready) {
        const tab = loginTab;
        try {
          if (!tab.closed) {
            tab.location.replace(href);
            returnToCode = true;
          }
          loginTab = null;
        } catch {
          // The visible link remains usable when the browser denies navigation.
          closeLoginTab();
        }
      } else if (prompted && !href) closeLoginTab();
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      previousLoginHref = null;
      if (error.status === 404) { job = { ...job, state: 'failed' }; view = 'result'; stopPolling(); closeLoginTab(); }
      showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic'); render();
    } finally { polling = false; if (dialog.open) schedule(); }
  }
  function askStart(selection) {
    if (busy) return;
    target = selection; confirmMode = 'start'; renderConfirmation(); confirmation.showModal(); $('cliConfirmNo').focus();
  }
  function askSlotChange(action, name) {
    if (busy || loading) return;
    target = { slot: name }; confirmMode = action; renderConfirmation();
    confirmation.showModal(); $('cliConfirmNo').focus();
  }
  async function mutateSlot() {
    const version = generation, name = target.slot, action = confirmMode;
    const deleting = action === 'delete', currentSlots = status.claude_slots;
    const index = currentSlots.findIndex(slot => slot.name === name);
    const live = () => version === generation && dialog.open;
    const route = `/cli-login/slots/${encodeURIComponent(name)}`;
    let focusSlot = name;
    busy = true; render();
    try {
      await api(deleting ? route : `${route}/${action}`, { method: deleting ? 'DELETE' : 'POST' });
      if (!live()) return;
      slotError = null;
      const slots = deleting ? currentSlots.filter(slot => slot.name !== name)
        : currentSlots.map(slot => slot.name !== name ? slot : {
          ...slot, authenticated: false, auth_expired: true,
          kind: action === 'disconnect' ? 'api_key_detached' : slot.kind,
        });
      status = { ...status, claude_slots: slots };
      if (deleting) focusSlot = currentSlots[index + 1]?.name;
    } catch (error) {
      if (!live()) return;
      const inUse = error.code === 'slot_busy';
      let key = `cli_login.${action}_${inUse ? 'busy' : 'error'}`, args = { name };
      if (error.code === 'slot_not_found') key = 'cli_login.delete_missing';
      if (deleting && error.code === 'slot_delete_failed') {
        key = 'cli_login.delete_failed';
        args = { path: error.details?.path || name, reason: error.details?.reason || '' };
        status = { ...status, claude_slots: currentSlots.map(slot => slot.name === name ? { ...slot, priority: null } : slot) };
      }
      slotError = { name, key, args, busy: inUse };
    }
    try {
      const result = await api('/cli-login/status');
      if (live()) status = result;
    } catch (error) {
      if (live() && !slotError) showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
    } finally {
      if (live()) {
        busy = false; confirmReturn = null;
        confirmation.close(); render();
        const next = [...$('cliProviders').querySelectorAll('[data-slot]')].find(row => row.dataset.slot === focusSlot);
        (next?.querySelector('.cli-slot-more:not(:disabled)') || $('cliProviders').querySelector('.cli-provider-head button:not(:disabled)') || $('cliClose')).focus();
        schedule();
      }
    }
  }
  function askCancel() {
    if (!active() || busy || cancelRequested) return;
    confirmMode = 'cancel'; renderConfirmation(); confirmation.showModal(); $('cliConfirmNo').focus();
  }
  async function startJob() {
    if (cancelNoticeKey) { cancelNoticeKey = null; showNotice(null); }
    const version = generation; busy = true; confirmReturn = null; renderConfirmation();
    try {
      const result = await api('/cli-login/jobs', { method: 'POST', body: target });
      if (version !== generation || !dialog.open) return;
      confirmation.close(); copied = false; cancelRequested = false; seq = 0;
      codeFocusPending = false; returnToCode = false;
      rawOutput.data = ''; linksKey = null; $('cliCodeForm').reset(); $('cliRawForm').reset(); $('cliRaw').open = false;
      job = { job_id: result.job_id, state: 'running' }; view = 'progress'; showNotice(null); render();
      await pollJob();
    } catch (error) {
      if (version !== generation || !dialog.open) return;
      closeLoginTab();
      confirmation.close();
      if (error.code === 'login_running') { view = 'busy'; job = null; status = { ...status, running: true }; }
      else { job = { state: 'failed' }; view = 'result'; }
      showNotice(error.code === 'api_key_slot' ? 'cli_login.api_key_slot' : error.code === 'invalid_slot' ? 'cli_login.invalid_slot' : error.code === 'bot_unreachable' ? 'error.bot_unreachable' : null);
    } finally {
      if (version === generation && dialog.open) {
        busy = false; render();
        if (view === 'busy') cliBody.scrollTop = 0;
        schedule();
      }
    }
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
    const version = generation; busy = true; cancelRequested = true; confirmation.close(); render();
    closeLoginTab();
    try {
      await api(`/cli-login/jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: 'POST' });
      if (version !== generation || !dialog.open) return;
      await pollJob();
    } catch (error) {
      if (version === generation && dialog.open) {
        cancelRequested = false;
        showNotice(error.code === 'bot_unreachable' ? 'error.bot_unreachable' : 'error.generic');
      }
    } finally {
      if (version === generation && dialog.open) {
        busy = false; render();
        if (view === 'list') loadStatus();
        else schedule();
      }
    }
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
    ++generation; status = null; target = null; job = null; view = 'list'; busy = false; loading = true; noticeKey = null; cancelNoticeKey = null;
    slotError = null; copied = false; cancelRequested = false; seq = 0; statusChecksLeft = 60; refreshChecksLeft = 0; refreshingProvider = null; $('cliRaw').open = false;
    dialog.showModal(); render(); $('cliClose').focus(); loadStatus();
  };
  const focusAfterReturn = () => {
    if (!returnToCode || document.hidden) return;
    codeFocusPending = true; focusCodeInput();
  };
  window.addEventListener('focus', focusAfterReturn);
  document.addEventListener('visibilitychange', focusAfterReturn);
  dialog.addEventListener('pointerdown', () => {
    codeFocusPending = false; returnToCode = false;
  });
  $('cliSlotName').addEventListener('input', () => { $('cliSlotPreview').textContent = t('cli_login.slot_preview', { name: slotName($('cliSlotName').value) }); });
  $('cliSlotForm').addEventListener('submit', event => {
    event.preventDefault();
    const name = $('cliSlotName').value, canonical = slotName(name);
    const key = !name || !/^claude(?:-[a-z0-9][a-z0-9_-]*)?$/.test(canonical) || canonical === 'claude-slots'
      ? 'cli_login.invalid_slot' : canonical === 'claude-api' ? 'cli_login.api_key_slot' : status?.claude_slots?.some(slot => slot.name === canonical) ? 'cli_login.duplicate_slot' : null;
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
  function dismissConfirmation() {
    if (busy) return;
    confirmation.close(); confirmReturn?.focus(); confirmReturn = null; schedule();
  }
  $('cliConfirmNo').addEventListener('click', dismissConfirmation);
  $('cliConfirmYes').addEventListener('click', () => {
    if (busy) return;
    if (confirmMode === 'cancel') cancelJob();
    else if (confirmMode !== 'start') mutateSlot();
    else {
      // Reserve the tab before the asynchronous job request consumes the gesture.
      previousLoginHref = null;
      loginTab = window.open('about:blank', '_blank');
      if (loginTab) loginTab.opener = null;
      startJob();
    }
  });
  $('cliRetry').addEventListener('click', retryJob);
  dialog.querySelectorAll('[data-cli-list]').forEach(control => control.addEventListener('click', () => { stopPolling(); view = 'list'; job = null; showNotice(null); render(); loadStatus(); }));
  function closeDialog() { if (active()) askCancel(); else dialog.close(); }
  $('cliClose').addEventListener('click', closeDialog);
  dialog.querySelector('.cli-body').addEventListener('scroll', positionSlotMenu);
  window.addEventListener('resize', positionSlotMenu);
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    if (slotMenu) closeSlotMenu(true); else closeDialog();
  });
  confirmation.addEventListener('cancel', event => { event.preventDefault(); dismissConfirmation(); });
  [dialog, confirmation].forEach(modal => modal.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...modal.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],summary,[tabindex="0"]')].filter(element => element.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }));
  dialog.addEventListener('close', () => {
    ++generation; closeSlotMenu(); stopPolling(); closeLoginTab(); confirmation.close(); confirmReturn = null; accountsKey = null; linksKey = null;
    codeFocusPending = false; returnToCode = false; $('cliSlotForm').reset(); $('cliCodeForm').reset(); $('cliRawForm').reset();
    rawOutput.data = ''; job = null; target = null; status = null; $('accountButton').focus();
  });
  document.addEventListener('language-changed', () => { if (dialog.open) render(); });
  document.addEventListener('auth-change', event => { if (!event.detail.authenticated) { refreshingProvider = null; confirmation.close(); dialog.close(); stopPolling(); } });
}
