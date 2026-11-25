import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const { chromium } = await import(pathToFileURL(path.resolve(process.env.DTW_PLAYWRIGHT_MODULE)));
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };
const server = http.createServer(async (request, response) => {
  const { pathname } = new URL(request.url, 'http://localhost');
  const name = pathname === '/' ? 'index.html' : pathname.replace(/^\/static\//, '');
  try {
    const body = await fs.readFile(path.join(config.static, name));
    const csp = name === 'starflow/base-stars.html' ? config.starflowCsp : config.csp;
    response.writeHead(200, { 'Content-Type': mime[path.extname(name)] || 'application/octet-stream', 'Content-Security-Policy': csp });
    response.end(body);
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

const beforeMidnight = '2025-12-31T14:59:00.000Z';
const afterMidnight = '2025-12-31T15:00:00.000Z';
const today = '2026-01-01T15:01:00.000Z';
const topic = {
  tid: '1.2', chat_id: 1, topic_id: 2, name: 'Message dates', group: 'telegram',
  count: 240, unread: 240, imported_count: 0, created_at: beforeMidnight, last_ts: today,
  preview: 'Please compare current messages with earlier messages.',
  read_only: false, renamable: true, deletable: true, busy: false,
};
const messages = Array.from({ length: topic.count }, (_, index) => ({
  id: `entry-${index + 1}`, seq: index + 1,
  ts: index < 74 ? beforeMidnight : index < 194 ? afterMidnight : today,
  role: index % 2 ? 'bot' : 'user', source: index % 3 ? 'web' : 'telegram',
  text: index % 2 ? 'Here is the point where the date changes in the conversation.' : 'Please compare current messages with earlier messages.',
  has_attachments: false,
}));
messages[74].role = 'event';
messages[74].kind = 'background';
async function mockChat(page, entries = messages, fixture = topic, onSocket = () => {}) {
  const fixtureTopic = { ...fixture, count: entries.length, unread: entries.length, last_ts: entries.at(-1).ts };
  await page.routeWebSocket('**/api/ws', onSocket);
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const fulfill = (data, status = 200) => route.fulfill({ status, json: data });
    switch (url.pathname) {
      case '/api/i18n': {
        const lang = url.searchParams.get('lang') || 'ko';
        return fulfill({ lang, strings: config.locales[lang] });
      }
      case '/api/auth/state':
        return fulfill({ authenticated: true, csrf: 'fixture', bridge_up: true, password_configured: true, totp_configured: true });
      case '/api/topics': return fulfill({ topics: [fixtureTopic] });
      case '/api/state': return fulfill({ telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, web_sessions: 1 });
      case '/api/outbox': return fulfill({ items: [] });
      case '/api/commands': return fulfill({ commands: [] });
      case '/api/files': return fulfill({ files: [] });
      case '/api/masterkey/status': return fulfill({ state: 'unconfigured', remaining_seconds: null });
      case '/api/topics/1.2/seen': {
        fixtureTopic.seen = Math.max(fixtureTopic.seen ?? -Infinity, request.postDataJSON().seq);
        fixtureTopic.unread = entries.filter(entry => entry.seq > fixtureTopic.seen).length;
        return fulfill({ seen: fixtureTopic.seen });
      }
      case '/api/topics/1.2/messages': {
        if (request.method() === 'POST') return fulfill({ turn_id: 'test-turn', phase: 'queued' });
        const before = Number(url.searchParams.get('before') ?? Infinity);
        const eligible = entries.filter(entry => entry.seq < before);
        return fulfill({ entries: eligible.slice(-100), has_more: eligible.length > 100 });
      }
      default: return fulfill({});
    }
  });
}

async function scrollPosition(page, gap) {
  return page.evaluate(async distance => {
    const thread = document.querySelector('#thread');
    if (distance !== undefined) thread.scrollTop = thread.scrollHeight - thread.clientHeight - distance;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const bounds = thread.getBoundingClientRect();
    const anchor = [...thread.querySelectorAll('[data-entry-seq]')].find(row => row.getBoundingClientRect().bottom > bounds.top);
    return {
      gap: thread.scrollHeight - thread.clientHeight - thread.scrollTop,
      seq: anchor.dataset.entrySeq,
      y: anchor.getBoundingClientRect().top,
    };
  }, gap);
}

async function calendarScroll(browser) {
  const entries = messages.slice(0, 40);
  const viewport = { width: 375, height: 667 };
  const oldDay = config.locales.ko['chat.today'];
  for (const gap of [0, 48, 64, 96]) {
    const context = await browser.newContext({ viewport, timezoneId: 'Asia/Seoul' });
    const page = await context.newPage();
    await page.clock.install({ time: new Date('2025-12-31T14:59:30.000Z') });
    await mockChat(page, entries);
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelectorAll('[data-entry-seq]').length === 40);
    await page.evaluate(() => document.fonts.ready);
    const before = await scrollPosition(page, gap);
    assert.equal(before.gap, gap);
    assert.equal(await page.locator('#thread .day').textContent(), oldDay);

    const remaining = Date.parse(afterMidnight) - await page.evaluate(() => Date.now());
    await page.clock.runFor(remaining + 1200);
    const date = await page.evaluate(ts => new Date(ts).toLocaleDateString('ko', { dateStyle: 'medium' }), beforeMidnight);
    assert.equal(await page.locator('#thread .day').textContent(), date);
    assert.deepEqual(await scrollPosition(page), before, `midnight at ${gap}px from latest`);
    await context.close();
  }

  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date(afterMidnight));
  await mockChat(page, entries);
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelectorAll('[data-entry-seq]').length === 40);
  await page.evaluate(() => document.fonts.ready);
  const before = await scrollPosition(page, 64);
  assert.equal(before.gap, 64);
  const stamp = page.locator('#thread [data-entry-seq="40"] time');
  const oldStamp = await stamp.textContent();
  const oldZone = await page.evaluate(() => new Intl.DateTimeFormat().resolvedOptions().timeZone);
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setTimezoneOverride', { timezoneId: oldZone === 'UTC' ? 'Asia/Seoul' : 'UTC' });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForFunction(text => document.querySelector('#thread [data-entry-seq="40"] time').textContent !== text, oldStamp);
  assert.deepEqual(await scrollPosition(page), before, 'timezone refresh on focus');

  const historyRefresh = page.waitForResponse(response =>
    response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith('/messages'));
  await page.evaluate(() => {
    window.resumeRow = document.querySelector('#thread [data-entry-seq="40"]');
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await (await historyRefresh).finished();
  assert.deepEqual(await scrollPosition(page), before, 'history resync after returning to the chat');
  assert(await page.evaluate(() => document.querySelector('#thread [data-entry-seq="40"]') === window.resumeRow));
  await context.close();
}

async function preserveOldestGroup(browser) {
  for (const split of [false, true]) {
    const entries = messages.slice(0, 100).map((entry, index) => ({
      ...entry, role: 'bot', ts: split && index < 30 ? beforeMidnight : afterMidnight,
    }));
    const seq = split ? 30 : 100;
    entries[seq - 1].text = 'Keep this selection.\n\n```text\n' + 'long code '.repeat(100) + '\n```';
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: 'Asia/Seoul' });
    const page = await context.newPage();
    await page.clock.setFixedTime(new Date(afterMidnight));
    await mockChat(page, entries);
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelectorAll('[data-entry-seq]').length === 100);
    const result = await page.evaluate(async ({ seq, latest, split }) => {
      const { handleEvent } = await import('/static/js/chat.js');
      const row = document.querySelector(`[data-entry-seq="${seq}"]`);
      const code = row.querySelector('pre > code');
      const range = document.createRange();
      range.selectNodeContents(row.querySelector('.md p'));
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      code.scrollLeft = 200;
      const before = { selection: window.getSelection().toString(), scrollLeft: code.scrollLeft };
      if (split) {
        await handleEvent({ type: 'turn', tid: '1.2', turn_id: 'live-turn', phase: 'started' });
      } else {
        await handleEvent({ type: 'entry', tid: '1.2', entry: { ...latest, id: 'entry-101', seq: 101 } });
      }
      return {
        before,
        after: { selection: window.getSelection().toString(), scrollLeft: code.scrollLeft },
        first: document.querySelector('[data-entry-seq]').dataset.entrySeq,
      };
    }, { seq, latest: entries.at(-1), split });
    assert.equal(result.first, '2');
    assert.deepEqual(result.before, { selection: 'Keep this selection.', scrollLeft: 200 });
    assert.deepEqual(result.after, result.before, split ? 'live turn in two date groups' : 'new entry in one date group');
    await context.close();
  }
}

async function chatFocus(browser) {
  for (const [width, hasTouch, readOnly] of [[375, true, false], [1280, false, true]]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: hasTouch, hasTouch });
    const page = await context.newPage();
    await mockChat(page, messages.slice(0, 5), { ...topic, read_only: readOnly });
    await page.goto(origin);
    await page.locator('[data-entry-seq="5"]').waitFor();
    const heading = page.locator('#chatTitle');
    await page.reload();
    await page.locator('[data-entry-seq="5"]').waitFor();
    const restored = await heading.evaluate(node => ({
      focused: node === document.activeElement, outline: getComputedStyle(node).outlineStyle,
    }));
    assert.deepEqual(restored, { focused: false, outline: 'none' });
    if (hasTouch) await page.locator('.chat-head [data-route="list"]').tap();
    else await page.evaluate(async () => (await import('/static/js/router.js')).go('manage'));
    const route = hasTouch ? '#list' : '#manage';
    await page.waitForFunction(hash => location.hash === hash, route);
    await page.evaluate(async () => (await import('/static/js/router.js')).go('chat'));
    await page.waitForFunction(() => location.hash === '#chat' && !document.body.classList.contains('m-list'));
    const afterRoute = await heading.evaluate(node => node === document.activeElement);
    await page.screenshot({ path: path.join(config.shots, `focus-${width}.png`) });

    if (hasTouch) await page.locator('.chat-head [data-route="list"]').tap();
    const cell = page.locator('#tlist [data-tid="1.2"]');
    if (hasTouch) await cell.tap();
    else await cell.click();
    await page.waitForFunction(() => location.hash === '#chat' && !document.body.classList.contains('m-list'));
    const expected = hasTouch ? page.locator('#msgIn') : heading;
    const afterSelection = await expected.evaluate(node => node === document.activeElement);
    console.log(JSON.stringify({ width, restored, afterRoute, afterSelection }));
    assert.ok(afterRoute, `${width}px chat route focuses the heading`);
    assert.ok(afterSelection, `${width}px topic selection focuses the expected element`);
    if (readOnly) {
      const delayedTopic = { ...topic, tid: '1.3', topic_id: 3, name: 'Delayed topic', read_only: true };
      const unreadTopic = { ...delayedTopic, tid: '1.4', topic_id: 4, name: 'Unread topic', count: 1, unread: 1 };
      await page.route('**/api/topics', route => route.fulfill({
        json: { topics: [{ ...topic, read_only: true }, delayedTopic, unreadTopic] },
      }));
      await page.route(/\/api\/topics\/1\.4\/messages\?/, route =>
        route.fulfill({ json: { entries: [messages[6]], has_more: false } }));
      await page.route('**/api/topics/1.4/seen', route => {
        unreadTopic.seen = route.request().postDataJSON().seq;
        unreadTopic.unread = 0;
        return route.fulfill({ json: { seen: unreadTopic.seen } });
      });
      await page.evaluate(async () => (await import('/static/js/topics.js')).refreshTopics());

      const unreadCell = page.locator('#tlist [data-tid="1.4"]');
      await unreadCell.locator('.unread').waitFor();
      const seen = page.waitForRequest(request => request.url().endsWith('/api/topics/1.4/seen') && request.method() === 'PUT');
      await unreadCell.click();
      await seen;
      await page.locator('[data-entry-seq="7"]').waitFor();
      assert.equal(await heading.evaluate(node => node === document.activeElement), true, 'unread topic selection focuses the heading');

      assert.equal(await unreadCell.locator('.unread').count(), 0);
      await unreadCell.click();
      await page.waitForFunction(() => document.activeElement?.id === 'chatTitle');

      let releaseHistory;
      const waitForRelease = new Promise(resolve => { releaseHistory = resolve; });
      const historyUrl = /\/api\/topics\/1\.3\/messages\?/;
      await page.route(historyUrl, async route => {
        await waitForRelease;
        await route.fulfill({ json: { entries: [messages[5]], has_more: false } });
      });
      const historyRequest = page.waitForRequest(historyUrl);
      const search = page.locator('#q');
      await page.locator('#tlist [data-tid="1.3"]').click();
      await historyRequest;
      await search.fill('draft search');
      releaseHistory();
      await page.locator('[data-entry-seq="6"]').waitFor();
      assert.deepEqual(await search.evaluate(node => ({
        focused: node === document.activeElement, value: node.value,
      })), { focused: true, value: 'draft search' }, 'history completion preserves the active search');
    }
    await context.close();
  }
}

async function messageDates(browser) {
  const measurements = [];
  for (const width of [375, 768, 1280]) {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: width === 1280 ? 900 : 800 }, timezoneId: 'Asia/Seoul', colorScheme: theme });
      const page = await context.newPage();
      await page.clock.setFixedTime(new Date(today));
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await mockChat(page);
      await page.goto(origin);
      await page.waitForFunction(() => document.querySelectorAll('[data-entry-seq]').length === 100);
      await page.evaluate(() => document.fonts.ready);
      const first = () => page.locator('[data-entry-seq]').first().getAttribute('data-entry-seq');
      assert.equal(await first(), '141');
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);

      const switchWindow = async upward => {
        const snapshot = await page.evaluate(up => {
          const thread = document.querySelector('#thread');
          thread.scrollTop = up ? 0 : thread.scrollHeight;
          const rows = [...thread.querySelectorAll('[data-entry-seq]')], top = thread.getBoundingClientRect().top;
          window.testAnchor = rows.find(row => row.getBoundingClientRect().bottom > top);
          return { first: rows[0].dataset.entrySeq, top: window.testAnchor.getBoundingClientRect().top };
        }, upward);
        await page.waitForFunction(seq => document.querySelector('[data-entry-seq]').dataset.entrySeq !== seq, snapshot.first);
        const drift = await page.evaluate(top => window.testAnchor.isConnected ? window.testAnchor.getBoundingClientRect().top - top : null, snapshot.top);
        assert.equal(drift, 0, `${width}/${theme}, ${upward ? 'older' : 'newer'} window`);
        assert.equal(await page.locator('[data-entry-seq]').count(), 100);
        return drift;
      };

      const navigate = async id => page.locator(`#${id}`).click();
      await navigate('threadTop');
      await page.waitForFunction(() => document.querySelector('[data-entry-seq]').dataset.entrySeq === '1');
      const stamp = seq => page.locator(`[data-entry-seq="${seq}"] time`);
      const labelFor = seq => page.locator(`[data-entry-seq="${seq}"]`).locator('..').locator('.day');
      assert.equal(await stamp(74).getAttribute('datetime'), beforeMidnight);
      assert.equal(await stamp(75).getAttribute('datetime'), afterMidnight);
      for (const locale of ['ko', 'en', 'de']) {
        await page.evaluate(async lang => (await import('/static/js/i18n.js')).loadI18n(lang), locale);
        for (const [seq, ts] of [[74, beforeMidnight], [75, afterMidnight]]) {
          const [day, compactDay, clock] = await page.evaluate(([value, lang]) => {
            const date = new Date(value);
            const dateLabel = date.toLocaleDateString(lang, { dateStyle: 'medium' });
            return [
              dateLabel,
              date.toLocaleDateString(lang, { month: 'numeric', day: 'numeric' }),
              date.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' }),
            ];
          }, [ts, locale]);
          assert.equal(await labelFor(seq).textContent(), day);
          assert.equal(await stamp(seq).innerText(), `${width <= 760 ? compactDay : day} · ${clock}`);
        }
      }
      await page.evaluate(async () => (await import('/static/js/i18n.js')).loadI18n('ko'));

      const readDay = async seq => page.evaluate(async number => {
        const thread = document.querySelector('#thread');
        const bounds = thread.getBoundingClientRect();
        const inset = parseFloat(getComputedStyle(thread).paddingTop);
        const row = thread.querySelector(`[data-entry-seq="${number}"]`);
        thread.scrollTop += row.getBoundingClientRect().top - bounds.top - inset - 38;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const rows = [...thread.querySelectorAll('[data-entry-seq]')];
        const firstVisible = rows.find(message => message.getBoundingClientRect().bottom > bounds.top);
        const group = firstVisible.parentElement;
        const divider = group.firstElementChild;
        return {
          groupDay: divider.textContent,
          firstDay: firstVisible.querySelector('time .tm-full').textContent,
          pinned: divider.hasAttribute('data-pinned') && Math.abs(divider.getBoundingClientRect().top - bounds.top - inset) < 1,
          messageClear: row.getBoundingClientRect().top >= divider.getBoundingClientRect().bottom,
          positions: [thread.scrollTop, bounds.top, divider.getBoundingClientRect().top, divider.getBoundingClientRect().bottom, row.getBoundingClientRect().top],
          sticky: getComputedStyle(divider).position,
        };
      }, seq);
      const december = await readDay(65), january = await readDay(84);
      assert.notEqual(december.groupDay, january.groupDay);
      for (const result of [december, january]) {
        assert.equal(result.groupDay, result.firstDay);
        assert.ok(result.pinned && result.messageClear, JSON.stringify(result));
      }
      if (width === 375) {
        const result = await page.evaluate(() => {
          const thread = document.querySelector('#thread');
          const button = document.querySelector('#threadBottom').getBoundingClientRect();
          const bounds = thread.getBoundingClientRect();
          const visibleTimes = [...thread.querySelectorAll('time')].map(time => time.getBoundingClientRect())
            .filter(time => time.bottom > bounds.top && time.top < bounds.bottom);
          return {
            scrollableBesideButton: document.elementFromPoint(bounds.left + 36, button.top + 22)?.closest('#thread') === thread,
            overlaps: visibleTimes.filter(time => time.right > button.left && time.top < button.bottom && time.bottom > button.top).length,
          };
        });
        assert.ok(result.scrollableBesideButton, 'mobile scrolling reaches the area beside navigation');
        assert.equal(result.overlaps, 0);
      }
      await readDay(72);
      await page.screenshot({ path: path.join(config.shots, `dates-a-${width}-${theme}.png`) });

      const newer = await switchWindow(false);
      const older = await switchWindow(true);
      await navigate('threadBottom');
      await page.waitForFunction(() => document.querySelector('[data-entry-seq="240"]'));
      assert.equal(await labelFor(240).textContent(), config.locales.ko['chat.today']);

      await page.locator('#msgIn').fill('Queued message');
      await page.locator('#sendBtn').click();
      const queued = page.locator('[data-queue-turn="test-turn"] time');
      await queued.waitFor();
      assert.equal(await queued.getAttribute('datetime'), today);
      assert.equal(await queued.textContent(), await stamp(240).textContent());
      await page.evaluate(async () => {
        const { handleEvent } = await import('/static/js/chat.js');
        await handleEvent({ type: 'turn', tid: '1.2', turn_id: 'test-turn', phase: 'started' });
        await handleEvent({ type: 'turn', tid: '1.2', turn_id: 'test-turn', phase: 'done' });
      });
      const completed = page.locator('[data-turn="test-turn"] time');
      assert.equal(await completed.getAttribute('datetime'), today);
      assert.equal(await completed.textContent(), await stamp(240).textContent());
      assert.deepEqual(errors, []);
      measurements.push({ width, theme, older, newer });
      await context.close();
    }
  }
  await calendarScroll(browser);
  console.log(JSON.stringify(measurements));
}

async function documentOverflow(browser, scenario) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  try {
    const page = await context.newPage();
    const paragraph = 'A paragraph in a long conversation.\n\n';
    const entries = messages.slice(0, 16).map(entry => ({ ...entry, text: paragraph.repeat(5) }));
    await mockChat(page, entries);
    await page.goto(origin);
    await page.waitForFunction(count => document.querySelectorAll('#thread [data-entry-seq]').length === count, entries.length);
    await page.evaluate(() => document.fonts.ready);

    await page.evaluate(async ({ scenario, paragraph, tid }) => {
      const { handleEvent } = await import('/static/js/chat.js');
      const turn = { tid, turn_id: 'overflow-turn' };
      const thread = document.querySelector('#thread');
      await handleEvent({ ...turn, type: 'turn', phase: 'started' });
      await handleEvent({
        ...turn, type: 'delta', text: scenario === 'overflow-footnote' ? paragraph.repeat(250) : 'Short response.',
      });
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      thread.scrollTop = 0;
      if (scenario === 'overflow-footnote') {
        await handleEvent({ ...turn, type: 'delta', text: 'Citation[^ref].\n\n[^ref]: Example source.\n\n' });
      } else {
        await handleEvent({ ...turn, type: 'tool', name: 'Read' });
        await handleEvent({ ...turn, type: 'turn', phase: 'done' });
      }
    }, { scenario, paragraph, tid: topic.tid });

    const selector = scenario === 'overflow-footnote'
      ? '#streamMsg .footnotes > h2.sr-only'
      : '[data-turn="overflow-turn"] .activity-trace summary > span.sr-only';
    await page.locator(selector).waitFor({ state: 'attached' });
    const { height, viewport } = await page.evaluate(() => ({
      height: document.scrollingElement.scrollHeight,
      viewport: window.innerHeight,
    }));
    assert.equal(height, viewport, `${scenario} expanded the document past the viewport`);
  } finally {
    await context.close();
  }
}

async function copyOpenBlockWithMouse(page, event) {
  const turnId = 'pointer-copy';
  await event({ type: 'delta', turn_id: turnId, text: '~~~text\nStreaming code' });
  const button = page.locator('[data-turn="pointer-copy"] [data-copy-code]');
  await button.waitFor();
  await button.scrollIntoViewIfNeeded();
  await page.evaluate(() => {
    window.copyToasts = 0;
    const toasts = document.querySelector('#toasts');
    toasts.replaceChildren();
    new MutationObserver(records => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node.querySelector?.('[data-i18n="common.copied"]')) window.copyToasts++;
        }
      }
    }).observe(toasts, { childList: true });
    document.addEventListener('pointerdown', event => {
      window.pressedCopyButton = event.target.closest('[data-copy-code]');
      window.pressedCopyTurn = window.pressedCopyButton?.closest('[data-turn]');
    }, true);
    document.addEventListener('pointerup', event => {
      const button = event.target.closest('[data-copy-code]');
      window.copyButtonReplaced = Boolean(window.pressedCopyButton && !window.pressedCopyButton.isConnected);
      window.releasedCode = button?.nextElementSibling.textContent.replace(/\r?\n$/, '');
    }, true);
  });

  async function copyAfterUpdate(update) {
    const before = await page.evaluate(async () => {
      await navigator.clipboard.writeText('Waiting for copy');
      document.querySelector('#toasts').replaceChildren();
      return window.copyToasts;
    });
    const bounds = await button.boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await update();
    await page.mouse.up();
    assert(await page.evaluate(() => window.releasedCode), 'mouse release lands on a code button');
    const replaced = await page.evaluate(() => window.copyButtonReplaced);
    await page.waitForFunction(async () => {
      return window.releasedCode && (await navigator.clipboard.readText()) === window.releasedCode;
    }, null, { timeout: 2000 });
    await page.waitForFunction(count => window.copyToasts > count, before, { timeout: 2000 });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(() => window.copyToasts), before + 1, 'one toast per click');
    return replaced;
  }

  const results = [];
  for (const interval of [30, 100, 250]) {
    let copied = 0, replaced = 0;
    for (let attempt = 0; attempt < 10; attempt++) {
      const updated = await copyAfterUpdate(() => page.evaluate(async ({ turnId, interval, phase }) => {
        const { handleEvent } = await import('/static/js/chat.js');
        const append = () => handleEvent({ tid: '1.2', turn_id: turnId, type: 'delta', text: ' more' });
        let updates;
        const first = setTimeout(() => {
          void append();
          updates = setInterval(append, interval);
        }, phase);
        await new Promise(resolve => setTimeout(resolve, 80));
        clearTimeout(first);
        clearInterval(updates);
      }, { turnId, interval, phase: attempt * interval / 10 }));
      if (updated) replaced++;
      copied++;
    }
    assert(replaced > 0, `a render replaces the pressed button at ${interval}ms`);
    results.push({ interval, copied, replaced });
  }

  const replacedByTool = await copyAfterUpdate(async () => {
    const thread = page.locator('#thread');
    const top = await thread.evaluate(node => node.scrollTop);
    await event({ type: 'delta', turn_id: turnId, text: ' after delta' });
    await event({ type: 'tool', turn_id: turnId, name: 'Read' });
    await page.waitForFunction(() => {
      const pressedRow = window.pressedCopyTurn;
      const row = document.querySelector('[data-turn="pointer-copy"]');
      return pressedRow && !pressedRow.isConnected && row?.querySelector('.activity-line')?.textContent.includes('Read');
    });
    // Restore the scroll position so the release hits the new button at the original coordinates.
    await thread.evaluate((node, top) => { node.scrollTop = top; }, top);
  });
  assert(replacedByTool, 'a tool event replaces the pressed button and its turn row');
  assert(await page.evaluate(() => window.releasedCode.endsWith(' after delta')));

  const nextTurn = 'pointer-copy-two-blocks';
  const firstCode = 'echo AAAAAAAAAA', secondCode = 'echo BBBBBBBBBB';
  await event({ type: 'delta', turn_id: nextTurn, text: '~~~sh\n' + firstCode });
  const next = page.locator('[data-turn="pointer-copy-two-blocks"] [data-copy-code]');
  await next.first().scrollIntoViewIfNeeded();
  const before = await page.evaluate(async () => {
    await navigator.clipboard.writeText('Waiting for copy');
    document.querySelector('#toasts').replaceChildren();
    return window.copyToasts;
  });
  const bounds = await next.first().boundingBox();
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  assert.equal(await page.evaluate(() => window.pressedCopyButton?.nextElementSibling.textContent.trim()), firstCode);
  await event({ type: 'delta', turn_id: nextTurn, text: '\n~~~\n\n~~~sh\n' + secondCode });
  await next.nth(1).waitFor();
  await page.mouse.up();
  assert(await page.evaluate(() => window.copyButtonReplaced), 'the pressed button was replaced');
  assert.equal(await page.evaluate(() => window.releasedCode), secondCode, 'auto-scroll puts the second block under the mouse');
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'Waiting for copy');
  assert.equal(await page.evaluate(() => window.copyToasts), before, 'releasing on a different block does not show a toast');
  return results;
}

async function copyFromCachedTopic(page, event) {
  const other = { ...topic, tid: '1.3', topic_id: 3, name: 'Other conversation', count: 1, unread: 1 };
  const original = { ...topic, name: 'Code examples', count: 2, unread: 2 };
  let topics = [original, other];
  await page.route('**/api/topics', route => route.fulfill({ json: { topics } }));
  await page.route(/\/api\/topics\/1\.3\/messages\?/, route => route.fulfill({
    json: { entries: [{ ...messages[0], seq: 42, text: 'Other conversation history' }], has_more: false },
  }));
  await event({ type: 'topics' });
  await page.locator('#tlist [data-tid="1.3"]').click();
  await page.locator('#thread [data-entry-seq="42"]').waitFor();

  // Fail the selected topic's history reload while removing the other topic.
  await page.route(/\/api\/topics\/1\.2\/messages\?/, route =>
    route.fulfill({ status: 503, json: {} }), { times: 1 });
  const unavailable = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/topics/1.2/messages' && response.status() === 503);
  topics = [original];
  await event({ type: 'topics' });
  await unavailable;
  assert.equal(await page.locator('#tlist [data-tid="1.3"]').count(), 0);
  const counts = seq => page.locator(`#thread [data-entry-seq="${seq}"] .md pre`).evaluateAll(blocks =>
    blocks.map(block => block.querySelectorAll(':scope > [data-copy-code]').length));
  assert.deepEqual(await counts(1), [1], 'cached user code block');
  assert.deepEqual(await counts(2), [1, 1], 'cached assistant code blocks');
}

async function codeCopy(browser) {
  const measurements = [];
  const fence = '`';
  const first = 'const path = "' + 'reports/weekly/'.repeat(14) + 'summary.txt";\n  console.log(path);  \n';
  const second = 'Literal <&>  ';
  const userCode = 'echo "typed by the user"';
  const answer = 'Two examples with ' + fence + 'inline code' + fence + '.\n\n'
    + fence.repeat(3) + 'js\n' + first + '\n' + fence.repeat(3) + '\n\n'
    + second.split('\n').map(line => '    ' + line).join('\n');
  const prompt = 'Example input:\n\n' + fence.repeat(3) + 'sh\n' + userCode + '\n' + fence.repeat(3);
  const entries = [
    { ...messages[194], seq: 1, role: 'user', text: prompt },
    { ...messages[195], seq: 2, role: 'bot', text: answer },
  ];
  for (const [width, height] of [[1280, 720], [375, 812]]) {
    for (const colorScheme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme,
        permissions: ['clipboard-read', 'clipboard-write'] });
      try {
        const page = await context.newPage();
        await page.addInitScript(() => localStorage.setItem('dtw-web-language', 'en'));
        await mockChat(page, entries, { ...topic, name: 'Code examples', preview: 'Copy a code block' });
        await page.goto(origin);
        const bot = page.locator('#thread [data-entry-seq="2"]');
        const user = page.locator('#thread [data-entry-seq="1"]');
        const codes = bot.getByRole('button', { name: 'Copy code', exact: true });
        await codes.last().waitFor();
        assert.equal(await page.locator('html').getAttribute('data-theme'), colorScheme);
        assert.equal(await codes.count(), 2);
        assert.equal(await bot.locator('p [data-copy-code]').count(), 0);

        const overflow = () => page.evaluate(() => ({
          documentHeight: document.scrollingElement.scrollHeight, viewportHeight: innerHeight,
          documentWidth: document.scrollingElement.scrollWidth, viewportWidth: innerWidth,
        }));
        const checkDocument = async () => {
          const bounds = await overflow();
          assert.equal(bounds.documentHeight, bounds.viewportHeight, JSON.stringify(bounds));
          assert.equal(bounds.documentWidth, bounds.viewportWidth, JSON.stringify(bounds));
        };
        const block = bot.locator('pre').first();
        const layout = await block.evaluate(pre => {
          const button = pre.querySelector('[data-copy-code]');
          const code = pre.querySelector('code');
          const before = button.getBoundingClientRect();
          code.scrollLeft = code.scrollWidth;
          const after = button.getBoundingClientRect();
          const scrolled = code.scrollLeft;
          const text = document.createRange();
          text.selectNodeContents(code);
          const endGap = after.left - text.getBoundingClientRect().right;
          code.scrollLeft = 0;
          return { scrolled, buttonFixed: before.x === after.x && before.y === after.y, endGap };
        });
        assert(layout.scrolled > 0, JSON.stringify(layout));
        assert(layout.buttonFixed, JSON.stringify(layout));
        assert(layout.endGap > 0, JSON.stringify(layout));
        const short = await user.locator('pre').evaluate(pre => {
          const code = pre.querySelector('code');
          const text = document.createRange();
          text.selectNodeContents(code);
          const button = pre.querySelector('[data-copy-code]').getBoundingClientRect();
          return { overflow: code.scrollWidth - code.clientWidth, endGap: button.left - text.getBoundingClientRect().right };
        });
        assert.equal(short.overflow, 0, JSON.stringify(short));
        assert(short.endGap > 0, JSON.stringify(short));
        const compact = await bot.locator('pre').last().evaluate(pre => {
          const button = pre.querySelector('[data-copy-code]');
          const block = pre.getBoundingClientRect();
          const target = button.getBoundingClientRect();
          return {
            height: block.height,
            paddingTop: getComputedStyle(pre.querySelector('code')).paddingTop,
            buttonHeight: target.height,
            buttonWidth: target.width,
            buttonFits: target.top >= block.top && target.bottom <= block.bottom
              && target.left >= block.left && target.right <= block.right,
            matchingBackground: getComputedStyle(button).backgroundColor === getComputedStyle(pre).backgroundColor,
          };
        });
        assert.equal(compact.paddingTop, '12px');
        assert(compact.buttonFits && compact.matchingBackground, JSON.stringify(compact));
        measurements.push({ width, colorScheme, ...compact, shortEndGap: short.endGap, longEndGap: layout.endGap });
        await checkDocument();
        await page.locator('#msgIn').focus();
        await bot.scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(config.shots, 'code-copy-' + width + '-' + colorScheme + '.png') });

        const readClipboard = value => page.waitForFunction(
          async expected => await navigator.clipboard.readText() === expected, value,
        );
        await codes.first().press('Enter');
        await readClipboard(first);
        await codes.last().click();
        await readClipboard(second);
        await user.getByRole('button', { name: 'Copy code', exact: true }).click();
        await readClipboard(userCode);
        await bot.getByRole('button', { name: 'Copy message', exact: true }).click();
        await readClipboard(answer);

        const event = detail => page.evaluate(async data => {
          const { handleEvent } = await import('/static/js/chat.js');
          await handleEvent({ tid: '1.2', turn_id: 'code-turn', ...data });
        }, detail);
        const live = page.locator('#streamMsg');
        const codeButton = live.getByRole('button', { name: 'Copy code', exact: true });
        await event({ type: 'turn', phase: 'started' });
        await event({ type: 'delta', text: fence.repeat(3) + 'sh\nfirst line' });
        await codeButton.waitFor();
        assert.equal(await codeButton.count(), 1);
        await codeButton.focus();
        await event({ type: 'delta', text: '\n  second line  ' });
        await page.waitForFunction(() => document.querySelector('#streamMsg pre code')?.textContent.includes('second line'));
        assert.equal(await codeButton.count(), 1);
        assert(await codeButton.evaluate(button => button === document.activeElement));
        await codeButton.press('Enter');
        await readClipboard('first line\n  second line  ');
        await event({ type: 'delta', text: '\n' + fence.repeat(3) + '\n\nEnd of response.' });
        await live.locator('[data-stream-complete] [data-copy-code]').waitFor();
        assert.equal(await codeButton.count(), 1);
        await event({ type: 'turn', phase: 'done' });
        const finished = page.locator('[data-turn="code-turn"]').getByRole('button', { name: 'Copy code', exact: true });
        await finished.waitFor();
        assert.equal(await finished.count(), 1);
        await finished.click();
        await readClipboard('first line\n  second line  ');
        await checkDocument();
        if (width === 1280 && colorScheme === 'light') {
          await copyFromCachedTopic(page, event);
          console.log(JSON.stringify(await copyOpenBlockWithMouse(page, event)));
        }
      } finally {
        await context.close();
      }
    }
  }
  console.log(JSON.stringify(measurements));
}

async function telegramCode(browser) {
  const { text, display, block, inline } = config.telegramCode;
  const entries = [
    { ...messages[194], seq: 1, role: 'user', source: 'telegram', text, buttons: [], display_markdown: display },
    { ...messages[195], seq: 2, role: 'user', source: 'telegram', text: '**Old message**' },
    { ...messages[196], seq: 3, role: 'user', source: 'telegram', text },
  ];

  const context = await browser.newContext({ viewport: { width: 1280, height: 720 },
    permissions: ['clipboard-read', 'clipboard-write'] });
  try {
    const page = await context.newPage();
    let socket;
    await mockChat(page, entries, topic, route => { socket = route; });
    await page.goto(origin);
    const formatted = page.locator('#thread [data-entry-seq="1"]');
    const legacy = page.locator('#thread [data-entry-seq="2"]');
    const live = page.locator('#thread [data-entry-seq="3"]');
    await live.waitFor();
    await formatted.locator('pre code').waitFor();
    assert.equal(await formatted.locator('pre code').textContent(), block + '\n');
    const span = formatted.locator('p code');
    assert.equal(await span.textContent(), inline);
    assert.equal(await span.innerText(), inline);
    assert.equal(await span.evaluate(node => getComputedStyle(node).whiteSpace), 'break-spaces');
    assert.equal(await formatted.locator('.response-button-row').count(), 0);
    assert.equal(await legacy.locator('strong').textContent(), 'Old message');
    assert.equal(await legacy.locator('pre').count(), 0);
    assert.equal(await live.locator('pre').count(), 0);

    socket.send(JSON.stringify({ type: 'entry_meta', tid: topic.tid, entry_id: entries[2].id,
      meta: { display_markdown: display, buttons: [] } }));
    await live.locator('pre code').waitFor();
    assert.equal(await live.locator('pre code').textContent(), block + '\n');
    assert.equal(await live.locator('p code').innerText(), inline);
    assert.equal(await live.locator('.response-button-row').count(), 0);
    await live.locator('[data-copy-message]').click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), text);

    const next = { ...topic, tid: '1.3', topic_id: 3, name: 'Second topic', count: 1, unread: 1 };
    await page.route('**/api/topics', route => route.fulfill({ json: { topics: [topic, next] } }));
    await page.route(/\/api\/topics\/1\.3\/messages\?/, route =>
      route.fulfill({ json: { entries: [messages[4]], has_more: false } }));
    socket.send(JSON.stringify({ type: 'topics' }));
    await page.locator('#tlist [data-tid="1.3"]').click();
    await page.locator('#thread [data-entry-seq="5"]').waitFor();

    await page.route(/\/api\/topics\/1\.2\/messages\?/, route => route.fulfill({ status: 503 }));
    await page.route('**/api/topics', route => route.fulfill({ json: { topics: [topic] } }));
    const historyError = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/topics/1.2/messages' && response.status() === 503);
    socket.send(JSON.stringify({ type: 'topics' }));
    await historyError;
    await span.waitFor();
    assert.equal(await span.innerText(), inline, 'cached topic keeps inline code whitespace');
    assert.equal(await span.evaluate(node => getComputedStyle(node).whiteSpace), 'break-spaces');
  } finally {
    await context.close();
  }
}

async function telegramCodeBoundaries(browser) {
  const cases = config.telegramCodeBoundaries;
  const entries = cases.map(({ text, display }, index) => ({
    ...messages[194 + index], seq: index + 1, role: 'user', source: 'telegram', text,
    ...(display === null ? {} : { display_markdown: display }),
  }));
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  try {
    const page = await context.newPage();
    await mockChat(page, entries);
    await page.goto(origin);
    for (const [index, { text, visible }] of cases.entries()) {
      const row = page.locator('#thread [data-entry-seq="' + (index + 1) + '"]');
      await row.waitFor();
      const bubble = row.locator('.md');
      const rendered = (await bubble.innerText()).replace(/\n+/g, '\n').trim();
      assert.doesNotMatch(rendered, /&#(?:96|92);/, text);
      assert.equal(rendered, visible, text);
      assert.equal(await bubble.getAttribute('data-display-markdown'), null, text);
      await row.locator('[data-copy-message]').click();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), text);
    }
  } finally {
    await context.close();
  }
}

async function userLineBreaks(browser) {
  const text = 'First line\nSecond line\nThird line';
  const xml = '<config>\n  <a>1</a>\n</config>';
  const code = 'one  two\n  three';
  const display = text + '\n\n- List first\n  List second\n\n```text\n' + code + '\n```';
  const entries = [
    { ...messages[194], seq: 1, role: 'user', source: 'web', text },
    { ...messages[195], seq: 2, role: 'bot', text },
    { ...messages[196], seq: 3, role: 'user', source: 'telegram', text, display_markdown: display },
    { ...messages[197], seq: 4, role: 'user', source: 'web', text: xml },
    { ...messages[198], seq: 5, role: 'bot', text: xml },
  ];
  const lineCount = locator => locator.evaluate(node => {
    const lines = new Set(), walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    while (walker.nextNode()) {
      range.selectNodeContents(walker.currentNode);
      for (const rect of range.getClientRects()) {
        if (rect.width) lines.add(Math.round(rect.top));
      }
    }
    return lines.size;
  });

  for (const width of [1280, 768, 375]) {
    const context = await browser.newContext({
      viewport: { width, height: 812 }, colorScheme: 'light',
    });
    try {
      const page = await context.newPage();
      await page.addInitScript(() => localStorage.setItem('dtw-web-language', 'en'));
      let socket;
      await mockChat(page, entries, topic, route => { socket = route; });
      await page.goto(origin);
      const message = seq => page.locator('#thread [data-entry-seq="' + seq + '"] .bubble');
      const checkMessages = async () => {
        await message(3).locator('pre code').waitFor();
        assert.equal(await lineCount(message(1).locator('p')), 3);
        assert.equal(await lineCount(message(2).locator('p')), 1);
        assert.equal(await lineCount(message(3).locator('p')), 3);
        assert.equal(await lineCount(message(3).locator('li')), 2);
        assert.equal(await message(3).locator('pre code').textContent(), code + '\n');
        assert.equal(await lineCount(message(4)), 3);
        assert.equal(await lineCount(message(5)), 1);
        for (const seq of [4, 5]) {
          assert.equal(await message(seq).textContent(), xml);
          assert.equal(await message(seq).locator('config, a').count(), 0);
        }
      };
      await page.evaluate(() => document.fonts.ready);
      await checkMessages();
      await page.screenshot({ path: path.join(config.shots, 'user-line-breaks-' + width + '.png') });

      const next = { ...topic, tid: '1.3', topic_id: 3, name: 'Second topic', count: 1, unread: 1 };
      await page.route('**/api/topics', route => route.fulfill({ json: { topics: [next] } }));
      await page.route(/\/api\/topics\/1\.3\/messages\?/, route =>
        route.fulfill({ json: { entries: [messages[5]], has_more: false } }));
      socket.send(JSON.stringify({ type: 'topics' }));
      await message(6).waitFor();

      let releaseHistory;
      const historyGate = new Promise(resolve => { releaseHistory = resolve; });
      const historyUrl = /\/api\/topics\/1\.2\/messages\?/;
      await page.route(historyUrl, async route => {
        if (route.request().method() === 'GET') await historyGate;
        await route.fallback();
      });
      await page.route('**/api/topics', route => route.fulfill({ json: { topics: [topic] } }));
      const historyRequest = page.waitForRequest(historyUrl);
      socket.send(JSON.stringify({ type: 'topics' }));
      await historyRequest;
      try {
        await checkMessages();
      } finally {
        releaseHistory();
      }

      await page.locator('#msgIn').fill(text);
      const submission = page.waitForRequest(request =>
        new URL(request.url()).pathname === '/api/topics/1.2/messages' && request.method() === 'POST');
      await page.locator('#sendBtn').click();
      assert.equal((await submission).postDataJSON().text, text);
      assert.equal(await lineCount(page.locator('#thread [data-queue-turn="test-turn"] p')), 3);
    } finally {
      await context.close();
    }
  }
}

async function unreadBadges(browser) {
  const page = await browser.newPage();
  try {
    const entries = messages.slice(0, 2);
    const current = { ...topic, read_only: true, seen: 1, unread: 1, count: 2 };
    const other = { ...current, tid: '1.3', topic_id: 3, name: 'Other topic', seen: null, unread: 0, count: 0 };
    let socket;
    await mockChat(page, entries, current, connection => { socket = connection; });
    await page.route('**/api/topics', route => route.fulfill({ json: { topics: [current, other] } }));
    const requests = [];
    page.on('request', request => {
      const { pathname } = new URL(request.url());
      if (pathname.startsWith('/api/topics')) requests.push(pathname);
    });
    await page.goto(origin);
    await page.locator('#thread [data-entry-seq="2"]').waitFor();
    assert.ok(socket);
    const badge = tid => page.locator(`#tlist [data-tid="${tid}"] .unread`);
    assert.equal(await badge(current.tid).textContent(), '1');
    const initialHistory = requests.filter(path => path.endsWith('/messages')).length;
    const duplicate = { type: 'entry', tid: current.tid, entry: entries[1] };
    const response = () => page.waitForResponse(reply => new URL(reply.url()).pathname === '/api/topics');
    for (const unread of [2, 2]) {
      const updated = response();
      current.unread = unread;
      socket.send(JSON.stringify(duplicate));
      await updated;
      await page.waitForFunction(({ tid, unread }) =>
        document.querySelector(`#tlist [data-tid="${tid}"] .unread`)?.textContent === String(unread),
      { tid: current.tid, unread });
    }
    const updated = response();
    other.unread = 1;
    socket.send(JSON.stringify({ type: 'entry', tid: other.tid, entry: entries[0] }));
    await updated;
    await badge(other.tid).waitFor();
    assert.equal(await badge(other.tid).textContent(), '1');
    assert.equal(await page.locator('#thread [data-entry-seq="2"]').count(), 1);
    assert.ok(!requests.includes('/api/topics/1.3/messages'));
    assert.equal(requests.filter(path => path.endsWith('/messages')).length, initialHistory);
  } finally {
    await page.close();
  }
}

async function unreadRecovery(browser, scenario) {
  const page = await browser.newPage();
  const row = { ...topic, seen: 2, unread: 3, count: 5 };
  const entryNull = scenario === 'unread-entry-null';
  const target = entryNull ? { ...row, tid: '1.3', topic_id: 3, name: 'Other topic' } : row;
  const list = target => ({ topics: entryNull ? [row, target] : [target] });
  const firstList = Promise.withResolvers(), releaseList = Promise.withResolvers();
  const writes = [];
  let socket, nextWrite, started = false, listCalls = 0;
  const busy = { status: 503, json: { error: 'history_busy' } };
  const failedWrite = scenario.endsWith('-notice'), overlap = scenario.endsWith('-overlap');
  try {
    await mockChat(page, messages.slice(0, 2), row, peer => { socket = peer; });
    await page.route('**/api/topics', async route => {
      if (!started) return route.fulfill({ json: list(target) });
      const call = ++listCalls;
      if (call === 1) {
        firstList.resolve();
        if (overlap) await releaseList.promise;
      }
      if ((overlap && call <= 2) || (failedWrite && call === 1)) {
        return route.fulfill(busy);
      }
      const unread = call === 1 ? null : entryNull ? 4 : scenario === 'unread-remote-null' ? 0 : 2;
      return route.fulfill({ json: list({ ...target, unread, preview: 'Updated topic' }) });
    });
    for (const path of ['**/api/topics/1.2/seen', '**/api/topics/seen-all']) {
      await page.route(path, async route => {
        const gate = Promise.withResolvers();
        writes.push(gate);
        nextWrite?.(gate);
        await route.fulfill(await gate.promise);
      });
    }
    await page.goto(origin);
    await page.locator('#thread [data-entry-seq="2"]').waitFor();
    await page.evaluate(async () => (await import('/static/js/topics.js')).refreshTopics());
    assert.ok(socket);
    const cell = page.locator(`#tlist [data-tid="${target.tid}"]`);
    const badge = cell.locator('.unread');
    assert.equal(await badge.textContent(), '3');
    await page.evaluate(() => {
      window.unreadNotices = 0;
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node.matches?.('.toast-error')) window.unreadNotices++;
        }
      }).observe(document.querySelector('#toasts'), { childList: true });
    });
    const write = seq => page.evaluate(async value => {
      (await import('/static/js/topics.js')).markSeen('1.2', value);
    }, seq);
    const waitForList = () => page.waitForResponse(reply => new URL(reply.url()).pathname === '/api/topics');
    const captureWrite = async trigger => {
      const gate = Promise.withResolvers();
      nextWrite = gate.resolve;
      await trigger();
      return gate.promise;
    };
    const put = seq => captureWrite(() => write(seq));
    const waitForBadge = expected => page.waitForFunction(({ tid, value }) =>
      (document.querySelector(`#tlist [data-tid="${tid}"] .unread`)?.textContent ?? '0') === value,
    { tid: target.tid, value: expected }, { timeout: 5500 });
    started = true;
    if (overlap) {
      (await put(3)).resolve({ json: { seen: 3 } });
      await firstList.promise;
      if (scenario === 'unread-direct-overlap') {
        await page.evaluate(async () => {
          await (await import('/static/js/topics.js')).refreshTopics().catch(() => {});
        });
      } else {
        const failed = waitForList();
        socket.send(JSON.stringify({ type: 'topics' }));
        assert.equal((await failed).status(), 503, 'the topic event request fails while the first list is held');
      }
      releaseList.resolve();
      await waitForBadge('2');
      assert.equal(listCalls, 3);
      assert.match(await page.locator('#tlist').textContent(), /Updated topic/);
    } else if (scenario === 'unread-overlapping-puts') {
      const first = await put(3), second = await put(4);
      const saved = page.waitForResponse(reply =>
        new URL(reply.url()).pathname === '/api/topics/1.2/seen' && reply.ok());
      first.resolve({ json: { seen: 3 } });
      await saved;
      second.resolve(busy);
      await waitForBadge('2');
      assert.equal(listCalls, 2, 'an earlier successful write still requires confirmation');
    } else if (entryNull) {
      const unknown = waitForList();
      socket.send(JSON.stringify({ type: 'entry', tid: target.tid, entry: messages[5] }));
      assert.equal((await unknown).status(), 200);
      await cell.getByText('Updated topic', { exact: true }).waitFor();
      assert.equal(await badge.textContent(), '3');
      await waitForBadge('4');
      assert.equal(listCalls, 2, 'the unavailable count is retried without another signal');
      assert.equal(writes.length, 0);
    } else if (scenario === 'unread-remote-null') {
      socket.send(JSON.stringify({ type: 'seen', seen: { '1.2': 5 } }));
      await waitForBadge('0');
      assert.equal(listCalls, 2, 'a remote seen event retries an unknown count');
      assert.equal(writes.length, 0);
    } else {
      const read = scenario === 'unread-post-notice'
        ? await captureWrite(() => page.locator('#markAllRead').click()) : await put(3);
      const failed = waitForList();
      read.resolve(busy);
      assert.equal((await failed).status(), 503);
      await waitForBadge('2');
      assert.equal(await page.evaluate(() => window.unreadNotices), 1);
    }
  } finally {
    releaseList.resolve();
    for (const write of writes) write.resolve(busy);
    await page.close();
  }
}

async function reconnectHistory(browser, scenario) {
  const context = await browser.newContext({ viewport: { width: 375, height: 667 } });
  try {
    const page = await context.newPage();
    await page.clock.install({ time: new Date(today) });
    const entries = messages.slice(0, 120);
    let socket, failTopics = false, historyReads = 0;
    await mockChat(page, entries, topic, peer => { socket = peer; });
    await page.route('**/api/topics', route => {
      if (failTopics) {
        failTopics = false;
        return route.fulfill({ status: 503, json: { error: 'bot_unreachable' } });
      }
      return route.fulfill({ json: { topics: [{ ...topic, count: entries.length }] } });
    });
    page.on('request', request => {
      if (request.method() === 'GET' && new URL(request.url()).pathname.endsWith('/messages')) historyReads++;
    });
    const topicsResponse = status => page.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/topics' && response.status() === status,
    { timeout: 4000 });
    await page.goto(origin);
    await page.locator('[data-entry-seq="120"]').waitFor();
    // Settle the read cursor and its topic badge update before disconnecting.
    await topicsResponse(200);

    const beforeReconnect = historyReads;
    const missing = { ...messages[120], text: 'Message received while disconnected' };
    entries.push(missing);
    const succeeds = scenario === 'reconnect-history-success';
    failTopics = !succeeds;
    const first = topicsResponse(succeeds ? 200 : 503);
    await socket.close();
    await page.clock.fastForward(2000);
    await first;
    if (!succeeds) {
      await page.locator('#toasts .toast-error').waitFor({ timeout: 2500 });
      assert.equal(await page.locator('#toasts .toast-error').count(), 1);
      assert.equal(historyReads, beforeReconnect);
      if (scenario === 'reconnect-history-auth-change') {
        await page.evaluate(() => document.dispatchEvent(new CustomEvent('auth-change', {
          detail: { authenticated: false },
        })));
        // Simulate a later topic update and advance beyond the scheduled retry.
        await page.evaluate(() => document.dispatchEvent(new CustomEvent('topics-updated')));
        await page.clock.runFor(1200);
        assert.equal(historyReads, beforeReconnect, 'a logged-out session must not reload its old history');
        assert.equal(await page.locator('[data-entry-seq="121"]').count(), 0);
        return;
      }
      const retried = topicsResponse(200);
      if (scenario === 'reconnect-history-topics-event') socket.send(JSON.stringify({ type: 'topics' }));
      else if (scenario === 'reconnect-history-next-resync') socket.send(JSON.stringify({ type: 'bridge', up: true }));
      else await page.clock.runFor(1000);
      await retried;
    }
    await page.locator('[data-entry-seq="121"]').waitFor({ timeout: 2500 })
      .catch(() => assert.fail(`history was not restored after reconnect (${historyReads - beforeReconnect} reads)`));
    assert.equal(historyReads - beforeReconnect, 2, 'recovery should read both history pages once');
    assert.equal(await page.locator('#toasts .toast-error').count(), succeeds ? 0 : 1);
    socket.send(JSON.stringify({ type: 'entry', tid: topic.tid, entry: messages[121] }));
    await page.locator('[data-entry-seq="122"]').waitFor({ timeout: 2500 });
    assert.equal(await page.locator('[data-entry-seq="121"]').count(), 1);
  } finally {
    await context.close();
  }
}

async function busyIndicator(browser, scenario) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.clock.install({ time: new Date(today) });
    const tid = topic.tid;
    const turn = { turn_id: 'active', phase: 'started', text: 'Prompt', received: today };
    const terminal = scenario.startsWith('busy-terminal');
    if (/-(cancelled|entry)$/.test(scenario)) turn.phase = 'queued';
    const row = { ...topic, busy: !scenario.startsWith('busy-reconnect'), turns: terminal ? [turn] : [] };
    let failOutbox = false, holdTopics, onHeldTopics, topicsReads = 0, socket;
    await mockChat(page, messages.slice(0, 2), row, peer => { socket = peer; });
    await page.route('**/api/topics', async route => {
      topicsReads++;
      const snapshot = structuredClone(row);
      if (holdTopics) {
        onHeldTopics?.();
        await holdTopics;
      }
      await route.fulfill({ json: { topics: [snapshot] } });
    });
    await page.route('**/api/outbox', route => failOutbox
      ? route.fulfill({ status: 503, json: { error: 'generic' } })
      : route.fulfill({ json: { items: [] } }));
    await page.goto(origin);
    await page.locator('[data-entry-seq="2"]').waitFor();

    const busy = page.locator('#thread [data-thread-key="busy"]');
    const live = page.locator('#streamMsg');
    const event = (type, fields = {}) => page.evaluate(async data => {
      const { handleEvent } = await import('/static/js/chat.js');
      await handleEvent(data);
    }, { tid, type, ...fields });
    async function expectActivity(element, visible, message) {
      await element.waitFor({ state: visible ? 'visible' : 'hidden', timeout: 2500 })
        .catch(() => assert.fail(message));
    }

    if (scenario === 'busy-refresh') {
      await expectActivity(busy, true, 'the snapshot should show activity');
      row.busy = false;
      await event('topics');
      await expectActivity(busy, false, 'an ordinary topic refresh should clear an idle topic');
      let release;
      holdTopics = new Promise(resolve => { release = resolve; });
      const requested = page.waitForRequest(request => new URL(request.url()).pathname === '/api/topics');
      const refreshing = event('topics');
      await requested;
      await event('busy', { busy: true, source: 'telegram' });
      release();
      await refreshing;
      holdTopics = null;
      await expectActivity(busy, true, 'an older idle response must preserve a newer Telegram start');
      await event('busy', { busy: false, source: 'telegram' });
      await expectActivity(busy, false, 'the Telegram finish should clear the indicator');
    } else if (scenario === 'busy-recheck') {
      await expectActivity(busy, true, 'the active CLI snapshot should show activity');
      const initialReads = topicsReads;
      await page.clock.runFor(2500);
      await expectActivity(busy, true, 'rechecks should keep an active CLI visible');
      assert.ok(topicsReads > initialReads);
      row.busy = false;
      await page.clock.runFor(10000);
      await expectActivity(busy, false, 'a recheck should clear the indicator without an event');
      const idleReads = topicsReads;
      await page.clock.runFor(40000);
      assert.equal(topicsReads, idleReads, 'idle chat should have no recheck timer');
    } else if (scenario.startsWith('busy-terminal-held-')) {
      const queued = turn.phase === 'queued';
      const indicator = queued ? page.locator('[data-queue-turn="active"] [data-cancel-turn]') : live;
      await expectActivity(indicator, true, 'the web turn should start with its own indicator');
      row.busy = false;
      row.turns = [];
      let release;
      holdTopics = new Promise(resolve => { release = resolve; });
      const requested = page.waitForRequest(request => new URL(request.url()).pathname === '/api/topics');
      try {
        if (scenario.endsWith('-entry')) {
          await event('entry', { entry: {
            id: 'cancelled', seq: 3, ts: today, role: 'user', source: 'web',
            text: turn.text, turn_id: turn.turn_id, status: 'cancelled',
          } });
        } else {
          await event('turn', { turn_id: turn.turn_id, phase: queued ? 'cancelled' : 'done' });
        }
        await requested;
        await expectActivity(indicator, false, 'the original indicator should end');
        assert.equal(await busy.isVisible(), false, 'the old busy snapshot must not flash while the response is held');
      } finally {
        release();
      }
      await expectActivity(busy, false, 'the fresh idle snapshot should leave the indicator hidden');
    } else if (scenario === 'busy-terminal') {
      await expectActivity(live, true, 'the active web turn should display its live reply');
      row.busy = false;
      row.turns = [];
      await event('turn', { turn_id: turn.turn_id, phase: 'done' });
      await expectActivity(live, false, 'the live response should end');
      await expectActivity(busy, false, 'a completed web turn should refresh the idle topic');

      row.busy = true;
      row.turns = [{ ...turn, turn_id: 'queued', phase: 'queued' }];
      await event('bridge', { up: true });
      await page.locator('[data-queue-turn="queued"]').waitFor();
      row.turns = [];
      await event('turn', { turn_id: 'queued', phase: 'cancelled' });
      await expectActivity(busy, true, 'other work in the topic should remain visible after cancellation');
      row.turns = [{ ...turn, turn_id: 'last', phase: 'queued' }];
      await event('bridge', { up: true });
      await page.locator('[data-queue-turn="last"]').waitFor();
      row.busy = false;
      row.turns = [];
      await event('turn', { turn_id: 'last', phase: 'cancelled' });
      await expectActivity(busy, false, 'cancelling the last queued turn should refresh the idle topic');
    } else if (scenario === 'busy-reconnect-held-list') {
      const stop = page.locator('#stopBtn');
      socket.send(JSON.stringify({ type: 'turn', tid, turn_id: turn.turn_id, phase: 'started' }));
      await expectActivity(live, true, 'the test should start with a live reply');
      await expectActivity(stop, true, 'the running reply should show Stop');
      row.busy = true;
      row.turns = [turn];
      const held = Promise.withResolvers(), release = Promise.withResolvers();
      holdTopics = release.promise;
      onHeldTopics = held.resolve;
      const refreshing = page.evaluate(async () => (await import('/static/js/topics.js')).refreshTopics());
      try {
        await held.promise;
        holdTopics = null;
        row.busy = false;
        row.turns = [];
        let replies = 0;
        const recovered = page.waitForResponse(reply =>
          new URL(reply.url()).pathname === '/api/topics' && reply.ok() && ++replies === 2,
        { timeout: 3000 });
        socket.send(JSON.stringify({ type: 'bridge', up: false }));
        socket.send(JSON.stringify({ type: 'bridge', up: true }));
        socket.send(JSON.stringify({ type: 'topics' }));
        await recovered.catch(() => assert.fail(`expected two new list responses while the old list was held, got ${replies}`));
        await expectActivity(live, false, 'the recovery should hide the finished reply before the old list returns');
        await expectActivity(stop, false, 'the recovery should hide Stop before the old list returns');
      } finally {
        release.resolve();
        await refreshing;
      }
      assert.equal(await live.isVisible(), false, 'the old list must not restore the reply');
      assert.equal(await stop.isVisible(), false, 'the old list must not restore Stop');
    } else if (scenario.startsWith('busy-reconnect-overlap-')) {
      await event('bridge', { up: true });
      await event('turn', { turn_id: turn.turn_id, phase: 'started' });
      await expectActivity(live, true, 'the test should start with a live reply');
      failOutbox = scenario.endsWith('503');
      await event('bridge', { up: false });
      row.busy = true;
      row.turns = [turn];
      let release;
      holdTopics = new Promise(resolve => { release = resolve; });
      const requested = new Promise(resolve => { onHeldTopics = resolve; });
      const reconnect = event('bridge', { up: true });
      try {
        await requested;
        holdTopics = null;
        row.busy = false;
        row.turns = [];
        await event('topics');
        await expectActivity(live, false, 'the newer response should settle the turn before the older request returns');
        assert.equal(await page.locator('#stopBtn').isVisible(), false);
      } finally {
        release();
        await reconnect;
      }
      await page.clock.fastForward(45000);
      assert.equal(await live.isVisible(), false, 'the newest idle response should settle the recovered turn');
      assert.equal(await page.locator('#stopBtn').isVisible(), false, 'the newest idle response should also hide Stop');
    } else {
      await event('busy', { busy: true });
      await event('turn', { turn_id: turn.turn_id, phase: 'started' });
      await expectActivity(live, true, 'the test should start with a live reply');
      failOutbox = true;
      await event('bridge', { up: false });
      await event('bridge', { up: true });
      await expectActivity(live, false, 'reconnect should clear a finished turn despite an outbox error');
      await expectActivity(busy, false, 'the successful idle snapshot should still apply');
    }
  } finally {
    await context.close();
  }
}

async function busyResumeScroll(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 667 } });
  try {
    const page = await context.newPage();
    const entries = messages.slice(0, 40);
    const row = { ...topic, busy: false, turns: [] };
    await mockChat(page, entries, row);
    await page.route('**/api/topics', route => route.fulfill({ json: { topics: [{ ...row, count: entries.length }] } }));
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelectorAll('[data-entry-seq]').length === 40);
    await page.evaluate(() => document.fonts.ready);
    const before = await scrollPosition(page, 64);
    assert.equal(before.gap, 64);
    async function setHidden(hidden) {
      await page.evaluate(value => {
        Object.defineProperty(document, 'hidden', { configurable: true, value });
        document.dispatchEvent(new Event('visibilitychange'));
      }, hidden);
    }
    await setHidden(true);
    row.busy = true;
    const history = page.waitForResponse(response =>
      response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith('/messages'));
    await setHidden(false);
    await (await history).finished();
    await page.locator('[data-thread-key="busy"]').waitFor();
    const after = await scrollPosition(page);
    assert.equal(after.seq, before.seq, 'tab resume should keep the same message visible');
    assert.equal(after.y, before.y, 'tab resume should preserve the message screen position');
  } finally {
    await context.close();
  }
}

const browser = await chromium.launch();
try {
  await fs.mkdir(config.shots, { recursive: true });
  const scenarios = { dates: messageDates, window: preserveOldestGroup, focus: chatFocus, 'code-copy': codeCopy, 'telegram-code': telegramCode, 'telegram-code-boundaries': telegramCodeBoundaries, 'user-line-breaks': userLineBreaks, 'unread-badges': unreadBadges };
  const scenario = process.argv[3] ?? 'dates';
  if (scenario.startsWith('overflow-')) await documentOverflow(browser, scenario);
  else if (scenario.startsWith('unread-') && scenario !== 'unread-badges') await unreadRecovery(browser, scenario);
  else if (scenario.startsWith('reconnect-history-')) await reconnectHistory(browser, scenario);
  else if (scenario.startsWith('busy-resume-scroll-')) await busyResumeScroll(browser, Number(scenario.split('-').at(-1)));
  else if (scenario.startsWith('busy-')) await busyIndicator(browser, scenario);
  else await scenarios[scenario](browser);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
