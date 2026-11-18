// Only escaped text and the approved small Markdown subset are rendered.
export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}
function inline(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
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
      output += `<div class="table-scroll" tabindex="0"><table><colgroup><col span="${alignments.length}"></colgroup><thead>` + row(cells(line), 'th') + '</thead><tbody>';
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
