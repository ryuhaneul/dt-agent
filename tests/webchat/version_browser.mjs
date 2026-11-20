import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const { chromium } = await import(pathToFileURL(path.resolve(process.env.DTW_PLAYWRIGHT_MODULE)));
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.woff2': 'font/woff2' };
const server = http.createServer(async (request, response) => {
  const { pathname } = new URL(request.url, 'http://localhost');
  const name = pathname === '/' ? 'index.html' : pathname.replace(/^\/static\//, '');
  try {
    const body = await fs.readFile(path.join(config.static, name));
    response.writeHead(200, {
      'Content-Type': mime[path.extname(name)] || 'application/octet-stream',
      'Content-Security-Policy': name === 'starflow/base-stars.html' ? config.starflowCsp : config.csp,
    }).end(body);
  } catch {
    response.writeHead(404).end();
  }
});
await fs.mkdir(config.shots, { recursive: true });
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

async function check(browser, width, theme) {
  const context = await browser.newContext({
    viewport: { width, height: 900 }, colorScheme: theme, reducedMotion: 'reduce',
  });
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let authenticated = false, stateFields = { version: '9.8.7' };
    let releaseState;
    await page.routeWebSocket('**/api/ws', () => {});
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const reply = json => route.fulfill({ json });
      switch (url.pathname) {
        case '/api/i18n': {
          const lang = url.searchParams.get('lang') || 'ko';
          return reply({ lang, strings: config.locales[lang] });
        }
        case '/api/auth/state':
          return reply({ authenticated, csrf: 'fixture-csrf', totp_configured: true, bridge_up: true });
        case '/api/auth/password': return reply({ next: 'totp' });
        case '/api/auth/totp': authenticated = true; return reply({ ok: true });
        case '/api/auth/logout': authenticated = false; return reply({ ok: true });
        case '/api/state': {
          const fields = stateFields;
          if (releaseState) await releaseState;
          return reply({ ...fields, telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, web_sessions: 1 });
        }
        default: return reply({ topics: [], commands: [], items: [], keys: [], files: [] });
      }
    });
    const app = page.locator('#app');
    const subtitle = page.locator('.bar .logo .sub span');
    async function expectSubtitle(expected) {
      await page.waitForFunction(text => document.querySelector('.bar .logo .sub span').textContent === text, expected, { timeout: 8000 });
    }
    async function login() {
      await page.locator('#pw').fill('test-password');
      await page.locator('#passwordNext').click();
      const inputs = page.locator('#otp input');
      await inputs.first().waitFor();
      for (const [index, digit] of Array.from('123456').entries()) await inputs.nth(index).fill(digit);
      await app.waitFor({ state: 'visible' });
    }

    await page.goto(origin);
    const koTitle = config.locales.ko['app.title'];
    const enTitle = config.locales.en['app.title'];
    const heading = page.locator('#pwForm h2');
    await page.waitForFunction(title => document.title === title, koTitle);
    assert.equal(await heading.innerText(), koTitle);
    assert.equal(await subtitle.textContent(), koTitle);

    await login();
    await expectSubtitle('v9.8.7');
    assert.equal(await subtitle.isVisible(), width > 760);
    assert.equal(await page.title(), koTitle);
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    assert.equal(await page.locator('.bar').evaluate(header => header.scrollWidth - header.clientWidth), 0);
    await page.screenshot({ path: path.join(config.shots, `version-${width}-${theme}.png`), animations: 'disabled' });

    await page.locator('#languageButton').click();
    await page.locator('[data-locale="en"]').click();
    await page.waitForFunction(title => document.title === title, enTitle);
    assert.equal(await subtitle.textContent(), 'v9.8.7');

    if (width === 1280 && theme === 'light') {
      await page.evaluate(async () => {
        const { api, checkSession } = await import('/static/js/api.js');
        await api('/auth/logout', { method: 'POST' });
        await checkSession();
      });
      await heading.waitFor();
      assert.equal(await heading.innerText(), enTitle);
      assert.equal(await subtitle.textContent(), enTitle);

      let release;
      releaseState = new Promise(resolve => { release = resolve; });
      stateFields = {};
      try {
        const stateRequest = page.waitForRequest('**/api/state');
        await login();
        await stateRequest;
        assert.equal(await subtitle.textContent(), enTitle);
      } finally {
        const stateResponse = page.waitForResponse('**/api/state');
        release();
        await stateResponse;
      }
      await page.waitForFunction(() => document.querySelector('#botDetail').classList.contains('badge-ok'));
      assert.equal(await subtitle.textContent(), enTitle);

      releaseState = null;
      stateFields = { version: '9.8.8' };
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await expectSubtitle('v9.8.8');
    }
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

try {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [width, theme] of [[1280, 'light'], [1280, 'dark'], [768, 'light'], [375, 'light']]) {
      await check(browser, width, theme);
    }
  } finally {
    await browser.close();
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
