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
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const dialogs = [
  'renameDlg', 'delDlg', 'diffDlg', 'cliLoginDlg', 'cliConfirmDlg', 'pwDialog',
  'vaultDlg', 'cronDlg', 'autocompactDlg', 'secretDlg', 'attachmentLimits', 'attachmentViewer',
];
const returnRoutes = { delDlg: 'manage', diffDlg: 'edit' };

async function backdropPoint(dialog) {
  return dialog.evaluate(element => {
    const { left, right, top, bottom } = element.getBoundingClientRect();
    const points = [[innerWidth / 2, 8], [8, innerHeight / 2],
      [innerWidth - 8, innerHeight / 2], [innerWidth / 2, innerHeight - 8]];
    const outside = ([x, y]) => x < left || x > right || y < top || y > bottom;
    // Use a point that reaches the dialog's backdrop if one is available.
    return points.find(([x, y]) => outside([x, y]) && document.elementFromPoint(x, y) === element)
      || points.find(outside);
  });
}

async function openDialog(page, dialog, id) {
  const route = returnRoutes[id];
  if (route) {
    await page.evaluate(route => { location.hash = route; }, route);
    if (id === 'delDlg') {
      await page.locator('#mlist [data-delete-tid]').click();
    } else {
      await page.locator('#ed').fill('Changed content');
      await page.locator('#fileSave').click();
    }
  } else {
    await dialog.evaluate(element => element.showModal());
  }
  await dialog.waitFor({ state: 'visible' });
  await dialog.evaluate(element => {
    delete element.dataset.testClosed;
    element.addEventListener('close', () => { element.dataset.testClosed = ''; }, { once: true });
  });
}

async function checkDialog(page, id, width) {
  const dialog = page.locator(`#${id}`);
  const waitForClose = async () => {
    await page.locator(`#${id}[data-test-closed]`).waitFor({ state: 'attached', timeout: 1500 });
    assert.equal(await dialog.isVisible(), false);
    if (returnRoutes[id]) {
      await page.waitForURL(`**/#${returnRoutes[id]}`);
      await page.evaluate(() => { location.hash = 'chat'; });
      await page.waitForURL('**/#chat');
    }
  };
  await openDialog(page, dialog, id);
  const bounds = await dialog.boundingBox();
  await page.mouse.click(bounds.x + 2, bounds.y + bounds.height / 2);
  await page.keyboard.press('Escape');
  await waitForClose();

  await openDialog(page, dialog, id);
  const outside = await backdropPoint(dialog);
  assert.ok(outside, `${id} has a backdrop point at ${width}px`);
  const heading = dialog.locator('h2');
  const box = await heading.boundingBox();
  if (box?.width && box?.height) {
    const inside = [box.x + 3, box.y + box.height / 2];
    await page.mouse.click(...inside);
    assert.equal(await dialog.isVisible(), true, `${id}: inside click`);
    await page.mouse.move(...inside);
    await page.mouse.down();
    await page.mouse.move(...outside, { steps: 5 });
    await page.mouse.up();
    assert.equal(await dialog.isVisible(), true, `${id}: release outside after dragging from the heading`);
  }
  if (width === 375) await page.touchscreen.tap(...outside);
  else await page.mouse.click(...outside);
  await waitForClose();
  console.log(`${width}px ${id}: backdrop and Escape closed the dialog${returnRoutes[id] ? ' and returned to the same route' : ''}.`);
}

async function check(width, browser) {
  const page = await browser.newPage({
    viewport: { width, height: 844 }, isMobile: width === 375, hasTouch: width === 375,
    reducedMotion: 'reduce',
  });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let releaseStart;
  const start = new Promise(resolve => { releaseStart = resolve; });
  try {
    await page.addInitScript(() => { window.open = () => null; });
    await page.routeWebSocket('**/api/ws', () => {});
    await page.route('**/api/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname;
      const reply = json => route.fulfill({ json });
      if (endpoint === '/api/i18n') return reply({ lang: 'en', strings: config.strings });
      if (endpoint === '/api/auth/state') return reply({ authenticated: true, csrf: 'fixture-csrf', bridge_up: true });
      if (endpoint === '/api/masterkey/status') return reply({ state: 'unconfigured' });
      if (endpoint === '/api/state') return reply({ telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 } });
      if (endpoint === '/api/topics') return reply({ topics: [{
        tid: '1.2', chat_id: 1, topic_id: 2, name: 'Example topic', group: 'telegram',
        count: 0, unread: 0, last_ts: null, preview: '', deletable: true, renamable: true,
      }] });
      if (endpoint.endsWith('/messages')) return reply({ entries: [], has_more: false });
      if (endpoint === '/api/files') return reply({ files: [{ name: 'PERSONA.md' }] });
      if (endpoint === '/api/files/PERSONA.md') return reply({
        name: 'PERSONA.md', content: 'Original content', sha256: 'fixture-base',
      });
      if (endpoint === '/api/cli-login/status') return reply({
        providers: { claude: 'installed' },
        claude_slots: [{ name: 'claude', authenticated: false, priority: 1 }],
        running: false,
      });
      if (endpoint === '/api/cli-login/jobs' && request.method() === 'POST') {
        await start;
        return reply({ job_id: 'fixture-login' });
      }
      if (endpoint === '/api/cli-login/jobs/fixture-login') return reply({
        job_id: 'fixture-login', provider: 'claude', slot: 'claude', state: 'running', seq: 0, output: [],
      });
      return reply({ topics: [], commands: [], items: [], keys: [], files: [] });
    });
    await page.goto(origin);
    await page.waitForURL('**/#chat');
    await page.locator('body:not([data-auth-pending])').waitFor();
    assert.deepEqual(await page.locator('dialog').evaluateAll(elements => elements.map(element => element.id)), dialogs);
    for (const id of dialogs) await checkDialog(page, id, width);

    const rename = page.locator('#renameDlg');
    await rename.evaluate(element => { element.style.padding = '12px'; });
    await openDialog(page, rename, 'renameDlg');
    const padding = await rename.evaluate(element => {
      const { x, y } = element.getBoundingClientRect();
      const point = [x + 6, y + 12];
      return document.elementFromPoint(...point) === element ? point : null;
    });
    assert.ok(padding, 'Test point hits the dialog padding itself');
    await page.mouse.click(...padding);
    assert.equal(await rename.isVisible(), true);
    await page.keyboard.press('Escape');
    await rename.waitFor({ state: 'hidden' });

    await page.locator('#accountButton').click();
    await page.locator('#openCliLogin').click();
    const login = page.locator('#cliLoginDlg');
    const confirm = page.locator('#cliConfirmDlg');
    const slotMenu = login.locator('.cli-slot-more');
    const slotAction = login.getByRole('menuitem', { name: config.strings['cli_login.login'], exact: true });
    for (const dismiss of ['Escape', 'backdrop']) {
      await slotMenu.click();
      await slotAction.waitFor();
      if (dismiss === 'Escape') await page.keyboard.press('Escape');
      else await page.mouse.click(...await backdropPoint(login));
      await slotAction.waitFor({ state: 'hidden' });
      assert.equal(await login.isVisible(), true, 'Dismissing the slot menu leaves the login dialog open');
    }
    await slotMenu.click();
    await slotAction.click();
    const starting = page.waitForRequest(request => new URL(request.url()).pathname === '/api/cli-login/jobs'
      && request.method() === 'POST');
    await confirm.locator('#cliConfirmYes').click();
    await starting;
    assert.equal(await confirm.locator('#cliConfirmYes').isDisabled(), true);
    await page.mouse.click(...await backdropPoint(confirm));
    assert.equal(await confirm.isVisible(), true, 'Busy confirmation stays open after a backdrop click');
    await page.keyboard.press('Escape');
    assert.equal(await confirm.isVisible(), true, 'Busy confirmation stays open after Escape');
    releaseStart();
    await login.locator('#cliProgress').waitFor();
    await confirm.waitFor({ state: 'hidden' });

    for (const dismiss of ['Escape', 'backdrop']) {
      if (dismiss === 'Escape') await page.keyboard.press('Escape');
      else await page.mouse.click(...await backdropPoint(login));
      await confirm.waitFor({ state: 'visible', timeout: 1500 });
      assert.equal(await login.isVisible(), true, 'Active login stays open while cancellation is confirmed');
      assert.equal(await confirm.locator('#cliConfirmTitle').innerText(), config.strings['cli_login.cancel_title']);
      await page.mouse.click(...await backdropPoint(confirm));
      await confirm.waitFor({ state: 'hidden', timeout: 1500 });
      assert.equal(await login.isVisible(), true, 'Backdrop dismisses only the top dialog');
    }
    assert.deepEqual(errors, []);
    console.log(`${width}px: padding, inside-to-outside drag, slot menu, busy confirmation, active login and stacked dialogs passed.`);
  } finally {
    releaseStart();
    await page.close();
  }
}

try {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [375, 1280]) await check(width, browser);
  } finally {
    await browser.close();
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
