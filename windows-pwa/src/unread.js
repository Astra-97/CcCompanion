/**
 * Per-contact read cursor persisted in localStorage.  Comparisons use the
 * server's ISO `ts` strings, which sort lexicographically in one format.
 */
const STORAGE_KEY = 'cc-pwa-last-seen-v1';

export function createUnreadStore({ storage = globalThis.localStorage, key = STORAGE_KEY } = {}) {
  let cursors = {};
  try {
    const raw = storage?.getItem(key);
    const parsed = raw ? JSON.parse(raw) : {};
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      Object.entries(parsed).forEach(([id, ts]) => { if (typeof ts === 'string') cursors[id] = ts; });
    }
  } catch { cursors = {}; }
  const persist = () => { try { storage?.setItem(key, JSON.stringify(cursors)); } catch { /* storage is best-effort */ } };
  return {
    lastSeen(contactId) { return cursors[contactId] || ''; },
    /** Advance-only: an older snapshot can never move the cursor backwards. */
    markSeen(contactId, ts) {
      const stamp = String(ts || '');
      if (!contactId || !stamp || stamp <= (cursors[contactId] || '')) return;
      cursors[contactId] = stamp;
      persist();
    },
    unreadCount(records = []) { return unreadCount(records, cursors) },
    unreadFor(contactId, records = []) {
      const seen = cursors[contactId] || '';
      if (!seen) return 0; // a fresh device treats existing history as read
      return (Array.isArray(records) ? records : []).filter((record) => record?.role === 'assistant' && String(record?.ts || '') > seen).length;
    },
  };
}

/** Pure counting helper kept separate for tests and non-store callers. */
export function unreadCount(records = [], lastSeenTs = '') {
  if (!lastSeenTs) return 0;
  return (Array.isArray(records) ? records : []).filter((record) => record?.role === 'assistant' && String(record?.ts || '') > lastSeenTs).length;
}

/** Latest assistant timestamp in a history, '' when none exists. */
export function latestAssistantTs(records = []) {
  return (Array.isArray(records) ? records : []).reduce((latest, record) => {
    const ts = record?.role === 'assistant' ? String(record?.ts || '') : '';
    return ts > latest ? ts : latest;
  }, '');
}

/** Seed a fresh device to each contact's current latest assistant message. */
export function seedUnreadCursors(store, historiesById = {}) {
  Object.entries(historiesById || {}).forEach(([contactId, records]) => {
    if (!contactId || store.lastSeen(contactId)) return;
    const ts = latestAssistantTs(records);
    if (ts) store.markSeen(contactId, ts);
  });
}

/** New assistant records since the read cursor, oldest first. */
export function unseenAssistant(records = [], lastSeenTs = '') {
  if (!lastSeenTs) return [];
  return (Array.isArray(records) ? records : []).filter((record) => record?.role === 'assistant' && String(record?.ts || '') > lastSeenTs);
}

/**
 * Drop lines that are raw structured events or log noise, never human words.
 * Group relays sometimes persist Codex event streams verbatim
 * (`{"type":"thread.started",…}` JSONL, `… ERROR codex_rmcp_client…` logs);
 * a contact preview must never surface that raw meat (Astra 2026-10-17).
 */
function isStructuredEventLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?\s/.test(trimmed)) return true;
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === 'object';
  } catch {
    return false;
  }
}

/** Human-readable residue of a record body; '' when nothing but events/logs. */
export function humanPreviewText(body) {
  return String(body || '')
    .split('\n')
    .filter((line) => !isStructuredEventLine(line))
    .join(' ')
    .replace(/```[\s\S]*?```/g, '[代码]')
    .replace(/\s+/g, ' ')
    .trim();
}

/** One-line contact list preview for the latest record. */
export function previewText(record, { maxLength = 42 } = {}) {
  if (!record) return '';
  const prefix = record.role === 'user' ? '我: ' : '';
  const body = humanPreviewText(record.body);
  if (!body && record.attachments?.length) return `${prefix}[附件] ${record.attachments[0].name}`;
  if (!body) return '';
  const clipped = body.length > maxLength ? `${body.slice(0, maxLength)}…` : body;
  return prefix + clipped;
}

/**
 * Latest record worth previewing: tool/activity rows (role "task") and
 * event-only noise are skipped so the contact list shows real chat content
 * (Astra 2026-10-18: tool calls must never surface in the preview).
 */
export function latestChatRecord(records = []) {
  if (!Array.isArray(records)) return null;
  for (let index = records.length - 1; index >= 0; index -= 1) {
    const record = records[index];
    if (!record || record.role === 'task') continue;
    if (humanPreviewText(record.body) || record.attachments?.length) return record;
  }
  return null;
}
