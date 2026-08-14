import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { createHttpAdapter, createMockAdapter, normalizeLiveState, normalizeRecord } from '../src/api.js';
import { createPwaBootstrap, isExplicitMockMode } from '../src/bootstrap.js';
import { createComposerState } from '../src/composer-state.js';
import { PAIRING_ALPHABET, formatPairingCode, normalizePairingCode } from '../src/pairing-code.js';
import { composeLiveMessages, shouldShowTypingBubble, reconcileSnapshotStream, reduceStreamDraft } from '../src/live-messages.js';

function hexToken(css, token) {
  const match = css.match(new RegExp(`--${token}:(#[0-9a-fA-F]{6})`));
  assert.ok(match, `missing --${token}`);
  return match[1];
}

function contrastRatio(first, second) {
  const luminance = (hex) => {
    const [red, green, blue] = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

test('normalizes bounded worker activity without relying on server naming', () => {
  assert.deepEqual(normalizeLiveState({
    busy: true, reply_state: 'generating', status_text: '生成中', draft: { text: 'partial', activity_count: 2,
      worker_activity_items: [{ worker_id: 'layout', name: 'windows_pwa_shell', status: 'running', count: 3 }] },
    stop_request: { supported: true, body: { contact_id: 'kairos', user_ts: 'turn-1' } },
  }), {
    busy: true, replyState: 'generating', turnId: '', revision: '', updatedAt: '', statusText: '生成中', draft: 'partial', activityText: '', activityCount: 2,
    stopRequest: { supported: true, body: { contact_id: 'kairos', user_ts: 'turn-1' } },
    workers: [{ id: 'layout', name: 'windows_pwa_shell', state: 'running', count: 3 }],
  });
});

test('typing status uses a stable live region and filters empty streaming placeholders', () => {
  const index = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  assert.match(index, /id="typing-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(app, /typingStatus\.dataset\.active/);
  assert.match(app, /message\.streaming && !String\(message\.body \|\| ''\)\.trim\(\)/);
  assert.match(app, /typing-bubble.*aria-hidden/);
});

test('HTTP adapter reads server records and derives contacts from the manifest', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === '/chat/contacts') return { contacts: [
      { id: 'xiaoke', display_name: '小克', provider: 'claude-code', capabilities: ['chat', 'stop'] },
      { id: 'kairos', display_name: 'Kairos', provider: 'codex-app-server', capabilities: ['chat', 'stop'] },
    ] };
    if (path.startsWith('/chat/history')) return { records: [{ ts: '2026-08-09T05:00:00+00:00', role: 'assistant', text: 'server record' }] };
    return { ok: true };
  } });
  const contacts = await adapter.contacts(); const history = await adapter.getHistory('kairos');
  assert.deepEqual(contacts.map(({ id, name }) => ({ id, name })), [{ id: 'xiaoke', name: '小克' }, { id: 'kairos', name: 'Kairos' }]);
  assert.equal(history[0].body, 'server record');
  assert.equal(calls[0].path, '/chat/contacts');
  assert.equal(calls[1].path, '/chat/history?contact_id=kairos');
});

test('normalizes every safe metadata attachment in order and dedupes the primary record field', () => {
  const record = normalizeRecord({
    ts: '2026-08-09T05:00:00Z', role: 'user', text: 'multiple files',
    attachment_url: '/attachments/cover.png', attachment_filename: 'cover.png', attachment_type: 'image',
    metadata: { attachments: [
      { attachment_url: '/attachments/cover.png', filename: 'cover.png', type: 'image', size: 10 },
      { attachment_url: '/attachments/notes.pdf', filename: 'notes.pdf', type: 'file', size: 20 },
      { attachment_url: '/attachments/data.csv', filename: 'data.csv', type: 'file', size: 30 },
    ] },
  });
  assert.deepEqual(record.attachments, [
    { name: 'cover.png', url: '/attachments/cover.png', type: 'image', size: 0 },
    { name: 'notes.pdf', url: '/attachments/notes.pdf', type: 'file', size: 20 },
    { name: 'data.csv', url: '/attachments/data.csv', type: 'file', size: 30 },
  ]);
});

test('normalizes only flat same-origin attachment paths and rejects encoded traversal', () => {
  const record = normalizeRecord({ metadata: { attachments: [
    { attachment_url: 'https://cccompanion.invalid/attachments/absolute-safe.png', filename: 'absolute-safe.png' },
    { attachment_url: 'file:///root/private.txt', filename: 'private.txt' },
    { attachment_url: '/home/ubuntu/private.txt', filename: 'private.txt' },
    { attachment_url: 'javascript:alert(1)', filename: 'bad.js' },
    { attachment_url: 'https://example.test/attachments/outside.png', filename: 'outside.png' },
    { attachment_url: 'https://cccompanion.invalid:8443/attachments/wrong-port.png', filename: 'wrong-port.png' },
    { attachment_url: 'https://user:password@cccompanion.invalid/attachments/credentials.png', filename: 'credentials.png' },
    { attachment_url: '/attachments/ok.png', filename: 'ok.png', type: 'image' },
    { attachment_url: '/attachments/sub/file.png', filename: 'sub.png' },
    { attachment_url: '/attachments/a%2Fb.png', filename: 'separator.png' },
    { attachment_url: '/attachments/a%5Cb.png', filename: 'backslash.png' },
    { attachment_url: '/attachments/%2e%2e%2fsecret.txt', filename: 'traversal.txt' },
    { attachment_url: '/attachments/a%252fb.png', filename: 'double-encoded.png' },
    { attachment_url: '/attachments/bad%ZZ.png', filename: 'malformed.png' },
    { attachment_url: '/attachments/../secret.txt', filename: 'secret.txt' },
    { attachment_url: '/attachments/fragment.png#x', filename: 'fragment.png' },
    { attachment_url: '/attachments/query.png?download=1', filename: 'query.png' },
  ] } });
  assert.deepEqual(record.attachments, [
    { name: 'absolute-safe.png', url: '/attachments/absolute-safe.png', type: 'file', size: 0 },
    { name: 'ok.png', url: '/attachments/ok.png', type: 'image', size: 0 },
  ]);
});

test('stop routing preserves the opaque per-contact server fence', async () => {
  const calls = []; const adapter = createHttpAdapter({ request: async (path, options = {}) => { calls.push({ path, options }); return { ok: true }; } });
  const fence = { supported: true, body: { contact_id: 'xiaoke', user_ts: 'user-turn', session: 'cc-main' } };
  await adapter.stop('xiaoke', fence);
  assert.deepEqual(calls[0], { path: '/chat/stop', options: { method: 'POST', body: fence.body } });
  await assert.rejects(() => adapter.stop('kairos', fence), /valid stop request/);
});

test('production live subscription performs network snapshots and accepts same-contact SSE events', async () => {
  const calls = []; const events = []; const stream = {};
  const adapter = createHttpAdapter({
    request: async (path) => {
      calls.push(path);
      if (path.startsWith('/chat/history')) return { records: [{ ts: '2026-08-09T05:00:00Z', role: 'assistant', text: 'poll result' }] };
      if (path.startsWith('/chat/status')) return { busy: true, reply_state: 'generating', draft: { text: 'draft' } };
      return { ok: true };
    },
    eventSourceFactory: (path) => { calls.push(path); return stream; },
    visibility: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    network: { navigator: { onLine: true }, addEventListener() {}, removeEventListener() {} },
  });
  const stop = adapter.subscribe((event) => events.push(event), { contactId: 'kairos' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  stream.onopen(); stream.onmessage({ data: JSON.stringify({ event: 'chunk', contact_id: 'kairos', stream_id: 's1', text: 'live' }) });
  stop();
  assert.ok(calls.includes('/chat/history?contact_id=kairos'));
  assert.ok(calls.includes('/chat/status?contact_id=kairos'));
  assert.ok(calls.includes('/chat/stream?contact_id=kairos'));
  assert.equal(events.find(({ type }) => type === 'snapshot').history[0].body, 'poll result');
  assert.equal(events.find(({ type }) => type === 'stream').payload.text, 'live');
});

test('live subscription keeps bounded hidden polling and reconnects immediately when visible again', async () => {
  const timers = []; const cleared = new Set(); const listeners = {}; const calls = []; const stream = { close() { this.closed = true; } }; let streamOpens = 0;
  const clock = { setTimeout(fn, delay) { const timer = { fn, delay }; timers.push(timer); return timer; }, clearTimeout(timer) { cleared.add(timer); } };
  const visibility = { visibilityState: 'hidden', addEventListener(name, fn) { listeners[name] = fn; }, removeEventListener() {} };
  const adapter = createHttpAdapter({
    request: async (path) => { calls.push(path); return path.startsWith('/chat/history') ? { records: [] } : { busy: false }; },
    clock, visibility, network: { navigator: { onLine: true }, addEventListener() {}, removeEventListener() {} }, eventSourceFactory: () => { streamOpens += 1; return stream; },
  });
  const stop = adapter.subscribe(() => {}, { contactId: 'kairos' });
  assert.equal(timers[0].delay, 0, 'initial snapshot is scheduled once');
  await timers.shift().fn();
  assert.equal(timers.at(-1).delay, 15_000, 'hidden pages still reconcile the authoritative snapshot');
  assert.equal(stream.closed, undefined, 'hidden state does not discard the live SSE connection');
  visibility.visibilityState = 'visible'; listeners.visibilitychange();
  assert.equal(timers.at(-1).delay, 0, 'visible recovery polls immediately');
  assert.equal(streamOpens, 1, 'visibility recovery does not open a duplicate SSE connection');
  assert.equal(calls.filter((path) => path.startsWith('/chat/history')).length, 1);
  stop();
  assert.equal(stream.closed, true, 'cleanup closes SSE');
});

test('Kairos draft is one transient row, SSE cannot duplicate it, and final history replaces it', () => {
  const history = [{ id: 'old', role: 'assistant', body: 'already there', time: '10:00' }];
  let stream = reduceStreamDraft(null, { event: 'draft', turn_id: 'turn-1', reply_state: 'generating', text: 'partial reply', updated_at: '2026-08-09T11:26:01Z' });
  stream = reduceStreamDraft(stream, { event: 'draft', turn_id: 'turn-1', reply_state: 'generating', text: 'partial reply revised', updated_at: '2026-08-09T11:26:02Z' });
  const withDraft = composeLiveMessages(history, { busy: true, draft: 'stale poll value' }, { contactId: 'kairos', stream });
  assert.equal(withDraft.length, 2);
  assert.equal(withDraft.at(-1).id, 'live-draft-kairos');
  assert.equal(withDraft.at(-1).body, 'partial reply revised');
  const finalHistory = [...history, { id: 'final', role: 'assistant', body: 'partial reply revised', time: '10:01' }];
  assert.equal(composeLiveMessages(finalHistory, { busy: false, draft: '' }, { contactId: 'kairos', stream }).length, 2, 'persisted final wins without a duplicate transient');
  const terminal = reduceStreamDraft(stream, { event: 'lifecycle', contact_id: 'kairos', turn_id: 'turn-1', reply_state: 'completed', terminal: true, refresh_history: true, updated_at: '2026-08-09T11:26:03Z' });
  assert.equal(terminal.terminal, true);
  assert.equal(terminal.refreshHistory, true);
  const newer = reduceStreamDraft(null, { event: 'draft', turn_id: 'turn-2', text: 'new turn', updated_at: '2026-08-09T11:27:00Z' });
  assert.equal(reduceStreamDraft(newer, { event: 'lifecycle', turn_id: 'turn-1', terminal: true, updated_at: '2026-08-09T11:26:03Z' }).turnId, 'turn-2', 'old lifecycle cannot erase a newer draft');
  assert.equal(composeLiveMessages(history, { busy: true, draft: '' }, { contactId: 'kairos', stream: { body: '', streaming: true } }).length, 1, 'busy without text has no empty bubble');
  assert.equal(composeLiveMessages(history, { busy: false, replyState: 'completed', draft: 'expired draft' }, { contactId: 'kairos' }).length, 1, 'idle terminal snapshots never revive an expired draft');
  let revised = reduceStreamDraft(null, { event: 'draft', turn_id: 'turn-r', revision: 2, text: 'new', updated_at: '2026-08-09T11:30:02Z' });
  revised = reduceStreamDraft(revised, { event: 'draft', turn_id: 'turn-r', revision: 1, text: 'old', updated_at: '2026-08-09T11:30:03Z' });
  assert.equal(revised.body, 'new', 'lower revision cannot overwrite a newer draft');
  const tombstone = reduceStreamDraft(revised, { event: 'lifecycle', turn_id: 'turn-r', revision: 2, terminal: true, updated_at: '2026-08-09T11:30:04Z' });
  assert.equal(reduceStreamDraft(tombstone, { event: 'draft', turn_id: 'turn-r', revision: 2, text: 'late', updated_at: '2026-08-09T11:30:05Z' }).terminal, true, 'terminal tombstone rejects a late same-revision draft');
  const pollWins = composeLiveMessages(history, { busy: true, replyState: 'generating', turnId: 'turn-r', revision: 3, updatedAt: '2026-08-09T11:30:06Z', draft: 'poll recovers full text' }, { contactId: 'kairos', stream: revised });
  assert.equal(pollWins.at(-1).body, 'poll recovers full text', 'newer authoritative poll heals missed SSE chunks');
  assert.equal(reduceStreamDraft(null, { event: 'done', stream_id: 'legacy', text: 'complete legacy reply' }).body, 'complete legacy reply', 'legacy done text is a full recovery payload');
  for (const replyState of ['completed', 'interrupted', 'failed']) {
    const reconciled = reconcileSnapshotStream({ turnId: 'turn-r', revision: '2', body: 'lost lifecycle', streaming: true, updatedAt: '2026-08-09T11:30:02Z' }, { busy: false, replyState, turnId: 'turn-r', revision: '2', updatedAt: '2026-08-09T11:30:03Z' });
    assert.equal(reconciled.terminal, true, `${replyState} poll snapshot closes a missed lifecycle`);
    assert.equal(composeLiveMessages(history, { busy: false, replyState, turnId: 'turn-r' }, { contactId: 'kairos', stream: reconciled }).length, 1);
  }
});

test('typing bubble is busy-only and yields to visible live draft text', () => {
  const history = [{ id: 'old', role: 'assistant', body: 'earlier', time: '10:00' }, { id: 'prompt', role: 'user', body: '继续', time: '10:01' }];
  assert.equal(shouldShowTypingBubble(history, { busy: true, replyState: 'generating', draft: '' }, { contactId: 'kairos' }), true);
  assert.equal(shouldShowTypingBubble(history, { busy: true, replyState: 'generating', draft: 'partial' }, { contactId: 'kairos' }), false);
  assert.equal(shouldShowTypingBubble(history, { busy: false, replyState: 'completed', draft: '' }, { contactId: 'kairos' }), false);
  assert.equal(shouldShowTypingBubble(history, { busy: true, replyState: 'generating', draft: '' }, { contactId: 'xiaoke' }), true);
  assert.equal(shouldShowTypingBubble([...history, { id: 'final', role: 'assistant', body: '已完成回复', streaming: false }], { busy: true, replyState: 'generating', draft: '' }, { contactId: 'kairos' }), false);
});

test('stages raw attachment bytes then sends only returned attachment IDs with memory-only CSRF', async () => {
  const calls = []; const uploads = [];
  const adapter = createHttpAdapter({
    request: async (path, options = {}) => { calls.push({ path, options }); if (path === '/web/session') return { ok: true, csrf_token: 'csrf-memory-only' }; return { ok: true }; },
    upload: async (path, options) => { uploads.push({ path, options }); options.onProgress(4, 4); return { attachments: [{ attachment_id: 'att-1', filename: 'note.txt', type: 'file', size: 4 }], expires_in_seconds: 900 }; },
  });
  await adapter.getWebSession();
  const staged = await adapter.uploadAttachments('kairos', [{ name: 'note.txt', type: 'text/plain', size: 4 }]);
  await adapter.sendMessage('kairos', { text: 'with attachment', attachmentIds: staged.map(({ attachment_id }) => attachment_id) });
  assert.equal(uploads[0].path, '/chat/upload?contact_id=kairos&filename=note.txt&role=user');
  assert.equal(uploads[0].options.body.size, 4);
  assert.equal(uploads[0].options.headers['X-CC-Web-CSRF'], 'csrf-memory-only');
  assert.deepEqual(calls.at(-1), { path: '/chat/send', options: { method: 'POST', body: { contact_id: 'kairos', text: 'with attachment', attachment_ids: ['att-1'] }, headers: { 'X-CC-Web-CSRF': 'csrf-memory-only' } } });
});

test('uploads an unnamed image with a display-only filename fallback without rewriting the File', async () => {
  const uploads = []; const file = { type: 'image/png', size: 4 };
  const adapter = createHttpAdapter({
    request: async (path) => path === '/web/session' ? { csrf_token: 'csrf' } : { ok: true },
    upload: async (path, options) => { uploads.push({ path, options }); return { attachments: [{ attachment_id: 'image-1' }] }; },
  });
  await adapter.getWebSession(); await adapter.uploadAttachments('kairos', [file]);
  assert.equal(uploads[0].path, '/chat/upload?contact_id=kairos&filename=pasted-image-1.png&role=user');
  assert.equal(uploads[0].options.body, file);
  assert.equal(file.type, 'image/png');
});

test('keeps ordinary selected filenames and rejects an unnamed non-image as before', async () => {
  const uploads = [];
  const adapter = createHttpAdapter({
    request: async (path) => path === '/web/session' ? { csrf_token: 'csrf' } : { ok: true },
    upload: async (path, options) => { uploads.push({ path, options }); return { attachments: [{ attachment_id: 'file-1' }] }; },
  });
  await adapter.getWebSession();
  await adapter.uploadAttachments('kairos', [{ name: 'notes.pdf', type: 'application/pdf', size: 4 }]);
  assert.match(uploads[0].path, /filename=notes\.pdf/);
  await assert.rejects(() => adapter.uploadAttachments('kairos', [{ type: 'application/pdf', size: 4 }]), /文件/);
});

test('uses the server-advertised pending total-byte and file-count limits', async () => {
  let uploads = 0;
  const adapter = createHttpAdapter({
    request: async (path) => path === '/web/session' ? { csrf_token: 'csrf', upload_limits: { max_file_bytes: 5, max_pending_files: 2, max_pending_bytes: 6, ttl_seconds: 900 } } : { ok: true },
    upload: async () => { uploads += 1; return { attachments: [{ attachment_id: `id-${uploads}` }] }; },
  });
  await adapter.getWebSession();
  assert.deepEqual(adapter.getUploadLimits(), { max_file_bytes: 5, max_pending_files: 2, max_pending_bytes: 6, ttl_seconds: 900 });
  await assert.rejects(() => adapter.uploadAttachments('kairos', [{ name: 'a', size: 4 }, { name: 'b', size: 3 }]), /合计/);
  await assert.rejects(() => adapter.uploadAttachments('kairos', [{ name: 'a', size: 1 }, { name: 'b', size: 1 }, { name: 'c', size: 1 }]), /最多/);
  assert.equal(uploads, 0);
});

test('memory adapter preserves category-only canonical scopes for top-level taxonomy entries', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path) => { calls.push(path); return { memories: [] }; } });
  await adapter.listMemories({ category: 'archive' });
  await adapter.listMemories({ category: 'archive', subcategory: 'archive.letters' });
  assert.equal(calls[0], '/memory/list?category=archive');
  assert.equal(calls[1], '/memory/list?category=archive&subcategory=archive.letters');
});

test('composer operations isolate concurrent contacts, cancellation, and stale finalizers', () => {
  const composers = createComposerState();
  const xiaoke = composers.begin('xiaoke'); const kairos = composers.begin('kairos');
  composers.setProgress('xiaoke', xiaoke, '1/2 · 50%'); composers.setProgress('kairos', kairos, '1/1 · 40%');
  assert.equal(composers.current('xiaoke').progress, '1/2 · 50%');
  assert.equal(composers.current('kairos').progress, '1/1 · 40%');
  composers.cancel('xiaoke'); assert.equal(xiaoke.controller.signal.aborted, true); assert.equal(kairos.controller.signal.aborted, false);
  composers.finish('xiaoke', xiaoke); assert.equal(composers.current('xiaoke'), null); assert.equal(composers.current('kairos'), kairos);
  const retry = composers.begin('xiaoke'); composers.finish('xiaoke', xiaoke);
  assert.equal(composers.current('xiaoke'), retry, 'an old operation cannot clear a new one');
  composers.cancelAll(); assert.equal(retry.controller.signal.aborted, true); assert.equal(kairos.controller.signal.aborted, true);
});

test('staged attachment failures cancel owned IDs and preserve server 409 errors', async () => {
  const calls = []; let uploadCount = 0;
  const adapter = createHttpAdapter({
    request: async (path, options = {}) => { calls.push({ path, options }); if (path === '/web/session') return { csrf_token: 'csrf' }; if (path === '/chat/send') throw Object.assign(new Error('attachment_missing_or_expired'), { status: 409 }); return { ok: true }; },
    upload: async () => { uploadCount += 1; if (uploadCount === 1) return { attachments: [{ attachment_id: 'owned-1' }] }; throw new Error('upload failed'); },
  });
  await adapter.getWebSession();
  await assert.rejects(() => adapter.uploadAttachments('xiaoke', [{ name: 'one.png', size: 1 }, { name: 'two.png', size: 1 }]), /upload failed/);
  assert.deepEqual(calls.at(-1), { path: '/chat/upload/cancel', options: { method: 'POST', body: { attachment_ids: ['owned-1'] }, headers: { 'X-CC-Web-CSRF': 'csrf' } } });
  await assert.rejects(() => adapter.sendMessage('xiaoke', { text: 'replay', attachmentIds: ['expired'] }), /attachment_missing_or_expired/);
});

test('logout uses CSRF then clears the in-memory token', async () => {
  const calls = []; const adapter = createHttpAdapter({ request: async (path, options = {}) => { calls.push({ path, options }); return path === '/web/session' ? { csrf_token: 'csrf-once' } : { ok: true }; } });
  await adapter.getWebSession(); await adapter.logout(); await adapter.sendMessage('kairos', { text: 'after logout' });
  assert.equal(calls[1].options.headers['X-CC-Web-CSRF'], 'csrf-once');
  assert.equal(calls[2].options.headers, undefined);
});

test('mock adapter maintains separate contact histories', async () => {
  const adapter = createMockAdapter(); const xiaokeBefore = await adapter.getHistory('xiaoke'); const kairosBefore = await adapter.getHistory('kairos');
  await adapter.sendMessage('kairos', { text: 'separate desk history' });
  const xiaokeAfter = await adapter.getHistory('xiaoke'); const kairosAfter = await adapter.getHistory('kairos');
  assert.equal(xiaokeAfter.length, xiaokeBefore.length);
  assert.equal(kairosAfter.length, kairosBefore.length + 2);
  const live = await adapter.getLiveState('kairos');
  await adapter.stop('kairos', live.stopRequest);
});

test('mock adapter resolves a top-level category without manufacturing a subcategory key', async () => {
  const adapter = createMockAdapter();
  const entries = await adapter.listMemories({ category: 'archive' });
  assert.equal(entries[0].title, '归档记忆');
});

test('mock is explicit while production bootstrap checks the cookie session', async () => {
  assert.equal(isExplicitMockMode({ search: '?mock=1' }, {}), true);
  assert.equal(isExplicitMockMode({ search: '' }, {}), false);
  const calls = [];
  const bootstrap = createPwaBootstrap({ locationLike: { search: '' }, windowLike: {}, request: async (path) => { calls.push(path); return { ok: true, authenticated: true }; } });
  assert.equal(bootstrap.mock, false);
  await bootstrap.checkSession();
  assert.deepEqual(calls, ['/web/session']);
});

test('pairing uses the unauthenticated same-origin endpoint and accepts its session payload', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path, options = {}) => { calls.push({ path, options }); return { ok: true, csrf_token: 'pair-csrf', upload_limits: { max_file_bytes: 7, max_pending_files: 2, max_pending_bytes: 9 } }; } });
  await adapter.pairWebSession({ code: 'ABCD1234' });
  assert.deepEqual(calls, [{ path: '/web/session/pair', options: { method: 'POST', body: { code: 'ABCD1234' } } }]);
  assert.equal(adapter.getUploadLimits().max_file_bytes, 7);
});

test('pairing normalization accepts only the shared eight-character alphabet', () => {
  assert.equal(PAIRING_ALPHABET, '23456789ABCDEFGHJKLMNPQRSTUVWXYZ');
  assert.equal(normalizePairingCode('2345 abcd'), '2345ABCD');
  assert.equal(formatPairingCode('2345abcd'), '2345 ABCD');
  for (const rejected of ['1234ABCD', '0234ABCD', 'I234ABCD', 'O234ABCD', '2345ABCD9', '2345-ABCD!']) {
    assert.equal(normalizePairingCode(rejected), '', rejected);
  }
});

test('warm paper palette preserves AA text and visible control boundaries', async () => {
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  const paper = hexToken(css, 'paper');
  for (const token of ['ink', 'muted', 'dim', 'placeholder', 'oxide', 'brass', 'moss', 'danger']) {
    assert.ok(contrastRatio(hexToken(css, token), paper) >= 4.5, `${token} must be AA against paper`);
  }
  assert.ok(contrastRatio(hexToken(css, 'control-border'), paper) >= 3, 'control boundary must remain visible');
  assert.match(css, /\.avatar\{color:var\(--paper\)\}/);
  assert.match(css, /\.login-card input\{[^}]*border:1px solid var\(--control-border\)/);
  assert.match(css, /input::placeholder\{color:var\(--placeholder\);opacity:1\}/);
  assert.match(css, /\.contact-button\.is-active\{border-color:var\(--brass\);box-shadow:inset 3px 0 0 var\(--brass\);padding-left:12px/);
  assert.match(css, /\.contact-button\.is-active \.contact-copy strong\{font-weight:700/);
  assert.match(css, /\.send-button:disabled\{opacity:1;background:var\(--brass-wash\);color:var\(--muted\);cursor:not-allowed\}/);
});

test('PWA shell declares installability and has no secret persistence', async () => {
  const [manifest, serviceWorker, source] = await Promise.all([
    readFile(new URL('../manifest.webmanifest', import.meta.url), 'utf8'),
    readFile(new URL('../sw.js', import.meta.url), 'utf8'),
    readFile(new URL('../src/api.js', import.meta.url), 'utf8'),
  ]);
  assert.match(manifest, /"display": "standalone"/);
  assert.match(manifest, /"background_color": "#fff6ec"/);
  assert.match(manifest, /"theme_color": "#fff6ec"/);
  assert.match(manifest, /icon-192\.png/);
  assert.match(manifest, /icon-512\.png/);
  assert.match(serviceWorker, /addEventListener\('fetch'/);
  assert.match(serviceWorker, /cccompanion-desk-v8/);
  assert.match(serviceWorker, /src\/styles\.css\?v=8/);
  assert.match(serviceWorker, /src\/app\.js\?v=8/);
  assert.match(serviceWorker, /src\/clipboard-images\.js\?v=8/);
  assert.match(serviceWorker, /src\/live-messages\.js/);
  assert.match(serviceWorker, /src\/pairing-code\.js/);
  assert.doesNotMatch(source, /shared_secret|localStorage\.setItem|sessionStorage\.setItem|credentials:\s*'include'/);
  assert.match(source, /credentials:\s*'same-origin'/);
  assert.match(source, /cache:\s*'no-store'/);
});

test('PWA source contracts preserve responsive, private, and accessible behavior', async () => {
  const [html, app, css, serviceWorker] = await Promise.all([
    readFile(new URL('../index.html', import.meta.url), 'utf8'),
    readFile(new URL('../src/app.js', import.meta.url), 'utf8'),
    readFile(new URL('../src/styles.css', import.meta.url), 'utf8'),
    readFile(new URL('../sw.js', import.meta.url), 'utf8'),
  ]);
  assert.match(html, /id="latest-button"/);
  assert.match(html, /id="pairing-code"/);
  assert.match(html, /href="\.\/src\/styles\.css\?v=8"/);
  assert.match(html, /src="\.\/src\/app\.js\?v=8"/);
  assert.match(html, /autocomplete="one-time-code"/);
  assert.match(html, /id="pairing-form"/);
  assert.match(html, /<details class="password-fallback">/);
  const csp = html.match(/<meta http-equiv="Content-Security-Policy" content="([^"]+)"/);
  assert.ok(csp, 'the static shell declares a CSP before body scripts can execute');
  assert.ok(html.indexOf(csp[0]) < html.indexOf('<script'), 'CSP appears before every script tag');
  assert.match(csp[1], /default-src 'self'/);
  assert.match(csp[1], /script-src 'self'/);
  assert.match(csp[1], /connect-src 'self'/);
  assert.match(csp[1], /worker-src 'self'/);
  assert.match(csp[1], /manifest-src 'self'/);
  assert.match(csp[1], /img-src 'self' data:/);
  assert.doesNotMatch(csp[1], /unsafe-(?:inline|eval)|static\.cloudflareinsights\.com/);
  assert.doesNotMatch(html, /style="/);
  assert.match(html, /class="svg-defs"/);
  assert.match(html, /class="file-input"/);
  assert.match(html, /id="appearance-status" aria-live="polite"/);
  assert.match(html, /id="drawer-appearance-button"/);
  assert.match(html, /role="button" tabindex="0" aria-label="添加图片或文件"/);
  assert.match(app, /const contactId = state\.activeContactId/);
  assert.match(app, /establishPairingSession/);
  assert.match(app, /normalizePairingCode/);
  assert.match(html, /23456789ABCDEFGHJKLMNPQRSTUVWXYZ/);
  assert.match(app, /adapter\.uploadAttachments\(contactId, queued/);
  assert.match(app, /adapter\.sendMessage\(contactId,/);
  assert.match(app, /extractClipboardImageFiles\(event\.clipboardData\)/);
  assert.match(app, /if \(!images\.length\) return/);
  assert.match(app, /event\.preventDefault\(\)/);
  assert.match(app, /createComposerState/);
  assert.match(app, /state\.composerState\.begin\(contactId\)/);
  assert.match(app, /state\.composerState\.isCurrent\(contactId, operation\)/);
  assert.match(app, /state\.composerState\.cancelAll\(\)/);
  assert.match(app, /Object\.hasOwn\(state\.scrollTops, id\)/);
  assert.match(app, /workerDrawerQuery\.addEventListener\('change', syncWorkerPanel\)/);
  assert.match(app, /function closeWorkers\(\) \{ setWorkers\(false, \{ returnFocus: true \}\); \}/);
  assert.match(app, /Intl\.DateTimeFormat\('zh-CN'/);
  assert.match(app, /if \(!subs\.length\)/);
  assert.match(app, /state\.followLatest\[state\.activeContactId\] !== false && nearBottom\(\)/);
  assert.match(app, /body\.dataset\.appearance = state\.appearance/);
  assert.match(app, /appearance-status'\)\.textContent/);
  assert.match(css, /height:100dvh/);
  assert.match(css, /safe-area-inset-bottom/);
  assert.match(css, /@media \(max-width:720px\)/);
  assert.match(css, /prefers-reduced-motion:reduce/);
  assert.match(css, /a:focus-visible,input:focus-visible/);
  assert.match(html, /name="theme-color" content="#fff6ec"/);
  assert.match(css, /color-scheme:light/);
  assert.match(css, /--paper:#fff6ec/);
  assert.match(css, /--surface:#ffefdf/);
  assert.match(css, /--ink:#2a170f/);
  assert.match(css, /--oxide:#a35347/);
  assert.match(css, /--dim:#79513c/);
  assert.match(css, /#pairing-code/);
  assert.match(css, /\[hidden\]\{display:none!important\}/);
  assert.match(css, /attachment-chip button,.taxonomy-choices button\{min-width:44px/);
  assert.equal((css.match(/@media \(max-width:390px\)/g) || []).length, 1);
  assert.match(css, /\.head-actions \.worker-toggle,\.head-actions #memory-button\{display:inline-block\}/);
  assert.match(serviceWorker, /web\\\/session\|chat\|memory\|attachments/);
  assert.match(serviceWorker, /const isShellAsset = ASSETS\.some/);
  assert.match(serviceWorker, /if \(url\.origin !== self\.location\.origin \|\| !isShellAsset \|\| isPrivateRoute\) return/);
});
