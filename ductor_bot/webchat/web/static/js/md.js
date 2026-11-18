// Only escaped text and the approved small Markdown subset are rendered.
export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}
function link(url, label) {
  // Input has already passed through esc(); never decode it into HTML again.
  if (!/^https?:\/\//i.test(url) || /[\s\\]|&(?:lt|gt|quot|#39);/i.test(url)) return null;
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return null;
  } catch { return null; }
  return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}
function urlEnd(text, start, limit, markdown = false) {
  const openings = [], pairs = { ']': '[', ')': '(', '}': '{' };
  let end = start;
  for (; end < limit; end++) {
    const character = text[end];
    if (/[\s<>"'`]/.test(character) || /^(?:&lt;|&gt;|&quot;|&#39;)/.test(text.slice(end, end + 6))
      || (!markdown && /[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]/.test(character))) break;
    if ('([{'.includes(character)) openings.push({ character, index: end });
    else if (Object.hasOwn(pairs, character)) {
      if (openings.at(-1)?.character !== pairs[character]) break;
      openings.pop();
    }
  }
  if (openings.length) end = openings[0].index;
  if (!markdown) {
    while (end > start && (/[.,!?:。！？、…]/.test(text[end - 1])
      || (text[end - 1] === ';' && !text.slice(start, end).endsWith('&amp;')))) end--;
  }
  return end;
}
function markdownStart(text, start, limit) {
  let depth = 1;
  for (let index = start + 1; index < limit; index++) {
    if (text[index] === '[') depth++;
    else if (text[index] === ']' && --depth === 0) {
      if (index > start + 1 && text[index + 1] === '(' && /^https?:\/\//i.test(text.slice(index + 2, index + 10))) {
        return { label: text.slice(start + 1, index), url: index + 2 };
      }
      break;
    }
  }
  return null;
}
function inline(value) {
  const text = esc(value), code = /(`+)(.+?)\1/gy;
  const http = /https?:\/\//giy;
  let output = '', index = 0, boldEnd = -1;
  // A token is considered only at the cursor; an earlier URL owns its entire body.
  while (index < text.length) {
    if (index === boldEnd) { output += '</b>'; boldEnd = -1; index += 2; continue; }
    const limit = boldEnd < 0 ? text.length : boldEnd;
    code.lastIndex = http.lastIndex = index;
    const codeToken = text[index] === '`' ? code.exec(text) : null;
    if (codeToken && code.lastIndex <= limit) {
      output += `<code>${codeToken[2]}</code>`; index = code.lastIndex; continue;
    }
    const markdownToken = text[index] === '[' ? markdownStart(text, index, limit) : null;
    if (markdownToken) {
      const end = urlEnd(text, markdownToken.url, limit, true);
      if (text[end] === ')') {
        output += link(text.slice(markdownToken.url, end), markdownToken.label) || text.slice(index, end + 1);
        index = end + 1; continue;
      }
    }
    if (http.test(text)) {
      const end = urlEnd(text, index, limit), url = text.slice(index, end);
      output += link(url, url) || url; index = end; continue;
    }
    // Keep the existing bold subset while code and link tokens remain indivisible.
    if (boldEnd < 0 && text.startsWith('**', index)) {
      const end = text.indexOf('**', index + 2);
      if (end > index + 2) { output += '<b>'; boldEnd = end; index += 2; continue; }
    }
    output += text[index++];
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
