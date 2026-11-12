// Only escaped text and the approved small Markdown subset are rendered.
export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}
export function md(text) {
  let output = '', list = false;
  for (const line of String(text).split('\n')) {
    const escaped = esc(line).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/`(.+?)`/g, '<code>$1</code>');
    if (/^- /.test(line)) {
      if (!list) { output += '<ul>'; list = true; }
      output += `<li>${escaped.slice(2)}</li>`;
      continue;
    }
    if (list) { output += '</ul>'; list = false; }
    if (/^## /.test(line)) output += `<h2>${escaped.slice(3)}</h2>`;
    else if (/^# /.test(line)) output += `<h1>${escaped.slice(2)}</h1>`;
    else if (line.trim()) output += `<p>${escaped}</p>`;
  }
  return output + (list ? '</ul>' : '');
}
