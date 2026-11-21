import { t } from './i18n.js';
import { esc } from './md.js';

const REFRESH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 3v5h5M4 13a8 8 0 0 0 14.6 4.5L20 16M20 21v-5h-5"/></svg>';
const MORE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const TOGGLE = 'aria-haspopup="dialog" aria-controls="claudeSlotPopover" aria-expanded="false"';
const REFRESH = `<button class="slot-refresh" type="button" data-slot-refresh>${REFRESH_ICON}</button>`;
const WINDOWS = ['five_hour', 'seven_day'];

function formatTime(seconds, withDay = false) {
  const date = new Date(seconds * 1000);
  const options = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
  if (withDay && date.toDateString() !== new Date().toDateString()) {
    Object.assign(options, { month: 'numeric', day: 'numeric' });
  }
  return date.toLocaleString(document.documentElement.lang, options);
}

function slotStatus(slot) {
  const keyLabel = ['api_key', 'api_key_detached'].includes(slot.kind) ? t(`slots.${slot.kind}`) : '';
  if (slot.state === 'cooldown') {
    return { kind: 'cooldown', label: t('slots.resume_at', { time: formatTime(slot.cooldown_until) }), badge: 'warn', keyLabel };
  }
  if (keyLabel) return { kind: 'key', label: keyLabel, badge: 'off' };
  if (slot.state === 'expired') return { kind: 'expired', label: t('slots.expired'), badge: 'down' };
  if (slot.state === 'ok' && WINDOWS.some(name => slot.usage?.windows?.[name])) {
    return { kind: 'ok', label: t('slots.ok'), badge: 'ok' };
  }
  return { kind: 'empty', label: t('slots.no_data'), badge: 'off' };
}

function meter(value, className, showPercent = true) {
  const percent = new Intl.NumberFormat(document.documentElement.lang, { style: 'percent' }).format(value);
  const level = value >= 0.9 ? 'high' : value >= 0.75 ? 'mid' : 'low';
  return `<progress class="${className}" data-level="${level}" max="1" value="${Math.min(value, 1)}" aria-hidden="true"></progress>` +
    (showPercent ? `<b class="slot-percent">${esc(percent)}</b>` : '');
}

function chip(slot, status, next, primary) {
  const window = slot.usage?.windows?.five_hour;
  const usage = window && (status.kind === 'ok' || status.kind === 'cooldown')
    ? meter(window.utilization, 'slot-mini', status.kind !== 'cooldown') : '';
  const label = status.kind === 'ok' ? (usage ? '' : t('slots.no_data')) : status.label;
  return `<button class="slot-chip" type="button" data-slot-toggle="${esc(slot.name)}" data-status="${status.kind}" ${next ? 'data-next' : ''} ${primary ? 'data-primary' : ''} ${TOGGLE}>
    ${next ? `<span class="slot-pip" aria-hidden="true"></span><span class="sr-only">${esc(t('slots.next'))}</span>` : ''}
    <span class="slot-name">${esc(slot.name)}</span>${usage}${label ? `<span class="slot-label">${esc(label)}</span>` : ''}
  </button>`;
}

function slotRow(slot, status, next) {
  const usage = slot.usage;
  const now = Date.now() / 1000;
  const rows = WINDOWS.map(name => {
    const window = usage?.windows?.[name];
    if (!window) return '';
    const reset = window.resets_at <= now;
    const resetText = reset ? t('slots.reset_done')
      : window.resets_at === slot.cooldown_until && status.kind === 'cooldown' ? status.label
      : t('slots.reset_at', { time: formatTime(window.resets_at, true) });
    return `<div class="slot-window">
      <span>${esc(t(`slots.${name}`))}</span>${meter(reset ? 0 : window.utilization, 'slot-meter')}
      <span class="slot-reset">${esc(resetText)}</span>
    </div>`;
  }).join('');
  const captured = usage ? t('slots.captured_at', { time: formatTime(usage.captured_at, true) }) : '';
  const label = status.keyLabel ? `${status.keyLabel} · ${status.label}` : status.label;
  return `<div class="slot-row" ${next ? 'data-next' : ''}>
    <div class="slot-row-top"><span class="slot-name">${esc(slot.name)}</span>
      ${next ? `<span class="slot-next-tag">${esc(t('slots.next'))}</span>` : ''}
      <span class="badge badge-${status.badge}">${esc(label)}</span>
    </div>${rows ? `<div class="slot-windows">${rows}</div>` : ''}
    ${captured ? `<div class="slot-time slot-captured">${esc(captured)}</div>` : ''}
  </div>`;
}

export function initSlots(refresh) {
  const root = document.querySelector('#claudeSlots');
  root.setAttribute('role', 'group');
  root.innerHTML = `<div class="slot-chips"></div>${REFRESH}<time class="slot-time"></time>
    <div id="claudeSlotPopover" class="slot-popover" role="dialog" tabindex="-1" hidden>
      <div class="slot-popover-head"><time class="slot-time"></time>${REFRESH}</div>
      <div class="slot-list"></div>
    </div>`;
  const chips = root.querySelector('.slot-chips');
  const popover = root.querySelector('.slot-popover');
  const list = root.querySelector('.slot-list');
  const times = root.querySelectorAll('time');
  const refreshButtons = root.querySelectorAll('[data-slot-refresh]');
  let data = null, updatedAt = null, trigger = null;
  let refreshing = false, failed = false;

  function positionPopover() {
    if (popover.hidden) return;
    const rect = root.getBoundingClientRect();
    const right = Math.min(innerWidth - 12, Math.max(popover.offsetWidth + 12, rect.right));
    popover.style.setProperty('--slot-right', `${rect.right - right}px`);
    popover.style.setProperty('--slot-top', `${Math.round(rect.bottom + 8)}px`);
  }

  function setOpen(open) {
    popover.hidden = !open;
    chips.querySelectorAll('button').forEach(button => button.setAttribute('aria-expanded', String(open)));
    if (open) positionPopover();
  }

  function renderRefresh() {
    const label = t(failed ? 'slots.refresh_failed' : 'slots.refresh');
    refreshButtons.forEach(button => {
      button.disabled = refreshing;
      button.toggleAttribute('data-failed', failed);
      button.setAttribute('aria-busy', String(refreshing));
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }

  function render() {
    root.hidden = !data?.slots?.length;
    if (root.hidden) {
      chips.replaceChildren();
      list.replaceChildren();
      trigger = null;
      setOpen(false);
      return;
    }
    root.setAttribute('aria-label', t('slots.title'));
    popover.setAttribute('aria-label', t('slots.title'));
    const scroll = list.scrollTop;
    const focused = document.activeElement;
    const more = data.slots.length - 1;
    const slots = data.slots.map(slot => ({
      slot, status: slotStatus(slot), next: slot.name === data.next,
    }));
    const primary = slots.find(({ next }) => next) ?? slots[0];
    chips.innerHTML = slots.map(item => chip(item.slot, item.status, item.next, item === primary)).join('') +
      (more > 0 ? `<button class="slot-chip slot-more" type="button" data-slot-more ${TOGGLE}
        aria-label="${esc(t('slots.show_all', { n: data.slots.length }))}">+${more}${MORE_ICON}</button>` : '');
    list.innerHTML = slots.map(({ slot, status, next }) => slotRow(slot, status, next)).join('');
    list.scrollTop = scroll;
    const clock = formatTime(updatedAt.getTime() / 1000);
    const label = t('slots.refreshed_at', { time: clock });
    times.forEach(time => {
      time.dateTime = updatedAt.toISOString();
      time.textContent = time.parentElement === root ? clock : label;
      time.setAttribute('aria-label', label);
    });
    function replacement(button) {
      if (button?.hasAttribute('data-slot-more')) return chips.querySelector('[data-slot-more]');
      return [...chips.querySelectorAll('[data-slot-toggle]')].find(item => item.dataset.slotToggle === button?.dataset.slotToggle);
    }
    trigger = replacement(trigger) || chips.querySelector('[data-primary]');
    replacement(focused)?.focus();
    setOpen(!popover.hidden);
    renderRefresh();
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button?.hasAttribute('data-slot-refresh')) void refresh();
    if (!button?.hasAttribute('aria-controls')) return;
    trigger = button;
    setOpen(popover.hidden);
    if (!popover.hidden) popover.focus({ preventScroll: true });
  });
  document.addEventListener('click', event => {
    if (!popover.hidden && !root.contains(event.target)) setOpen(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || popover.hidden) return;
    event.preventDefault();
    setOpen(false);
    const focus = trigger?.getClientRects().length ? trigger : chips.querySelector('[data-primary]');
    focus?.focus();
  });
  document.addEventListener('language-changed', render);
  window.addEventListener('resize', positionPopover);
  const layout = new ResizeObserver(positionPopover);
  layout.observe(root.closest('.bar'));
  layout.observe(root);

  return {
    update(value) {
      data = value;
      updatedAt = new Date();
      render();
    },
    setRefreshing(value, error = false) {
      refreshing = value;
      failed = error;
      renderRefresh();
    },
  };
}
