// Only escaped text and the approved small Markdown subset are rendered.
export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}
function uri(value) {
  // cmark preserves percent escapes while UTF-8 escaping URI delimiters and text.
  return encodeURI(value).replace(/%25/g, '%');
}
function decoded(value) {
  const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
  return value.replace(/\\([!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])|&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos);/gi,
    (raw, escaped, entity) => {
      if (escaped) return escaped;
      if (entity[0] !== '#') return entities[entity];
      const number = entity[1].toLowerCase() === 'x' ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
      return number > 0 && number <= 0x10ffff && !(number >= 0xd800 && number <= 0xdfff)
        ? String.fromCodePoint(number) : '\ufffd';
    });
}
function link(url, label) {
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    return `<a href="${esc(uri(url))}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  } catch { return null; }
}
function bareEnd(text, start) {
  let end = start;
  while (end < text.length && !/[\s<\u3131-\u3163\uac00-\ud7a3]/.test(text[end])) end++;
  let body = text.slice(start, end);
  let round = [...body].filter(c => c === ')').length - [...body].filter(c => c === '(').length;
  let square = [...body].filter(c => c === ']').length - [...body].filter(c => c === '[').length;
  while (body) {
    const last = body.at(-1);
    if (/[?!.,:*_~'"]/.test(last)) body = body.slice(0, -1);
    else if (last === ')' && round > 0) { body = body.slice(0, -1); round--; }
    else if (last === ']' && square > 0) { body = body.slice(0, -1); square--; }
    else if (last === ';') body = body.replace(/&[a-z]+;$/i, '').replace(/;$/, '');
    else break;
  }
  return start + body.length;
}
function codeAt(text, start) {
  const ticks = /^`+/.exec(text.slice(start))[0];
  let cursor = start + ticks.length;
  while (cursor < text.length) {
    const next = text.indexOf('`', cursor);
    if (next < 0) break;
    const run = /^`+/.exec(text.slice(next))[0];
    if (run.length === ticks.length) {
      let body = text.slice(start + ticks.length, next).replace(/\n/g, ' ');
      if (body.startsWith(' ') && body.endsWith(' ') && /\S/.test(body)) body = body.slice(1, -1);
      return { end: next + run.length, body };
    }
    cursor = next + run.length;
  }
  return null;
}
function bracketAt(text, start) {
  let depth = 1;
  for (let index = start + 1; index < text.length; index++) {
    if (text[index] === '\\') { index++; continue; }
    if (text[index] === '`') { const code = codeAt(text, index); if (code) { index = code.end - 1; continue; } }
    if (text[index] === '[') depth++;
    else if (text[index] === ']' && --depth === 0) return index;
  }
  return text.length;
}
function destinationAt(text, start) {
  let index = start, depth = 0;
  const angle = text[index] === '<';
  if (angle) index++;
  const beginning = index;
  for (; index < text.length; index++) {
    if (text[index] === '\\' && /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/.test(text[index + 1] || '')) { index++; continue; }
    if (angle) { if (text[index] === '>') break; if (/[\n<]/.test(text[index])) return null; }
    else {
      if (/\s/.test(text[index])) return null;
      if (text[index] === '(') depth++;
      if (text[index] === ')' && depth-- === 0) break;
    }
  }
  const end = index + (angle ? 1 : 0);
  return text[end] === ')' ? { url: decoded(text.slice(beginning, index)), end: end + 1 } : null;
}
function containsLink(text) {
  for (let index = 0; index < text.length; index++) {
    if (text[index] === '\\') { index++; continue; }
    if (text[index] === '`') { const code = codeAt(text, index); if (code) { index = code.end - 1; continue; } }
    if (text[index] === '[') {
      const close = bracketAt(text, index);
      if (text[close + 1] === '(' && destinationAt(text, close + 2)) return true;
    }
  }
  return false;
}
function inline(text, autolinks = true, markdown = true) {
  let output = '', index = 0;
  const http = /https?:\/\//iy;
  while (index < text.length) {
    if (text[index] === '`') {
      const code = codeAt(text, index);
      if (code) { output += `<code>${esc(code.body)}</code>`; index = code.end; continue; }
    }
    const angle = /^<(https?:\/\/[^\s<>]*)>/i.exec(text.slice(index));
    if (autolinks && angle) { output += link(angle[1], esc(angle[1])); index += angle[0].length; continue; }
    if (markdown && text[index] === '[') {
      const close = bracketAt(text, index), label = text.slice(index + 1, close);
      const destination = text[close + 1] === '(' ? destinationAt(text, close + 2) : null;
      if (destination && (!/^https?:\/\//i.test(destination.url) || !containsLink(label))) {
        // Non-HTTP destinations are one opaque text token, including their label.
        output += /^https?:\/\//i.test(destination.url)
          ? link(destination.url, inline(label, false, false)) || esc(text.slice(index, destination.end))
          : esc(text.slice(index, destination.end));
        index = destination.end; continue;
      }
      output += '[' + inline(label, false, true) + (close < text.length ? ']' : '');
      index = Math.min(close + 1, text.length); continue;
    }
    http.lastIndex = index;
    if (autolinks && !/[a-z]/i.test(text[index - 1] || '') && http.test(text)) {
      const host = /^[^\s/?#:]+/.exec(text.slice(http.lastIndex))?.[0] || '';
      if (/^[\p{L}\p{N}]/u.test(host) && !host.split('.').slice(-2).some(part => part.includes('_'))) {
        const end = bareEnd(text, index), url = text.slice(index, end);
        output += link(url, esc(url)) || esc(url); index = end; continue;
      }
    }
    const format = /^(\*\*|__|~~|\*|_)(?=\S)(.+?)\1/.exec(text.slice(index));
    if (format && !(format[1].includes('_') && /[\p{L}\p{N}]/u.test(text[index - 1] || ''))) {
      const tag = format[1].length === 1 ? 'em' : format[1] === '~~' ? 's' : 'b';
      output += `<${tag}>${inline(format[2], autolinks, markdown)}</${tag}>`;
      index += format[0].length; continue;
    }
    const escaped = /^\\[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]|^&(?:#x[\da-f]+|#\d+|amp|lt|gt|quot|apos);/i.exec(text.slice(index));
    if (escaped) { output += esc(decoded(escaped[0])); index += escaped[0].length; continue; }
    output += esc(text[index++]);
  }
  return output;
}
function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '')
    .split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}
function tableAlignments(header, delimiter) {
  if (!header.includes('|')) return null;
  const parts = cells(delimiter);
  if (parts.length !== cells(header).length || !parts.every(cell => /^:?-+:?$/.test(cell))) return null;
  return parts.map(cell => cell.startsWith(':') ? (cell.endsWith(':') ? 'center' : 'left')
    : cell.endsWith(':') ? 'right' : null);
}
export function md(text) {
  let output = '', list = false;
  const lines = String(text).split('\n');
  function closeList() {
    if (list) { output += '</ul>'; list = false; }
  }
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence && !(fence[1][0] === '`' && fence[2].includes('`'))) {
      closeList();
      const end = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      const code = [];
      while (++index < lines.length && !end.test(lines[index])) code.push(lines[index]);
      // A closing fence follows a newline; an unfinished streaming block may not.
      output += `<pre tabindex="0"><code>${esc(code.join('\n') + (index < lines.length && code.length ? '\n' : ''))}</code></pre>`;
      continue;
    }
    const alignments = index + 1 < lines.length ? tableAlignments(line, lines[index + 1]) : null;
    if (alignments) {
      closeList();
      const row = (values, tag) => '<tr>' + alignments.map((align, column) =>
        `<${tag}${align ? ` class="align-${align}"` : ''}>${inline(values[column] || '')}</${tag}>`).join('') + '</tr>';
      output += '<div class="table-scroll" tabindex="0"><table><thead>' + row(cells(line), 'th') + '</thead><tbody>';
      index += 2;
      while (index < lines.length && lines[index].trim()
        && !/^ {0,3}(?:#{1,6}(?:\s|$)|[-+*]\s|\d+[.)]\s|>|`{3,}|~{3,}|(?:[-*_]\s*){3,}$)/.test(lines[index])) {
        output += row(cells(lines[index]), 'td');
        index++;
      }
      index--;
      output += '</tbody></table></div>';
      continue;
    }
    const escaped = inline(line);
    if (/^- /.test(line)) {
      if (!list) { output += '<ul>'; list = true; }
      output += `<li>${escaped.slice(2)}</li>`;
      continue;
    }
    closeList();
    if (/^## /.test(line)) output += `<h2>${escaped.slice(3)}</h2>`;
    else if (/^# /.test(line)) output += `<h1>${escaped.slice(2)}</h1>`;
    else if (line.trim()) output += `<p>${escaped}</p>`;
  }
  return output + (list ? '</ul>' : '');
}
