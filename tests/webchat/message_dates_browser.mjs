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
  tid: '1.2', chat_id: 1, topic_id: 2, name: '날짜 표시 확인', group: 'telegram',
  count: 240, imported_count: 0, created_at: beforeMidnight, last_ts: today,
  preview: '오늘 메시지와 이전 메시지를 함께 확인해 줘.',
  read_only: false, renamable: true, deletable: true, busy: false,
};
const messages = Array.from({ length: topic.count }, (_, index) => ({
  id: `entry-${index + 1}`, seq: index + 1,
  ts: index < 74 ? beforeMidnight : index < 194 ? afterMidnight : today,
  role: index % 2 ? 'bot' : 'user', source: index % 3 ? 'web' : 'telegram',
  text: index % 2 ? '지난 대화에서 날짜가 바뀌는 구간을 확인했습니다.' : '오늘 메시지와 이전 메시지를 함께 확인해 줘.',
  has_attachments: false,
}));
messages[74].role = 'event';
messages[74].kind = 'background';
const browser = await chromium.launch();
const measurements = [];
try {
  await fs.mkdir(config.shots, { recursive: true });
  for (const width of [375, 768, 1280]) {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: width === 1280 ? 900 : 800 }, timezoneId: 'Asia/Seoul', colorScheme: theme });
      const page = await context.newPage();
      await page.clock.setFixedTime(new Date(today));
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
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
          case '/api/topics': return fulfill({ topics: [topic] });
          case '/api/state': return fulfill({ telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, web_sessions: 1 });
          case '/api/outbox': return fulfill({ items: [] });
          case '/api/commands': return fulfill({ commands: [] });
          case '/api/files': return fulfill({ files: [] });
          case '/api/masterkey/status': return fulfill({ state: 'unconfigured', remaining_seconds: null });
          case '/api/topics/1.2/seen': return fulfill({ seen: request.postDataJSON().seq });
          case '/api/topics/1.2/messages': {
            if (request.method() === 'POST') return fulfill({ turn_id: 'test-turn', phase: 'queued' });
            const before = Number(url.searchParams.get('before') ?? Infinity);
            const eligible = messages.filter(entry => entry.seq < before);
            return fulfill({ entries: eligible.slice(-100), has_more: eligible.length > 100 });
          }
          default: return fulfill({});
        }
      });
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
          const [day, timestamp] = await page.evaluate(([value, lang]) => {
            const date = new Date(value);
            const dateLabel = date.toLocaleDateString(lang, { dateStyle: 'medium' });
            return [dateLabel, dateLabel + ' · ' + date.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' })];
          }, [ts, locale]);
          assert.equal(await labelFor(seq).textContent(), day);
          assert.equal(await stamp(seq).textContent(), timestamp);
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
          firstDay: firstVisible.querySelector('time').textContent.split(' · ')[0],
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
        const overlap = await page.evaluate(() => {
          const button = document.querySelector('#threadBottom').getBoundingClientRect();
          const thread = document.querySelector('#thread').getBoundingClientRect();
          return button.height > 0 && button.top >= thread.bottom;
        });
        assert.ok(overlap, 'mobile navigation is outside the message viewport');
      }
      await readDay(72);
      await page.screenshot({ path: path.join(config.shots, `dates-a-${width}-${theme}.png`) });

      const newer = await switchWindow(false);
      const older = await switchWindow(true);
      await navigate('threadBottom');
      await page.waitForFunction(() => document.querySelector('[data-entry-seq="240"]'));
      assert.equal(await labelFor(240).textContent(), config.locales.ko['chat.today']);

      await page.locator('#msgIn').fill('대기 메시지');
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
  console.log(JSON.stringify(measurements));
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
