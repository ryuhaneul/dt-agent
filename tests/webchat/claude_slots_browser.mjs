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

const epoch = date => Date.parse(date) / 1000;
const now = epoch('2026-11-17T16:11:00+09:00');
const cooldown = epoch('2026-11-17T18:00:00+09:00');
const futureCooldown = epoch('2026-11-20T18:00:00+09:00');
const usage = (fiveHour, fiveHourReset, sevenDay, sevenDayReset) => ({
  captured_at: now - 120,
  windows: {
    five_hour: { utilization: fiveHour, resets_at: fiveHourReset },
    seven_day: { utilization: sevenDay, resets_at: sevenDayReset },
  },
});
function slot(name, kind, state, cooldown_until = null, usage = null) {
  return { name, kind, state, cooldown_until, usage };
}
function initialState() {
  return {
    next: 'claude',
    slots: [
      slot('claude', 'oauth', 'ok', null, usage(0.32,
        epoch('2026-11-17T19:40:00+09:00'), 0.41, epoch('2026-11-20T09:00:00+09:00'))),
      slot('claude-sub', 'oauth', 'cooldown', cooldown, usage(1, cooldown,
        0.87, epoch('2026-11-19T14:00:00+09:00'))),
      slot('claude-api', 'api_key', 'ok'),
      slot('claude-team', 'oauth', 'expired'),
      slot('claude-new', 'oauth', 'unauthenticated'),
    ],
  };
}

async function checkHeaderMatrix(browser) {
  const page = await browser.newPage({ viewport: { width: 844, height: 844 }, timezoneId: 'Asia/Seoul' });
  try {
    await page.clock.setFixedTime(new Date(now * 1000));
    let locale, unsent, state = initialState(), vaultState = 'unconfigured';
    await page.addInitScript(() => {
      window.headerLayout = () => {
        const visible = selector => [...document.querySelectorAll(selector)]
          .filter(element => element.getClientRects().length && !element.closest('.menu'));
        const controls = visible('.bar-acts button, .stat .badge, .slot-chips button, .claude-slots>.slot-refresh, .claude-slots>time');
        const rectangles = controls.map(control => control.getBoundingClientRect());
        const contained = (outer, inner) => inner.left >= outer.left - 0.1 && inner.right <= outer.right + 0.1
          && inner.top >= outer.top - 0.1 && inner.bottom <= outer.bottom + 0.1;
        const clipped = controls.filter((control, index) => {
          const content = [...control.children].filter(child => child.getClientRects().length
            && !child.classList.contains('sr-only'));
          return control.scrollWidth > control.clientWidth
            || content.some(child => !contained(rectangles[index], child.getBoundingClientRect())
              || (child instanceof HTMLElement && child.scrollWidth > child.clientWidth));
        }).length;
        const primary = visible('[data-slot-toggle][data-primary]');
        return {
          height: document.querySelector('.bar').getBoundingClientRect().height,
          document: document.documentElement.scrollWidth - innerWidth,
          overlaps: rectangles.reduce((count, a, i) => count + rectangles.slice(i + 1)
            .filter(b => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom).length, 0),
          clipped,
          shown: visible('[data-slot-toggle]').length,
          primary: primary.length,
          primaryWidth: primary[0]?.getBoundingClientRect().width,
          more: visible('[data-slot-more]')[0]?.textContent.trim(),
          refresh: visible('.claude-slots>.slot-refresh, .claude-slots>time').length,
          language: document.documentElement.lang,
          width: innerWidth,
          pending: !document.querySelector('#pendingBadge').hidden,
          vault: document.querySelector('#vaultBadge span')?.textContent,
        };
      };
    });
    await page.routeWebSocket('**/api/ws', () => {});
    await page.route('**/api/**', route => {
      const endpoint = new URL(route.request().url()).pathname;
      const reply = json => route.fulfill({ json });
      if (endpoint === '/api/i18n') {
        const lang = new URL(route.request().url()).searchParams.get('lang') || locale;
        return reply({ lang, strings: config.locales[lang] });
      }
      if (endpoint === '/api/auth/state') return reply({ authenticated: true, csrf: 'fixture-csrf', bridge_up: true });
      if (endpoint === '/api/masterkey/status') return reply({ state: vaultState });
      if (endpoint === '/api/state') return reply({
        telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: unsent },
        web_sessions: 1, version: '9.8.7', claude: state,
      });
      return reply({ topics: [], commands: [], items: [], keys: [], files: [] });
    });
    const widths = [375, 390, 768, 844, 1024, 1180];
    const summaries = Object.fromEntries(widths.map(width => [width, {
      document: 0, stat: 0, name: Infinity, overlaps: 0, vault: 0, clipped: 0,
    }]));
    const failures = [];
    const waitForHeader = async () => {
      await page.locator('.slot-chip[data-next]').waitFor();
      await page.locator('#vaultBadge').getByText(config.locales[locale]['vault.unconfigured'], { exact: true }).waitFor();
      await page.evaluate(() => document.fonts.ready);
    };
    const screenshot = name =>
      page.screenshot({ path: path.join(config.shots, `fix3-${name}.png`), animations: 'disabled' });
    for (locale of Object.keys(config.locales)) {
      for (const [pending, delayed] of [[0, false], [3, false], [0, true], [3, true]]) {
        unsent = pending;
        state = initialState();
        if (delayed) {
          state.slots[0].state = 'cooldown';
          state.slots[0].cooldown_until = futureCooldown;
          state.slots[0].usage.windows.five_hour.resets_at = futureCooldown;
        }
        await page.goto(origin);
        await waitForHeader();
        assert.equal(await page.locator('#pendingBadge').isVisible(), unsent !== 0);
        for (const width of widths) {
          await page.setViewportSize({ width, height: 844 });
          const measure = await page.evaluate(() => {
            const box = selector => document.querySelector(selector).getBoundingClientRect();
            const stat = document.querySelector('.stat');
            const chip = '.slot-chip[data-next]';
            const parts = ['.slot-name', '.slot-mini', ':is(.slot-percent, .slot-label)']
              .map(part => box(`${chip} ${part}`));
            const detail = document.querySelector(`${chip} .slot-label`);
            const controls = [...document.querySelectorAll('.bar-acts button, .slot-more')]
              .filter(button => button.getClientRects().length)
              .map(button => button.getBoundingClientRect());
            const rectangles = [...parts, ...controls];
            const overlaps = rectangles.reduce((count, a, i) => count + rectangles.slice(i + 1)
              .filter(b => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom).length, 0);
            function textOverflow(element, bounds) {
              const range = document.createRange();
              range.selectNodeContents(element);
              return Math.max(0, ...Array.from(range.getClientRects(), rect =>
                Math.max(bounds.left - rect.left, rect.right - bounds.right)));
            }
            const badges = [...stat.querySelectorAll('.badge:not([hidden])')];
            const clipped = badges.filter(badge => textOverflow(badge, stat.getBoundingClientRect()) > 0.5).length;
            return {
              document: document.documentElement.scrollWidth - innerWidth,
              stat: stat.scrollWidth - stat.clientWidth,
              name: parts[0].width,
              overlaps,
              vault: textOverflow(document.querySelector('#vaultBadge span'), box('#vaultBadge')),
              labelClipped: detail ? detail.scrollWidth - detail.clientWidth : 0,
              clipped: clipped + parts.filter(part => part.width <= 0 || part.right > box(chip).right + 0.5).length,
            };
          });
          const summary = summaries[width];
          for (const key of ['document', 'stat', 'vault']) summary[key] = Math.max(summary[key], measure[key]);
          summary.name = Math.min(summary.name, measure.name);
          summary.overlaps += measure.overlaps;
          summary.clipped += measure.clipped;
          if (measure.document || measure.stat || measure.name <= 0 || measure.overlaps
              || measure.vault > 0.5 || measure.clipped || measure.labelClipped) {
            failures.push({ width, locale, unsent, ...measure });
          }

        }
      }
    }
    console.log(`Header layout: ${JSON.stringify(summaries)}`);

    const wideWidths = [1181, 1280, 1366, 1440, 1920];
    const wide = Object.fromEntries(wideWidths.map(width => [width, {
      height: 0, heightChange: 0, document: 0, overlaps: 0, clipped: 0, countErrors: 0, cases: 0,
    }]));
    const layout = () => page.evaluate(() => window.headerLayout());
    const resize = async width => {
      await page.evaluate(width => {
        window.headerResize = new Promise(resolve => {
          const observer = new ResizeObserver(() => {
            if (innerWidth !== width) return;
            observer.disconnect();
            resolve();
          });
          observer.observe(document.querySelector('.bar'));
        });
      }, width);
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(() => window.headerResize);
    };
    for (locale of Object.keys(config.locales)) {
      for (unsent of [0, 3]) {
        state = null;
        await page.goto(origin);
        await page.locator('#vaultBadge').getByText(config.locales[locale]['vault.unconfigured'], { exact: true }).waitFor();
        await page.waitForFunction(() => document.querySelector('#appSubtitle').textContent === 'v9.8.7');
        await page.evaluate(() => document.fonts.ready);
        const baseline = {};
        for (const width of wideWidths) {
          await resize(width);
          baseline[width] = (await layout()).height;
        }
        for (const count of [2, 5, 12]) {
          state = initialState();
          state.slots = state.slots.slice(0, count);
          while (state.slots.length < count) {
            state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'unauthenticated'));
          }
          await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
          await page.waitForFunction(count => document.querySelectorAll('.slot-row').length === count, count);
          for (const width of wideWidths) {
            await resize(width);
            const measure = await layout();
            const change = Math.abs(measure.height - baseline[width]);
            const summary = wide[width];
            summary.cases++;
            summary.height = Math.max(summary.height, measure.height);
            summary.heightChange = Math.max(summary.heightChange, change);
            summary.document = Math.max(summary.document, measure.document);
            summary.overlaps += measure.overlaps;
            summary.clipped += measure.clipped;
            const hidden = count - measure.shown;
            const correctCount = measure.more === (hidden ? `+${hidden}` : undefined);
            summary.countErrors += Number(!correctCount);
            if (change > 1 || measure.document || measure.overlaps || measure.clipped
                || measure.primary !== 1 || measure.refresh !== 2 || !correctCount) {
              failures.push({ width, locale, unsent, count, baseline: baseline[width], ...measure });
            }
            if (count === 5 && ((width === 1280 && locale === 'ko' && !unsent)
                || (width === 1366 && locale === 'de' && unsent))) {
              console.log(`${width}px ${locale}, five slots: next chip ${measure.primaryWidth}px, ${measure.shown} shown, ${measure.more || 'no overflow button'}`);
              await screenshot(`${width}-${locale}-5`);
            }
            if (count === 5 && unsent === 0 && width === 1440 && locale === 'ko') {
              console.log(`1440px Korean, five slots: ${measure.shown} chips visible`);
              if (measure.shown !== 5) failures.push({ width, locale, count, design: measure.shown });
              await screenshot('1440-ko-5');
            }
          }
        }
      }
    }
    console.log(`Wide header layout: ${JSON.stringify(wide)}`);

    locale = 'ko';
    unsent = 3;
    state = initialState();
    while (state.slots.length < 12) {
      state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'unauthenticated'));
    }
    await resize(1920);
    await page.goto(origin);
    await waitForHeader();
    async function sampleFrames(name, change, expected) {
      await page.evaluate(() => {
        const frames = [];
        window.headerFrames = frames;
        function sample() {
          if (window.headerFrames !== frames) return;
          // Sample each rendering cycle after the application's layout observers.
          const observer = new ResizeObserver(() => {
            observer.disconnect();
            if (window.headerFrames !== frames) return;
            frames.push(window.headerLayout());
            requestAnimationFrame(sample);
          });
          observer.observe(document.querySelector('.bar'));
        }
        requestAnimationFrame(sample);
      });
      await change();
      await page.waitForFunction(expected => window.headerFrames.filter(frame =>
        Object.entries(expected).every(([key, value]) => frame[key] === value)).length >= 3, expected);
      const frames = await page.evaluate(() => {
        const captured = window.headerFrames;
        window.headerFrames = null;
        return captured;
      });
      const bad = frames.filter(frame => {
        const hidden = state.slots.length - frame.shown;
        return frame.overlaps || frame.clipped || frame.document
          || frame.more !== (hidden ? `+${hidden}` : undefined);
      });
      console.log(`${name}: ${frames.length} frames, ${bad.length} with geometry or count errors, ${frames.filter(frame => frame.overlaps || frame.clipped).length} with overlap or clipping`);
      if (bad.length) failures.push({ transition: name, first: bad[0] });
    }
    const selectLanguage = async lang => {
      await page.locator('#languageButton').click();
      await page.locator(`[data-locale="${lang}"]`).click();
    };
    for (let round = 1; round <= 5; round++) {
      if (round > 1) {
        await selectLanguage('ko');
        await page.waitForFunction(() => document.documentElement.lang === 'ko');
      }
      await sampleFrames(`Korean to French ${round}`, () => selectLanguage('fr'), { language: 'fr' });
    }
    await screenshot('1920-fr-12-pending3');
    await sampleFrames('Resize', () => page.setViewportSize({ width: 1181, height: 900 }), { width: 1181 });
    const refreshState = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    unsent = 0;
    await sampleFrames('Pending badge removed', refreshState, { pending: false });
    unsent = 3;
    await sampleFrames('Pending badge added', refreshState, { pending: true });
    vaultState = 'locked';
    await sampleFrames('Vault label changed', () => page.evaluate(() =>
      document.dispatchEvent(new Event('vault-refresh'))), { vault: config.locales.fr['vault.locked'] });
    vaultState = 'unconfigured';
    await page.evaluate(() => localStorage.removeItem('dtw-web-language'));


    unsent = 0;
    for (const [kind, language, width, reset] of [
      ['api_key', 'en', 1440, cooldown], ['api_key_detached', 'en', 1440, cooldown],
      ['api_key', 'es', 375, cooldown], ['api_key', 'en', 375, futureCooldown],
      ['oauth', 'en', 375, futureCooldown],
    ]) {
      locale = language;
      await page.setViewportSize({ width, height: 900 });
      state = initialState();
      state.slots[2] = slot('claude-api', kind, 'cooldown', reset,
        kind === 'oauth' ? usage(1, reset, 0.4, reset + 3600) : null);
      state.next = 'claude-api';
      await page.goto(origin);
      await waitForHeader();
      const keyChip = page.locator('[data-slot-toggle="claude-api"]');
      await keyChip.click();
      const row = page.locator('.slot-row').filter({ hasText: 'claude-api' });
      const time = new Date(reset * 1000).toLocaleString(locale, {
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Asia/Seoul',
        ...(reset === futureCooldown ? { month: 'numeric', day: 'numeric' } : {}),
      });
      const resume = config.locales[locale]['slots.resume_at'].replace('{time}', time);
      const type = config.locales[locale][`slots.${kind}`];
      const labelFits = await keyChip.locator('.slot-label')
        .evaluate(label => label.scrollWidth <= label.clientWidth);
      if (!labelFits || !(await keyChip.innerText()).includes(resume)
          || !(await row.locator('.badge').innerText()).includes(resume)
          || (type && !(await row.innerText()).includes(type))
          || (kind === 'oauth' && await row.locator('.slot-reset').first().innerText() !== resume)) {
        failures.push({ kind, locale, width, labelFits, resume, chip: await keyChip.innerText(), row: await row.innerText() });
      }
      if (reset === futureCooldown && kind === 'oauth') await screenshot('future-cooldown');
    }
    const examples = [...failures.slice(0, 6), ...failures.filter(failure => failure.kind || failure.transition).slice(0, 15)];
    assert.equal(failures.length, 0, JSON.stringify(examples, null, 2));
  } finally {
    await page.close();
  }
}

async function check(browser, width, height, theme) {
  const strings = config.locales.ko;
  const context = await browser.newContext({
    viewport: { width, height }, colorScheme: theme, reducedMotion: 'reduce', timezoneId: 'Asia/Seoul',
  });
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    if (width === 1440 && theme === 'light') await page.clock.install({ time: new Date(now * 1000) });
    await page.clock.setFixedTime(new Date(now * 1000));
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let socket, state = initialState(), requests = 0, pending = 0, maxPending = 0, hold = null, fail = false;
    let onHeld;
    await page.routeWebSocket('**/api/ws', ws => { socket = ws; });
    await page.route('**/api/**', async route => {
      const endpoint = new URL(route.request().url()).pathname;
      const reply = json => route.fulfill({ json });
      if (endpoint === '/api/i18n') return reply({ lang: 'ko', strings });
      if (endpoint === '/api/auth/state') {
        return reply({ authenticated: true, csrf: 'fixture-csrf', bridge_up: true });
      }
      if (endpoint === '/api/state') {
        requests++;
        pending++;
        maxPending = Math.max(maxPending, pending);
        try {
          if (hold) {
            onHeld?.();
            await hold;
          }
          if (fail) return await route.fulfill({ status: 503, json: { error: 'bot_unreachable' } });
          return await reply({
            telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 },
            web_sessions: 1, version: '9.8.7', claude: state,
          });
        } finally {
          pending--;
        }
      }
      if (endpoint === '/api/masterkey/status') return reply({ state: 'unconfigured' });
      if (endpoint === '/api/topics') {
        return reply({ topics: [{
          tid: '1.2', chat_id: 1, topic_id: 2, name: 'General', group: 'telegram',
          count: 2, seen: 2, renamable: true, deletable: true, read_only: false, busy: false,
          preview: 'The deployment looks healthy.', created_at: null, last_ts: null, imported_count: 0,
        }] });
      }
      if (endpoint.endsWith('/messages')) {
        return reply({ entries: [
          { id: 'user-entry', seq: 1, role: 'user', source: 'web', ts: '2026-11-17T07:06:00Z', text: 'Check the web chat deployment.' },
          { id: 'bot-entry', seq: 2, role: 'bot', source: 'telegram', ts: '2026-11-17T07:08:00Z', text: 'The deployment looks healthy.' },
        ], has_more: false });
      }
      return reply({ topics: [], commands: [], items: [], keys: [], files: [] });
    });

    const widget = page.locator('#claudeSlots');
    const chip = widget.locator('.slot-chip[data-next]');
    const more = widget.locator('[data-slot-more]');
    const popover = widget.locator('.slot-popover');
    const refresh = popover.locator('[data-slot-refresh]');
    const header = popover.locator('.slot-popover-head');
    const updated = header.locator('time');
    const list = popover.locator('.slot-list');
    async function shot(name) {
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({
        path: path.join(config.shots, `slots-${width}x${height}-${theme}-${name}.png`), animations: 'disabled',
      });
    }

    await page.goto(origin);
    await chip.waitFor();
    await page.locator('#thread [data-entry-seq="2"]').waitFor();
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    assert.equal(await page.locator('.bar').evaluate(bar => bar.scrollWidth - bar.clientWidth), 0);
    const badge = await page.locator('#telegramBadge').boundingBox();
    const current = await chip.boundingBox();
    if (width !== 1280) {
      assert.ok(Math.abs(badge.y + badge.height / 2 - current.y - current.height / 2) <= 1,
        `${width}x${height}: ${JSON.stringify({ badge, current })}`);
    }
    const shown = await widget.locator('[data-slot-toggle]:visible').count();
    assert.equal(await more.isVisible(), shown < 5);
    assert.ok(width <= 1180 ? shown === 1 : shown >= 1 && shown <= 5);
    assert.equal(await widget.locator(':scope > .slot-refresh').isVisible(), width > 1180);
    if ([1440, 768, 375].includes(width)) {
      assert.equal(await widget.locator('.slot-chip:visible .slot-name, .slot-chip:visible .slot-label')
        .evaluateAll(labels => labels.every(label => label.scrollWidth <= label.clientWidth)), true);
    }
    assert.equal(await popover.isVisible(), false);
    if (height > 390) await shot('closed');
    const trigger = width <= 1180 ? more : chip;
    await trigger.click();
    await popover.waitFor();
    await page.mouse.move(0, 0);
    assert.equal(await trigger.getAttribute('aria-expanded'), 'true');
    assert.equal(await list.locator('.slot-row').count(), 5);
    assert.equal(await list.locator('.slot-window').count(), 4);
    assert.equal(await list.locator('.slot-captured').count(), 2);
    const bounds = await popover.boundingBox();
    assert.ok(bounds.x >= 11 && bounds.x + bounds.width <= width - 11);
    if (height <= 390) {
      const start = await header.boundingBox();
      const scroll = await list.evaluate(element => {
        element.scrollTop = element.scrollHeight;
        return element.scrollTop;
      });
      assert.ok(scroll > 0);
      assert.equal((await header.boundingBox()).y, start.y);
      const last = await list.locator('.slot-row').last().boundingBox();
      assert.ok(last.y + last.height <= height, `Last row bottom: ${last.y + last.height}`);
      console.log(`${width}x${height} ${theme}: last row bottom ${(last.y + last.height).toFixed(0)}px`);
      await shot('scrolled');
    } else if (width === 1440 || width === 390) {
      await shot('open');
    }

    await page.keyboard.press('Escape');
    assert.equal(await popover.isVisible(), false);
    assert.equal(await trigger.evaluate(button => button === document.activeElement), true);
    await trigger.click();
    await page.locator('.logo').click();
    assert.equal(await trigger.getAttribute('aria-expanded'), 'false');

    if (width === 1440 && theme === 'light') {
      await page.setViewportSize({ width: 1180, height });
      assert.equal(await widget.locator('[data-slot-toggle]:visible').count(), 1);
      assert.equal(await more.isVisible(), true);
      await page.setViewportSize({ width: 1181, height });
      await widget.locator(':scope > .slot-refresh').waitFor();
      assert.ok(await chip.isVisible());
      const widescreenShown = await widget.locator('[data-slot-toggle]:visible').count();
      assert.equal(await more.isVisible(), widescreenShown < 5);
      await page.setViewportSize({ width, height });

      await page.waitForFunction(() => !document.querySelector('#claudeSlots button[data-slot-refresh]').disabled);
      const initialRequests = requests;
      state.slots[0].usage.windows.five_hour.utilization = 0.35;
      await page.clock.setFixedTime(new Date((now + 60) * 1000));
      socket.send(JSON.stringify({ type: 'turn', tid: '1.2', turn_id: 'fixture-turn', phase: 'done' }));
      await chip.locator('.slot-percent').filter({ hasText: '35%' }).waitFor();
      assert.equal(requests, initialRequests + 1);
      await chip.click();
      assert.equal(await updated.getAttribute('datetime'), new Date((now + 60) * 1000).toISOString());
      assert.ok((await list.locator('.slot-captured').first().innerText()).includes('16:09'));

      let release;
      hold = new Promise(resolve => { release = resolve; });
      fail = true;
      await page.clock.setFixedTime(new Date((now + 120) * 1000));
      const request = page.waitForRequest('**/api/state');
      await refresh.evaluate(button => { button.click(); button.click(); });
      await request;
      assert.equal(await refresh.isDisabled(), true);
      assert.equal(await refresh.getAttribute('aria-busy'), 'true');
      await page.evaluate(async () => {
        const { handleEvent } = await import('/static/js/chat.js');
        await handleEvent({ type: 'turn', tid: '1.2', turn_id: 'fixture-turn', phase: 'done' });
        await handleEvent({ type: 'turn', tid: '1.2', turn_id: 'overlapping-turn', phase: 'aborted' });
      });
      assert.equal(requests, initialRequests + 2);
      release();
      await popover.locator('[data-slot-refresh][data-failed]').waitFor();
      assert.equal(requests, initialRequests + 2);
      assert.equal(maxPending, 1);
      assert.equal(await refresh.isDisabled(), false);
      assert.equal(await refresh.getAttribute('title'), strings['slots.refresh_failed']);
      assert.equal(await refresh.getAttribute('aria-label'), strings['slots.refresh_failed']);
      assert.equal(await chip.locator('.slot-percent').innerText(), '35%');
      assert.equal(await updated.getAttribute('datetime'), new Date((now + 60) * 1000).toISOString());
      console.log('Turn end: 1 request; rapid clicks plus overlapping turn: 1 request; failed refresh preserved data.');

      fail = false;
      hold = null;
      // Time out a shared visibility refresh, then recover on the next one.
      hold = new Promise(resolve => { release = resolve; });
      const hung = new Promise(resolve => { onHeld = resolve; });
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await hung;
      const heldRequests = requests;
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      assert.equal(requests, heldRequests);
      await page.clock.fastForward(15_001);
      await popover.locator('[data-slot-refresh][data-failed]').waitFor();
      assert.equal(await refresh.isDisabled(), false);
      assert.equal(await chip.locator('.slot-percent').innerText(), '35%');
      assert.equal(await updated.getAttribute('datetime'), new Date((now + 60) * 1000).toISOString());
      assert.equal(await page.locator('#botBadge').innerText(), strings['status.bot_down']);
      hold = null;
      state.slots[0].usage.windows.five_hour.utilization = 0.95;
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await chip.locator('.slot-percent').filter({ hasText: '95%' }).waitFor();
      assert.equal(requests, heldRequests + 1);
      assert.equal(await page.locator('#botBadge').innerText(), strings['status.bot_up']);
      release();
      console.log('Shared state request: recovered on the next visibility refresh after 15 seconds.');

      state.slots[2].kind = 'api_key_detached';
      state.slots[0].usage.windows.five_hour.resets_at = now - 60;
      delete state.slots[0].usage.windows.seven_day;
      await refresh.click();
      await list.getByText(strings['slots.reset_done'], { exact: true }).waitFor();
      await chip.locator('.slot-percent').waitFor();
      assert.equal(await widget.locator('[data-slot-toggle="claude-api"]').getByText(strings['slots.api_key_detached']).count(), 1);
      assert.equal(await list.locator('.slot-row').first().locator('.slot-window').count(), 1);
      assert.equal(await list.locator('.slot-row').first().locator('.slot-percent').innerText(), '0%');
      assert.equal(await chip.locator('.slot-percent').innerText(), '0%');
      assert.equal(await chip.locator('.slot-mini').getAttribute('value'), '0');
      await page.screenshot({ path: path.join(config.shots, 'slots-reset-window.png'), animations: 'disabled' });

      state = null;
      await refresh.click();
      await widget.waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#telegramBadge').isVisible(), true);
    }
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

try {
  const browser = await chromium.launch({ headless: true });
  try {
    await checkHeaderMatrix(browser);
    for (const [width, height, theme] of [
      [1440, 900, 'light'], [1440, 900, 'dark'], [390, 844, 'light'],
      [844, 390, 'light'], [1280, 900, 'light'], [768, 900, 'light'], [375, 844, 'light'],
    ]) await check(browser, width, height, theme);
  } finally {
    await browser.close();
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
