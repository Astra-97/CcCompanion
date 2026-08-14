const MAX_NAME = 80;
const MAX_STICKERS_PER_MESSAGE = 48;
const TRUSTED_ASSET_ORIGINS = new Set(['https://test.xiaonancaleb.xyz']);

export function isSafeStickerName(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= MAX_NAME && value === value.normalize('NFC') && value === value.trim() && !/[\[\]:/\\?#%\u0000-\u001f\u007f]/u.test(value);
}

export function normalizeStickerCatalog(raw = {}) {
  const categories = Array.isArray(raw.categories) ? raw.categories.slice(0, 128).map((item) => {
    const id = typeof item?.id === 'string' && /^[a-z0-9](?:[a-z0-9_-]{0,46}[a-z0-9])?$/.test(item.id) ? item.id : '';
    return id && isSafeStickerName(item.name) ? { id, name: item.name } : null;
  }).filter(Boolean) : [];
  const categoryIds = new Set(categories.map(({ id }) => id));
  const seen = new Set();
  const stickers = Array.isArray(raw.stickers) ? raw.stickers.slice(0, 512).map((item) => {
    if (!isSafeStickerName(item?.name) || seen.has(item.name)) return null;
    let url; try { url = new URL(item.url); } catch { return null; }
    if (url.protocol !== 'https:' || !TRUSTED_ASSET_ORIGINS.has(url.origin) || url.username || url.password || url.search || url.hash
        || /[\\\u0000-\u001f\u007f]/u.test(url.pathname) || !/^\/(?:[^/]+\/)*[^/]+\.(?:png|jpe?g|gif|webp)$/iu.test(url.pathname)) return null;
    const categoryId = categoryIds.has(item.category_id) ? item.category_id : '__uncategorized';
    seen.add(item.name); return { name: item.name, label: isSafeStickerName(item.label) ? item.label : item.name, url: url.href, categoryId };
  }).filter(Boolean) : [];
  const pickerCategories = [...categories];
  if (stickers.some(({ categoryId }) => categoryId === '__uncategorized')) pickerCategories.push({ id: '__uncategorized', name: '未分类' });
  const upload = raw.upload && typeof raw.upload === 'object' ? {
    supported: raw.upload.supported === true,
    maxFileBytes: Number.isInteger(raw.upload.max_file_bytes) && raw.upload.max_file_bytes > 0 && raw.upload.max_file_bytes <= 8 * 1024 * 1024 ? raw.upload.max_file_bytes : 8 * 1024 * 1024,
    contentTypes: Array.isArray(raw.upload.content_types) ? raw.upload.content_types.filter((type) => ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(type)) : [],
    maxNameChars: raw.upload.max_name_chars === 80 ? 80 : 80,
  } : { supported: false, maxFileBytes: 8 * 1024 * 1024, contentTypes: [], maxNameChars: 80 };
  return { version: typeof raw.version === 'string' ? raw.version.slice(0, 128) : '', categories: pickerCategories, stickers, upload };
}

export function insertStickerToken(text, start, end, name) {
  if (!isSafeStickerName(name)) return { text, cursor: Math.max(0, Math.min(text.length, start || 0)) };
  const left = Math.max(0, Math.min(text.length, Number(start) || 0)); const right = Math.max(left, Math.min(text.length, Number(end) || left)); const token = `[bqb:${name}]`;
  return { text: text.slice(0, left) + token + text.slice(right), cursor: left + token.length };
}

export function stickerTokens(text, catalog) {
  const byName = new Map((catalog?.stickers || []).map((item) => [item.name, item])); const result = []; const pattern = /\[bqb:([^\]\r\n]{1,80})\]/gu; let match;
  while ((match = pattern.exec(String(text || ''))) && result.length < MAX_STICKERS_PER_MESSAGE) if (byName.has(match[1])) result.push({ ...byName.get(match[1]), token: match[0], index: match.index });
  return result;
}

export function parseStickerParts(text, catalog) {
  const source = String(text || ''); const tokens = stickerTokens(source, catalog); const parts = []; let cursor = 0;
  tokens.forEach((item) => {
    if (item.index > cursor) parts.push({ type: 'text', value: source.slice(cursor, item.index) });
    parts.push({ type: 'sticker', sticker: item }); cursor = item.index + item.token.length;
  });
  if (cursor < source.length || !parts.length) parts.push({ type: 'text', value: source.slice(cursor) });
  return parts;
}

export function removeStickerToken(text, tokenIndex, token) { return text.slice(0, tokenIndex) + text.slice(tokenIndex + token.length); }
