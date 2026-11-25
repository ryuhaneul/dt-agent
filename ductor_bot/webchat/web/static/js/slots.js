import { api } from './api.js';
import { t } from './i18n.js';
import { esc } from './md.js';

const REFRESH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 3v5h5M4 13a8 8 0 0 0 14.6 4.5L20 16M20 21v-5h-5"/></svg>';
const MORE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const TOGGLE = 'aria-haspopup="dialog" aria-controls="claudeSlotPopover" aria-expanded="false"';
const REFRESH = `<button class="slot-refresh" type="button" data-slot-refresh>${REFRESH_ICON}</button>`;
const WINDOWS = { five_hour: '5h', seven_day: '7d' };

export function slotOrderControls(name, previous, next, disabled) {
  if (!previous && !next) return '';
  const button = (direction, neighbor, shape) =>
    `<button class="theme-btn slot-order-button" type="button" data-slot-order="${direction}"${neighbor ? ` data-slot-neighbor="${esc(neighbor)}"` : ''}
      aria-label="${esc(t(`slots.move_${direction}`, { name }))}"${disabled || !neighbor ? ' disabled' : ''}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${shape}"/></svg></button>`;
  return `<div class="slot-order-controls">${button('up', previous, 'm6 15 6-6 6 6')}${button('down', next, 'm6 9 6 6 6-6')}</div>`;
}

export function swapSlotOrder(name, neighbor) {
  return api(`/cli-login/slots/${encodeURIComponent(name)}/swap/${encodeURIComponent(neighbor)}`, { method: 'POST' });
}

export function focusSlotOrder(root, name, direction) {
  const row = root.querySelector(`[data-slot="${CSS.escape(name)}"]`);
  const button = row?.querySelector(`[data-slot-order="${direction}"]:not(:disabled)`)
    || row?.querySelector('[data-slot-order]:not(:disabled)');
  if (!button) return false;
  button.focus({ preventScroll: true });
  button.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  return true;
}

function formatTime(seconds, withDay = false) {
  const date = new Date(seconds * 1000);
  const options = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
  if (withDay && date.toDateString() !== new Date().toDateString()) {
    Object.assign(options, { month: 'numeric', day: 'numeric' });
  }
  return date.toLocaleString(document.documentElement.lang, options);
}

function slotStatus(slot) {
  const keyLabel = slot.kind === 'api_key' ? t('slots.api_key') : '';
  if (slot.state === 'cooldown') {
    return { kind: 'cooldown', label: t('slots.resume_at', { time: formatTime(slot.cooldown_until, true) }), badge: 'warn', keyLabel };
  }
  if (keyLabel) return { kind: 'key', label: keyLabel, badge: 'off' };
  if (slot.state === 'expired') return { kind: 'expired', label: t('slots.expired'), badge: 'down' };
  if (slot.state === 'ok' && Object.keys(WINDOWS).some(name => slot.usage?.windows?.[name])) {
    return { kind: 'ok', label: t('slots.ok'), badge: 'ok' };
  }
  return { kind: 'empty', label: t('slots.no_data'), badge: 'off' };
}

function usageLevel(value) {
  return value >= 0.9 ? 'high' : value >= 0.75 ? 'mid' : 'low';
}

function meter(value, className, showPercent = true) {
  const level = usageLevel(value);
  const percent = showPercent
    ? new Intl.NumberFormat(document.documentElement.lang, { style: 'percent' }).format(value) : '';
  return `<progress class="${className}" data-level="${level}" max="1" value="${Math.min(value, 1)}" aria-hidden="true"></progress>` +
    (showPercent ? `<b class="slot-percent" data-level="${level}">${esc(percent)}</b>` : '');
}

function windowHasReset(window, now) {
  return window.resets_at != null && window.resets_at <= now;
}

function windowDuration(minutes) {
  if (minutes == null) return '?';
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

function usageWindows(slot) {
  if (slot.kind === 'codex') {
    return [...slot.usage.windows]
      .sort((a, b) => (a.window_minutes ?? Infinity) - (b.window_minutes ?? Infinity))
      .map(window => {
        const short = windowDuration(window.window_minutes);
        return { short, label: short, utilization: window.used_percent / 100, resets_at: window.resets_at };
      });
  }
  return Object.entries(WINDOWS).flatMap(([name, short]) => {
    const window = slot.usage?.windows?.[name];
    return window ? [{ ...window, short, label: t(`slots.${name}`) }] : [];
  });
}

function chip(slot, windows, status, next, primary, now) {
  const kind = status?.kind ?? (windows.length ? 'ok' : 'empty');
  let usage = '';
  if (kind === 'ok') {
    const rows = windows.map(window => `<span class="slot-chip-window">
      <span class="slot-period" title="${esc(window.label)}">${esc(window.short)}</span>
      ${meter(windowHasReset(window, now) ? 0 : window.utilization, 'slot-mini')}
    </span>`).join('');
    if (rows) usage = `<span class="slot-usage">${rows}</span>`;
  } else if (kind === 'cooldown' && slot.usage?.windows?.five_hour) {
    const window = slot.usage.windows.five_hour;
    usage = meter(windowHasReset(window, now) ? 0 : window.utilization, 'slot-mini', false);
  }
  const label = kind === 'ok' ? '' : status?.label ?? t('slots.no_data');
  return `<button class="slot-chip" type="button" data-slot-toggle="${esc(slot.name)}" data-provider="${slot.kind === 'codex' ? 'codex' : 'claude'}" data-status="${kind}" ${next ? 'data-next' : ''} ${primary ? 'data-primary' : ''} ${TOGGLE}>
    ${next ? `<span class="slot-pip" aria-hidden="true"></span><span class="sr-only">${esc(t('slots.next'))}</span>` : ''}
    <span class="slot-name">${esc(slot.name)}</span>${usage}${label ? `<span class="slot-label">${esc(label)}</span>` : ''}
  </button>`;
}

function slotRow(slot, windows, status, next, now, order) {
  const rows = windows.map(window => {
    const reset = windowHasReset(window, now);
    const resetText = reset ? t('slots.reset_done')
      : window.resets_at == null ? ''
      : window.resets_at === slot.cooldown_until && status?.kind === 'cooldown' ? status.label
      : t('slots.reset_at', { time: formatTime(window.resets_at, true) });
    return `<div class="slot-window">
      <span>${esc(window.label)}</span>${meter(reset ? 0 : window.utilization, 'slot-meter')}
      <span class="slot-reset">${esc(resetText)}</span>
    </div>`;
  }).join('');
  const captured = slot.usage ? t('slots.captured_at', { time: formatTime(slot.usage.captured_at, true) }) : '';
  const label = status?.keyLabel ? `${status.keyLabel} · ${status.label}` : status?.label;
  const info = `<span class="slot-name">${esc(slot.name)}</span>
    ${next ? `<span class="slot-next-tag">${esc(t('slots.next'))}</span>` : ''}
    ${status ? `<span class="badge badge-${status.badge}">${esc(label)}</span>` : ''}`;
  return `<div class="slot-row" data-slot="${esc(slot.name)}" ${next ? 'data-next' : ''}>
    <div class="slot-row-top">${order ? `<div class="slot-row-info">${info}</div>${order}` : info}</div>
    ${rows ? `<div class="slot-windows">${rows}</div>` : ''}
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
  const bar = root.closest('.bar');
  const neighbors = [...bar.children].filter(child => child !== root);
  const narrow = matchMedia('(max-width: 1180px)');
  const mobile = matchMedia('(max-width: 760px)');
  let data = null, codex = null, updatedAt = null, trigger = null;
  let refreshing = false, failed = false, orderRequest = null;

  function chipCapacity() {
    const header = getComputedStyle(bar);
    const available = bar.clientWidth - parseFloat(header.paddingLeft) - parseFloat(header.paddingRight);
    const headerGap = parseFloat(header.columnGap);
    if (mobile.matches) {
      return available - bar.querySelector('.stat').getBoundingClientRect().width - headerGap;
    }
    const controls = narrow.matches ? [] : [refreshButtons[0], times[0]];
    const reserved = [...neighbors, ...controls].reduce((width, element) =>
      width + element.getBoundingClientRect().width, 0);
    return available - neighbors.length * headerGap - reserved
      - controls.length * parseFloat(getComputedStyle(root).columnGap)
      - (narrow.matches ? 0 : parseFloat(getComputedStyle(times[0]).marginLeft));
  }

  function fitChips() {
    const buttons = [...chips.querySelectorAll('[data-slot-toggle]')];
    if (!buttons.length) return;
    const primary = chips.querySelector('[data-primary]');
    const more = chips.querySelector('[data-slot-more]');
    const setHiddenCount = count => {
      if (!more) return;
      more.firstElementChild.textContent = `+${count}`;
      more.hidden = count === 0;
    };

    const isNarrow = narrow.matches;
    // Let the cooldown chip shrink if the wide CSS applies before the next fit.
    primary.toggleAttribute('data-narrow-cooldown', isNarrow && primary.dataset.status === 'cooldown');
    root.removeAttribute('data-compact');
    primary.removeAttribute('data-wrap-cooldown');
    const name = primary.querySelector('.slot-name');
    const usage = primary.querySelector('.slot-usage');
    primary.style.removeProperty('max-width');
    primary.style.removeProperty('min-width');
    primary.style.removeProperty('--slot-cooldown-width');
    // Keep the narrow fold in the DOM when the wide styles first take effect.
    buttons.forEach(button => { button.hidden = isNarrow && button !== primary; });
    setHiddenCount(buttons.length - 1);
    const capacity = chipCapacity();
    const gap = parseFloat(getComputedStyle(chips).columnGap);
    const widthOf = button => button.getBoundingClientRect().width;
    const widthWithoutClippedName = () =>
      widthOf(primary) + Math.max(0, name.scrollWidth - name.clientWidth);
    const widths = () => buttons.map(widthOf);
    const groupWidth = sizes => sizes.reduce((sum, width) => sum + width, 0) + gap * (sizes.length - 1);
    const reserved = more ? gap + widthOf(more) : 0;
    if (usage && (isNarrow || groupWidth(widths()) > capacity)
        && widthWithoutClippedName() + reserved > capacity) {
      root.setAttribute('data-compact', '');
    }
    if (usage) {
      // Keep the meter rows inside the chip while the breakpoint styles catch up.
      const style = getComputedStyle(primary);
      const pip = primary.querySelector('.slot-pip');
      const fixedWidth = widthOf(usage) + parseFloat(style.columnGap) * (pip ? 2 : 1)
        + (pip ? widthOf(pip) : 0) + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
        + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
      primary.style.minWidth = `${Math.ceil(fixedWidth + 1)}px`;
    }
    if (isNarrow) return;

    const limitPrimary = limit => {
      const width = `${Math.max(0, limit)}px`;
      if (primary.dataset.status === 'cooldown') {
        primary.style.setProperty('--slot-cooldown-width', width);
        primary.setAttribute('data-wrap-cooldown', '');
      } else {
        primary.style.maxWidth = width;
      }
    };
    const sizes = widths();
    const fullWidth = groupWidth(sizes);
    if (fullWidth <= capacity || !more) {
      setHiddenCount(0);
      if (fullWidth > capacity) limitPrimary(capacity);
      return;
    }

    let hidden = buttons.length - 1;
    const primaryWidth = sizes[buttons.indexOf(primary)];
    const primaryLimit = capacity - reserved;
    if (primaryWidth > primaryLimit) limitPrimary(primaryLimit);
    let used = primaryWidth;
    // Try the remaining Claude slots in order. Codex is last and folds first.
    buttons.forEach((button, index) => {
      if (button === primary) return;
      setHiddenCount(hidden - 1);
      const required = sizes[index] + gap;
      const reserve = hidden > 1 ? gap + widthOf(more) : 0;
      button.hidden = (button.dataset.provider === 'codex' && hidden > 1)
        || used + required + reserve > capacity;
      if (!button.hidden) {
        hidden--;
        used += required;
      }
    });
    setHiddenCount(hidden);
  }

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
      button.disabled = refreshing || Boolean(orderRequest);
      button.toggleAttribute('data-failed', failed);
      button.setAttribute('aria-busy', String(refreshing));
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }

  function render() {
    const slots = (data?.slots ?? []).map(slot => ({
      slot, windows: usageWindows(slot), status: slotStatus(slot), next: slot.name === data.next,
    }));
    if (codex) {
      const slot = { name: 'Codex', kind: 'codex', usage: codex };
      slots.push({ slot, windows: usageWindows(slot), status: null, next: false });
    }
    root.hidden = slots.length === 0;
    if (root.hidden) {
      chips.replaceChildren();
      list.replaceChildren();
      trigger = null;
      setOpen(false);
      return;
    }
    const title = t(codex ? 'slots.usage_title' : 'slots.title');
    root.setAttribute('aria-label', title);
    popover.setAttribute('aria-label', title);
    const scroll = list.scrollTop;
    const focused = document.activeElement;
    const focusedOrder = list.contains(focused) && focused.dataset.slotOrder;
    const more = slots.length - 1;
    const now = Date.now() / 1000;
    const primary = slots.find(({ next }) => next) ?? slots[0];
    chips.innerHTML = slots.map(item => chip(item.slot, item.windows, item.status, item.next, item === primary, now)).join('') +
      (more > 0 ? `<button class="slot-chip slot-more" type="button" data-slot-more ${TOGGLE}
        aria-label="${esc(t(codex ? 'slots.show_all_usage' : 'slots.show_all', { n: slots.length }))}"><span>+${more}</span>${MORE_ICON}</button>` : '');
    const ranked = data?.slots ?? [];
    list.innerHTML = slots.map(({ slot, windows, status, next }, index) => {
      const order = index < ranked.length
        ? slotOrderControls(slot.name, ranked[index - 1]?.name, ranked[index + 1]?.name, Boolean(orderRequest))
        : '';
      return slotRow(slot, windows, status, next, now, order);
    }).join('');
    list.scrollTop = scroll;
    const clock = formatTime(updatedAt.getTime() / 1000);
    const label = t('slots.refreshed_at', { time: clock });
    times.forEach(time => {
      time.dateTime = updatedAt.toISOString();
      time.textContent = time.parentElement === root ? clock : label;
      time.setAttribute('aria-label', label);
    });
    fitChips();
    function replacement(button) {
      if (button?.hasAttribute('data-slot-more')) return chips.querySelector('[data-slot-more]');
      return [...chips.querySelectorAll('[data-slot-toggle]')].find(item =>
        item.dataset.provider === button?.dataset.provider && item.dataset.slotToggle === button?.dataset.slotToggle);
    }
    trigger = replacement(trigger) || chips.querySelector('[data-primary]');
    if (focusedOrder) {
      focusSlotOrder(popover, focused.closest('[data-slot]').dataset.slot, focusedOrder);
    } else replacement(focused)?.focus();
    setOpen(!popover.hidden);
    renderRefresh();
  }

  function setOrdering(request) {
    orderRequest = request;
    list.querySelectorAll('[data-slot-order]').forEach(button => {
      button.disabled = Boolean(orderRequest) || !button.dataset.slotNeighbor;
    });
    renderRefresh();
  }

  async function reorderSlot(button) {
    const { slotOrder, slotNeighbor } = button.dataset;
    if (orderRequest || !slotNeighbor) return;
    const name = button.closest('[data-slot]').dataset.slot;
    const swap = swapSlotOrder(name, slotNeighbor);
    setOrdering(swap);
    try {
      await swap;
      if (orderRequest !== swap) return;
      // A read started before the swap can still contain the previous order.
      if (refreshing) await refresh();
      if (orderRequest === swap && data) await refresh();
    } catch {
      if (orderRequest === swap) failed = true;
    } finally {
      if (orderRequest === swap) {
        setOrdering(null);
        if (!popover.hidden && !focusSlotOrder(popover, name, slotOrder)) {
          popover.focus({ preventScroll: true });
        }
      }
    }
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button?.hasAttribute('data-slot-order')) void reorderSlot(button);
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
  document.addEventListener('language-changed', () => queueMicrotask(render));
  function updateLayout() {
    fitChips();
    positionPopover();
  }
  window.addEventListener('resize', updateLayout);
  // Neighbor text and visibility changes must be folded before the next paint.
  const content = new MutationObserver(updateLayout);
  const layout = new ResizeObserver(updateLayout);
  layout.observe(bar);
  for (const neighbor of neighbors) {
    layout.observe(neighbor);
    content.observe(neighbor, { subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ['hidden', 'class'] });
  }

  return {
    update(value, codexUsage = null) {
      if (value === null) setOrdering(null);
      data = value;
      codex = codexUsage;
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
