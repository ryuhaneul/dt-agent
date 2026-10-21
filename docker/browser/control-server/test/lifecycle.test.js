const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const http = require("node:http");

function fixture() {
  const browsers = [];
  let launchCount = 0;
  let fail = false;
  class Page extends EventEmitter {
    constructor(context) { super(); this.context = context; this.closed = false; }
    isClosed() { return this.closed; }
    async goto() { assert.equal(this.closed, false); }
    url() { return "about:blank"; }
    async title() { return "Synthetic"; }
  }
  class Context {
    constructor() { this.closed = false; }
    async newPage() { this.page = new Page(this); return this.page; }
    async close() { this.closed = true; this.page.closed = true; }
  }
  class Browser extends EventEmitter {
    constructor() { super(); this.connected = true; this.contexts = []; }
    isConnected() { return this.connected; }
    async newContext() { const c = new Context(); this.contexts.push(c); return c; }
    async close() { this.connected = false; this.emit("disconnected"); }
  }
  require.cache[require.resolve("playwright")] = {
    id: require.resolve("playwright"), filename: require.resolve("playwright"), loaded: true,
    exports: { chromium: { launch: async () => {
      launchCount++;
      await new Promise(resolve => setImmediate(resolve));
      if (fail) throw new Error("synthetic launch failure");
      const b = new Browser(); browsers.push(b); return b;
    } } },
  };
  delete require.cache[require.resolve("../server")];
  const server = require("../server");
  return { ...server, browsers, launches: () => launchCount, fail: value => { fail = value; } };
}

// An old server must fail immediately instead of leaving its auto-listener alive.
const initial = fixture();
assert.equal(typeof initial.ensureBrowser, "function");
assert.equal(typeof initial.app, "function");

async function request(f, path, body) {
  const server = http.createServer(f.app);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    return await new Promise((resolve, reject) => {
      const req = http.request({
        hostname: "127.0.0.1", port: server.address().port, path,
        method: body ? "POST" : "GET",
        headers: { "x-browser-token": "t", "content-type": "application/json" },
      }, res => {
        let data = "";
        res.on("data", chunk => { data += chunk; });
        res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(data) }));
      });
      req.on("error", reject);
      req.end(body ? JSON.stringify(body) : undefined);
    });
  } finally { await new Promise(resolve => server.close(resolve)); }
}

test("concurrent first requests share launch and page initialization", async () => {
  const f = fixture();
  const [a, b] = await Promise.all([f.ensureBrowser(false), f.ensureBrowser(false)]);
  assert.equal(f.launches(), 1);
  assert.equal(a, b);
});

test("an old browser disconnect cannot discard its replacement", async () => {
  const f = fixture();
  await f.ensureBrowser(false);
  const old = f.browsers[0];
  old.connected = false;
  old.emit("disconnected");
  const current = await f.ensureBrowser(false);
  old.emit("disconnected");
  assert.equal(await f.ensureBrowser(false), current);
  assert.equal(f.launches(), 2);
});

test("health stays lazy and reflects launch failure and recovery", async () => {
  const f = fixture();
  assert.equal((await request(f, "/health")).status, 200);
  assert.equal(f.launches(), 0);
  f.fail(true);
  await assert.rejects(f.ensureBrowser(false), /synthetic launch failure/);
  const failed = await request(f, "/health");
  assert.equal(failed.status, 503);
  assert.equal(failed.body.status, "error");
  f.fail(false);
  await f.ensureBrowser(false);
  const recovered = await request(f, "/health");
  assert.equal(recovered.status, 200);
  assert.equal(recovered.body.status, "ok");
});

for (const insecure of [false, true]) {
  test(`page crash discards its context and recovers (insecure=${insecure})`, async () => {
    const f = fixture();
    assert.equal((await request(f, "/navigate", { url: "about:blank", insecure })).status, 200);
    const crashed = await f.ensureBrowser(insecure);
    crashed.emit("crash");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(crashed.context.closed, true);
    assert.equal((await request(f, "/navigate", { url: "about:blank", insecure })).status, 200);
    assert.notEqual(await f.ensureBrowser(insecure), crashed);
    assert.equal(f.launches(), 1);
  });
  test(`closed page is recreated (insecure=${insecure})`, async () => {
    const f = fixture();
    const old = await f.ensureBrowser(insecure);
    old.closed = true;
    const replacement = await f.ensureBrowser(insecure);
    assert.notEqual(replacement, old);
    assert.equal(replacement.isClosed(), false);
  });
}
