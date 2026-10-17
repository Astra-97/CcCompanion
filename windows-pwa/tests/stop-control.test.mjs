import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { normalizeLiveState, optimisticBusyState, stopControlState, mergeHistoryDelta } from '../src/api.js';
import { reduceStreamDraft } from '../src/live-messages.js';

test('optimistic send enters busy immediately with a disarmed stop key and never reuses an old fence', () => {
  const before = normalizeLiveState({ busy: false, reply_state: 'idle', status_text: '待命' });
  assert.deepEqual(stopControlState(before), { visible: false, enabled: false, label: '正在启动…' });
  const optimistic = optimisticBusyState(before);
  assert.equal(optimistic.busy, true);
  assert.equal(optimistic.replyState, 'generating');
  assert.equal(optimistic.statusText, '正在启动…');
  assert.deepEqual(optimistic.stopRequest, { supported: false, body: null });
  assert.deepEqual(stopControlState(optimistic), { visible: true, enabled: false, label: '正在启动…' }, 'visible but disabled until the server fence arrives');
  const staleFence = { ...before, stopRequest: { supported: true, body: { contact_id: 'xiaoke', user_ts: 'old-turn' } } };
  assert.equal(optimisticBusyState(staleFence).stopRequest.supported, false, 'a previous turn fence is never reused for a new send');
});

test('stop control arms when a late /chat/status brings stop_request, and SSE drafts never disarm it', () => {
  // Timeline: send accepted → optimistic → status poll without fence → SSE
  // draft merge → status poll with fence → final idle snapshot.
  let live = optimisticBusyState(normalizeLiveState({}));
  assert.equal(stopControlState(live).enabled, false);

  live = normalizeLiveState({ busy: true, reply_state: 'generating', status_text: '正在处理', draft: { text: '半截回复' } });
  assert.deepEqual(stopControlState(live), { visible: true, enabled: false, label: '正在启动…' }, 'busy without a fence stays visibly disabled, not hidden');

  const stream = reduceStreamDraft(null, { event: 'draft', turn_id: 'turn-9', reply_state: 'generating', text: '半截回复', updated_at: '2026-10-17T04:00:01Z' });
  assert.equal(stream.streaming, true);
  const sseMerged = normalizeLiveState({ ...live, busy: true, reply_state: 'generating', turn_id: 'turn-9', draft: { text: '半截回复更长' } });
  assert.equal(stopControlState(sseMerged).visible, true, 'SSE draft keeps the busy UI alive');

  const fenced = normalizeLiveState({ busy: true, reply_state: 'generating', status_text: '正在处理', stop_request: { supported: true, body: { contact_id: 'xiaoke', user_ts: 'turn-9' } } });
  assert.deepEqual(stopControlState(fenced), { visible: true, enabled: true, label: '停止' });

  const done = normalizeLiveState({ busy: false, reply_state: 'idle', status_text: '待命' });
  assert.equal(stopControlState(done).visible, false, 'idle snapshot hides the stop key again');
});

test('incremental history merges cannot wipe the optimistic busy projection', () => {
  const optimistic = optimisticBusyState(normalizeLiveState({}));
  const merged = mergeHistoryDelta(
    [{ id: 'u1', role: 'user', body: '刚发的', ts: '2026-10-17T04:00:00+00:00' }],
    [{ id: 'u1', role: 'user', body: '刚发的', ts: '2026-10-17T04:00:00+00:00' }],
    ['u1'],
  );
  assert.deepEqual(merged.pendingIds, [], 'send confirmation clears pending');
  assert.equal(stopControlState(optimistic).visible, true, 'live state is untouched by history merges');
});

test('app.js wires optimistic busy on send success and renders the stop key from stopControlState', () => {
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const sendSource = app.slice(app.indexOf('async function send('), app.indexOf('function autoResize'));
  assert.ok(sendSource.indexOf('normalizeSendRecord(response)') < sendSource.indexOf('optimisticBusyState('), 'optimistic busy applies only after the server accepted the send');
  assert.match(sendSource, /state\.live\[contactId\] = optimisticBusyState\(state\.live\[contactId\]\)/);
  assert.match(sendSource, /renderMessages\(\{ forceLatest: true \}\); renderHeader\(\)/, 'header re-renders in the same tick as the optimistic message');
  const headerSource = app.slice(app.indexOf('function renderHeader'), app.indexOf('function controlEntry'));
  assert.match(headerSource, /const stopControl = stopControlState\(live\)/);
  assert.match(headerSource, /stopButton\.hidden = !stopControl\.visible/);
  assert.match(headerSource, /stopButton\.disabled = !stopControl\.enabled/);
  assert.match(headerSource, /stopButton\.setAttribute\('aria-label', stopControl\.label\)/);
  assert.match(headerSource, /\$\('#send-button'\)\.hidden = stopControl\.visible/, 'busy swaps the send key for the stop circle like the App');
  assert.doesNotMatch(headerSource, /hidden = !\(isBusy\(\) && live\.stopRequest/, 'the old hide-without-fence logic is gone');
});
