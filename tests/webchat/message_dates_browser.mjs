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
  count: 240, imported_count: 0, created_at: beforeMidnight, last_ts: today,
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
async function mockChat(page, entries = messages, fixture = topic) {
  const fixtureTopic = { ...fixture, count: entries.length, last_ts: entries.at(-1).ts };
  await page.routeWebSocket('**/api/ws', () => {});
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
      case '/api/topics/1.2/seen': return fulfill({ seen: request.postDataJSON().seq });
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
      const unreadTopic = { ...delayedTopic, tid: '1.4', topic_id: 4, name: 'Unread topic', count: 1 };
      await page.route('**/api/topics', route => route.fulfill({
        json: { topics: [{ ...topic, read_only: true }, delayedTopic, unreadTopic] },
      }));
      await page.route(/\/api\/topics\/1\.4\/messages\?/, route =>
        route.fulfill({ json: { entries: [messages[6]], has_more: false } }));
      await page.route('**/api/topics/1.4/seen', route =>
        route.fulfill({ json: { seen: route.request().postDataJSON().seq } }));
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
          code.scrollLeft = 0;
          return { scrolled, buttonFixed: before.x === after.x && before.y === after.y };
        });
        assert(layout.scrolled > 0, JSON.stringify(layout));
        assert(layout.buttonFixed, JSON.stringify(layout));
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
        measurements.push({ width, colorScheme, ...compact });
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
      } finally {
        await context.close();
      }
    }
  }
  console.log(JSON.stringify(measurements));
}

const browser = await chromium.launch();
try {
  await fs.mkdir(config.shots, { recursive: true });
  const scenarios = { dates: messageDates, window: preserveOldestGroup, focus: chatFocus, 'code-copy': codeCopy };
  const scenario = process.argv[3] ?? 'dates';
  if (scenario.startsWith('overflow-')) await documentOverflow(browser, scenario);
  else await scenarios[scenario](browser);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
