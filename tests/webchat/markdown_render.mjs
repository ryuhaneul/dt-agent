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
  ...(raw ? {raw: micromark(String(text), {extensions: [gfm()], htmlExtensions: [gfmHtml()]})} : {})
}));
process.stdout.write(JSON.stringify({output, escaped: esc('&<>"\'')}));
