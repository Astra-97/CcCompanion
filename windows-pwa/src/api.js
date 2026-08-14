import { MOCK_CONTACTS, INITIAL_CONVERSATIONS, MOCK_MEMORIES, MOCK_TAXONOMY } from './data.js?v=8';
import { isImageFile, resolveAttachmentFilename } from './clipboard-images.js?v=8';

const clone = (value) => structuredClone(value);
const now = () => new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date());

/**
 * Normalize the server's draft/status shape to a contact-scoped UI state.
 * No credentials belong here: production uses the same-origin HttpOnly cookie.
 */
export function normalizeLiveState(raw = {}) {
  const draft = raw.draft && typeof raw.draft === 'object' ? raw.draft : raw;
  const busy = Boolean(raw.busy ?? draft.is_active ?? false);
  const replyState = raw.reply_state || draft.reply_state || (busy ? 'generating' : 'idle');
  return {
    busy,
    replyState,
    turnId: String(raw.turn_id || raw.turnId || draft.turn_id || draft.turnId || raw.user_ts || draft.user_ts || ''),
    revision: String(raw.revision || raw.draft_revision || draft.revision || draft.draft_revision || ''),
    updatedAt: String(raw.updated_at || raw.updatedAt || draft.updated_at || draft.updatedAt || ''),
    statusText: raw.status_text || raw.statusText || raw.status || draft.status_text || (busy ? '正在处理' : '待命'),
    draft: draft.text || '',
    activityText: draft.activity_text || raw.activity_text || raw.activityText || '',
    activityCount: Number(draft.activity_count || raw.activity_count || raw.activityCount || 0),
    stopRequest: (raw.stop_request || raw.stopRequest) && typeof (raw.stop_request || raw.stopRequest) === 'object' ? {
      supported: Boolean((raw.stop_request || raw.stopRequest).supported),
      body: (raw.stop_request || raw.stopRequest).body && typeof (raw.stop_request || raw.stopRequest).body === 'object' ? clone((raw.stop_request || raw.stopRequest).body) : null,
    } : { supported: false, body: null },
    workers: Array.isArray(draft.worker_activity_items) ? draft.worker_activity_items.map((item) => {
      const lifecycle = item.status || item.state; // `status` is the current server contract; state remains legacy-compatible.
      return {
        id: String(item.worker_id || item.id || item.name || 'worker'),
        name: String(item.name || '协作 worker'),
        state: ['running', 'completed', 'interrupted', 'failed'].includes(lifecycle) ? lifecycle : 'running',
        count: Math.max(1, Number(item.count || 1)),
      };
    }) : [],
  };
}

export function normalizeRecord(record = {}) {
  const stamp = String(record.ts || record.time || record.created_at || '');
  const parsed = new Date(stamp);
  const validStamp = Boolean(stamp) && !Number.isNaN(parsed.getTime());
  const time = validStamp ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(parsed) : '刚刚';
  const candidates = [];
  if (record.attachment_url) candidates.push({
    attachment_url: record.attachment_url,
    attachment_filename: record.attachment_filename,
    attachment_type: record.attachment_type,
  });
  if (Array.isArray(record.attachments)) candidates.push(...record.attachments);
  if (Array.isArray(record.metadata?.attachments)) candidates.push(...record.metadata.attachments);
  const seenAttachments = new Set();
  const attachments = candidates.map(normalizeAttachment).filter((attachment) => {
    if (!attachment || seenAttachments.has(attachment.url)) return false;
    seenAttachments.add(attachment.url); return true;
  });
  return {
    id: String(record.id || record.ts || crypto.randomUUID()),
    role: record.role === 'user' ? 'user' : 'assistant',
    body: String(record.text ?? record.content ?? record.body ?? ''),
    time,
    attachments,
  };
}

export function normalizeAttachment(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const url = safeAttachmentUrl(raw.attachment_url || raw.url || '');
  if (!url) return null;
  const filename = String(raw.filename || raw.attachment_filename || raw.name || '附件').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 240) || '附件';
  const type = String(raw.type || raw.attachment_type || 'file').toLowerCase() === 'image' ? 'image' : 'file';
  return { name: filename, url, type, size: Number(raw.size || 0) || 0 };
}

function safeAttachmentUrl(value) {
  if (typeof value !== 'string' || !value || value.length > 2048 || /[\u0000-\u001f\u007f\\]/.test(value)) return '';
  // The backend serves only flat generated basenames. Normalize both relative
  // and absolute values through URL + one decode pass before making this check;
  // never let an encoded separator or a second decode turn metadata into a path.
  try {
    const base = globalThis.location?.origin || 'https://cccompanion.invalid';
    const parsed = new URL(value, base);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.origin !== base || parsed.username || parsed.password || parsed.search || parsed.hash) return '';
    const pathname = decodeURIComponent(parsed.pathname);
    const match = /^\/attachments\/([^/\\]+)$/.exec(pathname);
    if (!match) return '';
    const basename = match[1];
    if (basename === '.' || basename === '..' || basename.startsWith('.') || basename.includes('..') || basename.includes('%') || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/.test(basename)) return '';
    return `/attachments/${basename}`;
  } catch { return ''; }
}

export function normalizeContact(contact = {}) {
  const capabilities = Array.isArray(contact.capabilities) ? contact.capabilities : [];
  return {
    id: String(contact.id || ''),
    name: String(contact.display_name || contact.name || contact.id || '联系人'),
    channel: String(contact.provider || contact.channel || 'CC COMPANION').toUpperCase(),
    note: capabilities.includes('chat') ? `${String(contact.provider || 'chat')} · 私人链路` : '只读历史',
    capabilities,
    readOnly: Boolean(contact.read_only) || !capabilities.includes('chat'),
    stop: contact.stop && typeof contact.stop === 'object' ? contact.stop : { supported: false },
  };
}

export function createHttpAdapter({ baseUrl = '', request = defaultRequest, upload = browserUpload, clock = globalThis, visibility = globalThis.document, eventSourceFactory = globalThis.EventSource ? (target) => new globalThis.EventSource(target) : null, network = globalThis } = {}) {
  const url = (path) => `${baseUrl.replace(/\/$/, '')}${path}`;
  let csrfToken = '';
  let uploadLimits = { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024, ttl_seconds: 15 * 60 };
  const acceptSession = (result) => {
    csrfToken = String(result?.csrf_token || '');
    const limits = result?.upload_limits;
    if (limits && typeof limits === 'object') uploadLimits = {
      max_file_bytes: Number(limits.max_file_bytes) || uploadLimits.max_file_bytes,
      max_pending_files: Number(limits.max_pending_files) || uploadLimits.max_pending_files,
      max_pending_bytes: Number(limits.max_pending_bytes) || uploadLimits.max_pending_bytes,
      ttl_seconds: Number(limits.ttl_seconds) || uploadLimits.ttl_seconds,
    };
    return result;
  };
  const call = async (path, options = {}) => {
    const headers = options.method === 'POST' && csrfToken ? { ...(options.headers || {}), 'X-CC-Web-CSRF': csrfToken } : options.headers;
    return request(path, headers ? { ...options, headers } : options);
  };
  return {
    async getWebSession() { return acceptSession(await call(url('/web/session'))); },
    async createWebSession(credentials) { return acceptSession(await call(url('/web/session'), { method: 'POST', body: credentials })); },
    // This endpoint intentionally does not use `call`: pairing starts without a
    // session, and must not inherit a stale CSRF header from a previous login.
    async pairWebSession({ code }) { return acceptSession(await request(url('/web/session/pair'), { method: 'POST', body: { code } })); },
    async logout() { const result = await call(url('/web/session/logout'), { method: 'POST', body: {} }); csrfToken = ''; return result; },
    getUploadLimits() { return { ...uploadLimits }; },
    async contacts() {
      const result = await call(url('/chat/contacts'));
      return (Array.isArray(result) ? result : result.contacts || []).map(normalizeContact).filter(({ id }) => id);
    },
    async getHistory(contactId, { signal } = {}) {
      const result = await call(url(`/chat/history?contact_id=${encodeURIComponent(contactId)}`), { signal });
      const records = Array.isArray(result) ? result : (result.records || result.messages || []);
      return records.map(normalizeRecord);
    },
    async getLiveState(contactId, { signal } = {}) {
      const status = await call(url(`/chat/status?contact_id=${encodeURIComponent(contactId)}`), { signal });
      return normalizeLiveState(status);
    },
    async sendMessage(contactId, { text, attachmentIds = [] }) {
      const body = { contact_id: contactId, text, ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}) };
      const result = await call(url('/chat/send'), { method: 'POST', body });
      return result;
    },
    async stop(contactId, stopRequest) {
      const body = stopRequest?.supported && stopRequest?.body ? clone(stopRequest.body) : null;
      if (!body || body.contact_id !== contactId) throw new Error('Server did not provide a valid stop request for this turn');
      return call(url('/chat/stop'), { method: 'POST', body });
    },
    async getTaxonomy() { return call(url('/memory/taxonomy')); },
    async listMemories({ category, subcategory }) {
      const query = new URLSearchParams({ category, ...(subcategory ? { subcategory } : {}) });
      const result = await call(url(`/memory/list?${query}`));
      return Array.isArray(result) ? result : (result.memories || []);
    },
    async uploadAttachments(contactId, files, { signal, onProgress } = {}) {
      if (files.length > uploadLimits.max_pending_files) throw new Error(`一次最多上传 ${uploadLimits.max_pending_files} 个文件`);
      const totalBytes = files.reduce((sum, file) => sum + Number(file?.size || 0), 0);
      if (totalBytes > uploadLimits.max_pending_bytes) throw new Error(`待上传文件合计不能超过 ${formatBytes(uploadLimits.max_pending_bytes)}`);
      const staged = [];
      try {
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index];
          const filename = resolveAttachmentFilename(file, index);
          if (!file || file.size <= 0 || file.size > uploadLimits.max_file_bytes || (!filename && !isImageFile(file))) throw new Error(`${filename || '文件'} 超出 ${formatBytes(uploadLimits.max_file_bytes)} 限制或为空`);
          const query = new URLSearchParams({ contact_id: contactId, filename, role: 'user' });
          const result = await upload(url(`/chat/upload?${query}`), {
            body: file,
            signal,
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { 'Content-Type': file.type || 'application/octet-stream', ...(csrfToken ? { 'X-CC-Web-CSRF': csrfToken } : {}) },
            onProgress: (loaded, total) => onProgress?.({ index, totalFiles: files.length, loaded, total, file }),
          });
          if (result?.upload_limits) acceptSession({ ...result, csrf_token: csrfToken });
          const attachment = result?.attachments?.[0];
          if (!attachment?.attachment_id) throw new Error('服务器未返回附件标识');
          staged.push(attachment);
        }
        return staged;
      } catch (error) {
        if (staged.length) await call(url('/chat/upload/cancel'), { method: 'POST', body: { attachment_ids: staged.map(({ attachment_id }) => attachment_id) } }).catch(() => {});
        throw error;
      }
    },
    async cancelUploads(attachments) {
      const attachmentIds = attachments.map((item) => typeof item === 'string' ? item : item.attachment_id).filter(Boolean);
      if (!attachmentIds.length) return { ok: true, canceled: 0 };
      return call(url('/chat/upload/cancel'), { method: 'POST', body: { attachment_ids: attachmentIds } });
    },
    /** SSE deltas plus bounded, abortable history/status polling fallback. */
    subscribe(listener, { contactId } = {}) {
      if (!contactId) throw new Error('contactId is required for a production live subscription');
      const VISIBLE_POLL_MS = 4_000; const BUSY_POLL_MS = 1_000; const HIDDEN_POLL_MS = 15_000;
      let stopped = false; let pollTimer = null; let reconnectTimer = null; let stream = null; let controller = null; let pollInFlight = false; let failureDelay = 1_000;
      const isHidden = () => visibility?.visibilityState === 'hidden';
      const closeStream = () => { if (stream) { stream.close?.(); stream = null; } };
      const schedulePoll = (delay) => {
        if (stopped) return;
        if (pollTimer) clock.clearTimeout(pollTimer);
        pollTimer = clock.setTimeout(() => { pollTimer = null; return poll(); }, delay);
      };
      const scheduleReconnect = (delay) => {
        if (stopped || network?.navigator?.onLine === false || stream || reconnectTimer) return;
        reconnectTimer = clock.setTimeout(() => { reconnectTimer = null; connectStream(); }, delay);
      };
      const poll = async () => {
        if (stopped || pollInFlight) return;
        if (network?.navigator?.onLine === false) { schedulePoll(HIDDEN_POLL_MS); return; }
        pollInFlight = true; const requestController = new AbortController(); controller = requestController;
        try {
          const [history, state] = await Promise.all([this.getHistory(contactId, { signal: requestController.signal }), this.getLiveState(contactId, { signal: requestController.signal })]);
          if (stopped) return;
          listener({ type: 'snapshot', contactId, history, state });
          failureDelay = 1_000;
          schedulePoll(isHidden() ? HIDDEN_POLL_MS : (state.busy ? BUSY_POLL_MS : VISIBLE_POLL_MS));
        } catch (error) {
          if (!stopped && error.name !== 'AbortError') listener({ type: 'connection', contactId, online: false, error });
          if (!stopped) schedulePoll(isHidden() ? HIDDEN_POLL_MS : failureDelay);
          failureDelay = Math.min(failureDelay * 2, 30_000);
        } finally {
          if (controller === requestController) controller = null;
          pollInFlight = false;
        }
      };
      const connectStream = () => {
        if (stopped || network?.navigator?.onLine === false || !eventSourceFactory || stream) return;
        try {
          stream = eventSourceFactory(url(`/chat/stream?contact_id=${encodeURIComponent(contactId)}`));
          stream.onopen = () => { failureDelay = 1_000; listener({ type: 'connection', contactId, online: true }); };
          stream.onmessage = (message) => {
            try { const payload = JSON.parse(message.data); if (payload.contact_id === contactId && payload.event !== 'connected') listener({ type: 'stream', contactId, payload }); } catch { /* malformed stream events are ignored; polling stays authoritative */ }
          };
          stream.onerror = () => { closeStream(); listener({ type: 'connection', contactId, online: false }); scheduleReconnect(failureDelay); failureDelay = Math.min(failureDelay * 2, 30_000); };
        } catch (error) { listener({ type: 'connection', contactId, online: false, error }); scheduleReconnect(failureDelay); failureDelay = Math.min(failureDelay * 2, 30_000); }
      };
      const onVisibility = () => { if (stopped) return; if (!isHidden()) { schedulePoll(0); connectStream(); } else if (!pollInFlight) schedulePoll(HIDDEN_POLL_MS); };
      const onOnline = () => { if (!stopped) { schedulePoll(0); connectStream(); } };
      const onOffline = () => { closeStream(); controller?.abort(); listener({ type: 'connection', contactId, online: false }); };
      visibility?.addEventListener?.('visibilitychange', onVisibility);
      network?.addEventListener?.('online', onOnline); network?.addEventListener?.('offline', onOffline);
      schedulePoll(0); connectStream();
      return () => { stopped = true; controller?.abort(); if (pollTimer) clock.clearTimeout(pollTimer); if (reconnectTimer) clock.clearTimeout(reconnectTimer); closeStream(); visibility?.removeEventListener?.('visibilitychange', onVisibility); network?.removeEventListener?.('online', onOnline); network?.removeEventListener?.('offline', onOffline); };
    },
  };
}

function formatBytes(bytes) { return `${Math.max(1, Math.round(Number(bytes || 0) / (1024 * 1024)))} MB`; }

function browserUpload(path, { body, headers, signal, onProgress }) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path, true);
    xhr.withCredentials = true;
    xhr.responseType = 'json';
    Object.entries(headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress?.(event.loaded, event.total); };
    xhr.onload = () => {
      const payload = xhr.response || (() => { try { return JSON.parse(xhr.responseText); } catch { return null; } })();
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload); else reject(new Error(payload?.error || `上传失败 (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('上传连接失败'));
    xhr.onabort = () => reject(new DOMException('上传已取消', 'AbortError'));
    signal?.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

async function defaultRequest(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: options.body ? { ...(options.headers || {}), 'Content-Type': 'application/json' } : options.headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
  if (!response.ok) {
    const error = new Error(`Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export function createMockAdapter() {
  const histories = clone(INITIAL_CONVERSATIONS);
  const states = Object.fromEntries(MOCK_CONTACTS.map(({ id }) => [id, normalizeLiveState()]));
  const listeners = new Set();
  const timers = new Map();
  const emit = (event) => listeners.forEach((listener) => listener(event));
  const emitState = (contactId) => emit({ type: 'state', contactId, state: clone(states[contactId]) });
  return {
    async getWebSession() { return { ok: true, authenticated: true, mock: true, csrf_token: 'mock', upload_limits: { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 } }; },
    async createWebSession() { return { ok: true, authenticated: true, mock: true, csrf_token: 'mock', upload_limits: { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 } }; },
    async pairWebSession() { return { ok: true, authenticated: true, mock: true, csrf_token: 'mock', upload_limits: { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 } }; },
    getUploadLimits() { return { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 }; },
    async logout() { return { ok: true, mock: true }; },
    async contacts() { return clone(MOCK_CONTACTS); },
    async getHistory(contactId) { return clone(histories[contactId] || []); },
    async getLiveState(contactId) { return clone(states[contactId] || normalizeLiveState()); },
    async sendMessage(contactId, { text, attachmentIds = [] }) {
      const message = { id: crypto.randomUUID(), role: 'user', body: text, attachments: attachmentIds.map((attachment_id) => ({ name: attachment_id })), time: now() };
      histories[contactId].push(message); emit({ type: 'message', contactId, message: clone(message) });
      states[contactId] = normalizeLiveState({ busy: true, reply_state: 'generating', status_text: '正在处理', activity_text: 'Kairos 忙活了几下', activity_count: 3, stop_request: { supported: true, body: { contact_id: contactId, user_ts: message.id, ...(contactId === 'xiaoke' ? { session: 'mock' } : {}) } }, worker_activity_items: contactId === 'kairos' ? [{ worker_id: 'pwa-shell', name: 'windows_pwa_shell', state: 'running', count: 1 }] : [] });
      emitState(contactId);
      const response = { id: crypto.randomUUID(), role: 'assistant', body: '', time: now(), streaming: true };
      histories[contactId].push(response); emit({ type: 'message', contactId, message: clone(response) });
      const words = contactId === 'kairos' ? '我在处理。桌面端会把这一条对话、现场状态和记忆入口都留在你的视线里。' : '收到。这条在小克自己的历史里，不会和 Kairos 串在一起。';
      let index = 0;
      const timer = setInterval(() => {
        index += 4; response.body = words.slice(0, index); emit({ type: 'message', contactId, message: clone(response) });
        if (index >= words.length) { clearInterval(timer); timers.delete(contactId); response.streaming = false; states[contactId] = normalizeLiveState(); emit({ type: 'message', contactId, message: clone(response) }); emitState(contactId); }
      }, 70);
      timers.set(contactId, timer);
      return { accepted: true, message };
    },
    async stop(contactId, stopRequest) {
      if (stopRequest?.body?.contact_id !== contactId) throw new Error('mock stop fence rejected');
      clearInterval(timers.get(contactId)); timers.delete(contactId);
      const last = histories[contactId].at(-1); if (last?.streaming) { last.streaming = false; last.body = `${last.body || '回复'}\n\n— 已停止`; emit({ type: 'message', contactId, message: clone(last) }); }
      states[contactId] = normalizeLiveState({ reply_state: 'idle', status_text: '已停止' }); emitState(contactId);
    },
    async getTaxonomy() { return clone(MOCK_TAXONOMY); },
    async listMemories({ category, subcategory }) { return clone(MOCK_MEMORIES[subcategory || category] || [{ title: '暂时没有可展示的条目', body: '此分类已从 taxonomy 读取。', timestamp: '—' }]); },
    async uploadAttachments(_contactId, files, { onProgress } = {}) { return files.map((file, index) => { onProgress?.({ index, totalFiles: files.length, loaded: file.size, total: file.size, file }); return { attachment_id: `mock-${crypto.randomUUID()}`, filename: file.name, type: file.type || 'file', size: file.size }; }); },
    async cancelUploads() { return { ok: true, canceled: 0 }; },
    subscribe(listener, { contactId } = {}) {
      const filtered = (event) => { if (!contactId || event.contactId === contactId) listener(event); };
      listeners.add(filtered); return () => listeners.delete(filtered);
    },
  };
}
