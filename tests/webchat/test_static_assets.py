# ruff: noqa: INP001

"""Static and non-browser checks for the web shell and authentication client."""

from __future__ import annotations

import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ductor_bot.i18n.loader import _load_toml
from ductor_bot.webchat.web.app import CSP, STARFLOW_CSP, STATE
from tests.webchat import test_web_app as fake_contract

site = fake_contract.site

STATIC = Path(__file__).resolve().parents[2] / "ductor_bot/webchat/web/static"
STRINGS = _load_toml(STATIC.parents[2] / "i18n/en/web.toml")


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.inline_scripts = []
        self._inline = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script":
            self._inline = "src" not in attrs

    def handle_endtag(self, tag):
        if tag == "script":
            self._inline = False

    def handle_data(self, data):
        if self._inline and data.strip():
            self.inline_scripts.append(data)


@pytest.fixture
def markup():
    return Markup((STATIC / "index.html").read_text())


def test_csp_markup_has_no_inline_execution(markup):
    assert not markup.inline_scripts
    for tag, attrs in markup.tags:
        assert tag != "style"
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs)
        for name in ("src", "href"):
            assert not attrs.get(name, "").lower().startswith("javascript:")
    scripts = [attrs for tag, attrs in markup.tags if tag == "script"]
    assert scripts[0]["src"] == "/static/js/theme-init.js"
    assert "async" not in scripts[0]
    assert "defer" not in scripts[0]
    assert any(attrs.get("type") == "module" for attrs in scripts)


def test_i18n_keys_and_placeholders_exist(markup):
    korean = _load_toml(STATIC.parents[2] / "i18n/ko/web.toml")
    assert STRINGS.keys() == korean.keys()
    for key, value in STRINGS.items():
        assert set(re.findall(r"\{\w+\}", value)) == set(re.findall(r"\{\w+\}", korean[key]))
    for _tag, attrs in markup.tags:
        if "data-i18n" in attrs:
            assert attrs["data-i18n"] in STRINGS
            placeholders = set(re.findall(r"\{(\w+)\}", STRINGS[attrs["data-i18n"]]))
            assert placeholders <= json.loads(attrs.get("data-i18n-args", "{}")).keys()
        for mapping in attrs.get("data-i18n-attr", "").split(";"):
            if mapping:
                assert mapping.split(":", 1)[1] in STRINGS


def test_all_local_assets_and_module_imports_resolve(markup):
    for _tag, attrs in markup.tags:
        for name in ("src", "href", "data-src"):
            value = attrs.get(name, "")
            if value.startswith("/static/"):
                assert (STATIC / value.removeprefix("/static/")).is_file(), value
    for path in (STATIC / "css").glob("*.css"):
        for url in re.findall(r"url\([\"']?([^\"')]+)", path.read_text()):
            assert (path.parent / url).resolve().is_file(), url
    for path in (STATIC / "js").glob("*.js"):
        for imported in re.findall(r"from ['\"]([^'\"]+)['\"]", path.read_text()):
            assert (path.parent / imported).is_file(), imported
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_shell_contains_no_sample_records_or_credentials(markup):
    text = (STATIC / "index.html").read_text()
    assert "password12" not in text
    assert "20:41" not in text
    assert "2026-11-10" not in text
    assert "streamMsg" not in text
    fields = {attrs.get("id"): attrs for tag, attrs in markup.tags if tag == "input"}
    assert "value" not in fields["pw"]
    assert fields["pw"]["autocomplete"] == "current-password"
    assert {attrs["data-auth"] for _, attrs in markup.tags if "data-auth" in attrs} == {
        "pw",
        "totp",
        "setup",
    }


def test_dirty_indicator_uses_presence_and_stable_file_names(markup):
    files = [attrs for _, attrs in markup.tags if attrs.get("class") == "fitem"]
    assert {attrs["data-file"] for attrs in files} == {
        "PERSONA.md",
        "BEHAVIOR.md",
        "MAINMEMORY.md",
        "SHAREDMEMORY.md",
    }
    css = (STATIC / "css/app.css").read_text()
    assert '.fitem[data-dirty="1"]' not in css
    assert re.search(r"\.fitem\[data-dirty\] \.pip\{display:block\}", css)
    # Both an empty boolean attribute and a previous nonempty value match this selector.
    for value in ["", "1", "PERSONA.md"]:
        item = Markup(f'<button class="fitem" data-file="PERSONA.md" data-dirty="{value}">')
        assert "data-dirty" in item.tags[0][1]


def _rgb(color):
    return tuple(int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))


def _contrast(first, second):
    def luminance(rgb):
        return sum(
            weight * (value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
            for value, weight in zip(rgb, (0.2126, 0.7152, 0.0722), strict=True)
        )

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_palette_contrast_including_white_star_bound():
    css = (STATIC / "css/app.css").read_text()
    light = dict(
        re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6})", re.search(r":root\{([^}]+)\}", css)[1])
    )
    dark = dict(
        re.findall(
            r"--([\w-]+):(#[0-9a-fA-F]{6})", re.search(r'\[data-theme="dark"\]\{([^}]+)\}', css)[1]
        )
    )
    for palette in [light, dark]:
        for foreground in ["fg", "muted", "accent"]:
            assert _contrast(_rgb(palette[foreground]), _rgb(palette["bg"])) >= 4.5
        assert _contrast(_rgb(palette["field-rule"]), _rgb(palette["field"])) >= 3
    worst_veil = tuple(channel * 0.92 + 0.08 for channel in _rgb(dark["bg"]))
    assert "--veil:color-mix(in srgb,var(--bg) 92%,transparent)" in css
    for foreground in ["fg", "muted", "accent"]:
        assert _contrast(_rgb(dark[foreground]), worst_veil) >= 4.5
    assert _contrast(_rgb(dark["field-rule"]), worst_veil) >= 3
    assert _contrast(_rgb(dark["field-rule"]), _rgb(dark["field"])) < 6
    assert "@media (max-width:1180px)" in css
    assert "@media (max-width:760px)" in css


def test_licenses_and_starflow_exception_are_bundled():
    for path in ["fonts/LICENSE-Pretendard.txt", "fonts/LICENSE-JetBrainsMono.txt"]:
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / path).read_text()
    assert "Permission is hereby granted" in (STATIC / "starflow/LICENSE-Three.js.txt").read_text()
    sky = (STATIC / "starflow/base-stars.html").read_text()
    assert '<html lang="en">' in sky
    assert "<title>Starflow background</title>" in sky
    assert "Three.js 0.180.0" in sky


async def test_real_static_responses_with_fake_bridge(site):
    client, fake = site
    client.app[STATE].static_dir = STATIC
    response = await client.get("/")
    assert response.status == 200
    assert response.headers["Content-Security-Policy"] == CSP
    assert 'id="pwForm"' in await response.text()
    for path in [
        "css/app.css",
        "js/auth.js",
        "js/ws.js",
        "js/topics.js",
        "js/chat.js",
        "js/md.js",
        "js/manage.js",
        "js/files.js",
        "js/diff.js",
        "fonts/JetBrainsMono-Latin.woff2",
        "starflow/LICENSE-Three.js.txt",
    ]:
        response = await client.get("/static/" + path)
        assert response.status == 200
        assert response.headers["Content-Security-Policy"] == CSP
    response = await client.get("/static/starflow/base-stars.html")
    assert response.status == 200
    assert response.headers["Content-Security-Policy"] == STARFLOW_CSP
    fake.language = "ko"
    response = await client.get("/api/i18n")
    data = await response.json()
    assert data["lang"] == "ko"
    assert data["strings"]["auth.qr"]


CLIENT_TESTS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const {staticDir, strings} = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(id = '') {
    this.id = id; this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.value = ''; this.textContent = ''; this.hidden = false;
    this.disabled = false; this.open = false;
    this.classes = new Set();
    this.classList = {toggle:(name,force) => {
      const present=force ?? !this.classes.has(name);
      if(present)this.classes.add(name);else this.classes.delete(name);
    },contains:name=>this.classes.has(name)};
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async emit(name, details = {}) {
    const event = { preventDefault() {}, target: this, ...details };
    for (const callback of this.listeners[name] || []) await callback(event);
    await new Promise(setImmediate);
  }
  appendChild(child) {
    const previous = this.children.at(-1);
    if (previous) { previous.nextElementSibling = child; child.previousElementSibling = previous; }
    this.children.push(child); return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  querySelector(selector) { return selector === 'span' ? this.label : null; }
  querySelectorAll(selector) { return selector === 'input' ? this.children : []; }
  focus() { document.activeElement = this; }
  remove() { this.removed = true; }
}
const ids = Object.fromEntries(['auth','app','pw','pwForm','totpForm','setupForm','otp','otp2',
  'seedIn','qr','setupLink','botBadge','toasts','regenerate','setupNew','setupPaste'].map(id => [id, new Element(id)]));
const seed = new Element('seed');
const modeButtons = ['new','paste'].map(mode => {
  const element = new Element(); element.dataset.setupMode = mode; return element;
});
const buttons = [...modeButtons, ids.regenerate];
ids.botBadge.label = new Element();
const document = new Element(); document.documentElement = new Element(); document.body = new Element();
document.querySelector = selector => {
  if (selector.startsWith('#')) return ids[selector.slice(1)];
  if (selector === '.seed') return seed;
  if (selector.startsWith('[data-setup-mode=')) return modeButtons.find(button => selector.includes(button.dataset.setupMode));
  if (selector.startsWith('[data-i18n=')) return { textContent: '' };
  return null;
};
document.querySelectorAll = selector => selector === '[data-setup-mode]' ? modeButtons
  : selector === '.otp input' ? [...ids.otp.children, ...ids.otp2.children]
  : selector === '#auth button' ? buttons : [];
document.createElement = () => new Element();
document.importNode = element => element;
document.dispatchEvent = event => (document.listeners[event.type] || []).forEach(callback => callback(event));
class CustomEvent { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }
let configured = true, authenticated = false;
const flags = { preauth: false, authenticated: false, routes: [] };
const calls = [];
function response(status, data) { return { ok: status < 400, status, json: async () => data }; }
async function fetch(url, options = {}) {
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({url, ...options, body});
  if (url === '/api/i18n') return response(200, {lang:'en', strings});
  if (url === '/api/auth/state') return response(200, {
    authenticated, totp_configured: configured, csrf: authenticated ? 'fake-csrf' : null, bridge_up:true
  });
  assert.equal(options.headers['X-Dtw-Client'], 'web');
  assert.equal(options.credentials, 'same-origin');
  if (url === '/api/auth/password') {
    if (body.password !== 'good-password') return response(403, {error:'invalid'});
    return response(200, {next: configured ? 'totp' : 'setup'});
  }
  if (url === '/api/auth/setup/begin') return response(200, {
    secret_display:'JBSW Y3DP EHPK 3PXP', qr_svg:'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
  });
  if (url === '/api/auth/setup/confirm') {
    assert.equal(body.code, '123456'); configured = true;
    return response(200, {ok:true});
  }
  if (url === '/api/auth/totp') {
    if (body.code !== '123456') return response(403, {error:'invalid'});
    authenticated = true; return response(200, {ok:true});
  }
  if (url === '/api/topics') {
    assert.equal(options.headers['X-Dtw-Csrf'], 'fake-csrf');
    return response(200, {topics:[]});
  }
  throw new Error('Unexpected API path: ' + url);
}
const context = vm.createContext({document, fetch, CustomEvent, console, setTimeout:() => 0,
  DOMParser: class { parseFromString(text) {
    assert.ok(text.startsWith('<svg'));
    return { documentElement:{ localName:'svg', namespaceURI:'http://www.w3.org/2000/svg' } };
  }}
});
const cache = new Map();
const router = new vm.SyntheticModule(['go','setAuthenticated','setPreauth'], function() {
  this.setExport('go', route => { flags.routes.push(route); document.dispatchEvent(new CustomEvent('route-change', {detail:{route}})); });
  this.setExport('setAuthenticated', value => { flags.authenticated = value; });
  this.setExport('setPreauth', value => { flags.preauth = value; });
}, {context});
await router.link(() => {}); await router.evaluate();
async function load(filename) {
  if (filename.endsWith('/router.js')) return router;
  if (cache.has(filename)) return cache.get(filename);
  const module = new vm.SourceTextModule(fs.readFileSync(filename,'utf8'), {context, identifier:filename});
  cache.set(filename, module);
  await module.link(specifier => load(path.resolve(path.dirname(filename),specifier)));
  return module;
}
const auth = await load(path.join(staticDir,'js/auth.js')); await auth.evaluate();
const translations = cache.get(path.join(staticDir,'js/i18n.js'));
await translations.namespace.loadI18n();
assert.equal(translations.namespace.t('auth.locked', {minutes:3}), strings['auth.locked'].replace('{minutes}','3'));
await auth.namespace.initAuth();
assert.equal(ids.otp.children.length, 6);
assert.equal(ids.otp.children[0].autocomplete, 'one-time-code');
assert.equal(ids.otp.children[0].attributes['aria-label'], strings['auth.digit'].replace('{n}','1'));
ids.pw.value = 'wrong'; await ids.pwForm.emit('submit');
assert.equal(flags.routes.at(-1), 'pw');
assert.equal(ids.toasts.children.at(-1).children[0].textContent, strings['auth.invalid']);
ids.pw.value = 'good-password'; await ids.pwForm.emit('submit');
assert.equal(flags.routes.at(-1), 'totp'); assert.equal(ids.pw.value,'');
ids.otp.children[0].value=''; await ids.otp.children[1].emit('keydown',{key:'Backspace'});
assert.equal(document.activeElement, ids.otp.children[0]);
await ids.otp.children[0].emit('paste',{clipboardData:{getData:()=>'12 34-56'}});
assert.equal(flags.routes.at(-1),'chat'); assert.equal(flags.authenticated,true);
const api = cache.get(path.join(staticDir,'js/api.js'));
await api.namespace.api('/topics',{method:'POST',body:{name:'new'}});
assert.equal(ids.otp.children.map(input=>input.value).join(''),'');
// Initial setup: QR comes from the server, confirmation returns to password entry.
authenticated=false; configured=false; api.namespace.setCsrf(null);
await auth.namespace.initAuth();
ids.pw.value='good-password'; await ids.pwForm.emit('submit');
assert.equal(flags.routes.at(-1),'setup'); assert.equal(ids.qr.children.length,1);
assert.equal(seed.textContent,'JBSW Y3DP EHPK 3PXP');
await modeButtons[1].emit('click'); ids.seedIn.value='JBSWY3DPEHPK3PXP';
await ids.otp2.children[0].emit('paste',{clipboardData:{getData:()=>'123456'}});
assert.equal(flags.routes.at(-1),'pw');
assert.ok(calls.some(call=>call.url==='/api/auth/setup/begin' && call.body.mode==='paste'));
assert.equal(seed.textContent,''); assert.equal(ids.seedIn.value,'');
// Theme initialization is synchronous and tolerates blocked local storage.
const themeSource=fs.readFileSync(path.join(staticDir,'js/theme-init.js'),'utf8');
for (const [saved,systemDark,wanted] of [['dark',false,'dark'],['light',true,'light'],[null,true,'dark'],['invalid',false,'light']]) {
  const root={dataset:{}};
  vm.runInNewContext(themeSource,{document:{documentElement:root},localStorage:{getItem:()=>saved},matchMedia:()=>({matches:systemDark})});
  assert.equal(root.dataset.theme,wanted);
  assert.equal(root.dataset.themePref,['dark','light'].includes(saved)?saved:'system');
}
const root={dataset:{}};
vm.runInNewContext(themeSource,{document:{documentElement:root},localStorage:{getItem(){throw new Error('blocked')}},matchMedia:()=>({matches:true})});
assert.equal(root.dataset.theme,'dark');
// Exercise the actual theme module, including live system preference changes.
const storage=new Map();
context.localStorage={setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
let mediaChanged;
const media={matches:true,addEventListener:(_event,callback)=>{mediaChanged=callback}};
context.matchMedia=()=>media;
const sky=ids['starflow-background']=new Element();sky.dataset.src='/static/starflow/base-stars.html';
Object.defineProperty(sky,'src',{set:value=>{sky.attributes.src=value}});
document.documentElement.dataset.themePref='system';
const theme=await load(path.join(staticDir,'js/theme.js'));await theme.evaluate();
theme.namespace.initTheme();
assert.equal(document.documentElement.dataset.theme,'dark');assert.ok(sky.hasAttribute('src'));
theme.namespace.setPreference('light');assert.equal(storage.get('dtw-theme'),'light');assert.ok(!sky.hasAttribute('src'));
theme.namespace.setPreference('system');assert.ok(!storage.has('dtw-theme'));assert.ok(sky.hasAttribute('src'));
media.matches=false;mediaChanged();assert.equal(document.documentElement.dataset.theme,'light');assert.ok(!sky.hasAttribute('src'));
// Route guards cannot expose the application or the second factor through a direct hash.
const query=document.querySelector;
const headings={};
const views=['chat','manage','files'].map(view=>{const element=new Element();element.dataset.view=view;return element});
['pw','totp','setup'].forEach(name=>{ids[name+'Form'].dataset.auth=name});
ids.connPanel=new Element();ids.info=new Element();
const dialogs=['delDlg','diffDlg'].map(id=>{
 const dialog=new Element(id);dialog.showModal=()=>{dialog.open=true};dialog.close=()=>{dialog.open=false};return dialog;
});
document.querySelector=selector=>selector.endsWith(' h2')?(headings[selector] ||= new Element(selector)):query(selector);
const queryAll=document.querySelectorAll;
document.querySelectorAll=selector=>selector==='[data-auth]'?[ids.pwForm,ids.totpForm,ids.setupForm]
 :selector==='[data-view]'?views:selector==='dialog'?dialogs:queryAll(selector);
context.location={hash:'#chat'};context.history={replaceState:(_state,_title,url)=>{context.location.hash=url}};
const emptyTheme=new vm.SyntheticModule(['closeMenus'],function(){this.setExport('closeMenus',()=>{})},{context});
await emptyTheme.link(()=>{});await emptyTheme.evaluate();
const realRouter=new vm.SourceTextModule(fs.readFileSync(path.join(staticDir,'js/router.js'),'utf8'),{context});
await realRouter.link(()=>emptyTheme);await realRouter.evaluate();
realRouter.namespace.renderRoute();assert.equal(context.location.hash,'#pw');assert.ok(ids.app.classList.contains('hidden'));
context.location.hash='#setup';realRouter.namespace.renderRoute();assert.equal(context.location.hash,'#pw');
realRouter.namespace.setPreauth(true);context.location.hash='#totp';realRouter.namespace.renderRoute();assert.equal(context.location.hash,'#totp');
realRouter.namespace.setAuthenticated(true);context.location.hash='#chat';realRouter.namespace.renderRoute();assert.ok(!ids.app.classList.contains('hidden'));
const opener=new Element();document.activeElement=opener;context.location.hash='#delete';realRouter.namespace.renderRoute();assert.equal(dialogs[0].open,true);
context.location.hash='#manage';realRouter.namespace.renderRoute();assert.equal(dialogs[0].open,false);assert.equal(document.activeElement,opener);
console.log('Client contracts passed: authentication, setup, OTP, CSRF, i18n, theme changes and route guards');
"""


def test_client_contracts_without_a_browser():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", CLIENT_TESTS],
        input=json.dumps({"staticDir": str(STATIC), "strings": STRINGS}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


BROWSER_TESTS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
const {chromium} = await import(process.env.DTW_PLAYWRIGHT_MODULE);
const {origin, width, artifactDir, strings} = JSON.parse(fs.readFileSync(0,'utf8'));
const browser = await chromium.launch({headless:true});
try {
  const context = await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
  const page = await context.newPage();
  let configured = true, authenticated = false, newQrCount = 0, wsPeer=null;
  const topic={tid:'1.2',chat_id:1,topic_id:2,name:'Example topic',group:'telegram',last_ts:null,preview:'',count:205,imported_count:0,created_at:null,busy:false,renamable:true,deletable:true,read_only:false};
  const archive={...topic,tid:'1.3',topic_id:3,name:'Archive',group:'imported',count:1,imported_count:1};
  let archiveDeleted=false;
  const fileData=Object.fromEntries(['PERSONA.md','BEHAVIOR.md','MAINMEMORY.md','SHAREDMEMORY.md'].map(name=>[name,{name,content:`# ${name}\n\nOriginal`,sha256:`base-${name}`,shared_block:name==='MAINMEMORY.md'}]));
  const entries=Array.from({length:205},(_,index)=>({id:`e${index+1}`,seq:index+1,ts:null,role:'bot',source:'telegram',kind:'message',text:`Message ${index+1}`,imported:false,mirror:null}));
  await page.routeWebSocket('**/api/ws',socket=>{wsPeer=socket;socket.send(JSON.stringify({type:'bridge',up:true}));});
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const body = request.postDataJSON();
    let status=200, result={ok:true};
    if (request.method()==='POST') assert.equal(request.headers()['x-dtw-client'],'web');
    if (url.pathname==='/api/i18n') result={lang:'en',strings};
    else if (url.pathname==='/api/auth/state') result={authenticated,totp_configured:configured,csrf:authenticated?'fake-csrf':null,bridge_up:true};
    else if (url.pathname==='/api/auth/password') {
      if (body.password!=='good-password') {status=403;result={error:'invalid'};}
      else result={next:configured?'totp':'setup'};
    } else if (url.pathname==='/api/auth/setup/begin') {
      if(body.mode==='new')newQrCount++;
      result={secret_display:'JBSW Y3DP EHPK 3PXP',...(body.mode==='new'?{qr_svg:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M0 0h20v20H0z"/></svg>'}:{})};
    } else if (url.pathname==='/api/auth/setup/confirm') {
      assert.equal(body.code,'123456');configured=true;
    } else if (url.pathname==='/api/auth/totp') {
      assert.equal(body.code,'123456');authenticated=true;
    } else if(url.pathname==='/api/state') result={telegram:{state:'down',down_since:null,last_check:null,interval_s:30,strip:Array(30).fill('none'),strip_end:null},outbox:{pending:1},bot:{up:true},web_sessions:1};
    else if(url.pathname==='/api/topics') result={topics:archiveDeleted?[topic]:[topic,archive]};
    else if(url.pathname==='/api/topics/1.2'&&request.method()==='PATCH') {assert.equal(request.headers()['x-dtw-csrf'],'fake-csrf');topic.name=body.name;result={topic};}
    else if(url.pathname==='/api/topics/1.3'&&request.method()==='DELETE') {assert.equal(body.confirm_name,archive.name);archiveDeleted=true;result={telegram_deleted:true,session_removed:true,history_entries:1,outbox_dropped:0};}
    else if(url.pathname==='/api/files') result={files:Object.values(fileData).map(file=>({name:file.name,sha256:file.sha256,size:Buffer.byteLength(file.content),mtime:null}))};
    else if(url.pathname.startsWith('/api/files/')) {
      const file=fileData[decodeURIComponent(url.pathname.split('/').at(-1))];assert.ok(file);
      if(request.method()==='PUT') {
        assert.equal(request.headers()['x-dtw-csrf'],'fake-csrf');
        if(body.base_sha256!==file.sha256){status=409;result={error:'conflict'};}
        else {file.content=body.content;file.sha256='saved-'+file.name;result={sha256:file.sha256,backup:`backups/2025-01-01/${file.name}.010101`};}
      } else result=file;
    }
    else if(url.pathname==='/api/outbox') result={items:[{id:'q1',kind:'bus',title:'Task result',preview:'Result preview',created:null,state:'pending'}]};
    else if(url.pathname==='/api/search') result={hits:[{tid:'1.2',seq:2,ts:null,role:'bot',snippet:'Message 2'}]};
    else if(url.pathname.endsWith('/messages')&&request.method()==='GET') {
      const before=url.searchParams.get('before');const eligible=entries.filter(entry=>before===null||entry.seq<Number(before));
      result={entries:eligible.slice(-100),has_more:eligible.length>100};
    } else if(url.pathname.endsWith('/messages')&&request.method()==='POST') {
      assert.equal(request.headers()['x-dtw-csrf'],'fake-csrf');status=202;result={turn_id:'browser-turn'};
      wsPeer.send(JSON.stringify({type:'turn',tid:'1.2',turn_id:'browser-turn',phase:'started'}));
      wsPeer.send(JSON.stringify({type:'delta',tid:'1.2',turn_id:'browser-turn',text:'Live **answer** <script>x</script>'}));
    } else if(url.pathname.endsWith('/abort')) {
      result={killed:1};const entry={...entries[0],id:'aborted',seq:206,source:'web',turn_id:'browser-turn',status:'aborted',text:'Live answer',mirror:{state:'pending'}};entries.push(entry);
      wsPeer.send(JSON.stringify({type:'entry',tid:'1.2',entry}));wsPeer.send(JSON.stringify({type:'turn',tid:'1.2',turn_id:'browser-turn',phase:'aborted'}));
    } else throw new Error('Unexpected browser API request: '+url.pathname);
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(result)});
  });
  await page.goto(origin);
  await page.locator('#passwordNext').waitFor({state:'visible'});
  for(const preference of ['system','dark','light']) {
    await page.locator('#auth .theme-btn').click();
    await page.locator(`#auth [data-pref="${preference}"]`).click();
    assert.equal(await page.locator('html').getAttribute('data-theme-pref'),preference);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(artifactDir,`auth-${width}-${preference}.png`),fullPage:true});
  }
  await page.locator('#auth .theme-btn').click();await page.keyboard.press('ArrowDown');await page.keyboard.press('Escape');
  assert.equal(await page.locator('#auth .theme-btn').evaluate(element=>element===document.activeElement),true);
  await page.locator('#pw').fill('wrong'); await page.locator('#passwordNext').click();
  await page.getByRole('alert').filter({hasText:strings['auth.invalid']}).waitFor();
  await page.locator('#pw').fill('good-password'); await page.locator('#passwordNext').click();
  await page.locator('#totpForm').waitFor({state:'visible'});
  await page.screenshot({path:path.join(artifactDir,`otp-${width}.png`),fullPage:true});
  await page.locator('#otp input').nth(1).focus(); await page.keyboard.press('Backspace');
  assert.equal(await page.locator('#otp input').first().evaluate(element=>element===document.activeElement),true);
  await page.locator('#otp input').first().evaluate(element=>{
    const clipboardData=new DataTransfer();clipboardData.setData('text','123456');
    element.dispatchEvent(new ClipboardEvent('paste',{clipboardData,bubbles:true,cancelable:true}));
  });
  await page.locator('#app').waitFor({state:'visible'});
  await page.locator('[data-entry-seq="205"]').waitFor();
  await page.screenshot({path:path.join(artifactDir,`shell-${width}.png`),fullPage:true});
  if(width<=760)await page.locator('[data-route="list"]').click();
  await page.locator('#q').fill('Message');await page.locator('#tlist [data-seq="2"]').waitFor();
  await page.locator('#tlist [data-seq="2"]').focus();await page.keyboard.press('Enter');
  await page.locator('[data-entry-seq="2"]').waitFor();
  await page.locator('#msgIn').fill('Hello <script>');await page.locator('#stopBtn').click();
  await page.locator('#streamMsg .caret').waitFor();
  assert.ok((await page.locator('#streamMsg').textContent()).includes('<script>'));
  assert.equal(await page.locator('#streamMsg script').count(),0);
  await page.screenshot({path:path.join(artifactDir,`stream-${width}.png`),fullPage:true});
  await page.locator('#stopBtn').click();await page.getByText(strings['chat.stopped'],{exact:true}).waitFor();
  assert.equal(await page.locator('#streamMsg .caret').count(),0);
  await page.locator('#telegramBadge').click();await page.locator('#connPanel h2').focus();
  await page.getByText('Task result',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:path.join(artifactDir,`chat-status-${width}.png`),fullPage:true});
  // Deferred management and file checks: native modal focus, API mutations and mobile views.
  if(width<=1180)await page.locator('.info-close').click();
  await page.locator('[data-view="chat"] .pane-head [data-route="manage"]').click();
  await page.locator('#mlist [data-rename="1.2"]').click();
  await page.locator('#renameInput').fill('Renamed <script>');await page.locator('#renameInput').press('Enter');
  await page.locator('#mlist .nm').filter({hasText:'Renamed <script>'}).waitFor();
  assert.equal(await page.locator('#mlist script').count(),0);
  await page.screenshot({path:path.join(artifactDir,`manage-${width}.png`),fullPage:true});
  await page.locator('#mlist [data-delete-tid="1.3"]').click();await page.locator('#delDlg').waitFor({state:'visible'});
  assert.equal(await page.locator('#delGo').isDisabled(),true);
  await page.locator('#delIn').fill('archive');assert.equal(await page.locator('#delGo').isDisabled(),true);
  await page.locator('#delIn').fill('Archive');assert.equal(await page.locator('#delGo').isEnabled(),true);
  await page.locator('#delGo').focus();await page.keyboard.press('Tab');
  assert.equal(await page.locator('#delIn').evaluate(element=>element===document.activeElement),true);
  await page.keyboard.press('Shift+Tab');assert.equal(await page.locator('#delGo').evaluate(element=>element===document.activeElement),true);
  await page.keyboard.press('Escape');await page.locator('#delDlg').waitFor({state:'hidden'});
  assert.equal(await page.locator('#mlist [data-delete-tid="1.3"]').evaluate(element=>element===document.activeElement),true);
  await page.locator('#mlist [data-delete-tid="1.3"]').click();await page.locator('#delIn').fill('Archive');await page.locator('#delGo').click();
  await page.locator('#mlist [data-delete-tid="1.3"]').waitFor({state:'detached'});
  await page.locator('.bar-acts [data-route="files"]').click();await page.waitForFunction(()=>!document.querySelector('#ed').disabled);
  await page.locator('#ed').fill('# Draft\n- **bold**\n<script>x</script>');
  assert.equal(await page.locator('#flist [data-file="PERSONA.md"]').getAttribute('data-dirty'),'');
  await page.locator('#flist [data-file="BEHAVIOR.md"]').click();await page.waitForFunction(()=>document.querySelector('#fname').textContent==='BEHAVIOR.md'&&!document.querySelector('#ed').disabled);
  await page.locator('#flist [data-file="PERSONA.md"]').click();await page.waitForFunction(()=>document.querySelector('#fname').textContent==='PERSONA.md');
  assert.ok((await page.locator('#ed').inputValue()).includes('# Draft'));
  await page.locator('#tPrev').focus();await page.keyboard.press('Enter');await page.locator('#prev').waitFor({state:'visible'});
  assert.equal(await page.locator('#tPrev').evaluate(element=>element===document.activeElement),true);
  assert.equal(await page.locator('#prev script').count(),0);assert.ok((await page.locator('#prev').textContent()).includes('<script>'));
  await page.locator('#fileSave').focus();await page.keyboard.press('Enter');await page.locator('#diffDlg').waitFor({state:'visible'});
  await page.locator('#diffSave').focus();await page.keyboard.press('Tab');
  assert.equal(await page.locator('#diffDlg [data-close-dialog]').evaluate(element=>element===document.activeElement),true);
  await page.screenshot({path:path.join(artifactDir,`diff-${width}.png`),fullPage:true});
  await page.keyboard.press('Escape');await page.locator('#diffDlg').waitFor({state:'hidden'});
  assert.equal(await page.locator('#fileSave').evaluate(element=>element===document.activeElement),true);
  await page.keyboard.press('Enter');await page.locator('#diffSave').click();await page.locator('#diffDlg').waitFor({state:'hidden'});
  assert.equal(await page.locator('#flist [data-file="PERSONA.md"]').getAttribute('data-dirty'),null);
  assert.ok((await page.locator('#backupTo').textContent()).includes('PERSONA.md.010101'));
  await page.locator('#flist [data-file="MAINMEMORY.md"]').click();await page.locator('#fileSharedHint').waitFor({state:'visible'});
  await page.locator('#ed').fill('Local pending');fileData['MAINMEMORY.md'].sha256='external';fileData['MAINMEMORY.md'].content='External current';
  await page.locator('#fileSave').click();await page.locator('#diffSave').click();await page.locator('#fileReload').waitFor({state:'visible'});
  assert.equal(await page.locator('#ed').inputValue(),'Local pending');
  await page.locator('#fileReload').click();await page.waitForFunction(()=>document.querySelector('#ed').value==='External current');
  assert.equal(await page.locator('#flist [data-file="MAINMEMORY.md"]').getAttribute('data-dirty'),null);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:path.join(artifactDir,`files-${width}.png`),fullPage:true});
  authenticated=false;configured=false;await page.reload();
  await page.locator('#setupLink').waitFor({state:'visible'});await page.locator('#setupLink').click();
  assert.equal(new URL(page.url()).hash,'#pw');
  await page.locator('#pw').fill('good-password');await page.locator('#passwordNext').click();
  await page.locator('#qr svg').waitFor();assert.equal(newQrCount,1);
  await page.screenshot({path:path.join(artifactDir,`setup-${width}.png`),fullPage:true});
  await page.locator('#regenerate').click();await page.waitForFunction(()=>!document.querySelector('#regenerate').disabled);
  assert.equal(newQrCount,2);
  await page.locator('#segPaste').click();await page.locator('#seedIn').fill('JBSWY3DPEHPK3PXP');
  for(let index=0;index<6;index++)await page.locator('#otp2 input').nth(index).fill('123456'[index]);
  await page.locator('#pwForm').waitFor({state:'visible'});
  assert.equal(await page.locator('.seed').textContent(),'');
  assert.deepEqual(failures,[]);
  await context.close();
} finally {await browser.close();}
"""


@pytest.mark.skipif(
    not os.environ.get("DTW_UI_TEST_ORIGIN"),
    reason="Browser execution requires the separately approved L verification environment",
)
@pytest.mark.parametrize("width", [1280, 768, 375])
def test_browser_auth_theme_keyboard_and_capture(width, tmp_path):
    """Deferred: run only against an explicitly approved test origin with Playwright."""
    assert os.environ.get("DTW_PLAYWRIGHT_MODULE")
    result = subprocess.run(
        ["node", "--input-type=module", "-e", BROWSER_TESTS],
        input=json.dumps(
            {
                "origin": os.environ["DTW_UI_TEST_ORIGIN"],
                "width": width,
                "artifactDir": str(tmp_path),
                "strings": STRINGS,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


CHAT_CLIENT_TESTS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const {staticDir,strings}=JSON.parse(fs.readFileSync(0,'utf8'));
const pending=[];
class Element {
  constructor(){this.listeners={};this.dataset={};this.children=[];this.textContent='';this.html='';this.value='';this.hidden=false;this.scrollTop=0;this.scrollHeight=500;this.clientHeight=500;this.attributes={};this.disabled=false;}
  addEventListener(type,callback){(this.listeners[type] ||= []).push(callback);}
  emit(type,extra={}){for(const callback of this.listeners[type] || []){const result=callback({type,target:this,preventDefault(){this.prevented=true},...extra});if(result?.then)pending.push(result);}}
  setAttribute(key,value){this.attributes[key]=String(value);}
  removeAttribute(key){delete this.attributes[key];}
  appendChild(child){this.children.push(child);return child;}
  append(...children){this.children.push(...children);}
  replaceChildren(){this.children=[];this.html='';}
  remove(){}
  focus(){document.activeElement=this;}
  closest(selector){return selector==='[data-tid]'&&this.dataset.tid?this:null;}
  querySelector(selector){
    if(selector.startsWith('[data-entry-seq=')){
      const seq=Number(selector.match(/"(-?\d+)"/)[1]);
      return this.html.includes(`data-entry-seq="${seq}"`)?{scrollIntoView(){scrolled=seq}}:null;
    }
    return (this.nodes ||= {})[selector] ||= new Element();
  }
  querySelectorAll(){return [];}
  get innerHTML(){return this.html;}
  set innerHTML(value){this.html=value;}
}
const document=new Element();document.documentElement={lang:'en'};document.hidden=false;
const nodes=new Map();
document.querySelector=selector=>{if(!nodes.has(selector))nodes.set(selector,new Element());return nodes.get(selector)};
const infoValues=Array.from({length:3},()=>new Element());
document.querySelectorAll=selector=>selector==='#topicPanel dd'?infoValues:[];
document.createElement=()=>new Element();
document.dispatchEvent=event=>{document.emit(event.type,{detail:event.detail});return true;};
class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail}}
const sockets=[], timers=new Map();let timerId=0,scrolled=null;
class WebSocket extends Element{
  constructor(url){super();this.url=String(url);sockets.push(this)}
  close(){this.closed=true;this.emit('close')}
}
const storage=new Map(), calls=[], notices=[], routes=[];
let topicsHold=null;
let blockedStorage=false, session=true, coarse=false, failSend=null, turnNumber=0;
const localStorage={getItem:key=>{if(blockedStorage)throw Error('blocked');return storage.get(key)??null},setItem:(key,value)=>{if(blockedStorage)throw Error('blocked');storage.set(key,value)}};
const makeEntry=(seq,extras={})=>({id:`e${seq}`,seq,ts:'2025-01-01T01:01:00Z',role:'bot',source:'telegram',kind:'message',text:`Entry ${seq}`,imported:seq<0,mirror:null,...extras});
const historyA=Array.from({length:205},(_,index)=>makeEntry(index+1));
const historyB=[makeEntry(-5,{source:'import',text:'Imported <script>x</script>'})];
const historyUnknown=[makeEntry(-9,{source:'import'})];
let topicRows=[
 {tid:'1.2',chat_id:1,topic_id:2,name:'Alpha <img>',group:'telegram',last_ts:'2025-01-02T00:00:00Z',preview:'preview',count:205,imported_count:0,created_at:null,busy:false,renamable:true,deletable:true,read_only:false},
 {tid:'1.3',chat_id:1,topic_id:3,name:'Archive',group:'imported',last_ts:null,preview:'',count:1,imported_count:1,created_at:null,busy:false,renamable:true,deletable:true,read_only:false},
 {tid:'unknown',chat_id:null,topic_id:null,name:'Unknown',group:'unknown',last_ts:null,preview:'',count:1,imported_count:1,busy:false,renamable:false,deletable:false,read_only:true}
];
const queue=[{id:'q1',tid:'1.2',kind:'user_mirror',preview:'<script>user</script>',created:null,state:'pending'},
 {id:'q2',tid:'1.2',kind:'reply_mirror',preview:'reply',created:null,state:'pending'},
 {id:'q3',tid:'1.2',kind:'bus',title:'Result <img>',preview:'ignored',created:null,state:'pending',origin:'task_result'}];
function response(status,body){return {status,ok:status<400,json:async()=>structuredClone(body)}}
async function fetch(url,options={}){
  const parsed=new URL(url,'https://chat.example.com');const body=options.body?JSON.parse(options.body):null;
  calls.push({url,options,body});
  if(parsed.pathname==='/api/i18n')return response(200,{lang:'en',strings});
  if(parsed.pathname==='/api/auth/state')return response(200,{authenticated:session,csrf:'fake-csrf'});
  if(!session)return response(403,{error:'auth_required'});
  if(options.method==='POST'){
    assert.equal(options.headers['X-Dtw-Client'],'web');assert.equal(options.headers['X-Dtw-Csrf'],'fake-csrf');
    if(parsed.pathname.endsWith('/abort'))return response(200,{killed:1});
    if(parsed.pathname.endsWith('/messages')){
      if(failSend)return response(409,{error:failSend});
      assert.ok(body.text.trim());assert.ok(body.client_id);
      return response(202,{turn_id:`turn${++turnNumber}`});
    }
    if(parsed.pathname==='/api/topics'){
      const topic={...topicRows[0],tid:'1.4',topic_id:4,name:body.name,count:0};topicRows.push(topic);return response(201,{topic});
    }
  }
  if(parsed.pathname==='/api/topics'){const snapshot=structuredClone(topicRows);if(topicsHold)await topicsHold;return response(200,{topics:snapshot});}
  if(parsed.pathname==='/api/search')return response(200,{hits:[{tid:'1.2',seq:2,ts:null,role:'bot',snippet:'Found <script>x</script>'}]});
  if(parsed.pathname==='/api/state')return response(200,{telegram:{state:'down',down_since:'2025-01-01T00:00:00Z',last_check:null,interval_s:30,strip:['ok','mixed','down',...Array(27).fill('none')],strip_end:null},outbox:{pending:3},bot:{up:true},web_sessions:2});
  if(parsed.pathname==='/api/outbox')return response(200,{items:queue});
  if(parsed.pathname.endsWith('/messages')){
    const tid=decodeURIComponent(parsed.pathname.split('/')[3]);
    let entries=tid==='1.2'?historyA:tid==='1.3'?historyB:tid==='unknown'?historyUnknown:[];
    if(parsed.searchParams.has('before'))entries=entries.filter(entry=>entry.seq<Number(parsed.searchParams.get('before')));
    return response(200,{entries:entries.slice(-100),has_more:entries.length>100});
  }
  throw Error(`Unexpected API: ${url}`);
}
const context=vm.createContext({document,CustomEvent,WebSocket,fetch,URL,localStorage,console,
  location:{href:'https://chat.example.com/#list',hash:'#list'},
  setTimeout:(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id},
  clearTimeout:id=>timers.delete(id),
  matchMedia:()=>({matches:!coarse}),crypto:{randomUUID:()=>`client-${calls.length}`}});
const modules=new Map();
for(const [name,exports] of Object.entries({
 'router.js':{go:route=>{context.location.hash=`#${route}`;routes.push(route);document.dispatchEvent(new CustomEvent('route-change',{detail:{route,view:route==='manage'?'manage':'chat'}}))}},
 'auth.js':{notify:key=>notices.push(key)}
})){
  const module=new vm.SyntheticModule(Object.keys(exports),function(){for(const [key,value]of Object.entries(exports))this.setExport(key,value)},{context});
  await module.link(()=>{});await module.evaluate();modules.set(path.join(staticDir,'js',name),module);
}
async function load(filename){
  if(modules.has(filename))return modules.get(filename);
  const module=new vm.SourceTextModule(fs.readFileSync(filename,'utf8'),{context,identifier:filename});modules.set(filename,module);
  await module.link(specifier=>load(path.resolve(path.dirname(filename),specifier)));return module;
}
async function flush(){for(let i=0;i<10;i++){await new Promise(setImmediate);await Promise.all(pending.splice(0));}}
async function runTimer(delay){const entry=[...timers].find(([,value])=>value.delay===delay);assert.ok(entry,`timer ${delay}`);timers.delete(entry[0]);entry[1].callback();await flush();}
const chat=await load(path.join(staticDir,'js/chat.js'));await chat.evaluate();
const topics=modules.get(path.join(staticDir,'js/topics.js')).namespace;
const api=modules.get(path.join(staticDir,'js/api.js')).namespace;
await modules.get(path.join(staticDir,'js/i18n.js')).namespace.loadI18n();api.setCsrf('fake-csrf');
const markdown=modules.get(path.join(staticDir,'js/md.js')).namespace;
assert.equal(markdown.md('# Title\n## Sub\n- **bold**\n- `code`\n<script>x</script>\n[x](javascript:x)'),'<h1>Title</h1><h2>Sub</h2><ul><li><b>bold</b></li><li><code>code</code></li></ul><p>&lt;script&gt;x&lt;/script&gt;</p><p>[x](javascript:x)</p>');
const dedup=new Map();chat.namespace.mergeEntries(dedup,[makeEntry(2),makeEntry(-1)]);
assert.deepEqual(Array.from(chat.namespace.mergeEntries(dedup,[makeEntry(2,{text:'updated'})]),e=>e.seq),[-1,2]);assert.equal(dedup.size,2);
// Session start: three groups, real snapshots and exact unread count after stored seq.
storage.set('dtw-seen:1.2','203');topics.initTopics();chat.namespace.initChat();
document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:true}}));await flush();
assert.equal(sockets.length,1);assert.equal(sockets[0].url,'wss://chat.example.com/api/ws');
const list=document.querySelector('#tlist'),thread=document.querySelector('#thread');
assert.ok(list.innerHTML.includes('Alpha &lt;img&gt;'));assert.ok(!list.innerHTML.includes('Alpha <img>'));
assert.ok(list.innerHTML.includes(strings['topic.group_imported']));assert.ok(list.innerHTML.includes(strings['topic.group_unknown']));
assert.equal(topics.getTopic().tid,'1.2');assert.ok(thread.innerHTML.includes('Entry 205'));assert.ok(!thread.innerHTML.includes('Entry 1</p>'));
assert.equal(storage.get('dtw-seen:1.2'),'203');assert.ok(list.innerHTML.includes('2 unread'));
topics.selectTopic('1.2');await flush();assert.equal(storage.get('dtw-seen:1.2'),'205');
assert.equal(document.querySelector('#outboxCount').textContent,3);
assert.ok(document.querySelector('#outboxRows').innerHTML.includes('Result &lt;img&gt;'));
assert.ok(document.querySelector('#outboxRows').innerHTML.includes(strings['outbox.tag_reply']));
assert.ok(document.querySelector('#hb').innerHTML.includes('data-s="pending"'));
assert.equal(document.querySelector('.composer .line').hidden,false);
assert.equal(document.querySelector('#webSessions').textContent,2);
// Older pages prepend and preserve scroll position; searching jumps to an earlier seq.
thread.scrollTop=0;thread.emit('scroll');await flush();assert.ok(thread.innerHTML.includes('Entry 6</p>'));
const q=document.querySelector('#q');q.value='entry';q.emit('input');await runTimer(300);
assert.ok(list.innerHTML.includes('Found &lt;script&gt;'));assert.ok(calls.some(call=>call.url==='/api/search?q=entry'));
topics.selectTopic('1.2',2);await flush();assert.equal(scrolled,2);assert.ok(thread.innerHTML.includes('Entry 1</p>'));
// Read-only unknown and writable imported histories; no invented timestamps.
topics.selectTopic('unknown');await flush();assert.equal(document.querySelector('.composer').hidden,true);assert.equal(document.querySelector('#topicDelete').hidden,true);
topics.selectTopic('1.3');await flush();assert.equal(document.querySelector('.composer').hidden,false);
assert.ok(thread.innerHTML.includes('Imported &lt;script&gt;'));assert.ok(thread.innerHTML.includes(strings['chat.imported_day']));assert.equal(infoValues[2].textContent,'—');
// Pointer-specific Enter, preserved drafts, duplicate-submit guard, web stop and terminal state.
topics.selectTopic('1.2');await flush();const input=document.querySelector('#msgIn'),send=document.querySelector('#stopBtn');
input.value='touch draft';coarse=true;input.emit('keydown',{key:'Enter',shiftKey:false,isComposing:false});await flush();assert.equal(turnNumber,0);
coarse=false;input.emit('keydown',{key:'Enter',shiftKey:true});await flush();assert.equal(turnNumber,0);
input.emit('keydown',{key:'Enter',shiftKey:false,isComposing:true});await flush();assert.equal(turnNumber,0);
input.value='hello <script>';input.emit('input');input.emit('keydown',{key:'Enter',shiftKey:false,isComposing:false});await flush();assert.equal(turnNumber,1);assert.equal(input.value,'');assert.equal(send.className,'btn btn-danger');
await chat.namespace.handleEvent({type:'turn',tid:'1.2',turn_id:'turn1',phase:'started'});
await chat.namespace.handleEvent({type:'delta',tid:'1.2',turn_id:'turn1',text:'**Hello** <img>'});
await chat.namespace.handleEvent({type:'tool',tid:'1.2',turn_id:'turn1',name:'Read <script>'});
assert.ok(thread.innerHTML.includes('<b>Hello</b> &lt;img&gt;'));assert.ok(thread.innerHTML.includes('Read &lt;script&gt;'));
send.emit('click');await flush();assert.ok(calls.some(call=>call.url==='/api/topics/1.2/abort'));assert.ok(!calls.some(call=>call.url==='/api/topics/1.3/abort'));
await chat.namespace.handleEvent({type:'turn',tid:'1.2',turn_id:'turn1',phase:'aborted'});assert.equal(send.className,'btn btn-primary');assert.ok(thread.innerHTML.includes(strings['chat.stopped']));assert.ok(!thread.innerHTML.includes('class="caret"'));
const final=makeEntry(206,{source:'web',turn_id:'turn1',status:'aborted',text:'Final',mirror:{state:'pending'}});historyA.push(final);
await chat.namespace.handleEvent({type:'entry',tid:'1.2',entry:final});await chat.namespace.handleEvent({type:'entry',tid:'1.2',entry:final});
assert.equal((thread.innerHTML.match(/data-entry-seq="206"/g)||[]).length,1);assert.ok(!thread.innerHTML.includes('data-turn="turn1"'));assert.ok(thread.innerHTML.includes(strings['chat.pending_mirror']));
final.mirror={state:'sent'};await chat.namespace.handleEvent({type:'outbox',pending:0,item:{...queue[0],state:'sent'}});assert.ok(thread.innerHTML.includes(strings['chat.delivered']));assert.equal(document.querySelector('#pendingBadge').hidden,true);
final.mirror={state:'failed'};await chat.namespace.handleEvent({type:'outbox',pending:0,item:{...queue[0],state:'failed'}});assert.ok(thread.innerHTML.includes(strings['chat.mirror_failed']));
// Telegram busy displays metadata only, never a synthetic body or web stop action.
await chat.namespace.handleEvent({type:'busy',tid:'1.2',busy:true,source:'telegram'});assert.ok(thread.innerHTML.includes(strings['chat.answering']));assert.equal(send.className,'btn btn-primary');
await chat.namespace.handleEvent({type:'busy',tid:'1.2',busy:false,source:'telegram'});
failSend='web_turn_running';input.value='retry me';send.emit('click');await flush();assert.equal(input.value,'retry me');assert.equal(notices.at(-1),'error.web_turn_running');failSend=null;
const bus=makeEntry(207,{role:'event',source:'system',kind:'task_result',status:'failed',title:'Task <img>',text:'failure',mirror:{state:'pending'}});historyA.push(bus);await chat.namespace.handleEvent({type:'entry',tid:'1.2',entry:bus});assert.ok(thread.innerHTML.includes('badge-down'));assert.ok(thread.innerHTML.includes('Task &lt;img&gt;'));
// Hidden views keep unseen entries unread. Local storage denial never breaks selection.
q.value='';q.emit('input');
context.location.hash='#files';document.emit('route-change',{detail:{route:'files',view:'files'}});
await chat.namespace.handleEvent({type:'entry',tid:'1.3',entry:makeEntry(1,{text:'new'})});assert.ok(list.innerHTML.includes('1 unread'));
blockedStorage=true;topics.selectTopic('1.3');await flush();blockedStorage=false;
// Topic creation hands off the created row for the next management chunk.
document.querySelector('.tfoot button').emit('click');await flush();assert.equal(routes.at(-1),'manage');assert.equal(topics.getTopic().tid,'1.4');
// Browser socket loss does not send an abort. Reconnect snapshots recover missed history.
topics.selectTopic('1.2');await flush();input.value='finish offline';send.emit('click');await flush();assert.equal(send.className,'btn btn-danger');
const aborts=calls.filter(call=>call.url.endsWith('/abort')).length;
sockets[0].close();await flush();assert.equal(document.querySelector('#botBadge').className,'badge badge-down');assert.equal(calls.filter(call=>call.url.endsWith('/abort')).length,aborts);
historyA.push(makeEntry(208,{text:'missed while offline',source:'web',turn_id:'turn2',status:'ok'}));await runTimer(2000);assert.equal(sockets.length,2);sockets[1].emit('open');await flush();assert.ok(thread.innerHTML.includes('missed while offline'));assert.equal(send.className,'btn btn-primary');
await chat.namespace.handleEvent({type:'bridge',up:false});assert.equal(document.querySelector('#botBadge').className,'badge badge-down');
await chat.namespace.handleEvent({type:'bridge',up:true});await flush();assert.equal(document.querySelector('#botBadge').className,'badge badge-ok');
// Auth rejection resets the session through the existing auth-required contract and stops sockets.
let required=false;document.addEventListener('auth-required',()=>{required=true;document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:false}}))});
session=false;sockets[1].close();await flush();assert.equal(required,true);assert.equal(thread.innerHTML,'');assert.equal(input.value,'');assert.equal(sockets[1].closed,true);assert.equal(document.querySelector('#outboxRows').innerHTML,'');assert.equal(document.querySelector('#pendingBadge').hidden,true);
// A standalone connection doubles retries up to 30 seconds and cancels on cleanup.
session=true;const ws=modules.get(path.join(staticDir,'js/ws.js')).namespace;const stop=ws.connectEvents(()=>{},()=>{});
for(const delay of [2000,4000,8000,16000,30000,30000]){sockets.at(-1).close();await flush();await runTimer(delay)}
stop();assert.equal(sockets.at(-1).closed,true);
// A fresh browser recovers Stop from the active web turn snapshot; Telegram busy stays Send.
topicRows[0].busy=true;topicRows[0].turn_id='restored-turn';
let releaseTopics;topicsHold=new Promise(resolve=>{releaseTopics=resolve});
document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:true}}));
await new Promise(setImmediate);
await chat.namespace.handleEvent({type:'telegram',state:'up'});
await chat.namespace.handleEvent({type:'turn',tid:'1.3',turn_id:'unrelated-turn',phase:'started'});
await chat.namespace.handleEvent({type:'busy',tid:'1.2',busy:false});
releaseTopics();topicsHold=null;await flush();
topics.selectTopic('1.2');await flush();assert.equal(send.className,'btn btn-danger');
const restoredAborts=calls.filter(call=>call.url.endsWith('/abort')).length;
send.emit('click');await flush();assert.equal(calls.filter(call=>call.url.endsWith('/abort')).length,restoredAborts+1);
topicRows[0].turn_id=null;await chat.namespace.handleEvent({type:'bridge',up:true});await flush();
assert.equal(send.className,'btn btn-primary');assert.ok(thread.innerHTML.includes(strings['chat.answering']));
// A newer turn in the same topic survives the older delayed snapshot.
topicsHold=new Promise(resolve=>{releaseTopics=resolve});
const syncing=chat.namespace.handleEvent({type:'bridge',up:true});await new Promise(setImmediate);
await chat.namespace.handleEvent({type:'turn',tid:'1.2',turn_id:'newer-turn',phase:'started'});
releaseTopics();topicsHold=null;await syncing;await flush();assert.equal(send.className,'btn btn-danger');
await chat.namespace.handleEvent({type:'turn',tid:'1.2',turn_id:'newer-turn',phase:'done'});
topicRows[0].busy=false;document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:false}}));
// No topic means no send target: keep the composer hidden rather than accepting a lost draft.
topicRows=[];document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:true}}));await flush();assert.equal(document.querySelector('.composer').hidden,true);assert.equal(send.disabled,true);document.dispatchEvent(new CustomEvent('auth-change',{detail:{authenticated:false}}));
console.log('Chat contracts passed: groups, search, unread, pagination, safe Markdown, turns, stop, status, outbox, reconnect and session reset');
"""


def test_chat_client_against_fake_api_and_event_contract():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", CHAT_CLIENT_TESTS],
        input=json.dumps({"staticDir": str(STATIC), "strings": STRINGS}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


MANAGEMENT_CLIENT_TESTS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {performance} from 'node:perf_hooks';
const {staticDir,strings}=JSON.parse(fs.readFileSync(0,'utf8'));
const pending=[],calls=[],notices=[],saved=[];
let document,hash='#chat',authenticated=true,unavailable=false,putError=null,readHold=null;
const decode=text=>text.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const camel=name=>name.replace(/-([a-z])/g,(_match,letter)=>letter.toUpperCase());
class Element {
  constructor(id=''){this.id=id;this.listeners={};this.dataset={};this.attributes={};this.value='';this.textContent='';this.html='';this.children=[];this.disabled=false;this.hidden=false;this.open=false;this.isConnected=true;this.classes=new Set();this.classList={toggle:(name,force)=>{if(force??!this.classes.has(name))this.classes.add(name);else this.classes.delete(name)},contains:name=>this.classes.has(name)};}
  addEventListener(type,callback){(this.listeners[type] ||= []).push(callback)}
  emit(type,extra={}){const event={type,target:this,currentTarget:this,preventDefault(){this.defaultPrevented=true},stopPropagation(){},...extra};for(const callback of this.listeners[type] || []){const result=callback(event);if(result?.then)pending.push(result)}return event;}
  appendChild(child){child.parent=this;this.children.push(child);return child}
  append(...children){children.forEach(child=>this.appendChild(child))}
  replaceChildren(){for(const child of this.children)child.isConnected=false;this.children=[];this.html=''}
  setAttribute(name,value){this.attributes[name]=String(value)}
  hasAttribute(name){return Object.hasOwn(this.attributes,name)}
  removeAttribute(name){delete this.attributes[name]}
  toggleAttribute(name,on){if(on)this.setAttribute(name,'');else this.removeAttribute(name)}
  focus(){document.activeElement=this}
  select(){this.selectionStart=0;this.selectionEnd=this.value.length}
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end}
  remove(){this.isConnected=false}
  closest(selector){
    if(selector==='[hidden], .hidden')return this.hidden||this.classes.has('hidden')?this:null;
    if(selector==='form')return this.form || null;
    const match=selector.match(/^\[data-([\w-]+)\]$/);return match && Object.hasOwn(this.dataset,camel(match[1]))?this:null;
  }
  contains(element){return this===element||this.children.includes(element)}
  getClientRects(){return this.hidden?[]:[{}]}
  showModal(){this.open=true;this.children.find(child=>!child.disabled&&!child.hidden)?.focus()}
  close(){this.open=false}
  querySelector(selector){if(selector==='[type="submit"]')return this.children.find(child=>child.type==='submit');return null}
  querySelectorAll(selector){
    if(selector==='button')return this.children.filter(child=>child.tag==='button');
    if(selector.includes(':not([disabled])'))return this.children.filter(child=>!child.disabled);
    const match=selector.match(/^\[data-([\w-]+)\]$/);return match?this.children.filter(child=>Object.hasOwn(child.dataset,camel(match[1]))):[];
  }
  get innerHTML(){return this.html}
  set innerHTML(value){
    this.replaceChildren();this.html=value;
    if(this.id!=='mlist')return;
    for(const match of value.matchAll(/<button\b([^>]*)>/g)){
      const button=new Element();button.tag='button';button.disabled=/\bdisabled\b/.test(match[1]);button.type=/type="submit"/.test(match[1])?'submit':'button';
      for(const attr of match[1].matchAll(/data-([\w-]+)(?:="([^"]*)")?/g))button.dataset[camel(attr[1])]=decode(attr[2]||'');
      this.appendChild(button);
    }
    const input=value.match(/<input id="renameInput"[^>]*value="([^"]*)"([^>]*)>/);
    if(input){const element=new Element('renameInput');element.value=decode(input[1]);element.disabled=/disabled/.test(input[2]);element.form=this;this.appendChild(element)}
  }
}
document=new Element();document.body=new Element();document.documentElement={lang:'en'};document.activeElement=new Element('initial');
const nodes=new Map();const get=selector=>{if(!nodes.has(selector))nodes.set(selector,new Element(selector.startsWith('#')?selector.slice(1):selector));return nodes.get(selector)};
const fileButtons=['PERSONA.md','BEHAVIOR.md','MAINMEMORY.md','SHAREDMEMORY.md'].map(name=>{const element=new Element();element.dataset.file=name;return element});
const authViews=['pw','totp','setup'].map(name=>{const element=new Element();element.dataset.auth=name;return element});
const views=['chat','manage','files'].map(name=>{const element=new Element();element.dataset.view=name;return element});
const del=get('#delDlg'),diffDialog=get('#diffDlg'),delCancel=get('#delCancel'),diffCancel=get('#diffCancel');
delCancel.dataset.closeDialog='';diffCancel.dataset.closeDialog='';del.children=[get('#delIn'),delCancel,get('#delGo')];diffDialog.children=[diffCancel,get('#diffSave')];
document.querySelector=selector=>selector==='#renameInput'?get('#mlist').children.find(child=>child.id==='renameInput')||null:get(selector);
document.querySelectorAll=selector=>selector==='#flist [data-file]'?fileButtons:selector==='dialog'?[del,diffDialog]:selector==='[data-auth]'?authViews:selector==='[data-view]'?views:selector==='#mlist [data-rename]'?get('#mlist').querySelectorAll('[data-rename]'):selector==='#mlist [data-delete-tid]'?get('#mlist').querySelectorAll('[data-delete-tid]'):[];
document.createElement=()=>new Element();document.dispatchEvent=event=>{document.emit(event.type,{detail:event.detail});return true};
class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail}}
const window=new Element();
const location={get hash(){return hash},set hash(value){hash=value.startsWith('#')?value:`#${value}`;pending.push(Promise.resolve().then(()=>window.emit('hashchange')))}};
const history={replaceState:(_state,_title,value)=>{hash=value}};
let topicRows=[
 {tid:'1.2',chat_id:1,topic_id:2,name:'Alpha <img>',group:'telegram',count:5,imported_count:0,last_ts:null,renamable:true,deletable:true},
 {tid:'1',chat_id:1,topic_id:null,name:'General',group:'telegram',count:0,imported_count:0,last_ts:null,renamable:false,deletable:false},
 {tid:'1.3',chat_id:1,topic_id:3,name:'Archive',group:'imported',count:10,imported_count:10,last_ts:null,renamable:true,deletable:true},
 {tid:'unknown',name:'Unknown',group:'unknown',count:1,imported_count:1,renamable:false,deletable:false}
];
const remote=Object.fromEntries(fileButtons.map(button=>[button.dataset.file,{name:button.dataset.file,content:`# ${button.dataset.file}\n\nOriginal`,sha256:`base-${button.dataset.file}`,shared_block:button.dataset.file==='MAINMEMORY.md'}]));
function response(status,data){return{status,ok:status<400,json:async()=>structuredClone(data)}}
async function fetch(url,options={}){
 const body=options.body?JSON.parse(options.body):null;calls.push({url,options,body});
 if(url==='/api/i18n')return response(200,{lang:'en',strings});
 if(!authenticated)return response(403,{error:'auth_required'});
 if(['PATCH','DELETE','PUT'].includes(options.method)){assert.equal(options.headers['X-Dtw-Client'],'web');assert.equal(options.headers['X-Dtw-Csrf'],'fake-csrf');}
 if(url==='/api/topics')return response(200,{topics:topicRows});
 if(url.startsWith('/api/topics/')){
  if(unavailable)return response(503,{error:'telegram_unavailable'});
  const tid=url.split('/')[3],topic=topicRows.find(topic=>topic.tid===tid);
  if(options.method==='PATCH'){if(!topic.renamable)return response(409,{error:'not_a_topic'});topic.name=body.name;return response(200,{topic})}
  if(options.method==='DELETE'){if(body.confirm_name!==topic.name)return response(400,{error:'confirm_mismatch'});topicRows=topicRows.filter(topic=>topic.tid!==tid);return response(200,{telegram_deleted:true,session_removed:true,history_entries:topic.count,outbox_dropped:1})}
 }
 if(url==='/api/files')return response(200,{files:Object.values(remote).map(file=>({name:file.name,sha256:file.sha256,size:Buffer.byteLength(file.content),mtime:null}))});
 if(url.startsWith('/api/files/')){
  const name=decodeURIComponent(url.slice('/api/files/'.length)),file=remote[name];assert.ok(file);
  if(options.method==='PUT'){
   if(putError)return response(putError,{error:putError===413?'too_large':'generic'});
   if(body.base_sha256!==file.sha256)return response(409,{error:'conflict'});
   assert.ok(Buffer.byteLength(body.content)<=262144);file.content=body.content;file.sha256=`saved-${calls.length}`;saved.push(name);
   return response(200,{sha256:file.sha256,backup:`backups/2025-01-01/${name}.010101`});
  }
  if(readHold)await readHold;
  return response(200,file);
 }
 throw Error(`Unexpected contract path ${url}`);
}
const context=vm.createContext({document,window,location,history,CustomEvent,fetch,TextEncoder,console,
 localStorage:{getItem:()=>null,setItem(){}},setTimeout:()=>0,clearTimeout() {}});
const modules=new Map();
for(const [name,exports]of Object.entries({'auth.js':{notify:key=>notices.push(key)},'theme.js':{closeMenus:()=>{}}})){
 const module=new vm.SyntheticModule(Object.keys(exports),function(){for(const [key,value]of Object.entries(exports))this.setExport(key,value)},{context});await module.link(()=>{});await module.evaluate();modules.set(path.join(staticDir,'js',name),module);
}
async function load(filename){if(modules.has(filename))return modules.get(filename);const module=new vm.SourceTextModule(fs.readFileSync(filename,'utf8'),{context,identifier:filename});modules.set(filename,module);await module.link(specifier=>load(path.resolve(path.dirname(filename),specifier)));return module;}
async function flush(){for(let count=0;count<15;count++){await new Promise(setImmediate);await Promise.all(pending.splice(0));}}
const management=await load(path.join(staticDir,'js/manage.js'));await management.evaluate();
const files=await load(path.join(staticDir,'js/files.js'));await files.evaluate();
const router=modules.get(path.join(staticDir,'js/router.js')).namespace;
const topics=modules.get(path.join(staticDir,'js/topics.js')).namespace;
const api=modules.get(path.join(staticDir,'js/api.js')).namespace;
await modules.get(path.join(staticDir,'js/i18n.js')).namespace.loadI18n();api.setCsrf('fake-csrf');
router.initRouter();topics.initTopics();management.namespace.initManage();files.namespace.initFiles();router.setAuthenticated(true);router.renderRoute();await topics.refreshTopics();await flush();
const list=get('#mlist');const action=(key,tid)=>list.children.find(child=>child.dataset[key]===tid);
const clickList=element=>{assert.ok(element);element.focus();list.emit('click',{target:element})};
// Telegram and imported groups, API permissions and escaped names.
router.go('manage');await flush();assert.ok(list.innerHTML.includes(strings['topic.group_imported']));assert.ok(list.innerHTML.includes('Alpha &lt;img&gt;'));assert.ok(!list.innerHTML.includes('data-rename="1"'));assert.ok(!list.innerHTML.includes('Unknown'));
clickList(action('rename','1.2'));await flush();let input=document.querySelector('#renameInput');assert.equal(document.activeElement,input);
input.value='   ';list.emit('input',{target:input});list.emit('submit');await flush();assert.ok(!calls.some(call=>call.options.method==='PATCH'));
input.value='Renamed <script>';list.emit('input',{target:input});list.emit('submit');await flush();assert.equal(topicRows[0].name,'Renamed <script>');assert.ok(list.innerHTML.includes('Renamed &lt;script&gt;'));
// Network errors preserve the inline draft. Escape cancels without a PATCH.
clickList(action('rename','1.3'));input=document.querySelector('#renameInput');input.value='Keep draft';list.emit('input',{target:input});unavailable=true;list.emit('submit');await flush();assert.equal(notices.at(-1),'error.telegram_unavailable');assert.equal(document.querySelector('#renameInput').value,'Keep draft');unavailable=false;list.emit('keydown',{key:'Escape'});assert.equal(document.querySelector('#renameInput'),null);
// A newly created topic enters inline edit mode.
const created={...topicRows[0],tid:'1.4',topic_id:4,name:'New topic'};topicRows.push(created);await topics.refreshTopics();document.dispatchEvent(new CustomEvent('topic-created',{detail:created}));await flush();assert.equal(document.querySelector('#renameInput').value,'New topic');list.emit('keydown',{key:'Escape'});
// Exact confirmation, scoped DELETE, Tab/Shift+Tab trapping, Esc and opener return.
let deleteButton=action('deleteTid','1.3');clickList(deleteButton);await flush();assert.equal(del.open,true);assert.equal(get('#delGo').disabled,true);
get('#delIn').value='archive';get('#delIn').emit('input');assert.equal(get('#delGo').disabled,true);
get('#delIn').value='Archive';get('#delIn').emit('input');assert.equal(get('#delGo').disabled,false);
get('#delGo').focus();const tab=del.emit('keydown',{key:'Tab',shiftKey:false});assert.equal(tab.defaultPrevented,true);assert.equal(document.activeElement,get('#delIn'));
del.emit('keydown',{key:'Tab',shiftKey:true});assert.equal(document.activeElement,get('#delGo'));
await topics.refreshTopics();await flush();
del.emit('cancel');await flush();assert.equal(del.open,false);assert.equal(location.hash,'#manage');assert.equal(document.activeElement.dataset.deleteTid,'1.3');
clickList(action('deleteTid','1.3'));await flush();get('#delIn').value='Archive';get('#delIn').emit('input');get('#delGo').emit('click');await flush();assert.ok(!topicRows.some(topic=>topic.tid==='1.3'));assert.equal(del.open,false);assert.equal(document.activeElement,get('[data-view="manage"] h2'));
assert.ok(calls.some(call=>call.options.method==='DELETE'&&call.url==='/api/topics/1.3'&&call.body.confirm_name==='Archive'));
// The info-panel opener returns to its original status overlay, not a hidden view.
topics.selectTopic('1.2',null,false);router.go('status');await flush();get('#topicDelete').focus();get('#topicDelete').emit('click');await flush();del.emit('cancel');await flush();assert.equal(location.hash,'#status');assert.equal(document.activeElement,get('#topicDelete'));
router.go('delete');await flush();assert.equal(get('#delIn').value,'');assert.equal(get('#delGo').disabled,true);del.emit('cancel');await flush();assert.equal(location.hash,'#manage');
// File list, cached drafts, boolean dirty presence and undo back to original.
router.go('files');await flush();const editor=get('#ed');const persona=fileButtons[0];assert.ok(editor.value.includes('PERSONA.md'));assert.equal(editor.disabled,false);assert.equal(persona.hasAttribute('data-dirty'),false);
const original=editor.value;editor.value='# Draft\n- **bold**\n<script>x</script>';editor.emit('input');assert.equal(persona.hasAttribute('data-dirty'),true);assert.equal(persona.attributes['data-dirty'],'');
await files.namespace.openFile('BEHAVIOR.md');await flush();await files.namespace.openFile('PERSONA.md');await flush();assert.ok(editor.value.includes('# Draft'));
editor.value=original;editor.emit('input');assert.equal(persona.hasAttribute('data-dirty'),false);
editor.value='# Draft\n- **bold**\n<script>x</script>';editor.emit('input');router.go('preview');await flush();assert.ok(get('#prev').innerHTML.includes('<b>bold</b>'));assert.ok(get('#prev').innerHTML.includes('&lt;script&gt;'));assert.ok(!get('#prev').innerHTML.includes('<script>'));
get('#tPrev').emit('keydown',{key:'ArrowLeft'});await flush();assert.equal(location.hash,'#edit');assert.equal(get('#tEdit').attributes['aria-selected'],'true');assert.equal(document.activeElement,get('#tEdit'));
// Review opens a native modal; cancel restores the original save button and draft.
get('#fileSave').focus();get('#fileSave').emit('click');await flush();assert.equal(diffDialog.open,true);assert.ok(get('#diffBox').innerHTML.includes('&lt;script&gt;'));assert.equal(saved.length,0);
get('#diffSave').focus();diffDialog.emit('keydown',{key:'Tab',shiftKey:false});assert.equal(document.activeElement,diffCancel);
diffDialog.emit('cancel');await flush();assert.equal(document.activeElement,get('#fileSave'));assert.ok(editor.value.includes('# Draft'));
get('#fileSave').emit('click');await flush();get('#diffSave').emit('click');await flush();assert.equal(saved.at(-1),'PERSONA.md');assert.equal(persona.hasAttribute('data-dirty'),false);assert.equal(diffDialog.open,false);assert.ok(get('#backupTo').textContent.includes('webchat/backups/2025-01-01/PERSONA.md.010101'));
assert.equal(get('#toasts').children.at(-1).className,'toast toast-ok');
// Unchanged files have no diff and cannot write.
const puts=calls.filter(call=>call.options.method==='PUT').length;get('#fileSave').emit('click');await flush();assert.equal(get('#diffSave').disabled,true);assert.ok(get('#diffBox').innerHTML.includes(strings['files.no_changes']));diffDialog.emit('cancel');await flush();assert.equal(calls.filter(call=>call.options.method==='PUT').length,puts);
// Shared knowledge is hinted only for the stripped shared block. Conflict keeps the draft until explicit reload.
await files.namespace.openFile('MAINMEMORY.md');await flush();assert.equal(get('#fileSharedHint').hidden,false);
editor.value='Local pending';editor.emit('input');remote['MAINMEMORY.md'].sha256='external-sha';remote['MAINMEMORY.md'].content='External current';
get('#fileSave').emit('click');await flush();get('#diffSave').emit('click');await flush();assert.equal(notices.at(-1),'files.conflict');assert.equal(editor.value,'Local pending');assert.equal(get('#fileReload').hidden,false);assert.equal(fileButtons[2].hasAttribute('data-dirty'),true);
let releaseRead;readHold=new Promise(resolve=>{releaseRead=resolve});get('#fileReload').emit('click');await new Promise(setImmediate);assert.equal(editor.disabled,true);releaseRead();readHold=null;await flush();assert.equal(editor.value,'External current');assert.equal(get('#fileReload').hidden,true);assert.equal(fileButtons[2].hasAttribute('data-dirty'),false);
await files.namespace.openFile('BEHAVIOR.md');await flush();assert.equal(get('#fileSharedHint').hidden,true);
// Byte limits use UTF-8, not JS string length. Neither client nor server failure discards edits.
editor.value='é'.repeat(131073);editor.emit('input');get('#fileSave').emit('click');await flush();assert.equal(notices.at(-1),'files.too_large');assert.equal(diffDialog.open,false);assert.equal(editor.value.length,131073);
editor.value='Server rejects this';editor.emit('input');putError=413;get('#fileSave').emit('click');await flush();get('#diffSave').emit('click');await flush();assert.equal(notices.at(-1),'files.too_large');assert.equal(editor.value,'Server rejects this');assert.equal(fileButtons[1].hasAttribute('data-dirty'),true);assert.equal(get('#diffSave').disabled,false);putError=null;diffDialog.emit('cancel');await flush();
// Session loss clears file contents, cached drafts, dirty indicators and the active modal.
get('#fileSave').emit('click');await flush();router.setAuthenticated(false);router.go('pw');await flush();assert.equal(editor.value,'');assert.equal(get('#prev').innerHTML,'');assert.equal(diffDialog.open,false);assert.equal(fileButtons.some(button=>button.hasAttribute('data-dirty')),false);
// Diff reconstruction and minimal edit counts match the mockup LCS below the cap.
const diff=modules.get(path.join(staticDir,'js/diff.js')).namespace;
assert.equal(diff.MAX_DIFF_CELLS,4_000_000);
function naive(a,b){a=a.split('\n');b=b.split('\n');const L=Array.from({length:a.length+1},()=>Array(b.length+1).fill(0));for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)L[i][j]=a[i]===b[j]?L[i+1][j+1]+1:Math.max(L[i+1][j],L[i][j+1]);return a.length+b.length-2*L[0][0]}
function checkDiff(a,b,minimal=true){const rows=Array.from(diff.diff(a,b));assert.equal(rows.filter(([kind])=>kind!=='add').map(([,line])=>line).join('\n'),a);assert.equal(rows.filter(([kind])=>kind!=='del').map(([,line])=>line).join('\n'),b);if(minimal)assert.equal(rows.filter(([kind])=>kind!=='ctx').length,naive(a,b));return rows}
const samples=['','a','b','a\nb','b\nb','a\nb\na','<script>\n\nlast\n'];for(const a of samples)for(const b of samples)checkDiff(a,b);
const lines=Array(8192).fill('v'+'x'.repeat(30));const large=lines.join('\n')+'\n';assert.equal(Buffer.byteLength(large),262144);lines[4096]='w'+'x'.repeat(30);const changed=lines.join('\n')+'\n';let start=performance.now();const largeRows=checkDiff(large,changed,false);const oneLineMs=performance.now()-start;assert.equal(largeRows.filter(([kind])=>kind!=='ctx').length,2);
const capA=Array.from({length:2001},(_,i)=>`A${i}`).join('\n'),capB=Array.from({length:2001},(_,i)=>i===1000?'A1000':`B${i}`).join('\n');start=performance.now();const capRows=checkDiff(capA,capB,false);const capMs=performance.now()-start;assert.equal(capRows.length,4002);assert.equal(capRows.some(([kind])=>kind==='ctx'),false);
console.log(`Management/file contracts passed; 256 KB one-line diff ${oneLineMs.toFixed(2)} ms; capped diff ${capMs.toFixed(2)} ms`);
"""


def test_management_and_files_against_fake_contract():
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", MANAGEMENT_CLIENT_TESTS],
        input=json.dumps({"staticDir": str(STATIC), "strings": STRINGS}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_management_file_markup_matches_client_hooks(markup):
    elements = {attrs["id"]: attrs for _, attrs in markup.tags if "id" in attrs}
    for name in ["fileSave", "fileReload", "fileSharedHint", "backupTo", "backupPath", "diffSave"]:
        assert name in elements
    for name in ["fileSave", "delGo", "diffSave"]:
        assert "data-route" not in elements[name]
    assert elements["fileSharedHint"]["data-i18n"] == "files.shared_hint"
    assert "hidden" in elements["fileReload"]
    assert sum("data-close-dialog" in attrs for _, attrs in markup.tags) == 2
    css = (STATIC / "css/app.css").read_text()
    assert ".file-notice{margin:8px 16px 0;color:var(--muted)}" in css
