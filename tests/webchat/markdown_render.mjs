// No DOM: this exercises the exact string returned before innerHTML assignment.
import fs from 'node:fs';
import {md, esc} from '../../ductor_bot/webchat/web/static/js/md.js';
import {micromark, gfm, gfmHtml} from '../../ductor_bot/webchat/web/static/js/vendor/micromark-gfm.js';

const {inputs, raw = false} = JSON.parse(fs.readFileSync(0, 'utf8'));
const output = inputs.map(text => ({
  html: md(text),
  ...(raw ? {raw: micromark(String(text), {extensions: [gfm()], htmlExtensions: [gfmHtml()]})} : {})
}));
process.stdout.write(JSON.stringify({output, escaped: esc('&<>"\'')}));
