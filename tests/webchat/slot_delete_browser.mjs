import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const { chromium } = await import(pathToFileURL(process.env.DTW_PLAYWRIGHT_MODULE));
const mime = {
  '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
  '.woff2': 'font/woff2',
};
await fs.mkdir(config.shots, { recursive: true });

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const target = path.join(
    config.static, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''),
  );
  try {
    const body = await fs.readFile(target);
    response.writeHead(200, {
      'Content-Type': mime[path.extname(target)] || 'application/octet-stream',
      'Content-Security-Policy': url.pathname === '/static/starflow/base-stars.html'
        ? (config.starflowCsp || config.csp) : config.csp,
    }).end(body);
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true });
const origin = 'http://127.0.0.1:' + server.address().port;

async function run(width, theme) {
  const context = await browser.newContext({
    viewport: { width, height: width < 600 ? 812 : 900 }, colorScheme: theme,
    reducedMotion: 'reduce',
  });
  try {
    const page = await context.newPage();
    const errors = [], mutations = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/api/ws', () => {});
    let blockWork = true, blockCooldownRead = true, blockLogout = true, blockDisconnect = true;
    let refreshes = 0;
    let slots = [
      { name: 'claude', kind: 'oauth', authenticated: true, priority: 1 },
      { name: 'claude-work', kind: 'oauth', authenticated: true, priority: 2 },
      { name: 'claude-unused', kind: 'oauth', authenticated: false, auth_expired: true, priority: null },
      { name: 'claude-api', kind: 'api_key', authenticated: true, priority: 3 },
      { name: 'claude-key', kind: 'api_key_detached', authenticated: false, priority: 4 },
    ];
    await page.route('**/api/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname.slice(4);
      const reply = (json, status = 200) => route.fulfill({ json, status });
      if (endpoint === '/i18n') return reply({ lang: 'ko', strings: config.strings });
      if (endpoint === '/auth/state') return reply({ authenticated: true, csrf: 'fixture-csrf' });
      if (endpoint === '/topics') return reply({ topics: [] });
      if (endpoint === '/state') return reply({ telegram: { state: 'up' }, outbox: { pending: 0 } });
      if (endpoint === '/cli-login/status') {
        refreshes++;
        return reply({
          providers: { claude: 'authenticated', codex: 'installed', grok: 'installed' },
          claude_slots: slots, running: false,
        });
      }
      if (endpoint.startsWith('/cli-login/slots/')) {
        const [, , , name, action = 'delete'] = endpoint.split('/');
        assert.equal(request.method(), action === 'delete' ? 'DELETE' : 'POST');
        assert.equal(request.headers()['x-dtw-client'], 'web');
        assert.equal(request.headers()['x-dtw-csrf'], 'fixture-csrf');
        mutations.push([name, action]);
        const slot = slots.find(slot => slot.name === name);
        if (action === 'logout') {
          if (blockLogout) return reply({ error: 'slot_busy' }, 409);
          slot.authenticated = false;
          slot.auth_expired = true;
        } else if (action === 'disconnect') {
          if (blockDisconnect) return reply({ error: 'slot_busy' }, 409);
          slot.kind = 'api_key_detached';
          slot.authenticated = false;
        } else {
          if (name === 'claude-work' && blockWork) return reply({ error: 'slot_busy' }, 409);
          if (name === 'claude-work' && blockCooldownRead) {
            return reply({ error: 'slot_delete_error' }, 500);
          }
          if (name === 'claude-api') {
            slot.priority = null;
            return reply({
              error: 'slot_delete_failed', path: '.cli-auth/claude-api', reason: 'Permission denied',
            }, 500);
          }
          slots = slots.filter(slot => slot.name !== name);
        }
        return reply({ ok: true });
      }
      return reply({ commands: [], keys: [], files: [], items: [] });
    });

    await page.goto(origin);
    await page.locator('#accountButton').click();
    await page.locator('#openCliLogin').click();
    const dialog = page.locator('#cliLoginDlg');
    const confirm = page.locator('#cliConfirmDlg');
    const row = name => dialog.locator('[data-slot="' + name + '"]');
    const trigger = name => row(name).locator('.cli-slot-more');
    const surface = theme === 'dark' ? 'rgb(23, 26, 29)' : 'rgb(255, 255, 255)';
    const t = key => config.strings['cli_login.' + key];

    async function settle(dialog) {
      await dialog.evaluate(async element => {
        await document.fonts.ready;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      assert.equal(await dialog.evaluate(el => getComputedStyle(el).backgroundColor), surface);
    }
    async function shot(state, target = dialog) {
      await page.mouse.move(0, 0);
      await settle(target);
      await page.screenshot({
        path: path.join(config.shots, ['slots', state, width, theme].join('-') + '.png'),
        animations: 'disabled',
      });
    }
    async function verifyLayout() {
      assert.deepEqual(await dialog.evaluate(el => {
        const issues = [];
        const rect = node => node.getBoundingClientRect();
        const overlap = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
        const inViewport = box => box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
        for (const element of [document.documentElement, el, el.querySelector('.cli-body')]) {
          if (element.scrollWidth > element.clientWidth) issues.push('horizontal overflow');
        }
        for (const row of el.querySelectorAll('.cli-slot')) {
          const children = [...row.children].filter(node => !node.matches('[popover]')).map(rect);
          for (const [index, box] of children.entries()) {
            if (box.left < rect(row).left || box.right > rect(row).right ||
                children.slice(index + 1).some(other => overlap(box, other))) issues.push('slot row layout');
          }
        }
        for (const menu of el.querySelectorAll('.cli-slot-menu:popover-open')) {
          const button = el.querySelector('[aria-controls="' + menu.id + '"]');
          if (!inViewport(rect(menu)) || overlap(rect(menu), rect(button))) issues.push('menu layout');
        }
        const confirmation = document.querySelector('#cliConfirmDlg[open]');
        if (confirmation) {
          const buttons = [...confirmation.querySelectorAll('.dlg-foot button')].map(rect);
          if (!inViewport(rect(confirmation)) || confirmation.scrollWidth > confirmation.clientWidth ||
              overlap(...buttons)) issues.push('confirmation layout');
        }
        return issues;
      }), []);
    }
    async function openMenu(name) {
      const button = trigger(name);
      await button.click();
      const menu = row(name).getByRole('menu');
      await menu.waitFor();
      await page.waitForFunction(id => {
        const menu = document.getElementById(id);
        return menu.matches(':popover-open') && menu.style.top &&
          document.querySelector('[aria-controls="' + id + '"]').getAttribute('aria-expanded') === 'true';
      }, await menu.getAttribute('id'));
      await verifyLayout();
      return menu;
    }
    async function item(name, action) {
      const menu = await openMenu(name);
      await menu.locator('[data-i18n="cli_login.' + action + '"]').click();
      await confirm.waitFor();
      assert.equal(await confirm.locator('#cliConfirmTarget').textContent(), name);
      assert.equal(await confirm.locator('#cliConfirmTitle').textContent(), t(action + '_title'));
      assert.equal(await confirm.locator('#cliConfirmWarning').textContent(), t(action + '_warning'));
      assert.deepEqual(await confirm.locator('.dlg-foot button').allTextContents(), [config.strings['common.cancel'], t(action)]);
      assert.equal(await page.evaluate(() => document.activeElement.id), 'cliConfirmNo');
      assert.equal(await confirm.locator('#cliConfirmYes').evaluate(button => getComputedStyle(button).color),
        theme === 'dark' ? 'rgb(240, 145, 137)' : 'rgb(163, 49, 42)');
      await verifyLayout();
    }
    async function submit() {
      const polls = refreshes;
      await confirm.locator('#cliConfirmYes').click();
      await confirm.waitFor({ state: 'hidden' });
      assert.ok(refreshes > polls);
    }
    async function focused(locator) {
      assert.equal(await locator.evaluate(el => document.activeElement === el), true);
    }
    async function menuLabels(name, expected) {
      const menu = await openMenu(name);
      assert.deepEqual(await menu.getByRole('menuitem').allTextContents(), expected.map(t));
      await page.keyboard.press('Escape');
      await menu.waitFor({ state: 'hidden' });
      await focused(trigger(name));
    }

    await trigger('claude').waitFor();
    await verifyLayout();
    await shot('list');
    for (const name of slots.map(slot => slot.name)) {
      const button = trigger(name);
      const size = await button.boundingBox();
      assert.deepEqual([size.width, size.height], [44, 44]);
      assert.equal(await button.getAttribute('aria-label'), t('slot_more').replace('{name}', name));
    }
    assert.equal(await row('claude-unused').locator('.badge').textContent(), t('logged_out'));
    assert.equal(await row('claude-key').locator('+ .cli-slot-reconnect').textContent(), t('key_reconnect'));
    await menuLabels('claude', ['relogin', 'logout']);
    await menuLabels('claude-work', ['relogin', 'logout', 'delete']);
    await menuLabels('claude-unused', ['login', 'delete']);
    await menuLabels('claude-api', ['disconnect', 'delete']);
    await menuLabels('claude-key', ['delete']);

    const menu = await openMenu('claude-work');
    const items = menu.getByRole('menuitem');
    await focused(items.first());
    await shot('menu');
    await page.keyboard.press('Shift+Tab');
    await focused(items.last());
    await page.keyboard.press('Tab');
    await focused(items.first());
    await dialog.locator('#cliLoginTitle').click();
    await menu.waitFor({ state: 'hidden' });
    assert.equal(await trigger('claude-work').getAttribute('aria-expanded'), 'false');

    const body = dialog.locator('.cli-body');
    await body.evaluate(el => { el.style.maxHeight = '280px'; el.scrollTop = 0; });
    const scrollMenu = await openMenu('claude');
    await body.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await scrollMenu.waitFor({ state: 'hidden' });
    assert.equal(await trigger('claude').getAttribute('aria-expanded'), 'false');
    await body.evaluate(el => { el.style.maxHeight = ''; el.scrollTop = 0; });

    await item('claude', 'logout');
    await page.keyboard.press('Shift+Tab');
    await focused(confirm.locator('#cliConfirmYes'));
    await page.keyboard.press('Tab');
    await focused(confirm.locator('#cliConfirmNo'));
    await shot('logout-confirm', confirm);
    await page.keyboard.press('Escape');
    await confirm.waitFor({ state: 'hidden' });
    await focused(trigger('claude'));
    assert.deepEqual(mutations, []);

    await item('claude', 'logout');
    await submit();
    assert.equal(await row('claude').locator('.badge').textContent(), t('slot_in_use'));
    assert.equal(await row('claude').locator('+ .slot-error').textContent(), t('logout_busy').replace('{name}', 'claude'));
    await focused(trigger('claude'));
    await shot('busy');
    blockLogout = false;
    await item('claude', 'logout');
    await submit();
    assert.equal(await row('claude').locator('.badge').textContent(), t('logged_out'));
    await menuLabels('claude', ['login']);

    await item('claude-api', 'disconnect');
    await shot('disconnect-confirm', confirm);
    await submit();
    assert.equal(await row('claude-api').locator('.badge').textContent(), t('slot_in_use'));
    assert.equal(await row('claude-api').locator('+ .slot-error').textContent(),
      t('disconnect_busy').replace('{name}', 'claude-api'));
    blockDisconnect = false;
    await item('claude-api', 'disconnect');
    await submit();
    assert.equal(await row('claude-api').locator('.badge').textContent(), t('key_disconnected'));
    await menuLabels('claude-api', ['delete']);

    await item('claude-work', 'delete');
    await submit();
    assert.equal(await row('claude-work').locator('.badge').textContent(), t('slot_in_use'));
    assert.equal(await row('claude-work').locator('+ .slot-error').textContent(), t('delete_busy').replace('{name}', 'claude-work'));
    await focused(trigger('claude-work'));
    blockWork = false;
    await item('claude-work', 'delete');
    await submit();
    assert.equal(await row('claude-work').locator('+ .slot-error').textContent(), t('delete_error').replace('{name}', 'claude-work'));
    assert.equal(await row('claude-work').locator('.badge').textContent(), t('authenticated'));
    blockCooldownRead = false;
    await item('claude-work', 'delete');
    await submit();
    await row('claude-work').waitFor({ state: 'detached' });
    await focused(trigger('claude-unused'));
    await item('claude-unused', 'delete');
    await submit();
    await row('claude-unused').waitFor({ state: 'detached' });
    await item('claude-api', 'delete');
    await submit();
    assert.match(await row('claude-api').locator('~ .slot-error').first().textContent(),
      /\.cli-auth\/claude-api.*Permission denied/);
    assert.deepEqual(mutations, [
      ['claude', 'logout'], ['claude', 'logout'], ['claude-api', 'disconnect'], ['claude-api', 'disconnect'],
      ['claude-work', 'delete'], ['claude-work', 'delete'], ['claude-work', 'delete'],
      ['claude-unused', 'delete'], ['claude-api', 'delete'],
    ]);
    assert.deepEqual(errors, []);
    console.log(width, theme, 'passed');
  } finally {
    await context.close();
  }
}

try {
  for (const width of [375, 768, 1280]) {
    for (const theme of ['light', 'dark']) await run(width, theme);
  }
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
