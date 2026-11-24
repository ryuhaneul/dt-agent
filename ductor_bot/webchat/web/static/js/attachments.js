import { api, ApiError } from './api.js';
import { t } from './i18n.js';
import { notify } from './auth.js';
import { esc } from './md.js';
import { Sha256 } from './sha256.js';

const ENCODE_LIMIT = 32 * 1024 * 1024, PIXEL_LIMIT = 32_000_000;
const modes = ['original', 'resize', 'compress', 'resize-compress'];
const scales = [80, 50, 25];
const resizes = mode => mode === 'resize' || mode === 'resize-compress';
const completionRejections = new Map([
  ['415:mime_mismatch', 'attach.type_rejected'],
  ['415:invalid_type', 'attach.type_rejected'],
  ['415:invalid_image', 'attach.type_rejected'],
  ['413:image_too_large', 'attach.image_too_large'],
]);
const nonRetryableErrors = new Set(completionRejections.values());
const bytes = size => size >= 1024 ** 2 ? `${(size / 1024 ** 2).toFixed(1)} MiB` : `${Math.ceil(size / 1024)} KiB`;
const failure = (key, values = {}) => Object.assign(new Error(key), { attachmentKey: key, attachmentValues: values });
function raster(type) { return type.startsWith('image/') && !type.startsWith('image/svg'); }

async function smallerLosslessWebP(canvas, maxSize) {
  try {
    const candidate = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', 1));
    if (candidate?.type !== 'image/webp' || candidate.size >= maxSize) return null;
    const buffer = await candidate.arrayBuffer();
    const data = new DataView(buffer);
    const tag = offset => String.fromCharCode(...new Uint8Array(buffer, offset, 4));
    if (data.byteLength < 12 || tag(0) !== 'RIFF' || tag(8) !== 'WEBP') return null;
    for (let offset = 12; offset + 8 <= data.byteLength;) {
      const length = data.getUint32(offset + 4, true);
      if (offset + 8 + length > data.byteLength) break;
      if (tag(offset) === 'VP8L') return candidate;
      offset += 8 + length + length % 2;
    }
  } catch {
    return null;
  }
  return null;
}

export async function processImage(file, options, onDimensions) {
  const original = options.mode === 'original';
  if (original && !onDimensions) return file;
  let image, source, displayed;
  try {
    if (file.size > ENCODE_LIMIT) throw failure('attach.processing_limit');
    image = await createImageBitmap(file);
    if (image.width * image.height > PIXEL_LIMIT) throw failure('attach.processing_limit');
    const shrinking = resizes(options.mode);
    const ratio = shrinking ? Math.min(1, options.scale / 100) : 1;
    const width = Math.max(1, Math.round(image.width * ratio));
    const height = Math.max(1, Math.round(image.height * ratio));
    source = `${image.width}×${image.height}`;
    if (original) return file;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) throw failure('attach.processing_failed');
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const compress = options.mode !== 'resize';
    const type = compress ? 'image/webp' : ['image/jpeg','image/png','image/webp'].includes(file.type) ? file.type : 'image/png';
    const quality = compress ? options.quality / 100 : 0.92;
    let blob;
    try {
      blob = await new Promise(resolve => canvas.toBlob(resolve, type, quality));
      if (!blob || blob.type !== type) throw failure('attach.processing_failed');
      if (!compress && file.type !== 'image/jpeg') {
        blob = await smallerLosslessWebP(canvas, Math.min(blob.size, file.size)) ?? blob;
      }
    } finally {
      canvas.width = canvas.height = 0;
    }
    if (blob.size >= file.size) return file;
    displayed = shrinking ? `${source} → ${width}×${height}` : source;
    const extension = { 'image/jpeg':'jpg', 'image/png':'png', 'image/webp':'webp' }[blob.type];
    return new File([blob], file.name.replace(/\.[^.]*$/, '') + '.' + extension, {
      type: blob.type, lastModified: file.lastModified,
    });
  } catch (error) {
    if (original) return file;
    throw error;
  } finally {
    image?.close();
    if (source) onDimensions?.(displayed ?? source, source);
  }
}

export function initAttachments({ getTopic, changed, resume }) {
  const picker = document.querySelector('#filePick'), tray = document.querySelector('#attachmentTray');
  if (!picker || !tray || picker.type !== 'file') return null;
  initAttachmentSettings();
  const wrap = document.querySelector('#attachmentWrap'), button = document.querySelector('#attachBtn');
  const panel = document.querySelector('#imageOptions'), drop = document.querySelector('#attachmentDrop');
  const status = document.querySelector('#attachmentStatus'), input = document.querySelector('#msgIn');
  const topics = new Map();
  let busy = false, generation = 0, menu = null, previewWork = Promise.resolve(), timer = null, controller = null, dragDepth = 0, activeRun = null, posting = false, activeItem = null, cancelled = false;
  const items = tid => topics.get(tid) || [];
  const current = () => items(getTopic()?.tid);
  const available = () => { const topic = getTopic(); return topic && !topic.read_only && topic.tid !== 'unknown' && !busy; };
  let measuredValue = '', measuredWidth = 0, measuredMaximum = 0, minimumHeight;
  function resize(fromInput = false) {
    const value = input.value;
    const maximum = (window.visualViewport?.height || innerHeight) * 0.4;
    const sameBox = fromInput && maximum === measuredMaximum
      && measuredWidth > 0 && input.clientWidth === measuredWidth;
    const scrollHeight = sameBox ? input.scrollHeight : 0;
    // Overflow exposes the natural height even during replacements such as IME edits.
    const hasNaturalHeight = sameBox && scrollHeight > input.clientHeight;
    // Otherwise scrollHeight has a floor at clientHeight, so remeasure possible shrinks.
    const canKeepHeight = sameBox && !hasNaturalHeight
      && (value.startsWith(measuredValue) || input.style.height === minimumHeight);
    if (!canKeepHeight) {
      let height = scrollHeight;
      if (!hasNaturalHeight) {
        input.style.height = 'auto';
        height = input.scrollHeight;
        measuredWidth = input.clientWidth;
      }
      input.style.height = Math.min(height, maximum) + 'px';
      input.style.overflowY = height > maximum ? 'auto' : 'hidden';
      if (!value && measuredWidth > 0) minimumHeight = input.style.height;
    }
    measuredValue = value; measuredMaximum = maximum;
    position();
  }
  function render() {
    const list = current(); wrap.hidden = !list.length;
    document.querySelector('#attachmentCount').textContent = t('attach.count', { n:list.length });
    tray.innerHTML = list.map(item => `<article class="attach-draft" data-attachment="${item.id}">
      <div class="attach-top">${item.url ? `<img class="attach-thumb" src="${esc(item.url)}" alt="${esc(t('attach.preview', { name:item.output.name }))}">` : `<span class="attach-icon" aria-hidden="true">${esc((item.output.name.split('.').at(-1) || 'FILE').slice(0,5).toUpperCase())}</span>`}
      <div class="grow"><span class="attach-name" title="${esc(item.output.name)}">${esc(item.output.name)}</span><span class="attach-sub">${esc(bytes(item.output.size))} · ${esc(item.output.type || 'FILE')}</span></div>
      <button type="button" class="attach-remove" data-remove="${item.id}" aria-label="${esc(t('attach.remove', { name:item.output.name }))}"${busy?' disabled':''}>×</button></div>
      ${item.image ? `<div class="attach-processing"><button type="button" class="attach-chip" data-options="${item.id}" aria-haspopup="dialog" aria-expanded="${menu?.item===item}"${busy?' disabled':''}>${esc(t('attach.'+item.options.mode))} <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button><span class="attach-estimate">${esc(t('attach.estimate', { size:bytes(item.output.size) }))}</span></div>` : ''}${progressMarkup(item)}</article>`).join('');
    button.disabled = !available(); picker.disabled = !available();
    status.hidden = !busy; status.textContent = busy ? t(posting?'attach.dispatching':'attach.uploading') : '';
    list.forEach(updateProgress); resize();
  }
  function progressMarkup(item) {
    return `<div class="attach-transfer" data-transfer${!item.state||item.state==='ready'?' hidden':''}>
      <div class="attach-transfer-head"><span data-state></span><span data-amount></span></div>
      <progress value="0" max="${Math.max(1,item.output.size)}" aria-label="${esc(t('attach.progress',{name:item.output.name}))}"></progress>
      <p data-transfer-error role="status"></p><div class="attach-transfer-actions"><button type="button" class="btn btn-sm" data-resume="${item.id}">${esc(t('attach.resume'))}</button><button type="button" class="btn btn-sm" data-cancel-upload="${item.id}">${esc(t('attach.cancel_upload'))}</button></div></div>`;
  }
  function updateProgress(item) {
    const card=tray.querySelector(`[data-attachment="${item.id}"]`), block=card?.querySelector('[data-transfer]');
    if(!block)return;
    block.hidden=!item.state||item.state==='ready';block.dataset.uploadState=item.state||'ready';
    const received=item.received||0,percent=item.state==='complete'?100:item.output.size?Math.floor(received/item.output.size*100):0;
    block.querySelector('[data-state]').textContent=t('attach.state_'+(item.state||'ready'));
    block.querySelector('[data-amount]').textContent=`${percent}% · ${bytes(received)} / ${bytes(item.output.size)}`;
    const meter=block.querySelector('progress');meter.value=item.state==='complete'?meter.max:received;
    block.querySelector('[data-transfer-error]').textContent=item.error?t(item.error):'';
    const retry=block.querySelector('[data-resume]'),cancel=block.querySelector('[data-cancel-upload]');
    retry.hidden=nonRetryableErrors.has(item.error)||!['failed','cancelled','paused'].includes(item.state);retry.disabled=busy;
    cancel.hidden=!busy||posting||!['hashing','uploading','verifying'].includes(item.state);
    cancel.disabled=posting;
  }
  function free(item) { if (item.url) URL.revokeObjectURL(item.url); }
  function setPreview(item) {
    free(item);
    item.url = item.image && item.output.size <= ENCODE_LIMIT ? URL.createObjectURL(item.output) : null;
  }
  async function limits() {
    const result = await api('/attach-settings');
    if (![result.max_file_bytes,result.max_files_per_message,result.chunk_bytes].every(value => Number.isSafeInteger(value) && value > 0)) throw failure('attach.settings_failed');
    return result;
  }
  function validate(list, settings) {
    if (list.length > settings.max_files_per_message) throw failure('attach.too_many', { n:settings.max_files_per_message });
    const large = list.find(item => item.output.size > settings.max_file_bytes);
    if (large) throw failure('attach.too_large', { name:large.output.name, size:bytes(settings.max_file_bytes) });
  }
  function showError(error) {
    if (error.code === 'auth_required') return;
    notify(error.attachmentKey || 'attach.failed', error.attachmentValues || {});
  }
  async function add(files) {
    if (!available() || !files.length) return;
    const topic = getTopic(), epoch = generation, incoming = [...files];
    try {
      const settings = await limits();
      if (epoch !== generation) return;
      const existing = items(topic.tid);
      const added = incoming.map(file => ({ id:crypto.randomUUID(), original:file, output:file, image:raster(file.type), options:{ mode:'original',scale:scales[0],quality:80 }, url:null, upload:null, state:'ready', received:0, error:null }));
      validate([...existing,...added], settings);
      for (const item of added) setPreview(item);
      topics.set(topic.tid,[...existing,...added]); render(); changed();
    } catch (error) { if (epoch === generation) showError(error); }
  }
  function closeMenu(focus = true) {
    const id = menu?.item.id; menu = null; clearTimeout(timer); panel.hidden = true;
    tray.querySelectorAll('[data-options]').forEach(node => node.setAttribute('aria-expanded','false'));
    if (focus && id) tray.querySelector(`[data-options="${id}"]`)?.focus();
  }
  function position() {
    if (!menu || panel.hidden) return;
    const trigger = tray.querySelector(`[data-options="${menu.item.id}"]`);
    if (!trigger) { closeMenu(false); return; }
    const rect = trigger.getBoundingClientRect();
    panel.style.left = Math.max(12,Math.min(rect.left,innerWidth-panel.offsetWidth-12))+'px';
    panel.style.top = Math.max(12,rect.top-panel.offsetHeight-8)+'px';
  }
  function optionsChanged() {
    if (!menu) return;
    const snapshot = menu; const revision = ++snapshot.revision;
    snapshot.result = null;
    panel.querySelector('[data-apply]').disabled = true;
    panel.querySelectorAll('[data-mode]').forEach(node => node.setAttribute('aria-pressed',String(node.dataset.mode===snapshot.options.mode)));
    panel.querySelector('[data-scale-row]').hidden = !resizes(snapshot.options.mode);
    panel.querySelector('[data-quality-row]').hidden = !['compress','resize-compress'].includes(snapshot.options.mode);
    panel.querySelector('output').textContent = snapshot.options.quality;
    const estimate = panel.querySelector('[data-estimate-text]');
    const dimensions = panel.querySelector('[data-dimensions]');
    const keptOriginal = panel.querySelector('[data-kept-original]');
    keptOriginal.hidden = true;
    estimate.textContent = t('attach.calculating');
    dimensions.textContent = '';
    clearTimeout(timer);
    timer = setTimeout(() => {
      previewWork = previewWork.catch(()=>{}).then(async () => {
        if (menu !== snapshot || revision !== snapshot.revision) return;
        try {
          const { item, options } = snapshot;
          let size = item.originalDimensions;
          let result = item.original;
          if (options.mode !== 'original' || size === undefined) {
            let source = '';
            try {
              result = await processImage(item.original, {...options}, (displayed, original) => {
                size = displayed;
                source = original;
              });
            } finally {
              item.originalDimensions ??= source;
            }
          }
          if (menu !== snapshot || revision !== snapshot.revision) return;
          snapshot.result = result;
          estimate.textContent = t('attach.estimate',{size:bytes(result.size)}) + ' · ' + (result.type || 'FILE');
          dimensions.textContent = size ?? '';
          keptOriginal.hidden = options.mode === 'original' || result !== item.original;
          panel.querySelector('[data-apply]').disabled = false;
        } catch (error) {
          if (menu===snapshot && revision===snapshot.revision) estimate.textContent=t(error.attachmentKey || 'attach.processing_failed');
        }
        position();
      });
    },120);
    position();
  }
  function openMenu(item) {
    closeMenu(false);
    menu = {item, options:{...item.options}, revision:0, result:null};
    panel.innerHTML = `<h3>${esc(t('attach.processing'))}</h3><div class="attach-modes" role="group" aria-label="${esc(t('attach.processing'))}">${modes.map(mode=>`<button type="button" data-mode="${mode}" aria-pressed="${mode===item.options.mode}">${esc(t('attach.'+mode))}</button>`).join('')}</div>
      <label data-scale-row>${esc(t('attach.scale'))}<select data-scale aria-label="${esc(t('attach.scale'))}">${scales.map(scale=>`<option value="${scale}"${scale===item.options.scale?' selected':''}>${scale}%</option>`).join('')}</select></label>
      <label data-quality-row>WebP · ${esc(t('attach.quality'))}<input data-quality type="range" min="10" max="100" value="${item.options.quality}" aria-label="${esc(t('attach.quality'))}"><output>${item.options.quality}</output></label>
      ${item.original.type==='image/gif'?`<p class="attach-note">${esc(t('attach.gif_note'))}</p>`:''}<p data-estimate role="status"><span data-estimate-text></span><span data-dimensions></span><span data-kept-original hidden>${esc(t('attach.kept_original'))}</span></p><div class="attach-actions"><button type="button" class="btn btn-sm" data-cancel>${esc(t('common.cancel'))}</button><button type="button" class="btn btn-primary btn-sm" data-apply>${esc(t('attach.apply'))}</button></div>`;
    panel.hidden = false;
    tray.querySelector(`[data-options="${item.id}"]`).setAttribute('aria-expanded','true');
    optionsChanged(); panel.querySelector(`[data-mode="${item.options.mode}"]`).focus();
  }
  button.addEventListener('click',()=>picker.click());
  picker.addEventListener('change',()=>{ void add(picker.files); picker.value=''; });
  tray.addEventListener('error',event=>{
    if(event.target.matches('.attach-thumb')){const icon=document.createElement('span');icon.className='attach-icon';icon.setAttribute('aria-hidden','true');icon.textContent='IMG';event.target.replaceWith(icon);}
  },true);
  tray.addEventListener('click',event=>{
    const remove = event.target.closest('[data-remove]'), option = event.target.closest('[data-options]');
    const cancel=event.target.closest('[data-cancel-upload]'),retry=event.target.closest('[data-resume]');
    if(cancel && busy && !posting){cancelled=true;controller?.abort();return;}
    if(retry && !busy){resume?.();return;}
    if (!available()) return;
    if (remove) {
      const item = current().find(item=>item.id===remove.dataset.remove);
      if (!item) return;
      closeMenu(false); free(item);
      if(item.upload?.id)void api(`/topics/${encodeURIComponent(getTopic().tid)}/uploads/${item.upload.id}`,{method:'DELETE'}).catch(()=>{});
      topics.set(getTopic().tid,current().filter(value=>value!==item)); render(); changed(); input.focus();
    }
    if (option) { const item=current().find(item=>item.id===option.dataset.options); if(item) openMenu(item); }
  });
  panel.addEventListener('click',async event=>{
    const mode = event.target.closest('[data-mode]');
    if (!menu) return;
    if (mode) { menu.options.mode=mode.dataset.mode; optionsChanged(); }
    if (event.target.closest('[data-cancel]')) closeMenu();
    if (event.target.closest('[data-apply]') && menu.result) {
      const snapshot=menu, revision=menu.revision, result=menu.result, options={...menu.options};
      try {
        const settings=await limits();
        if (menu!==snapshot || snapshot.revision!==revision) return;
        validate(current().map(item=>item===snapshot.item?{...item,output:result}:item),settings);
        snapshot.item.output=result; snapshot.item.options=options; snapshot.item.upload=null;snapshot.item.state='ready';snapshot.item.received=0;snapshot.item.error=null;
        setPreview(snapshot.item); const id=snapshot.item.id; closeMenu(false); render(); changed();
        tray.querySelector(`[data-options="${id}"]`)?.focus();
      } catch(error) { showError(error); }
    }
  });
  panel.addEventListener('input',event=>{
    if (!menu) return;
    if (event.target.matches('[data-quality]')) { menu.options.quality=Number(event.target.value); optionsChanged(); }
  });
  panel.addEventListener('change',event=>{
    if(menu && event.target.matches('[data-scale]')) { menu.options.scale=Number(event.target.value); optionsChanged(); }
  });
  panel.addEventListener('keydown',event=>{
    if(event.key==='Escape') { event.preventDefault();closeMenu(); }
    if(event.key==='Tab') {
      const nodes=[...panel.querySelectorAll('button:not(:disabled),select,input')].filter(node=>!node.closest('[hidden]'));
      const first=nodes[0],last=nodes.at(-1);
      if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus();}
      else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus();}
    }
  });
  document.addEventListener('pointerdown',event=>{if(menu && !panel.contains(event.target) && !event.target.closest('[data-options]'))closeMenu(false);});
  const pane=document.querySelector('[data-view="chat"]');
  pane.addEventListener('paste',event=>{
    let files=[...(event.clipboardData?.items||[])].filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
    if(!files.length)files=[...(event.clipboardData?.files||[])];
    if(files.length){ event.preventDefault();void add(files); }
  });
  pane.addEventListener('dragenter',event=>{if(available() && [...(event.dataTransfer?.types||[])].includes('Files')){event.preventDefault();dragDepth++;drop.hidden=false;}});
  pane.addEventListener('dragover',event=>{if([...(event.dataTransfer?.types||[])].includes('Files')){event.preventDefault();if(event.dataTransfer)event.dataTransfer.dropEffect=available()?'copy':'none';}});
  pane.addEventListener('dragleave',()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)drop.hidden=true;});
  pane.addEventListener('drop',event=>{if(event.dataTransfer?.files.length){event.preventDefault();dragDepth=0;drop.hidden=true;void add(event.dataTransfer.files);}});
  document.addEventListener('topic-selected',()=>{closeMenu(false);render();});
  document.addEventListener('route-change',()=>{closeMenu(false);render();});
  document.addEventListener('topic-updated',render);
  document.addEventListener('language-changed',()=>{closeMenu(false);render();});
  document.addEventListener('auth-change',event=>{
    if(!event.detail.authenticated){++generation;controller?.abort();closeMenu(false);for(const list of topics.values())list.forEach(free);topics.clear();busy=false;posting=false;activeRun=null;activeItem=null;}
    render();changed();
  });
  wrap.addEventListener('scroll',position);
  async function digest(file, signal) {
    const hash=new Sha256();
    for(let offset=0;offset<file.size;offset+=1024*1024){
      if(signal.aborted)throw failure('attach.failed');
      hash.update(new Uint8Array(await file.slice(offset,offset+1024*1024).arrayBuffer()));
      await new Promise(resolve=>setTimeout(resolve,0));
    }
    return hash.hex();
  }
  return {
    render,
    has:tid=>items(tid).length>0,
    async upload(tid) {
      if(busy)throw failure('attach.failed');
      closeMenu(false);busy=true;posting=false;cancelled=false;controller=new AbortController();
      const run=crypto.randomUUID();activeRun=run;
      const signal=controller.signal, epoch=generation, batch=[...items(tid)];
      for(const item of batch){if(item.state!=='complete')item.state='waiting';item.error=null;}
      render();changed();
      try {
        const settings=await limits();validate(batch,settings);
        const prefix=`/topics/${encodeURIComponent(tid)}/uploads`,ids=[];
        for(const item of batch){
          if(epoch!==generation || signal.aborted)throw failure('attach.failed');
          activeItem=item;
          const file=item.output;
          if(!item.upload){item.state='hashing';updateProgress(item);item.upload={hash:await digest(file,signal),id:null};}
          const upload=item.upload;
          let state=null;
          if(upload.id){try{state=await api(`${prefix}/${upload.id}`,{signal});}catch(error){if(error.status!==404)throw error;upload.id=null;}}
          if(!upload.id){state=await api(prefix,{method:'POST',body:{name:file.name,size:file.size,mime:file.type},signal});upload.id=state.id;}
          let offset=state.offset;
          if(!Number.isSafeInteger(offset)||offset<0||offset>file.size)throw failure('attach.failed');
          item.received=offset;item.state='uploading';updateProgress(item);
          while(offset<file.size){
            const result=await api(`${prefix}/${upload.id}`,{method:'PUT',body:file.slice(offset,offset+settings.chunk_bytes),binary:true,headers:{'Upload-Offset':String(offset)},signal});
            if(!Number.isSafeInteger(result.offset)||result.offset<=offset||result.offset>file.size)throw failure('attach.failed');
            offset=result.offset;item.received=offset;updateProgress(item);
          }
          item.state='verifying';updateProgress(item);
          await api(`${prefix}/${upload.id}/complete`,{method:'POST',body:{sha256:upload.hash},signal});
          item.state='complete';item.received=file.size;updateProgress(item);ids.push(upload.id);
        }
        if(epoch!==generation)throw failure('attach.failed');
        posting=true;render();changed();
        return {ids,batch,run};
      } catch(error) {
        let result=error.code==='auth_required'||error.attachmentKey?error:failure('attach.failed');
        if(epoch===generation){
          if(activeItem){
            if(activeItem.state==='verifying'){
              const rejection=completionRejections.get(error.status+':'+error.code);
              if(rejection)result=failure(rejection);
            }
            activeItem.state=cancelled?'cancelled':'failed';
            activeItem.error=cancelled?null:(result.attachmentKey||'attach.failed');
          }
          batch.filter(item=>item.state==='waiting').forEach(item=>{item.state='paused';});
        }
        result.attachmentRun=run;if(cancelled)result.silent=true;throw result;
      } finally {if(activeRun===run){controller=null;render();changed();}}
    },
    finish(run,error=null){
      if(!run||activeRun!==run)return;
      if(error && posting)for(const item of current()){if(item.state==='complete'){item.state='failed';item.error='attach.message_failed';}}
      const focus=activeItem?.id;busy=false;posting=false;activeRun=null;activeItem=null;render();changed();
      if(error?.silent && focus)tray.querySelector(`[data-resume="${focus}"]`)?.focus();
    },
    sent(tid,batch){const list=items(tid);batch.forEach(free);topics.set(tid,list.filter(item=>!batch.includes(item)));render();changed();},
    resize,
  };
}

function initAttachmentSettings() {
  const dialog=document.querySelector('#attachmentLimits');
  if(!dialog || dialog.tagName!=='DIALOG')return;
  const opener=document.querySelector('#openAttachSettings'),form=document.querySelector('#attachmentLimitsForm');
  const size=document.querySelector('#attachmentMaxSize'),count=document.querySelector('#attachmentMaxCount');
  const message=document.querySelector('#attachmentLimitsNotice'),save=document.querySelector('#saveAttachmentLimits');
  let settings=null,sequence=0;
  const show=(key,values={},error=false)=>{message.textContent=t(key,values);message.hidden=false;message.className=error?'attach-limit-error':'attach-limit-success';};
  function fill(value){settings=value;size.value=String(value.max_file_bytes/1024**2);count.value=String(value.max_files_per_message);size.max=String(value.server_max_file_bytes/1024**2);count.max=String(value.server_max_files_per_message);document.querySelector('#attachmentServerLimits').textContent=t('attach.server_limits',{size:value.server_max_file_bytes/1024**2,n:value.server_max_files_per_message});}
  function enabled(value){size.disabled=count.disabled=!value;save.disabled=!value;form.setAttribute('aria-busy',String(!value));}
  opener.addEventListener('click',async()=>{
    document.querySelector('#accountMenu').hidden=true;document.querySelector('#accountButton').setAttribute('aria-expanded','false');
    const version=++sequence;settings=null;message.hidden=true;form.reset();enabled(false);dialog.showModal();
    try{const value=await api('/attach-settings');if(version!==sequence)return;fill(value);enabled(true);size.focus();}
    catch(error){if(version===sequence && error.code!=='auth_required')show('attach.settings_failed',{},true);}
  });
  document.querySelector('#closeAttachmentLimits').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{++sequence;form.reset();settings=null;document.querySelector('#accountButton').focus();});
  document.addEventListener('auth-change',event=>{if(!event.detail.authenticated)dialog.close();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!settings)return;
    size.removeAttribute('aria-invalid');count.removeAttribute('aria-invalid');
    const mib=Number(size.value),number=Number(count.value),amount=Math.round(mib*1024**2);
    const badSize=!Number.isFinite(mib)||mib<=0||!Number.isSafeInteger(amount)||amount<1||mib>settings.server_max_file_bytes/1024**2;
    const badCount=!Number.isSafeInteger(number)||number<1||number>settings.server_max_files_per_message;
    if(badSize||badCount){if(badSize)size.setAttribute('aria-invalid','true');if(badCount)count.setAttribute('aria-invalid','true');show('attach.limit_invalid',{size:settings.server_max_file_bytes/1024**2,n:settings.server_max_files_per_message},true);(badSize?size:count).focus();return;}
    const version=sequence;enabled(false);message.hidden=true;
    try{const value=await api('/attach-settings',{method:'PUT',body:{max_file_bytes:amount,max_files_per_message:number}});if(version!==sequence)return;fill(value);show('attach.limits_saved');}
    catch(error){if(version===sequence && error.code!=='auth_required')show('attach.settings_failed',{},true);}
    finally{if(version===sequence)enabled(true);}
  });
}
