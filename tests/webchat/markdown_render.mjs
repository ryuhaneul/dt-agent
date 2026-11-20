// No DOM: this exercises the exact string returned before innerHTML assignment.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {md, esc, createMarkdownCache, createMarkdownStream} from '../../ductor_bot/webchat/web/static/js/md.js';
import {micromark, gfm, gfmHtml} from '../../ductor_bot/webchat/web/static/js/vendor/micromark-gfm.js';

const {inputs, raw = false, checks = false} = JSON.parse(fs.readFileSync(0, 'utf8'));
if (checks) {
  const cache = createMarkdownCache('topic:1');
  const note = 'A[^1]\n\n[^1]: note';
  const first = cache({seq:1,text:note});
  assert.equal(cache({seq:1,text:note}), first);
  assert.notEqual(cache({seq:2,text:note}), first);
  assert.notEqual(createMarkdownCache('topic:2')({seq:1,text:note}), first);
  assert.match(cache({seq:3,text:'costarring'}), /costarring/);
  assert.match(cache({seq:3,text:'liquid'}), /liquid/); // known FNV-1a collision
  const cases = [
    '# Header\n\n**body** https://example.test/path\n\n- item\n- next',
    'long paragraph **strong** https://example.test '.repeat(500),
    '```js\n'+'const value = "test";\n'.repeat(1000)+'```',
    '[ref][id]\n\n[id]: https://example.test/',
    'A[^1]\n\n[^1]: body',
    '- first\n\n  second paragraph\n\n- second',
    '| a | b |\n| - | - |\n| x | y |',
    '<script>bad()</script> ![image](https://image.test/a)'
  ];
  for (const source of cases) for (const size of source.length > 500 ? [137,1024] : [1,7,137,1024]) {
    const stream = createMarkdownStream('stream-test');
    for (let end=size; end<source.length; end+=size) stream.render(source.slice(0,end));
    stream.render(source);
    assert.equal(stream.render(source,true),md(source,{scope:'stream-test'}));
    assert.equal(stream.render(source,true),md(source,{scope:'stream-test'}));
  }
  const listSource='1. first\n\n    - nested\n\n2. second\n';
  const fenceSource='```text\n'+'x'.repeat(1016)+'\n# still code\nhttps://example.test/code\n';
  for(const [source,kind] of [[listSource,'ol'],[fenceSource,'pre'],[listSource.replaceAll('\n','\r'),'ol'],[fenceSource.replaceAll('\n','\r'),'pre']])for(const size of [1,137,1024]) {
    const stream=createMarkdownStream('block-context');
    for(let end=size;end<source.length;end+=size) {
      const live=stream.render(source.slice(0,end));
      if(kind==='pre'&&source.slice(0,end).includes('```text\n')){assert.equal((live.match(/<pre /g)||[]).length,1);assert.ok(!/<h[1-6]>|<a href=/.test(live));}
    }
    const live=stream.render(source);
    assert.equal(live,md(source,{scope:'block-context'}));
    assert.equal((live.match(new RegExp('<'+kind+'(?: |>)','g'))||[]).length,1);
    if(kind==='ol'){assert.ok(live.includes('<ul>'));assert.ok(!live.includes('<pre'));}
    assert.equal(stream.render(source,true),md(source,{scope:'block-context'}));
  }
  for(const [kind,indent,loose] of [['ol',0,false],['ol',3,true],['ol-one',0,true],['ol-repeat',0,true],['ul',0,false],['ul',1,true]]) {
    const source=Array.from({length:180},(_,i)=>' '.repeat(indent)+(kind.startsWith('ol')?`${kind==='ol-one'?1:kind==='ol-repeat'?i%2+1:i+7}. `:'- ')+`item ${i+7} `+'body '.repeat(12)+'\n'+(loose?'\n':'')+' '.repeat(indent+(kind.startsWith('ol')?String(kind==='ol-one'?1:kind==='ol-repeat'?i%2+1:i+7).length+3:2))+`- child ${i+7}\n`+(loose?'\n':'')).join('');
    const stream=createMarkdownStream('long-list');
    for(let end=137;end<source.length;end+=137)stream.render(source.slice(0,end));
    const live=stream.render(source);
    assert.ok(!live.includes('<pre'),'indented child lists stay lists');
    const children=live.match(/<li>\s*(?:<p>)?item [^<]*(?:<\/p>)?\s*<ul>/g)||[];
    assert.equal(children.length,180,'every child list remains inside its parent item');
    const roots=[...live.matchAll(new RegExp('<'+(kind.startsWith('ol')?'ol':'ul')+(kind.startsWith('ol')?'(?: start="(\\d+)")?':'')+'>\\s*<li>\\s*(?:<p>)?item (\\d+)','g'))];
    assert.ok(roots.length>1,'long lists commit several bounded pieces');
    if(kind.startsWith('ol'))for(const root of roots)assert.equal(Number(root[1]||1),Number(root[2])-(kind==='ol'?0:6),'ordered pieces preserve logical numbering');
    assert.equal(stream.render(source,true),md(source,{scope:'long-list'}));
  }
  const firstList=Array.from({length:100},(_,i)=>`1. item ${i+1} `+'words '.repeat(12)+'\n').join('');
  for(const separator of ['# Fresh block','---','```text\nmiddle\n```']) {
    const freshSource=firstList+separator+'\n\n'+Array.from({length:100},(_,i)=>`7. fresh ${i+7} `+'words '.repeat(12)+'\n').join('');
    const fresh=createMarkdownStream('fresh-list');for(let end=137;end<freshSource.length;end+=137)fresh.render(freshSource.slice(0,end));
    const freshLive=fresh.render(freshSource),freshRoots=[...freshLive.matchAll(/<ol(?: start="(\d+)")?>\s*<li>fresh (\d+)/g)];
    assert.ok(freshRoots.length>1);for(const root of freshRoots)assert.equal(Number(root[1]||1),Number(root[2]));
    assert.equal(fresh.render(freshSource,true),md(freshSource,{scope:'fresh-list'}));
  }
  const protectedFence='```text\n'+'code\n'.repeat(1000)+'1. literal\n- literal\n';
  const protectedStream=createMarkdownStream('protected-fence');
  for(let end=137;end<protectedFence.length;end+=137)protectedStream.render(protectedFence.slice(0,end));
  assert.equal(protectedStream.render(protectedFence),md(protectedFence,{scope:'protected-fence'}));
  const definitions=createMarkdownStream('definition');
  definitions.render('[ref][id]\n\n');
  const defined='[ref][id]\n\n[id]: https://example.test/';
  assert.equal(definitions.render(defined),md(defined,{scope:'definition'}));
  const quoted=createMarkdownStream('quoted-definition');
  quoted.render('Visit [id].\n\n');
  const quotedSource='Visit [id].\n\n> [id]: https://example.test/';
  quoted.render(quotedSource);
  assert.equal(quoted.render(quotedSource,true),md(quotedSource,{scope:'quoted-definition'}));
  const nativePerformance=globalThis.performance;
  let clock=0;
  globalThis.performance={now:()=>clock};
  try {
    for(const source of ['[guide](https://example.test/guide)','- [ ] todo','- [x] done','text [brackets]']) {
      const stream=createMarkdownStream('ordinary');stream.render(source);
      assert.equal(stream.revision,0,'ordinary links and tasks must not trigger a full refresh');
    }
    for(const prefix of ['[r]: https://example.test/','   [r]: <https://example.test/>','[^note]: body','[r]:\n  https://example.test/','> [r]: https://example.test/','- [r]: https://example.test/','> [^r]: note','```md\n[r]: https://example.test/\n```','plain ]: text']) {
      clock=0;const stream=createMarkdownStream('rate-limited');
      stream.render(prefix);assert.equal(stream.revision,1);
      let source=prefix;
      for(clock=100;clock<1000;clock+=100){source+=' appended';stream.render(source);assert.equal(stream.revision,1);}
      clock=1000;source+=' refresh';stream.render(source);assert.equal(stream.revision,2);
      clock=1100;source+=' final';assert.equal(stream.render(source,true),md(source,{scope:'rate-limited'}));
    }
  } finally {globalThis.performance=nativePerformance;}
  process.stdout.write(JSON.stringify({checks:'passed'}));
  process.exit(0);
}

const output = inputs.map(text => ({
  html: md(text),
  ...(raw ? {raw: micromark(String(text), {extensions: [gfm(), { disable: { null: ['strikethrough'] } }], htmlExtensions: [gfmHtml()]})} : {})
}));
process.stdout.write(JSON.stringify({output, escaped: esc('&<>"\'')}));
