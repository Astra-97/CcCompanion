import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createHttpAdapter, createMockAdapter, normalizeControlPreferences, normalizeControlStatus, controlPanelSupported } from '../src/api.js';
import { humanPreviewText, previewText } from '../src/unread.js';

test('control panel support is exactly the four provider contacts', () => {
  for (const id of ['xiaoke', 'kairos', 'kimi', 'kiro']) assert.ok(controlPanelSupported(id), id);
  for (const id of ['hakimi', 'apples', 'toolbot', '']) assert.ok(!controlPanelSupported(id), id || '(empty)');
});

test('normalizeControlStatus projects each provider payload onto one shape', () => {
  const xiaoke = normalizeControlStatus('xiaoke', {
    ok: true, provider: 'Claude Code', model: 'Fable 5', effort: 'high', busy: false,
    active_session_id: 'c7f2a1d0-4b6e', context: { available: true, used_percent: 38 },
    quota: { windows: [{ label: '5 小时窗口', used_percent: 41, text: '41% 已用', reset_text: '2 小时后重置' }] },
  });
  assert.deepEqual(xiaoke, {
    available: true, provider: 'Claude Code', busy: false, model: 'Fable 5', effort: 'high',
    sessionId: 'c7f2a1d0-4b6e', context: { available: true, usedPercent: 38, text: '' },
    quotaText: '', quotaWindows: [{ label: '5 小时窗口', usedPercent: 41, text: '41% 已用', resetText: '2 小时后重置' }],
  });

  const kairos = normalizeControlStatus('kairos', {
    ok: true, model: 'gpt-5.5', reasoning_effort: 'high', busy: true,
    active_session_id: '01a06f90-dea6', context: { used_percent: 57 }, quota: { plan: 'Plus', windows: [] },
  });
  assert.equal(kairos.effort, 'high');
  assert.equal(kairos.busy, true);
  assert.equal(kairos.context.usedPercent, 57, 'codex context lacks available=true but still carries a percent');
  assert.equal(kairos.quotaText, 'Plus');

  const kimi = normalizeControlStatus('kimi', {
    ok: true, provider: 'Kimi Code', model: 'kimi-for-coding', reasoning_effort: 'max', busy: false,
    active_session_id: 'km-1', context: { available: true, used_percent: 24, text: '48.2k / 200k tokens' },
    quota: { text: '额度充足', windows: [] },
  });
  assert.equal(kimi.context.text, '48.2k / 200k tokens');
  assert.equal(kimi.quotaText, '额度充足');

  const kiro = normalizeControlStatus('kiro', {
    ok: true, provider: 'Kiro', selection: { model: 'kiro-auto', effort: 'medium' },
    current_effort: 'medium', busy: false, context_usage_percent: 31,
  });
  assert.equal(kiro.model, 'kiro-auto');
  assert.equal(kiro.effort, 'medium');
  assert.equal(kiro.context.usedPercent, 31);

  assert.equal(normalizeControlStatus('xiaoke', null).available, false);
  assert.equal(normalizeControlStatus('apples', { ok: true }).available, false, 'unknown contacts never project');
});

test('normalizeControlPreferences projects pick-lists and selection per provider', () => {
  const kairos = normalizeControlPreferences('kairos', {
    ok: true, models: [
      { id: 'gpt-5.5', supported_reasoning_efforts: ['low', 'medium', 'high', 'xhigh'] },
      { id: 'gpt-5.5-codex', supported_reasoning_efforts: ['medium', 'high'] },
    ],
    selection: { model: 'gpt-5.5', reasoning_effort: 'high' }, busy: false, applies_from: 'next_turn',
  });
  assert.equal(kairos.model, 'gpt-5.5');
  assert.equal(kairos.effort, 'high');
  assert.deepEqual(kairos.models[0], { id: 'gpt-5.5', label: 'gpt-5.5', efforts: ['low', 'medium', 'high', 'xhigh'] });
  assert.equal(kairos.appliesFrom, 'next_turn');

  const xiaoke = normalizeControlPreferences('xiaoke', {
    ok: true, provider: 'Claude Code', model: 'Fable 5', effort: 'high',
    available_models: ['Fable 5', 'Opus 4.5'], available_efforts: ['low', 'medium', 'high'],
    applies_from: { model: 'Fable 5', effort: 'high' },
  });
  assert.deepEqual(xiaoke.models.map(({ id }) => id), ['Fable 5', 'Opus 4.5']);
  assert.deepEqual(xiaoke.efforts, ['low', 'medium', 'high']);
  assert.equal(xiaoke.appliesFrom, 'next_session', 'object applies_from becomes a session hint');

  const kiro = normalizeControlPreferences('kiro', {
    ok: true, provider: 'Kiro', available_models: ['kiro-auto'], available_efforts: ['low', 'medium'],
    selection: { model: 'kiro-auto', effort: 'low' },
  });
  assert.equal(kiro.model, 'kiro-auto');
  assert.equal(kiro.effort, 'low');
});

test('control adapter hits only the gated paths with the per-provider effort field', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path, options = {}) => { calls.push({ path, options }); return { ok: true }; } });
  await adapter.getControlStatus('xiaoke');
  await adapter.getControlStatus('kairos');
  await adapter.getControlStatus('kimi');
  await adapter.getControlStatus('kiro');
  await adapter.getControlPreferences('kiro');
  assert.deepEqual(calls.map(({ path }) => path), ['/xiaoke/status', '/codex/status', '/kimi/status', '/kiro/preferences', '/kiro/preferences'],
    'kiro folds its status read into the preferences GET');
  await adapter.saveControlPreferences('xiaoke', { model: 'Fable 5', effort: 'low' });
  await adapter.saveControlPreferences('kairos', { model: 'gpt-5.5', effort: 'xhigh' });
  await adapter.saveControlPreferences('kimi', { effort: 'max' });
  await adapter.saveControlPreferences('kiro', { model: 'kiro-auto', effort: 'high' });
  assert.deepEqual(calls[5].options.body, { model: 'Fable 5', effort: 'low' });
  assert.deepEqual(calls[6].options.body, { model: 'gpt-5.5', reasoning_effort: 'xhigh' }, 'kairos posts reasoning_effort');
  assert.deepEqual(calls[7].options.body, { reasoning_effort: 'max' }, 'absent fields are omitted so the server keeps them');
  assert.deepEqual(calls[8].options.body, { model: 'kiro-auto', effort: 'high' });
  await assert.rejects(() => adapter.saveControlPreferences('apples', { model: 'x' }), /没有控制面板/);
  await assert.rejects(() => adapter.saveControlPreferences('xiaoke', {}), /没有要应用的偏好变更/);
});

test('mock adapter serves and applies control fixtures', async () => {
  const adapter = createMockAdapter();
  const status = await adapter.getControlStatus('xiaoke');
  assert.equal(status.model, 'Fable 5');
  assert.equal(status.quotaWindows.length, 2);
  const kairos = await adapter.getControlPreferences('kairos');
  assert.equal(kairos.models[0].efforts.includes('xhigh'), true);
  await adapter.saveControlPreferences('kairos', { effort: 'xhigh' });
  assert.equal((await adapter.getControlPreferences('kairos')).effort, 'xhigh', 'mock save persists within the session');
  assert.equal((await adapter.getControlStatus('kairos')).effort, 'xhigh');
  assert.equal((await adapter.getControlPreferences('apples')).available, false);
});

test('contact previews never surface raw event-stream JSON or log lines', () => {
  // Real payload shape from tokens/chat_history_apples.jsonl (2026-10-03).
  const rawEventBody = [
    '{"type":"thread.started","thread_id":"01a06f90-dea6-76d3-b8e4-e6d800361d09"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-6-astra` not found."}}',
    '{"type":"turn.started"}',
    '2026-10-03T11:14:22.401Z [33mWARN[0m codex_core::stream: something noisy',
    '',
  ].join('\n');
  assert.equal(humanPreviewText(rawEventBody), '');
  assert.equal(previewText({ role: 'assistant', body: rawEventBody }), '', 'event-only records leave the preview to the note fallback');
  const mixed = `小朋友们好\n{"type":"turn.started"}\n今天天气不错`;
  assert.equal(previewText({ role: 'assistant', body: mixed }), '小朋友们好 今天天气不错');
  assert.equal(previewText({ role: 'user', body: `我说\n{"type":"x"}` }), '我: 我说');
  assert.equal(previewText({ role: 'assistant', body: '看代码\n```js\nconst a = 1;\n```\n完了' }), '看代码 [代码] 完了', 'code blocks still fold after line filtering');
});

test('draft clearing happens before any await in send(), and el() never uses dataset writes', () => {
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const sendSource = app.slice(app.indexOf('async function send('), app.indexOf('function autoResize'));
  const clearAt = sendSource.indexOf("draft.text = ''; input.value = '';");
  assert.ok(clearAt > -1, 'send clears the draft and the textarea');
  const firstAwait = sendSource.indexOf('await adapter.uploadAttachments');
  assert.ok(clearAt < firstAwait, 'the draft clears synchronously, before the first await — no render can resurrect it');
  assert.match(app, /else node\.setAttribute\(key, value\)/, 'el() writes data-* via setAttribute (DOMStringMap rejects hyphenated keys)');
  assert.doesNotMatch(app, /node\.dataset\[key/, 'the dataset write that crashed renderMessages mid-send is gone');
});

test('chat header is avatar + name + one status line; busy text lives only under the name', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  assert.doesNotMatch(html, /id="contact-mode"/);
  assert.match(html, /id="contact-avatar"/);
  assert.match(html, /id="contact-status"/);
  const headerSource = app.slice(app.indexOf('function renderHeader'), app.indexOf('function controlEntry'));
  assert.doesNotMatch(headerSource, /#contact-mode/, 'renderHeader no longer fills a channel eyebrow');
  assert.match(headerSource, /\$\('#connection-state'\)\.textContent = '已连接'/, 'the transport line no longer mirrors busy text');
  assert.doesNotMatch(headerSource, /isBusy\(\) \? \(live\.statusText/, 'no second copy of the typing status in the header');
});
