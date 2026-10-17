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

/** New assistant records since the read cursor, oldest first. */
export function unseenAssistant(records = [], lastSeenTs = '') {
  if (!lastSeenTs) return [];
  return (Array.isArray(records) ? records : []).filter((record) => record?.role === 'assistant' && String(record?.ts || '') > lastSeenTs);
}

/** One-line contact list preview for the latest record. */
export function previewText(record, { maxLength = 42 } = {}) {
  if (!record) return '';
  const prefix = record.role === 'user' ? '我: ' : '';
  const body = String(record.body || '').replace(/```[\s\S]*?```/g, '[代码]').replace(/\s+/g, ' ').trim();
  if (!body && record.attachments?.length) return `${prefix}[附件] ${record.attachments[0].name}`;
  if (!body) return '';
  const clipped = body.length > maxLength ? `${body.slice(0, maxLength)}…` : body;
  return prefix + clipped;
}
