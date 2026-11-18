import {micromark, gfm, gfmHtml} from './vendor/micromark-gfm.js';

export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

function attribute(tag, name) {
  return new RegExp(` ${name}="([^"]*)"`).exec(tag)?.[1] || '';
}

// micromark has already filtered protocols. Only absolute destinations and
// fragments survive the display policy; relative and empty links become text.
function linked(href) {
  return /^(?:[a-z][a-z\d+.-]*:|#)/i.test(href);
}

export function md(text) {
  const html = micromark(String(text), {
    extensions: [gfm()], htmlExtensions: [gfmHtml()]
  });
  const anchors = [];
  // This scans only micromark-generated tags: source HTML is escaped by default.
  // Replace images before any DOM parsing, so no image request can be started.
  return html.replace(/<[^>]*>/g, tag => {
    if (tag.startsWith('<a ')) {
      const keep = linked(attribute(tag, 'href')) && !anchors.includes('a');
      // Footnote backreferences still need the generated id after unwrapping.
      const id = attribute(tag, 'id'), replacement = keep ? 'a' : id ? 'span' : '';
      anchors.push(replacement);
      return keep ? tag.slice(0, -1) + ' target="_blank" rel="noopener noreferrer">'
        : id ? `<span id="${id}">` : '';
    }
    if (tag === '</a>') {
      const replacement = anchors.pop();
      return replacement ? `</${replacement}>` : '';
    }
    if (tag.startsWith('<img ')) {
      const alt = attribute(tag, 'alt'), href = attribute(tag, 'src');
      if (anchors.length || !linked(href)) return alt;
      const title = attribute(tag, 'title');
      return `<a href="${href}"${title ? ` title="${title}"` : ''} target="_blank" rel="noopener noreferrer">${alt}</a>`;
    }
    if (tag === '<pre>') return '<pre tabindex="0">';
    return tag.replace(/ align="(left|center|right)"/, ' class="align-$1"');
  }).replace(/<table>([\s\S]*?)<\/table>/g, (_table, body) => {
    const header = /<tr>([\s\S]*?)<\/tr>/.exec(body)?.[1] || '';
    const columns = (header.match(/<th(?: |>)/g) || []).length;
    return `<div class="table-scroll" tabindex="0"><table><colgroup><col span="${columns}"></colgroup>${body}</table></div>`;
  });
}
