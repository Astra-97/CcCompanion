import { createPwaBootstrap } from './bootstrap.js?v=25';
import { createComposerState } from './composer-state.js?v=25';
import { formatPairingCode, normalizePairingCode } from './pairing-code.js?v=25';
import { composeLiveMessages, shouldShowTypingBubble, reconcileSnapshotStream, reduceStreamDraft } from './live-messages.js?v=25';
import { normalizeLiveState, normalizeSendRecord, optimisticBusyState, stopControlState, reconcileHistorySnapshot, mergeHistoryDelta, historyCursor, upsertHistoryRecord } from './api.js?v=25';
import { extractClipboardImageFiles, resolveAttachmentFilename } from './clipboard-images.js?v=25';
import { insertStickerToken, isSafeStickerName, parseStickerParts, removeStickerToken, stickerTokens } from './sticker-protocol.js?v=25';
import { appendMarkdown, copyText, parseMarkdown } from './markdown.js?v=25';
import { createUnreadStore, previewText, seedUnreadCursors, unseenAssistant } from './unread.js?v=25';
import { createNotifier } from './notifications.js?v=25';
import { buildMonthGrid, monthKey, shiftMonth, todayKey } from './memory-calendar.js?v=25';
import { clockText, greetingFor, todoFeed, weekStrip } from './home-dashboard.js?v=25';

const WEB_SHELL_VERSION = 'desk-v25 · 2026-10-17';

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
  unread: createUnreadStore(), notifier: createNotifier(), messageNodes: new Map(), stopBackground: null, backgroundPollInFlight: false,
  view: 'chat', mobileChatOpen: false, stopTerminalPoll: null, terminalSnapshots: {}, plugins: null, pluginsError: false,
  memory: { loading: false, stats: null, segment: 'memories', month: monthKey(), days: {}, selectedDate: '', boardPage: 1, boardLoaded: false },
  dash: { appearance: { nicknames: {}, avatars: {} }, todos: null, weather: null, weatherFailed: false },
};
const contactList = $('#contact-list'); const messages = $('#message-list'); const input = $('#message-input');
const HOME_DEFAULT_CITY = '北京'; // Android HomeDashboardPolicy.HOME_DEFAULT_CITY
const DASH_DATA_INTERVAL_MS = 15 * 60_000;

function contact() { return state.contacts.find(({ id }) => id === state.activeContactId) || { id: '', name: '连接中', channel: 'PRIVATE CHANNEL', note: '', capabilities: [] }; }
/** Server nickname overrides win over the contacts payload (App parity). */
function displayNameOf(item) { return state.dash.appearance.nicknames[item?.id] || item?.name || ''; }
function avatarUriOf(id) { return state.dash.appearance.avatars[id] || ''; }
// Fallback accent colors mirror Android ChatListScreen.toChatContact(): fixed
// brand hues for known contacts; everyone else picks from the same six-color
// palette via Java String.hashCode() (Android dynamicContactColor).
const AVATAR_ACCENTS = { xiaoke: 'oxide', apples: 'rose', kairos: 'brass', kimi: 'lavender', toolbot: 'steel' };
const AVATAR_ACCENT_PALETTE = ['oxide', 'brass', 'rose', 'lavender', 'steel', 'moss'];
function javaStringHash(text) { let hash = 0; for (let i = 0; i < text.length; i += 1) hash = (Math.imul(hash, 31) + text.charCodeAt(i)) | 0; return hash; }
function avatarAccentOf(id) { return AVATAR_ACCENTS[id] || AVATAR_ACCENT_PALETTE[(javaStringHash(id) & 0x7fffffff) % AVATAR_ACCENT_PALETTE.length]; }
/** Round avatar: server image when allowed, first-character fallback otherwise. */
function avatarFace(item, accent) {
  const face = el('span', { class: `avatar ${accent}` });
  const uri = avatarUriOf(item.id);
  if (uri) {
    const img = el('img', { class: 'avatar-img', src: uri, alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => { img.remove(); face.textContent = displayNameOf(item).slice(0, 1) || '·'; }, { once: true });
    face.append(img);
  } else face.textContent = displayNameOf(item).slice(0, 1) || '·';
  return face;
}
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
    const history = state.histories[item.id] || [];
    const unread = item.id === state.activeContactId ? 0 : state.unread.unreadFor(item.id, history);
    const preview = previewText(history.at(-1)) || item.note;
    const name = displayNameOf(item);
    const button = el('button', { class: `contact-button ${item.id === state.activeContactId ? 'is-active' : ''}`, type: 'button', 'aria-current': item.id === state.activeContactId ? 'page' : 'false' });
    const accent = avatarAccentOf(item.id);
    button.append(avatarFace(item, accent), el('span', { class: 'contact-copy' }, [el('strong', { text: name }), el('small', { class: 'contact-preview', text: preview })]));
    if (unread > 0) button.append(el('span', { class: 'unread-badge', text: unread > 99 ? '99+' : String(unread), 'aria-label': `${unread} 条未读消息` }));
    button.append(el('span', { class: `contact-led ${busy ? 'is-busy' : ''}`, 'aria-label': busy ? '正在处理' : '待命' }));
    button.addEventListener('click', () => switchContact(item.id)); return button;
  }));
}
const VIEW_IDS = { chat: 'conversation', terminal: 'terminal-page', apps: 'apps-page', settings: 'settings-page', memory: 'memory-page' };
// The dashboard is the chat landing on every shell size (phone, tablet and
// desktop).  Phones/tablets (<=1199px) swap the whole surface; desktop keeps
// the contacts column beside whichever surface is active.
const homeQuery = window.matchMedia('(max-width:1199px)');
function chatSurface() { return state.mobileChatOpen ? 'conversation' : 'chat-list-page'; }
/** Single source of truth for which surface is on screen. */
function syncView() {
  const surface = state.view === 'chat' ? chatSurface() : '';
  $('#conversation').hidden = surface !== 'conversation';
  $('#chat-list-page').hidden = surface !== 'chat-list-page';
  ['terminal', 'apps', 'settings', 'memory'].forEach((name) => { $(`#${VIEW_IDS[name]}`).hidden = state.view !== name; });
  $('#side-notes').classList.toggle('view-hidden', state.view !== 'chat');
  // Desktop layout keys off these two dataset flags (dashboard hides the
  // instrument panel; non-chat views collapse the rail to its nav column).
  const shell = $('.desk-shell');
  shell.dataset.view = state.view;
  shell.dataset.surface = state.view === 'chat' ? (state.mobileChatOpen ? 'conversation' : 'dashboard') : '';
  document.querySelectorAll('.tab-item').forEach((button) => {
    const active = button.dataset.view === state.view;
    button.classList.toggle('is-active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('#chat-back-button').hidden = !(homeQuery.matches && state.view === 'chat' && state.mobileChatOpen);
}
function switchView(view) {
  if (!Object.hasOwn(VIEW_IDS, view)) return;
  if (view !== state.view) { closeStickerDialog(); setWorkers(false); }
  state.view = view;
  // The chat tab opens on its list on phones, like the Android home dashboard.
  if (view === 'chat') state.mobileChatOpen = false;
  if (view === 'terminal') startTerminalPolling(); else stopTerminalPolling();
  if (view === 'apps') void loadPlugins();
  if (view === 'memory') void refreshMemoryPage();
  syncView();
}
/** Second level of the chat tab (all shell sizes): dashboard -> conversation. */
function openMobileChat(id) {
  state.mobileChatOpen = true;
  syncView();
  void switchContact(id);
}
function renderDashboard() {
  const now = new Date();
  const clock = clockText(now);
  $('#dash-clock-time').textContent = clock.time;
  $('#dash-clock-date').textContent = clock.date;
  const greeting = greetingFor(now);
  $('#dash-quote-eyebrow').textContent = greeting.eyebrow;
  $('#dash-quote-title').textContent = greeting.title;
  $('#dash-quote-sub').textContent = greeting.subtitle;
  const week = weekStrip(now);
  $('#dash-week-title').textContent = week.title;
  $('#dash-week-days').replaceChildren(...week.days.map((day) => el('div', { class: 'dash-week-col' }, [
    el('small', { text: day.weekday }),
    el('span', { class: `dash-week-day${day.isToday ? ' is-today' : ''}`, text: String(day.day) }),
  ])));
  const weather = state.dash.weather;
  const placeChip = $('#dash-weather-place');
  placeChip.hidden = !weather?.place;
  placeChip.textContent = weather?.place || '';
  $('#dash-weather').textContent = weather ? `${weather.temperatureC}° ${weather.label}`.trim() : (state.dash.weatherFailed ? '天气暂不可用' : '天气读取中…');
  const feed = todoFeed(state.dash.todos || [], now);
  $('#dash-todo-summary').textContent = feed.title;
  const todoList = $('#dash-todo-list');
  if (!feed.total) todoList.replaceChildren(el('li', { class: 'dash-todo-empty', text: state.dash.todos === null ? '待办读取中…' : '今天没有待办' }));
  else todoList.replaceChildren(...feed.items.map((item) => el('li', { class: `dash-todo-item${item.done ? ' is-done' : ''}` }, [
    el('span', { class: 'dash-todo-check', 'aria-hidden': 'true' }),
    el('span', { class: 'dash-todo-text', text: item.text }),
    el('span', { class: 'dash-todo-due', text: item.dueLabel }),
  ])));
}
async function loadDashAppearance() {
  try {
    state.dash.appearance = await adapter.getDashAppearance();
    renderContacts(); renderChatListPage(); renderHeader();
  } catch { /* nickname/avatar overrides stay empty; contacts payload names remain */ }
}
async function loadDashData() {
  try { state.dash.weather = await adapter.getWeather({ city: HOME_DEFAULT_CITY }); state.dash.weatherFailed = !state.dash.weather; }
  catch { state.dash.weatherFailed = true; }
  try { state.dash.todos = await adapter.getTodos(); } catch { state.dash.todos = state.dash.todos ?? []; }
  renderDashboard();
}
function renderChatListPage() {
  renderDashboard();
  const list = $('#chat-list');
  list.replaceChildren(...state.contacts.map((item) => {
    const busy = isBusy(item.id);
    const history = state.histories[item.id] || [];
    const unread = state.unread.unreadFor(item.id, history);
    const accent = avatarAccentOf(item.id);
    const name = displayNameOf(item);
    const button = el('button', { type: 'button', class: 'chat-card', 'aria-label': `打开与 ${name} 的对话` });
    const face = el('span', { class: 'chat-card-face' }, [avatarFace(item, accent)]);
    if (unread > 0) face.append(el('span', { class: 'unread-badge', text: unread > 99 ? '99+' : String(unread), 'aria-label': `${unread} 条未读消息` }));
    face.append(el('span', { class: `contact-led ${busy ? 'is-busy' : ''}`, 'aria-label': busy ? '正在处理' : '待命' }));
    button.append(face, el('strong', { text: name }));
    button.addEventListener('click', () => openMobileChat(item.id));
    return button;
  }));
}
function renderHeader() {
  const current = contact(); const live = state.live[current.id] || { replyState: 'idle', statusText: '待命', workers: [] };
  const sending = Boolean(sendOperation(current.id));
  const name = displayNameOf(current) || current.name;
  $('#contact-name').textContent = name; $('#contact-mode').textContent = current.channel;
  const headerAvatar = $('#contact-avatar');
  headerAvatar.className = `avatar contact-avatar ${avatarAccentOf(current.id)}`;
  headerAvatar.replaceChildren();
  const headerAvatarUri = avatarUriOf(current.id);
  if (headerAvatarUri) {
    const img = el('img', { class: 'avatar-img', src: headerAvatarUri, alt: '', decoding: 'async' });
    img.addEventListener('error', () => { img.remove(); headerAvatar.textContent = (name || '克').slice(0, 1); }, { once: true });
    headerAvatar.append(img);
  } else headerAvatar.textContent = (name || '克').slice(0, 1);
  $('#contact-status').textContent = live.statusText || '待命';
  input.placeholder = `写给${name}…`; $('#connection-state').textContent = isBusy() ? (live.statusText || '正在处理') : '已连接';
  $('#signal-dot').classList.toggle('is-busy', isBusy());
  // The stop key tracks the busy turn itself: visible the moment a send goes
  // optimistic-busy, armed only once the server provides its stop fence.
  const stopControl = stopControlState(live);
  const stopButton = $('#stop-button');
  stopButton.hidden = !stopControl.visible;
  stopButton.disabled = !stopControl.enabled;
  stopButton.textContent = stopControl.label;
  stopButton.setAttribute('aria-disabled', String(!stopControl.enabled));
  stopButton.title = stopControl.enabled ? '停止本轮回复' : '正在启动，等待服务端确认本轮后可停止';
  input.disabled = sending; $('#attachment-input').disabled = sending; $('.attach-button').classList.toggle('is-disabled', sending); $('.attach-button').setAttribute('aria-disabled', String(sending));
  $('#sticker-button').disabled = sending || !current.id || Boolean(current.readOnly);
  $('#send-button').disabled = sending || !current.id || Boolean(current.readOnly) || (!composer().text.trim() && !composer().attachments.length);
  $('#upload-cancel-button').hidden = !(sending && sendOperation(current.id)?.uploading); $('#upload-cancel-button').disabled = !(sending && sendOperation(current.id)?.uploading);
}

function formatTokens(value) { return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1_000 ? `${(value / 1_000).toFixed(1)}k` : String(value); }
function renderInstrument() {
  const live = state.live[state.activeContactId] || {}; const instrument = live.instrument;
  const supportsInstrument = ['kairos', 'xiaoke'].includes(state.activeContactId);
  $('#instrument-contact').textContent = instrument?.provider || `${displayNameOf(contact()) || contact().name} / ${contact().channel === 'CODEX APP' ? 'Kairos' : 'CC'}`;
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
function stickerTextNodes(value) {
  return parseStickerParts(value, state.stickerCatalog).map((part) => part.type === 'text'
    ? document.createTextNode(part.value)
    : el('img', { class: 'message-sticker-inline', src: part.sticker.url, alt: part.sticker.label, loading: 'lazy', decoding: 'async' }));
}
function renderMessageBody(message) {
  const source = message.body || '…';
  // Assistant text goes through the safe Markdown subset (DOM-built, escaped
  // by construction); user drafts keep the plain pre-wrap rendering.
  if (message.role === 'assistant') {
    const body = el('div', { class: 'message-body message-md' });
    appendMarkdown(body, parseMarkdown(source), { mapText: stickerTextNodes });
    if (message.streaming) body.classList.add('is-streaming');
    return { body, standalone: false };
  }
  const parts = parseStickerParts(source, state.stickerCatalog);
  const stickers = parts.filter(({ type }) => type === 'sticker'); const standalone = stickers.length > 0 && parts.every((part) => part.type === 'sticker' || !part.value.trim());
  const body = el(standalone ? 'div' : 'p', { class: `message-body ${standalone ? 'sticker-only-message' : stickers.length ? 'sticker-mixed-message' : ''}` });
  parts.forEach((part) => {
    if (part.type === 'text') body.append(document.createTextNode(part.value));
    else body.append(el('img', { class: standalone ? 'message-sticker-large' : 'message-sticker-inline', src: part.sticker.url, alt: part.sticker.label, loading: 'lazy', decoding: 'async' }));
  });
  if (message.streaming) body.classList.add('is-streaming'); return { body, standalone };
}
/** Collapsible thinking chain: translation by default, toggle to the original. */
function renderThinking(message) {
  const hasTranslation = Boolean(message.thinking); const hasOriginal = Boolean(message.thinkingOriginal);
  const details = el('details', { class: 'thinking-bubble' });
  details.append(el('summary', { text: '思考过程' }));
  const text = el('div', { class: 'thinking-text' });
  const view = hasTranslation ? 'translation' : 'original';
  details.dataset.view = view;
  text.textContent = view === 'translation' ? message.thinking : message.thinkingOriginal;
  details.append(text);
  if (hasTranslation && hasOriginal) {
    // The toggle label names the side currently on screen, not the action.
    const toggle = el('button', { type: 'button', class: 'thinking-toggle', text: '译文', 'aria-label': '切换思考过程语言，当前显示译文' });
    toggle.addEventListener('click', () => {
      const next = details.dataset.view === 'translation' ? 'original' : 'translation';
      details.dataset.view = next;
      text.textContent = next === 'translation' ? message.thinking : message.thinkingOriginal;
      toggle.textContent = next === 'translation' ? '译文' : '原文';
      toggle.setAttribute('aria-label', `切换思考过程语言，当前显示${next === 'translation' ? '译文' : '原文'}`);
    });
    details.append(toggle);
  }
  return details;
}
function renderMessageActions(message) {
  const actions = el('div', { class: 'message-actions' });
  const copy = el('button', { type: 'button', class: 'message-action', text: '复制', 'aria-label': '复制消息内容' });
  copy.addEventListener('click', async () => {
    const ok = await copyText(message.body || '');
    copy.textContent = ok ? '已复制' : '复制失败';
    setTimeout(() => { copy.textContent = '复制'; }, 1600);
  });
  actions.append(copy);
  return actions;
}
function messageSignature(message) {
  return [message.role, message.time, message.body, message.thinking, message.thinkingOriginal, message.streaming, message.attachments?.length || 0].map((part) => String(part ?? '').length + ':' + String(part ?? '')).join('|');
}
function buildMessageNode(item, message) {
  item.className = `message ${message.role === 'user' ? 'from-user' : 'from-assistant'}`;
  const meta = el('div', { class: 'message-meta' }, [el('span', { text: message.role === 'user' ? 'ASTRA' : (displayNameOf(contact()) || contact().name).toUpperCase() }), el('time', { text: message.time || '刚刚' })]);
  const rendered = renderMessageBody(message); item.classList.toggle('has-sticker-only', rendered.standalone);
  const children = [meta];
  if (message.role === 'assistant' && (message.thinking || message.thinkingOriginal)) children.push(renderThinking(message));
  children.push(rendered.body);
  if (message.attachments?.length) children.push(el('div', { class: 'message-files', role: 'list', 'aria-label': '消息附件' }, message.attachments.map(renderAttachment)));
  children.push(renderMessageActions(message));
  item.replaceChildren(...children);
  return item;
}
/** Reuse one <li> per message id; rebuild content only when the record changed. */
function messageNode(message) {
  const key = `${state.activeContactId}:${message.id || `transient-${message.role}-${message.time}`}`;
  const signature = messageSignature(message);
  const cached = state.messageNodes.get(key);
  if (cached && cached.signature === signature) return cached.node;
  const node = buildMessageNode(cached?.node || el('li'), message);
  node.dataset.messageId = key;
  state.messageNodes.set(key, { signature, node });
  return node;
}
/** Order-preserving child sync: remove strays, then move/append in place. */
function reconcileChildren(parent, desired) {
  const keep = new Set(desired);
  [...parent.children].forEach((child) => { if (!keep.has(child)) child.remove(); });
  desired.forEach((node) => parent.append(node));
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
  const desired = history.filter((message) => !(message.role === 'assistant' && message.streaming && !String(message.body || '').trim())).map(messageNode);
  if (showTyping) {
    const existingTyping = [...messages.children].find((node) => node.classList?.contains('typing-bubble'));
    desired.push(existingTyping || createTypingBubble());
  }
  reconcileChildren(messages, desired);
  requestAnimationFrame(() => { if (shouldFollow) scrollLatest(); else updateLatestControl(); });
}
function createTypingBubble() {
  const item = el('li', { class: 'message from-assistant typing-bubble', 'data-contact-id': state.activeContactId, 'aria-hidden': 'true' });
  const meta = el('div', { class: 'message-meta' }, [el('span', { text: (displayNameOf(contact()) || contact().name).toUpperCase() }), el('time', { text: '现在' })]);
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
function renderAll(options) { renderContacts(); renderChatListPage(); renderHeader(); renderActivities(); renderMessages(options); renderWorkers(); renderInstrument(); renderTerminal(); renderAttachments(); renderDateRule(); }

async function switchContact(id) {
  if (state.stickerUpload.uploading) return;
  // Every shell size lands on the dashboard; picking any contact opens the chat.
  if (!state.mobileChatOpen) { state.mobileChatOpen = true; syncView(); }
  if (id === state.activeContactId) return;
  closeStickerDialog();
  const epoch = ++state.epoch;
  state.scrollTops[state.activeContactId] = $('#conversation-scroll').scrollTop; composer().text = input.value; state.activeContactId = id;
  // Message nodes are keyed per contact; a switch starts with a clean list.
  state.messageNodes.clear(); messages.replaceChildren();
  input.value = composer(id).text; state.followLatest[id] ??= true; autoResize(); syncWorkerPanel(); renderAll({ forceLatest: !Object.hasOwn(state.scrollTops, id) });
  if (Object.hasOwn(state.scrollTops, id)) requestAnimationFrame(() => { $('#conversation-scroll').scrollTop = state.scrollTops[id]; updateLatestControl(); });
  markActiveSeen();
  if (await refreshContact(id, { epoch }) && epoch === state.epoch && id === state.activeContactId) { markActiveSeen(); watchLive(id, epoch); }
}
/** Reading the active conversation advances its persistent read cursor. */
function markActiveSeen() {
  if (document.visibilityState === 'hidden') return;
  const cursor = historyCursor(state.histories[state.activeContactId] || []);
  if (cursor && cursor > state.unread.lastSeen(state.activeContactId)) { state.unread.markSeen(state.activeContactId, cursor); renderContacts(); }
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
    // Enter optimistic busy the instant the server accepts the send.  The
    // next /chat/status snapshot or SSE draft overwrites this projection;
    // mergeHistoryDelta only touches history and can never wipe it.
    state.live[contactId] = optimisticBusyState(state.live[contactId]);
    if (contactId === state.activeContactId && state.composerState.isCurrent(contactId, operation)) { renderMessages({ forceLatest: true }); renderHeader(); }
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
async function openMemory() { switchView('memory'); }
function renderMemorySubtitle() {
  const stats = state.memory.stats;
  const categories = state.taxonomy?.categories?.length || Object.keys(stats?.byCategory || {}).length;
  $('#memory-subtitle').textContent = stats ? `${stats.total} 条记忆 · ${categories} 个分类` : '正在统计…';
}
async function refreshMemoryPage({ force = false } = {}) {
  if (state.memory.loading) return;
  state.memory.loading = true;
  renderMemorySubtitle();
  try {
    const [stats, taxonomy] = await Promise.all([
      adapter.getMemoryStats(),
      state.taxonomy && !force ? Promise.resolve(state.taxonomy) : adapter.getTaxonomy(),
    ]);
    state.memory.stats = stats; state.taxonomy = taxonomy;
    renderMemorySubtitle(); renderTaxonomy();
  } catch {
    $('#memory-subtitle').textContent = '记忆库暂不可用；不会使用硬编码分类替代。';
    $('#taxonomy').replaceChildren(el('p', { class: 'error', text: '分类读取失败；可点右上角刷新重试。' }));
  } finally { state.memory.loading = false; }
  await loadCalendarMonth(state.memory.month, { keepSelection: true });
}
function memoryChip(scope, label) {
  const selected = state.selectedScope?.category === scope.category && (state.selectedScope?.subcategory || '') === (scope.subcategory || '');
  const button = el('button', { type: 'button', class: selected ? 'is-selected' : '', text: label });
  button.addEventListener('click', () => selectMemory(scope));
  return button;
}
function renderTaxonomy() {
  const area = $('#taxonomy'); const categories = state.taxonomy?.categories || [];
  const choices = el('div', { class: 'taxonomy-choices', role: 'list' });
  categories.forEach((category) => {
    const subs = category.subcategories || [];
    if (!subs.length) { choices.append(memoryChip({ category: category.key }, `${category.label} · ${category.count ?? 0}`)); return; }
    const total = category.count ?? subs.reduce((sum, sub) => sum + (Number(sub.count) || 0), 0);
    choices.append(memoryChip({ category: category.key }, `${category.label} · ${total}`));
    subs.forEach((sub) => choices.append(memoryChip({ category: category.key, subcategory: sub.key }, `${sub.label} · ${sub.count ?? 0}`)));
  });
  area.replaceChildren(choices);
}
function formatMemoryTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || '');
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}
/** Renders both normalized memory items and the legacy mock {title, body} shape. */
function memoryEntryView(entry) {
  const content = String(entry.body || entry.content || '');
  const card = el('article', { class: 'memory-card' });
  const meta = [entry.createdAt ? formatMemoryTime(entry.createdAt) : (entry.timestamp || ''), entry.category || '', entry.subcategory || '', entry.similarity != null ? `相似度 ${Math.round(entry.similarity * 100)}%` : ''].filter(Boolean).join(' · ');
  card.append(el('small', { text: meta || '记忆条目' }));
  if (entry.title) card.append(el('h3', { text: entry.title }));
  card.append(el('p', { text: content.length > 260 ? `${content.slice(0, 260)}…` : content }));
  if (entry.tags?.length) card.append(el('div', { class: 'memory-tags' }, entry.tags.map((tag) => el('span', { text: tag }))));
  return card;
}
async function selectMemory(scope) {
  state.selectedScope = scope; state.memory.selectedDate = ''; renderTaxonomy(); renderCalendar();
  const result = $('#memory-results'); result.replaceChildren(el('p', { class: 'loading', text: '正在读取记忆…' }));
  try {
    const entries = await adapter.listMemories(scope);
    result.replaceChildren(...(entries.length ? entries.map(memoryEntryView) : [el('p', { text: '这个分类暂时没有记忆。' })]));
  } catch { result.replaceChildren(el('p', { class: 'error', text: '这个分类暂时无法读取。' })); }
}
function renderCalendar() {
  const grid = buildMonthGrid(state.memory.month);
  const today = todayKey();
  $('#calendar-title').textContent = `时间档案 · ${grid.year}年${grid.month}月`;
  $('#calendar-grid').replaceChildren(...grid.weeks.flat().map((cell) => {
    const hasEntries = Boolean(state.memory.days[cell.date]?.entries?.length);
    const classes = ['calendar-day', cell.inMonth ? '' : 'is-outside', cell.date === today ? 'is-today' : '', hasEntries ? 'has-entries' : '', state.memory.selectedDate === cell.date ? 'is-selected' : ''].filter(Boolean).join(' ');
    const button = el('button', { type: 'button', class: classes, text: String(cell.day), 'aria-label': `${cell.date}${hasEntries ? '，有记忆' : ''}` });
    button.addEventListener('click', () => selectCalendarDate(cell.date));
    return button;
  }));
}
async function loadCalendarMonth(month, { keepSelection = false } = {}) {
  state.memory.month = month;
  if (!keepSelection) state.memory.selectedDate = '';
  renderCalendar();
  try {
    const result = await adapter.getMemoryCalendar(month);
    if (state.memory.month === month) { state.memory.days = result.days; renderCalendar(); }
  } catch { if (state.memory.month === month) { state.memory.days = {}; renderCalendar(); } }
}
function selectCalendarDate(date) {
  state.memory.selectedDate = date; state.selectedScope = null; renderTaxonomy(); renderCalendar();
  const entries = state.memory.days[date]?.entries || [];
  const result = $('#memory-results');
  result.replaceChildren(...(entries.length ? entries.map(memoryEntryView) : [el('p', { text: `${date} 没有记忆条目。` })]));
}
async function runMemorySearch() {
  const query = $('#memory-search').value.trim();
  if (!query) return;
  state.selectedScope = null; state.memory.selectedDate = ''; renderTaxonomy(); renderCalendar();
  const result = $('#memory-results'); result.replaceChildren(el('p', { class: 'loading', text: '正在搜索…' }));
  try {
    const entries = await adapter.searchMemories(query, { limit: 20 });
    result.replaceChildren(...(entries.length ? entries.map(memoryEntryView) : [el('p', { text: '没有语义相近的记忆。' })]));
  } catch { result.replaceChildren(el('p', { class: 'error', text: '搜索暂时不可用。' })); }
}
function setMemorySegment(segment) {
  state.memory.segment = segment;
  $('#memory-tab-memories').setAttribute('aria-selected', String(segment === 'memories'));
  $('#memory-tab-board').setAttribute('aria-selected', String(segment === 'board'));
  $('#memory-browse').hidden = segment !== 'memories';
  $('#memory-board').hidden = segment !== 'board';
  if (segment === 'board' && !state.memory.boardLoaded) void loadMemoryBoard(1);
}
async function loadMemoryBoard(page = 1) {
  const result = $('#board-results'); result.replaceChildren(el('p', { class: 'loading', text: '正在读取留言板…' }));
  try {
    const board = await adapter.getMemoryBoard({ page, perPage: 20 });
    state.memory.boardPage = board.pagination.page; state.memory.boardLoaded = true;
    result.replaceChildren(...(board.messages.length ? board.messages.map((message) => {
      const card = el('article', { class: 'memory-card' });
      card.append(el('small', { text: [message.from, formatMemoryTime(message.createdAt)].filter(Boolean).join(' · ') }), el('p', { text: message.content }));
      return card;
    }) : [el('p', { text: '留言板还是空的。' })]));
    $('#board-page-label').textContent = `第 ${board.pagination.page} / ${board.pagination.totalPages} 页`;
    $('#board-prev').disabled = board.pagination.page <= 1;
    $('#board-next').disabled = board.pagination.page >= board.pagination.totalPages;
  } catch { result.replaceChildren(el('p', { class: 'error', text: '留言板暂时无法读取。' })); }
}
function stopTerminalPolling() { state.stopTerminalPoll?.(); state.stopTerminalPoll = null; }
async function pollTerminalPage() {
  if (state.terminalPollInFlight) return;
  state.terminalPollInFlight = true;
  try {
    const results = await Promise.all(state.contacts.map(async (item) => {
      try { return [item.id, await adapter.getLiveState(item.id)]; } catch { return [item.id, null]; }
    }));
    if (state.view !== 'terminal') return;
    results.forEach(([id, live]) => { state.terminalSnapshots[id] = live; });
    renderTerminalPage();
  } finally { state.terminalPollInFlight = false; }
}
function startTerminalPolling() {
  stopTerminalPolling();
  void pollTerminalPage();
  const timer = setInterval(() => { void pollTerminalPage(); }, 4_000);
  state.stopTerminalPoll = () => clearInterval(timer);
}
/** Full-page read-only terminal projection for every contact (Android parity). */
function renderTerminalPage() {
  const list = $('#terminal-page-list');
  if (!state.contacts.length) { list.replaceChildren(el('p', { class: 'empty-workers', text: '联系人尚未连接。' })); return; }
  list.replaceChildren(...state.contacts.map((item) => {
    const live = state.terminalSnapshots[item.id];
    const terminal = live?.terminal || { available: false, busy: false, phase: 'unavailable', events: [] };
    const status = !live ? '读取失败，下一次轮询重试' : !terminal.available ? '观察器暂不可用' : terminal.busy ? terminal.phase : '空闲 · 没有正在运行的安全事件';
    const accent = avatarAccentOf(item.id);
    const name = displayNameOf(item);
    const card = el('article', { class: 'glass-card terminal-page-card' });
    card.append(el('div', { class: 'terminal-page-head' }, [avatarFace(item, accent), el('div', {}, [el('strong', { text: name }), el('small', { text: item.channel })])]));
    card.append(el('p', { class: 'terminal-status', text: status }));
    const events = el('ol', { class: 'terminal-events', 'aria-label': `${name} 的只读终端观察事件` });
    terminal.events.forEach((event) => {
      const row = el('li');
      row.append(el('time', { text: `${String(Math.floor(event.elapsedSeconds / 60)).padStart(2, '0')}:${String(event.elapsedSeconds % 60).padStart(2, '0')}` }), el('span', { text: event.label }));
      events.append(row);
    });
    card.append(events);
    return card;
  }));
}
async function loadPlugins({ force = false } = {}) {
  if (state.plugins && !force) { renderPlugins(); return; }
  try { state.plugins = await adapter.listPlugins(); state.pluginsError = false; }
  catch { state.plugins = state.plugins || []; state.pluginsError = true; }
  renderPlugins();
}
function renderPlugins() {
  const list = $('#plugin-list');
  if (state.pluginsError) { list.replaceChildren(el('p', { class: 'error', text: '插件列表暂时读取失败。' })); return; }
  if (!state.plugins) { list.replaceChildren(el('p', { class: 'loading', text: '正在读取插件…' })); return; }
  if (!state.plugins.length) { list.replaceChildren(el('p', { class: 'empty-workers', text: '还没有安装插件。' })); return; }
  list.replaceChildren(...state.plugins.map((plugin) => {
    const link = el('a', { class: 'glass-card plugin-card', href: `/plugins/${plugin.id}/`, target: '_blank', rel: 'noreferrer', 'aria-label': `打开插件 ${plugin.name}` });
    link.append(el('div', { class: 'plugin-copy' }, [el('strong', { text: plugin.name }), el('small', { text: plugin.description || plugin.id })]));
    if (plugin.version) link.append(el('span', { class: 'plugin-version', text: `v${plugin.version}` }));
    return link;
  }));
}
function notifyNewMessages(contactId, records) {
  const fresh = unseenAssistant(records, state.unread.lastSeen(contactId));
  if (!fresh.length) return;
  const name = displayNameOf(state.contacts.find(({ id }) => id === contactId)) || contactId;
  fresh.slice(-3).forEach((record) => state.notifier.notify({
    contactId, title: name, body: previewText(record, { maxLength: 80 }),
    onOpen: (target) => { if (state.view !== 'chat') switchView('chat'); if (homeQuery.matches) { state.mobileChatOpen = true; syncView(); } void switchContact(target); },
  }));
}
function applyLiveEvent(event) {
  if (event.type === 'message') { updateMessage(event.contactId, event.message); if (event.message?.role === 'assistant') notifyNewMessages(event.contactId, [event.message]); }
  if (event.type === 'state') state.live[event.contactId] = event.state;
  if (event.type === 'snapshot') {
    if (event.incremental) {
      const merged = mergeHistoryDelta(state.histories[event.contactId] || [], event.history, state.pendingCanonical[event.contactId] || []);
      state.histories[event.contactId] = merged.history; state.pendingCanonical[event.contactId] = merged.pendingIds;
      notifyNewMessages(event.contactId, event.history);
    } else {
      const reconciled = reconcileHistorySnapshot(state.histories[event.contactId] || [], event.history, state.pendingCanonical[event.contactId] || []);
      state.histories[event.contactId] = reconciled.history; state.pendingCanonical[event.contactId] = reconciled.pendingIds;
    }
    state.live[event.contactId] = event.state; state.streams[event.contactId] = reconcileSnapshotStream(state.streams[event.contactId], event.state);
  }
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
  if (event.type === 'connection' && event.contactId === state.activeContactId && !event.online) $('#connection-state').textContent = '正在重连'; if (event.contactId === state.activeContactId) { markActiveSeen(); renderAll(); } else renderContacts();
}
function watchLive(contactId, epoch = state.epoch) { state.stopLive?.(); state.stopLive = adapter.subscribe((event) => { if (epoch === state.epoch && contactId === state.activeContactId) applyLiveEvent(event); }, { contactId }); }
/**
 * Inactive contacts have no SSE subscription; a slow incremental poll keeps
 * their previews, unread badges and (hidden-page) notifications up to date.
 */
const BACKGROUND_POLL_MS = 20_000;
async function pollInactiveContacts() {
  if (state.backgroundPollInFlight || navigator.onLine === false) return;
  state.backgroundPollInFlight = true;
  try {
    for (const item of state.contacts) {
      if (item.id === state.activeContactId) continue;
      const known = state.histories[item.id] || [];
      const since = historyCursor(known);
      try {
        const delta = await adapter.getHistory(item.id, since ? { since, limit: 50 } : { limit: 1 });
        if (!delta.length) continue;
        // Notify only for records newer than everything already known; a
        // misbehaving adapter returning a full window cannot re-notify.
        const fresh = since ? delta.filter((record) => String(record.ts || '') > since) : [];
        const merged = mergeHistoryDelta(known, delta, state.pendingCanonical[item.id] || []);
        state.histories[item.id] = merged.history; state.pendingCanonical[item.id] = merged.pendingIds;
        // The first fetch only seeds the preview: backlog is not "new".
        if (known.length && fresh.length) notifyNewMessages(item.id, fresh);
      } catch { /* one contact failing must not starve the others */ }
    }
  } finally { state.backgroundPollInFlight = false; }
  renderContacts();
}
function startBackgroundPolling() {
  const timer = setInterval(() => { void pollInactiveContacts(); }, BACKGROUND_POLL_MS);
  return () => clearInterval(timer);
}
function syncNotificationButton() {
  const supported = state.notifier.supported();
  const on = state.notifier.isEnabled();
  const button = $('#notification-button');
  if (button) {
    button.hidden = !supported;
    button.querySelector('use')?.setAttribute('href', on ? '#icon-bell' : '#icon-bell-off');
    button.title = on ? '桌面通知：开（点击关闭）' : '桌面通知：关（点击开启）';
    button.setAttribute('aria-label', on ? '关闭桌面通知，当前已开启' : '开启桌面通知，当前已关闭');
    button.setAttribute('aria-pressed', String(on));
  }
  const label = $('#settings-notification-label'); if (label) label.textContent = !supported ? '此浏览器不支持' : (on ? '已开启' : '已关闭');
  const toggle = $('#settings-notification-button'); if (toggle) { toggle.textContent = on ? '关闭' : '开启'; toggle.disabled = !supported; }
}
async function toggleNotifications() {
  const wantsOn = !state.notifier.isEnabled();
  const live = await state.notifier.setEnabled(wantsOn);
  syncNotificationButton();
  if (wantsOn && !live) $('#connection-state').textContent = '通知权限未授予，请在浏览器设置中允许';
}
const workerDrawerQuery = window.matchMedia('(max-width:1199px)');
function syncWorkerPanel() {
  const panel = $('#side-notes'); const drawer = workerDrawerQuery.matches;
  if (!drawer) { document.body.classList.remove('workers-open'); panel.setAttribute('aria-hidden', 'false'); panel.inert = false; $('#worker-toggle').setAttribute('aria-expanded', 'false'); return; }
  const open = document.body.classList.contains('workers-open'); panel.setAttribute('aria-hidden', String(!open)); panel.inert = !open; $('#worker-toggle').setAttribute('aria-expanded', String(open));
}
function setWorkers(open, { returnFocus = false } = {}) { if (!workerDrawerQuery.matches) return syncWorkerPanel(); document.body.classList.toggle('workers-open', open); syncWorkerPanel(); if (open) $('#worker-close').focus(); else if (returnFocus) $('#worker-toggle').focus(); }
function closeWorkers() { setWorkers(false, { returnFocus: true }); }
function cycleAppearance() { const modes = ['default', 'reading', 'compact']; state.appearance = modes[(modes.indexOf(state.appearance) + 1) % modes.length]; document.body.dataset.appearance = state.appearance; const names = { default: '默认', reading: '阅读', compact: '紧凑' }; $('#appearance-button').setAttribute('aria-label', `切换显示模式：${names[state.appearance]}`); $('#appearance-button').title = `显示模式：${names[state.appearance]}`; $('#drawer-appearance-button').textContent = `显示模式：${names[state.appearance]}`; $('#appearance-status').textContent = `显示模式：${names[state.appearance]}`; $('#settings-appearance-label').textContent = names[state.appearance]; }

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
$('#memory-button').addEventListener('click', openMemory); $('#worker-toggle').addEventListener('click', () => setWorkers(!document.body.classList.contains('workers-open'))); $('#worker-close').addEventListener('click', closeWorkers);
document.querySelectorAll('.tab-item').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
$('#chat-back-button').addEventListener('click', () => { state.mobileChatOpen = false; syncView(); });
$('#memory-back').addEventListener('click', () => switchView('chat'));
$('#memory-refresh').addEventListener('click', () => { state.memory.stats = null; void refreshMemoryPage({ force: true }); });
$('#memory-tab-memories').addEventListener('click', () => setMemorySegment('memories'));
$('#memory-tab-board').addEventListener('click', () => setMemorySegment('board'));
$('#memory-search').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); void runMemorySearch(); } });
$('#calendar-prev').addEventListener('click', () => void loadCalendarMonth(shiftMonth(state.memory.month, -1)));
$('#calendar-next').addEventListener('click', () => void loadCalendarMonth(shiftMonth(state.memory.month, 1)));
$('#calendar-today').addEventListener('click', () => void loadCalendarMonth(monthKey()));
$('#board-prev').addEventListener('click', () => void loadMemoryBoard(state.memory.boardPage - 1));
$('#board-next').addEventListener('click', () => void loadMemoryBoard(state.memory.boardPage + 1));
$('#open-memory-card').addEventListener('click', openMemory);
$('#dash-full-calendar').addEventListener('click', openMemory);
const goHomeDashboard = () => { state.mobileChatOpen = false; switchView('chat'); };
$('#open-weather-card').addEventListener('click', goHomeDashboard);
$('#open-todos-card').addEventListener('click', goHomeDashboard);
$('#settings-appearance-button').addEventListener('click', cycleAppearance);
$('#settings-notification-button').addEventListener('click', toggleNotifications);
$('#settings-logout-button').addEventListener('click', logout);
$('#settings-version').textContent = WEB_SHELL_VERSION;
homeQuery.addEventListener('change', syncView);
async function logout() { state.epoch += 1; state.stickerLoadId += 1; state.composerState.cancelAll(); state.stickerUpload.controller?.abort(); state.stickerUpload.uploading = false; closeStickerDialog(); state.stickerCatalog = null; state.stickerLoading = false; state.stickerError = ''; state.stickerSearch = ''; state.stickerCategory = '__all'; state.stickerTargetContactId = ''; try { await adapter.logout(); } finally { state.stopLive?.(); state.stopLive = null; state.stopBackground?.(); state.stopBackground = null; stopTerminalPolling(); state.terminalSnapshots = {}; state.plugins = null; state.pluginsError = false; state.contacts = []; state.histories = {}; state.live = {}; state.streams = {}; state.pendingCanonical = {}; state.messageNodes.clear(); messages.replaceChildren(); state.composerState = createComposerState(); state.activeContactId = ''; state.view = 'chat'; state.mobileChatOpen = false; state.taxonomy = null; state.selectedScope = null; state.memory = { loading: false, stats: null, segment: 'memories', month: monthKey(), days: {}, selectedDate: '', boardPage: 1, boardLoaded: false }; state.dash = { appearance: { nicknames: {}, avatars: {} }, todos: null, weather: null, weatherFailed: false }; renderAll(); setMemorySegment('memories'); renderMemorySubtitle(); renderPlugins(); syncView(); if (!bootstrap.mock) showLogin(); } }
$('#logout-button').addEventListener('click', logout); $('#drawer-logout-button').addEventListener('click', logout);
$('#appearance-button').addEventListener('click', cycleAppearance); $('#drawer-appearance-button').addEventListener('click', cycleAppearance);
$('#notification-button').addEventListener('click', toggleNotifications);
document.addEventListener('visibilitychange', () => { if (document.visibilityState !== 'hidden') markActiveSeen(); });
window.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); input.focus(); } if (event.altKey && /^[1-9]$/.test(event.key)) { const target = state.contacts[Number(event.key) - 1]; if (target) switchContact(target.id); } if (event.key === 'Escape') { if ($('#sticker-dialog').open) closeStickerDialog({ returnFocus: true }); else if (state.view === 'memory') switchView('chat'); if (document.body.classList.contains('workers-open')) closeWorkers(); } });
window.addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); state.deferredInstall = event; $('#install-button').hidden = false; }); $('#install-button').addEventListener('click', async () => { await state.deferredInstall?.prompt(); state.deferredInstall = null; $('#install-button').hidden = true; });
workerDrawerQuery.addEventListener('change', syncWorkerPanel); syncWorkerPanel(); document.body.append($('#svg-defs').content.cloneNode(true)); if ('serviceWorker' in navigator) { let swReloaded = false; navigator.serviceWorker.addEventListener('controllerchange', () => { if (swReloaded) return; swReloaded = true; location.reload(); }); window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js')); }
async function start() {
  try { await bootstrap.checkSession(); } catch (error) { if (!bootstrap.mock) { showLogin(); return; } }
  try { state.contacts = await adapter.contacts(); if (!state.contacts.length) throw new Error('没有可用联系人'); state.activeContactId = state.contacts.find(({ id }) => id === 'xiaoke')?.id || state.contacts[0].id; state.contacts.forEach(({ id }) => { state.histories[id] = []; state.live[id] = { replyState: 'idle', statusText: '待命', workers: [] }; composer(id); state.followLatest[id] = true; }); await Promise.all(state.contacts.map(({ id }) => refreshContact(id))); seedUnreadCursors(state.unread, state.histories); markActiveSeen(); watchLive(state.activeContactId); state.stopBackground?.(); state.stopBackground = startBackgroundPolling(); syncNotificationButton(); renderAll({ forceLatest: true }); syncView(); void loadStickerCatalog(); void loadDashAppearance(); void loadDashData(); }
  catch (error) { $('#connection-state').textContent = '连接不可用'; console.warn('PWA bootstrap failed', error); }
  // Screenshot/dev hook, mock sessions only: ?view=terminal|apps|settings|memory
  // lands directly on a page, and &chat=open pretends a conversation is open.
  if (bootstrap.mock) {
    const params = new URLSearchParams(globalThis.location?.search || '');
    const target = params.get('view');
    if (target && Object.hasOwn(VIEW_IDS, target)) switchView(target);
    if (params.get('chat') === 'open') { state.mobileChatOpen = true; syncView(); }
  }
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
renderAll(); syncView(); start(); setInterval(renderDashboard, 30_000); setInterval(() => void loadDashData(), DASH_DATA_INTERVAL_MS);
