import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
const {chromium} = await import(process.env.DTW_PLAYWRIGHT_MODULE);
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));
const browser = await chromium.launch({headless:true});
const samples = [], failures = [], violations = [];
const koreanParagraph=String.fromCodePoint(0xbb38,0xb2e8),koreanEmphasis=String.fromCodePoint(0xac15,0xc870);
const percentile = values => [...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];
const mime = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.woff2':'font/woff2','.svg':'image/svg+xml'};
async function serve(root) {
  const base = path.join(root,'ductor_bot/webchat/web/static');
  const server = http.createServer((request,response)=>{
    const relative = new URL(request.url,'http://localhost').pathname;
    const filename = relative === '/' ? path.join(base,'index.html') : path.join(base,relative.replace(/^\/static\//,''));
    if(!filename.startsWith(base+path.sep)||!fs.existsSync(filename)){response.writeHead(404);response.end();return;}
    response.writeHead(200,{'Content-Type':mime[path.extname(filename)]||'application/octet-stream','Content-Security-Policy':relative.startsWith('/static/starflow/')?cfg.starflowCsp:cfg.csp,'Cache-Control':'no-store'});
    response.end(fs.readFileSync(filename));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {server,origin:`http://127.0.0.1:${server.address().port}`};
}
const servers = {current:await serve(cfg.root),baseline:await serve(cfg.baseline)};
async function open(kind,width,entries,secondEntries=entries) {
  const context=await browser.newContext({viewport:{width,height:900},reducedMotion:'reduce'});
  await context.addInitScript(()=>{
    localStorage.setItem('dtw-theme','light');
    window.__violations=[];document.addEventListener('securitypolicyviolation',event=>window.__violations.push(event.violatedDirective+':'+event.blockedURI));
    window.__rafSamples=[];window.__frameCounts={};window.__measuring=false;
    const native=requestAnimationFrame;
    window.requestAnimationFrame=callback=>native.call(window,timestamp=>{
      const measuring=window.__measuring&&callback.toString().includes('streamUpdates'),start=performance.now();callback(timestamp);
      if(measuring){window.__rafSamples.push(performance.now()-start);window.__frameCounts[timestamp]=(window.__frameCounts[timestamp]||0)+1;}
    });
  });
  if(cfg.mode==='navigation')await context.addInitScript(()=>{
    const idle=requestIdleCallback,cancel=cancelIdleCallback;let serial=0;
    window.__holdHistory=false;window.__historyJobs=new Map();
    window.requestIdleCallback=callback=>{
      if(!window.__holdHistory)return idle(callback);
      window.__historyJobs.set(--serial,callback);return serial;
    };
    window.cancelIdleCallback=id=>id<0?window.__historyJobs.delete(id):cancel(id);
  });
  const page=await context.newPage();
  page.on('pageerror',error=>failures.push(error.message));
  let socket;
  await page.routeWebSocket('**/api/ws',peer=>{socket=peer;});
  const topic={tid:'1.2',chat_id:1,topic_id:2,name:'Markdown fixture',group:'telegram',count:entries.length,seen:entries.length,renamable:true,deletable:true,read_only:false,preview:'',last_ts:null,created_at:null,imported_count:0,busy:false};
  const secondTopic={...topic,tid:'1.3',topic_id:3,name:'Second topic',seen:secondEntries.length};
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url());let result;
    const tid=url.pathname.split('/')[3], topicEntries=tid==='1.3'?secondEntries:entries;
    if(url.pathname==='/api/i18n')result={lang:'en',strings:cfg.strings};
    else if(url.pathname==='/api/auth/state')result={authenticated:true,csrf:'fake-csrf',bridge_up:true};
    else if(url.pathname==='/api/commands')result={commands:[]};
    else if(url.pathname==='/api/topics')result={topics:[topic,{...secondTopic,count:secondEntries.length},...(cfg.mode==='navigation'?[{...topic,tid:'1.4',topic_id:4,name:'Third topic'}]:[])]};
    else if(url.pathname==='/api/state')result={telegram:{state:'up',strip:Array(30).fill('ok')},outbox:{pending:0},bot:{up:true},web_sessions:1};
    else if(url.pathname==='/api/outbox')result={items:[]};
    else if(url.pathname.endsWith('/messages')){
      const before=Number(url.searchParams.get('before')||Infinity),rows=topicEntries.filter(entry=>entry.seq<before);
      result={entries:rows.slice(-100).map(entry=>({...entry,text:`Topic ${tid}\n\n${entry.text}`})),has_more:rows.length>100};
    }else if(url.pathname.endsWith('/seen'))result={seen:topicEntries.at(-1)?.seq||0};
    else if(url.pathname==='/api/cli-login/status')result={providers:{},claude_slots:[],running:false};
    else if(url.pathname==='/api/masterkey/status')result={state:'unconfigured',ttl_seconds:0};
    else if(url.pathname==='/api/files')result={files:[{name:'PERSONA.md'}]};
    else if(url.pathname==='/api/files/PERSONA.md' && cfg.mode==='preview-overflow')result={
      name:'PERSONA.md',content:'A paragraph in a long file.\n\n'.repeat(300)+'    file preview code\n\nCitation[^ref].\n\n[^ref]: Example source.',
      sha256:'fake-sha',
    };
    else if(url.pathname==='/api/files/PERSONA.md')result={name:'PERSONA.md',content:'# Preview\n\n**bold** [link](https://example.test/preview)\n\nA[^1]\n\n[^1]: preview note\n\n<script>attack()</script>',sha256:'fake-sha'};
    else throw Error('Unexpected API '+url.pathname);
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(result)});
  });
  await page.goto(servers[kind].origin+'/#list');
  await page.waitForFunction(()=>!document.querySelector('#app').classList.contains('hidden'));
  await page.waitForFunction(count=>document.querySelectorAll('[data-entry-seq]').length===count,Math.min(entries.length,100));
  await page.evaluate(()=>document.fonts.ready);
  return {page,context,get socket(){return socket;}};
}
const entries=Array.from({length:600},(_,index)=>({id:`e${index+1}`,seq:index+1,role:'bot',source:'telegram',text:`Entry ${index+1}\n\n**Message** https://example.test/${index+1}\n\n- item\n- next`,ts:null}));
async function loadAll(page){
  await page.locator('#threadTop').click();
  await page.waitForFunction(()=>document.querySelector('[data-entry-seq]').dataset.entrySeq==='1');
  await page.locator('#threadBottom').click();
  await page.waitForFunction(()=>{
    const thread=document.querySelector('#thread');
    return !!thread.querySelector('[data-entry-seq="600"]') && thread.scrollHeight-thread.clientHeight-thread.scrollTop<1;
  });
}
async function stream(page,text,phase,id){
  return page.evaluate(async({text,phase,id})=>{
    const chat=await import('/static/js/chat.js'),{md}=await import('/static/js/md.js');
    await chat.handleEvent({type:'turn',tid:'1.3',turn_id:id,phase:'started'});
    window.__rafSamples=[];window.__frameCounts={};window.__measuring=true;
    const start=performance.now();let sent='';const chunks=60;
    for(let index=0;index<chunks;index++){
      const delta=text.slice(Math.floor(index*text.length/chunks),Math.floor((index+1)*text.length/chunks));sent+=delta;
      await chat.handleEvent({type:'delta',tid:'1.3',turn_id:id,text:delta});
      const remaining=start+(index+1)*1000/60-performance.now();
      if(remaining>0)await new Promise(resolve=>setTimeout(resolve,remaining));
    }
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    window.__measuring=false;
    const times=[...window.__rafSamples],counts=Object.values(window.__frameCounts);
    await chat.handleEvent({type:'turn',tid:'1.3',turn_id:id,phase});
    const element=[...document.querySelectorAll('[data-turn]')].find(node=>node.dataset.turn===id);
    const normalized=document.createElement('div');normalized.innerHTML=md(text,{scope:`turn:${id}`});
    return {times,characters:Array.from(text).length,maxPerFrame:Math.max(...counts),sent:sent===text,finalEqual:element.querySelector('.bubble').innerHTML===normalized.innerHTML,length:text.length};
  },{text,phase,id});
}

async function visual() {
  const notes=[
    {id:'note1',seq:1,role:'bot',source:'telegram',ts:null,text:'First[^1] [external](https://example.test/click) [missing](#missing)\n\n[^1]: First note'},
    {id:'note2',seq:2,role:'bot',source:'telegram',ts:null,text:'Second[^1]\n\n[^1]: Second note'},
    {id:'layout',seq:3,role:'bot',source:'telegram',ts:null,text:'| A | B | C | D | E | F |\n| - | - | - | - | - | - |\n| 1 | 2 | 3 | 4 | 5 | 6 |\n\n```js\n'+('const value = "long code"; '.repeat(20))+'\n```\n\n![safe](https://image.test/x) <img src=https://image.test/y onerror=attack()> <script>attack()</script>'}
  ];
  const results=[];
  fs.mkdirSync(path.join(cfg.artifacts,'screenshots'),{recursive:true});
  for(const width of [375,768,1280]){
    const {page,context}=await open('current',width,notes);
    let imageRequests=0;
    await context.route('https://example.test/**',route=>route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><title>external</title>'}));
    await context.route('https://image.test/**',route=>{imageRequests++;return route.abort();});
    if(width<=760)await page.locator('[data-route="list"]').click();
    const topicButton=page.locator('#tlist [data-tid="1.2"]');
    if(width===768){await topicButton.focus();await topicButton.press('Enter');}
    else await topicButton.click();
    await page.waitForFunction(()=>location.hash==='#chat'&&document.activeElement===document.querySelector('#msgIn'));
    const ids=await page.locator('#thread [id]').evaluateAll(nodes=>nodes.map(node=>node.id));
    assert.equal(new Set(ids).size,ids.length);
    for(const seq of [1,2]){
      const reference=page.locator(`[data-entry-seq="${seq}"] [data-footnote-ref]`);
      const href=await reference.getAttribute('href');assert.equal(await reference.getAttribute('target'),null);
      await reference.click();
      assert.equal(await page.evaluate(()=>location.hash),'#chat');
      assert.equal(await page.evaluate(()=>document.activeElement.id),href.slice(1));
      assert.equal(await page.evaluate(()=>document.activeElement.closest('[data-entry-seq]').dataset.entrySeq),String(seq));
      const back=page.locator(`[data-entry-seq="${seq}"] [data-footnote-backref]`);
      const target=await back.getAttribute('href');await back.click();
      assert.equal(await page.evaluate(()=>document.activeElement.id),target.slice(1));
    }
    await page.locator('#thread a[href="#missing"]').click();assert.equal(await page.evaluate(()=>location.hash),'#chat');
    const popupPromise=page.waitForEvent('popup');await page.locator('#thread a[href="https://example.test/click"]').click();
    const popup=await popupPromise;await popup.waitForLoadState();assert.equal(await popup.evaluate(()=>window.opener),null);await popup.close();
    assert.equal(await page.locator('#thread img, #thread script, #thread [onerror]').count(),0);
    for(const theme of ['light','dark']){
      await page.evaluate(async theme=>{const {setPreference}=await import('/static/js/theme.js');setPreference(theme);},theme);
      const overflow=await page.locator('#thread .bubble,#thread .table-scroll,#thread pre').evaluateAll(nodes=>nodes.map(node=>({tag:node.tagName,kind:node.className,right:node.getBoundingClientRect().right,left:node.getBoundingClientRect().left})).filter(rect=>rect.right>innerWidth+.5||rect.left<-.5));
      assert.deepEqual(overflow,[]);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(cfg.artifacts,'screenshots',`markdown-${width}-${theme}.png`)});
      results.push({width,theme,overflow:overflow.length});
    }
    await page.locator('.bar-acts [data-route="files"]').click();await page.waitForFunction(()=>!document.querySelector('#ed').disabled);
    await page.locator('#tPrev').click();await page.locator('#prev').waitFor({state:'visible'});
    assert.equal(await page.locator('#prev strong').textContent(),'bold');assert.equal(await page.locator('#prev script,#prev img').count(),0);
    const previewRef=page.locator('#prev [data-footnote-ref]'),previewTarget=await previewRef.getAttribute('href');await previewRef.click();
    assert.equal(await page.evaluate(()=>document.activeElement.id),previewTarget.slice(1));assert.equal(await page.evaluate(()=>location.hash),'#preview');
    assert.equal(imageRequests,0);
    violations.push(...await page.evaluate(()=>window.__violations));
    await context.close();
  }
  fs.writeFileSync(path.join(cfg.artifacts,'visual.json'),JSON.stringify({results,failures,violations},null,2));
  assert.deepEqual(failures,[]);assert.deepEqual(violations,[]);
  console.log(JSON.stringify({results,failures,violations}));
}

async function previewOverflow() {
  const {page,context}=await open('current',1280,[]);
  try {
    await page.locator('.bar-acts [data-route="files"]').click();
    await page.waitForFunction(()=>!document.querySelector('#ed').disabled);
    await page.locator('#tPrev').click();
    await page.locator('#prev .footnotes > h2.sr-only').waitFor({state:'attached'});
    assert.equal(await page.locator('#prev pre code').textContent(),'file preview code\n');
    assert.equal(await page.locator('#prev [data-copy-code]').count(),0);
    const {height,viewport}=await page.evaluate(()=>({
      height:document.scrollingElement.scrollHeight,
      viewport:window.innerHeight,
    }));
    assert.equal(height,viewport,'Markdown file preview expanded the document past the viewport');
  } finally {
    await context.close();
  }
}

async function navigation() {
  const results=[];
  const rows=entries.slice(-100).map((entry,index)=>({...entry,
    ts:`2025-01-${index<40?'01':index<80?'02':'03'}T12:00:00Z`,imported:index<30,
    text:entry.text+(index>=98?'\n\nNote[^1]\n\n[^1]: Footnote body':'')}));
  for(const width of [1280,375]){
    const {page,context}=await open('current',width,rows);
    await page.evaluate(()=>window.__holdHistory=true);
    for(const [tid,pinned] of [['1.3',false],['1.4',true]]){
      if(width<=760)await page.locator('[data-route="list"]').click();
      await page.locator(`#tlist [data-tid="${tid}"]`).click();
      await page.waitForFunction(()=>document.querySelectorAll('[data-entry-seq]').length===12);
      assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#msgIn')),true);
      const result=await page.evaluate(async pinned=>{
        const thread=document.querySelector('#thread');
        if(!pinned)thread.scrollTop=0;
        const anchor=thread.querySelector('[data-entry-seq]'),top=anchor.getBoundingClientRect().top;
        const focused=document.activeElement;let batches=0;
        while(window.__historyJobs.size){
          const [id,callback]=window.__historyJobs.entries().next().value;
          window.__historyJobs.delete(id);callback();batches++;
        }
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const count=thread.querySelectorAll('[data-entry-seq]').length;
        const keptAnchor=anchor.isConnected;
        const drift=pinned?thread.scrollHeight-thread.clientHeight-thread.scrollTop:Math.abs(anchor.getBoundingClientRect().top-top);
        const keptFocus=document.activeElement===focused;
        const html=thread.innerHTML;const {renderThread}=await import('/static/js/chat.js');renderThread();
        const same=html===thread.innerHTML;
        thread.scrollTop=0;const first=thread.querySelector('[data-entry-seq]').dataset.entrySeq;
        thread.scrollTop=thread.scrollHeight;const last=[...thread.querySelectorAll('[data-entry-seq]')].at(-1).dataset.entrySeq;
        return {count,batches,keptAnchor,keptFocus,drift,same,first,last};
      },pinned);
      assert.equal(result.count,100);assert(result.keptAnchor&&result.keptFocus&&result.same);assert(result.drift<=1);
      assert.equal(result.first,'501');assert.equal(result.last,'600');
      const reference=page.locator('[data-entry-seq="600"] [data-footnote-ref]'),href=await reference.getAttribute('href');await reference.click();
      assert.equal(await page.evaluate(()=>document.activeElement.id),href.slice(1));
      assert.equal(await page.evaluate(()=>location.hash),'#chat');
      results.push({width,tid,pinned,...result});
    }
    violations.push(...await page.evaluate(()=>window.__violations));await context.close();
  }
  fs.mkdirSync(cfg.artifacts,{recursive:true});
  fs.writeFileSync(path.join(cfg.artifacts,'navigation.json'),JSON.stringify({results,failures,violations},null,2));
  assert.deepEqual(failures,[]);assert.deepEqual(violations,[]);console.log(JSON.stringify({results,failures,violations}));
}

async function historyWindow() {
  const results=[];
  fs.mkdirSync(cfg.artifacts,{recursive:true});
  for(const width of [375,768,1280]){
    const rows=entries.map(entry=>({...entry,source:'web'})),secondRows=entries.slice(0,300);
    const session=await open('current',width,rows,secondRows);
    const {page,context}=session;
    const historyRequests=[],secondRequests=[];
    page.on('request',request=>{
      const url=new URL(request.url());
      if(url.pathname==='/api/topics/1.2/messages')historyRequests.push(url.searchParams.get('before'));
      if(url.pathname==='/api/topics/1.3/messages')secondRequests.push(url.searchParams.get('before'));
    });
    await page.evaluate(async()=>{
      const {selectTopic}=await import('/static/js/topics.js');
      selectTopic('1.2');
      const thread=document.querySelector('#thread');
      window.maxMounted=0;
      new MutationObserver(()=>{
        window.maxMounted=Math.max(window.maxMounted,thread.querySelectorAll('[data-entry-seq]').length);
      }).observe(thread,{childList:true});
    });
    await page.waitForFunction(()=>document.querySelector('#msgIn')===document.activeElement);
    async function boundary(toOlder){
      const snapshot=await page.evaluate(toOlder=>{
        const thread=document.querySelector('#thread');
        thread.scrollTop=toOlder?0:thread.scrollHeight;
        const nodes=[...thread.querySelectorAll('[data-entry-seq]')], box=thread.getBoundingClientRect();
        const anchor=nodes.find(node=>node.getBoundingClientRect().bottom>box.top);
        window.windowTestAnchor=anchor;
        return {first:nodes[0].dataset.entrySeq,top:anchor.getBoundingClientRect().top};
      },toOlder);
      await page.waitForFunction(first=>document.querySelector('[data-entry-seq]').dataset.entrySeq!==first,snapshot.first);
      const drift=await page.evaluate(top=>{
        if(!window.windowTestAnchor.isConnected)throw Error('Visible message was detached');
        return Math.abs(window.windowTestAnchor.getBoundingClientRect().top-top);
      },snapshot.top);
      assert.equal(drift,0);
      return drift;
    }
    await boundary(true);
    assert(historyRequests.includes('501'));
    await page.locator('#threadTop').click();
    await page.waitForFunction(()=>document.querySelector('[data-entry-seq]').dataset.entrySeq==='1');
    const afterFetch=historyRequests.length;
    const down=await boundary(false), up=await boundary(true);
    assert.equal(historyRequests.length,afterFetch);
    await page.locator('#threadBottom').click();
    await page.waitForFunction(()=>document.querySelector('[data-entry-seq]').dataset.entrySeq==='501');
    assert.equal(await page.locator('[data-entry-seq="1"]').count(),0);
    await page.evaluate(async()=>{
      const {handleEvent}=await import('/static/js/chat.js');
      await handleEvent({type:'outbox',pending:0,item:{id:'receipt',tid:'1.2',state:'sent'},history_id:'e1',mirror:'sent'});
    });
    await page.locator('#threadTop').click();
    await page.waitForFunction(()=>document.querySelector('[data-entry-seq]').dataset.entrySeq==='1');
    await page.locator('[data-entry-seq="1"] .deliv').waitFor();
    assert.equal(historyRequests.length,afterFetch);
    await page.evaluate(async()=>{const {selectTopic}=await import('/static/js/topics.js');selectTopic('1.2',260);});
    await page.waitForFunction(()=>{
      const node=document.querySelector('[data-entry-seq="260"]'), range=document.querySelector('#thread').getBoundingClientRect();
      return node && node.getBoundingClientRect().top>=range.top && node.getBoundingClientRect().bottom<=range.bottom;
    });
    assert.equal(historyRequests.length,afterFetch);
    const maxMounted=await page.evaluate(()=>window.maxMounted);
    assert.equal(maxMounted,100);
    const olderRequests=historyRequests.filter(Boolean).length;

    const live={...entries[300],ts:'2025-01-01T12:00:00Z',text:'Live search hit'};
    secondRows.push(live);
    session.socket.send(JSON.stringify({type:'entry',tid:'1.3',entry:live}));
    await page.locator('#tlist [data-tid="1.3"] .pv').filter({hasText:live.text}).waitFor({state:'attached'});
    const beforeFirstSearch=secondRequests.length;
    await page.evaluate(async()=>{const {selectTopic}=await import('/static/js/topics.js');selectTopic('1.3',301);});
    const mounted=page.locator('#thread [data-entry-seq]');
    await mounted.first().and(page.locator('[data-entry-seq="202"]')).waitFor({timeout:5000});
    assert.equal(await mounted.count(),100);
    assert.equal(await mounted.last().getAttribute('data-entry-seq'),'301');
    assert(await page.locator('#threadTop').isVisible());
    assert.deepEqual(secondRequests.slice(beforeFirstSearch),[null]);
    await page.locator('#thread').evaluate(thread=>{thread.scrollTop=0;});
    await mounted.first().and(page.locator('[data-entry-seq="152"]')).waitFor({timeout:5000});
    assert.deepEqual(secondRequests.slice(beforeFirstSearch),[null,'202']);

    const reconnectResults=[];
    for(const failTopics of [false,true]){
      await page.evaluate(async()=>{const {selectTopic}=await import('/static/js/topics.js');selectTopic('1.2');});
      await page.waitForFunction(()=>document.querySelector('#msgIn')===document.activeElement);
      if(failTopics){
        await page.route('**/api/topics',route=>route.fulfill({status:500,json:{error:'generic'}}),{times:1});
      }
      await session.socket.close();
      const missed=entries.slice(secondRows.length,secondRows.length+4);
      secondRows.push(...missed);
      if(failTopics){
        await page.locator('#toasts [role="alert"]').filter({hasText:cfg.strings['error.generic']}).waitFor();
      }else{
        rows.push({...rows.at(-1),id:'e601',seq:601,text:'Resynced current topic'});
        await page.locator('#thread [data-entry-seq="601"]').waitFor();
      }
      const beforeReconnectSearch=secondRequests.length;
      await page.evaluate(async()=>{const {selectTopic}=await import('/static/js/topics.js');selectTopic('1.3',250);});
      await page.waitForFunction(()=>{
        const node=document.querySelector('[data-entry-seq="250"]'), range=document.querySelector('#thread').getBoundingClientRect();
        return node && node.getBoundingClientRect().top>=range.top && node.getBoundingClientRect().bottom<=range.bottom;
      });
      await page.locator('#threadBottom').click();
      const recovered=await mounted.evaluateAll(nodes=>nodes.slice(-4).map(node=>Number(node.dataset.entrySeq)));
      assert.deepEqual(recovered,missed.map(entry=>entry.seq),`Missed messages after ${failTopics?'failed':'successful'} resync and search`);
      assert.deepEqual(secondRequests.slice(beforeReconnectSearch),[null]);
      const screenshot=failTopics?'failed-resync-search':'reconnect-search';
      await page.screenshot({path:path.join(cfg.artifacts,`${screenshot}-${width}.png`)});
      reconnectResults.push({failTopics,recovered});
    }
    results.push({width,maxMounted,down,up,olderRequests,searchBefore:secondRequests[beforeFirstSearch+1],reconnectResults});
    await context.close();
  }
  assert.deepEqual(failures,[]);
  fs.writeFileSync(path.join(cfg.artifacts,'window.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
}

try {
  if(cfg.mode==='window')await historyWindow();
  else if(cfg.mode==='navigation')await navigation();
  else if(cfg.mode==='visual') await visual();
  else if(cfg.mode==='preview-overflow') await previewOverflow();
  else {
  const texts={
    blocks:(`## Section\n\n${koreanParagraph} **${koreanEmphasis}** https://example.test/path 🙂\n\n`).repeat(500).slice(0,20000),
    paragraph:(`${koreanParagraph} **${koreanEmphasis}** https://example.test/path 🙂 `).repeat(600).slice(0,20000),
    code:'```js\n'+('const value = "https://example.test/path";\n').repeat(600).slice(0,19990)+'\n```'
  };
  for(const shape of ['blocks','paragraph'])texts[shape]=Array.from(texts[shape].repeat(2)).slice(0,20000).join('');
  for(const width of [1280,375]){
    const opening={baseline:[],current:[]};
    for(let run=0;run<(cfg.runs||5);run++){
      for(const kind of run%2?['current','baseline']:['baseline','current']){
        const {page,context}=await open(kind,width,entries);
        const elapsed=await page.evaluate(async()=>{
          const topics=await import('/static/js/topics.js');
          const start=performance.now();topics.selectTopic('1.3');
          await new Promise(resolve=>{const check=()=>{
            const thread=document.querySelector('#thread'),rows=thread.querySelectorAll('[data-entry-seq]');
            if(rows.length>=12&&thread.scrollHeight-thread.clientHeight-thread.scrollTop<=1)resolve();
            else requestAnimationFrame(check);
          };requestAnimationFrame(check);});
          const firstVisible=performance.now()-start;
          await new Promise(resolve=>{const check=()=>document.querySelectorAll('[data-entry-seq]').length===100?requestAnimationFrame(resolve):requestAnimationFrame(check);requestAnimationFrame(check);});
          return {firstVisible,settled:performance.now()-start};
        });
        opening[kind].push(elapsed.firstVisible);
        samples.push({kind,width,run,openingTiming:elapsed});
        {
          await loadAll(page);
          for(const [shape,text] of Object.entries(texts)){
            const measured=await stream(page,text,['done','aborted','error'][run%3],`stream-${run}-${shape}`);
            assert(measured.sent&&measured.finalEqual);assert(measured.maxPerFrame<=1);
            samples.push({kind,width,run,shape,p95:percentile(measured.times),callbacks:measured.times.length,...measured});
          }
        }
        violations.push(...await page.evaluate(()=>window.__violations));await context.close();
      }
    }
    samples.push({width,opening,openingP95:{baseline:percentile(opening.baseline),current:percentile(opening.current)},openingIncrease:percentile(opening.current)-percentile(opening.baseline)});
  }
  fs.mkdirSync(cfg.artifacts,{recursive:true});
  fs.writeFileSync(path.join(cfg.artifacts,'performance.json'),JSON.stringify({samples,failures,violations},null,2));
  assert.deepEqual(failures,[]);assert.deepEqual(violations,[]);
  console.log(JSON.stringify({samples:samples.map(({times,...sample})=>sample),failures,violations}));
  for(const width of [1280,375])for(const shape of ['blocks','paragraph','code']){
    const pooled=kind=>samples.filter(sample=>sample.kind===kind&&sample.width===width&&sample.shape===shape).flatMap(sample=>sample.times);
    assert(percentile(pooled('current'))<=percentile(pooled('baseline'))+16.7,`RAF regression exceeded 16.7ms: ${width}/${shape}`);
  }
  assert(samples.filter(sample=>sample.openingIncrease!==undefined).every(sample=>sample.openingIncrease<=150),'Topic opening regression exceeded 150ms');
  }
}finally{
  await browser.close();for(const {server} of Object.values(servers))server.close();
}
