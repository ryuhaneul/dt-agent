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

export function md(text, { scope = null } = {}) {
  const prefix = scope === null ? 'user-content-' : `dtw-${encodeURIComponent(scope)}-`;
  const html = micromark(String(text), {
    extensions: [gfm()], htmlExtensions: [gfmHtml({ clobberPrefix: prefix })]
  });
  const anchors = [];
  // This scans only micromark-generated tags: source HTML is escaped by default.
  // Replace images before any DOM parsing, so no image request can be started.
  return html.replace(/<[^>]*>/g, tag => {
    // The extension exposes clobberPrefix for footnotes but fixes the label id.
    if (scope !== null) tag = tag.replace(/(id|aria-describedby)="footnote-label"/g, `$1="${prefix}footnote-label"`);
    if (tag.startsWith('<a ')) {
      const keep = linked(attribute(tag, 'href')) && !anchors.includes('a');
      // Footnote backreferences still need the generated id after unwrapping.
      const id = attribute(tag, 'id'), replacement = keep ? 'a' : id ? 'span' : '';
      anchors.push(replacement);
      return keep ? attribute(tag, 'href').startsWith('#') ? tag
        : tag.slice(0, -1) + ' target="_blank" rel="noopener noreferrer">'
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
      return `<a href="${href}"${title ? ` title="${title}"` : ''}${href.startsWith('#') ? '' : ' target="_blank" rel="noopener noreferrer"'}>${alt}</a>`;
    }
    if (tag === '<pre>') return '<pre tabindex="0">';
    return tag.replace(/ align="(left|center|right)"/, ' class="align-$1"');
  }).replace(/<table>([\s\S]*?)<\/table>/g, (_table, body) => {
    const header = /<tr>([\s\S]*?)<\/tr>/.exec(body)?.[1] || '';
    const columns = (header.match(/<th(?: |>)/g) || []).length;
    return `<div class="table-scroll" tabindex="0"><table><colgroup><col span="${columns}"></colgroup>${body}</table></div>`;
  });
}

// One current rendering per sequence; verify text as well as its hash so hash
// collisions cannot reuse stale markup. The history owns this cache's lifetime.
export function createMarkdownCache(scope) {
  const entries = new Map();
  return entry => {
    const text = String(entry.text || '');
    let hash = 2166136261;
    for (let index = 0; index < text.length; index++) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
    const cached = entries.get(entry.seq);
    if (cached?.hash === hash && cached.text === text) return cached.html;
    const html = md(text, { scope: `${scope}:${entry.seq}` });
    entries.set(entry.seq, { hash, text, html });
    return html;
  };
}

// Blank lines are an approximate streaming boundary. Definitions can affect
// earlier text, so refresh their complete rendering at most once per second.
// Every terminal state is always rendered from the complete source.
export function createMarkdownStream(scope) {
  let previous = '', output = '', scanned = 0, boundary = 0, complete = '', fence = null;
  let definitions = false, finalized = false, tail = '', revision = 0, lastRefresh = -Infinity;
  return {
    get revision() { return revision; },
    get completeHTML() { return complete; },
    get tailHTML() { return tail; },
    render(value, final = false) {
      const text = String(value);
      if (text === previous && (!final || finalized)) return output;
      if (!text.startsWith(previous)) {
        scanned = boundary = 0; complete = ''; fence = null; definitions = false;
        revision++;
      }
      if (final) {
        output = md(text, { scope }); finalized = true;
      } else {
        let end;
        while ((end = text.indexOf('\n', scanned)) !== -1) {
          const line = text.slice(scanned, end).replace(/\r$/, '');
          const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
          if (fence) {
            if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
          } else if (marker && !(marker[1][0] === '`' && marker[2].includes('`'))) {
            fence = marker[1];
          } else {
            if (!line.trim()) {
              complete += md(text.slice(boundary, end + 1), { scope });
              boundary = end + 1;
            }
          }
          scanned = end + 1;
        }
        definitions = text.includes(']:');
        const now = definitions ? performance.now() : 0;
        if (definitions && now - lastRefresh >= 1000) {
          complete = md(text, { scope }); tail = ''; boundary = text.length;
          lastRefresh = now; revision++;
        } else {
          while (text.length - boundary > 1024) {
            complete += md(text.slice(boundary, boundary + 1024), { scope });
            boundary += 1024;
          }
          tail = md(text.slice(boundary), { scope });
        }
        output = complete + tail;
        finalized = false;
      }
      previous = text;
      return output;
    }
  };
}

// Fragment navigation stays within its message and never enters the app router.
export function initMarkdownLinks(root) {
  root.addEventListener('click', event => {
    const anchor = event.target.closest('a[href^="#"]');
    if (!anchor || !root.contains(anchor)) return;
    const message = anchor.closest('.md');
    if (!message) return;
    event.preventDefault();
    const id = anchor.getAttribute('href').slice(1);
    const target = [...message.querySelectorAll('[id]')].find(element => element.id === id);
    if (!target) return;
    target.setAttribute('tabindex', '-1');
    target.scrollIntoView({ block: 'center' }); target.focus({ preventScroll: true });
  });
}
