import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const cfg=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const {chromium}=await import(pathToFileURL(process.env.DTW_PLAYWRIGHT_MODULE));
await fs.mkdir(cfg.shots,{recursive:true});
const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2','.png':'image/png'};
const server=http.createServer(async(req,res)=>{
  const pathname=new URL(req.url,'http://localhost').pathname;
  const target=path.join(cfg.static,pathname==='/'?'index.html':pathname.replace(/^\/static\//,''));
  try{res.writeHead(200,{'Content-Type':mime[path.extname(target)]||'application/octet-stream','Content-Security-Policy':cfg.csp});res.end(await fs.readFile(target));}
  catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1280,height:720},permissions:['clipboard-read','clipboard-write']});
const page=await context.newPage(), errors=[], sessions=new Map(), messages=[], requests=[];
page.on('pageerror',error=>errors.push(error.message));
const topic={tid:'123.7',chat_id:123,topic_id:7,name:'Attachment checks',group:'telegram',count:0,imported_count:0,read_only:false,renamable:true,deletable:true,busy:false,seen:0};
let settings={max_file_bytes:2*1024**3,max_files_per_message:10,chunk_bytes:1024**2,server_max_file_bytes:2*1024**3,server_max_files_per_message:10}, failPut=false;
let emptyHistoryEnabled=false,historyEnabled=false,dropAfterAck=false,holdPut=false,releasePut=null,settingsPuts=0;
const putOffsets=[];
let wsPeer=null,backgroundReady=false;
const incoming='[INCOMING FILE]\nThe user sent you a file via Telegram.\nPath: telegram_files/2026-11-18/photo.png\nType: image/png\nOriginal filename: photo.png\n\nCheck tools/media_tools/CLAUDE.md for file handling instructions.\n\nUser message: Please check the photo.';
const longName='project-'+ 'f'.repeat(220)+'report.pdf';
const entries=[{id:'aaaaaaaaaaaa',seq:1,role:'user',source:'telegram',text:incoming,ts:'2026-11-18T10:00:00Z'}, {id:'bbbbbbbbbbbb',seq:2,role:'bot',source:'web',text:`Project specification. <file:/safe/${longName}>`,ts:'2026-11-18T10:01:00Z'}, {id:'cccccccccccc',seq:3,role:'user',source:'web',text:'<file:/outside/'+ 'p'.repeat(320)+'/secret.txt>',ts:'2026-11-18T10:02:00Z'}, {id:'dddddddddddd',seq:4,role:'user',source:'web',text:'Web attachment caption',ts:'2026-11-18T10:03:00Z'}, {id:'eeeeeeeeeeee',seq:5,role:'user',source:'web',text:'',ts:'2026-11-18T10:04:00Z'}, {id:'ffffffffffff',seq:6,role:'event',source:'system',kind:'background',text:'Background <file:/safe/background.txt>',ts:'2026-11-18T10:05:00Z'}];
const cardMeta=(entry,id,name,mime,inline)=>{const base=`/api/topics/123.7/messages/${entry}/attachments/${id}`;return{id,name,mime,size:2048,path:name,inline_allowed:inline,download_url:base,original_url:inline?base+'?inline=1':null,preview_url:inline?base+'/preview':null};};
const response=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await page.routeWebSocket('**/api/ws',peer=>{wsPeer=peer;});
await page.route('**/api/**',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname.slice(4),method=request.method();
  requests.push({p,method});
  if(p==='/i18n')return response(route,{lang:'ko',strings:cfg.strings});
  if(p==='/auth/state')return response(route,{authenticated:true,csrf:'fixture-csrf',bridge_up:true,password_configured:true,totp_configured:true});
  if(p==='/topics')return response(route,{topics:[topic]});
  if(p==='/state')return response(route,{telegram:{state:'up'},outbox:{pending:0},bot:{up:true},web_sessions:1});
  if(p==='/outbox')return response(route,{items:[]});
  if(p==='/commands')return response(route,{commands:[]});
  if(p==='/files')return response(route,{files:[]});
  if(p==='/cli-login/status')return response(route,{providers:{claude:'installed',codex:'installed'},claude_slots:[],running:false});
  if(p==='/masterkey/status')return response(route,{configured:true,state:'locked',remaining_seconds:null});
  if(p==='/attach-settings'){
    if(method==='PUT'){const value=request.postDataJSON();assert(value.max_file_bytes<=settings.server_max_file_bytes);assert(value.max_files_per_message<=settings.server_max_files_per_message);settingsPuts++;settings={...settings,...value};}
    return response(route,settings);
  }
  if(p.endsWith('/seen')&&method==='PUT')return response(route,{seen:request.postDataJSON().seq});
  const attachmentPath=p.match(/^\/topics\/123\.7\/messages\/([^/]+)\/attachments(?:\/([a-f0-9]{32})(?:\/preview)?)?$/);
  if(attachmentPath){
    if(attachmentPath[2])return route.fulfill({contentType:attachmentPath[1]==='bbbbbbbbbbbb'?'application/pdf':'image/png',body:await fs.readFile(path.join(cfg.directory,'sample.png'))});
    return response(route,{attachments:attachmentPath[1]==='aaaaaaaaaaaa'?[cardMeta(attachmentPath[1],'a'.repeat(32),'photo.png','image/png',true)]:attachmentPath[1]==='bbbbbbbbbbbb'?[cardMeta(attachmentPath[1],'b'.repeat(32),longName,'application/pdf',false)]:attachmentPath[1]==='dddddddddddd'?[cardMeta(attachmentPath[1],'d'.repeat(32),'web-notes.txt','text/plain',false)]:attachmentPath[1]==='eeeeeeeeeeee'?[cardMeta(attachmentPath[1],'e'.repeat(32),'attachment-only.txt','text/plain',false)]:attachmentPath[1]==='ffffffffffff'&&backgroundReady?[cardMeta(attachmentPath[1],'f'.repeat(32),'background.txt','text/plain',false)]:[]});
  }
  if(p.endsWith('/messages') && method==='GET')return response(route,{entries:emptyHistoryEnabled?Array.from({length:100},(_,i)=>({id:i.toString(16).padStart(12,'0'),seq:i+1,role:'bot',source:'web',text:'Plain message '+i,has_attachments:false,ts:'2026-11-18T10:00:00Z'})):historyEnabled?entries.map(entry=>({...entry,has_attachments:['aaaaaaaaaaaa','bbbbbbbbbbbb','dddddddddddd','eeeeeeeeeeee'].includes(entry.id)||(entry.id==='ffffffffffff'&&backgroundReady)})):[],has_more:false});
  if(p.includes('/uploads')){
    const suffix=p.split('/uploads')[1];
    if(method==='POST' && suffix===''){
      assert.equal(request.headers()['x-dtw-csrf'],'fixture-csrf');
      const metadata=request.postDataJSON(),id=crypto.randomBytes(16).toString('hex');
      const state={...metadata,id,offset:0,state:'uploading',parts:[],puts:0};sessions.set(id,state);
      return response(route,{id,offset:0,state:'uploading'});
    }
    const id=suffix.split('/')[1],state=sessions.get(id);assert(state);
    if(method==='GET')return response(route,{id,offset:state.offset,state:state.state});
    if(method==='DELETE'){sessions.delete(id);return response(route,{ok:true});}
    if(method==='PUT'){
      if(failPut){failPut=false;return response(route,{error:'upload_timeout'},408);}
      assert.equal(request.headers()['content-type'],'application/octet-stream');
      assert.equal(request.headers()['x-dtw-client'],'web');
      assert.equal(request.headers()['x-dtw-csrf'],'fixture-csrf');
      assert.equal(Number(request.headers()['upload-offset']),state.offset);
      const bytes=request.postDataBuffer();assert(bytes.length<=settings.chunk_bytes);putOffsets.push({id,offset:state.offset});
      if(holdPut){holdPut=false;await new Promise(resolve=>{releasePut=resolve;});releasePut=null;}
      state.parts.push(bytes);state.offset+=bytes.length;state.puts++;
      if(dropAfterAck&&state.offset>=2*settings.chunk_bytes){dropAfterAck=false;return route.abort('connectionreset');}
      return response(route,{id,offset:state.offset,state:'uploading'});
    }
    if(suffix.endsWith('/complete')){
      state.bytes=Buffer.concat(state.parts);assert.equal(state.bytes.length,state.size);
      assert.equal(request.postDataJSON().sha256,crypto.createHash('sha256').update(state.bytes).digest('hex'));
      state.state='complete';return response(route,{id,offset:state.offset,state:'complete'});
    }
  }
  if(p.endsWith('/messages') && method==='POST'){
    const data=request.postDataJSON();
    assert(data.attachment_ids.length);assert(data.attachment_ids.every(id=>sessions.get(id).state==='complete'));
    messages.push(data);return response(route,{turn_id:crypto.randomBytes(6).toString('hex')});
  }
  return response(route,{seen:0,keys:[]});
});
const results={screens:[],uploads:[],checks:[]};
async function reset(){await page.goto(origin);await page.waitForSelector('#app:not(.hidden)');await page.waitForFunction(()=>!document.querySelector('#attachBtn').disabled);assert.deepEqual(errors,[]);}
async function addFile(name='sample.png'){await page.locator('#filePick').setInputFiles(path.join(cfg.directory,name));await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length>0);}
async function popup(){await page.locator('[data-options]').first().click();await page.waitForSelector('#imageOptions:not([hidden])');}
async function selectMode(mode){await page.locator(`[data-mode="${mode}"]`).click();await page.waitForFunction(()=>!document.querySelector('[data-apply]').disabled);assert.equal(await page.evaluate(()=>document.activeElement.dataset.mode),mode);}
try{
  emptyHistoryEnabled=true;
  await page.goto(origin+'/?empty-page=1');
  await page.waitForSelector('#app:not(.hidden)');
  await page.waitForFunction(()=>document.querySelectorAll('[data-entry-seq]').length===100);
  await page.waitForTimeout(100);
  results.emptyPage={entries:100,attachmentRequests:requests.filter(item=>/\/messages\/[^/]+\/attachments$/.test(item.p)).length};
  assert.equal(results.emptyPage.attachmentRequests,0);
  results.checks.push('100 attachment-free messages issue zero attachment requests');
  await page.screenshot({path:path.join(cfg.shots,'attachment-free-100.png')});
  emptyHistoryEnabled=false;
  // Keep the requested resize format; unsupported raster formats use PNG.
  await reset();
  const other={};for(const extension of ['gif','bmp'])other[extension]=(await fs.readFile(path.join(cfg.directory,'sample.'+extension))).toString('base64');
  results.formatMatrix=await page.evaluate(async other=>{
    const {processImage}=await import('/static/js/attachments.js'),output=[];
    const canvas=document.createElement('canvas');canvas.width=48;canvas.height=36;canvas.getContext('2d').fillRect(0,0,48,36);
    const files=[];
    for(const type of ['image/jpeg','image/png','image/webp']){const blob=await new Promise(resolve=>canvas.toBlob(resolve,type));files.push(new File([blob],'image.bin',{type}));}
    for(const extension of ['gif','bmp'])files.push(new File([Uint8Array.from(atob(other[extension]),c=>c.charCodeAt(0))],'image.'+extension,{type:'image/'+extension}));
    for(const file of files){const original=await processImage(file,{mode:'original'}),converted=await processImage(file,{mode:'resize',edge:24,quality:80}),bitmap=await createImageBitmap(converted);output.push({source:file.type,type:converted.type,width:bitmap.width,height:bitmap.height,originalSame:original===file});bitmap.close();}
    const big=new File([new Uint8Array(32*1024*1024+1)],'big.png',{type:'image/png'});
    let code=null;try{await processImage(big,{mode:'resize',edge:1280,quality:80});}catch(error){code=error.attachmentKey;}
    if(code!=='attach.processing_limit'||await processImage(big,{mode:'original'})!==big)throw Error('processing cap');
    return output;
  },other);
  for(const item of results.formatMatrix){assert.equal(item.width,24);assert.equal(item.height,18);assert(item.originalSame);assert.equal(item.type,['image/jpeg','image/png','image/webp'].includes(item.source)?item.source:'image/png');}
  // Clipboard image and ordinary file, plus a real DataTransfer drop.
  await page.evaluate(async png=>{
    const blob=new Blob([Uint8Array.from(atob(png),c=>c.charCodeAt(0))],{type:'image/png'});
    await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
  },(await fs.readFile(path.join(cfg.directory,'sample.png'))).toString('base64'));
  await page.evaluate(()=>{window.pasted=[];document.addEventListener('paste',event=>{for(const file of event.clipboardData.files){window.pasted.push((async()=>({name:file.name,hash:[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(byte=>byte.toString(16).padStart(2,'0')).join('')}))());}},true);});
  await page.locator('#msgIn').focus();await page.keyboard.press('Control+V');
  await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===1);
  await page.evaluate(()=>{const data=new DataTransfer();data.items.add(new File(['clipboard file\n'],'notes.txt',{type:'text/plain'}));document.querySelector('#msgIn').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));});
  await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===2);
  await page.evaluate(()=>{const data=new DataTransfer();data.items.add(new File(['dropped file'],'dropped.txt',{type:'text/plain'}));const pane=document.querySelector('[data-view="chat"]');pane.dispatchEvent(new DragEvent('dragenter',{dataTransfer:data,bubbles:true,cancelable:true}));pane.dispatchEvent(new DragEvent('drop',{dataTransfer:data,bubbles:true,cancelable:true}));});
  await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===3);
  assert(await page.locator('#attachmentDrop').isHidden());results.checks.push('native PNG clipboard','file ClipboardEvent','DataTransfer drop');
  const draft=Array(24).fill('Please review the attached screenshot and notes.').join('\n');
  await page.locator('#msgIn').fill(draft);
  for(const [width,height] of [[375,667],[768,960],[1280,720]]){
    await page.setViewportSize({width,height});
    for(const theme of ['light','dark']){
      await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      await page.waitForTimeout(150);
      const geometry=await page.evaluate(()=>{const b=document.querySelector('#sendBtn').getBoundingClientRect(),i=document.querySelector('#msgIn').getBoundingClientRect();return{overflow:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)-innerWidth,bottom:b.bottom,input:i.height,height:innerHeight,tray:document.querySelector('#attachmentWrap').clientHeight};});
      assert(geometry.overflow<=0,JSON.stringify(geometry));assert(geometry.bottom<=height,JSON.stringify(geometry));assert(geometry.input<=height*.4+1,JSON.stringify(geometry));
      const name=`4a-${width}-${theme}.png`;await page.screenshot({path:path.join(cfg.shots,name)});results.screens.push({name,width,height,theme,...geometry});
    }
  }
  await page.setViewportSize({width:390,height:844});
  results.extraViewport=await page.evaluate(()=>({width:innerWidth,height:innerHeight,overflow:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)-innerWidth,bottom:document.querySelector('#sendBtn').getBoundingClientRect().bottom}));
  assert.equal(results.extraViewport.overflow,0);assert(results.extraViewport.bottom<=844);
  await page.locator('[data-remove]').last().click();assert.equal(await page.locator('#msgIn').inputValue(),draft);results.checks.push('remove preserves draft');
  await popup();await selectMode('compress');await page.locator('[data-cancel]').click();assert((await page.locator('.attach-chip').first().innerText()).includes(cfg.strings['attach.original']));results.checks.push('cancel preserves mode');
  await page.evaluate(()=>{const data=new DataTransfer();data.items.add(new File(['dropped file'],'dropped-again.txt',{type:'text/plain'}));document.querySelector('[data-view="chat"]').dispatchEvent(new DragEvent('drop',{dataTransfer:data,bubbles:true,cancelable:true}));});
  await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===3);
  const pasted=await page.evaluate(()=>Promise.all(window.pasted));
  await page.locator('#sendBtn').click();await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===0);
  const first=messages.at(-1);assert.equal(first.text,draft);assert.equal(first.attachment_ids.length,3);
  const firstFiles=first.attachment_ids.map(id=>sessions.get(id));
  for(const file of pasted){const stored=firstFiles.find(value=>value.name===file.name);assert(stored);assert.equal(crypto.createHash('sha256').update(stored.bytes).digest('hex'),file.hash);}
  assert.equal(firstFiles.find(file=>file.name==='dropped-again.txt').bytes.toString(),'dropped file');
  results.checks.push('clipboard and dropped bytes preserved through upload');
  // Every selected representation goes through binary PUT and verified SHA completion.
  for(const mode of ['original','resize','compress','resize-compress']){
    await reset();await addFile(mode==='resize'?'sample.jpg':'sample.png');
    if(mode!=='original'){
      await popup();await selectMode(mode);
      if(['resize','resize-compress'].includes(mode))await page.locator('[data-edge]').selectOption('1280');
      if(['compress','resize-compress'].includes(mode)){
        await page.locator('[data-quality]').focus();
        await page.locator('[data-quality]').evaluate(node=>{node.value='55';node.dispatchEvent(new Event('input',{bubbles:true}));});
      }
      await page.waitForFunction(()=>!document.querySelector('[data-apply]').disabled);
      await page.locator('[data-apply]').click();await page.waitForSelector('#imageOptions[hidden]',{state:'attached'});
    }
    const before=messages.length;
    await page.locator('#sendBtn').click();await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===0);
    assert.equal(messages.length,before+1);assert.equal(messages.at(-1).text,'');
    const state=sessions.get(messages.at(-1).attachment_ids[0]);
    const filename=`uploaded-${mode}.${state.mime.split('/')[1]}`;await fs.writeFile(path.join(cfg.directory,filename),state.bytes);
    if(mode==='original')assert.deepEqual(state.bytes,await fs.readFile(path.join(cfg.directory,'sample.png')));
    if(mode==='resize')assert.equal(state.mime,'image/jpeg');
    if(mode.includes('compress'))assert.equal(state.mime,'image/webp');
    results.uploads.push({mode,name:state.name,size:state.size,mime:state.mime,puts:state.puts,filename});
  }
  // Caps come from the API and prevent selecting files or starting uploads.
  settings={...settings,max_files_per_message:1};await reset();
  await page.locator('#filePick').setInputFiles([path.join(cfg.directory,'notes.txt'),path.join(cfg.directory,'sample.png')]);
  await page.waitForSelector('#toasts [role="alert"]');assert.equal(await page.locator('.attach-draft').count(),0);
  settings={...settings,max_file_bytes:5};await reset();await page.locator('#filePick').setInputFiles(path.join(cfg.directory,'notes.txt'));await page.waitForSelector('#toasts [role="alert"]');assert.equal(await page.locator('.attach-draft').count(),0);results.checks.push('count cap','size cap');
  settings={...settings,max_file_bytes:2*1024**3,max_files_per_message:10};await reset();await addFile();
  await page.locator('#msgIn').fill('Draft after transfer failure');failPut=true;await page.locator('#sendBtn').click();await page.waitForSelector('#toasts [role="alert"]');assert.equal(await page.locator('.attach-draft').count(),1);assert.equal(await page.locator('#msgIn').inputValue(),'Draft after transfer failure');results.checks.push('transfer failure preserves draft');
  if(cfg.scenario==='4b'){
    await reset();await addFile();await page.locator('#msgIn').fill('Resume upload check');dropAfterAck=true;
    await page.locator('#sendBtn').click();await page.waitForSelector('[data-upload-state="failed"]');
    const failed=[...sessions.values()].at(-1);assert(failed.offset>=2*settings.chunk_bytes);
    const accepted=failed.offset;await page.locator('[data-resume]').click();await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===0);
    assert.equal(messages.at(-1).text,'Resume upload check');
    const offsets=putOffsets.filter(value=>value.id===failed.id);assert.equal(offsets[2].offset,accepted);assert.equal(failed.bytes.length,failed.size);
    results.checks.push('lost chunk acknowledgement resumes at server offset and SHA matches');
    await reset();await addFile();await page.locator('#msgIn').fill('Draft after cancellation');holdPut=true;
    await page.locator('#sendBtn').click();
    for(let i=0;!releasePut&&i<300;i++)await new Promise(resolve=>setTimeout(resolve,10));assert(releasePut);
    for(const [width,height] of [[375,667],[768,960],[1280,720]]){await page.setViewportSize({width,height});for(const theme of ['light','dark']){await page.evaluate(value=>document.documentElement.dataset.theme=value,theme);const metrics=await page.evaluate(()=>({overflow:Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)-innerWidth,bottom:document.querySelector('#sendBtn').getBoundingClientRect().bottom}));assert(metrics.overflow<=0);assert(metrics.bottom<=height);await page.screenshot({path:path.join(cfg.shots,`4b-progress-${width}-${theme}.png`)});}}
    await page.locator('[data-cancel-upload]:visible').click();await page.waitForSelector('[data-upload-state="cancelled"]');
    assert.equal(await page.locator('#msgIn').inputValue(),'Draft after cancellation');
    const cancelled=[...sessions.values()].at(-1);releasePut();await page.locator('[data-remove]').click();await page.waitForFunction(()=>document.querySelectorAll('.attach-draft').length===0);
    for(let i=0;sessions.has(cancelled.id)&&i<100;i++)await new Promise(resolve=>setTimeout(resolve,10));assert(!sessions.has(cancelled.id));results.checks.push('cancel retains draft; remove cancels unfinished session');
    await reset();await page.locator('#accountButton').click();await page.locator('#openAttachSettings').click();await page.waitForFunction(()=>!document.querySelector('#attachmentMaxSize').disabled);
    const previous=settingsPuts;await page.locator('#attachmentMaxSize').fill('2049');await page.locator('#attachmentMaxCount').fill('11');await page.locator('#saveAttachmentLimits').click();
    assert.equal(settingsPuts,previous);assert.equal(await page.locator('#attachmentMaxSize').getAttribute('aria-invalid'),'true');
    await page.locator('#attachmentMaxSize').fill('1');await page.locator('#attachmentMaxCount').fill('2');await page.locator('#saveAttachmentLimits').click();await page.waitForFunction(expected=>document.querySelector('#attachmentLimitsNotice').textContent===expected,cfg.strings['attach.limits_saved']);
    assert.equal(settings.max_file_bytes,1024**2);assert.equal(settings.max_files_per_message,2);await page.keyboard.press('Escape');await page.waitForFunction(()=>document.activeElement.id==='accountButton');
    await page.locator('#filePick').setInputFiles(path.join(cfg.directory,'sample.png'));await page.waitForSelector('#toasts [role="alert"]');assert.equal(await page.locator('.attach-draft').count(),0);
    await page.locator('#accountButton').click();await page.locator('#openAttachSettings').click();await page.waitForFunction(()=>!document.querySelector('#attachmentMaxSize').disabled);assert.equal(await page.locator('#attachmentMaxSize').inputValue(),'1');
    await page.locator('#attachmentMaxSize').fill('2048');await page.locator('#attachmentMaxCount').fill('10');await page.locator('#saveAttachmentLimits').click();await page.waitForFunction(expected=>document.querySelector('#attachmentLimitsNotice').textContent===expected,cfg.strings['attach.limits_saved']);await page.keyboard.press('Escape');
    results.checks.push('settings persist; maximum rejected before PUT; next selection uses new cap');
    historyEnabled=true;await reset();await page.waitForSelector('.message-attachment');await page.waitForFunction(()=>document.querySelectorAll('.message-attachment').length===4);
    backgroundReady=true;wsPeer.send(JSON.stringify({type:'attachments_changed',tid:'123.7',entry_id:'ffffffffffff'}));
    await page.waitForFunction(()=>document.querySelectorAll('.message-attachment').length===5);
    assert.equal(await page.locator('.attachment-filename').allTextContents().then(value=>value.sort()).then(value=>value.join(',')),['attachment-only.txt','photo.png',longName,'web-notes.txt','background.txt'].sort().join(','));
    assert(!await page.locator('#thread').innerText().then(value=>value.includes('[INCOMING FILE]')));assert(await page.locator('#thread').innerText().then(value=>value.includes('Please check the photo.')));
    assert.equal(await page.locator('.message-attachment').filter({hasText:'secret.txt'}).count(),0);assert(await page.locator('#thread').innerText().then(value=>value.includes('Web attachment caption')));
    assert(!await page.locator('#thread').innerText().then(value=>value.includes('<file:/safe/background.txt>')));
    for(const [width,height] of [[375,667],[768,960],[1280,720]]){
      await page.setViewportSize({width,height});
      for(const theme of ['light','dark']){
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await page.waitForTimeout(100);
        assert(await page.evaluate(()=>Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)<=innerWidth));
        await page.waitForFunction(()=>innerWidth>1180||getComputedStyle(document.querySelector('.info')).visibility==='hidden');
        const bounds=await page.evaluate(()=>({viewport:innerWidth,bodyClass:document.body.className,info:{visibility:getComputedStyle(document.querySelector('.info')).visibility,left:document.querySelector('.info').getBoundingClientRect().left},elements:[...document.querySelectorAll('#thread .bubble,#thread .message-attachment,#thread .attachment-download')].map(node=>({kind:node.className,left:node.getBoundingClientRect().left,right:node.getBoundingClientRect().right,width:node.getBoundingClientRect().width,scroll:node.scrollWidth,containerRight:(node.closest('.bubble')||node).getBoundingClientRect().right}))}));
        results.layouts??=[];results.layouts.push({width,theme,...bounds});
        assert(bounds.elements.every(node=>node.left>=0&&node.right<=width+0.5&&node.right<=node.containerRight+0.5&&node.scroll<=Math.ceil(node.width)),JSON.stringify(bounds));
        await page.locator('.message-attachment').filter({hasText:'photo.png'}).evaluate(node=>node.scrollIntoView({block:'start'}));
        await page.screenshot({path:path.join(cfg.shots,`4b-photo-${width}-${theme}.png`)});
        await page.locator('.message-attachment').filter({hasText:longName}).evaluate(node=>node.scrollIntoView({block:'center'}));
        await page.screenshot({path:path.join(cfg.shots,`4b-cards-${width}-${theme}.png`)});
        const trigger=page.locator('[data-open-attachment]').first();await trigger.click();await page.waitForSelector('#attachmentViewer[open]');
        await page.locator('#attachmentZoom').click();assert.equal(await page.locator('#attachmentZoom').getAttribute('aria-pressed'),'true');await page.locator('#attachmentZoom').click();
        await page.screenshot({path:path.join(cfg.shots,`4b-viewer-${width}-${theme}.png`)});
        await page.keyboard.press('Escape');await page.waitForFunction(()=>document.activeElement.hasAttribute('data-open-attachment'));
        await page.locator('#accountButton').click();await page.locator('#openAttachSettings').click();await page.waitForFunction(()=>!document.querySelector('#attachmentMaxSize').disabled);await page.screenshot({path:path.join(cfg.shots,`4b-settings-${width}-${theme}.png`)});await page.keyboard.press('Escape');
      }
    }
    results.checks.push('Web text-only history and attachment-only message cards; Telegram photo and bot file cards; outside user path has no card; native overlay focus/Escape/zoom');
  }
  assert.deepEqual(errors,[]);console.log(JSON.stringify(results));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
