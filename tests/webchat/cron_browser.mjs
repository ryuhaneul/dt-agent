// Run with Atlas enabled, DTW_PLAYWRIGHT_MODULE and DTW_PYTHON set.
// All listeners, credentials and cron data are disposable and local to /tmp.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { createHmac } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const { chromium } = await import(process.env.DTW_PLAYWRIGHT_MODULE || 'playwright');
const home = await fs.mkdtemp(path.join(os.tmpdir(), 'dtw-cron-browser-'));
const output = path.resolve(process.env.DTW_CRON_OUTPUT || path.join(home, 'captures'));
await fs.mkdir(output, { recursive: true });
const password = 'isolated-browser-password';
execFileSync('openssl', ['req','-x509','-newkey','rsa:2048','-nodes','-keyout',path.join(home,'key.pem'),'-out',path.join(home,'cert.pem'),'-days','1','-subj','/CN=localhost'], {stdio:'ignore'});
const serverCode = String.raw`
import asyncio, json, os, ssl, sys
from pathlib import Path
from aiohttp import web
from ductor_bot.config import AgentConfig, ChatLogConfig, WebchatConfig
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.chat_log.runtime import ChatLogRuntime
from ductor_bot.session.key import SessionKey
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.web.app import create_app
from ductor_bot.webchat.auth import PASSWORD_KEY, hash_password
from ductor_bot.infra.secret_store import set_secret
async def main():
 home=Path(os.environ['DUCTOR_HOME'])
 config=AgentConfig(ductor_home=str(home),telegram_token='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',allowed_user_ids=[123],user_timezone='Asia/Seoul',language='ko',chat_log=ChatLogConfig(enabled=True),webchat=WebchatConfig(enabled=True,domain='chat.example.test',allowed_ips=['127.0.0.1/32'],acme_email='test@example.com'))
 bot=TelegramBot(config)
 bot._orchestrator=Orchestrator(config,DuctorPaths(ductor_home=home),lock_pool=bot._lock_pool)
 await bot._orch._sessions.resolve_session(SessionKey.telegram(-123,7))
 bot._topic_names.set(-123,7,'Browser topic')
 runtime=ChatLogRuntime(bot)
 set_secret(home/'secrets.env',PASSWORD_KEY,hash_password(os.environ['DTW_TEST_PASSWORD']))
 bridge=web.AppRunner(create_bridge_app(bot,runtime,'isolated-bridge-token'),access_log=None)
 await bridge.setup()
 bridge_site=web.TCPSite(bridge,'127.0.0.1',0)
 await bridge_site.start()
 port=bridge_site._server.sockets[0].getsockname()[1]
 # Reserve the public port before constructing the origin checked by middleware.
 import socket
 sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);sock.setblocking(False)
 origin='https://127.0.0.1:'+str(sock.getsockname()[1])
 app=web.AppRunner(create_app(bridge_url='http://127.0.0.1:'+str(port),bridge_token='isolated-bridge-token',public_origin=origin),access_log=None)
 await app.setup()
 tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(home/'cert.pem',home/'key.pem')
 await web.SockSite(app,sock,ssl_context=tls).start()
 print(json.dumps({'origin':origin}),flush=True)
 try:
  await asyncio.to_thread(sys.stdin.read)
 finally:
  await app.cleanup();await bridge.cleanup();await bot._bot.session.close()
asyncio.run(main())
`;
const child = spawn(process.env.DTW_PYTHON || 'python3', ['-u','-c',serverCode], {
 cwd:root, env:{...process.env,HOME:home,DUCTOR_HOME:home,DUCTOR_SECRETS_FILE:path.join(home,'secrets.env'),DTW_TEST_PASSWORD:password},stdio:['pipe','pipe','pipe'],
});
let diagnostics='';child.stderr.on('data',chunk=>{diagnostics+=chunk;});
const origin = await new Promise((resolve,reject)=>{
 let buffer='';child.stdout.on('data',chunk=>{buffer+=chunk;for(const line of buffer.split('\n')){try{const data=JSON.parse(line);if(data.origin)resolve(data.origin);}catch{}}});
 child.once('exit',code=>reject(new Error(`Server exited ${code}: ${diagnostics}`)));
 setTimeout(()=>reject(new Error(`Server startup timeout: ${diagnostics}`)),30000).unref();
});
function totp(seed) {
 const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';let bits='';
 for(const char of seed.replace(/=+$/,''))bits+=alphabet.indexOf(char).toString(2).padStart(5,'0');
 const bytes=Buffer.from((bits.match(/.{8}/g)||[]).map(value=>parseInt(value,2)));
 const counter=Buffer.alloc(8);counter.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)));
 const digest=createHmac('sha1',bytes).update(counter).digest(),offset=digest[19]&15;
 return String((digest.readUInt32BE(offset)&0x7fffffff)%1000000).padStart(6,'0');
}
let browser;
const results=[];
try {
 browser=await chromium.launch({headless:true,...(process.env.DTW_CHROMIUM?{executablePath:process.env.DTW_CHROMIUM}:{})});
 const auth=await browser.newContext({ignoreHTTPSErrors:true});
 const headers={'Origin':origin,'X-Dtw-Client':'web'};
 async function authPost(step,data){const response=await auth.request.post(origin+'/api/auth/'+step,{headers,data});assert.equal(response.status(),200,await response.text());return response.json();}
 await authPost('password',{password});
 const setup=await authPost('setup/begin',{mode:'new'});
 const seed=setup.secret_display.replace(/ /g,'');
 await authPost('setup/confirm',{code:totp(seed)});
 // Enrollment deliberately does not log in; use a fresh TOTP slot.
 await new Promise(resolve=>setTimeout(resolve,30000-Date.now()%30000+100));
 await authPost('password',{password});await authPost('totp',{code:totp(seed)});
 const storageState=await auth.storageState();await auth.close();
 const jobsFile=path.join(home,'cron_jobs.json'),tasks=path.join(home,'workspace/cron_tasks');
 const definitions=async()=>JSON.parse(await fs.readFile(jobsFile,'utf8')).jobs;
 for(const width of [375,768,1280])for(const theme of ['light','dark']){
  const context=await browser.newContext({ignoreHTTPSErrors:true,storageState,viewport:{width,height:900},deviceScaleFactor:2,colorScheme:theme,timezoneId:'Asia/Seoul'});
  await context.addInitScript(theme=>localStorage.setItem('dtw-theme',theme),theme);
  const page=await context.newPage(),errors=[],responses=[];page.setDefaultTimeout(15000);console.log('open '+width+' '+theme);
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.url().includes('/cron'))responses.push({url:response.url(),status:response.status(),method:response.request().method()});});
  await page.goto(origin+'/#chat');
  await page.waitForFunction(()=>!document.querySelector('#headerCron').disabled).catch(async error=>{await page.screenshot({path:path.join(output,'failure.png')});console.error(await page.locator('body').innerText());console.error(diagnostics);throw error;});
  await page.click('#headerCron');await page.locator('[data-cron-action=create]').click();
  const title=`Browser cron ${width} ${theme}`,body='Read public reports.\nReturn a concise summary.';
  await page.fill('#cronName',title);await page.fill('#cronInstruction',body);
  await page.click('[data-cron-ac=tokens]'); // Blank tokens must persist as 500k.
  await page.locator('#cronForm button[type=submit]').click();await page.waitForSelector('.cron-instruction');
  let job=(await definitions()).find(job=>job.title===title);assert.ok(job);
  const id=job.id,folder=path.join(tasks,id);
  assert.equal(job.autocompact,500000);assert.equal(job.chat_id,-123);assert.equal(job.topic_id,7);assert.equal(job.transport,'tg');
  assert.equal(await fs.readFile(path.join(folder,'TASK_DESCRIPTION.md'),'utf8'),body);
  assert.ok(await page.locator(`[data-cron-id="${id}"]`).count());
  async function checkGeometry(screen){
   const result=await page.evaluate(()=>{
    const dialog=document.querySelector('#cronDlg'),footer=dialog.querySelector('.cron-foot')?.getBoundingClientRect();
    const scrolls=[...dialog.querySelectorAll('.cron-body,.cron-list')].filter(el=>el.getClientRects().length);
    return {overflow:document.documentElement.scrollWidth>innerWidth||dialog.scrollWidth>dialog.clientWidth||scrolls.some(el=>el.scrollWidth>el.clientWidth),footer:!footer||(footer.top>=0&&footer.bottom<=innerHeight)};
   });assert.equal(result.overflow,false,`${width}/${theme}/${screen} overflow`);assert.equal(result.footer,true,`${width}/${theme}/${screen} footer`);
  }
  const prefix=`${width}-${theme}`;
  await checkGeometry('detail');await page.screenshot({path:path.join(output,`${prefix}-detail.png`),fullPage:true});
  async function captureRegions(state){
   const selectors={header:'.cron-head',list:'.cron-list',body:'.cron-detail .cron-body',footer:'.cron-foot'};
   for(const [name,selector]of Object.entries(selectors)){const locator=page.locator(selector).first();if(await locator.isVisible())await locator.screenshot({path:path.join(output,`${prefix}-${state}-${name}.png`)});}
  }
  await captureRegions('detail');
  await page.locator('[data-cron-action=edit]').click();
  await page.click('[data-cron-mode=direct]');await page.fill('#cronExpression','0 9 1 1 *');
  await page.waitForFunction(()=>document.querySelectorAll('#cronPreview li').length===3);
  if (width === 375 && theme === 'light') {
   const nextRuns = page.locator('#cronPreview li');
   const notice = page.locator('#cronError');
   const timezone = page.locator('#cronTimezone');
   const loading = await page.evaluate(async () => (await import('/static/js/i18n.js')).t('cron.loading'));
   async function checkPreview(zone) {
    const response = page.waitForResponse(response => response.url().endsWith('/cron/preview') &&
      response.request().postDataJSON().timezone === zone);
    await timezone.fill(zone);
    const result = await response;
    assert.equal(result.status(), 200);
    const preview = await result.json();
    assert.equal(preview.next_runs.length, 3);
    await page.waitForFunction(text => document.querySelector('#cronPreview').textContent !== text, loading);
    const expected = zone === 'localtime' ? preview.next_runs : await page.evaluate(
      ({ next_runs, timezone }) => next_runs.map(date => new Date(date).toLocaleString(document.documentElement.lang, { timeZone: timezone })),
      preview,
    );
    assert.deepEqual(
      { output: await nextRuns.allTextContents(), errorVisible: await notice.isVisible() },
      { output: expected, errorVisible: false },
      `timezone ${JSON.stringify(zone)}`,
    );
   }
   for (const zone of ['', 'localtime', 'Asia/Tokyo']) await checkPreview(zone);
   await page.route('**/cron/preview', route => route.fulfill({ status: 503, json: { error: 'bot_unreachable' } }), { times: 1 });
   await timezone.fill('Asia/Seoul');
   await notice.waitFor({ state: 'visible' });
   const invalidResponse = page.waitForResponse(response => response.url().endsWith('/cron/preview') &&
     response.request().postDataJSON().timezone === 'Invalid/Timezone');
   await timezone.fill('Invalid/Timezone');
   assert.equal((await invalidResponse).status(), 400);
   await page.waitForFunction(text => {
    const preview = document.querySelector('#cronPreview');
    return preview.classList.contains('cron-invalid') && preview.textContent !== text;
   }, loading);
   const noticeAfterInvalid = await notice.isVisible();
   await checkPreview('Asia/Tokyo');
   assert.equal(noticeAfterInvalid, false, 'a 400 preview response clears the connection error');
   await timezone.fill('Asia/Seoul');
  }
  const updatedBody='Updated instruction.\nUse load_secrets("BROWSER_TEST_TOKEN") when needed.';
  await page.fill('#cronInstruction',updatedBody);await page.fill('#cronTokens','600k');
  await page.locator('.cron-detail .cron-body').evaluate(el=>el.scrollTop=0);
  await checkGeometry('edit');await page.screenshot({path:path.join(output,`${prefix}-edit.png`),fullPage:true});await captureRegions('edit');
  await page.locator('#cronForm button[type=submit]').click();await page.waitForSelector('.cron-instruction');
  job=(await definitions()).find(job=>job.id===id);assert.equal(job.schedule,'0 9 1 1 *');assert.equal(job.autocompact,600000);
  assert.equal(await fs.readFile(path.join(folder,'TASK_DESCRIPTION.md'),'utf8'),updatedBody);
  await page.locator('.cron-warning').filter({hasText:'BROWSER_TEST_TOKEN'}).waitFor();
  if(width<=768)await page.click('[data-cron-action=list]');
  await checkGeometry('list');await page.screenshot({path:path.join(output,`${prefix}-list.png`),fullPage:true});
  const toggle=page.locator(`[data-cron-toggle="${id}"]`);
  await toggle.click();await page.waitForFunction(id=>document.querySelector(`[data-cron-toggle="${id}"]`)?.getAttribute('aria-checked')==='false',id);
  assert.equal((await definitions()).find(job=>job.id===id).enabled,false);
  await toggle.click();await page.waitForFunction(id=>document.querySelector(`[data-cron-toggle="${id}"]`)?.getAttribute('aria-checked')==='true',id);
  assert.equal((await definitions()).find(job=>job.id===id).enabled,true);
  const registry=JSON.parse(await fs.readFile(jobsFile,'utf8'));
  registry.jobs.find(job=>job.id===id).schedule='every day';
  await fs.writeFile(jobsFile,JSON.stringify(registry));
  await page.click('#cronClose');await page.click('#headerCron');
  const card=page.locator('.cron-job').filter({has:page.locator(`[data-cron-id="${id}"]`)});
  await card.locator('.cron-invalid').waitFor();
  await toggle.click();await page.waitForFunction(id=>document.querySelector(`[data-cron-toggle="${id}"]`)?.getAttribute('aria-checked')==='false',id);
  assert.equal((await definitions()).find(job=>job.id===id).enabled,false);
  await page.locator(`[data-cron-id="${id}"]`).click();await page.waitForSelector('.cron-instruction');
  await page.locator('.cron-detail .cron-invalid').waitFor();
  await page.click('[data-cron-action=delete]');await checkGeometry('delete');await page.screenshot({path:path.join(output,`${prefix}-delete.png`),fullPage:true});
  await page.click('[data-cron-action=confirm-delete]');await page.waitForSelector('.cron-empty');
  assert.equal((await definitions()).some(job=>job.id===id),false);
  await assert.rejects(fs.stat(folder),{code:'ENOENT'});
  const archived=(await fs.readdir(path.join(tasks,'.archive'))).find(name=>name.startsWith(id+'-'));assert.ok(archived);
  assert.equal(await fs.readFile(path.join(tasks,'.archive',archived,'TASK_DESCRIPTION.md'),'utf8'),updatedBody);
  const orphanTitle='Cron with missing task folder';
  await page.click('[data-cron-action=create]');
  await page.fill('#cronName',orphanTitle);await page.fill('#cronInstruction',body);
  await page.locator('#cronForm button[type=submit]').click();await page.waitForSelector('.cron-instruction');
  const orphan=(await definitions()).find(job=>job.title===orphanTitle);
  assert.ok(orphan);
  await fs.rm(path.join(tasks,orphan.id),{recursive:true});
  await page.click('#cronClose');
  const detail404=page.waitForResponse(response=>response.url().endsWith('/cron/'+orphan.id)&&response.request().method()==='GET'&&response.status()===404);
  await page.click('#headerCron');
  if(width<=768)await page.click(`[data-cron-id="${orphan.id}"]`);
  await detail404;
  await page.waitForSelector('#cronDlg[aria-busy=false]');
  assert.equal(await page.locator('[data-cron-action=delete]').isVisible(),true,'Missing-folder cron can be deleted from its detail');
  assert.equal(await page.locator('[data-cron-action=edit]').count(),0);
  await checkGeometry('missing-folder');await page.screenshot({path:path.join(output,`${prefix}-missing-folder.png`),fullPage:true});
  await page.click('[data-cron-action=delete]');
  const orphanDeleted=page.waitForResponse(response=>response.url().endsWith('/cron/'+orphan.id)&&response.request().method()==='DELETE');
  await page.click('[data-cron-action=confirm-delete]');
  assert.equal((await orphanDeleted).status(),200);
  await page.waitForSelector('.cron-empty');
  assert.equal((await definitions()).some(job=>job.id===orphan.id),false);
  assert.equal((await fs.readdir(path.join(tasks,'.archive'))).some(name=>name.startsWith(orphan.id+'-')),false);
  assert.deepEqual(errors,[]);assert.ok(responses.some(item=>item.method==='POST'&&item.status===201));assert.ok(responses.some(item=>item.method==='PUT'&&item.status===200));assert.ok(responses.some(item=>item.method==='DELETE'&&item.status===200));
  results.push({width,theme,scale:2,pageerror:0,overflow:false,footerAccessible:true,id,archived,orphanRemoved:orphan.id,requests:responses});
  console.log(JSON.stringify({width,theme,pageerror:0,overflow:false,crud:'passed',archive:'verified',missingFolderDelete:'passed'}));
  await context.close();
 }

 const desktop = await browser.newContext({
  ignoreHTTPSErrors: true, storageState, viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2, colorScheme: 'light',
 });
 await desktop.addInitScript(() => localStorage.setItem('dtw-theme', 'light'));
 const largePage = await desktop.newPage();
 const largeErrors = [];
 largePage.on('pageerror', error => largeErrors.push(error.message));
 await largePage.goto(origin + '/#chat');
 await largePage.click('#headerCron');
 await largePage.locator('[data-cron-action=create]').click();
 const initialTitle = 'Large instruction fixture';
 const openingLine = 'Read the public weather report.';
 await largePage.fill('#cronName', initialTitle);
 await largePage.fill('#cronInstruction', openingLine);
 await largePage.locator('#cronForm button[type=submit]').click();
 await largePage.locator('.cron-instruction').waitFor();
 const [largeJob] = await definitions();
 assert.equal(largeJob.title, initialTitle);
 const description = path.join(tasks, largeJob.id, 'TASK_DESCRIPTION.md');
 const readOnly = `${openingLine}\n# ${'Public weather notes. '.repeat(50000)}`.slice(0, 1024 ** 2 + 1);
 assert.equal(Buffer.byteLength(readOnly), 1024 ** 2 + 1);
 await fs.writeFile(description, readOnly);
 await largePage.click('#cronClose');
 await largePage.click('#headerCron'); // Desktop selects the first cron automatically.

 const readOnlyText = await largePage.evaluate(async () => (
  (await import('/static/js/i18n.js')).t('cron.readonly_notice', { size: '1.0 MiB', limit: '1.0 MiB' })
 ));
 assert.ok(readOnlyText);
 const readOnlyNotice = largePage.locator('.cron-detail .cron-limit').filter({ hasText: readOnlyText });
 await readOnlyNotice.waitFor();
 assert.equal(await largePage.locator('.cron-detail-title h3').textContent(), initialTitle);
 assert.equal(await largePage.locator('#cronError').isVisible(), false);
 assert.equal(await largePage.locator('.cron-instruction').evaluate(el => el.textContent.length), readOnly.length);
 assert.equal(await largePage.locator('.cron-instruction').evaluate(el => el.textContent.split('\n')[0]), openingLine);
 await largePage.screenshot({ path: path.join(output, 'large-cron-1440-readonly.png') });

 await largePage.click('[data-cron-action=edit]');
 await readOnlyNotice.waitFor();
 assert.equal(await largePage.locator('#cronInstruction').count(), 0);
 const updatedTitle = 'Large instruction renamed';
 await largePage.fill('#cronName', updatedTitle);
 const saved = largePage.waitForResponse(response => response.url().endsWith('/cron/' + largeJob.id) &&
  response.request().method() === 'PUT');
 await largePage.locator('#cronForm button[type=submit]').click();
 const savedResponse = await saved;
 assert.equal(savedResponse.status(), 200);
 const savedPayload = savedResponse.request().postDataJSON();
 assert.equal(savedPayload.title, updatedTitle);
 assert.equal(Object.hasOwn(savedPayload, 'content'), false);
 assert.equal(Object.hasOwn(savedPayload, 'base_sha256'), false);
 await largePage.locator('.cron-detail-title h3').filter({ hasText: updatedTitle }).waitFor();
 assert.equal((await definitions())[0].title, updatedTitle);
 assert.equal(await fs.readFile(description, 'utf8'), readOnly);

 const hiddenSize = 8 * 1024 ** 2 + 1;
 await fs.truncate(description, hiddenSize);
 await largePage.click('#cronClose');
 await largePage.click('#headerCron');
 const hiddenText = await largePage.evaluate(async () => (
  (await import('/static/js/i18n.js')).t('cron.hidden_notice', { size: '8.0 MiB' })
 ));
 assert.ok(hiddenText);
 const hiddenNotice = largePage.locator('.cron-detail .cron-limit').filter({ hasText: hiddenText });
 await hiddenNotice.waitFor();
 assert.equal(await largePage.locator('.cron-instruction').count(), 0);
 assert.equal(await largePage.locator('#cronError').isVisible(), false);
 await largePage.screenshot({ path: path.join(output, 'large-cron-1440-hidden.png') });
 await largePage.click('[data-cron-action=edit]');
 await hiddenNotice.waitFor();
 assert.equal(await largePage.locator('#cronInstruction').count(), 0);
 assert.deepEqual(largeErrors, []);
 await desktop.close();

 const mobile = await browser.newContext({
  ignoreHTTPSErrors: true, storageState, viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2, colorScheme: 'light',
 });
 await mobile.addInitScript(() => localStorage.setItem('dtw-theme', 'light'));
 const mobilePage = await mobile.newPage();
 await mobilePage.goto(origin + '/#chat');
 await mobilePage.click('#headerCron');
 await mobilePage.locator(`[data-cron-id="${largeJob.id}"]`).click();
 await mobilePage.locator('.cron-detail .cron-limit').filter({ hasText: hiddenText }).waitFor();
 assert.equal(await mobilePage.locator('#cronError').isVisible(), false);
 await mobilePage.screenshot({ path: path.join(output, 'large-cron-390-hidden.png') });
 await mobile.close();
 const largeInstruction = { desktop: [1440, 900], mobile: [390, 844], readOnly: 'passed', hidden: 'passed', metadataSave: 'passed' };
 console.log(JSON.stringify(largeInstruction));
 const reference=process.env.DTW_CRON_REFERENCE;
 if(reference){
  const comparisons=[];
  for(const width of [375,768,1280])for(const theme of ['light','dark']){
   const context=await browser.newContext({viewport:{width,height:900},deviceScaleFactor:2,colorScheme:theme});
   const page=await context.newPage();await page.goto(pathToFileURL(reference).href);
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);if(await page.locator('#veil').evaluate(el=>el.hidden))await page.click('#open');await page.evaluate(()=>document.fonts.ready);
   const prefix=`${width}-${theme}`;
   for(const state of ['detail','edit']){
    if(state==='edit')await page.locator('[data-edit]').first().click();
    await page.screenshot({path:path.join(output,`${prefix}-reference-${state}.png`),fullPage:true});
    const regions={header:'.modalhead',list:'.split>.scroll',body:'.detail .body',footer:'.foot'};
    for(const [name,selector]of Object.entries(regions)){
     const locator=page.locator(selector).first();if(!await locator.isVisible())continue;
     const file=`${prefix}-reference-${state}-${name}.png`;
     await locator.screenshot({path:path.join(output,file)});
     const current=`${prefix}-${state}-${name}.png`;
     try{await fs.access(path.join(output,current));comparisons.push({width,theme,state,region:name,current,reference:file});}catch{}
    }
   }
   await context.close();
  }
  const rows=comparisons.map(item=>`<section><h2>${item.width} ${item.theme} ${item.state} ${item.region}</h2><div><figure><figcaption>Approved A</figcaption><img src="${item.reference}"></figure><figure><figcaption>Current</figcaption><img src="${item.current}"></figure></div></section>`).join('');
  await fs.writeFile(path.join(output,'comparison.html'),`<!doctype html><meta charset="utf-8"><title>Cron region comparisons</title><style>body{font-family:sans-serif}section>div{display:flex;gap:16px}figure{margin:0;max-width:48%}img{width:100%;height:auto;border:1px solid #aaa}</style>${rows}`);
 }
 await fs.writeFile(path.join(output,'results.json'),JSON.stringify({home,results,largeInstruction},null,2));
} catch(error) {console.error(error);throw error;} finally {
 if(browser)await browser.close();
 child.stdin.end();await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(()=>child.kill('SIGTERM'),5000).unref();});
 await fs.writeFile(path.join(output,'server.log'),diagnostics);
 // Retain the isolated home for evidence; never modify an existing installation.
 console.log(JSON.stringify({home,output}));
}
