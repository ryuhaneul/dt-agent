import { api } from './api.js';
import { esc } from './md.js';
import { t } from './i18n.js';

const SAFE_IMAGES=new Set(['image/jpeg','image/png','image/webp','image/gif','image/bmp']);
const sizeLabel=size=>size<1024?`${size} B`:size<1024**2?`${(size/1024).toFixed(1)} KiB`:`${(size/1024**2).toFixed(1)} MiB`;
const HEADER=/\[INCOMING FILE\]\nThe user sent you a file via (?:Web|Telegram)\.\nPath: ([^\n]+)\nType: ([^\n]+)\nOriginal filename: ([^\n]+)\n\nCheck tools\/media_tools\/CLAUDE\.md for file handling instructions\.(?:\nWeb attachments are in a temporary folder \([^\n]*\); if the user asks to keep them, copy them to a workspace location outside cleanup-managed directories\.)?(?:\nThis is (?:an audio\/voice message|a video file)\.[^\n]*)?/g;
function belongs(match,item){return match[3]===item.name && (match[1]===item.path || match[1].split('/').at(-1)===item.path);}
function caption(text,list){
  const matches=[...text.matchAll(HEADER)];
  if(!matches.length||matches[0].index!==0||!list.some(item=>belongs(matches[0],item)))return text;
  let remaining=text;
  // Generated later attachments are suffixes, so caption text resembling a header stays intact.
  for(let index=list.length-1;index>=1;index--){const item=list[index],match=[...matches].reverse().find(value=>belongs(value,item)&&remaining.endsWith('\n\n'+value[0]));if(!match)return text;remaining=remaining.slice(0,-match[0].length-2);}
  remaining=remaining.slice(matches[0][0].length);
  if(!remaining)return '';
  return remaining.startsWith('\n\nUser message: ')?remaining.slice('\n\nUser message: '.length):text;
}

export function initAttachmentView({changed}) {
  const dialog=document.querySelector('#attachmentViewer');
  if(!dialog||dialog.tagName!=='DIALOG')return null;
  const thread=document.querySelector('#thread'),image=document.querySelector('#attachmentOriginal');
  const cache=new Map(),references=new Map();
  let generation=0,live=false,opener=null,openKey=null,zoom=false;
  const key=(tid,id)=>JSON.stringify([tid,id]);
  function validate(tid,entry,list){
    if(!Array.isArray(list))return [];
    return list.filter(item=>{
      if(!item||!(/^[a-f0-9]{32}$/.test(item.id))||typeof item.name!=='string'||typeof item.mime!=='string'||!Number.isSafeInteger(item.size)||item.size<0)return false;
      const base=`/api/topics/${encodeURIComponent(tid)}/messages/${encodeURIComponent(entry.id)}/attachments/${item.id}`;
      return item.download_url===base;
    }).map(item=>{
      const inline=item.inline_allowed===true&&SAFE_IMAGES.has(item.mime),base=item.download_url;
      return {...item,original_url:inline&&item.original_url===base+'?inline=1'?item.original_url:null,preview_url:inline&&item.preview_url===base+'/preview'?item.preview_url:null};
    });
  }
  function list(tid,entry){return entry.has_attachments?cache.get(key(tid,entry.id))?.files||[]:[];}
  function restore(){
    image.removeAttribute('src');image.classList.remove('zoomed');
    if(live){const target=opener?.isConnected?opener:[...thread.querySelectorAll('[data-open-attachment]')].find(node=>node.dataset.openAttachment===openKey);(target||document.querySelector('#msgIn')).focus();}
    opener=null;openKey=null;
  }
  function open(button){
    const item=references.get(button.dataset.openAttachment);if(!item?.original_url)return;
    opener=button;openKey=button.dataset.openAttachment;zoom=false;
    document.querySelector('#attachmentViewerTitle').textContent=item.name;
    document.querySelector('#attachmentViewerMeta').textContent=sizeLabel(item.size)+' · '+item.mime;
    document.querySelector('#attachmentViewerError').hidden=true;
    image.alt=item.name;image.classList.remove('zoomed');image.src=item.original_url;
    document.querySelector('#attachmentOriginalTab').href=item.original_url;
    document.querySelector('#attachmentOriginalDownload').href=item.download_url;
    document.querySelector('#attachmentZoom').textContent=t('attach.zoom');
    document.querySelector('#attachmentZoom').setAttribute('aria-pressed','false');
    dialog.showModal();document.querySelector('#closeAttachmentViewer').focus();
  }
  document.querySelector('#closeAttachmentViewer').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',restore);
  document.querySelector('#attachmentZoom').addEventListener('click',event=>{zoom=!zoom;image.classList.toggle('zoomed',zoom);event.currentTarget.textContent=t(zoom?'attach.fit':'attach.zoom');event.currentTarget.setAttribute('aria-pressed',String(zoom));});
  image.addEventListener('error',()=>{document.querySelector('#attachmentViewerError').hidden=false;});
  thread.addEventListener('click',event=>{const button=event.target.closest('[data-open-attachment]');if(button)open(button);});
  thread.addEventListener('error',event=>{if(event.target.matches('.message-attachment img')){const button=event.target.closest('[data-open-attachment]');if(button)button.textContent=t('attach.open_original');}},true);
  document.addEventListener('auth-change',event=>{live=event.detail.authenticated;++generation;cache.clear();references.clear();dialog.close();});
  document.addEventListener('topic-selected',()=>dialog.close());
  return {
    invalidate(tid,id){cache.delete(key(tid,id));},
    reset(){cache.clear();},
    load(tid,entries){
      const epoch=generation;
      for(const entry of entries){
        const identity=key(tid,entry.id);if(!entry.has_attachments){cache.delete(identity);continue;}if(cache.has(identity))continue;
        const pending={files:[]};cache.set(identity,pending);
        void api(`/topics/${encodeURIComponent(tid)}/messages/${encodeURIComponent(entry.id)}/attachments`).then(result=>{
          if(epoch!==generation||cache.get(identity)!==pending)return;cache.set(identity,{files:validate(tid,entry,result.attachments)});changed(tid,entry.id);
        }).catch(()=>{});
      }
    },
    text(tid,entry){
      const files=list(tid,entry),text=entry.text||'';if(!files.length)return text;
      if(entry.role==='user')return caption(text,files);
      if(entry.role!=='bot'&&entry.role!=='event')return text;
      return text.replace(/<file:([^>]+)>/g,(tag,path)=>{
        let name;try{name=decodeURIComponent(path.trim()).replace(/\\/g,'/').split('/').at(-1);}catch{return tag;}
        return files.some(item=>item.name===name||item.path===name)?'':tag;
      });
    },
    markup(tid,entry){
      return list(tid,entry).map(item=>{
        if(item.status==='expired')return `<article class="message-attachment"><div class="grow"><span class="attachment-filename">${esc(item.name)}</span><span class="attachment-meta" role="status">${esc(t('attach.expired'))}</span></div></article>`;
        const token=encodeURIComponent(key(tid,entry.id)+item.id);references.set(token,item);
        const action=item.original_url?`<button type="button" class="attachment-image${item.preview_url?'':' attachment-no-preview'}" data-open-attachment="${token}" aria-label="${esc(t('attach.view',{name:item.name}))}">${item.preview_url?`<img src="${esc(item.preview_url)}" loading="lazy" alt="${esc(t('attach.preview',{name:item.name}))}">`:esc(t('attach.open_original'))}</button>`:'';
        return `<article class="message-attachment">${action}<div class="attachment-fileline"><div class="grow"><span class="attachment-filename">${esc(item.name)}</span><span class="attachment-meta">${esc(sizeLabel(item.size))} · ${esc(item.mime)}</span></div><a class="btn btn-sm attachment-download" href="${esc(item.download_url)}" download aria-label="${esc(t('attach.download_name',{name:item.name}))}">${esc(t('attach.download'))}</a></div></article>`;
      }).join('');
    },
  };
}
