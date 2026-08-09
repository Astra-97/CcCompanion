import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { createHttpAdapter, createMockAdapter, normalizeLiveState, normalizeRecord } from '../src/api.js';
import { createPwaBootstrap, isExplicitMockMode } from '../src/bootstrap.js';

test('normalizes bounded worker activity without relying on server naming', () => {
  assert.deepEqual(normalizeLiveState({
    busy: true, reply_state: 'generating', status_text: '生成中', draft: { text: 'partial', activity_count: 2,
      worker_activity_items: [{ worker_id: 'layout', name: 'windows_pwa_shell', status: 'running', count: 3 }] },
    stop_request: { supported: true, body: { contact_id: 'kairos', user_ts: 'turn-1' } },
  }), {
    busy: true, replyState: 'generating', statusText: '生成中', draft: 'partial', activityText: '', activityCount: 2,
    stopRequest: { supported: true, body: { contact_id: 'kairos', user_ts: 'turn-1' } },
    workers: [{ id: 'layout', name: 'windows_pwa_shell', state: 'running', count: 3 }],
  });
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

test('mock is explicit while production bootstrap checks the cookie session', async () => {
  assert.equal(isExplicitMockMode({ search: '?mock=1' }, {}), true);
  assert.equal(isExplicitMockMode({ search: '' }, {}), false);
  const calls = [];
  const bootstrap = createPwaBootstrap({ locationLike: { search: '' }, windowLike: {}, request: async (path) => { calls.push(path); return { ok: true, authenticated: true }; } });
  assert.equal(bootstrap.mock, false);
  await bootstrap.checkSession();
  assert.deepEqual(calls, ['/web/session']);
});

test('PWA shell declares installability and has no secret persistence', async () => {
  const [manifest, serviceWorker, source] = await Promise.all([
    readFile(new URL('../manifest.webmanifest', import.meta.url), 'utf8'),
    readFile(new URL('../sw.js', import.meta.url), 'utf8'),
    readFile(new URL('../src/api.js', import.meta.url), 'utf8'),
  ]);
  assert.match(manifest, /"display": "standalone"/);
  assert.match(manifest, /icon-192\.png/);
  assert.match(manifest, /icon-512\.png/);
  assert.match(serviceWorker, /addEventListener\('fetch'/);
  assert.doesNotMatch(source, /shared_secret|localStorage\.setItem|sessionStorage\.setItem|credentials:\s*'include'/);
  assert.match(source, /credentials:\s*'same-origin'/);
  assert.match(source, /cache:\s*'no-store'/);
});
