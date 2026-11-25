import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const playwright = await import(pathToFileURL(process.env.DTW_PLAYWRIGHT_MODULE));
const engine = playwright[config.browser || 'chromium'];
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const file = path.resolve(config.static, url.pathname === '/' ? 'index.html' : url.pathname.slice(8));
  if (!file.startsWith(path.resolve(config.static) + path.sep)) return response.writeHead(404).end();
  try {
    const body = await fs.readFile(file);
    const csp = url.pathname.startsWith('/static/starflow/') ? config.starflowCsp : config.csp;
    response.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream',
      'Content-Security-Policy': csp }).end(body);
  } catch {
    response.writeHead(404).end();
  }
});
const cases = [
  { provider: 'claude', slot: 'claude', new_slot: false },
  { provider: 'claude', slot: 'claude-work', new_slot: false },
  { provider: 'claude', slot: 'claude-unused', new_slot: false },
  { provider: 'claude', slot: 'claude-new', new_slot: true },
  { provider: 'codex' },
  { provider: 'grok' },
];
const targets = {
  claude: 'https://claude.com/cai/oauth/authorize?state=fixture',
  codex: 'https://auth.openai.com/device',
  grok: 'https://accounts.x.ai/device',
};
const slots = [
  { name: 'claude', kind: 'oauth', authenticated: true, priority: 1 },
  { name: 'claude-work', kind: 'oauth', authenticated: true, priority: 2 },
  { name: 'claude-unused', kind: 'oauth', authenticated: false, priority: null },
];

async function run(browser, origin, width, theme) {
  const touch = width !== 1280;
  const act = locator => touch ? locator.tap() : locator.click();
  const context = await browser.newContext({
    viewport: { width, height: width === 375 ? 812 : 900 },
    hasTouch: touch, isMobile: touch, colorScheme: theme, reducedMotion: 'reduce',
  });
  context.setDefaultTimeout(3500);
  const failures = [];
  let current, cancelled = false, resumeAfterCancel = false;
  let accountSlots = slots, releaseSwap, swapStarted, rejectSwap = false;
  const swaps = [];
  try {
    // Simulate browser popup restrictions while retaining real user gestures for normal and fallback links.
    await context.addInitScript(() => {
      const open = window.open;
      window.open = (...args) => {
        if (window.testPopupMode === 'blocked') return null;
        const tab = open.apply(window, args);
        if (window.testPopupMode !== 'denied' || !tab) return tab;
        return {
          close: () => tab.close(),
          get closed() { return tab.closed; },
          set opener(value) { tab.opener = value; },
          location: { replace() { throw new DOMException('Navigation denied', 'SecurityError'); } },
        };
      };
    });
    await context.route('https://*/**', route => route.fulfill({
      contentType: 'text/html', body: '<body>Login fixture</body>',
    }));
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.routeWebSocket('**/api/ws', () => {});
    await page.route('**/api/**', route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname;
      const reply = json => route.fulfill({ json });
      if (endpoint === '/api/i18n') return reply({ lang: 'en', strings: config.strings });
      if (endpoint === '/api/auth/state') return reply({ authenticated: true, csrf: 'fixture-csrf', bridge_up: true });
      if (endpoint === '/api/topics') return reply({ topics: [{
        tid: '1.2', chat_id: 1, topic_id: 2, name: 'Markdown fixture', group: 'telegram',
        count: 1, seen: 1, renamable: true, deletable: true, read_only: false, busy: false,
        preview: '~~literal~~ and bold', created_at: null, last_ts: null, imported_count: 0,
      }] });
      if (endpoint === '/api/masterkey/status') return reply({ state: 'unconfigured' });
      if (endpoint.endsWith('/messages')) return reply({ entries: [{
        id: 'fixture-entry', seq: 1, role: 'bot', source: 'telegram', ts: null,
        text: '~~literal~~ and **bold**',
      }] });
      if (endpoint === '/api/state') return reply({
        telegram: { state: 'up' }, outbox: { pending: 0 }, bot: { up: true }, web_sessions: 1,
      });
      if (endpoint === '/api/cli-login/status') return reply({
        providers: { claude: 'authenticated', codex: 'installed', grok: 'installed' },
        claude_slots: accountSlots, running: cancelled && resumeAfterCancel,
        ...(cancelled && resumeAfterCancel ? { provider: 'claude', job_id: 'fixture-resumed' } : {}),
      });
      if (endpoint.includes('/swap/')) {
        assert.equal(request.method(), 'POST');
        assert.equal(request.postData(), null);
        const parts = endpoint.split('/');
        const name = parts.at(-3), other = parts.at(-1);
        swaps.push([name, other]);
        if (rejectSwap) return route.fulfill({ status: 500, json: { error: 'slot_move_error' } });
        return new Promise(resolve => {
          releaseSwap = () => {
            const a = accountSlots.findIndex(item => item.name === name);
            const b = accountSlots.findIndex(item => item.name === other);
            [accountSlots[a], accountSlots[b]] = [accountSlots[b], accountSlots[a]];
            accountSlots = accountSlots.map((item, index) =>
              ({ ...item, priority: item.priority == null ? null : index + 1 }));
            resolve(reply({ ok: true }));
          };
          swapStarted();
        });
      }
      if (endpoint === '/api/cli-login/jobs') {
        current = request.postDataJSON();
        cancelled = false;
        return reply({ job_id: 'fixture-login' });
      }
      if (endpoint === '/api/cli-login/jobs/fixture-login/cancel') {
        cancelled = true;
        return route.fulfill({ status: 202, json: { ok: true } });
      }
      if (endpoint === '/api/cli-login/jobs/fixture-resumed') return reply({
        provider: 'claude', slot: 'claude-work', job_id: 'fixture-resumed',
        state: 'running', seq: 1, output: [], needs_input: true,
      });
      if (endpoint === '/api/cli-login/jobs/fixture-login') return reply({
        ...current, job_id: 'fixture-login', state: cancelled ? 'cancelled' : 'running', seq: 1, output: [],
        urls: current.provider === 'claude' ? config.claudeUrls : [targets[current.provider]],
        needs_input: current.provider === 'claude',
        code: current.provider === 'claude' ? null : 'TEST-CODE',
      });
      return reply({ commands: [], keys: [], files: [], items: [] });
    });

    async function shot(name) {
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      await page.screenshot({
        path: path.join(config.shots, `${name}-${width}-${theme}.png`), animations: 'disabled',
      });
    }
    async function login(selection, mode = 'normal', save = false) {
      try {
        current = null; cancelled = false;
        await page.goto(origin);
        await page.evaluate(mode => { window.testPopupMode = mode; }, mode);
        await act(page.locator('#accountButton'));
        await act(page.locator('#openCliLogin'));
        const dialog = page.locator('#cliLoginDlg');
        const confirm = page.locator('#cliConfirmDlg');
        if (selection.new_slot) {
          await act(dialog.locator('.cli-provider-head [data-i18n="cli_login.add_slot"]'));
          await dialog.locator('#cliSlotName').fill(selection.slot.slice(7));
          await act(dialog.locator('#cliSlotForm button[type=submit]'));
        } else if (selection.slot) {
          const row = dialog.locator(`[data-slot="${selection.slot}"]`);
          const more = row.locator('.cli-slot-more');
          await row.waitFor();
          if (await more.count()) await act(more);
          const key = selection.slot === 'claude-unused' ? 'login' : 'relogin';
          await act(row.locator(`button[data-i18n="cli_login.${key}"]`));
        } else {
          const provider = dialog.locator('.cli-provider').filter({ hasText: selection.provider });
          await act(provider.locator('[data-i18n="cli_login.login"]'));
        }
        await confirm.waitFor();
        const popup = mode === 'normal' ? context.waitForEvent('page', { timeout: 3500 }) : null;
        if (popup) popup.catch(() => {});
        await act(confirm.locator('#cliConfirmYes'));
        await page.locator('#cliProgress').waitFor();
        assert.deepEqual(current, selection);
        const planned = targets[selection.provider];
        if (mode === 'normal') {
          const tab = await popup;
          try {
            await tab.waitForURL(planned, { timeout: 3500 });
          } catch (error) {
            throw Error(`Popup remained at ${tab.url()}, rendered links: ${await page.locator('#cliLinkRows a').count()}`,
              { cause: error });
          }
          assert.equal(await tab.evaluate(() => window.opener), null);
          if (save) await shot('login-opened');
        }
        const link = page.locator('#cliLinkRows a');
        await link.waitFor();
        assert.equal(await link.getAttribute('href'), planned);
        assert.equal(await page.locator('.cli-url').textContent(), planned);
        assert.equal(await page.locator('.cli-url').evaluate(el => {
          const style = getComputedStyle(el);
          return style.userSelect ?? style.webkitUserSelect;
        }), 'text');
        const copy = page.locator('[data-i18n="cli_login.copy_link"]');
        assert.equal(await copy.isVisible(), true);
        if (mode !== 'normal') {
          assert.deepEqual(context.pages().map(tab => tab.url()), [page.url()]);
          if (save) await shot('login-fallback');
          await page.evaluate(() => {
            Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw Error(); } } });
          });
          await act(copy);
          assert.equal(await page.evaluate(() => getSelection().toString()), planned);
          const fallback = context.waitForEvent('page');
          await act(link);
          await (await fallback).waitForURL(planned);
        }
      } catch (error) {
        failures.push(`${selection.slot || selection.provider} ${mode}: ${error.message}`);
      } finally {
        for (const tab of context.pages()) if (tab !== page) await tab.close();
      }
    }

    for (const selection of cases) await login(selection, 'normal', selection === cases[0]);
    await login(cases[0], 'denied', true);
    await login(cases[0], 'blocked');

    for (const selection of [{ provider: 'codex' }, cases[0]]) {
      try {
        await login(selection);
        await act(page.locator('#cliCancel'));
        const refresh = page.waitForResponse(response =>
          new URL(response.url()).pathname === '/api/cli-login/status');
        await act(page.locator('#cliConfirmYes'));
        assert.equal((await refresh).status(), 200);
        await page.locator('#cliAccounts .cli-provider-head button[data-i18n="cli_login.login"]:enabled').first().waitFor();
        assert.equal(await page.locator('#cliResult').isVisible(), false);
        const notice = page.locator('#cliNotice');
        const codexNotice = selection.provider === 'codex';
        assert.equal(await notice.isVisible(), codexNotice, `${selection.provider} cancellation notice after refresh`);
        assert.equal(await notice.textContent(), codexNotice ? config.strings['cli_login.cancelled_codex'] : '');
        if (codexNotice && width === 375 && theme === 'light') await shot('login-codex-cancel-notice');
      } catch (error) {
        failures.push(`${selection.provider} cancellation notice: ${error.message}`);
      }
    }
    resumeAfterCancel = true;

    try {
      await login(cases[0]);
      await act(page.locator('#cliCancel'));
      await act(page.locator('#cliConfirmYes'));
      await page.locator('#cliTarget').getByText('claude · claude-work').waitFor();
      assert.equal(await page.locator('#cliCancel').isEnabled(), true, 'resumed cancel is enabled');
      assert.equal(await page.locator('#cliCodeInput').isEnabled(), true, 'resumed code input is enabled');
      await act(page.locator('#cliClose'));
      await page.locator('#cliConfirmDlg').waitFor();
      assert.equal(await page.locator('#cliConfirmTitle').textContent(), config.strings['cli_login.cancel_title']);
      await shot('login-resumed-after-cancel');
    } catch (error) {
      failures.push(`resume after cancel: ${error.message}`);
    }

    try {
      for (const badge of ['#telegramBadge', '#botBadge']) {
        await page.goto(origin);
        await act(page.locator(badge));
        const panel = page.locator('#connPanel');
        await page.locator('body.info-open').waitFor();
        const heading = panel.locator('h2');
        await heading.waitFor();
        assert.equal(new URL(page.url()).hash, '#status');
        assert.equal(await page.locator('#info').evaluate(el => el.scrollTop), 0);
        assert.equal(await panel.evaluate(el => getComputedStyle(el).boxShadow), 'none');
        assert.equal(await heading.evaluate(el => getComputedStyle(el).outlineStyle), 'none');
        await act(panel.locator('dt').first());
        assert.equal(await panel.evaluate(el => getComputedStyle(el).boxShadow), 'none');
      }
      await shot('status');
      await page.goto(origin);
      const badge = page.locator('#telegramBadge');
      await badge.waitFor();
      for (let step = 0; step < 25; step++) {
        if (await badge.evaluate(el => el === document.activeElement)) break;
        await page.keyboard.press('Tab');
      }
      assert.equal(await badge.evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('Enter');
      const heading = page.locator('#connPanel h2');
      await page.waitForFunction(el => document.activeElement === el, await heading.elementHandle());
      assert.equal(await heading.evaluate(el => getComputedStyle(el).outlineStyle), 'solid');
      await shot('status-keyboard');
    } catch (error) {
      failures.push(`status: ${error.message}`);
    }
    try {
      await page.goto(origin + '/#chat');
      const message = page.locator('#thread [data-entry-seq="1"] .md');
      await message.waitFor();
      assert.equal(await message.textContent(), '~~literal~~ and bold');
      assert.equal(await message.locator('strong').textContent(), 'bold');
      assert.equal(await message.locator('del').count(), 0);
      await shot('markdown');
    } catch (error) {
      failures.push(`markdown: ${error.message}`);
    }
    if ([1280, 375].includes(width) && theme === 'light') {
      const long = 'claude-long-research-account-with-a-descriptive-name';
      const unused = slots.find(item => item.priority == null);
      accountSlots = [slots[0], { ...slots[1], name: long },
        { ...slots[1], name: 'claude-backup', priority: 3 }, unused];
      cancelled = false;
      await page.goto(origin);
      const open = async () => {
        await act(page.locator('#accountButton'));
        await act(page.locator('#openCliLogin'));
      };
      await open();
      const dialog = page.locator('#cliLoginDlg');
      const row = name => dialog.locator(`.cli-slot[data-slot="${name}"]`);
      const button = (name, direction) => row(name).locator(`[data-slot-order="${direction}"]`);
      await button(long, 'up').waitFor();
      assert.equal(await dialog.locator('.slot-order-button').count(), 6);
      assert.equal(await row(unused.name).locator('.slot-order-button').count(), 0);
      assert.equal(await button('claude', 'up').isDisabled(), true);
      assert.equal(await button('claude-backup', 'down').isDisabled(), true);
      assert.equal(await button(long, 'up').getAttribute('aria-label'),
        config.strings['slots.move_up'].replace('{name}', long));
      assert.deepEqual(await dialog.evaluate(panel => {
        const issues = [];
        for (const row of panel.querySelectorAll('.cli-slot')) {
          const options = row.querySelector('.cli-slot-more').getBoundingClientRect();
          const name = row.querySelector('.cli-slot-name').getBoundingClientRect();
          const badge = row.querySelector('.badge').getBoundingClientRect();
          for (const button of row.querySelectorAll('.slot-order-button')) {
            const bounds = button.getBoundingClientRect();
            if (bounds.width < 44 || bounds.height < 44 || bounds.right > options.left
                || Math.max(name.right, badge.right) > bounds.left) issues.push(row.dataset.slot);
          }
        }
        return [panel.scrollWidth > panel.clientWidth, ...issues];
      }), [false]);

      async function move(direction, focused) {
        const started = new Promise(resolve => { swapStarted = resolve; });
        await act(button(long, direction));
        await started;
        assert.deepEqual(swaps.at(-1), [long, 'claude']);
        assert.equal(await dialog.locator('.slot-order-button:not(:disabled)').count(), 0);
        assert.equal(await row(long).locator('.cli-slot-more').isDisabled(), true);
        releaseSwap();
        await page.waitForFunction(({ long, focused }) => document.activeElement.matches(
          `.cli-slot[data-slot="${long}"] [data-slot-order="${focused}"]:not(:disabled)`), { long, focused });
        assert.deepEqual(await dialog.locator('.cli-slot').evaluateAll(rows => rows.map(row => row.dataset.slot)),
          accountSlots.map(item => item.name));
      }
      await move('up', 'down');
      await move('down', 'down');

      rejectSwap = true;
      await act(button(long, 'up'));
      await dialog.locator('.slot-error').waitFor();
      assert.equal(await button(long, 'up').isEnabled(), true);
      await act(dialog.locator('#cliClose'));
      accountSlots = [slots[0], unused];
      await open();
      await row(unused.name).waitFor();
      assert.equal(await dialog.locator('.slot-order-button').count(), 0);

      try {
        await act(dialog.locator('#cliClose'));
        rejectSwap = false;
        accountSlots = [slots[0], { ...slots[1], name: long },
          { ...slots[1], name: 'claude-backup', priority: 3 }, unused];
        const started = new Promise(resolve => { swapStarted = resolve; });
        const swapsBefore = swaps.length;
        await open();
        await act(button(long, 'up'));
        await started;
        await act(dialog.locator('#cliClose'));
        await open();
        await row(long).waitFor();
        const disabledWhileReopened = await button(long, 'up').isDisabled();
        const complete = releaseSwap;
        await button(long, 'up').evaluate(element => element.click());
        complete();
        assert.equal(disabledWhileReopened, true, 'an in-flight swap must stay disabled after reopening');
        await page.waitForFunction(name =>
          document.querySelector('#cliLoginDlg .cli-slot')?.dataset.slot === name, long);
        assert.equal(swaps.length, swapsBefore + 1);
        assert.equal(await button(long, 'down').isEnabled(), true);
      } catch (error) {
        failures.push(`slot reorder after reopen: ${error.message}`);
      }

      if (width === 375) {
        try {
          await page.setViewportSize({ width, height: 667 });
          accountSlots = Array.from({ length: 12 }, (_, index) =>
            ({ ...slots[0], name: `claude-slot-${index}`, priority: index + 1 }));
          rejectSwap = false;
          await page.goto(origin);
          await open();
          const target = button('claude-slot-6', 'down');
          await target.evaluate(element => {
            const scroller = element.closest('.cli-body');
            scroller.scrollTop += element.getBoundingClientRect().bottom
              - scroller.getBoundingClientRect().bottom + 2;
            element.focus({ preventScroll: true });
          });
          const started = new Promise(resolve => { swapStarted = resolve; });
          await page.keyboard.press('Enter');
          await started;
          releaseSwap();
          await page.waitForFunction(() => document.activeElement.matches(
            '.cli-slot[data-slot="claude-slot-6"] [data-slot-order="down"]:not(:disabled)'));
          const outside = await target.evaluate(element => {
            const button = element.getBoundingClientRect();
            const pane = element.closest('.cli-body').getBoundingClientRect();
            return Math.max(pane.top - button.top, button.bottom - pane.bottom, 0);
          });
          assert.ok(outside <= 0.5, `restored CLI slot button is clipped by ${outside}px`);
        } catch (error) {
          failures.push(`slot reorder keyboard visibility: ${error.message}`);
        }
      }
    }
    assert.deepEqual(pageErrors, []);
    return failures;
  } finally {
    await context.close();
  }
}

async function loginLifecycle(browser, origin, width, theme) {
  const loginPath = '/api/cli-login/';
  const shots = config.shots;
  const failures = [];
  const contexts = [];
  async function open(width, token) {
    const touch = width === 375;
    const context = await browser.newContext({
      viewport: { width, height: touch ? 812 : 900 }, isMobile: touch, hasTouch: touch,
      colorScheme: theme, reducedMotion: 'reduce',
    });
    contexts.push(context);
    await context.addCookies([{ name: 'dtw_session', value: token, url: origin }]);
    await context.addInitScript(() => { window.open = () => null; });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    await page.route('**/api/**', route => {
      const endpoint = new URL(route.request().url()).pathname;
      if (endpoint.startsWith(loginPath) || ['/api/auth/state', '/api/i18n', '/api/state'].includes(endpoint)) {
        return route.continue();
      }
      if (endpoint === '/api/masterkey/status') return route.fulfill({ json: { state: 'unconfigured' } });
      return route.fulfill({ json: { topics: [], commands: [], files: [], items: [] } });
    });
    await page.routeWebSocket('**/api/ws', () => {});
    await page.goto(origin);
    const act = locator => touch ? locator.tap() : locator.click();
    return { page, act };
  }
  const body = page => page.locator('#cliLoginDlg .cli-body');
  const outside = locator => locator.evaluate(el => {
    const bounds = el.getBoundingClientRect();
    const viewport = el.closest('.cli-body').getBoundingClientRect();
    return bounds.top < viewport.top || bounds.bottom > viewport.bottom;
  });
  async function screenshot(page, label) {
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    await page.screenshot({
      path: path.join(shots, `${label}-${width}-${theme}.png`), animations: 'disabled',
    });
  }
  async function accounts({page, act}) {
    const statusResponse = page.waitForResponse(response => new URL(response.url()).pathname === loginPath + 'status');
    await act(page.locator('#accountButton'));
    await act(page.locator('#openCliLogin'));
    const response = await statusResponse;
    assert.equal(response.status(), 200);
    await page.locator('#cliProviders .cli-provider').last().waitFor();
    return response.json();
  }
  async function select({page, act}, selection) {
    if (selection.new_slot) {
      await act(page.locator('.cli-provider-head [data-i18n="cli_login.add_slot"]'));
      await page.locator('#cliSlotName').fill(selection.slot.slice(7));
      await act(page.locator('#cliSlotForm button[type=submit]'));
    } else if (selection.slot) {
      const slot = page.locator(`[data-slot="${selection.slot}"]`);
      await act(slot.locator('.cli-slot-more'));
      const key = selection.slot === 'claude-unused' ? 'login' : 'relogin';
      await act(slot.locator(`button[data-i18n="cli_login.${key}"]`));
    } else {
      const provider = page.locator('.cli-provider').filter({ hasText: selection.provider });
      await act(provider.locator('[data-i18n="cli_login.login"]'));
    }
    await page.locator('#cliConfirmDlg').waitFor();
  }
  async function start({page, act}) {
    const request = page.waitForResponse(response => new URL(response.url()).pathname === loginPath + 'jobs');
    await act(page.locator('#cliConfirmYes'));
    return request;
  }
  async function cancel({page, act}) {
    await page.bringToFront();
    await page.locator('#cliProgress').waitFor();
    await act(page.locator('#cliCancel'));
    const request = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/cancel'));
    const refresh = page.waitForResponse(response => new URL(response.url()).pathname === loginPath + 'status');
    await act(page.locator('#cliConfirmYes'));
    const response = await request;
    assert.equal(response.status(), 202);
    const status = await refresh;
    assert.equal(status.status(), 200);
    assert.equal((await status.json()).running, false);
    return page.locator('#cliResult').isVisible();
  }

  try {
    const owner = await open(1280, config.sessions[0]);
    const guest = await open(width, config.sessions[1]);
    for (const selection of cases) {
      const label = selection.slot || selection.provider;
      assert.equal((await accounts(guest)).running, false);
      await select(guest, selection);
      await accounts(owner);
      await select(owner, { provider: 'codex' });
      assert.equal((await start(owner)).status(), 202);
      const response = await start(guest);
      assert.equal(response.status(), 409);
      assert.deepEqual(await response.json(), { error: 'login_running' });
      const message = guest.page.getByText(config.busyMessage, { exact: true });
      await message.waitFor();
      await guest.page.evaluate(() => new Promise(requestAnimationFrame));
      const clipped = await outside(message);
      console.log(`${width} ${theme} ${label}: 409 login_running, notice ${clipped ? 'outside viewport' : 'in viewport'}, scroll ${await body(guest.page).evaluate(el => el.scrollTop)}`);
      if (clipped) failures.push(`${label}: busy notice outside viewport`);
      if (selection.provider === 'grok') await screenshot(guest.page, 'login-conflict');
      await guest.act(guest.page.locator('#cliClose'));

      const status = await accounts(guest);
      assert.deepEqual([status.running, status.provider, status.job_id], [true, 'codex', null]);
      await message.waitFor();
      if (await outside(message)) failures.push('Initial busy notice outside viewport');
      const blockedActions = guest.page.locator('#cliProviders button[data-i18n="cli_login.login"], #cliProviders button[data-i18n="cli_login.relogin"], #cliProviders button[data-i18n="cli_login.add_slot"]');
      assert.ok(await blockedActions.count() >= 5);
      assert.equal(await blockedActions.evaluateAll(buttons => buttons.every(button => button.disabled)), true);
      if (selection.provider === 'grok') await screenshot(guest.page, 'login-busy');
      await guest.act(guest.page.locator('#cliClose'));

      const resultVisible = await cancel(owner);
      console.log(`${width} ${theme} ${label}: 202 cancel, status refreshed, ${resultVisible ? 'cancelled result' : 'accounts'}`);
      if (resultVisible || !await owner.page.locator('#cliAccounts').isVisible()) {
        failures.push(`${label}: cancellation did not return to accounts`);
      }
      await owner.act(owner.page.locator('#cliClose'));
    }
    await accounts(guest);
    await select(guest, cases[0]);
    assert.equal((await start(guest)).status(), 202);
    const resultVisible = await cancel(guest);
    if (resultVisible || !await guest.page.locator('#cliAccounts').isVisible()) {
      failures.push('Cancelling Claude did not return to accounts');
    }
    await screenshot(guest.page, 'login-cancel');
    return failures;
  } finally {
    await Promise.all(contexts.map(context => context.close()));
  }
}

await fs.mkdir(config.shots, { recursive: true });
if (!config.origin) await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = config.origin || 'http://127.0.0.1:' + server.address().port;
const browser = await engine.launch({
  headless: config.headless ?? true, executablePath: config.executablePath,
});
let failures = 0;
try {
  for (const width of config.widths || [375, 768, 1280]) {
    for (const theme of config.themes || ['light', 'dark']) {
      const scenario = config.origin ? loginLifecycle : run;
      const errors = await scenario(browser, origin, width, theme);
      failures += errors.length;
      console.log(width, theme, errors.length ? JSON.stringify(errors) : 'passed');
    }
  }
  assert.equal(failures, 0);
} finally {
  await browser.close();
  if (server.listening) await new Promise(resolve => server.close(resolve));
}
