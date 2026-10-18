import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createHttpAdapter, historyCursor, mergeHistoryDelta, normalizeRecord } from '../src/api.js';
import { createUnreadStore, latestChatRecord, previewText, seedUnreadCursors, unreadCount, unseenAssistant } from '../src/unread.js';
import { createNotifier } from '../src/notifications.js';

function memoryStorage() {
  const map = new Map();
  return { getItem: (key) => map.get(key) ?? null, setItem: (key, value) => map.set(key, String(value)), removeItem: (key) => map.delete(key), _map: map };
}

test('normalizeRecord preserves thinking translation, original metadata, and the raw server ts', () => {
  const record = normalizeRecord({
    id: 'a1', role: 'assistant', text: '正文', ts: '2026-10-17T02:00:00+00:00',
    thinking: '中文译文思考',
    metadata: { thinking_original: 'English original chain', thinking_translation_suspect: true },
  });
  assert.equal(record.thinking, '中文译文思考');
  assert.equal(record.thinkingOriginal, 'English original chain');
  assert.equal(record.ts, '2026-10-17T02:00:00+00:00');
  const plain = normalizeRecord({ id: 'u1', role: 'user', text: '没有思考', ts: '2026-10-17T02:01:00+00:00' });
  assert.equal(plain.thinking, '');
  assert.equal(plain.thinkingOriginal, '');
});

test('getHistory adds since/limit only for incremental calls', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path) => { calls.push(path); return { records: [] }; } });
  await adapter.getHistory('xiaoke');
  await adapter.getHistory('xiaoke', { since: '2026-10-17T02:00:00+00:00', limit: 50 });
  await adapter.getHistory('xiaoke', { since: '2026-10-17T02:00:00+00:00', limit: 999_999 });
  assert.equal(calls[0], '/chat/history?contact_id=xiaoke');
  assert.equal(calls[1], '/chat/history?contact_id=xiaoke&since=2026-10-17T02%3A00%3A00%2B00%3A00&limit=50');
  assert.equal(calls[2], '/chat/history?contact_id=xiaoke&since=2026-10-17T02%3A00%3A00%2B00%3A00&limit=10000', 'limit is capped');
});

test('mergeHistoryDelta upserts by id, keeps absent records, and clears confirmed pending sends', () => {
  const current = [
    { id: 'old', role: 'assistant', body: '旧', ts: '2026-10-17T01:00:00+00:00' },
    { id: 'send-1', role: 'user', body: '本地待发', ts: '2026-10-17T01:01:00+00:00' },
  ];
  const delta = [
    { id: 'send-1', role: 'user', body: '服务器 canonical', ts: '2026-10-17T01:01:00+00:00' },
    { id: 'new-a', role: 'assistant', body: '新回复', ts: '2026-10-17T01:02:00+00:00' },
  ];
  const merged = mergeHistoryDelta(current, delta, ['send-1']);
  assert.deepEqual(merged.history.map(({ id }) => id), ['old', 'send-1', 'new-a']);
  assert.equal(merged.history[1].body, '服务器 canonical');
  assert.deepEqual(merged.pendingIds, []);
  const untouched = mergeHistoryDelta(current, [], ['send-1']);
  assert.deepEqual(untouched.pendingIds, ['send-1'], 'delta absence never drops a pending send');
  assert.equal(untouched.history.length, 2);
  assert.equal(historyCursor(merged.history), '2026-10-17T01:02:00+00:00');
});

test('subscribe polls full history once then switches to since-cursor deltas', async () => {
  const calls = []; const events = []; const timers = [];
  const clock = { setTimeout(fn, delay) { const timer = { fn, delay }; timers.push(timer); return timer; }, clearTimeout() {} };
  const adapter = createHttpAdapter({
    request: async (path) => {
      calls.push(path);
      if (path.startsWith('/chat/history')) {
        if (path.includes('since=')) return { records: [{ ts: '2026-10-17T03:00:00+00:00', role: 'assistant', text: '增量' }] };
        return { records: [{ ts: '2026-10-17T02:00:00+00:00', role: 'assistant', text: '全量' }] };
      }
      return { busy: false };
    },
    clock,
    visibility: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    network: { navigator: { onLine: true }, addEventListener() {}, removeEventListener() {} },
  });
  const stop = adapter.subscribe((event) => events.push(event), { contactId: 'xiaoke' });
  await timers.shift().fn(); // first poll: full snapshot
  assert.equal(calls.filter((path) => path.startsWith('/chat/history')).length, 1);
  assert.equal(events.at(-1).incremental, false);
  await timers.shift().fn(); // second poll: delta with cursor
  const historyCalls = calls.filter((path) => path.startsWith('/chat/history'));
  assert.equal(historyCalls.length, 2);
  assert.ok(historyCalls[1].includes('since=2026-10-17T02%3A00%3A00%2B00%3A00'), historyCalls[1]);
  assert.ok(historyCalls[1].includes('limit=500'));
  assert.equal(events.at(-1).incremental, true);
  assert.equal(events.at(-1).history[0].body, '增量');
  stop();
});

test('unread store counts only unseen assistant messages and persists cursors advance-only', () => {
  const storage = memoryStorage();
  const store = createUnreadStore({ storage });
  const history = [
    { id: '1', role: 'user', ts: '2026-10-17T01:00:00+00:00' },
    { id: '2', role: 'assistant', ts: '2026-10-17T01:01:00+00:00' },
    { id: '3', role: 'assistant', ts: '2026-10-17T01:02:00+00:00' },
  ];
  assert.equal(store.unreadFor('xiaoke', history), 0, 'a fresh device treats history as read');
  store.markSeen('xiaoke', '2026-10-17T01:01:00+00:00');
  assert.equal(store.unreadFor('xiaoke', history), 1);
  store.markSeen('xiaoke', '2026-10-17T00:59:00+00:00');
  assert.equal(store.lastSeen('xiaoke'), '2026-10-17T01:01:00+00:00', 'cursor never moves backwards');
  store.markSeen('xiaoke', '2026-10-17T01:02:00+00:00');
  assert.equal(store.unreadFor('xiaoke', history), 0);
  const restored = createUnreadStore({ storage });
  assert.equal(restored.lastSeen('xiaoke'), '2026-10-17T01:02:00+00:00', 'cursor survives reload via storage');
  assert.equal(unreadCount(history, '2026-10-17T01:00:00+00:00'), 2);
  assert.deepEqual(unseenAssistant(history, '2026-10-17T01:01:00+00:00').map(({ id }) => id), ['3']);
});

test('unread store tolerates corrupted storage payloads', () => {
  const storage = memoryStorage();
  storage.setItem('cc-pwa-last-seen-v1', '{broken json');
  const store = createUnreadStore({ storage });
  assert.equal(store.lastSeen('xiaoke'), '');
  storage.setItem('cc-pwa-last-seen-v1', JSON.stringify(['not-an-object']));
  assert.equal(createUnreadStore({ storage }).lastSeen('xiaoke'), '');
});

test('initial unread seeding marks existing histories read without touching established cursors', () => {
  const storage = memoryStorage();
  const store = createUnreadStore({ storage });
  store.markSeen('kairos', '2026-10-17T01:30:00+00:00');
  seedUnreadCursors(store, {
    xiaoke: [
      { id: 'u1', role: 'user', ts: '2026-10-17T01:00:00+00:00' },
      { id: 'a1', role: 'assistant', ts: '2026-10-17T01:01:00+00:00' },
    ],
    kairos: [
      { id: 'k1', role: 'assistant', ts: '2026-10-17T01:02:00+00:00' },
      { id: 'k2', role: 'assistant', ts: '2026-10-17T01:40:00+00:00' },
    ],
    empty: [],
  });
  assert.equal(store.lastSeen('xiaoke'), '2026-10-17T01:01:00+00:00');
  assert.equal(store.lastSeen('kairos'), '2026-10-17T01:30:00+00:00', 'existing cursor stays advance-only');
  assert.equal(store.lastSeen('empty'), '');
  assert.equal(store.unreadFor('xiaoke', [{ id: 'a2', role: 'assistant', ts: '2026-10-17T01:03:00+00:00' }]), 1);
});

test('preview text clips long bodies, folds code blocks, and prefixes own messages', () => {
  assert.equal(previewText({ role: 'assistant', body: '短消息' }), '短消息');
  assert.equal(previewText({ role: 'user', body: '我发的' }), '我: 我发的');
  assert.equal(previewText({ role: 'assistant', body: '看代码\n```js\nconst a = 1;\n```\n完了' }), '看代码 [代码] 完了');
  assert.ok(previewText({ role: 'assistant', body: '长'.repeat(100) }).length <= 44);
  assert.equal(previewText({ role: 'user', body: '', attachments: [{ name: 'photo.png' }] }), '我: [附件] photo.png');
  assert.equal(previewText(null), '');
});

test('task/tool-event rows stay out of the contact preview and unread count', () => {
  const history = [
    { id: 'm1', role: 'assistant', body: '真实回复', ts: '2026-10-18T01:00:00+00:00' },
    { id: 't1', role: 'task', body: '⚡执行命令: cd /tmp && ls', ts: '2026-10-18T01:01:00+00:00' },
    { id: 't2', role: 'task', body: '⚡执行命令: rm -rf /tmp/old', ts: '2026-10-18T01:02:00+00:00' },
  ];
  assert.equal(latestChatRecord(history)?.id, 'm1', 'preview walks back past tool events to the latest real chat record');
  assert.equal(previewText(latestChatRecord(history)), '真实回复');
  assert.equal(latestChatRecord([{ id: 't3', role: 'task', body: '⚡执行命令: pwd' }]), null, 'a history of only tool events has no previewable record');
  const store = createUnreadStore({ storage: memoryStorage() });
  store.markSeen('kimi', '2026-10-18T00:59:00+00:00');
  assert.equal(store.unreadFor('kimi', history), 1, 'tool events never count as unread chat');
});

test('normalizeRecord keeps role "task" instead of coercing tool events to assistant bubbles', () => {
  const task = normalizeRecord({ id: 't1', role: 'task', text: '⚡执行命令: cd /tmp', ts: '2026-10-18T01:01:00+00:00' });
  assert.equal(task.role, 'task');
  assert.equal(normalizeRecord({ id: 'a1', role: 'assistant', text: '好' }).role, 'assistant');
  assert.equal(normalizeRecord({ id: 'u1', role: 'user', text: '嗯' }).role, 'user');
});

test('notifier fires only when enabled, granted, and the page is hidden', async () => {
  const storage = memoryStorage(); const fired = [];
  const FakeNotification = class {
    static permission = 'granted';
    constructor(title, options) { fired.push({ title, options }); }
    close() {}
  };
  const hidden = { visibilityState: 'hidden' };
  const visible = { visibilityState: 'visible' };
  const notifier = createNotifier({ notificationApi: FakeNotification, storage, visibility: hidden, windowLike: { focus() { this.focused = true; } } });
  assert.equal(notifier.isEnabled(), false, 'disabled by default');
  notifier.notify({ contactId: 'xiaoke', title: '小克', body: '你好' });
  assert.equal(fired.length, 0, 'never fires while disabled');
  assert.equal(await notifier.setEnabled(true), true);
  assert.equal(storage.getItem('cc-pwa-notifications-enabled-v1'), '1');
  notifier.notify({ contactId: 'xiaoke', title: '小克', body: '来了' });
  assert.equal(fired.length, 1);
  assert.equal(fired[0].options.tag, 'cc-xiaoke');
  const visibleNotifier = createNotifier({ notificationApi: FakeNotification, storage, visibility: visible });
  visibleNotifier.notify({ contactId: 'xiaoke', title: '小克', body: '来了' });
  assert.equal(fired.length, 1, 'visible page relies on the in-app UI instead');
  assert.equal(await notifier.setEnabled(false), false);
  notifier.notify({ contactId: 'xiaoke', title: '小克', body: '又来' });
  assert.equal(fired.length, 1, 'toggle off silences immediately');
});

test('notifier requests permission on enable and refuses when denied', async () => {
  let asked = 0;
  const DeniedNotification = class { static permission = 'default'; static requestPermission() { asked += 1; return Promise.resolve('denied'); } };
  const notifier = createNotifier({ notificationApi: DeniedNotification, storage: memoryStorage(), visibility: { visibilityState: 'hidden' } });
  assert.equal(await notifier.setEnabled(true), false);
  assert.equal(asked, 1);
  assert.equal(notifier.isEnabled(), false);
  const unsupported = createNotifier({ notificationApi: undefined, storage: memoryStorage() });
  assert.equal(unsupported.supported(), false);
  assert.equal(await unsupported.setEnabled(true), false);
});

test('notification click focuses the window and routes to the contact', () => {
  let clicked; let focused = false; const opened = [];
  const FakeNotification = class {
    static permission = 'granted';
    constructor() { clicked = this; }
    close() { this.closed = true; }
  };
  const notifier = createNotifier({ notificationApi: FakeNotification, storage: memoryStorage(), visibility: { visibilityState: 'hidden' }, windowLike: { focus: () => { focused = true; } } });
  return notifier.setEnabled(true).then(() => {
    notifier.notify({ contactId: 'kairos', title: 'Kairos', body: '完成', onOpen: (id) => opened.push(id) });
    clicked.onclick();
    assert.equal(focused, true);
    assert.deepEqual(opened, ['kairos']);
    assert.equal(clicked.closed, true);
  });
});

test('first-batch UX source contracts: no innerHTML, markdown renderer, thinking toggle, copy actions', () => {
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const markdown = readFileSync(new URL('../src/markdown.js', import.meta.url), 'utf8');
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const sw = readFileSync(new URL('../sw.js', import.meta.url), 'utf8');
  assert.doesNotMatch(app + markdown, /\.innerHTML\s*=[^=]/, 'message rendering never parses HTML');
  assert.doesNotMatch(app + markdown, /insertAdjacentHTML|outerHTML\s*=/);
  assert.match(app, /appendMarkdown\(body, parseMarkdown\(source\)/);
  assert.match(app, /if \(message\.role === 'assistant'\)/);
  assert.match(app, /message\.thinkingOriginal/);
  assert.match(app, /class: 'thinking-toggle'/);
  assert.match(app, /切换思考过程语言，当前显示译文/, 'toggle labels the side currently on screen');
  assert.match(app, /toggle\.textContent = next === 'translation' \? '译文' : '原文'/);
  assert.match(app, /renderMessageActions/);
  assert.match(app, /reconcileChildren\(messages, desired\)/, 'keyed diff replaces wholesale replaceChildren');
  assert.doesNotMatch(app, /messages\.replaceChildren\(\.\.\.history/, 'no full-list replaceChildren on the message list');
  assert.match(app, /mergeHistoryDelta\(state\.histories\[event\.contactId\]/);
  assert.match(app, /state\.unread\.markSeen/);
  assert.match(app, /state\.notifier\.notify/);
  assert.match(app, /startBackgroundPolling\(\)/);
  assert.match(html, /id="notification-button"[^>]*aria-pressed="false"/);
  assert.match(sw, /src\/markdown\.js\?v=31/);
  assert.match(sw, /src\/unread\.js\?v=31/);
  assert.match(sw, /src\/notifications\.js\?v=31/);
});
