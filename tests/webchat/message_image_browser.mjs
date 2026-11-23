import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const cfg = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const { chromium } = await import(pathToFileURL(path.resolve(process.env.DTW_PLAYWRIGHT_MODULE)));
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const server = http.createServer(async (request, response) => {
  const { pathname } = new URL(request.url, 'http://localhost');
  const name = pathname === '/' ? 'index.html' : pathname.replace(/^\/static\//, '');
  try {
    const body = await fs.readFile(path.join(cfg.static, name));
    response.writeHead(200, {
      'Content-Type': mime[path.extname(name)] || 'application/octet-stream',
      'Content-Security-Policy': name === 'starflow/base-stars.html' ? cfg.starflowCsp : cfg.csp,
    }).end(body);
  } catch {
    response.writeHead(404).end();
  }
});

const names = Object.keys(cfg.sizes);
const evidence = { layouts: [], resize: {}, scroll: {} };
const fixtures = new Map(await Promise.all(names.map(async name =>
  [name, await fs.readFile(path.join(cfg.directory, `${name}.png`))],
)));
const topic = {
  tid: '1.2', chat_id: 1, topic_id: 2, name: 'Images', preview: 'Image previews', group: 'telegram', seen: 32,
};
const stamp = '2026-01-01T12:00:00Z';

async function openChat(page, images, delayed = null) {
  const entries = Array.from({ length: 32 }, (_, i) => {
    const seq = i + 1;
    const name = images[seq];
    return {
      id: `message-${seq}`, seq, ts: stamp, role: 'bot', source: 'web',
      text: name ? `<file:./${name}.png>` : `Message ${seq}`,
      has_attachments: Boolean(name),
    };
  });
  await page.routeWebSocket('**/api/ws', () => {});
  await page.route('**/api/**', async route => {
    const { pathname } = new URL(route.request().url());
    const reply = json => route.fulfill({ json });
    switch (pathname) {
      case '/api/i18n': return reply({ lang: 'ko', strings: cfg.strings });
      case '/api/auth/state':
        return reply({ authenticated: true, csrf: 'fixture', bridge_up: true, totp_configured: true });
      case '/api/topics':
        return reply({ topics: [{ ...topic, count: entries.length, created_at: stamp, last_ts: stamp }] });
      case '/api/state':
        return reply({ telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, web_sessions: 1 });
      case '/api/masterkey/status':
        return reply({ configured: true, state: 'locked', remaining_seconds: null });
      case '/api/topics/1.2/messages': return reply({ entries, has_more: false });
      default: {
        const match = pathname.match(/\/messages\/message-(\d+)\/attachments(.*)$/);
        if (!match) return reply({ items: [], commands: [], files: [] });
        const [, seq, suffix] = match;
        const filename = images[seq];
        if (suffix.endsWith('/preview')) {
          if (delayed && Number(seq) === delayed.seq) await delayed.ready;
          return route.fulfill({ contentType: 'image/png', body: fixtures.get(filename) });
        }
        const id = 'a'.repeat(32);
        const base = `${pathname}/${id}`;
        return reply({ attachments: [{
          id, size: 2048, mime: 'image/png', name: `${filename}.png`, inline_allowed: true,
          download_url: base, original_url: `${base}?inline=1`, preview_url: `${base}/preview`,
        }] });
      }
    }
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator('#thread [data-entry-seq="1"]').waitFor();
}

const entry = (page, seq) => page.locator(`#thread [data-entry-seq="${seq}"]`);
const image = (page, seq) => entry(page, seq).locator('.attachment-image img');

async function measure(page, seq, name) {
  const preview = image(page, seq);
  await preview.scrollIntoViewIfNeeded();
  await preview.evaluate(img => img.decode());
  const result = await preview.evaluate(img => {
    const bounds = img.getBoundingClientRect();
    const card = img.closest('.attachment-image').getBoundingClientRect();
    const thread = img.closest('#thread');
    return {
      width: bounds.width, height: bounds.height, thread: thread.getBoundingClientRect().height,
      naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight,
      overflow: Math.max(0, thread.scrollWidth - thread.clientWidth,
        document.documentElement.scrollWidth - innerWidth,
        bounds.right - card.right, card.left - bounds.left),
    };
  });
  const [width, height] = cfg.sizes[name];
  result.ratioError = Math.abs(result.width / result.height / (width / height) - 1);
  assert(result.ratioError <= 0.01, JSON.stringify(result));
  assert(result.height <= result.thread * 0.8 + 0.02, JSON.stringify(result));
  assert.equal(result.overflow, 0, JSON.stringify(result));
  assert(result.width <= result.naturalWidth && result.height <= result.naturalHeight);
  return result;
}

async function checkLayout(browser, width, height, theme) {
  const context = await browser.newContext({ viewport: { width, height }, colorScheme: theme });
  try {
    const page = await context.newPage();
    await openChat(page, { 4: 'wide', 8: 'square', 12: 'portrait', 16: 'small' });
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    for (const [i, name] of names.entries()) {
      const seq = (i + 1) * 4;
      const result = await measure(page, seq, name);
      evidence.layouts.push({ viewport: `${width}x${height}`, theme, name, ...result });
      if (width === 1280 && theme === 'light' || width === 375) {
        await page.screenshot({ path: path.join(cfg.shots, `${name}-${width}-${theme}.png`) });
      }
    }
    if (width === 375) {
      const before = await measure(page, 12, 'portrait');
      await page.setViewportSize({ width, height: 680 });
      const after = await measure(page, 12, 'portrait');
      assert(after.thread < before.thread && after.height < before.height);
      evidence.resize = { before, after };
    }
  } finally {
    await context.close();
  }
}

async function checkDelayedLoad(browser, pinned) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const seq = pinned ? 32 : 8;
  const { promise, resolve } = Promise.withResolvers();
  try {
    const page = await context.newPage();
    await openChat(page, { [seq]: 'portrait' }, { seq, ready: promise });
    const anchor = entry(page, 10);
    await image(page, seq).waitFor({ state: 'attached' });
    if (pinned) {
      await page.locator('#thread').evaluate(thread => { thread.scrollTop = thread.scrollHeight; });
    } else {
      await anchor.evaluate(node => {
        const thread = node.closest('#thread');
        thread.scrollTop += node.getBoundingClientRect().top - thread.getBoundingClientRect().top - 5;
      });
    }
    const measureScroll = () => anchor.evaluate(node => {
      const thread = node.closest('#thread');
      return {
        offset: node.getBoundingClientRect().top - thread.getBoundingClientRect().top,
        gap: thread.scrollHeight - thread.clientHeight - thread.scrollTop,
      };
    });
    const placeholder = await image(page, seq).evaluate(img => img.parentElement.getBoundingClientRect().height);
    const before = await measureScroll();
    assert(placeholder >= 100);
    if (pinned) assert(Math.abs(before.gap) <= 1);
    resolve();
    await image(page, seq).evaluate(async img => {
      await img.decode();
      await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
    });
    const after = await measureScroll();
    if (pinned) assert(Math.abs(after.gap) <= 1, JSON.stringify({ before, after }));
    else assert(Math.abs(after.offset - before.offset) <= 1, JSON.stringify({ before, after }));
    evidence.scroll[pinned ? 'pinned' : 'reading'] = { placeholder, before, after };
  } finally {
    resolve();
    await context.close();
  }
}

await fs.mkdir(cfg.shots, { recursive: true });
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const layout of [[1280, 720, 'light'], [1280, 720, 'dark'], [768, 900, 'light'], [375, 812, 'light']]) {
      await checkLayout(browser, ...layout);
    }
    await checkDelayedLoad(browser, false);
    await checkDelayedLoad(browser, true);
  } finally {
    await browser.close();
  }
  await fs.writeFile(path.join(cfg.shots, 'geometry.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify(evidence));
} finally {
  await new Promise(resolve => server.close(resolve));
}
