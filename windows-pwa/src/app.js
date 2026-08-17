import { createPwaBootstrap } from './bootstrap.js?v=12';
import { createComposerState } from './composer-state.js?v=12';
import { formatPairingCode, normalizePairingCode } from './pairing-code.js?v=12';
import { composeLiveMessages, shouldShowTypingBubble, reconcileSnapshotStream, reduceStreamDraft } from './live-messages.js?v=12';
import { normalizeLiveState, normalizeSendRecord, reconcileHistorySnapshot, upsertHistoryRecord } from './api.js?v=12';
import { extractClipboardImageFiles, resolveAttachmentFilename } from './clipboard-images.js?v=12';
import { insertStickerToken, isSafeStickerName, parseStickerParts, removeStickerToken, stickerTokens } from './sticker-protocol.js?v=12';

const $ = (selector) => document.querySelector(selector);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('data-')) node.dataset[key.slice(5)] = value;
    else node.setAttribute(key, value);
  });
  children.forEach((child) => node.append(child)); return node;
};

const bootstrap = createPwaBootstrap();
const adapter = bootstrap.adapter;
const state = {
  contacts: [], activeContactId: '', histories: {}, live: {}, taxonomy: null,
  selectedScope: null, deferredInstall: null, loginBound: false, stopLive: null, streams: {}, epoch: 0,
  composerState: createComposerState(), appearance: 'default', followLatest: {}, scrollTops: {},
  stickerCatalog: null, stickerLoading: false, stickerLoadId: 0, stickerError: '', stickerCategory: '__all', stickerSearch: '', stickerTargetContactId: '', stickerSelection: { start: 0, end: 0 }, pendingCanonical: {},
  stickerUpload: { file: null, previewUrl: '', uploading: false, controller: null },
};
const contactList = $('#contact-list'); const messages = $('#message-list'); const input = $('#message-input');

function contact() { return state.contacts.find(({ id }) => id === state.activeContactId) || { id: '', name: '连接中', channel: 'PRIVATE CHANNEL', note: '', capabilities: [] }; }
function isBusy(id = state.activeContactId) { const live = state.live[id]; return Boolean(live?.busy || ['generating', 'queued'].includes(live?.replyState)); }
function composer(id = state.activeContactId) { return state.composerState.get(id); }
function sendOperation(id = state.activeContactId) { return state.composerState.current(id); }
function formatSize(size) { return size > 1_000_000 ? `${(size / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1000))} KB`; }
function attachmentName(file, index = 0) { return resolveAttachmentFilename(file, index) || '附件'; }
function formatLimit(size) { return `${Math.max(1, Math.round(size / (1024 * 1024)))} MiB`; }
function attachmentPreflight(files) {
  const limits = adapter.getUploadLimits?.() || { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 };
  if (files.length > limits.max_pending_files) return `一次最多选择 ${limits.max_pending_files} 个文件`;
  const total = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  if (total > limits.max_pending_bytes) return `附件合计不能超过 ${formatLimit(limits.max_pending_bytes)}`;
  const tooLarge = files.find((file) => !file.size || file.size > limits.max_file_bytes);
  return tooLarge ? `${attachmentName(tooLarge)} 为空或超过 ${formatLimit(limits.max_file_bytes)}` : '';
}
function renderDateRule() {
  const now = new Date(); const label = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', weekday: 'short' }).format(now);
  $('#date-rule-label').textContent = `今天 · ${label}`;
}
function renderContacts() {
  contactList.replaceChildren(...state.contacts.map((item) => {
    const busy = isBusy(item.id);
    const button = el('button', { class: `contact-button ${item.id === state.activeContactId ? 'is-active' : ''}`, type: 'button', 'aria-current': item.id === state.activeContactId ? 'page' : 'false' });
    const accent = item.id === 'kairos' ? 'brass' : 'oxide';
    button.append(el('span', { class: `avatar ${accent}`, text: item.name.slice(0, 1) }), el('span', { class: 'contact-copy' }, [el('strong', { text: item.name }), el('small', { text: item.note })]), el('span', { class: `contact-led ${busy ? 'is-busy' : ''}`, 'aria-label': busy ? '正在处理' : '待命' }));
    button.addEventListener('click', () => switchContact(item.id)); return button;
  }));
}
function renderHeader() {
  const current = contact(); const live = state.live[current.id] || { replyState: 'idle', statusText: '待命', workers: [] };
  const sending = Boolean(sendOperation(current.id));
  $('#contact-name').textContent = current.name; $('#contact-mode').textContent = current.channel;
  input.placeholder = `写给${current.name}…`; $('#connection-state').textContent = isBusy() ? (live.statusText || '正在处理') : '已连接';
  $('#signal-dot').classList.toggle('is-busy', isBusy()); $('#stop-button').hidden = !(isBusy() && live.stopRequest?.supported);
  input.disabled = sending; $('#attachment-input').disabled = sending; $('.attach-button').classList.toggle('is-disabled', sending); $('.attach-button').setAttribute('aria-disabled', String(sending));
  $('#sticker-button').disabled = sending || !current.id || Boolean(current.readOnly);
  $('#send-button').disabled = sending || !current.id || Boolean(current.readOnly) || (!composer().text.trim() && !composer().attachments.length);
  $('#upload-cancel-button').hidden = !(sending && sendOperation(current.id)?.uploading); $('#upload-cancel-button').disabled = !(sending && sendOperation(current.id)?.uploading);
}

function formatTokens(value) { return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1_000 ? `${(value / 1_000).toFixed(1)}k` : String(value); }
function renderInstrument() {
  const live = state.live[state.activeContactId] || {}; const instrument = live.instrument;
  const supportsInstrument = ['kairos', 'xiaoke'].includes(state.activeContactId);
  $('#instrument-contact').textContent = instrument?.provider || `${contact().name} / ${contact().channel === 'CODEX APP' ? 'Kairos' : 'CC'}`;
  $('#instrument-status').textContent = live.statusText || '待命';
  const model = supportsInstrument && instrument?.model ? instrument.model : '不可用';
  const effort = supportsInstrument && instrument?.effort ? instrument.effort : '—';
  $('#instrument-model').textContent = model; $('#instrument-effort').textContent = effort;
  const context = supportsInstrument ? instrument?.context : null;
  const contextDetail = $('#instrument-context-detail'); const contextBar = $('#instrument-context-bar');
  if (context?.available) { const percent = context.usedPercent; contextDetail.textContent = context.usedTokens !== null && context.windowTokens !== null ? `${percent}% · ${formatTokens(context.usedTokens)} / ${formatTokens(context.windowTokens)}` : `${percent}% 已用`; contextBar.value = percent; contextBar.hidden = false; }
  else { contextDetail.textContent = '暂无 token 记录'; contextBar.hidden = true; }
  const quotas = $('#instrument-quota'); quotas.replaceChildren();
  const windows = supportsInstrument ? (instrument?.quota?.windows || []) : [];
  if (!windows.length) { quotas.append(el('p', { class: 'instrument-empty', text: supportsInstrument ? '额度信息暂不可用' : '此联系人没有工作仪表' })); return; }
  const plan = instrument?.quota?.plan; if (plan) quotas.append(el('p', { class: 'quota-plan', text: plan }));
  quotas.append(...windows.map((window) => el('div', { class: 'quota-window' }, [el('span', { text: window.label }), el('strong', { text: `${window.mode === 'used' ? '已用' : '剩余'} ${window.percent}%` }), el('small', { text: window.resetLabel })])));
}

function observerEventKey(event, occurrence) { return `${event.elapsedSeconds}:${event.label}:${occurrence}`; }
function renderTerminal() {
  const terminal = state.live[state.activeContactId]?.terminal || { available: false, busy: false, phase: 'unavailable', events: [] };
  const status = $('#terminal-status'); const list = $('#terminal-events');
  status.textContent = !terminal.available ? '观察器暂不可用' : terminal.busy ? terminal.phase : '空闲 · 没有正在运行的安全事件';
  const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 28;
  const seen = new Map(); const desired = [];
  terminal.events.forEach((event) => { const base = `${event.elapsedSeconds}:${event.label}`; const count = seen.get(base) || 0; seen.set(base, count + 1); desired.push({ key: observerEventKey(event, count), event }); });
  const existing = new Map([...list.children].map((node) => [node.dataset.key, node]));
  const nodes = desired.map(({ key, event }) => {
    let node = existing.get(key);
    if (!node) { node = el('li', { 'data-key': key }); node.append(el('time'), el('span')); }
    node.querySelector('time').textContent = `${String(Math.floor(event.elapsedSeconds / 60)).padStart(2, '0')}:${String(event.elapsedSeconds % 60).padStart(2, '0')}`;
    node.querySelector('span').textContent = event.label;
    return node;
  });
  list.replaceChildren(...nodes);
  if (nearEnd) requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
}
function renderActivities() {
  const live = state.live[state.activeContactId] || { replyState: 'idle' }; const strip = $('#activity-strip'); strip.replaceChildren();
  if (!isBusy()) return;
  const label = live.activityText || (live.replyState === 'queued' ? '正在排队' : '正在处理');
  strip.append(el('div', { class: 'activity-strip' }, [el('span', { class: 'activity-pulse', 'aria-hidden': 'true' }), el('span', { text: label }), live.activityCount ? el('span', { class: 'activity-count', text: `${live.activityCount} 个动作` }) : document.createTextNode('')]));
}
function nearBottom() { const scroll = $('#conversation-scroll'); return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 88; }
function updateLatestControl() { const follow = state.followLatest[state.activeContactId] !== false; $('#latest-button').hidden = follow; }
function scrollLatest() { const scroll = $('#conversation-scroll'); scroll.scrollTop = scroll.scrollHeight; state.followLatest[state.activeContactId] = true; updateLatestControl(); }
function renderMessageBody(message) {
  const source = message.body || '…'; const parts = parseStickerParts(source, state.stickerCatalog);
  const stickers = parts.filter(({ type }) => type === 'sticker'); const standalone = stickers.length > 0 && parts.every((part) => part.type === 'sticker' || !part.value.trim());
  const body = el(standalone ? 'div' : 'p', { class: `message-body ${standalone ? 'sticker-only-message' : stickers.length ? 'sticker-mixed-message' : ''}` });
  parts.forEach((part) => {
    if (part.type === 'text') body.append(document.createTextNode(part.value));
    else body.append(el('img', { class: standalone ? 'message-sticker-large' : 'message-sticker-inline', src: part.sticker.url, alt: part.sticker.label, loading: 'lazy', decoding: 'async' }));
  });
  if (message.streaming) body.classList.add('is-streaming'); return { body, standalone };
}
function renderMessages({ forceLatest = false } = {}) {
  const shouldFollow = forceLatest || (state.followLatest[state.activeContactId] !== false && nearBottom());
  const history = composeLiveMessages(state.histories[state.activeContactId] || [], state.live[state.activeContactId], { contactId: state.activeContactId, stream: state.streams[state.activeContactId] });
  const showTyping = shouldShowTypingBubble(state.histories[state.activeContactId] || [], state.live[state.activeContactId], { contactId: state.activeContactId, stream: state.streams[state.activeContactId] });
  const typingStatus = $('#typing-status');
  const wasTyping = typingStatus.dataset.active === 'true';
  if (showTyping !== wasTyping) {
    typingStatus.dataset.active = String(showTyping);
    typingStatus.setAttribute('aria-hidden', String(!showTyping));
    typingStatus.textContent = showTyping ? '正在输入…' : '';
  }
  const existingTyping = messages.querySelector(`.typing-bubble[data-contact-id="${state.activeContactId}"]`);
  const typingBubble = showTyping ? (existingTyping || createTypingBubble()) : null;
  messages.replaceChildren(...history.filter((message) => !(message.role === 'assistant' && message.streaming && !String(message.body || '').trim())).map((message) => {
    const item = el('li', { class: `message ${message.role === 'user' ? 'from-user' : 'from-assistant'}` });
    const meta = el('div', { class: 'message-meta' }, [el('span', { text: message.role === 'user' ? 'ASTRA' : contact().name.toUpperCase() }), el('time', { text: message.time || '刚刚' })]);
    const rendered = renderMessageBody(message); item.classList.toggle('has-sticker-only', rendered.standalone); item.append(meta, rendered.body);
    if (message.attachments?.length) item.append(el('div', { class: 'message-files', role: 'list', 'aria-label': '消息附件' }, message.attachments.map(renderAttachment)));
    return item;
  }), ...(typingBubble ? [typingBubble] : []));
  requestAnimationFrame(() => { if (shouldFollow) scrollLatest(); else updateLatestControl(); });
}
function createTypingBubble() {
  const item = el('li', { class: 'message from-assistant typing-bubble', 'data-contact-id': state.activeContactId, 'aria-hidden': 'true' });
  const meta = el('div', { class: 'message-meta' }, [el('span', { text: contact().name.toUpperCase() }), el('time', { text: '现在' })]);
  const body = el('p', { class: 'message-body typing-bubble-body' });
  body.append(el('span', { class: 'typing-dots', 'aria-hidden': 'true' }, [el('i'), el('i'), el('i')]));
  item.append(meta, body);
  return item;
}
function renderAttachment(file) {
  const label = `打开${file.type === 'image' ? '图片' : '文件'}：${file.name}`;
  const link = el('a', { href: file.url, target: '_blank', rel: 'noreferrer', class: file.type === 'image' ? 'attachment-image' : 'attachment-file', role: 'listitem', 'aria-label': label });
  if (file.type === 'image') { const image = el('img', { src: file.url, alt: file.name, loading: 'lazy' }); link.append(image, el('span', { text: file.name })); } else link.textContent = `⌁ ${file.name}`;
  return link;
}
function renderWorkers() {
  const workers = state.live[state.activeContactId]?.workers || []; const list = $('#worker-list'); $('#worker-total').textContent = workers.length;
  if (!workers.length) { list.replaceChildren(el('p', { class: 'empty-workers', text: '没有协作 worker 在场。' })); return; }
  list.replaceChildren(...workers.map((worker) => { const labels = { running: '进行中', completed: '完成', interrupted: '中断', failed: '失败' }; const card = el('article', { class: `worker-card ${worker.state}` }); card.append(el('span', { class: 'worker-status', 'aria-hidden': 'true' }), el('div', { class: 'worker-copy' }, [el('strong', { text: worker.name }), el('small', { text: `${labels[worker.state]} · ${worker.count} 次` })])); return card; }));
}
function renderAttachments() {
  const tray = $('#attachment-tray'); const current = composer(); const operation = sendOperation(); const chips = current.attachments.map((file, index) => {
    const name = attachmentName(file, index);
    const chip = el('span', { class: 'attachment-chip' }, [el('span', { text: `⌁ ${name} · ${formatSize(file.size)}${operation?.progress ? ` · ${operation.progress}` : ''}` })]);
    const remove = el('button', { type: 'button', 'aria-label': `移除 ${name}`, text: '×', ...(operation ? { disabled: '' } : {}) }); remove.addEventListener('click', () => { if (sendOperation()) return; current.attachments.splice(index, 1); renderAttachments(); renderHeader(); }); chip.append(remove); return chip;
  });
  stickerTokens(current.text, state.stickerCatalog).forEach((sticker) => {
    const chip = el('span', { class: 'attachment-chip sticker-chip' }, [el('img', { src: sticker.url, alt: '', loading: 'lazy' }), el('span', { text: sticker.label })]);
    const remove = el('button', { type: 'button', 'aria-label': `移除表情包 ${sticker.label}`, text: '×', ...(operation ? { disabled: '' } : {}) });
    remove.addEventListener('click', () => { if (sendOperation()) return; current.text = removeStickerToken(current.text, sticker.index, sticker.token); input.value = current.text; autoResize(); renderAttachments(); renderHeader(); }); chip.append(remove); chips.push(chip);
  });
  tray.replaceChildren(...chips);
}
function renderAll(options) { renderContacts(); renderHeader(); renderActivities(); renderMessages(options); renderWorkers(); renderInstrument(); renderTerminal(); renderAttachments(); renderDateRule(); }

async function switchContact(id) {
  if (id === state.activeContactId || state.stickerUpload.uploading) return;
  closeStickerDialog();
  const epoch = ++state.epoch;
  state.scrollTops[state.activeContactId] = $('#conversation-scroll').scrollTop; composer().text = input.value; state.activeContactId = id;
  input.value = composer(id).text; state.followLatest[id] ??= true; autoResize(); syncWorkerPanel(); renderAll({ forceLatest: !Object.hasOwn(state.scrollTops, id) });
  if (Object.hasOwn(state.scrollTops, id)) requestAnimationFrame(() => { $('#conversation-scroll').scrollTop = state.scrollTops[id]; updateLatestControl(); });
  if (await refreshContact(id, { epoch }) && epoch === state.epoch && id === state.activeContactId) watchLive(id, epoch);
}
async function refreshContact(id, { epoch = state.epoch } = {}) {
  try { const [history, live] = await Promise.all([adapter.getHistory(id), adapter.getLiveState(id)]); if (epoch !== state.epoch) return false; const reconciled = reconcileHistorySnapshot(state.histories[id] || [], history, state.pendingCanonical[id] || []); state.histories[id] = reconciled.history; state.pendingCanonical[id] = reconciled.pendingIds; state.live[id] = live; if (id === state.activeContactId) renderAll(); else renderContacts(); }
  catch (error) { $('#connection-state').textContent = '离线草稿'; console.warn('CcCompanion adapter unavailable', error); }
  if (epoch !== state.epoch) return false;
  return true;
}
function updateMessage(contactId, message) {
  const result = upsertHistoryRecord(state.histories[contactId] || [], message);
  state.histories[contactId] = result.history;
}
async function send(event) {
  event.preventDefault(); const contactId = state.activeContactId; const draft = composer(contactId); const text = input.value.trim();
  if (sendOperation(contactId) || (!text && !draft.attachments.length) || !contactId || contact().readOnly) return;
  const preflight = attachmentPreflight(draft.attachments); if (preflight) { $('#connection-state').textContent = preflight; return; }
  const queued = [...draft.attachments]; const operation = state.composerState.begin(contactId); operation.uploading = queued.length > 0; let staged = [];
  draft.text = ''; input.value = ''; autoResize(); state.composerState.setProgress(contactId, operation, queued.length ? '准备上传' : '发送中');
  if (contactId === state.activeContactId) { renderAttachments(); renderHeader(); }
  try {
    staged = await adapter.uploadAttachments(contactId, queued, {
      signal: operation.controller.signal,
      onProgress: ({ index, totalFiles, loaded, total }) => {
        state.composerState.setProgress(contactId, operation, `${index + 1}/${totalFiles} · ${Math.round((loaded / total) * 100)}%`);
        if (contactId === state.activeContactId && state.composerState.isCurrent(contactId, operation)) renderAttachments();
      },
    });
    operation.uploading = false; if (contactId === state.activeContactId && state.composerState.isCurrent(contactId, operation)) renderHeader();
    const response = await adapter.sendMessage(contactId, { text, attachmentIds: staged.map(({ attachment_id }) => attachment_id) });
    // The server's record is authoritative (including its canonical id and
    // normalized attachment metadata). Render it immediately; the next
    // polling snapshot then replaces/deduplicates by that same id.
    const record = normalizeSendRecord(response);
    if (!record) throw new Error('发送响应缺少 canonical record');
    state.pendingCanonical[contactId] = [...new Set([...(state.pendingCanonical[contactId] || []), record.id])];
    const merged = upsertHistoryRecord(state.histories[contactId] || [], record);
    state.histories[contactId] = merged.history;
    if (contactId === state.activeContactId && state.composerState.isCurrent(contactId, operation)) renderMessages({ forceLatest: true });
    if (state.composerState.isCurrent(contactId, operation)) draft.attachments = [];
  } catch (error) {
    if (staged.length) await adapter.cancelUploads(staged).catch(() => {});
    // This contact's composer is disabled while an operation is active. The
    // identity check prevents a stale completion from restoring over a newer one.
    if (state.composerState.isCurrent(contactId, operation)) draft.text = text;
    if (contactId === state.activeContactId && state.composerState.isCurrent(contactId, operation)) { input.value = draft.text; autoResize(); $('#connection-state').textContent = error.name === 'AbortError' ? '上传已取消' : '发送失败'; }
  } finally {
    const current = state.composerState.isCurrent(contactId, operation); state.composerState.finish(contactId, operation);
    if (current && contactId === state.activeContactId) { renderAttachments(); renderHeader(); }
  }
}
function autoResize() { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }
async function openMemory() { const dialog = $('#memory-dialog'); dialog.showModal(); if (state.taxonomy) return renderTaxonomy(); $('#taxonomy').replaceChildren(el('p', { class: 'loading', text: '正在读取 taxonomy…' })); try { state.taxonomy = await adapter.getTaxonomy(); renderTaxonomy(); } catch { $('#taxonomy').replaceChildren(el('p', { class: 'error', text: '记忆库暂不可用；不会使用硬编码分类替代。' })); } }
function renderTaxonomy() {
  const area = $('#taxonomy'); const categories = state.taxonomy?.categories || []; area.replaceChildren(...categories.map((category) => {
    const group = el('section', { class: 'taxonomy-group' }); group.append(el('h3', { text: category.label })); const choices = el('div', { class: 'taxonomy-choices', role: 'list' }); const subs = category.subcategories || [];
    if (!subs.length) { const button = el('button', { type: 'button', class: state.selectedScope?.category === category.key && !state.selectedScope?.subcategory ? 'is-selected' : '', text: `${category.label} · ${category.count ?? 0}` }); button.addEventListener('click', () => selectMemory({ category: category.key })); choices.append(button); }
    subs.forEach((sub) => { const button = el('button', { type: 'button', class: state.selectedScope?.category === category.key && state.selectedScope?.subcategory === sub.key ? 'is-selected' : '', text: `${sub.label} · ${sub.count ?? 0}` }); button.addEventListener('click', () => selectMemory({ category: category.key, subcategory: sub.key })); choices.append(button); }); group.append(choices); return group;
  }));
}
async function selectMemory(scope) { state.selectedScope = scope; renderTaxonomy(); const result = $('#memory-results'); result.replaceChildren(el('p', { class: 'loading', text: '正在读取记忆…' })); try { const entries = await adapter.listMemories(scope); result.replaceChildren(...entries.map((entry) => el('article', { class: 'memory-card' }, [el('small', { text: entry.timestamp || entry.created_at || '记忆条目' }), el('h3', { text: entry.title || entry.content?.slice(0, 40) || '无标题' }), el('p', { text: entry.body || entry.content || '' })]))); } catch { result.replaceChildren(el('p', { class: 'error', text: '这个分类暂时无法读取。' })); } }
function applyLiveEvent(event) {
  if (event.type === 'message') updateMessage(event.contactId, event.message); if (event.type === 'state') state.live[event.contactId] = event.state; if (event.type === 'snapshot') { const reconciled = reconcileHistorySnapshot(state.histories[event.contactId] || [], event.history, state.pendingCanonical[event.contactId] || []); state.histories[event.contactId] = reconciled.history; state.pendingCanonical[event.contactId] = reconciled.pendingIds; state.live[event.contactId] = event.state; state.streams[event.contactId] = reconcileSnapshotStream(state.streams[event.contactId], event.state); }
  if (event.type === 'stream') {
    const next = reduceStreamDraft(state.streams[event.contactId], event.payload);
    state.streams[event.contactId] = next;
    const incomingTurnId = String(event.payload?.turn_id || event.payload?.turnId || event.payload?.stream_id || 'current');
    const incomingRevision = String(event.payload?.revision ?? event.payload?.draft_revision ?? '');
    const acceptedDraft = event.payload?.event === 'draft' && next?.turnId === incomingTurnId && String(next?.revision ?? '') === incomingRevision && (!event.payload?.updated_at || next?.updatedAt === event.payload.updated_at);
    if (acceptedDraft) state.live[event.contactId] = normalizeLiveState({
      ...(state.live[event.contactId] || {}), busy: true, reply_state: event.payload.reply_state || 'generating', turn_id: incomingTurnId, revision: incomingRevision, updated_at: event.payload.updated_at || '',
      draft: { text: event.payload.text || '', activity_text: event.payload.activity_text || '', activity_count: event.payload.activity_count || 0, worker_activity_items: event.payload.worker_activity_items || [] },
    });
    if (next?.terminal && event.payload?.refresh_history) void refreshContact(event.contactId, { epoch: state.epoch });
  }
  if (event.type === 'connection' && event.contactId === state.activeContactId && !event.online) $('#connection-state').textContent = '正在重连'; if (event.contactId === state.activeContactId) renderAll(); else renderContacts();
}
function watchLive(contactId, epoch = state.epoch) { state.stopLive?.(); state.stopLive = adapter.subscribe((event) => { if (epoch === state.epoch && contactId === state.activeContactId) applyLiveEvent(event); }, { contactId }); }
const workerDrawerQuery = window.matchMedia('(max-width:1199px)');
function syncWorkerPanel() {
  const panel = $('#side-notes'); const drawer = workerDrawerQuery.matches;
  if (!drawer) { document.body.classList.remove('workers-open'); panel.setAttribute('aria-hidden', 'false'); panel.inert = false; $('#worker-toggle').setAttribute('aria-expanded', 'false'); return; }
  const open = document.body.classList.contains('workers-open'); panel.setAttribute('aria-hidden', String(!open)); panel.inert = !open; $('#worker-toggle').setAttribute('aria-expanded', String(open));
}
function setWorkers(open, { returnFocus = false } = {}) { if (!workerDrawerQuery.matches) return syncWorkerPanel(); document.body.classList.toggle('workers-open', open); syncWorkerPanel(); if (open) $('#worker-close').focus(); else if (returnFocus) $('#worker-toggle').focus(); }
function closeWorkers() { setWorkers(false, { returnFocus: true }); }
function cycleAppearance() { const modes = ['default', 'reading', 'compact']; state.appearance = modes[(modes.indexOf(state.appearance) + 1) % modes.length]; document.body.dataset.appearance = state.appearance; const names = { default: '默认', reading: '阅读', compact: '紧凑' }; $('#appearance-button').setAttribute('aria-label', `切换显示模式：${names[state.appearance]}`); $('#appearance-button').title = `显示模式：${names[state.appearance]}`; $('#drawer-appearance-button').textContent = `显示模式：${names[state.appearance]}`; $('#appearance-status').textContent = `显示模式：${names[state.appearance]}`; }

function stickerSearchKey(value) { return String(value || '').normalize('NFC').toLocaleLowerCase('zh-CN'); }
function clearStickerUploadFile({ clearInput = false } = {}) {
  if (state.stickerUpload.previewUrl) URL.revokeObjectURL(state.stickerUpload.previewUrl);
  state.stickerUpload.file = null; state.stickerUpload.previewUrl = ''; const preview = $('#sticker-upload-preview'); preview.hidden = true; preview.removeAttribute('src');
  if (clearInput) $('#sticker-upload-file').value = '';
}
function resetStickerUpload() {
  clearStickerUploadFile();
  state.stickerUpload = { file: null, previewUrl: '', uploading: false, controller: null }; $('#sticker-upload-form').reset(); $('#sticker-upload-form').hidden = true; $('#sticker-grid').hidden = false;
  $('#sticker-tools').hidden = false; $('#sticker-categories').hidden = false; $('#sticker-upload-progress').hidden = true; $('#sticker-upload-error').textContent = ''; renderStickerCategoryFields();
}
function closeStickerDialog({ returnFocus = false } = {}) {
  const dialog = $('#sticker-dialog'); if (state.stickerUpload.uploading) return; const wasOpen = dialog.open;
  if (wasOpen) dialog.close(); resetStickerUpload(); $('#sticker-button').setAttribute('aria-expanded', 'false'); state.stickerTargetContactId = ''; if (returnFocus && wasOpen) $('#sticker-button').focus();
}
async function loadStickerCatalog({ force = false, targetCategory = '' } = {}) {
  if (state.stickerLoading || (state.stickerCatalog && !force)) return;
  const epoch = state.epoch; const loadId = ++state.stickerLoadId; state.stickerLoading = true; state.stickerError = ''; renderStickerPicker();
  try {
    const catalog = await adapter.getStickerCatalog(); if (epoch !== state.epoch || loadId !== state.stickerLoadId) return; state.stickerCatalog = catalog;
    const ids = new Set(catalog.categories.map(({ id }) => id)); state.stickerCategory = ids.has(targetCategory) ? targetCategory : (ids.has(state.stickerCategory) ? state.stickerCategory : '__all');
    renderMessages(); renderAttachments();
  } catch { if (epoch === state.epoch && loadId === state.stickerLoadId) state.stickerError = '目录暂时读取失败。'; }
  finally { if (loadId === state.stickerLoadId) { state.stickerLoading = false; if (epoch === state.epoch) renderStickerPicker(); } }
}
function renderStickerPicker() {
  const status = $('#sticker-status'); const categories = $('#sticker-categories'); const grid = $('#sticker-grid'); const catalog = state.stickerCatalog;
  if (state.stickerLoading) { status.textContent = '正在读取表情包…'; grid.replaceChildren(); return; }
  if (state.stickerError) {
    status.textContent = state.stickerError; const retry = el('button', { type: 'button', class: 'text-button', text: '重试目录' }); retry.addEventListener('click', () => loadStickerCatalog({ force: true })); grid.replaceChildren(retry); return;
  }
  if (!catalog) { status.textContent = ''; grid.replaceChildren(); return; }
  const categoryItems = [{ id: '__all', name: '全部' }, ...catalog.categories];
  categories.replaceChildren(...categoryItems.map((category, index) => {
    const active = category.id === state.stickerCategory; const button = el('button', { type: 'button', role: 'tab', 'aria-selected': String(active), tabindex: active ? '0' : '-1', 'data-category-id': category.id, text: category.name });
    button.addEventListener('click', () => { state.stickerCategory = category.id; renderStickerPicker(); });
    button.addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? categoryItems.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + categoryItems.length) % categoryItems.length; const targetId = categoryItems[next].id; state.stickerCategory = targetId; renderStickerPicker(); [...categories.querySelectorAll('[role="tab"]')].find((tab) => tab.dataset.categoryId === targetId)?.focus(); });
    return button;
  }));
  const query = stickerSearchKey(state.stickerSearch); const filtered = catalog.stickers.filter((sticker) => (state.stickerCategory === '__all' || sticker.categoryId === state.stickerCategory) && (!query || stickerSearchKey(`${sticker.label} ${sticker.name}`).includes(query)));
  status.textContent = filtered.length ? `${filtered.length} 张；点选后加入当前草稿，不会自动发送。` : '没有符合条件的表情包。';
  grid.replaceChildren(...filtered.map((sticker, index) => {
    const button = el('button', { type: 'button', class: 'sticker-tile', 'aria-label': `加入表情包 ${sticker.label}`, 'data-sticker-name': sticker.name });
    button.append(el('img', { src: sticker.url, alt: '', loading: index < 12 ? 'eager' : 'lazy', decoding: 'async' }), el('span', { text: sticker.label }));
    button.addEventListener('click', () => insertSticker(sticker));
    button.addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const tiles = [...grid.querySelectorAll('.sticker-tile')]; const columns = Math.max(1, Math.round(grid.clientWidth / Math.max(88, tiles[0]?.clientWidth || 88))); const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns, ArrowDown: columns }; const next = event.key === 'Home' ? 0 : event.key === 'End' ? tiles.length - 1 : Math.max(0, Math.min(tiles.length - 1, index + moves[event.key])); tiles[next]?.focus(); }); return button;
  }));
  $('#sticker-upload-open').disabled = !catalog.upload.supported; $('#sticker-upload-open').title = catalog.upload.supported ? '' : '服务器暂未开放上传'; renderStickerUploadCategories();
}
function insertSticker(sticker) {
  if (state.stickerTargetContactId !== state.activeContactId || sendOperation()) return;
  const draft = composer(); const selection = state.stickerSelection; const inserted = insertStickerToken(draft.text, selection.start, selection.end, sticker.name);
  draft.text = inserted.text; input.value = inserted.text; state.stickerSelection = { start: inserted.cursor, end: inserted.cursor }; autoResize(); renderAttachments(); renderHeader();
  requestAnimationFrame(() => input.setSelectionRange(inserted.cursor, inserted.cursor));
}
async function openStickerDialog() {
  if ($('#sticker-button').disabled) return; state.stickerTargetContactId = state.activeContactId; state.stickerSelection = { start: input.selectionStart ?? input.value.length, end: input.selectionEnd ?? input.value.length };
  const targetContactId = state.activeContactId; $('#sticker-dialog').showModal(); $('#sticker-button').setAttribute('aria-expanded', 'true'); $('#sticker-search').value = state.stickerSearch; renderStickerPicker(); await loadStickerCatalog(); if ($('#sticker-dialog').open && state.stickerTargetContactId === targetContactId) $('#sticker-search').focus();
}
function renderStickerUploadCategories() {
  const select = $('#sticker-upload-category'); const categories = (state.stickerCatalog?.categories || []).filter(({ id }) => id !== '__uncategorized'); const selected = select.value;
  select.replaceChildren(...categories.map((category) => el('option', { value: category.id, text: category.name })));
  if (categories.some(({ id }) => id === selected)) select.value = selected;
}
function renderStickerCategoryFields() {
  const mode = new FormData($('#sticker-upload-form')).get('sticker-category-mode') || 'existing'; $('#sticker-existing-wrap').hidden = mode !== 'existing'; $('#sticker-new-wrap').hidden = mode !== 'new';
}
function showStickerUpload() {
  if (!state.stickerCatalog?.upload.supported) return; renderStickerUploadCategories(); $('#sticker-grid').hidden = true; $('#sticker-tools').hidden = true; $('#sticker-categories').hidden = true; $('#sticker-status').textContent = '上传只在你按下“上传”后执行；失败不会自动重试。'; $('#sticker-upload-form').hidden = false; $('#sticker-upload-file').focus();
}
function setStickerUploadFile(file) {
  const contract = state.stickerCatalog?.upload; const error = $('#sticker-upload-error'); error.textContent = '';
  clearStickerUploadFile({ clearInput: !file });
  if (!file) return;
  if (!contract?.contentTypes.includes(file.type) || file.size <= 0 || file.size > contract.maxFileBytes) { error.textContent = `请选择服务器支持且不超过 ${formatLimit(contract?.maxFileBytes || 8 * 1024 * 1024)} 的图片。`; $('#sticker-upload-file').value = ''; return; }
  const previewUrl = URL.createObjectURL(file); state.stickerUpload.file = file; state.stickerUpload.previewUrl = previewUrl;
  const preview = $('#sticker-upload-preview'); preview.src = previewUrl; preview.hidden = false; const proposed = String(file.name || '').replace(/\.[^.]+$/u, '').normalize('NFC').slice(0, 80); if (isSafeStickerName(proposed) && !$('#sticker-upload-name').value) $('#sticker-upload-name').value = proposed;
}
async function uploadSticker(event) {
  event.preventDefault(); if (state.stickerUpload.uploading) return; const file = state.stickerUpload.file; const name = $('#sticker-upload-name').value.normalize('NFC'); const mode = new FormData(event.currentTarget).get('sticker-category-mode'); const categoryId = mode === 'existing' ? $('#sticker-upload-category').value : ''; const newCategoryName = mode === 'new' ? $('#sticker-upload-new-category').value.normalize('NFC') : '';
  const allowedCategory = (state.stickerCatalog?.categories || []).some(({ id }) => id === categoryId && id !== '__uncategorized'); const error = $('#sticker-upload-error');
  if (!file || !isSafeStickerName(name) || (mode === 'existing' ? !allowedCategory : !isSafeStickerName(newCategoryName))) { error.textContent = '请填写有效名称、图片和分类。'; return; }
  state.stickerUpload.uploading = true; state.stickerUpload.controller = new AbortController(); error.textContent = ''; const progress = $('#sticker-upload-progress'); progress.hidden = false; progress.value = 0; [...event.currentTarget.elements].forEach((control) => { control.disabled = true; });
  try {
    const result = await adapter.uploadSticker(file, { name, categoryId, newCategoryName, signal: state.stickerUpload.controller.signal, onProgress: (loaded, total) => { progress.value = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0; } });
    const targetCategory = String(result?.category?.id || categoryId); resetStickerUpload(); await loadStickerCatalog({ force: true, targetCategory });
    if (state.stickerError) $('#sticker-status').textContent = '上传成功，但目录刷新失败；请只重试目录。';
  } catch (reason) { error.textContent = reason?.message || '上传失败；表单已保留，可手动重试。'; }
  finally { state.stickerUpload.uploading = false; [...event.currentTarget.elements].forEach((control) => { control.disabled = false; }); }
}

$('#composer').addEventListener('submit', send);
input.addEventListener('input', () => { composer().text = input.value; autoResize(); renderAttachments(); renderHeader(); });
input.addEventListener('paste', (event) => {
  const contactId = state.activeContactId; const current = contact();
  if (!contactId || current.readOnly || sendOperation(contactId)) return;
  const images = extractClipboardImageFiles(event.clipboardData);
  if (!images.length) return;
  event.preventDefault();
  const draft = composer(contactId); const next = [...draft.attachments, ...images]; const error = attachmentPreflight(next);
  if (error) { $('#connection-state').textContent = error; return; }
  draft.attachments = next; renderAttachments(); renderHeader();
  $('#connection-state').textContent = `${images.length} 张图片已添加`;
});
input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); } });
$('#conversation-scroll').addEventListener('scroll', () => { state.followLatest[state.activeContactId] = nearBottom(); updateLatestControl(); }); $('#latest-button').addEventListener('click', scrollLatest);
$('#attachment-input').addEventListener('change', (event) => { const draft = composer(); const next = [...draft.attachments, ...event.target.files]; const error = attachmentPreflight(next); event.target.value = ''; if (error) { $('#connection-state').textContent = error; return; } draft.attachments = next; renderAttachments(); renderHeader(); });
$('.attach-button').addEventListener('keydown', (event) => { if ((event.key === 'Enter' || event.key === ' ') && !$('#attachment-input').disabled) { event.preventDefault(); $('#attachment-input').click(); } });
$('#sticker-button').addEventListener('click', openStickerDialog); $('#sticker-close').addEventListener('click', () => closeStickerDialog({ returnFocus: true }));
$('#sticker-dialog').addEventListener('cancel', (event) => { if (state.stickerUpload.uploading) event.preventDefault(); else { event.preventDefault(); closeStickerDialog({ returnFocus: true }); } });
$('#sticker-dialog').addEventListener('click', (event) => { if (event.target === event.currentTarget) closeStickerDialog({ returnFocus: true }); });
$('#sticker-search').addEventListener('input', (event) => { state.stickerSearch = event.target.value.normalize('NFC').slice(0, 80); renderStickerPicker(); });
$('#sticker-upload-open').addEventListener('click', showStickerUpload); $('#sticker-upload-cancel').addEventListener('click', () => { if (!state.stickerUpload.uploading) { resetStickerUpload(); renderStickerPicker(); $('#sticker-search').focus(); } });
$('#sticker-upload-file').addEventListener('change', (event) => setStickerUploadFile(event.target.files?.[0])); $('#sticker-upload-form').addEventListener('change', (event) => { if (event.target.name === 'sticker-category-mode') renderStickerCategoryFields(); }); $('#sticker-upload-form').addEventListener('submit', uploadSticker);
$('#upload-cancel-button').addEventListener('click', () => state.composerState.cancel(state.activeContactId)); $('#stop-button').addEventListener('click', () => adapter.stop(state.activeContactId, state.live[state.activeContactId]?.stopRequest).catch(() => { $('#connection-state').textContent = '停止请求失效'; }));
$('#memory-button').addEventListener('click', openMemory); $('#memory-close').addEventListener('click', () => $('#memory-dialog').close()); $('#worker-toggle').addEventListener('click', () => setWorkers(!document.body.classList.contains('workers-open'))); $('#worker-close').addEventListener('click', closeWorkers);
async function logout() { state.epoch += 1; state.stickerLoadId += 1; state.composerState.cancelAll(); state.stickerUpload.controller?.abort(); state.stickerUpload.uploading = false; closeStickerDialog(); state.stickerCatalog = null; state.stickerLoading = false; state.stickerError = ''; state.stickerSearch = ''; state.stickerCategory = '__all'; state.stickerTargetContactId = ''; try { await adapter.logout(); } finally { state.stopLive?.(); state.stopLive = null; state.contacts = []; state.histories = {}; state.live = {}; state.streams = {}; state.pendingCanonical = {}; state.composerState = createComposerState(); state.activeContactId = ''; renderAll(); if (!bootstrap.mock) showLogin(); } }
$('#logout-button').addEventListener('click', logout); $('#drawer-logout-button').addEventListener('click', logout);
$('#appearance-button').addEventListener('click', cycleAppearance); $('#drawer-appearance-button').addEventListener('click', cycleAppearance);
window.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); input.focus(); } if (event.altKey && /^[1-9]$/.test(event.key)) { const target = state.contacts[Number(event.key) - 1]; if (target) switchContact(target.id); } if (event.key === 'Escape') { if ($('#sticker-dialog').open) closeStickerDialog({ returnFocus: true }); if ($('#memory-dialog').open) $('#memory-dialog').close(); if (document.body.classList.contains('workers-open')) closeWorkers(); } });
window.addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); state.deferredInstall = event; $('#install-button').hidden = false; }); $('#install-button').addEventListener('click', async () => { await state.deferredInstall?.prompt(); state.deferredInstall = null; $('#install-button').hidden = true; });
workerDrawerQuery.addEventListener('change', syncWorkerPanel); syncWorkerPanel(); document.body.append($('#svg-defs').content.cloneNode(true)); if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js'));
async function start() {
  try { await bootstrap.checkSession(); } catch (error) { if (!bootstrap.mock) { showLogin(); return; } }
  try { state.contacts = await adapter.contacts(); if (!state.contacts.length) throw new Error('没有可用联系人'); state.activeContactId = state.contacts.find(({ id }) => id === 'xiaoke')?.id || state.contacts[0].id; state.contacts.forEach(({ id }) => { state.histories[id] = []; state.live[id] = { replyState: 'idle', statusText: '待命', workers: [] }; composer(id); state.followLatest[id] = true; }); await Promise.all(state.contacts.map(({ id }) => refreshContact(id))); watchLive(state.activeContactId); renderAll({ forceLatest: true }); void loadStickerCatalog(); }
  catch (error) { $('#connection-state').textContent = '连接不可用'; console.warn('PWA bootstrap failed', error); }
}
function pairingError(error) {
  if (error?.status === 429) return '尝试次数过多，请稍后再生成新码。';
  if ([400, 401, 404, 410].includes(error?.status)) return '配对码无效或已过期；请在 Android 上刷新后重试。';
  return '暂时无法连接；请检查网络后重试。';
}
function showLogin() {
  const panel = $('#login-panel'); const codeInput = $('#pairing-code'); const pairingSubmit = $('#pairing-submit');
  panel.hidden = false; codeInput.focus();
  if (state.loginBound) return;
  state.loginBound = true;
  codeInput.addEventListener('input', () => { codeInput.value = formatPairingCode(codeInput.value); pairingSubmit.disabled = !normalizePairingCode(codeInput.value); $('#pairing-error').textContent = ''; });
  $('#pairing-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const code = normalizePairingCode(codeInput.value); const error = $('#pairing-error');
    if (!code || pairingSubmit.disabled) return;
    pairingSubmit.disabled = true; pairingSubmit.textContent = '正在连接…'; error.textContent = '';
    try { await bootstrap.establishPairingSession({ code }); codeInput.value = ''; panel.hidden = true; await start(); }
    catch (reason) { error.textContent = pairingError(reason); codeInput.focus(); }
    finally { codeInput.value = ''; pairingSubmit.textContent = '连接工作台 ↗'; pairingSubmit.disabled = true; }
  });
  $('#login-form').addEventListener('submit', async (event) => { event.preventDefault(); const submit = $('#login-submit'); const error = $('#login-error'); submit.disabled = true; error.textContent = ''; try { await bootstrap.establishSession({ username: $('#login-username').value, password: $('#login-password').value }); panel.hidden = true; await start(); } catch { error.textContent = '登录未通过，请检查账号或连接。'; } finally { $('#login-password').value = ''; submit.disabled = false; } });
}
renderAll(); start();
