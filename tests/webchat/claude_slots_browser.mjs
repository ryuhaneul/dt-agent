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
      slot('claude-new', 'oauth', 'ok'),
    ],
  };
}

async function checkHeaderMatrix(browser, cooldownTransition) {
  const page = await browser.newPage({ viewport: { width: 844, height: 844 }, timezoneId: 'Asia/Seoul' });
  try {
    await page.clock.setFixedTime(new Date(now * 1000));
    let locale, unsent, codex, state = initialState(), vaultState = 'unconfigured';
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
        const overlaps = rectangles.flatMap((a, i) => rectangles.slice(i + 1)
          .filter(b => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom)
          .map(b => Math.min(a.right, b.right) - Math.max(a.left, b.left)));
        return {
          height: document.querySelector('.bar').getBoundingClientRect().height,
          neighborWidth: ['.stat', '.bar-acts'].reduce((sum, selector) =>
            sum + document.querySelector(selector).getBoundingClientRect().width, 0),
          document: document.documentElement.scrollWidth - innerWidth,
          overlaps: overlaps.length,
          maxOverlap: Math.max(0, ...overlaps),
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
        web_sessions: 1, version: '9.8.7', claude: state, codex,
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
    const layout = () => page.evaluate(() => window.headerLayout());
    const settle = () => page.evaluate(() => new Promise(resolve => {
      const observer = new ResizeObserver(() => {
        observer.disconnect();
        resolve();
      });
      observer.observe(document.querySelector('.bar'));
    }));
    if (cooldownTransition) {
      const { language, count } = cooldownTransition;
      locale = language;
      unsent = 3;
      state.slots = state.slots.slice(0, count);
      state.slots[0] = slot('claude', 'oauth', 'cooldown', futureCooldown,
        usage(1, futureCooldown, 0.41, futureCooldown));
      await page.goto(origin);
      await waitForHeader();
      await checkBreakpoint(`${locale} cooldown, ${count} slots`);
      return failures;
    }
    let loadedLocale;
    const showHeaderFixture = async () => {
      if (loadedLocale !== locale) {
        await page.goto(origin);
        loadedLocale = locale;
      } else {
        await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      }
      await page.waitForFunction(({ count, pending, status, language, vault }) =>
        document.documentElement.lang === language
          && document.querySelector('#appSubtitle').textContent === 'v9.8.7'
          && document.querySelector('#vaultBadge span')?.textContent === vault
          && !document.querySelector('#pendingBadge').hidden === pending
          && document.querySelectorAll('.slot-row').length === count
          && document.querySelector('.slot-chip[data-next]')?.dataset.status === status, {
        count: state?.slots.length ?? 0,
        pending: unsent !== 0,
        status: state?.slots[0].state,
        language: locale,
        vault: config.locales[locale]['vault.unconfigured'],
      });
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
        await showHeaderFixture();
        assert.equal(await page.locator('#pendingBadge').isVisible(), unsent !== 0);
        for (const width of widths) {
          await page.setViewportSize({ width, height: 844 });
          const measure = await page.evaluate(() => {
            const box = selector => document.querySelector(selector).getBoundingClientRect();
            const stat = document.querySelector('.stat');
            const chip = '.slot-chip[data-next]';
            const parts = [...document.querySelectorAll(`${chip} :is(.slot-name, .slot-period, .slot-mini, .slot-percent, .slot-label)`)]
              .filter(element => element.getClientRects().length)
              .map(element => element.getBoundingClientRect());
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
    let longestLocale = { locale: '', width: 0 };
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
        await showHeaderFixture();
        const baseline = {};
        for (const width of wideWidths) {
          await resize(width);
          baseline[width] = (await layout()).height;
        }
        for (const count of [2, 5, 12]) {
          state = initialState();
          state.slots = state.slots.slice(0, count);
          while (state.slots.length < count) {
            state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'ok'));
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
            if (count === 5 && width === 1280 && !unsent && locale !== 'ko'
                && measure.neighborWidth > longestLocale.width) {
              longestLocale = { locale, width: measure.neighborWidth };
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

    let cooldownCases = 0;
    let cooldownOverflows = 0;
    let labelOverflows = 0;
    let maxLabelOverflow = 0;
    let shiftedBadges = 0;
    let maxBadgeShift = 0;
    let wrappedCases = 0;
    let widthMismatches = 0;
    let maxGlyphClipping = 0;
    await page.setViewportSize({ width: 1181, height: 900 });
    for (locale of Object.keys(config.locales)) {
      for (unsent of [0, 3]) {
        for (const count of [2, 5, 12]) {
          state = initialState();
          state.slots[0] = slot('claude', 'oauth', 'cooldown', futureCooldown,
            usage(1, futureCooldown, 0.41, futureCooldown));
          state.slots = state.slots.slice(0, count);
          while (state.slots.length < count) {
            state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'ok'));
          }
          await showHeaderFixture();
          const measure = await page.locator('.slot-chip[data-next]').evaluate(chip => {
            const box = chip.getBoundingClientRect();
            const name = chip.querySelector('.slot-name');
            const label = chip.querySelector('.slot-label');
            const textOverflow = element => {
              const range = document.createRange();
              range.selectNodeContents(element);
              return Math.max(0, ...[...range.getClientRects()].map(rect => rect.right - box.right));
            };
            const wrapped = chip.hasAttribute('data-wrap-cooldown');
            let glyphClipping = 0;
            if (wrapped) {
              // The next chip has a 1px inset outline inside its border.
              const inner = chip.clientTop + 1;
              const canvas = document.createElement('canvas').getContext('2d');
              const range = document.createRange();
              for (const row of [name, label]) {
                const style = getComputedStyle(row);
                const bounds = row.getBoundingClientRect();
                const node = row.firstChild;
                // A zero-height inline box locates the baseline without changing the text width.
                const marker = document.createElement('span');
                marker.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
                row.append(marker);
                const baseline = marker.getBoundingClientRect().bottom;
                marker.remove();
                canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                let offset = 0;
                for (const glyph of node.data) {
                  range.setStart(node, offset);
                  offset += glyph.length;
                  if (!glyph.trim()) continue;
                  range.setEnd(node, offset);
                  const x = range.getBoundingClientRect().left;
                  const ink = canvas.measureText(glyph);
                  glyphClipping = Math.max(glyphClipping,
                    Math.max(bounds.top, box.top + inner) - (baseline - ink.actualBoundingBoxAscent),
                    baseline + ink.actualBoundingBoxDescent - Math.min(bounds.bottom, box.bottom - inner),
                    Math.max(bounds.left, box.left + inner) - (x - ink.actualBoundingBoxLeft),
                    x + ink.actualBoundingBoxRight - Math.min(bounds.right, box.right - inner));
                }
              }
            }
            const stat = document.querySelector('.stat');
            const widthLimit = chip.style.getPropertyValue('--slot-cooldown-width');
            const measure = {
              badgeLeft: stat.getBoundingClientRect().left,
              headerHeight: document.querySelector('.bar').getBoundingClientRect().height,
              chipHeight: box.height,
              chipWidth: box.width,
              widthLimit: Number.parseFloat(widthLimit),
              wrapped, glyphClipping,
              nameClipped: name.scrollWidth > name.clientWidth || textOverflow(name) > 0.5,
              labelClipped: label.scrollWidth > label.clientWidth,
              labelOverflow: textOverflow(label),
            };
            let referenceBadgeLeft = measure.badgeLeft;
            if (wrapped && widthLimit) {
              // Measure the single-line layout with the same width limit.
              const maxWidth = chip.style.maxWidth;
              try {
                chip.removeAttribute('data-wrap-cooldown');
                chip.style.maxWidth = widthLimit;
                referenceBadgeLeft = stat.getBoundingClientRect().left;
              } finally {
                chip.style.maxWidth = maxWidth;
                chip.setAttribute('data-wrap-cooldown', '');
              }
            }
            return { ...measure, referenceBadgeLeft };
          });
          cooldownCases++;
          labelOverflows += Number(measure.labelClipped || measure.labelOverflow > 0.5);
          maxLabelOverflow = Math.max(maxLabelOverflow, measure.labelOverflow);
          if (measure.nameClipped || measure.labelClipped || measure.labelOverflow > 0.5) {
            cooldownOverflows++;
            failures.push({ cooldown: 'future', width: 1181, locale, unsent, count, ...measure });
          }
          const shift = Math.abs(measure.badgeLeft - measure.referenceBadgeLeft);
          const widthMismatch = measure.wrapped && (!Number.isFinite(measure.widthLimit)
            || Math.abs(measure.chipWidth - measure.widthLimit) > 0.5);
          shiftedBadges += Number(shift > 0.5);
          maxBadgeShift = Math.max(maxBadgeShift, shift);
          wrappedCases += Number(measure.wrapped);
          widthMismatches += Number(widthMismatch);
          maxGlyphClipping = Math.max(maxGlyphClipping, measure.glyphClipping);
          if (shift > 0.5 || widthMismatch || measure.glyphClipping > 0.5
              || measure.chipHeight !== 30 || measure.headerHeight !== 65) {
            failures.push({ cooldown: 'geometry', width: 1181, locale, unsent, count, widthMismatch, ...measure });
          }
          if (locale === 'de' && unsent === 3 && count === 2) await screenshot('issue87-de-1181');
        }
      }
    }
    console.log(`Issue 87 cooldown: ${cooldownOverflows}/${cooldownCases} overflowing cases (${labelOverflows} labels), max label overflow ${maxLabelOverflow.toFixed(1)}px`);
    console.log(`Issue 87 cooldown geometry: ${shiftedBadges}/${cooldownCases} badge shifts, max ${maxBadgeShift.toFixed(2)}px; ${widthMismatches}/${wrappedCases} wrapped chip width mismatches, max glyph clipping ${maxGlyphClipping.toFixed(2)}px`);
    assert.equal(cooldownCases, 54);
    assert.ok(wrappedCases > 0);

    async function checkBreakpoint(name) {
      for (const width of [1440, 1180, 1181]) {
        await page.setViewportSize({ width, height: 900 });
        await settle();
        const measure = await layout();
        if (measure.overlaps || measure.clipped || measure.document) {
          failures.push({ transition: name, settled: true, ...measure });
        }
      }
      const frames = [];
      for (let repeat = 0; repeat < 60; repeat++) {
        for (const width of [1440, 1180]) {
          await page.setViewportSize({ width, height: 900 });
          await settle();
        }
        await page.evaluate(() => {
          window.breakpointFrames = new Promise(resolve => {
            const samples = [];
            function sample() {
              if (innerWidth === 1181) {
                const layout = window.headerLayout();
                const primary = document.querySelector('.slot-chip[data-primary]');
                const chip = primary.getBoundingClientRect();
                const meterOverflow = [...primary.querySelectorAll('.slot-mini')]
                  .filter(meter => meter.getClientRects().length)
                  .some(meter => meter.getBoundingClientRect().right > chip.right + 0.5);
                samples.push({
                  overlaps: layout.overlaps, maxOverlap: layout.maxOverlap, meterOverflow, primaryWidth: layout.primaryWidth,
                });
              }
              if (samples.length === 8) resolve(samples);
              else requestAnimationFrame(sample);
            }
            requestAnimationFrame(sample);
          });
        });
        await page.setViewportSize({ width: 1181, height: 900 });
        frames.push(...await page.evaluate(() => window.breakpointFrames));
      }
      const overlaps = frames.filter(frame => frame.overlaps).length;
      const meters = frames.filter(frame => frame.meterOverflow).length;
      const minWidth = Math.min(...frames.map(frame => frame.primaryWidth));
      const maxOverlap = Math.max(...frames.map(frame => frame.maxOverlap));
      console.log(`Issue 87 ${name}: ${frames.length} frames, ${overlaps} with control overlap (max ${maxOverlap.toFixed(1)}px), ${meters} with meter overflow, min next chip at 1181px ${minWidth.toFixed(1)}px`);
      assert.equal(frames.length, 480);
      if (overlaps || meters) failures.push({ transition: name, overlaps, maxOverlap, meters, minWidth });
    }

    locale = 'fr';
    unsent = 3;
    state = initialState();
    while (state.slots.length < 12) {
      state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'ok'));
    }
    await page.goto(origin);
    await waitForHeader();
    await checkBreakpoint('French, 12 Claude slots');
    await screenshot('issue87-fr-1181');

    locale = 'ko';
    unsent = 0;
    const teamUsage = usage(0.26, cooldown, 0.94, futureCooldown);
    state = { next: 'claude-team', slots: [
      slot('claude', 'oauth', 'ok', null, teamUsage),
      slot('claude-weekly', 'oauth', 'ok', null, { captured_at: now, windows: {
        seven_day: teamUsage.windows.seven_day,
      } }),
      slot('claude-empty', 'oauth', 'ok'),
      slot('claude-team', 'oauth', 'ok', null, teamUsage),
    ] };
    codex = { captured_at: now, windows: [
      { used_percent: 64, window_minutes: 300, resets_at: cooldown },
      { used_percent: 91, window_minutes: 10080, resets_at: futureCooldown },
    ] };
    await page.goto(origin);
    await waitForHeader();
    await page.locator('.slot-chip[data-provider="codex"]').waitFor({ state: 'attached' });
    await checkBreakpoint('Korean, four Claude slots and Codex');
    codex = null;

    locale = 'ko';
    unsent = 3;
    state = initialState();
    while (state.slots.length < 12) {
      state.slots.push(slot(`claude-slot-${state.slots.length + 1}`, 'oauth', 'ok'));
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
    await sampleFrames('1181 to 1180', () => page.setViewportSize({ width: 1180, height: 900 }), { width: 1180 });
    await sampleFrames('1180 to 1181', () => page.setViewportSize({ width: 1181, height: 900 }), { width: 1181 });
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
      ['api_key', 'en', 1440, cooldown],
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
    return longestLocale;
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
      await more.waitFor();
      assert.equal(await widget.locator('[data-slot-toggle]:visible').count(), 1);
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
      assert.equal(await chip.locator('.slot-percent').first().innerText(), '35%');
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
      assert.equal(await chip.locator('.slot-percent').first().innerText(), '35%');
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

      state.slots[0].usage.windows.five_hour.resets_at = now - 60;
      delete state.slots[0].usage.windows.seven_day;
      await refresh.click();
      await list.getByText(strings['slots.reset_done'], { exact: true }).waitFor();
      await chip.locator('.slot-percent').waitFor();
      assert.equal(await widget.locator('[data-slot-toggle="claude-api"]').getByText(strings['slots.api_key']).count(), 1);
      assert.equal(await list.locator('.slot-row').first().locator('.slot-window').count(), 1);
      assert.equal(await list.locator('.slot-row').first().locator('.slot-percent').innerText(), '0%');
      assert.equal(await chip.locator('.slot-percent').innerText(), '0%');
      assert.equal(await chip.locator('.slot-mini').getAttribute('value'), '0');
      await page.screenshot({ path: path.join(config.shots, 'slots-reset-window.png'), animations: 'disabled' });

      state = { next: null, slots: [] };
      await refresh.click();
      await widget.waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#telegramBadge').isVisible(), true);
      console.log('An empty Claude slot list hides the widget.');

      state = initialState();
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await widget.waitFor();
      state = null;
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await widget.waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#telegramBadge').isVisible(), true);
    }
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

async function checkUsageDesign(browser, longestLocale) {
  const firstReset = epoch('2026-11-17T19:40:00+09:00');
  const weekReset = epoch('2026-11-20T09:00:00+09:00');
  const weekly = usage(0, firstReset, 0.44, weekReset);
  delete weekly.windows.five_hour;
  const claude = {
    next: 'claude-team',
    slots: [
      slot('claude', 'oauth', 'ok', null, usage(0.12, firstReset, 0.44, weekReset)),
      slot('claude-sub', 'oauth', 'ok', null, weekly),
      slot('claude-team', 'oauth', 'ok', null, usage(0.91, firstReset, 0.30, weekReset)),
    ],
  };
  const codex = {
    captured_at: now - 120,
    windows: [
      { used_percent: 55, window_minutes: 10080, resets_at: weekReset },
      { used_percent: 20, window_minutes: 300, resets_at: firstReset },
    ],
  };
  console.log(`Longest comparison locale: ${longestLocale.locale} (${longestLocale.width}px of header controls)`);
  for (const locale of ['ko', longestLocale.locale]) {
    for (const [width, height, theme] of [
      [1280, 900, 'light'], [1280, 900, 'dark'], [1440, 900, 'light'],
      [768, 900, 'light'], [375, 844, 'light'],
    ]) {
      const page = await browser.newPage({
        viewport: { width, height }, deviceScaleFactor: 2, colorScheme: theme, timezoneId: 'Asia/Seoul',
        reducedMotion: 'reduce',
      });
      try {
        let claudeState = claude;
        let codexState = codex;
        const strings = config.locales[locale];
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.clock.setFixedTime(new Date(now * 1000));
        await page.routeWebSocket('**/api/ws', () => {});
        await page.route('**/api/**', route => {
          const endpoint = new URL(route.request().url()).pathname;
          const reply = json => route.fulfill({ json });
          if (endpoint === '/api/i18n') return reply({ lang: locale, strings });
          if (endpoint === '/api/auth/state') return reply({ authenticated: true, csrf: 'fixture-csrf', bridge_up: true });
          if (endpoint === '/api/masterkey/status') return reply({ state: 'unconfigured' });
          if (endpoint === '/api/state') return reply({
            telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 }, version: '9.8.7',
            web_sessions: 1, claude: claudeState, codex: codexState,
          });
          return reply({ topics: [], commands: [], items: [], keys: [], files: [] });
        });
        await page.goto(origin);
        const widget = page.locator('#claudeSlots');
        const primary = widget.locator('[data-primary]');
        const chips = widget.locator('[data-slot-toggle]');
        const more = widget.locator('[data-slot-more]');
        const popover = widget.locator('.slot-popover');
        await primary.waitFor();
        await page.locator('#vaultBadge').getByText(strings['vault.unconfigured'], { exact: true }).waitFor();
        await page.evaluate(() => document.fonts.ready);
        const compact = await widget.getAttribute('data-compact') !== null;
        const measure = await widget.evaluate(root => {
          const controls = [...document.querySelectorAll(
            '.bar-acts button, .stat .badge, .slot-chips button, .claude-slots>.slot-refresh, .claude-slots>time'
          )].filter(element => element.getClientRects().length && !element.closest('.menu'));
          const boxes = controls.map(element => element.getBoundingClientRect());
          const visible = [...root.querySelectorAll('[data-slot-toggle]')]
            .filter(element => element.getClientRects().length);
          return {
            overflow: document.documentElement.scrollWidth - innerWidth,
            overlaps: boxes.some((a, i) => boxes.slice(i + 1).some(b =>
              a.left + 0.1 < b.right && b.left + 0.1 < a.right
              && a.top + 0.1 < b.bottom && b.top + 0.1 < a.bottom)),
            clipped: visible.some(element => element.scrollWidth > element.clientWidth),
            widths: Object.fromEntries(visible.map(element =>
              [element.dataset.slotToggle, Number(element.getBoundingClientRect().width.toFixed(1))])),
            height: document.querySelector('.bar').getBoundingClientRect().height,
          };
        });
        const scenario = `${locale}-${width}-${theme}`;
        assert.deepEqual([measure.overflow, measure.overlaps, measure.clipped], [0, false, false], scenario);
        assert.equal(compact, width <= 768, scenario);
        assert.equal(await primary.locator('.slot-chip-window').count(), 2);
        assert.deepEqual(await primary.locator('.slot-chip-window .slot-period').allTextContents(), ['5h', '7d']);
        assert.equal(await primary.locator('.slot-mini:visible').count(), compact ? 0 : 2);
        for (const [name, periods, values] of [
          ['claude', ['5h', '7d'], ['12', '44']], ['claude-sub', ['7d'], ['44']],
          ['claude-team', ['5h', '7d'], ['91', '30']], ['Codex', ['5h', '7d'], ['20', '55']],
        ]) {
          const chip = widget.locator(`[data-slot-toggle="${name}"]`);
          assert.deepEqual(await chip.locator('.slot-period').allTextContents(), periods);
          assert.deepEqual((await chip.locator('.slot-percent').allTextContents())
            .map(value => value.replace(/[^0-9]/g, '')), values);
        }
        assert.equal(await primary.locator('.slot-percent[data-level="high"]').count(), 1);
        assert.deepEqual(await chips.evaluateAll(nodes => nodes.map(node => node.dataset.slotToggle)),
          ['claude', 'claude-sub', 'claude-team', 'Codex']);
        const shown = Object.keys(measure.widths);
        if (width <= 768) assert.deepEqual(shown, ['claude-team']);
        if (shown.includes('Codex')) assert.equal(shown.length, 4);
        assert.equal(await more.isVisible(), shown.length !== 4);
        if (shown.length !== 4) assert.equal((await more.innerText()).trim(), `+${4 - shown.length}`);
        console.log(`Usage layout ${scenario}: ${JSON.stringify({ compact, ...measure })}`);

        const shot = (state, options = {}) => page.screenshot({
          path: path.join(config.shots, `usage-${scenario}-${state}.png`),
          animations: 'disabled', scale: 'css', ...options,
        });
        await shot('closed');
        if (width === 1280) {
          const { x, y, width: w, height: h } = await widget.locator('.slot-chips').boundingBox();
          await shot('closed-chips-2x', {
            scale: 'device',
            clip: { x: Math.floor(x) - 5, y: Math.floor(y) - 5, width: Math.ceil(w) + 10, height: Math.ceil(h) + 10 },
          });
        }
        await (shown.length === 4 ? primary : more).click();
        await popover.waitFor();
        const rows = popover.locator('.slot-row');
        assert.deepEqual(await rows.locator('.slot-row-top .slot-name').allTextContents(),
          ['claude', 'claude-sub', 'claude-team', 'Codex']);
        for (const [index, durations, resets] of [
          [0, [strings['slots.five_hour'], strings['slots.seven_day']], [firstReset, weekReset]],
          [1, [strings['slots.seven_day']], [weekReset]],
          [2, [strings['slots.five_hour'], strings['slots.seven_day']], [firstReset, weekReset]],
          [3, ['5h', '7d'], [firstReset, weekReset]],
        ]) {
          const row = rows.nth(index);
          assert.deepEqual(await row.locator('.slot-window > span:first-child').allTextContents(), durations);
          const time = seconds => new Date(seconds * 1000).toLocaleString(locale, {
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Asia/Seoul',
            ...(seconds === weekReset ? { month: 'numeric', day: 'numeric' } : {}),
          });
          assert.deepEqual(await row.locator('.slot-reset').allTextContents(),
            resets.map(reset => strings['slots.reset_at'].replace('{time}', time(reset))));
          assert.equal(await row.locator('.slot-captured').innerText(),
            strings['slots.captured_at'].replace('{time}', time(now - 120)));
        }
        assert.deepEqual((await rows.last().locator('.slot-percent').allTextContents())
          .map(value => value.replace(/[^0-9]/g, '')), ['20', '55']);
        await shot('open');

        const refresh = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
        codexState = { ...codex, windows: [codex.windows[0]] };
        await refresh();
        await rows.last().locator('.slot-window').first().getByText('7d', { exact: true }).waitFor();
        await page.waitForFunction(() =>
          document.querySelectorAll('[data-slot-toggle="Codex"] .slot-chip-window').length === 1);
        assert.equal(await rows.last().locator('.slot-window').count(), 1);

        codexState = null;
        await refresh();
        await widget.locator('[data-slot-toggle="Codex"]').waitFor({ state: 'detached' });
        assert.equal(await rows.count(), 3);
        assert.equal(await rows.getByText('Codex', { exact: true }).count(), 0);

        claudeState = null;
        codexState = { ...codex, windows: [{ used_percent: 0, window_minutes: null, resets_at: null }] };
        await refresh();
        await widget.locator('[data-slot-toggle="Codex"]').waitFor();
        assert.equal(await widget.locator('[data-slot-toggle="Codex"] .slot-period').innerText(), '?');
        assert.equal(await rows.count(), 1);
        assert.equal(await rows.first().locator('.slot-reset').innerText(), '');
        assert.deepEqual(errors, []);
      } finally {
        await page.close();
      }
    }
  }
}

async function checkUsageRegressions(browser) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 900 }, timezoneId: 'Asia/Seoul' });
  try {
    const topic = { tid: '1.2', name: 'Usage topic', group: 'telegram', unread: 0 };
    const observation = percent => ({
      captured_at: now, windows: [{ used_percent: percent, window_minutes: 300, resets_at: cooldown }],
    });
    let claude = initialState(), codex = null, stateReads = 0, socket, holdState, releaseCommand;
    let commandReceived, stateStarted;
    const commandStarted = new Promise(resolve => { commandReceived = resolve; });
    await page.clock.setFixedTime(new Date(now * 1000));
    await page.routeWebSocket('**/api/ws', connection => { socket = connection; });
    await page.route('**/api/**', async route => {
      const endpoint = new URL(route.request().url()).pathname;
      const reply = json => route.fulfill({ json });
      if (endpoint === '/api/i18n') return reply({ lang: 'ko', strings: config.locales.ko });
      if (endpoint === '/api/auth/state') return reply({ authenticated: true, csrf: 'fixture', bridge_up: true });
      if (endpoint === '/api/masterkey/status') return reply({ state: 'unconfigured' });
      if (endpoint === '/api/state') {
        stateReads++;
        const snapshot = codex;
        const hold = holdState;
        holdState = null;
        if (hold) {
          stateStarted();
          await hold;
        }
        return reply({
          telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 },
          web_sessions: 1, claude, codex: snapshot,
        });
      }
      if (endpoint === '/api/topics') return reply({ topics: [topic] });
      if (endpoint.endsWith('/messages')) {
        if (route.request().method() !== 'POST') return reply({ entries: [], has_more: false });
        assert.equal(route.request().postDataJSON().text, '/usage');
        await new Promise(resolve => {
          releaseCommand = resolve;
          commandReceived();
        });
        return reply({ text: 'Usage result', buttons: [] });
      }
      return reply({ commands: [], items: [], keys: [], files: [] });
    });
    await page.goto(origin);
    await page.waitForURL('**/#chat');
    const widget = page.locator('#claudeSlots');
    const codexChip = widget.locator('[data-provider="codex"]');
    const claudeNamedCodex = widget.locator('[data-provider="claude"][data-slot-toggle="Codex"]');
    const stateResponse = () => page.waitForResponse(response => response.url().endsWith('/api/state'));
    await widget.locator('[data-primary]').waitFor();
    await page.waitForFunction(() => !document.querySelector('#msgIn').disabled);
    await page.waitForFunction(() => !document.querySelector('#claudeSlots>.slot-refresh').disabled);
    const send = event => socket.send(JSON.stringify(event));
    const changeUsage = () => send({ type: 'codex_usage_changed' });
    const endTurn = id => send({ type: 'turn', tid: topic.tid, turn_id: id, phase: 'done' });

    async function expectPercent(percent) {
      await codexChip.locator('.slot-percent').filter({ hasText: `${percent}%` }).waitFor({ state: 'attached' });
      await page.waitForFunction(() => !document.querySelector('#claudeSlots>.slot-refresh').disabled);
    }

    let before = stateReads;
    let releaseState;
    holdState = new Promise(resolve => { releaseState = resolve; });
    const stale = stateResponse();
    const stateArrived = new Promise(resolve => { stateStarted = resolve; });
    endTurn('first-codex-turn');
    await stateArrived;
    assert.equal(await codexChip.count(), 0);
    codex = observation(10);
    changeUsage();
    changeUsage();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    releaseState();
    await stale;
    await expectPercent(10);
    assert.equal(stateReads - before, 2);
    console.log('First Codex observation: 1 stale turn request + 1 event-triggered request');

    before = stateReads;
    const turnState = stateResponse();
    endTurn('second-codex-turn');
    await turnState;
    await expectPercent(10);
    codex = observation(77);
    changeUsage();
    await expectPercent(77);
    assert.equal(stateReads - before, 2);
    console.log('Updated Codex observation: 1 turn request + 1 event-triggered request');

    before = stateReads;
    const commandResponse = page.waitForResponse(response =>
      response.url().endsWith('/messages') && response.request().method() === 'POST');
    await page.locator('#msgIn').fill('/usage');
    await page.locator('#sendBtn').click();
    await commandStarted;
    assert.equal(stateReads, before);
    codex = observation(52);
    changeUsage();
    releaseCommand();
    await commandResponse;
    await expectPercent(52);
    assert.equal(stateReads - before, 1);
    console.log('Web /usage: 1 event-triggered request');

    claude = { next: 'claude', slots: [
      slot('claude', 'oauth', 'ok'), slot('Codex', 'oauth', 'ok'),
      slot('claude-long-alternate-name', 'oauth', 'ok'), slot('claude-other', 'oauth', 'ok'),
    ] };
    await page.setViewportSize({ width: 1280, height: 900 });
    codex = null;
    changeUsage();
    await claudeNamedCodex.waitFor();
    assert.equal(await codexChip.count(), 0);
    codex = observation(52);
    changeUsage();
    await codexChip.waitFor({ state: 'attached' });
    assert.equal(await claudeNamedCodex.isVisible(), true);
    assert.equal(await codexChip.isVisible(), false);
    await page.setViewportSize({ width: 1920, height: 900 });
    await codexChip.click();
    const popover = widget.locator('.slot-popover');
    await popover.waitFor();
    assert.equal(await popover.locator('.slot-name').getByText('Codex', { exact: true }).count(), 2);
    codex = observation(64);
    changeUsage();
    await expectPercent(64);
    await page.keyboard.press('Escape');
    assert.equal(await codexChip.evaluate(button => button === document.activeElement), true);
    console.log('Claude slot named Codex: kept before the Codex provider chip and focus restored to its provider');

    claude = { next: 'claude-team', slots: [
      slot('claude', 'oauth', 'ok', null, usage(0.26, cooldown, 0.94, futureCooldown)),
      slot('claude-weekly', 'oauth', 'ok', null, { captured_at: now, windows: {
        seven_day: { utilization: 0.94, resets_at: futureCooldown },
      } }),
      slot('claude-empty', 'oauth', 'ok'),
      slot('claude-team', 'oauth', 'ok', null, usage(0.26, cooldown, 0.94, futureCooldown)),
    ] };
    codex.windows.push({ used_percent: 91, window_minutes: 10080, resets_at: futureCooldown });
    changeUsage();
    await widget.locator('[data-primary][data-slot-toggle="claude-team"]').waitFor();
    const settle = () => page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))));
    let clipped = 0;
    for (let repeat = 0; repeat < 20; repeat++) {
      await page.setViewportSize({ width: 1440, height: 900 });
      await settle();
      await page.setViewportSize({ width: 1180, height: 900 });
      await settle();
      await page.evaluate(() => {
        window.usageTransition = [];
        function sample() {
          if (innerWidth === 1181) {
            const primary = document.querySelector('#claudeSlots [data-primary]');
            const chip = primary.getBoundingClientRect();
            const parts = [...primary.querySelectorAll('.slot-name,.slot-period,.slot-mini,.slot-percent')];
            window.usageTransition.push(parts.filter(part => part.getClientRects().length).some(part => {
              const bounds = part.getBoundingClientRect();
              return bounds.width === 0 || bounds.left < chip.left - 0.6 || bounds.right > chip.right + 0.6;
            }));
          }
          if (window.usageTransition.length < 8) requestAnimationFrame(sample);
        }
        requestAnimationFrame(sample);
      });
      await page.setViewportSize({ width: 1181, height: 900 });
      await page.waitForFunction(() => window.usageTransition.length === 8);
      clipped += await page.evaluate(() => window.usageTransition.filter(Boolean).length);
    }
    console.log(`1180 to 1181 usage transition: ${clipped}/160 clipped frames`);
    assert.equal(clipped, 0);

  } finally {
    await page.close();
  }
}


async function checkUsageSlotOrder(browser, width, lifecycle) {
  const page = await browser.newPage({ viewport: { width, height: lifecycle === 'scroll' ? 667 : 812 } });
  page.setDefaultTimeout(3500);
  try {
    const long = 'claude-long-research-account-with-a-descriptive-name';
    const slots = lifecycle === 'scroll'
      ? Array.from({ length: 14 }, (_, index) => slot(`claude-slot-${index}`, 'oauth', 'ok'))
      : [slot('claude', 'oauth', 'ok'), slot(long, 'oauth', 'ok'), slot('claude-backup', 'oauth', 'ok')];
    let singleton = false, rejectSwap = false, release, swapStarted, authenticated = true, session = 1;
    let stateReads = 0, stateUnavailable = false, staleView = false;
    const calls = [];
    await page.routeWebSocket('**/api/ws', () => {});
    await page.route('**/api/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url()).pathname;
      if (endpoint === '/api/i18n') return route.fulfill({ json: { lang: 'en', strings: config.locales.en } });
      if (endpoint === '/api/auth/state') return route.fulfill({ json: {
        authenticated, csrf: authenticated ? `fixture-${session}` : null, bridge_up: true, totp_configured: true,
      } });
      if (endpoint === '/api/auth/password') return route.fulfill({ json: { next: 'totp' } });
      if (endpoint === '/api/auth/totp') {
        authenticated = true; session++;
        return route.fulfill({ json: { ok: true } });
      }
      if (endpoint === '/api/masterkey/status') return route.fulfill({ json: { state: 'unconfigured' } });
      if (endpoint === '/api/state') {
        stateReads++;
        if (!authenticated) return route.fulfill({ status: 403, json: { error: 'auth_required' } });
        return route.fulfill({ json: {
          telegram: { state: 'up' }, bot: { up: true }, outbox: { pending: 0 },
          claude: stateUnavailable ? null : { next: slots[0].name, slots: singleton ? slots.slice(0, 1) : slots },
          codex: { captured_at: now, windows: [] },
        } });
      }
      if (endpoint.includes('/swap/')) {
        assert.equal(request.method(), 'POST');
        const expected = staleView ? ['claude', long, 'claude-backup'] : slots.map(slot => slot.name);
        assert.deepEqual(request.postDataJSON(), { view: 'usage', expected });
        const [name, other] = endpoint.split('/').slice(-3).filter(part => part !== 'swap');
        calls.push([name, other]);
        if (staleView) return route.fulfill({ status: 409, json: { error: 'slot_order_changed' } });
        if (rejectSwap) return route.fulfill({ status: 500, json: { error: 'slot_move_error' } });
        const requestSession = session;
        await new Promise(resolve => { release = resolve; swapStarted(); });
        if (requestSession === session) {
          const a = slots.findIndex(item => item.name === name), b = slots.findIndex(item => item.name === other);
          [slots[a], slots[b]] = [slots[b], slots[a]];
        }
        return route.fulfill({ json: { ok: true } });
      }
      return route.fulfill({ json: { topics: [], commands: [], items: [], keys: [], files: [] } });
    });

    await page.goto(origin);
    await page.locator('#claudeSlots [data-primary]').click();
    const popover = page.locator('#claudeSlotPopover');
    const row = name => popover.locator(`.slot-row[data-slot="${name}"]`);
    const button = (name, direction) => row(name).locator(`[data-slot-order="${direction}"]`);
    if (lifecycle === 'scroll') {
      const moving = 'claude-slot-8';
      const target = button(moving, 'down');
      await target.evaluate(element => {
        const scroller = element.closest('.slot-list');
        scroller.scrollTop += element.getBoundingClientRect().bottom
          - scroller.getBoundingClientRect().bottom + 2;
        element.focus({ preventScroll: true });
      });
      const started = new Promise(resolve => { swapStarted = resolve; });
      await page.keyboard.press('Enter');
      await started;
      release();
      await page.waitForFunction(name => document.activeElement.matches(
        `.slot-row[data-slot="${name}"] [data-slot-order="down"]:not(:disabled)`), moving);
      const outside = await target.evaluate(element => {
        const button = element.getBoundingClientRect();
        const pane = element.closest('.slot-list').getBoundingClientRect();
        return Math.max(pane.top - button.top, button.bottom - pane.bottom, 0);
      });
      assert.ok(outside <= 0.5, `restored usage slot button is clipped by ${outside}px`);
      return;
    }

    if (lifecycle === 'session') {
      const started = new Promise(resolve => { swapStarted = resolve; });
      await button(long, 'up').click();
      await started;
      authenticated = false;
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await page.locator('#pw').fill('fixture-password');
      await page.locator('#pwForm button[type=submit]').click();
      await page.locator('#otp input').first().waitFor();
      for (let index = 0; index < 6; index++) await page.locator('#otp input').nth(index).fill('1');
      await page.locator('#claudeSlots [data-primary]').click();
      await row(long).waitFor();
      const refresh = popover.locator('.slot-popover-head [data-slot-refresh]');
      const enabledBeforeCompletion = await refresh.isEnabled();
      await popover.focus();
      const reads = stateReads;
      const response = page.waitForResponse(result => result.url().includes('/swap/'));
      release();
      const completed = await response;
      assert.equal(completed.status(), 200);
      await completed.finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.deepEqual({
        enabledBeforeCompletion,
        enabledAfterCompletion: await refresh.isEnabled(),
        focus: await popover.evaluate(panel => panel === document.activeElement),
        stateReads: stateReads - reads,
      }, { enabledBeforeCompletion: true, enabledAfterCompletion: true, focus: true, stateReads: 0 });
      return;
    }

    if (lifecycle === 'unavailable') {
      const started = new Promise(resolve => { swapStarted = resolve; });
      await button(long, 'up').click();
      await started;
      const complete = release;
      const refresh = () => page.evaluate(() =>
        document.dispatchEvent(new Event('claude-slot-order-changed')));
      stateUnavailable = true;
      await refresh();
      await row(long).waitFor({ state: 'detached' });
      stateUnavailable = false;
      await refresh();
      await row(long).waitFor();
      const disabledDuringSwap = await button(long, 'up').isDisabled();
      await button(long, 'up').evaluate(element => element.click());
      complete();
      assert.equal(disabledDuringSwap, true, 'order controls enabled while swap is pending');
      await page.waitForFunction(name => document.activeElement.matches(
        `.slot-row[data-slot="${name}"] [data-slot-order="down"]:not(:disabled)`), long);
      assert.deepEqual(calls, [[long, 'claude']]);
      return;
    }

    if (lifecycle === 'conflict') {
      // The server changed the order without the browser receiving another state snapshot.
      [slots[0], slots[2]] = [slots[2], slots[0]];
      staleView = true;
      const response = page.waitForResponse(result => result.url().includes('/swap/'));
      await button(long, 'up').click();
      assert.equal((await response).status(), 409);
      await page.waitForFunction(() => document.querySelector(
        '#claudeSlotPopover .slot-row[data-slot]')?.dataset.slot === 'claude-backup');
      assert.deepEqual(calls, [[long, 'claude']]);
      const refresh = popover.locator('.slot-popover-head [data-slot-refresh]:enabled');
      await refresh.waitFor();
      assert.equal(await button(long, 'up').isEnabled(), true);
      assert.equal(await refresh.getAttribute('data-failed'), null, 'the order refresh succeeded');
      return;
    }

    await button(long, 'up').waitFor();
    assert.equal(await popover.locator('.slot-order-button').count(), 6);
    assert.equal(await popover.locator('.slot-row').last().locator('.slot-order-button').count(), 0);
    assert.equal(await button('claude', 'up').isDisabled(), true);
    assert.equal(await button('claude-backup', 'down').isDisabled(), true);
    assert.equal(await button(long, 'up').getAttribute('aria-label'),
      config.locales.en['slots.move_up'].replace('{name}', long));

    assert.deepEqual(await popover.evaluate(panel => {
      const issues = [];
      for (const row of panel.querySelectorAll('.slot-row')) {
        const bounds = row.getBoundingClientRect();
        const info = row.querySelector('.slot-name').getBoundingClientRect();
        for (const button of row.querySelectorAll('.slot-order-button')) {
          const rect = button.getBoundingClientRect();
          if (rect.width < 44 || rect.height < 44 || rect.right > bounds.right || info.right > rect.left) {
            issues.push(row.dataset.slot);
          }
        }
      }
      return [panel.scrollWidth > panel.clientWidth, ...issues];
    }), [false]);

    async function move(direction, other, focused) {
      const started = new Promise(resolve => { swapStarted = resolve; });
      await button(long, direction).click();
      await started;
      assert.equal(await button(long, direction).isDisabled(), true);
      assert.equal(await popover.locator('.slot-order-button:not(:disabled)').count(), 0);
      assert.deepEqual(calls.at(-1), [long, other]);
      release();
      await page.waitForFunction(({ long, focused }) => document.activeElement.matches(
        `.slot-row[data-slot="${long}"] [data-slot-order="${focused}"]:not(:disabled)`), { long, focused });
      assert.deepEqual(await popover.locator('.slot-row-top .slot-name').allTextContents(),
        [...slots.map(item => item.name), 'Codex']);
    }
    await move('up', 'claude', 'down');
    await move('down', 'claude', 'down');

    rejectSwap = true;
    await button(long, 'up').click();
    await popover.locator('.slot-popover-head [data-slot-refresh][data-failed]').waitFor();
    assert.equal(await button(long, 'up').isEnabled(), true);
    assert.deepEqual(await popover.locator('.slot-row-top .slot-name').allTextContents(),
      ['claude', long, 'claude-backup', 'Codex']);

    singleton = true;
    await popover.locator('.slot-popover-head [data-slot-refresh]').click();
    await row(long).waitFor({ state: 'detached' });
    assert.equal(await popover.locator('.slot-order-button').count(), 0);
  } finally {
    await page.close();
  }
}

async function checkSlotInteractions(browser) {
  await checkUsageRegressions(browser);
  for (const [width, height, theme] of [
    [1440, 900, 'light'], [1440, 900, 'dark'], [390, 844, 'light'],
    [844, 390, 'light'], [1280, 900, 'light'], [768, 900, 'light'], [375, 844, 'light'],
  ]) await check(browser, width, height, theme);
}

try {
  const browser = await chromium.launch({ headless: true });
  try {
    const cooldownTransitions = [
      { language: 'de', count: 4 },
      { language: 'de', count: 2 },
      { language: 'fr', count: 4 },
    ];
    const [longestLocale, , , ...cooldownFailures] = await Promise.all([
      checkHeaderMatrix(browser),
      checkSlotInteractions(browser),
      Promise.all([
        ...[1280, 375].map(width => checkUsageSlotOrder(browser, width)),
        checkUsageSlotOrder(browser, 375, 'scroll'),
        checkUsageSlotOrder(browser, 1280, 'session'),
        checkUsageSlotOrder(browser, 1280, 'unavailable'),
        checkUsageSlotOrder(browser, 1280, 'conflict'),
      ]),
      ...cooldownTransitions.map(scenario => checkHeaderMatrix(browser, scenario)),
    ]);
    assert.deepEqual(cooldownFailures.flat(), []);
    await checkUsageDesign(browser, longestLocale);
  } finally {
    await browser.close();
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
