import { createPwaBootstrap } from './bootstrap.js';

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
  contacts: [], activeContactId: '', histories: {}, live: {},
  attachments: [], taxonomy: null, selectedScope: null, deferredInstall: null, loginBound: false, stopLive: null, uploadController: null, uploadProgress: '',
};

const contactList = $('#contact-list'); const messages = $('#message-list'); const input = $('#message-input');

function contact() { return state.contacts.find(({ id }) => id === state.activeContactId) || { id: '', name: '连接中', channel: 'PRIVATE CHANNEL', note: '', capabilities: [] }; }
function isBusy() { const live = state.live[state.activeContactId]; return Boolean(live?.busy || ['generating', 'queued'].includes(live?.replyState)); }
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

function renderContacts() {
  contactList.replaceChildren(...state.contacts.map((item) => {
    const busy = Boolean(state.live[item.id]?.busy || ['generating', 'queued'].includes(state.live[item.id]?.replyState));
    const button = el('button', { class: `contact-button ${item.id === state.activeContactId ? 'is-active' : ''}`, type: 'button', 'aria-current': item.id === state.activeContactId ? 'page' : 'false' });
    const accent = item.id === 'kairos' ? 'brass' : 'oxide';
    button.append(el('span', { class: `avatar ${accent}`, text: item.name.slice(0, 1) }), el('span', { class: 'contact-copy' }, [el('strong', { text: item.name }), el('small', { text: item.note })]), el('span', { class: `contact-led ${busy ? 'is-busy' : ''}`, 'aria-label': busy ? '正在处理' : '待命' }));
    button.addEventListener('click', () => switchContact(item.id)); return button;
  }));
}

function renderHeader() {
  const current = contact(); const live = state.live[current.id] || { replyState: 'idle', statusText: '待命', workers: [] };
  $('#contact-name').textContent = current.name; $('#contact-mode').textContent = current.channel;
  $('#message-input').placeholder = `写给${current.name}…`; $('#state-channel').textContent = `${current.name} / ${current.channel === 'CODEX APP' ? 'Kairos' : 'CC'}`;
  $('#state-status').textContent = live.statusText || '待命'; $('#connection-state').textContent = isBusy() ? (live.statusText || '正在处理') : '已连接';
  $('#signal-dot').classList.toggle('is-busy', isBusy()); $('#stop-button').hidden = !(isBusy() && live.stopRequest?.supported); $('#send-button').disabled = !current.id || Boolean(current.readOnly) || (isBusy() && !input.value.trim());
}

function renderActivities() {
  const live = state.live[state.activeContactId] || { replyState: 'idle' }; const strip = $('#activity-strip'); strip.replaceChildren();
  if (!isBusy()) return;
  const label = live.activityText || (live.replyState === 'queued' ? '正在排队' : '正在处理');
  strip.append(el('div', { class: 'activity-strip' }, [el('span', { class: 'activity-pulse', 'aria-hidden': 'true' }), el('span', { text: label }), live.activityCount ? el('span', { class: 'activity-count', text: `${live.activityCount} 个动作` }) : document.createTextNode('')]));
}

function renderMessages() {
  const history = state.histories[state.activeContactId] || []; messages.replaceChildren(...history.map((message) => {
    const item = el('li', { class: `message ${message.role === 'user' ? 'from-user' : 'from-assistant'}` });
    const meta = el('div', { class: 'message-meta' }, [el('span', { text: message.role === 'user' ? 'ASTRA' : contact().name.toUpperCase() }), el('time', { text: message.time || '刚刚' })]);
    const body = el('p', { class: 'message-body', text: message.body || '…' }); if (message.streaming) body.classList.add('is-streaming');
    item.append(meta, body);
    if (message.attachments?.length) item.append(el('div', { class: 'message-files', role: 'list', 'aria-label': '消息附件' }, message.attachments.map((file) => renderAttachment(file))));
    return item;
  }));
  requestAnimationFrame(() => { const scroll = $('#conversation-scroll'); scroll.scrollTop = scroll.scrollHeight; });
}

function renderAttachment(file) {
  const label = `打开${file.type === 'image' ? '图片' : '文件'}：${file.name}`;
  const link = el('a', { href: file.url, target: '_blank', rel: 'noreferrer', class: file.type === 'image' ? 'attachment-image' : 'attachment-file', role: 'listitem', 'aria-label': label });
  if (file.type === 'image') {
    const image = el('img', { src: file.url, alt: file.name, loading: 'lazy' });
    link.append(image, el('span', { text: file.name }));
  } else link.textContent = `⌁ ${file.name}`;
  return link;
}

function renderWorkers() {
  const workers = state.live[state.activeContactId]?.workers || []; const list = $('#worker-list'); $('#worker-total').textContent = workers.length;
  if (!workers.length) { list.replaceChildren(el('p', { class: 'empty-workers', text: '没有协作 worker 在场。' })); return; }
  list.replaceChildren(...workers.map((worker) => {
    const labels = { running: '进行中', completed: '完成', interrupted: '中断', failed: '失败' };
    const card = el('article', { class: `worker-card ${worker.state}` });
    card.append(el('span', { class: 'worker-status', 'aria-hidden': 'true' }), el('div', { class: 'worker-copy' }, [el('strong', { text: worker.name }), el('small', { text: `${labels[worker.state]} · ${worker.count} 次` })])); return card;
  }));
}

function renderAttachments() {
  const tray = $('#attachment-tray'); tray.replaceChildren(...state.attachments.map((file, index) => {
    const chip = el('span', { class: 'attachment-chip' }, [el('span', { text: `⌁ ${file.name} · ${formatSize(file.size)}${state.uploadProgress ? ` · ${state.uploadProgress}` : ''}` })]);
    const remove = el('button', { type: 'button', 'aria-label': `移除 ${file.name}`, text: '×' }); remove.addEventListener('click', () => { state.attachments.splice(index, 1); renderAttachments(); }); chip.append(remove); return chip;
  }));
}

function renderAll() { renderContacts(); renderHeader(); renderActivities(); renderMessages(); renderWorkers(); renderAttachments(); }

async function switchContact(id) {
  state.activeContactId = id; state.attachments = []; input.value = ''; autoResize(); renderAll(); await refreshContact(id); watchLive(id);
}

async function refreshContact(id) {
  try {
    const [history, live] = await Promise.all([adapter.getHistory(id), adapter.getLiveState(id)]);
    state.histories[id] = history; state.live[id] = live; if (id === state.activeContactId) renderAll(); else renderContacts();
  } catch (error) { $('#connection-state').textContent = '离线草稿'; console.warn('CcCompanion adapter unavailable', error); }
}

function updateMessage(contactId, message) {
  const history = state.histories[contactId] || []; const found = history.findIndex(({ id }) => id === message.id);
  if (found >= 0) history[found] = message; else history.push(message); state.histories[contactId] = history;
}

async function send(event) {
  event.preventDefault(); const text = input.value.trim(); if ((!text && !state.attachments.length) || !contact().id || contact().readOnly) return;
  const preflight = attachmentPreflight(state.attachments); if (preflight) { $('#connection-state').textContent = preflight; return; }
  const queued = state.attachments; let staged = []; input.value = ''; autoResize();
  state.uploadController = new AbortController(); state.uploadProgress = queued.length ? '准备上传' : ''; renderAttachments(); renderHeader(); $('#upload-cancel-button').hidden = !queued.length;
  try {
    staged = await adapter.uploadAttachments(state.activeContactId, queued, {
      signal: state.uploadController.signal,
      onProgress: ({ index, totalFiles, loaded, total }) => { state.uploadProgress = `${index + 1}/${totalFiles} · ${Math.round((loaded / total) * 100)}%`; renderAttachments(); },
    });
    state.uploadProgress = queued.length ? '已暂存' : ''; renderAttachments(); state.attachments = []; renderAttachments();
    await adapter.sendMessage(state.activeContactId, { text, attachmentIds: staged.map(({ attachment_id }) => attachment_id) });
  } catch (error) {
    if (staged.length) await adapter.cancelUploads(staged).catch(() => {});
    state.attachments = queued; input.value = text; state.uploadProgress = error.name === 'AbortError' ? '已取消' : '上传失败'; renderAttachments(); $('#connection-state').textContent = error.name === 'AbortError' ? '上传已取消' : '发送失败';
  } finally { state.uploadController = null; state.uploadProgress = ''; $('#upload-cancel-button').hidden = true; renderAttachments(); renderHeader(); }
}

function autoResize() { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }

async function openMemory() {
  const dialog = $('#memory-dialog'); dialog.showModal();
  if (state.taxonomy) return renderTaxonomy();
  $('#taxonomy').replaceChildren(el('p', { class: 'loading', text: '正在读取 taxonomy…' }));
  try { state.taxonomy = await adapter.getTaxonomy(); renderTaxonomy(); } catch { $('#taxonomy').replaceChildren(el('p', { class: 'error', text: '记忆库暂不可用；不会使用硬编码分类替代。' })); }
}

function renderTaxonomy() {
  const area = $('#taxonomy'); const categories = state.taxonomy?.categories || []; area.replaceChildren(...categories.map((category) => {
    const group = el('section', { class: 'taxonomy-group' }); group.append(el('h3', { text: category.label }));
    const choices = el('div', { class: 'taxonomy-choices', role: 'list' });
    (category.subcategories || []).forEach((sub) => { const button = el('button', { type: 'button', class: state.selectedScope?.subcategory === sub.key ? 'is-selected' : '', text: `${sub.label} · ${sub.count ?? 0}` }); button.addEventListener('click', () => selectMemory({ category: category.key, subcategory: sub.key })); choices.append(button); }); group.append(choices); return group;
  }));
}

async function selectMemory(scope) {
  state.selectedScope = scope; renderTaxonomy(); const result = $('#memory-results'); result.replaceChildren(el('p', { class: 'loading', text: '正在读取记忆…' }));
  try { const entries = await adapter.listMemories(scope); result.replaceChildren(...entries.map((entry) => el('article', { class: 'memory-card' }, [el('small', { text: entry.timestamp || entry.created_at || '记忆条目' }), el('h3', { text: entry.title || entry.content?.slice(0, 40) || '无标题' }), el('p', { text: entry.body || entry.content || '' })]))); }
  catch { result.replaceChildren(el('p', { class: 'error', text: '这个分类暂时无法读取。' })); }
}

function applyLiveEvent(event) {
  if (event.type === 'message') updateMessage(event.contactId, event.message);
  if (event.type === 'state') state.live[event.contactId] = event.state;
  if (event.type === 'snapshot') { state.histories[event.contactId] = event.history; state.live[event.contactId] = event.state; }
  if (event.type === 'stream') {
    const payload = event.payload || {}; const streamId = `stream-${payload.stream_id || 'current'}`;
    const history = state.histories[event.contactId] || []; let item = history.find(({ id }) => id === streamId);
    if (!item) { item = { id: streamId, role: 'assistant', body: '', time: '现在', streaming: true }; history.push(item); }
    if (payload.event === 'chunk') item.body += String(payload.text || '');
    if (payload.event === 'done') item.streaming = false;
    state.histories[event.contactId] = history;
  }
  if (event.type === 'connection' && event.contactId === state.activeContactId && !event.online) $('#connection-state').textContent = '正在重连';
  if (event.contactId === state.activeContactId) renderAll(); else renderContacts();
}

function watchLive(contactId) {
  state.stopLive?.();
  state.stopLive = adapter.subscribe(applyLiveEvent, { contactId });
}

$('#composer').addEventListener('submit', send);
input.addEventListener('input', () => { autoResize(); renderHeader(); });
input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); } });
$('#attachment-input').addEventListener('change', (event) => {
  const next = [...state.attachments, ...event.target.files]; const error = attachmentPreflight(next); event.target.value = '';
  if (error) { $('#connection-state').textContent = error; return; }
  state.attachments = next; renderAttachments();
});
$('#upload-cancel-button').addEventListener('click', () => state.uploadController?.abort());
$('#stop-button').addEventListener('click', () => adapter.stop(state.activeContactId, state.live[state.activeContactId]?.stopRequest).catch(() => { $('#connection-state').textContent = '停止请求失效'; }));
$('#memory-button').addEventListener('click', openMemory); $('#memory-close').addEventListener('click', () => $('#memory-dialog').close());
$('#logout-button').addEventListener('click', async () => {
  try { await adapter.logout(); } finally {
    state.stopLive?.(); state.stopLive = null; state.contacts = []; state.histories = {}; state.live = {}; state.activeContactId = ''; state.attachments = []; renderAll();
    if (!bootstrap.mock) showLogin();
  }
});
$('#appearance-button').addEventListener('click', () => document.body.classList.toggle('compact'));
window.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); input.focus(); } if (event.altKey && /^[1-9]$/.test(event.key)) { const target = state.contacts[Number(event.key) - 1]; if (target) switchContact(target.id); } if (event.key === 'Escape' && $('#memory-dialog').open) $('#memory-dialog').close(); });
window.addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); state.deferredInstall = event; $('#install-button').hidden = false; });
$('#install-button').addEventListener('click', async () => { await state.deferredInstall?.prompt(); state.deferredInstall = null; $('#install-button').hidden = true; });

document.body.append($('#svg-defs').content.cloneNode(true));
if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js'));

async function start() {
  try {
    await bootstrap.checkSession();
  } catch (error) {
    if (!bootstrap.mock) { showLogin(); return; }
  }
  try {
    state.contacts = await adapter.contacts();
    if (!state.contacts.length) throw new Error('没有可用联系人');
    state.activeContactId = state.contacts.find(({ id }) => id === 'xiaoke')?.id || state.contacts[0].id;
    state.contacts.forEach(({ id }) => { state.histories[id] = []; state.live[id] = { replyState: 'idle', statusText: '待命', workers: [] }; });
    await Promise.all(state.contacts.map(({ id }) => refreshContact(id))); watchLive(state.activeContactId); renderAll();
  } catch (error) { $('#connection-state').textContent = '连接不可用'; console.warn('PWA bootstrap failed', error); }
}

function showLogin() {
  const panel = $('#login-panel'); panel.hidden = false; $('#login-username').focus();
  if (state.loginBound) return;
  state.loginBound = true;
  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const submit = $('#login-submit'); const error = $('#login-error'); submit.disabled = true; error.textContent = '';
    try { await bootstrap.establishSession({ username: $('#login-username').value, password: $('#login-password').value }); panel.hidden = true; await start(); }
    catch { error.textContent = '登录未通过，请检查账号或连接。'; } finally { submit.disabled = false; }
  });
}

renderAll(); start();
