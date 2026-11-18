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
function bareLinks(text) {
  const pattern = /https?:\/\/(?:(?!&(?:lt|gt|quot|#39);)[^\s<>"'`\u1100-\u11ff\u3130-\u318f\uac00-\ud7af])+/gi;
  let output = '', offset = 0, match;
  while ((match = pattern.exec(text))) {
    let url = match[0];
    const brackets = [];
    for (let index = 0; index < url.length; index++) {
      if (url[index] === '[') brackets.push(index);
      else if (url[index] === ']') {
        if (!brackets.length) { url = url.slice(0, index); break; }
        brackets.pop();
      }
    }
    if (brackets.length) url = url.slice(0, brackets[0]);
    // Preserve balanced URL parentheses, but leave surrounding prose outside.
    while (true) {
      const previous = url;
      url = url.replace(/[.,!?:。！？、…]+$/, '');
      if (url.endsWith(';') && !url.endsWith('&amp;')) url = url.slice(0, -1);
      for (const [open, close] of [['(', ')'], ['{', '}']]) {
        if (url.endsWith(close) && url.split(close).length > url.split(open).length) url = url.slice(0, -1);
      }
      if (url === previous) break;
    }
    output += text.slice(offset, match.index) + (link(url, url) || url);
    // Resume at the actual URL boundary so a following URL remains discoverable.
    offset = pattern.lastIndex = match.index + url.length;
  }
  return output + text.slice(offset);
}
function inlineEscaped(text) {
  // Confirm Markdown/code tokens before looking for bare URLs in intervening text.
  const tokens = /(`+)(.+?)\1|\[([^\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)|\*\*(.+?)\*\*/g;
  let output = '', offset = 0;
  for (const match of text.matchAll(tokens)) {
    const [raw, ticks, code, label, destination, bold] = match;
    output += bareLinks(text.slice(offset, match.index));
    if (ticks) output += `<code>${code}</code>`;
    else if (label !== undefined) output += link(destination, label) || raw;
    else output += `<b>${inlineEscaped(bold)}</b>`;
    offset = match.index + raw.length;
  }
  return output + bareLinks(text.slice(offset));
}
function inline(text) {
  return inlineEscaped(esc(text));
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
