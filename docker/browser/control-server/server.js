/**
 * Browser control server for ductor.
 *
 * Provides an HTTP API for headless Chromium interaction using Playwright.
 * Accessibility-tree based element referencing (ref numbers) instead of CSS
 * selectors — resilient to DOM changes.
 *
 * Endpoints:
 *   GET  /health           — status check
 *   POST /navigate          — navigate to URL
 *   POST /snapshot          — get accessibility tree with ref numbers
 *   POST /act               — interact with element by ref
 *   POST /screenshot        — capture screenshot
 *   POST /evaluate          — run JS expression
 */

const express = require("express");
const { chromium } = require("playwright");

const PORT = parseInt(process.env.PORT || "18800", 10);
const BROWSER_TOKEN = process.env.BROWSER_TOKEN || "";
if (!BROWSER_TOKEN) {
  console.error("BROWSER_TOKEN is required");
  process.exit(1);
}

const app = express();
app.use(express.json({ limit: "1mb" }));

// Require the browser token on all non-health routes.
app.use((req, res, next) => {
  if (req.path === "/health") return next();
  if (req.headers["x-browser-token"] !== BROWSER_TOKEN) {
    return res.status(401).json({ error: "unauthorized" });
  }
  next();
});

/** @type {import('playwright').Browser | null} */
let browser = null;
/** @type {import('playwright').Page | null} */
let page = null;
/** @type {import('playwright').Page | null} */
let insecurePage = null;
/** @type {import('playwright').Page | null} */
let activePage = null;
let activeInsecure = false;
let browserPromise = null;
let lastError = null;
/** @type {Map<number, {role: string, name: string, selector: string}>} */
let refMap = new Map();
let nextRef = 1;

// ── Lifecycle ────────────────────────────────────────────────────────

async function ensureBrowser(insecure = null) {
  if (browserPromise) {
    await browserPromise;
    return ensureBrowser(insecure);
  }
  browserPromise = (async () => {
    if (!browser || !browser.isConnected()) {
      const launched = await chromium.launch({
        headless: true,
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-dev-shm-usage",
          "--disable-gpu",
        ],
      });
      browser = launched;
      page = null;
      insecurePage = null;
      activePage = null;
      activeInsecure = false;
      launched.on("disconnected", () => {
        if (browser !== launched) return;
        browser = null;
        page = null;
        insecurePage = null;
        activePage = null;
        activeInsecure = false;
      });
    }
    if (page && page.isClosed()) {
      if (activePage === page) {
        activePage = null;
        activeInsecure = false;
      }
      page = null;
    }
    if (insecurePage && insecurePage.isClosed()) {
      if (activePage === insecurePage) {
        activePage = null;
        activeInsecure = false;
      }
      insecurePage = null;
    }
    if (!page) {
      const context = await browser.newContext({
        viewport: { width: 1280, height: 720 },
      });
      const created = await context.newPage();
      page = created;
      created.on("crash", () => {
        if (page === created) page = null;
        if (activePage === created) {
          activePage = null;
          activeInsecure = false;
        }
        context.close().catch(() => {});
      });
    }
    if (insecure === true && !insecurePage) {
      const context = await browser.newContext({
        viewport: { width: 1280, height: 720 },
        ignoreHTTPSErrors: true,
      });
      const created = await context.newPage();
      insecurePage = created;
      created.on("crash", () => {
        if (insecurePage === created) insecurePage = null;
        if (activePage === created) {
          activePage = null;
          activeInsecure = false;
        }
        context.close().catch(() => {});
      });
    }
    return insecure === true ? insecurePage : insecure === false ? page : activePage || page;
  })();
  try {
    const ready = await browserPromise;
    lastError = null;
    return ready;
  } catch (err) {
    lastError = err;
    throw err;
  } finally {
    browserPromise = null;
  }
}

// ── Routes ───────────────────────────────────────────────────────────

app.get("/health", (_req, res) => {
  if (lastError && (!browser || !browser.isConnected())) {
    return res.status(503).json({ status: "error" });
  }
  res.json({ status: "ok", hasPage: !!page, insecure: activeInsecure });
});

app.post("/navigate", async (req, res) => {
  try {
    const { url, insecure = false } = req.body;
    if (!url) return res.status(400).json({ error: "url required", insecure: activeInsecure });
    const p = await ensureBrowser(insecure === true);
    await p.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    activePage = p;
    activeInsecure = insecure === true;
    refMap.clear();
    nextRef = 1;
    res.json({ ok: true, url: p.url(), title: await p.title(), insecure: activeInsecure });
  } catch (err) {
    res.status(500).json({ error: err.message, insecure: activeInsecure });
  }
});

app.post("/snapshot", async (req, res) => {
  try {
    const p = await ensureBrowser();
    refMap.clear();
    nextRef = 1;

    // Build accessibility tree with ref numbers
    const tree = await p.evaluate(() => {
      const walk = (el) => {
        const items = [];
        const role = el.getAttribute("role") || el.tagName.toLowerCase();
        const name =
          el.getAttribute("aria-label") ||
          el.getAttribute("alt") ||
          el.getAttribute("title") ||
          el.textContent?.trim().slice(0, 80) ||
          "";

        // Only include interactive/meaningful elements
        const interestingTags = new Set([
          "a", "button", "input", "select", "textarea", "details",
          "summary", "label", "h1", "h2", "h3", "h4", "h5", "h6",
          "img", "video", "audio", "iframe", "form",
        ]);
        const interestingRoles = new Set([
          "button", "link", "textbox", "checkbox", "radio",
          "combobox", "listbox", "option", "tab", "tabpanel",
          "menuitem", "heading", "img", "navigation", "main",
          "dialog", "alert", "banner", "search",
        ]);

        const tag = el.tagName.toLowerCase();
        const isInteresting =
          interestingTags.has(tag) ||
          interestingRoles.has(role) ||
          el.getAttribute("role") ||
          el.onclick ||
          el.getAttribute("tabindex");

        if (isInteresting && name) {
          items.push({ tag, role, name, path: getSelector(el) });
        }

        for (const child of el.children) {
          items.push(...walk(child));
        }
        return items;
      };

      const getSelector = (el) => {
        if (el.id) return `#${el.id}`;
        const parts = [];
        let node = el;
        while (node && node !== document.body) {
          let selector = node.tagName.toLowerCase();
          if (node.id) {
            parts.unshift(`#${node.id}`);
            break;
          }
          const parent = node.parentElement;
          if (parent) {
            const siblings = Array.from(parent.children).filter(
              (c) => c.tagName === node.tagName
            );
            if (siblings.length > 1) {
              selector += `:nth-of-type(${siblings.indexOf(node) + 1})`;
            }
          }
          parts.unshift(selector);
          node = node.parentElement;
        }
        return parts.join(" > ");
      };

      return walk(document.body);
    });

    // Assign ref numbers
    const snapshot = tree.map((item) => {
      const ref = nextRef++;
      refMap.set(ref, {
        role: item.role,
        name: item.name,
        selector: item.path,
      });
      return { ref, role: item.role, name: item.name };
    });

    res.json({ elements: snapshot, count: snapshot.length, url: p.url(), insecure: activeInsecure });
  } catch (err) {
    res.status(500).json({ error: err.message, insecure: activeInsecure });
  }
});

app.post("/act", async (req, res) => {
  try {
    const p = await ensureBrowser();
    const { action, ref, value } = req.body;

    if (!action || ref == null)
      return res.status(400).json({ error: "action and ref required", insecure: activeInsecure });

    const target = refMap.get(ref);
    if (!target)
      return res.status(404).json({ error: `ref ${ref} not found — run /snapshot first`, insecure: activeInsecure });

    const el = await p.locator(target.selector).first();

    switch (action) {
      case "click":
        await el.click({ timeout: 5000 });
        break;
      case "type":
        if (!value) return res.status(400).json({ error: "value required for type", insecure: activeInsecure });
        await el.fill(value, { timeout: 5000 });
        break;
      case "hover":
        await el.hover({ timeout: 5000 });
        break;
      case "scroll":
        await el.scrollIntoViewIfNeeded({ timeout: 5000 });
        break;
      case "select":
        if (!value) return res.status(400).json({ error: "value required for select", insecure: activeInsecure });
        await el.selectOption(value, { timeout: 5000 });
        break;
      default:
        return res.status(400).json({ error: `unknown action: ${action}`, insecure: activeInsecure });
    }

    // Brief pause for page to settle
    await p.waitForTimeout(500);
    res.json({ ok: true, action, ref, target: target.name, insecure: activeInsecure });
  } catch (err) {
    res.status(500).json({ error: err.message, insecure: activeInsecure });
  }
});

app.post("/screenshot", async (req, res) => {
  try {
    const p = await ensureBrowser();
    const { fullPage } = req.body;
    const buffer = await p.screenshot({
      fullPage: !!fullPage,
      type: "png",
    });
    res.set("Content-Type", "image/png");
    res.send(buffer);
  } catch (err) {
    res.status(500).json({ error: err.message, insecure: activeInsecure });
  }
});

app.post("/evaluate", async (req, res) => {
  const { expression, insecure } = req.body;
  try {
    const p = await ensureBrowser(insecure);
    if (!expression)
      return res.status(400).json({ error: "expression required", insecure: typeof insecure === "boolean" ? insecure : activeInsecure });
    const result = await p.evaluate(expression);
    res.json({ result, insecure: typeof insecure === "boolean" ? insecure : activeInsecure });
  } catch (err) {
    res.status(500).json({ error: err.message, insecure: typeof insecure === "boolean" ? insecure : activeInsecure });
  }
});

// ── Start ────────────────────────────────────────────────────────────

if (require.main === module) {
  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Browser control server listening on :${PORT}`);
  });
}

module.exports = { app, ensureBrowser };

// Graceful shutdown
process.on("SIGTERM", async () => {
  console.log("Shutting down...");
  if (browser) await browser.close();
  process.exit(0);
});
