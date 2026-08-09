import { createPwaBootstrap } from './bootstrap.js?v=6';
import { createComposerState } from './composer-state.js?v=6';
import { formatPairingCode, normalizePairingCode } from './pairing-code.js?v=6';
import { composeLiveMessages, reconcileSnapshotStream, reduceStreamDraft } from './live-messages.js?v=6';
import { normalizeLiveState } from './api.js?v=6';

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
};
const contactList = $('#contact-list'); const messages = $('#message-list'); const input = $('#message-input');

function contact() { return state.contacts.find(({ id }) => id === state.activeContactId) || { id: '', name: '连接中', channel: 'PRIVATE CHANNEL', note: '', capabilities: [] }; }
function isBusy(id = state.activeContactId) { const live = state.live[id]; return Boolean(live?.busy || ['generating', 'queued'].includes(live?.replyState)); }
function composer(id = state.activeContactId) { return state.composerState.get(id); }
function sendOperation(id = state.activeContactId) { return state.composerState.current(id); }
function formatSize(size) { return size > 1_000_000 ? `${(size / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1000))} KB`; }
function formatLimit(size) { return `${Math.max(1, Math.round(size / (1024 * 1024)))} MiB`; }
function attachmentPreflight(files) {
  const limits = adapter.getUploadLimits?.() || { max_file_bytes: 50 * 1024 * 1024, max_pending_files: 10, max_pending_bytes: 64 * 1024 * 1024 };
  if (files.length > limits.max_pending_files) return `一次最多选择 ${limits.max_pending_files} 个文件`;
  const total = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  if (total > limits.max_pending_bytes) return `附件合计不能超过 ${formatLimit(limits.max_pending_bytes)}`;
  const tooLarge = files.find((file) => !file.size || file.size > limits.max_file_bytes);
  return tooLarge ? `${tooLarge.name} 为空或超过 ${formatLimit(limits.max_file_bytes)}` : '';
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
  input.placeholder = `写给${current.name}…`; $('#state-channel').textContent = `${current.name} / ${current.channel === 'CODEX APP' ? 'Kairos' : 'CC'}`;
  $('#state-status').textContent = live.statusText || '待命'; $('#connection-state').textContent = isBusy() ? (live.statusText || '正在处理') : '已连接';
  $('#signal-dot').classList.toggle('is-busy', isBusy()); $('#stop-button').hidden = !(isBusy() && live.stopRequest?.supported);
  input.disabled = sending; $('#attachment-input').disabled = sending; $('.attach-button').classList.toggle('is-disabled', sending); $('.attach-button').setAttribute('aria-disabled', String(sending));
  $('#send-button').disabled = sending || !current.id || Boolean(current.readOnly) || (!composer().text.trim() && !composer().attachments.length);
  $('#upload-cancel-button').hidden = !(sending && sendOperation(current.id)?.uploading); $('#upload-cancel-button').disabled = !(sending && sendOperation(current.id)?.uploading);
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
function renderMessages({ forceLatest = false } = {}) {
  const shouldFollow = forceLatest || (state.followLatest[state.activeContactId] !== false && nearBottom());
  const history = composeLiveMessages(state.histories[state.activeContactId] || [], state.live[state.activeContactId], { contactId: state.activeContactId, stream: state.streams[state.activeContactId] }); messages.replaceChildren(...history.map((message) => {
    const item = el('li', { class: `message ${message.role === 'user' ? 'from-user' : 'from-assistant'}` });
    const meta = el('div', { class: 'message-meta' }, [el('span', { text: message.role === 'user' ? 'ASTRA' : contact().name.toUpperCase() }), el('time', { text: message.time || '刚刚' })]);
    const body = el('p', { class: 'message-body', text: message.body || '…' }); if (message.streaming) body.classList.add('is-streaming'); item.append(meta, body);
    if (message.attachments?.length) item.append(el('div', { class: 'message-files', role: 'list', 'aria-label': '消息附件' }, message.attachments.map(renderAttachment)));
    return item;
  }));
  requestAnimationFrame(() => { if (shouldFollow) scrollLatest(); else updateLatestControl(); });
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
  const tray = $('#attachment-tray'); const current = composer(); const operation = sendOperation(); tray.replaceChildren(...current.attachments.map((file, index) => {
    const chip = el('span', { class: 'attachment-chip' }, [el('span', { text: `⌁ ${file.name} · ${formatSize(file.size)}${operation?.progress ? ` · ${operation.progress}` : ''}` })]);
    const remove = el('button', { type: 'button', 'aria-label': `移除 ${file.name}`, text: '×', ...(operation ? { disabled: '' } : {}) }); remove.addEventListener('click', () => { if (sendOperation()) return; current.attachments.splice(index, 1); renderAttachments(); renderHeader(); }); chip.append(remove); return chip;
  }));
}
function renderAll(options) { renderContacts(); renderHeader(); renderActivities(); renderMessages(options); renderWorkers(); renderAttachments(); renderDateRule(); }

async function switchContact(id) {
  if (id === state.activeContactId) return;
  const epoch = ++state.epoch;
  state.scrollTops[state.activeContactId] = $('#conversation-scroll').scrollTop; composer().text = input.value; state.activeContactId = id;
  input.value = composer(id).text; state.followLatest[id] ??= true; autoResize(); syncWorkerPanel(); renderAll({ forceLatest: !Object.hasOwn(state.scrollTops, id) });
  if (Object.hasOwn(state.scrollTops, id)) requestAnimationFrame(() => { $('#conversation-scroll').scrollTop = state.scrollTops[id]; updateLatestControl(); });
  if (await refreshContact(id, { epoch }) && epoch === state.epoch && id === state.activeContactId) watchLive(id, epoch);
}
async function refreshContact(id, { epoch = state.epoch } = {}) {
  try { const [history, live] = await Promise.all([adapter.getHistory(id), adapter.getLiveState(id)]); if (epoch !== state.epoch) return false; state.histories[id] = history; state.live[id] = live; if (id === state.activeContactId) renderAll(); else renderContacts(); }
  catch (error) { $('#connection-state').textContent = '离线草稿'; console.warn('CcCompanion adapter unavailable', error); }
  if (epoch !== state.epoch) return false;
  return true;
}
function updateMessage(contactId, message) { const history = state.histories[contactId] || []; const found = history.findIndex(({ id }) => id === message.id); if (found >= 0) history[found] = message; else history.push(message); state.histories[contactId] = history; }
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
    await adapter.sendMessage(contactId, { text, attachmentIds: staged.map(({ attachment_id }) => attachment_id) });
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
  if (event.type === 'message') updateMessage(event.contactId, event.message); if (event.type === 'state') state.live[event.contactId] = event.state; if (event.type === 'snapshot') { state.histories[event.contactId] = event.history; state.live[event.contactId] = event.state; state.streams[event.contactId] = reconcileSnapshotStream(state.streams[event.contactId], event.state); }
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

$('#composer').addEventListener('submit', send);
input.addEventListener('input', () => { composer().text = input.value; autoResize(); renderHeader(); });
input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); } });
$('#conversation-scroll').addEventListener('scroll', () => { state.followLatest[state.activeContactId] = nearBottom(); updateLatestControl(); }); $('#latest-button').addEventListener('click', scrollLatest);
$('#attachment-input').addEventListener('change', (event) => { const draft = composer(); const next = [...draft.attachments, ...event.target.files]; const error = attachmentPreflight(next); event.target.value = ''; if (error) { $('#connection-state').textContent = error; return; } draft.attachments = next; renderAttachments(); renderHeader(); });
$('.attach-button').addEventListener('keydown', (event) => { if ((event.key === 'Enter' || event.key === ' ') && !$('#attachment-input').disabled) { event.preventDefault(); $('#attachment-input').click(); } });
$('#upload-cancel-button').addEventListener('click', () => state.composerState.cancel(state.activeContactId)); $('#stop-button').addEventListener('click', () => adapter.stop(state.activeContactId, state.live[state.activeContactId]?.stopRequest).catch(() => { $('#connection-state').textContent = '停止请求失效'; }));
$('#memory-button').addEventListener('click', openMemory); $('#memory-close').addEventListener('click', () => $('#memory-dialog').close()); $('#worker-toggle').addEventListener('click', () => setWorkers(!document.body.classList.contains('workers-open'))); $('#worker-close').addEventListener('click', closeWorkers);
async function logout() { state.epoch += 1; state.composerState.cancelAll(); try { await adapter.logout(); } finally { state.stopLive?.(); state.stopLive = null; state.contacts = []; state.histories = {}; state.live = {}; state.streams = {}; state.composerState = createComposerState(); state.activeContactId = ''; renderAll(); if (!bootstrap.mock) showLogin(); } }
$('#logout-button').addEventListener('click', logout); $('#drawer-logout-button').addEventListener('click', logout);
$('#appearance-button').addEventListener('click', cycleAppearance); $('#drawer-appearance-button').addEventListener('click', cycleAppearance);
window.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); input.focus(); } if (event.altKey && /^[1-9]$/.test(event.key)) { const target = state.contacts[Number(event.key) - 1]; if (target) switchContact(target.id); } if (event.key === 'Escape') { if ($('#memory-dialog').open) $('#memory-dialog').close(); if (document.body.classList.contains('workers-open')) closeWorkers(); } });
window.addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); state.deferredInstall = event; $('#install-button').hidden = false; }); $('#install-button').addEventListener('click', async () => { await state.deferredInstall?.prompt(); state.deferredInstall = null; $('#install-button').hidden = true; });
workerDrawerQuery.addEventListener('change', syncWorkerPanel); syncWorkerPanel(); document.body.append($('#svg-defs').content.cloneNode(true)); if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js'));
async function start() {
  try { await bootstrap.checkSession(); } catch (error) { if (!bootstrap.mock) { showLogin(); return; } }
  try { state.contacts = await adapter.contacts(); if (!state.contacts.length) throw new Error('没有可用联系人'); state.activeContactId = state.contacts.find(({ id }) => id === 'xiaoke')?.id || state.contacts[0].id; state.contacts.forEach(({ id }) => { state.histories[id] = []; state.live[id] = { replyState: 'idle', statusText: '待命', workers: [] }; composer(id); state.followLatest[id] = true; }); await Promise.all(state.contacts.map(({ id }) => refreshContact(id))); watchLive(state.activeContactId); renderAll({ forceLatest: true }); }
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
