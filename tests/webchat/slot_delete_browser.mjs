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
      'Content-Security-Policy': config.csp,
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
    viewport: { width, height: width < 600 ? 812 : 720 }, colorScheme: theme,
  });
  try {
    const page = await context.newPage();
    const errors = [], deletions = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/api/ws', () => {});
    let blockWork = true, blockCooldownRead = true;
    let slots = [
      { name: 'claude', authenticated: true, priority: 1 },
      { name: 'claude-work', authenticated: true, priority: 2 },
      { name: 'claude-unused', authenticated: false, priority: null },
      { name: 'claude-api', kind: 'api_key', authenticated: true, priority: 3 },
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
        return reply({
          providers: { claude: 'authenticated', codex: 'installed', grok: 'installed' },
          claude_slots: slots, running: false,
        });
      }
      if (endpoint.startsWith('/cli-login/slots/')) {
        assert.equal(request.method(), 'DELETE');
        assert.equal(request.headers()['x-dtw-client'], 'web');
        assert.equal(request.headers()['x-dtw-csrf'], 'fixture-csrf');
        const name = endpoint.split('/').at(-1);
        deletions.push(name);
        if (name === 'claude-work' && blockWork) return reply({ error: 'slot_busy' }, 409);
        if (name === 'claude-work' && blockCooldownRead) {
          return reply({ error: 'slot_delete_error' }, 500);
        }
        if (name === 'claude-api') {
          return reply({
            error: 'slot_delete_failed', path: '.cli-auth/claude-api', reason: 'Permission denied',
          }, 500);
        }
        slots = slots.filter(slot => slot.name !== name);
        return reply({ ok: true });
      }
      return reply({ commands: [], keys: [], files: [], items: [] });
    });
    await page.goto(origin);
    await page.locator('#accountButton').click();
    await page.locator('#openCliLogin').click();
    const slot = name => page.locator('[data-slot="' + name + '"]');
    const remove = name => slot(name).locator('[data-delete]');
    const confirm = page.locator('#cliConfirmDlg');
    const shot = state => page.screenshot({
      path: path.join(config.shots, ['slots-a', state, width, theme].join('-') + '.png'),
    });
    await remove('claude-work').waitFor();
    assert.equal(await remove('claude').count(), 0);
    assert.equal(await remove('claude-unused').count(), 1);
    assert.equal(await remove('claude-api').count(), 1);
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    await page.evaluate(() => document.fonts.ready);
    await shot('list');

    await remove('claude-work').click();
    assert.equal(await confirm.locator('#cliConfirmTarget').textContent(), 'claude-work');
    assert.equal(await confirm.locator('#cliConfirmWarning').textContent(), config.strings['cli_login.delete_warning']);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'cliConfirmNo');
    await shot('confirm');
    await page.keyboard.press('Escape');
    await confirm.waitFor({ state: 'hidden' });
    assert.deepEqual(deletions, []);

    await remove('claude-work').click();
    await confirm.locator('#cliConfirmYes').click();
    await confirm.waitFor({ state: 'hidden' });
    assert.equal(await slot('claude-work').locator('.badge').textContent(), config.strings['cli_login.slot_in_use']);
    const busy = slot('claude-work').locator('+ .slot-error');
    assert.equal(await busy.textContent(), config.strings['cli_login.delete_busy'].replace('{name}', 'claude-work'));
    assert.equal(await remove('claude-work').evaluate(button => document.activeElement === button), true);
    await shot('busy');

    blockWork = false;
    await remove('claude-work').click();
    await confirm.locator('#cliConfirmYes').click();
    await confirm.waitFor({ state: 'hidden' });
    assert.equal(await slot('claude-work').locator('+ .slot-error').textContent(),
      config.strings['cli_login.delete_error'].replace('{name}', 'claude-work'));
    assert.equal(await slot('claude-work').locator('.badge').textContent(),
      config.strings['cli_login.authenticated']);
    await shot('cooldown-error');
    blockCooldownRead = false;
    await remove('claude-work').click();
    await confirm.locator('#cliConfirmYes').click();
    await slot('claude-work').waitFor({ state: 'detached' });
    assert.equal(await remove('claude-unused').evaluate(button => document.activeElement === button), true);
    await remove('claude-unused').click();
    await confirm.locator('#cliConfirmYes').click();
    await slot('claude-unused').waitFor({ state: 'detached' });
    await remove('claude-api').click();
    await confirm.locator('#cliConfirmYes').click();
    await confirm.waitFor({ state: 'hidden' });
    assert.match(await slot('claude-api').locator('+ .slot-error').textContent(),
      /\.cli-auth\/claude-api.*Permission denied/);
    assert.deepEqual(deletions, ['claude-work', 'claude-work', 'claude-work', 'claude-unused', 'claude-api']);
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
