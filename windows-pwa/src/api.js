import { MOCK_CONTACTS, INITIAL_CONVERSATIONS, MOCK_MEMORIES, MOCK_TAXONOMY, MOCK_MEMORY_STATS, MOCK_MEMORY_CALENDAR_ENTRIES, MOCK_MEMORY_BOARD, MOCK_MEMORY_SEARCH, MOCK_PLUGINS, MOCK_TODOS, MOCK_WEATHER, MOCK_APPEARANCE, MOCK_CONTROL } from './data.js?v=30';
import { isImageFile, resolveAttachmentFilename } from './clipboard-images.js?v=30';
import { normalizeStickerCatalog } from './sticker-protocol.js?v=30';

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
    instrument: normalizeInstrument(raw.instrument),
    terminal: normalizeTerminal(raw.terminal),
  };
}

const SAFE_EFFORTS = new Set(['low', 'medium', 'high', 'xhigh', 'max']);
const SAFE_PHASES = new Set(['idle', 'unavailable', '正在准备', '正在启动', '正在处理', '处理完成', '已中断', '处理失败']);
const SAFE_OBSERVER_LABELS = new Set([
  '已接收任务，正在准备', '正在启动本轮处理', '开始处理', '正在整理回复（内容已隐藏）',
  '正在分析（思考内容已隐藏）', '运行命令（参数与输出已隐藏）', '修改文件（路径与内容已隐藏）',
  '调用工具（名称与参数已隐藏）', '调用协作代理（任务内容已隐藏）', '协作代理处理中（详情已隐藏）',
  '搜索资料（查询内容已隐藏）', '查看图片（路径已隐藏）', '生成图片（提示词已隐藏）',
  '整理会话上下文（内容已隐藏）', '等待外部步骤完成',
]);
const safeCount = (value, maximum = 2_000_000_000) => Number.isInteger(value) && value >= 0 && value <= maximum ? value : null;
const safePercent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value * 10) / 10 : null;
const safeDisplay = (value, maximum = 48) => typeof value === 'string' && value.length > 0 && value.length <= maximum && !value.includes('@') && /^[A-Za-z0-9\u4e00-\u9fff .:_+\-/()]+$/.test(value) ? value : '';

/** Defense-in-depth projection for the optional, read-only Kairos instrument. */
export function normalizeInstrument(raw) {
  const empty = { available: false, provider: '', model: '', effort: '', context: { available: false, usedPercent: null, usedTokens: null, windowTokens: null }, quota: { plan: '', windows: [] } };
  if (!raw || typeof raw !== 'object') return empty;
  const provider = new Set(['Codex', 'Claude Code']).has(raw.provider) ? raw.provider : '';
  const model = typeof raw.model === 'string' && (/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(raw.model) || raw.model === 'Fable 5') ? raw.model : '';
  const effort = SAFE_EFFORTS.has(raw.effort) ? raw.effort : '';
  const sourceContext = raw.context && typeof raw.context === 'object' ? raw.context : {};
  // The stream reducer can re-normalize an existing client snapshot after a
  // draft event.  Accept only our own camelCase representation as a second
  // spelling; this is not a generic aliasing layer.
  const usedTokens = safeCount(sourceContext.used_tokens ?? sourceContext.usedTokens);
  const windowTokens = safeCount(sourceContext.window_tokens ?? sourceContext.windowTokens);
  const suppliedPercent = safePercent(sourceContext.used_percentage ?? sourceContext.usedPercent);
  const countsAvailable = usedTokens !== null && windowTokens !== null && windowTokens > 0 && usedTokens <= windowTokens;
  const contextAvailable = sourceContext.available === true && (countsAvailable || suppliedPercent !== null);
  const context = { available: contextAvailable, usedPercent: contextAvailable ? (countsAvailable ? Math.round(Math.min(100, Math.max(0, (usedTokens / windowTokens) * 100)) * 10) / 10 : suppliedPercent) : null, usedTokens: countsAvailable ? usedTokens : null, windowTokens: countsAvailable ? windowTokens : null };
  const sourceQuota = raw.quota && typeof raw.quota === 'object' ? raw.quota : {};
  const windows = Array.isArray(sourceQuota.windows) ? sourceQuota.windows.slice(0, 3).map((item) => {
    if (!item || typeof item !== 'object') return null;
    const mode = item.mode === 'used' ? 'used' : 'remaining'; const percent = safePercent(mode === 'used' ? (item.used_percent ?? item.usedPercent ?? item.percent) : (item.remaining_percent ?? item.remainingPercent ?? item.percent)); const label = safeDisplay(item.label, 32); const resetLabel = safeDisplay(item.reset_label ?? item.resetLabel, 48);
    return percent === null || !label || !resetLabel ? null : { label, percent, mode, resetLabel };
  }).filter(Boolean) : [];
  return { available: raw.available === true && Boolean(provider || model || effort || context.available || windows.length), provider, model, effort, context, quota: { plan: safeDisplay(sourceQuota.plan, 40), windows } };
}

/** The terminal is observer labels only: never accept upstream text as a line. */
export function normalizeTerminal(raw) {
  const empty = { available: false, busy: false, phase: 'unavailable', events: [] };
  if (!raw || typeof raw !== 'object') return empty;
  const available = raw.available === true;
  const busy = available && raw.busy === true;
  const phase = SAFE_PHASES.has(raw.phase) ? raw.phase : (available ? 'idle' : 'unavailable');
  const events = busy && Array.isArray(raw.events) ? raw.events.slice(-40).map((event) => {
    if (!event || typeof event !== 'object') return null;
    const elapsedSeconds = safeCount(event.elapsed_seconds ?? event.elapsedSeconds, 604800); const label = event.label;
    return elapsedSeconds === null || !SAFE_OBSERVER_LABELS.has(label) ? null : { elapsedSeconds, label };
  }).filter(Boolean) : [];
  return { available, busy, phase: busy ? phase : (available ? 'idle' : 'unavailable'), events };
}

/**
 * Control panel contract (2026-10-17): the App's per-contact sheet projected
 * onto the PWA side rail.  The server gates exactly these paths for the web
 * session cookie (WEB_CONTROL_* in push.py); every other provider endpoint
 * stays native-only.  kairos posts `reasoning_effort`, xiaoke/kiro `effort`.
 */
const CONTROL_PROVIDERS = { xiaoke: 'Claude Code', kairos: 'Codex', kimi: 'Kimi Code', kiro: 'Kiro' };
const CONTROL_PATHS = {
  xiaoke: { status: '/xiaoke/status', preferences: '/xiaoke/preferences', effortField: 'effort' },
  kairos: { status: '/codex/status', preferences: '/codex/preferences', effortField: 'reasoning_effort' },
  kimi: { status: '/kimi/status', preferences: '/kimi/preferences', effortField: 'reasoning_effort' },
  kiro: { status: '', preferences: '/kiro/preferences', effortField: 'effort' },
};
export function controlPanelSupported(contactId) { return Boolean(CONTROL_PATHS[contactId]); }

/** Bounded plain text for panel rendering (nodes only ever get textContent). */
const controlText = (value, maximum = 120) => String(value ?? '').replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, maximum);
const controlPercent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value * 10) / 10 : null;

function normalizeControlQuotaWindows(raw) {
  const windows = Array.isArray(raw) ? raw : [];
  return windows.slice(0, 4).map((item) => {
    if (!item || typeof item !== 'object') return null;
    const usedPercent = controlPercent(item.used_percent ?? item.usedPercent);
    const label = controlText(item.label, 40);
    if (usedPercent === null || !label) return null;
    return { label, usedPercent, text: controlText(item.text, 60), resetText: controlText(item.reset_text ?? item.resetText ?? item.reset_label, 60) };
  }).filter(Boolean);
}

/** Unified status projection: provider/busy/model/effort/session/context/quota. */
export function normalizeControlStatus(contactId, raw = {}) {
  const provider = CONTROL_PROVIDERS[contactId] || '';
  const empty = { available: false, provider, busy: false, model: '', effort: '', sessionId: '', context: { available: false, usedPercent: null, text: '' }, quotaText: '', quotaWindows: [] };
  if (!raw || typeof raw !== 'object' || !provider) return empty;
  const sourceContext = raw.context && typeof raw.context === 'object' ? raw.context : {};
  const sourceQuota = raw.quota && typeof raw.quota === 'object' ? raw.quota : {};
  let model = ''; let effort = ''; let sessionId = ''; let contextText = ''; let contextPercent = controlPercent(sourceContext.used_percent ?? sourceContext.usedPercent);
  let contextAvailable = sourceContext.available === true && contextPercent !== null;
  if (contactId === 'kiro') {
    // Kiro folds its live snapshot into the preferences GET.
    const selection = raw.selection && typeof raw.selection === 'object' ? raw.selection : {};
    model = controlText(selection.model || raw.model || raw.current_model, 80);
    effort = controlText(selection.effort || raw.effort || raw.current_effort, 24);
    contextPercent = controlPercent(raw.context_usage_percent);
    contextAvailable = contextPercent !== null;
  } else {
    model = controlText(raw.model, 80);
    effort = controlText(contactId === 'xiaoke' ? raw.effort : raw.reasoning_effort, 24);
    sessionId = controlText(raw.active_session_id, 80);
    contextText = controlText(sourceContext.text, 120);
    if (contactId === 'kairos') contextAvailable = contextPercent !== null;
  }
  const quotaWindows = normalizeControlQuotaWindows(sourceQuota.windows);
  const quotaText = controlText(sourceQuota.text ?? sourceQuota.plan ?? '', 120);
  return {
    available: raw.ok === true,
    provider: controlText(raw.provider, 40) || provider,
    busy: raw.busy === true,
    model, effort, sessionId,
    context: { available: contextAvailable, usedPercent: contextAvailable ? contextPercent : null, text: contextText },
    quotaText, quotaWindows,
  };
}

/** Unified preferences projection: current selection plus pick-lists. */
export function normalizeControlPreferences(contactId, raw = {}) {
  const provider = CONTROL_PROVIDERS[contactId] || '';
  const empty = { available: false, provider, busy: false, model: '', effort: '', models: [], efforts: [], appliesFrom: '' };
  if (!raw || typeof raw !== 'object' || !provider) return empty;
  const selection = raw.selection && typeof raw.selection === 'object' ? raw.selection : {};
  const rawModels = Array.isArray(raw.models) ? raw.models : (Array.isArray(raw.available_models) ? raw.available_models : []);
  const models = rawModels.slice(0, 24).map((item) => {
    if (typeof item === 'string') {
      const id = controlText(item, 80);
      return id ? { id, label: id, efforts: [] } : null;
    }
    if (!item || typeof item !== 'object') return null;
    const id = controlText(item.id, 80);
    if (!id) return null;
    const efforts = (Array.isArray(item.supported_reasoning_efforts) ? item.supported_reasoning_efforts : []).map((effort) => controlText(effort, 24)).filter(Boolean).slice(0, 8);
    return { id, label: controlText(item.label, 80) || id, efforts };
  }).filter(Boolean);
  const efforts = (Array.isArray(raw.available_efforts) ? raw.available_efforts : []).map((effort) => controlText(effort, 24)).filter(Boolean).slice(0, 8);
  let appliesFrom = '';
  if (typeof raw.applies_from === 'string') appliesFrom = controlText(raw.applies_from, 40);
  else if (raw.applies_from && typeof raw.applies_from === 'object') appliesFrom = 'next_session';
  return {
    available: raw.ok === true,
    provider: controlText(raw.provider, 40) || provider,
    busy: raw.busy === true,
    model: controlText(selection.model || raw.model, 80),
    effort: controlText(selection.reasoning_effort || selection.effort || raw.reasoning_effort || raw.effort, 24),
    models, efforts, appliesFrom,
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
    // A missing server identity is not safe to replace with a client UUID:
    // the same record could then be inserted again when the next snapshot
    // arrives.  Real chat records have either an id or a canonical timestamp.
    id: String(record.id || record.ts || record.created_at || ''),
    // role="task" rows are tool/activity events (metadata.activity_summary,
    // codex:kairos:activity, kimi/kiro tool cards).  Android folds them into
    // a ToolStack (groupMessagesWithToolStacks); coercing them to 'assistant'
    // here would render every tool call as a full chat bubble.
    role: record.role === 'user' ? 'user' : (record.role === 'task' ? 'task' : 'assistant'),
    body: String(record.text ?? record.content ?? record.body ?? ''),
    time,
    // Raw server timestamp drives incremental `since` polling and unread
    // cursors; keep the exact string so string comparison matches the server.
    ts: validStamp ? stamp : '',
    // Thinking chain: `thinking` carries the Chinese translation while
    // metadata.thinking_original keeps the English source for the toggle.
    thinking: typeof record.thinking === 'string' ? record.thinking.slice(0, 100_000) : '',
    thinkingOriginal: typeof record.metadata?.thinking_original === 'string' ? record.metadata.thinking_original.slice(0, 100_000) : '',
    attachments,
  };
}

/** De-duplicate already-normalized UI history without changing its records. */
export function dedupeHistory(records = []) {
  if (!Array.isArray(records)) return [];
  const result = [];
  const positions = new Map();
  records.forEach((raw) => {
    const record = raw;
    if (!record || typeof record !== 'object') return;
    if (record.id && positions.has(record.id)) result[positions.get(record.id)] = record;
    else {
      if (record.id) positions.set(record.id, result.length);
      result.push(record);
    }
  });
  return result;
}

/** Normalize raw server history once, at the adapter boundary. */
export function normalizeHistory(records = []) {
  if (!Array.isArray(records)) return [];
  return dedupeHistory(records.map(normalizeRecord));
}

/**
 * Upsert an already-normalized canonical UI record returned by POST /chat/send.
 * Call normalizeSendRecord at the server boundary first.
 */
export function upsertHistoryRecord(history = [], record) {
  const next = dedupeHistory(history);
  if (!record || typeof record !== 'object' || !record.id) return { history: next, record: null };
  const found = next.findIndex(({ id }) => id === record.id);
  if (found >= 0) next[found] = record;
  else next.push(record);
  return { history: next, record };
}

/**
 * Reconcile a server snapshot with records returned by a successful send.
 * A poll that started before POST /chat/send may be stale, so retain pending
 * canonical records until a snapshot explicitly contains each one.
 */
export function reconcileHistorySnapshot(currentHistory = [], snapshot = [], pendingIds = []) {
  const next = dedupeHistory(snapshot);
  const pending = new Set(pendingIds);
  const snapshotIds = new Set(next.map(({ id }) => id).filter(Boolean));
  pending.forEach((id) => {
    if (snapshotIds.has(id)) return pending.delete(id);
    const retained = currentHistory.find((record) => record?.id === id);
    if (retained && !snapshotIds.has(id)) next.push(retained);
  });
  return { history: dedupeHistory(next), pendingIds: [...pending] };
}

/**
 * Merge an incremental `since` delta into the retained history.  Unlike a
 * full snapshot, absent records are not dropped; pending canonical sends stay
 * pending until the delta explicitly contains them.
 */
export function mergeHistoryDelta(currentHistory = [], delta = [], pendingIds = []) {
  const next = dedupeHistory(currentHistory);
  const positions = new Map();
  next.forEach((record, index) => { if (record?.id) positions.set(record.id, index); });
  (Array.isArray(delta) ? delta : []).forEach((record) => {
    if (!record || typeof record !== 'object' || !record.id) return;
    if (positions.has(record.id)) next[positions.get(record.id)] = record;
    else { positions.set(record.id, next.length); next.push(record); }
  });
  const seen = new Set((Array.isArray(delta) ? delta : []).map(({ id }) => id).filter(Boolean));
  return { history: next, pendingIds: pendingIds.filter((id) => !seen.has(id)) };
}

/** Latest server timestamp in a normalized history ('' when unknown). */
export function historyCursor(records = []) {
  return (Array.isArray(records) ? records : []).reduce((latest, record) => {
    const ts = String(record?.ts || '');
    return ts > latest ? ts : latest;
  }, '');
}

/**
 * Optimistic busy projection applied locally the moment POST /chat/send
 * succeeds.  The next authoritative /chat/status snapshot or SSE draft
 * replaces it; until then the UI must not look idle.  The old turn's stop
 * fence is never reused: a fresh turn starts without a stop request.
 */
export function optimisticBusyState(live = {}) {
  const base = live && typeof live === 'object' ? live : {};
  return { ...base, busy: true, replyState: 'generating', statusText: base.busy || ['generating', 'queued'].includes(base.replyState) ? (base.statusText || '正在处理') : '正在启动…', stopRequest: { supported: false, body: null } };
}

/** Header stop control: visible while busy, armed only with a server fence. */
export function stopControlState(live = {}) {
  const busy = Boolean(live?.busy || ['generating', 'queued'].includes(live?.replyState));
  const canStop = busy && live?.stopRequest?.supported === true;
  return { visible: busy, enabled: canStop, label: canStop ? '停止' : '正在启动…' };
}

export function normalizeSendRecord(result) {
  const record = result && typeof result === 'object' ? (result.record || result.message) : null;
  if (!record || typeof record !== 'object' || Array.isArray(record)) return null;
  const normalized = normalizeRecord(record);
  return normalized.id ? normalized : null;
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

/** Memory items cross a trust boundary: bound every field, never trust HTML. */
export function normalizeMemoryItem(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const content = String(raw.display_markdown || raw.content || raw.body || raw.title || '').slice(0, 20_000);
  const id = String(raw.id || '').slice(0, 128);
  if (!content && !id) return null;
  return {
    id,
    content,
    category: String(raw.category || '').slice(0, 64),
    subcategory: String(raw.subcategory || '').slice(0, 96),
    tags: (Array.isArray(raw.tags) ? raw.tags : []).map((tag) => String(tag).slice(0, 40)).slice(0, 12),
    createdAt: String(raw.createdAt || raw.created_at || raw.timestamp || '').slice(0, 40),
    similarity: typeof raw.similarity === 'number' && Number.isFinite(raw.similarity) ? Math.round(raw.similarity * 1000) / 1000 : null,
  };
}

/** Board messages are plain guest-book entries: id/from/content/createdAt. */
export function normalizeBoardMessage(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const content = String(raw.content || '').slice(0, 4_000);
  if (!content) return null;
  return {
    id: String(raw.id || '').slice(0, 128),
    from: String(raw.from || '小克').slice(0, 60),
    content,
    createdAt: String(raw.createdAt || raw.created_at || '').slice(0, 40),
  };
}

/** Plugin ids become URL path segments, so accept only flat slug ids. */
export function normalizePlugin(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const id = String(raw.id || '');
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)) return null;
  return {
    id,
    name: String(raw.name || id).slice(0, 60),
    version: String(raw.version || '').slice(0, 24),
    description: String(raw.description || '').slice(0, 200),
    builtin: raw.builtin === true,
  };
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

/** /todos sections projection: keep only the fields the dashboard reads. */
export function normalizeTodoSections(raw = {}) {
  const sections = Array.isArray(raw?.sections) ? raw.sections : [];
  return sections.map((section) => ({
    section: String(section?.section || '').slice(0, 60),
    source: String(section?.source || '').slice(0, 40),
    items: (Array.isArray(section?.items) ? section.items : []).map((item) => ({
      text: String(item?.text || '').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 300),
      done: item?.done === true,
      time: typeof item?.time === 'string' && /^([01]?\d|2[0-3]):[0-5]\d$/.test(item.time) ? item.time : '',
      dueDate: typeof item?.dueDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(item.dueDate) ? item.dueDate : '',
      eventId: String(item?.event_id || '').slice(0, 128),
    })).filter((item) => item.text),
  }));
}

/**
 * Appearance subset from /plugins/xiaonan-dash/appearance: nickname overrides
 * plus same-origin flat avatar URIs only — the server already sanitizes, this
 * is defense-in-depth at the client boundary.
 */
export function normalizeDashAppearance(raw = {}) {
  const cleanMap = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});
  const nicknames = {};
  Object.entries(cleanMap(raw?.contact_nicknames)).forEach(([key, value]) => {
    const id = String(key).slice(0, 64);
    const name = String(value ?? '').replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 60);
    if (id && name) nicknames[id] = name;
  });
  const avatars = {};
  Object.entries(cleanMap(raw?.contact_avatar_uris)).forEach(([key, value]) => {
    const id = String(key).slice(0, 64);
    const uri = String(value ?? '');
    const match = /^\/appearance-assets\/([^/\\]+)$/.exec(uri);
    if (id && match && !match[1].startsWith('.') && !match[1].includes('..')) avatars[id] = uri;
  });
  return { nicknames, avatars };
}

/** Weather proxy payload → { place, temperatureC, weatherCode, label } | null. */
export function normalizeWeather(raw = {}) {
  const temperature = Number(raw?.temperature_c);
  if (!Number.isFinite(temperature)) return null;
  const code = Number(raw?.weather_code);
  const place = raw?.place && typeof raw.place === 'object' ? raw.place : {};
  return {
    place: String(place.name || '').replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 60),
    temperatureC: Math.round(temperature),
    weatherCode: Number.isInteger(code) ? code : -1,
    label: String(raw?.label || '').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 24),
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
    async getHistory(contactId, { signal, since, limit } = {}) {
      const query = new URLSearchParams({ contact_id: contactId });
      // Incremental sync: the server already accepts since/limit and answers
      // with only the records newer than the cursor (strict ts comparison).
      if (since) query.set('since', String(since));
      if (limit) query.set('limit', String(Math.min(Math.max(Number(limit) || 0, 1), 10_000)));
      const result = await call(url(`/chat/history?${query}`), { signal });
      const records = Array.isArray(result) ? result : (result.records || result.messages || []);
      return normalizeHistory(records);
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
    async getStickerCatalog() { return normalizeStickerCatalog(await call(url('/stickers/catalog'))); },
    // Control panel: only the per-provider paths the web cookie is gated to.
    async getControlStatus(contactId) {
      const paths = CONTROL_PATHS[contactId];
      if (!paths) return normalizeControlStatus(contactId, null);
      return normalizeControlStatus(contactId, await call(url(paths.status || paths.preferences)));
    },
    async getControlPreferences(contactId) {
      const paths = CONTROL_PATHS[contactId];
      if (!paths) return normalizeControlPreferences(contactId, null);
      return normalizeControlPreferences(contactId, await call(url(paths.preferences)));
    },
    async saveControlPreferences(contactId, { model, effort } = {}) {
      const paths = CONTROL_PATHS[contactId];
      if (!paths) throw new Error('此联系人没有控制面板');
      const body = {};
      if (model) body.model = model;
      if (effort) body[paths.effortField] = effort;
      if (!Object.keys(body).length) throw new Error('没有要应用的偏好变更');
      return call(url(paths.preferences), { method: 'POST', body });
    },
    async uploadSticker(file, { name, categoryId, newCategoryName, signal, onProgress } = {}) {
      const query = new URLSearchParams({ name, filename: resolveAttachmentFilename(file, 0) || 'sticker.png', ...(categoryId ? { category_id: categoryId } : { new_category_name: newCategoryName }) });
      return upload(url(`/stickers/upload?${query}`), { body: file, signal, credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': file.type, ...(csrfToken ? { 'X-CC-Web-CSRF': csrfToken } : {}) }, onProgress });
    },
    async getTaxonomy() { return call(url('/memory/taxonomy')); },
    async listMemories({ category, subcategory }) {
      const query = new URLSearchParams({ category, ...(subcategory ? { subcategory } : {}) });
      const result = await call(url(`/memory/list?${query}`));
      const items = Array.isArray(result) ? result : (result.memories || result.items || []);
      return items.map(normalizeMemoryItem).filter(Boolean);
    },
    async getMemoryStats() {
      const result = await call(url('/memory/stats'));
      const source = result && typeof result === 'object' ? result : {};
      const rawCategories = (source.byCategory || source.by_category) && typeof (source.byCategory || source.by_category) === 'object' ? (source.byCategory || source.by_category) : {};
      const byCategory = {};
      Object.entries(rawCategories).forEach(([key, value]) => {
        const count = Number(value);
        if (/^[A-Za-z0-9_.-]{1,64}$/.test(key) && Number.isInteger(count) && count >= 0) byCategory[key] = count;
      });
      const total = Number(source.total ?? source.total_count);
      return { total: Number.isInteger(total) && total >= 0 ? total : Object.values(byCategory).reduce((sum, count) => sum + count, 0), byCategory };
    },
    async getMemoryCalendar(month) {
      if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(String(month || ''))) throw new Error('month must use YYYY-MM');
      const result = await call(url(`/memory/calendar?${new URLSearchParams({ month })}`));
      // The upstream answers either { days: {date: {entries, mood}} } or the
      // bare date map; both normalize to the same { days } projection.
      const rawDays = result?.days && typeof result.days === 'object' ? result.days : (result && typeof result === 'object' && !Array.isArray(result) ? result : {});
      const days = {};
      Object.entries(rawDays).forEach(([date, value]) => {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !value || typeof value !== 'object') return;
        const entries = (Array.isArray(value.entries) ? value.entries : []).map(normalizeMemoryItem).filter(Boolean);
        if (!entries.length) return;
        days[date] = { entries, mood: typeof value.mood === 'string' ? value.mood.slice(0, 24) : '' };
      });
      return { days };
    },
    async searchMemories(query, { limit = 20 } = {}) {
      const bounded = Math.min(Math.max(Number(limit) || 20, 1), 200);
      const result = await call(url(`/memory/semantic-search?${new URLSearchParams({ query: String(query || '').slice(0, 200), limit: String(bounded) })}`));
      const items = Array.isArray(result) ? result : (result?.memories || result?.results || result?.items || []);
      return items.map(normalizeMemoryItem).filter(Boolean);
    },
    async getMemoryBoard({ page = 1, perPage = 20 } = {}) {
      const safePage = Math.min(Math.max(Number(page) || 1, 1), 1_000_000);
      const safePerPage = [20, 50, 100].includes(Number(perPage)) ? Number(perPage) : 20;
      const result = await call(url(`/memory/board?${new URLSearchParams({ page: String(safePage), per_page: String(safePerPage) })}`));
      const rawMessages = Array.isArray(result) ? result : (result?.messages || []);
      const rawPagination = result?.pagination && typeof result.pagination === 'object' ? result.pagination : {};
      const totalPages = Number(rawPagination.totalPages ?? rawPagination.total_pages);
      return {
        messages: rawMessages.map(normalizeBoardMessage).filter(Boolean),
        pagination: {
          page: Number.isInteger(Number(rawPagination.page)) ? Number(rawPagination.page) : safePage,
          totalPages: Number.isInteger(totalPages) && totalPages >= 1 ? totalPages : 1,
        },
      };
    },
    async listPlugins() {
      const result = await call(url('/plugins'));
      return (Array.isArray(result?.plugins) ? result.plugins : []).map(normalizePlugin).filter(Boolean);
    },
    async getTodos() { return normalizeTodoSections(await call(url('/todos'))); },
    async getDashAppearance() { return normalizeDashAppearance(await call(url('/plugins/xiaonan-dash/appearance'))); },
    async getWeather({ city = '', lat = null, lon = null } = {}) {
      const query = new URLSearchParams();
      if (city) query.set('city', String(city).slice(0, 80));
      else if (lat !== null && lat !== undefined && lon !== null && lon !== undefined) { query.set('lat', String(lat)); query.set('lon', String(lon)); }
      else throw new Error('city or lat+lon required');
      return normalizeWeather(await call(url(`/plugins/xiaonan-dash/weather?${query}`)));
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
      let stopped = false; let pollTimer = null; let reconnectTimer = null; let stream = null; let controller = null; let pollInFlight = false; let failureDelay = 1_000; let cursor = '';
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
        // First poll is a full snapshot; later polls fetch only the delta
        // after the newest record seen so far (server `since` is exclusive).
        const since = cursor;
        try {
          const [history, state] = await Promise.all([this.getHistory(contactId, { signal: requestController.signal, ...(since ? { since, limit: 500 } : {}) }), this.getLiveState(contactId, { signal: requestController.signal })]);
          if (stopped) return;
          const nextCursor = historyCursor(history);
          if (nextCursor > cursor) cursor = nextCursor;
          listener({ type: 'snapshot', contactId, history, state, incremental: Boolean(since) });
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
    async getControlStatus(contactId) {
      const fixture = MOCK_CONTROL[contactId];
      return normalizeControlStatus(contactId, fixture ? clone(fixture.status || fixture.preferences) : null);
    },
    async getControlPreferences(contactId) {
      const fixture = MOCK_CONTROL[contactId];
      return normalizeControlPreferences(contactId, fixture ? clone(fixture.preferences) : null);
    },
    async saveControlPreferences(contactId, { model, effort } = {}) {
      const fixture = MOCK_CONTROL[contactId];
      if (!fixture) throw new Error('此联系人没有控制面板');
      if (!model && !effort) throw new Error('没有要应用的偏好变更');
      const preferences = fixture.preferences;
      const selection = preferences.selection && typeof preferences.selection === 'object' ? preferences.selection : null;
      if (model) { if (selection) selection.model = model; preferences.model = model; if (fixture.status) fixture.status.model = model; }
      if (effort) {
        const field = contactId === 'kairos' || contactId === 'kimi' ? 'reasoning_effort' : 'effort';
        if (selection) selection[field === 'effort' ? 'effort' : 'reasoning_effort'] = effort;
        preferences[field] = effort;
        if (fixture.status) fixture.status[field] = effort;
      }
      return { ok: true, mock: true };
    },
    async getStickerCatalog() { return normalizeStickerCatalog({ version: 'mock', categories: [{ id: 'common', name: '常用' }], stickers: [{ name: '爱', label: '爱', category_id: 'common', url: 'https://test.xiaonancaleb.xyz/stickers/%E7%88%B1.gif' }], upload: { supported: true, max_file_bytes: 8388608, content_types: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'], max_name_chars: 80 } }); },
    async uploadSticker() { throw new Error('mock upload unavailable'); },
    async listMemories({ category, subcategory }) { return clone(MOCK_MEMORIES[subcategory || category] || [{ title: '暂时没有可展示的条目', body: '此分类已从 taxonomy 读取。', timestamp: '—' }]); },
    async getMemoryStats() { return clone(MOCK_MEMORY_STATS); },
    async getMemoryCalendar(month) {
      if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(String(month || ''))) throw new Error('month must use YYYY-MM');
      const days = {};
      MOCK_MEMORY_CALENDAR_ENTRIES.forEach(({ offsetDay, mood, entries }) => {
        const date = `${month}-${String(offsetDay).padStart(2, '0')}`;
        days[date] = { entries: clone(entries), mood };
      });
      return { days };
    },
    async searchMemories(query) {
      const needle = String(query || '').trim();
      if (!needle) return [];
      const matched = MOCK_MEMORY_SEARCH.filter((item) => item.content.includes(needle));
      return clone(matched.length ? matched : MOCK_MEMORY_SEARCH);
    },
    async getMemoryBoard({ page = 1 } = {}) { const result = clone(MOCK_MEMORY_BOARD); result.pagination.page = Math.max(1, Number(page) || 1); return result; },
    async listPlugins() { return clone(MOCK_PLUGINS); },
    async getTodos() {
      const fixture = clone(MOCK_TODOS);
      const pad = (value) => String(value).padStart(2, '0');
      const dayKey = (offset) => { const d = new Date(); d.setDate(d.getDate() + offset); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
      fixture.sections.forEach((section) => section.items.forEach((item) => {
        if (item.dueDate === '__TODAY__') item.dueDate = dayKey(0);
        if (item.dueDate === '__TOMORROW__') item.dueDate = dayKey(1);
      }));
      return normalizeTodoSections(fixture);
    },
    async getDashAppearance() { return normalizeDashAppearance(clone(MOCK_APPEARANCE)); },
    async getWeather() { return normalizeWeather(clone(MOCK_WEATHER)); },
    async uploadAttachments(_contactId, files, { onProgress } = {}) { return files.map((file, index) => { onProgress?.({ index, totalFiles: files.length, loaded: file.size, total: file.size, file }); return { attachment_id: `mock-${crypto.randomUUID()}`, filename: file.name, type: file.type || 'file', size: file.size }; }); },
    async cancelUploads() { return { ok: true, canceled: 0 }; },
    subscribe(listener, { contactId } = {}) {
      const filtered = (event) => { if (!contactId || event.contactId === contactId) listener(event); };
      listeners.add(filtered); return () => listeners.delete(filtered);
    },
  };
}
