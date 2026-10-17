/**
 * Dependency-free Markdown subset for assistant messages.
 *
 * Security contract: the parser returns plain-data tokens only, and the DOM
 * builder below assigns every fragment through textContent.  No branch ever
 * touches innerHTML, so markup in the source text can only appear verbatim.
 */

const FENCE_RE = /^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9+#._-]{0,32})\s*$/;
const ULIST_RE = /^\s*[-*+]\s+(.*)$/;
const OLIST_RE = /^\s*\d{1,9}[.)]\s+(.*)$/;

/** Only http/https links may become anchors; anything else stays literal. */
export function safeLinkHref(value) {
  const href = String(value || '').trim();
  if (!href || href.length > 2048 || /[\u0000-\u001f\u007f\\"']/.test(href)) return '';
  return /^https?:\/\/\S+$/i.test(href) ? href : '';
}

/** Parse inline emphasis/code/links into plain-data tokens. */
export function parseInline(text) {
  const source = String(text ?? '');
  const tokens = [];
  // Order matters: inline code protects its body from further parsing.
  const pattern = /(`+)([^`]+)\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|(?<![A-Za-z0-9])\*([^*\n]+)\*(?![A-Za-z0-9])|(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])|\[([^\]\n]{1,500})\]\(([^)\s]{1,2048})\)/g;
  let cursor = 0;
  let match;
  const pushText = (value) => { if (value) tokens.push({ type: 'text', value }); };
  while ((match = pattern.exec(source)) !== null) {
    pushText(source.slice(cursor, match.index));
    if (match[1]) tokens.push({ type: 'code', value: match[2] });
    else if (match[3] !== undefined) tokens.push({ type: 'bold', value: match[3] });
    else if (match[4] !== undefined) tokens.push({ type: 'bold', value: match[4] });
    else if (match[5] !== undefined) tokens.push({ type: 'italic', value: match[5] });
    else if (match[6] !== undefined) tokens.push({ type: 'italic', value: match[6] });
    else {
      const href = safeLinkHref(match[8]);
      if (href) tokens.push({ type: 'link', href, value: match[7] });
      else pushText(match[0]); // unsafe or malformed link target: keep it literal
    }
    cursor = match.index + match[0].length;
  }
  pushText(source.slice(cursor));
  return mergeAdjacentText(tokens);
}

function mergeAdjacentText(tokens) {
  const merged = [];
  tokens.forEach((token) => {
    const last = merged.at(-1);
    if (token.type === 'text' && last?.type === 'text') last.value += token.value;
    else merged.push(token);
  });
  return merged;
}

/**
 * Split a message into blocks: fenced code, ordered/unordered lists and
 * paragraphs.  Single newlines inside a paragraph are preserved as breaks.
 */
export function parseMarkdown(text) {
  const lines = String(text ?? '').split('\n');
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const marker = fence[2];
      const closing = new RegExp('^\\s*' + (marker[0] === '`' ? '`' : '~') + '{' + marker.length + ',}\\s*$');
      const body = [];
      index += 1;
      while (index < lines.length && !closing.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // consume the closing fence (or run off the end)
      blocks.push({ type: 'code', lang: fence[3] || '', text: body.join('\n') });
      continue;
    }
    const unordered = ULIST_RE.exec(line);
    const ordered = OLIST_RE.exec(line);
    if (unordered || ordered) {
      const isOrdered = Boolean(ordered);
      const items = [];
      while (index < lines.length) {
        const item = (isOrdered ? OLIST_RE : ULIST_RE).exec(lines[index]);
        if (!item) break;
        items.push(parseInline(item[1]));
        index += 1;
      }
      blocks.push({ type: 'list', ordered: isOrdered, items });
      continue;
    }
    if (!line.trim()) { index += 1; continue; }
    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !FENCE_RE.test(lines[index]) && !ULIST_RE.test(lines[index]) && !OLIST_RE.test(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ type: 'paragraph', inlines: paragraph.map(parseInline) });
  }
  return blocks;
}

/**
 * Append rendered blocks to `container` using only createElement/textContent.
 * `mapText(value)` may expand a plain-text fragment into extra nodes (the PWA
 * uses it to upgrade sticker tokens); it must return DOM nodes.
 */
export function appendMarkdown(container, blocks, { mapText, onCopy } = {}) {
  const doc = container.ownerDocument;
  const textNodes = (value, parent) => {
    const nodes = mapText ? mapText(value) : [doc.createTextNode(value)];
    nodes.forEach((node) => parent.append(node));
  };
  const appendInline = (tokens, parent) => {
    tokens.forEach((token) => {
      if (token.type === 'text') return textNodes(token.value, parent);
      if (token.type === 'code') { const code = doc.createElement('code'); code.className = 'inline-code'; code.textContent = token.value; return parent.append(code); }
      if (token.type === 'bold') { const strong = doc.createElement('strong'); textNodes(token.value, strong); return parent.append(strong); }
      if (token.type === 'italic') { const em = doc.createElement('em'); textNodes(token.value, em); return parent.append(em); }
      if (token.type === 'link') {
        const link = doc.createElement('a');
        link.href = token.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.className = 'message-link';
        textNodes(token.value, link);
        return parent.append(link);
      }
      return textNodes('', parent);
    });
  };
  blocks.forEach((block) => {
    if (block.type === 'code') {
      const wrap = doc.createElement('div'); wrap.className = 'code-block';
      const head = doc.createElement('div'); head.className = 'code-block-head';
      const lang = doc.createElement('span'); lang.className = 'code-block-lang'; lang.textContent = block.lang || 'code';
      const copy = doc.createElement('button'); copy.type = 'button'; copy.className = 'code-copy'; copy.textContent = '复制';
      copy.setAttribute('aria-label', '复制代码块内容');
      copy.addEventListener('click', async () => {
        const ok = await copyText(block.text);
        copy.textContent = ok ? '已复制' : '复制失败';
        onCopy?.(ok);
        setTimeout(() => { copy.textContent = '复制'; }, 1600);
      });
      head.append(lang, copy);
      const pre = doc.createElement('pre'); const code = doc.createElement('code'); code.textContent = block.text; pre.append(code);
      wrap.append(head, pre);
      container.append(wrap);
      return;
    }
    if (block.type === 'list') {
      const list = doc.createElement(block.ordered ? 'ol' : 'ul'); list.className = 'message-md-list';
      block.items.forEach((item) => { const li = doc.createElement('li'); appendInline(item, li); list.append(li); });
      container.append(list);
      return;
    }
    const paragraph = doc.createElement('p'); paragraph.className = 'message-md-paragraph';
    block.inlines.forEach((inlineLine, lineIndex) => {
      if (lineIndex > 0) paragraph.append(doc.createElement('br'));
      appendInline(inlineLine, paragraph);
    });
    container.append(paragraph);
  });
  return container;
}

/** Clipboard with a legacy fallback; resolves false instead of throwing. */
export async function copyText(text) {
  try {
    if (globalThis.navigator?.clipboard?.writeText) { await globalThis.navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through to the legacy path */ }
  try {
    const doc = globalThis.document;
    if (!doc) return false;
    const area = doc.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.style.position = 'fixed'; area.style.opacity = '0';
    doc.body.append(area); area.select();
    const ok = doc.execCommand?.('copy') === true;
    area.remove();
    return ok;
  } catch { return false; }
}
