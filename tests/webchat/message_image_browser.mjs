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
const evidence = { layouts: [], filenames: [], nearLatest: [], resize: {}, scroll: {}, delayed: [], resizing: {} };
const fixtures = new Map(await Promise.all(names.map(async name =>
  [name, await fs.readFile(path.join(cfg.directory, `${name}.png`))],
)));
const topic = {
  tid: '1.2', chat_id: 1, topic_id: 2, name: 'Images', preview: 'Image previews', group: 'telegram', seen: 32,
};
const stamp = '2026-01-01T12:00:00Z';

async function openChat(page, images, { count = 32, delayed, previewDelay, filenames = {}, switchable = false } = {}) {
  const entries = Array.from({ length: count }, (_, i) => {
    const seq = i + 1;
    const name = images[seq] && (filenames[seq] ?? `${images[seq]}.png`);
    return {
      id: `message-${seq}`, seq, ts: stamp, role: 'bot', source: 'web',
      text: name ? `<file:./${name}>` : `Message ${seq}`,
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
      case '/api/topics': {
        const primary = { ...topic, count: entries.length, created_at: stamp, last_ts: stamp };
        const other = { ...primary, tid: '1.3', topic_id: 3, name: 'Other images' };
        return reply({ topics: switchable ? [primary, other] : [primary] });
      }
      case '/api/state':
        return reply({ telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, web_sessions: 1 });
      case '/api/masterkey/status':
        return reply({ configured: true, state: 'locked', remaining_seconds: null });
      case '/api/topics/1.2/messages':
      case '/api/topics/1.3/messages': return reply({ entries, has_more: false });
      default: {
        const match = pathname.match(/\/messages\/message-(\d+)\/attachments(.*)$/);
        if (!match) return reply({ items: [], commands: [], files: [] });
        const [, seq, suffix] = match;
        const filename = images[seq];
        if (suffix.endsWith('/preview')) {
          if (Number(seq) === delayed?.seq) await delayed.ready;
          if (previewDelay) await new Promise(resolve => setTimeout(resolve, previewDelay(Number(seq))));
          return route.fulfill({ contentType: 'image/png', body: fixtures.get(filename) });
        }
        const id = 'a'.repeat(32);
        const base = `${pathname}/${id}`;
        return reply({ attachments: [{
          id, size: 2048, mime: 'image/png', name: filenames[seq] ?? `${filename}.png`, inline_allowed: true,
          download_url: base, original_url: `${base}?inline=1`, preview_url: `${base}/preview`,
        }] });
      }
    }
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await entry(page, Math.max(1, count - 99)).waitFor();
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
    const line = img.closest('.message-attachment').querySelector('.attachment-fileline');
    const filename = line.querySelector('.attachment-filename');
    const text = document.createRange();
    text.selectNodeContents(filename);
    const style = getComputedStyle(line);
    const filelineWidth = parseFloat(getComputedStyle(filename.parentElement).minWidth) +
      line.querySelector('.attachment-download').getBoundingClientRect().width +
      parseFloat(style.columnGap) + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const filenameBounds = filename.getBoundingClientRect();
    const lines = new Set([...text.getClientRects()].filter(rect => rect.top < filenameBounds.bottom)
      .map(rect => rect.top));
    return {
      filename: filename.textContent, title: filename.getAttribute('title'),
      cardWidth: card.width, gutter: card.width - bounds.width,
      filelineWidth, filenameLines: lines.size,
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
    await openChat(page, { 4: 'wide', 8: 'square', 12: 'portrait', 16: 'small' }, {
      filenames: { 16: 'a.png' },
    });
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

async function checkFilenames(browser, width, height) {
  const context = await browser.newContext({ viewport: { width, height }, colorScheme: 'light' });
  try {
    const page = await context.newPage();
    const samples = [
      'KakaoTalk_Photo_2026-11-23-13-58-13_001.png',
      'Screenshot 2026-11-23 at 1.58.13 PM meeting notes final copy.png',
      'small-icon.png',
      'small.png',
    ];
    const images = {}, filenames = {};
    for (const [index, filename] of samples.entries()) {
      for (const [offset, name] of ['portrait', 'small'].entries()) {
        const seq = index * 6 + offset * 3 + 2;
        images[seq] = name;
        filenames[seq] = filename;
      }
    }
    await openChat(page, images, { filenames });
    await page.evaluate(() => document.fonts.ready);
    for (const [seq, name] of Object.entries(images)) {
      const result = await measure(page, seq, name);
      evidence.filenames.push({ viewport: `${width}x${height}`, name, ...result });
      if (name === 'portrait' && result.filename === samples[0] ||
          name === 'small' && result.filename === samples[1]) {
        await entry(page, seq).locator('.message-attachment').scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(cfg.shots, `longname-${name}-${width}-light.png`) });
      }
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
    await openChat(page, { [seq]: 'portrait' }, { delayed: { seq, ready: promise } });
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

const gap = page => page.locator('#thread').evaluate(
  node => node.scrollHeight - node.clientHeight - node.scrollTop,
);
const afterLayout = page => page.evaluate(
  () => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))),
);

async function checkNearLatest(browser, initialGap) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const { promise, resolve } = Promise.withResolvers();
  try {
    const page = await context.newPage();
    await openChat(page, { 32: 'portrait' }, { delayed: { seq: 32, ready: promise } });
    const preview = image(page, 32);
    await preview.waitFor({ state: 'attached' });
    await page.locator('#thread').evaluate((thread, offset) => {
      thread.scrollTop = thread.scrollHeight - thread.clientHeight - offset;
    }, initialGap);
    await afterLayout(page);
    assert.equal(await gap(page), initialGap);
    const anchor = entry(page, 28);
    const offset = () => anchor.evaluate(node =>
      node.getBoundingClientRect().top - node.closest('#thread').getBoundingClientRect().top);
    const before = await offset();
    resolve();
    await preview.evaluate(img => img.decode());
    await afterLayout(page);
    const loaded = await gap(page);
    const button = await page.locator('#threadBottom').isVisible();
    const shift = await offset() - before;
    await page.evaluate(async () => {
      const { handleEvent } = await import('/static/js/chat.js');
      await handleEvent({ type: 'entry', tid: '1.2', entry: {
        id: 'message-33', seq: 33, ts: '2026-01-01T12:00:00Z', role: 'bot',
        source: 'web', text: 'Message 33',
      } });
    });
    await afterLayout(page);
    evidence.nearLatest.push({ initialGap, loaded, button, shift, delivered: await gap(page) });
  } finally {
    resolve();
    await context.close();
  }
}

async function checkManyImages(browser, delay, round) {
  const viewport = delay === 'shuffled' ? { width: 375, height: 812 } : { width: 1280, height: 720 };
  const context = await browser.newContext({ viewport });
  try {
    const page = await context.newPage();
    const previews = new Set();
    page.on('requestfinished', request => {
      if (request.url().endsWith('/preview')) previews.add(request.url());
    });
    const images = Object.fromEntries(Array.from({ length: 60 }, (_, i) => [
      (i + 1) * 2, delay === 'shuffled' ? names[i % names.length] : i % 2 ? 'portrait' : 'square',
    ]));
    const previewDelay = seq => delay === 'shuffled' ? 30 + (seq * 73 + round * 17) % 111 : delay;
    await openChat(page, images, { count: 120, previewDelay });
    await page.waitForTimeout(2500);
    await page.waitForLoadState('networkidle');
    assert(previews.size > 1);
    const opened = await gap(page);
    let delivered = null;
    if (delay === 'shuffled' && opened <= 1) {
      images[121] = 'square';
      await page.evaluate(async () => {
        const { handleEvent } = await import('/static/js/chat.js');
        await handleEvent({ type: 'entry', tid: '1.2', entry: {
          id: 'message-121', seq: 121, ts: '2026-01-01T12:00:00Z', role: 'bot',
          source: 'web', text: '<file:./square.png>', has_attachments: true,
        } });
      });
      await image(page, 121).evaluate(img => img.decode());
      await afterLayout(page);
      delivered = await gap(page);
    }
    evidence.delayed.push({ delay, round, opened, delivered, previews: previews.size });
  } finally {
    await context.close();
  }
}

async function checkResizing(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  try {
    const page = await context.newPage();
    await openChat(page, { 28: 'square', 30: 'portrait', 32: 'square' });
    for (const seq of [28, 30, 32]) await image(page, seq).evaluate(img => img.decode());
    await page.locator('#thread').evaluate(thread => { thread.scrollTop = thread.scrollHeight; });
    await afterLayout(page);
    const input = page.locator('#msgIn');
    await input.fill(Array(8).fill('Compose a message').join('\n'));
    await afterLayout(page);
    const expanded = await gap(page);
    await input.clear();
    await afterLayout(page);
    const cleared = await gap(page);
    await page.locator('#thread').evaluate(thread => { thread.scrollTop = thread.scrollHeight; });
    await afterLayout(page);
    await page.setViewportSize({ width: 1280, height: 500 });
    await afterLayout(page);
    const shortened = await gap(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    await afterLayout(page);
    const restored = await gap(page);
    evidence.resizing = { expanded, cleared, shortened, restored };
  } finally {
    await context.close();
  }
}

async function checkHeightTransitions(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  try {
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.imagePhase = 'firstLoad';
      window.imageFrames = [];
      function frame() {
        const thread = document.querySelector('#thread');
        const img = thread?.querySelector('.attachment-image img');
        if (img) {
          const phase = window.imagePhase;
          // These rectangles come from the rendering step after ResizeObserver delivery.
          const observer = new IntersectionObserver(entries => {
            observer.disconnect();
            const height = entries.find(entry => entry.target === img)?.boundingClientRect.height;
            const threadHeight = entries.find(entry => entry.target === thread)?.boundingClientRect.height;
            if (!height || !threadHeight || !img.naturalHeight) return;
            const expected = Math.min(img.naturalHeight, threadHeight * 0.8);
            window.imageFrames.push({
              phase, height, threadHeight, expected,
              hasSize: !!thread.style.getPropertyValue('--thread-height'),
            });
          });
          observer.observe(thread);
          observer.observe(img);
        }
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
    await openChat(page, { 32: 'portrait' }, { switchable: true });
    const preview = image(page, 32);
    const current = async () => {
      await preview.evaluate(img => img.decode());
      await page.waitForFunction(() => window.imageFrames.some(frame => frame.phase === window.imagePhase));
      await afterLayout(page);
      return preview.evaluate(img => ({
        image: img.getBoundingClientRect().height,
        thread: img.closest('#thread').getBoundingClientRect().height,
        overflow: document.scrollingElement.scrollHeight - innerHeight,
      }));
    };
    const stage = async (phase, change) => {
      await page.evaluate(phase => { window.imagePhase = phase; }, phase);
      await change();
      return current();
    };
    const input = page.locator('#msgIn');
    const firstLoad = await current();
    const composerExpanded = await stage('composerExpanded', () => input.fill('Draft\n'.repeat(7) + 'Draft'));
    const heightChanged = await stage('heightChanged', () => page.setViewportSize({ width: 1280, height: 650 }));
    const widthChanged = await stage('widthChanged', () => page.setViewportSize({ width: 1000, height: 650 }));
    const short = await stage('short', () => page.setViewportSize({ width: 1000, height: 600 }));
    const mobile = await stage('mobile', () => page.setViewportSize({ width: 375, height: 812 }));
    await input.clear();
    await page.setViewportSize({ width: 1280, height: 720 });
    const topicChanged = await stage('topicChanged', async () => {
      await page.locator('#tlist [data-tid="1.3"]').click();
      await page.waitForFunction(() =>
        document.querySelector('#thread .attachment-image img')?.src.includes('/topics/1.3/'));
    });
    assert(composerExpanded.image < firstLoad.image);
    assert(heightChanged.image < composerExpanded.image);

    const frames = await page.evaluate(() => window.imageFrames);
    assert(frames.length > 0);
    const invalid = frames.filter(frame => Math.abs(frame.height - frame.expected) > 1);
    assert.deepEqual(invalid, [], 'Image height differed from the thread cap during a frame');
    if (frames.some(frame => frame.hasSize)) {
      assert(frames.every(frame => frame.hasSize), 'A decoded preview was laid out before sizing was initialized');
    }
    const fallback = await preview.evaluate(img => {
      const probe = img.parentElement.cloneNode(true);
      probe.style.cssText = 'position:absolute;visibility:hidden';
      document.body.append(probe);
      const size = parseFloat(getComputedStyle(probe.querySelector('img')).maxHeight);
      probe.remove();
      return size;
    });
    if (frames.some(frame => frame.hasSize)) assert(Math.abs(fallback - 0.8 * 720) <= 1);
    evidence.transitions = { firstLoad, composerExpanded, heightChanged, widthChanged, short, mobile, topicChanged,
      frames: frames.length, framesBeforeSize: frames.filter(frame => !frame.hasSize).length, fallback };
  } finally {
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
    for (const viewport of [[1280, 720], [375, 812]]) await checkFilenames(browser, ...viewport);
    for (const initialGap of [0, 3, 40, 79, 200]) await checkNearLatest(browser, initialGap);
    await checkDelayedLoad(browser, false);
    await checkDelayedLoad(browser, true);
    for (const delay of [30, 100, 'shuffled']) {
      for (let round = 1; round <= 5; round++) await checkManyImages(browser, delay, round);
    }
    await checkResizing(browser);
    await checkHeightTransitions(browser);
  } finally {
    await browser.close();
  }
  await fs.writeFile(path.join(cfg.shots, 'geometry.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify(evidence));
  const gaps = [...evidence.delayed.flatMap(row => [row.opened, row.delivered ?? 0]),
    ...Object.values(evidence.resizing)];
  assert(gaps.every(value => Math.abs(value) <= 1), 'Latest message lost its pinned position');
  for (const row of evidence.filenames) {
    assert(row.filenameLines <= 2, JSON.stringify(row));
    assert.equal(row.title, row.filename);
    assert(Math.abs(row.cardWidth - Math.max(row.width, row.filelineWidth)) <= 1, JSON.stringify(row));
  }
  for (const row of evidence.nearLatest) {
    if (row.initialGap < 80) {
      assert(!row.button && row.delivered <= 2, JSON.stringify(row));
    } else {
      assert(Math.abs(row.shift) <= 1, JSON.stringify(row));
    }
  }
  for (const row of evidence.layouts) {
    if (row.name === 'small') {
      assert(row.gutter > 0);
      assert.equal(row.filenameLines, 1);
      assert(Math.abs(row.cardWidth - row.filelineWidth) <= 1, JSON.stringify(row));
    } else {
      assert(Math.abs(row.gutter) <= 1, JSON.stringify(row));
    }
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
