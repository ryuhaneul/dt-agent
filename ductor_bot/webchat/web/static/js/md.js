import {micromark, gfm, gfmHtml, sanitizeUri, normalizeIdentifier} from './vendor/micromark-gfm.js';

export function esc(value) {
  return String(value).replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

const hrefProtocols = /^(https?|ircs?|mailto|xmpp)$/i;
const imageProtocols = /^https?$/i;
function linked(href) { return /^(?:[a-z][a-z\d+.-]*:|#)/i.test(href); }

const userLineEnding = {
  tokenize(effects, ok, nok) {
    return code => {
      const construct = this.parser.constructs.text[code].find(item => item.name === 'lineEnding');
      return effects.attempt(construct, ok, nok)(code);
    };
  },
  resolve(events) {
    events[0][1].userLineEnding = true;
    return events;
  },
  resolveAll(events) {
    for (let index = 1; index < events.length; index++) {
      const [phase, token] = events[index];
      const previous = events[index - 1][1].type;
      if (phase === 'enter' && token.userLineEnding && previous.startsWith('hardBreak')) {
        token.userLineEnding = false;
      }
    }
    return events;
  },
};
const userLineBreaks = { text: { [-5]: userLineEnding, [-4]: userLineEnding, [-3]: userLineEnding } };

// Compose nested media from tokens, before emitting HTML. No generated attributes
// are parsed again, and images never become browser image elements.
function displayHtml(prefix, scoped, orderedStart, preserveLineBreaks) {
  const stack = [];
  const base = gfmHtml({ clobberPrefix: prefix });
  const labelId = scoped ? `${prefix}footnote-label` : 'footnote-label';
  const inImage = () => stack.some(media => media.image);
  function render(parts, inside = false, depth = 0, alt = false) {
    return parts.map(part => {
      if (typeof part === 'string') return part;
      if (part.footnote) {
        if (alt) return '';
        const id = `${prefix}fnref-${part.id}${part.reuse > 1 ? `-${part.reuse}` : ''}`;
        return inside ? `<sup><span id="${id}">${part.number}</span></sup>`
          : `<sup><a href="#${prefix}fn-${part.id}" id="${id}" data-footnote-ref="" aria-describedby="${labelId}">${part.number}</a></sup>`;
      }
      const href = sanitizeUri(part.destination, part.image ? imageProtocols : hrefProtocols);
      const keep = !alt && linked(href) && !inside && !(part.image && depth);
      const content = render(part.parts, inside || keep, depth + 1, alt || part.image);
      if (!keep) return content;
      return `<a href="${href}"${part.title ? ` title="${part.title}"` : ''}${href.startsWith('#') ? '' : ' target="_blank" rel="noopener noreferrer"'}>${content}</a>`;
    }).join('');
  }
  function append(part) {
    if (stack.length) {
      const parent = stack.at(-1);
      parent.parts.push(this.resume(), part);
      this.buffer();
    } else this.tag(render([part]));
  }
  function enterMedia(token) { stack.push({ image: token.type === 'image', parts: [] }); }
  function exitMedia() {
    const media = stack.pop();
    if (media.destination === undefined) {
      const definition = this.getData('definitions')[normalizeIdentifier(media.referenceId || media.labelId)];
      media.destination = definition.destination; media.title = definition.title;
    }
    append.call(this, media);
  }
  function destinationEnter() { this.buffer(); this.setData('ignoreEncode', true); }
  function destinationExit() { stack.at(-1).destination = this.resume(); this.setData('ignoreEncode'); }
  function autolink(token, protocol = '') {
    const text = this.sliceSerialize(token);
    append.call(this, { destination: protocol + text, parts: [this.encode(text)] });
  }
  function cell(name) {
    const alignments = this.getData('tableAlign'), column = this.getData('tableColumn');
    const alignment = alignments[column];
    if (alignment === undefined) this.buffer();
    else { this.lineEndingIfNeeded(); this.tag(`<${name}${alignment === 'none' ? '' : ` class="align-${alignment}"`}>`); }
  }
  const enter = {
    link: enterMedia, image: enterMedia,
    resource() { this.buffer(); stack.at(-1).destination = ''; },
    resourceDestinationString: destinationEnter,
    codeFenced() { this.lineEndingIfNeeded(); this.tag('<pre tabindex="0"><code'); this.setData('fencesCount', 0); },
    codeIndented() { this.lineEndingIfNeeded(); this.tag('<pre tabindex="0"><code>'); },
    table(token) {
      this.lineEndingIfNeeded();
      this.tag(`<div class="table-scroll" tabindex="0"><table><colgroup><col span="${token._align.length}"></colgroup>`);
      this.setData('tableAlign', token._align);
    },
    tableData() { cell.call(this, 'td'); },
    tableHeader() { cell.call(this, 'th'); },
    codeText() { this.setData('inCodeText', true); if (!inImage()) this.tag('<code>'); }
  };
  if (orderedStart !== null) enter.listItemValue = function (token) {
    if (this.getData('expectFirstItem')) {
      const value = orderedStart === null ? Number.parseInt(this.sliceSerialize(token), 10) : orderedStart;
      orderedStart = null;
      if (value !== 1) this.tag(` start="${this.encode(String(value))}"`);
    }
  };
  const exit = {
    link: exitMedia, image: exitMedia,
    labelText(token) { stack.at(-1).labelId = this.sliceSerialize(token); },
    label() { stack.at(-1).parts.push(this.resume()); },
    referenceString(token) { stack.at(-1).referenceId = this.sliceSerialize(token); },
    resourceDestinationString: destinationExit,
    resourceTitleString() { stack.at(-1).title = this.resume(); },
    autolinkProtocol: autolink,
    autolinkEmail(token) { autolink.call(this, token, 'mailto:'); },
    literalAutolinkHttp: autolink,
    literalAutolinkWww(token) { autolink.call(this, token, 'http://'); },
    literalAutolinkEmail(token) { autolink.call(this, token, 'mailto:'); },
    codeText() { this.setData('inCodeText'); if (!inImage()) this.tag('</code>'); },
    hardBreakEscape() { if (!inImage()) this.tag('<br />'); },
    hardBreakTrailing() { if (!inImage()) this.tag('<br />'); },
    table() { base.exit.table.call(this); this.tag('</div>'); },
    tableRow() {
      const alignments = this.getData('tableAlign');
      let column = this.getData('tableColumn');
      while (column < alignments.length) {
        this.lineEndingIfNeeded();
        const align = alignments[column++];
        this.tag(`<td${align === 'none' ? '' : ` class="align-${align}"`}></td>`);
      }
      this.setData('tableColumn', column); this.lineEndingIfNeeded(); this.tag('</tr>');
    },
    gfmFootnoteCallString(token) {
      let calls = this.getData('gfmFootnoteCallOrder'), counts = this.getData('gfmFootnoteCallCounts');
      const id = normalizeIdentifier(this.sliceSerialize(token));
      this.resume();
      if (!calls) this.setData('gfmFootnoteCallOrder', calls = []);
      if (!counts) this.setData('gfmFootnoteCallCounts', counts = {});
      let index = calls.indexOf(id);
      if (index === -1) { calls.push(id); counts[id] = 1; index = calls.length - 1; }
      else counts[id]++;
      append.call(this, { footnote: true, id: sanitizeUri(id.toLowerCase()), reuse: counts[id], number: index + 1 });
    },
    null() {
      const calls = this.getData('gfmFootnoteCallOrder') || [], counts = this.getData('gfmFootnoteCallCounts') || {};
      const definitions = this.getData('gfmFootnoteDefinitions') || {};
      if (!calls.length) return;
      this.lineEndingIfNeeded();
      this.tag(`<section data-footnotes="" class="footnotes"><h2 id="${labelId}" class="sr-only">`);
      this.raw(this.encode('Footnotes')); this.tag('</h2>'); this.lineEndingIfNeeded(); this.tag('<ol>');
      calls.forEach((id, index) => {
        const safeId = sanitizeUri(id.toLowerCase()), references = [];
        for (let reuse = 1; reuse <= counts[id]; reuse++) {
          const suffix = reuse > 1 ? `-${reuse}` : '';
          references.push(`<a href="#${prefix}fnref-${safeId}${suffix}" data-footnote-backref="" aria-label="Back to reference ${index + 1}${suffix}" class="data-footnote-backref">↩${reuse > 1 ? `<sup>${reuse}</sup>` : ''}</a>`);
        }
        const reference = references.join(' ');
        let injected = false;
        this.lineEndingIfNeeded(); this.tag(`<li id="${prefix}fn-${safeId}">`); this.lineEndingIfNeeded();
        // Match the upstream GFM footer's paragraph placement, without reading attributes.
        this.tag(definitions[id].replace(/<\/p>(?:\r?\n|\r)?$/, ending => { injected = true; return ' ' + reference + ending; }));
        if (!injected) { this.lineEndingIfNeeded(); this.tag(reference); }
        this.lineEndingIfNeeded(); this.tag('</li>');
      });
      this.lineEndingIfNeeded(); this.tag('</ol>'); this.lineEndingIfNeeded(); this.tag('</section>');
    }
  };
  for (const [token, tag] of Object.entries({ emphasis: 'em', strong: 'strong' })) {
    enter[token] = function () { if (!inImage()) this.tag(`<${tag}>`); };
    exit[token] = function () { if (!inImage()) this.tag(`</${tag}>`); };
  }
  if (preserveLineBreaks) {
    let inSetextHeading = false;
    enter.setextHeadingText = () => { inSetextHeading = true; };
    exit.setextHeadingText = function () {
      inSetextHeading = false;
      this.setData('slurpAllLineEndings', true);
    };
    enter.lineEnding = function (token) {
      if (token.userLineEnding && !inSetextHeading && !inImage()) this.tag('<br />');
    };
  }
  return [base, { enter, exit }];
}

export function md(text, { scope = null, orderedStart = null, preserveLineBreaks = false } = {}) {
  const prefix = scope === null ? 'user-content-' : `dtw-${encodeURIComponent(scope)}-`;
  const extensions = [gfm(), { disable: { null: ['strikethrough'] } }];
  if (preserveLineBreaks) extensions.push(userLineBreaks);
  return micromark(String(text), {
    extensions,
    htmlExtensions: displayHtml(prefix, scope !== null, orderedStart, preserveLineBreaks),
  });
}

// One current rendering per sequence; verify text as well as its hash so hash
// collisions cannot reuse stale markup. The history owns this cache's lifetime.
export function createMarkdownCache(scope) {
  const entries = new Map();
  return entry => {
    const text = String(entry.text || '');
    const preserveLineBreaks = entry.role === 'user';
    let hash = 2166136261;
    for (let index = 0; index < text.length; index++) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
    const cached = entries.get(entry.seq);
    if (cached?.hash === hash && cached.text === text && cached.preserveLineBreaks === preserveLineBreaks) return cached.html;
    const html = md(text, { scope: `${scope}:${entry.seq}`, preserveLineBreaks });
    entries.set(entry.seq, { hash, text, preserveLineBreaks, html });
    return html;
  };
}

// Blank lines are an approximate streaming boundary. Definitions can affect
// earlier text, so refresh their complete rendering at most once per second.
// Every terminal state is always rendered from the complete source.
export function createMarkdownStream(scope) {
  let previous = '', output = '', scanned = 0, boundary = 0, complete = '', fence = null;
  let definitions = false, finalized = false, tail = '', revision = 0, lastRefresh = -Infinity, pending = 0, listIndent = null, listKind = null, listStart = null, listCount = 0, chunkStart = null, afterBlankAt = -1;
  const part = value => md(value, { scope, orderedStart: chunkStart });
  return {
    get revision() { return revision; },
    get completeHTML() { return complete; },
    get tailHTML() { return tail; },
    render(value, final = false) {
      const text = String(value);
      if (text === previous && (!final || finalized)) return output;
      if (!text.startsWith(previous)) {
        scanned = boundary = pending = 0; complete = ''; fence = null; definitions = false; listIndent = listKind = listStart = chunkStart = null; listCount = 0; afterBlankAt = -1;
        revision++;
      }
      if (final) {
        output = md(text, { scope }); finalized = true;
      } else {
        const continuation = /^(?:[ \t]|(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)|>)/;
        function confirm(line, partial = false) {
          if (!pending || fence || !line.trim() || (partial && /^\d{1,9}$/.test(line))) return;
          if (continuation.test(line)) { if (partial) afterBlankAt = scanned; pending = 0; return; }
          complete += part(text.slice(boundary, pending));
          boundary = pending; pending = 0; listIndent = listKind = listStart = chunkStart = null; listCount = 0;
        }
        let end;
        while ((end = text.indexOf('\n', scanned)) !== -1) {
          const line = text.slice(scanned, end).replace(/\r$/, '');
          const afterBlank = pending > 0 || afterBlankAt === scanned;
          confirm(line);
          const block = !fence && /^( {0,3})(?:#{1,6}(?:[ \t]|$)|>|`{3,}|~{3,}|([-*_])(?:[ \t]*\2){2,}[ \t]*$)/.exec(line);
          if (block && listIndent !== null && block[1].length <= listIndent) { listIndent = listKind = listStart = null; listCount = 0; }
          const item = !fence && !block && /^( {0,3})([-+*]|\d{1,9}[.)])(?:[ \t]+|$)/.exec(line);
          if (item && (listIndent !== null || afterBlank || scanned === boundary)) {
            const indent = item[1].length;
            listIndent = listIndent === null ? indent : Math.min(listIndent, indent);
            if (indent === listIndent) {
              const kind = item[2].at(-1);
              if (kind !== listKind) { listKind = kind; listStart = /^\d/.test(item[2]) ? Number.parseInt(item[2], 10) : null; listCount = 0; }
              // Keep children with their parent and continue logical ordered numbering.
              if (scanned - boundary > 4096) {
                complete += part(text.slice(boundary, scanned));
                boundary = scanned; pending = 0; chunkStart = listStart === null ? null : listStart + listCount;
              }
              listCount++;
            }
          }
          const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
          if (fence) {
            if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
          } else if (marker && !(marker[1][0] === '`' && marker[2].includes('`'))) {
            fence = marker[1];
          } else {
            if (!line.trim()) {
              pending = end + 1;
            }
          }
          scanned = end + 1;
        }
        confirm(text.slice(scanned), true);
        const unfinished = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text.slice(scanned));
        const openFence = fence || (unfinished && !(unfinished[1][0] === '`' && unfinished[2].includes('`')));
        definitions = text.includes(']:');
        const now = definitions ? performance.now() : 0;
        if (definitions && now - lastRefresh >= 1000) {
          complete = md(text, { scope }); tail = ''; boundary = text.length; pending = 0; chunkStart = null;
          lastRefresh = now; revision++;
        } else {
          // Only a single plain paragraph can use provisional length chunks.
          // Multiline blocks retain their parser context until a safe blank boundary.
          const paragraph = !openFence && !/[\r\n]/.test(text.slice(boundary)) && !/^(?:[ \t>#`~]|(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$))/.test(text.slice(scanned));
          while (paragraph && text.length - boundary > 1024) {
            complete += md(text.slice(boundary, boundary + 1024), { scope });
            boundary += 1024;
          }
          tail = part(text.slice(boundary));
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
