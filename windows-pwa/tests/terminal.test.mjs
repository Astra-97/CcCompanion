import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHttpAdapter, createMockAdapter } from '../src/api.js';
import {
  TERMINAL_TARGETS, TERMINAL_MODE, TERMINAL_KEYBAR, SERVER_TERMINAL_KEYS, initialMode, modeAfterCapture, canSend, canResize,
  modeAfterWaitingInputRejected, normalizeTerminalCapture, resolveKeybarKey, ctrlLetterKey, passthroughKey, terminalGrid,
  slashSuggestions, terminalErrorText, createTerminalController,
} from '../src/terminal.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

/** Manual timer queue so the 1.5s poll loop is deterministic. */
function fakeClock() {
  const timers = new Map(); let next = 1;
  return {
    setTimer: (fn, ms) => { const id = next++; timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { timers.delete(id); },
    pending: () => [...timers.values()].map(({ ms }) => ms),
    async flush() { const due = [...timers.entries()]; timers.clear(); for (const [, { fn }] of due) void fn(); await settle(); },
  };
}
const settle = async () => { for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setImmediate(resolve)); };

function fakeAdapter(overrides = {}) {
  const calls = [];
  return {
    calls,
    async getTerminalCapture(session, options) { calls.push(['capture', session, options.lines]); return overrides.capture ? overrides.capture(session) : { content: `${session} pane\n`, state: 'ready' }; },
    async sendTerminalText(session, text) { calls.push(['send', session, text]); if (overrides.send) return overrides.send(session, text); return { ok: true }; },
    async sendTerminalKey(session, key) { calls.push(['key', session, key]); if (overrides.key) return overrides.key(session, key); return { ok: true }; },
    async resizeTerminal(session, columns, rows) { calls.push(['resize', session, columns, rows]); return { ok: true }; },
    async releaseTerminal(target) { calls.push(['release', target]); return { ok: true }; },
  };
}

test('terminal targets and interaction policy are a literal Android TerminalInteractionPolicy port', () => {
  assert.deepEqual(TERMINAL_TARGETS.map(({ id, session }) => [id, session]), [['kairos', 'kairos'], ['xiaoke', 'cctg'], ['kimi', 'kimi'], ['kiro', 'kiro']]);
  assert.equal(initialMode('xiaoke'), TERMINAL_MODE.INTERACTIVE);
  for (const id of ['kairos', 'kimi', 'kiro']) assert.equal(initialMode(id), TERMINAL_MODE.CONNECTING);
  assert.equal(modeAfterCapture('xiaoke', ''), TERMINAL_MODE.INTERACTIVE);
  assert.equal(modeAfterCapture('kairos', 'ready'), TERMINAL_MODE.INTERACTIVE);
  assert.equal(modeAfterCapture('kairos', 'waiting'), TERMINAL_MODE.READ_ONLY);
  assert.equal(modeAfterCapture('kiro', 'waiting'), TERMINAL_MODE.READ_ONLY);
  assert.equal(modeAfterCapture('kimi', 'waiting'), TERMINAL_MODE.INTERACTIVE, 'Kimi busy pane is the real TUI');
  assert.equal(modeAfterCapture('kairos', 'starting'), TERMINAL_MODE.CONNECTING, 'unknown states fail closed');
  assert.equal(canSend('xiaoke', TERMINAL_MODE.CONNECTING), true);
  assert.equal(canSend('kairos', TERMINAL_MODE.READ_ONLY), false);
  assert.equal(canResize('xiaoke', TERMINAL_MODE.INTERACTIVE), false, 'never resize the shared cctg window');
  assert.equal(canResize('kimi', TERMINAL_MODE.INTERACTIVE), true);
  assert.equal(canResize('kiro', TERMINAL_MODE.CONNECTING), false);
  assert.equal(modeAfterWaitingInputRejected('kairos'), TERMINAL_MODE.READ_ONLY);
  assert.equal(modeAfterWaitingInputRejected('kimi'), TERMINAL_MODE.INTERACTIVE);
});

test('capture normalization keeps plain text only and fails closed on state', () => {
  assert.deepEqual(normalizeTerminalCapture({ content: 'a\r\nb\n', state: 'READY', lease: 'x'.repeat(40), session: 'cctg' }), { content: 'a\nb\n', state: 'ready' });
  assert.deepEqual(normalizeTerminalCapture({ content: 42, state: 'weird' }), { content: '', state: '' });
  assert.deepEqual(normalizeTerminalCapture('raw text'), { content: 'raw text', state: '' });
  assert.equal(normalizeTerminalCapture({ content: 'x'.repeat(600 * 1024) }).content.length, 512 * 1024);
});

test('keybar, sticky CTRL and passthrough only ever produce server-whitelisted tmux keys', () => {
  assert.deepEqual(TERMINAL_KEYBAR.map(({ label }) => label), ['ESC', 'CTRL', 'TAB', 'ENTER', '⌫', '^C', 'CLR', '↑', '↓', '←', '→']);
  assert.ok(!TERMINAL_KEYBAR.some(({ label }) => label === 'ALT'), 'ALT is dropped: the server has no M- keys');
  for (const def of TERMINAL_KEYBAR.filter(({ key }) => key)) assert.ok(SERVER_TERMINAL_KEYS.has(def.key), def.label);
  assert.equal(resolveKeybarKey('Escape'), 'Escape');
  assert.equal(resolveKeybarKey('C-u', { ctrl: true }), 'C-u', 'combo keys are sent as-is');
  assert.equal(resolveKeybarKey('Up', { ctrl: true }), '', 'C-Up would be a server 400');
  assert.equal(ctrlLetterKey('R'), 'C-r');
  assert.equal(ctrlLetterKey('x'), '');
  assert.equal(ctrlLetterKey('1'), '');
  assert.equal(passthroughKey({ key: 'ArrowUp' }), 'Up');
  assert.equal(passthroughKey({ key: 'Backspace' }), 'BSpace');
  assert.equal(passthroughKey({ key: 'Escape' }), '', 'Esc would interrupt a running turn: button only');
  assert.equal(passthroughKey({ key: 'c', ctrlKey: true }), '', 'Ctrl+C stays copy');
  assert.equal(passthroughKey({ key: 'ArrowUp', shiftKey: true }), '');
});

test('viewport grid clamps like the server and slash suggestions match Android lists', () => {
  assert.deepEqual(terminalGrid({ width: 800, height: 480, cellWidth: 8, cellHeight: 16 }), { columns: 100, rows: 30 });
  assert.deepEqual(terminalGrid({ width: 40, height: 40, cellWidth: 8, cellHeight: 16 }), { columns: 20, rows: 8 });
  assert.deepEqual(terminalGrid({ width: 9000, height: 9000, cellWidth: 8, cellHeight: 16 }), { columns: 320, rows: 160 });
  assert.equal(terminalGrid({ width: 0, height: 10, cellWidth: 8, cellHeight: 16 }), null);
  assert.deepEqual(slashSuggestions('xiaoke', '/fo').map(([command]) => command), ['/forge']);
  assert.deepEqual(slashSuggestions('xiaoke', '/forge'), [], 'an exact match closes the popup');
  assert.deepEqual(slashSuggestions('kairos', '/st', false), [], 'no suggestions while read-only');
  assert.equal(terminalErrorText('xiaoke', { status: 401 }), '登录已过期，请刷新页面重新配对');
  assert.equal(terminalErrorText('kairos', { status: 503, payload: { error: 'pane gone' } }), 'Kairos 终端暂时不可用：pane gone（HTTP 503）');
  assert.equal(terminalErrorText('kimi', new TypeError('Failed to fetch')), 'Kimi 终端暂时不可用：连接中断，正在重连…');
});

test('HTTP adapter speaks the Android terminal protocol with cookie + CSRF only', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path, options = {}) => { calls.push({ path, options }); return path === '/web/session' ? { csrf_token: 'term-csrf' } : { ok: true }; } });
  await adapter.getWebSession();
  await adapter.getTerminalCapture('cctg', { lines: 200 });
  await adapter.sendTerminalText('cctg', 'hello');
  await adapter.sendTerminalKey('kairos', 'Escape');
  await adapter.resizeTerminal('kimi', 100, 30);
  await adapter.releaseTerminal('kairos');
  const [, capture, send, key, resize, release] = calls;
  assert.equal(capture.path, '/tmux/capture?session=cctg&lines=200'); assert.equal(capture.options.method, undefined);
  assert.deepEqual([send.path, send.options.body], ['/tmux/send', { session: 'cctg', keys: 'hello' }]);
  assert.deepEqual([key.path, key.options.body], ['/terminal/key', { session: 'kairos', key: 'Escape' }]);
  assert.deepEqual([resize.path, resize.options.body], ['/terminal/resize', { session: 'kimi', columns: 100, rows: 30 }]);
  assert.deepEqual([release.path, release.options.body], ['/terminal/release', { target: 'kairos' }]);
  for (const call of [send, key, resize, release]) { assert.equal(call.options.method, 'POST'); assert.equal(call.options.headers['X-CC-Web-CSRF'], 'term-csrf'); }
  for (const call of calls) assert.ok(!JSON.stringify(call).match(/X-Auth-Token|shared_secret/), 'no native secret ever leaves the browser');
});

test('mock adapter terminal echoes input for offline design work', async () => {
  const adapter = createMockAdapter();
  await adapter.sendTerminalText('cctg', 'hi');
  await adapter.sendTerminalKey('cctg', 'Up');
  const capture = await adapter.getTerminalCapture('cctg');
  assert.match(capture.content, /> hi\necho: hi\n\[Up\]/); assert.equal(capture.state, 'ready');
});

test('controller polls every 1.5s while started, backs off on errors and recovers', async () => {
  const clock = fakeClock(); let fail = false;
  const adapter = fakeAdapter({ capture: (session) => { if (fail) { const error = new Error('down'); error.status = 503; throw error; } return { content: `${session} ok\n`, state: '' }; } });
  const views = []; const controller = createTerminalController({ adapter, ...clock, onChange: (view) => views.push(view) });
  assert.deepEqual(clock.pending(), [], 'nothing polls before start');
  controller.start(); assert.deepEqual(clock.pending(), [0]);
  await clock.flush();
  assert.deepEqual(adapter.calls.at(-1), ['capture', 'cctg', 200]);
  assert.equal(controller.state.output, 'cctg ok\n'); assert.deepEqual(clock.pending(), [1500]);
  fail = true; await clock.flush();
  assert.equal(controller.state.error, 'HTTP 503'); assert.equal(controller.state.output, 'cctg ok\n', 'last good frame stays on screen');
  assert.deepEqual(clock.pending(), [1500]); await clock.flush(); assert.deepEqual(clock.pending(), [3000], 'exponential backoff');
  fail = false; await clock.flush(); assert.equal(controller.state.error, ''); assert.deepEqual(clock.pending(), [1500]);
  controller.stop(); assert.deepEqual(clock.pending(), [], 'stop cancels the loop');
  assert.ok(views.length > 3);
});

test('controller fences stale replies across a target switch and releases Kairos like Android', async () => {
  const clock = fakeClock(); let resolveCapture;
  const adapter = fakeAdapter({ capture: (session) => session === 'kairos' ? { content: 'kairos ready\n', state: 'ready' } : new Promise((resolve) => { resolveCapture = () => resolve({ content: 'LATE cctg\n', state: '' }); }) });
  const targets = []; const controller = createTerminalController({ adapter, ...clock, onTargetChange: (id) => targets.push(id) });
  controller.start(); await clock.flush(); // cctg capture now pending
  controller.select('kairos');
  resolveCapture(); await settle();
  assert.equal(controller.state.output, '', 'late cctg frame never paints the Kairos view');
  assert.equal(controller.state.mode, TERMINAL_MODE.CONNECTING);
  await clock.flush();
  assert.equal(controller.state.output, 'kairos ready\n'); assert.equal(controller.state.mode, TERMINAL_MODE.INTERACTIVE);
  controller.select('xiaoke'); await settle();
  assert.deepEqual(adapter.calls.filter(([kind]) => kind === 'release'), [['release', 'kairos']], 'leaving Kairos releases its console');
  controller.select('kairos'); await clock.flush();
  controller.stop(); await settle();
  assert.equal(adapter.calls.filter(([kind]) => kind === 'release').length, 2, 'stopping on Kairos releases too');
  assert.equal(controller.state.mode, TERMINAL_MODE.CONNECTING, 'fail closed until the next capture revalidates');
  assert.deepEqual(targets, ['kairos', 'xiaoke', 'kairos']);
});

test('controller sends text/keys only when writable, refreshes after, and maps 423 to read-only', async () => {
  const clock = fakeClock(); let reject423 = false;
  const adapter = fakeAdapter({
    capture: (session) => ({ content: `${session}\n`, state: 'ready' }),
    send: () => { if (reject423) { const error = new Error('locked'); error.status = 423; throw error; } return { ok: true }; },
  });
  const controller = createTerminalController({ adapter, ...clock, initialTarget: 'kairos' });
  assert.equal(await controller.sendText('too early'), false, 'Kairos is read-only until a ready capture');
  controller.start(); await clock.flush();
  const pending = controller.sendText('hello kairos'); await settle();
  assert.equal(controller.state.sending, true);
  await clock.flush(); // 500ms settle → re-capture
  assert.equal(await pending, true);
  assert.deepEqual(adapter.calls.filter(([kind]) => kind === 'send'), [['send', 'kairos', 'hello kairos']]);
  assert.equal(controller.state.sending, false);
  assert.equal(await controller.sendText('   '), false, 'blank input never reaches tmux');
  assert.equal(await controller.sendKey('M-Tab'), false, 'non-whitelisted keys are dropped client-side');
  reject423 = true;
  assert.equal(await controller.sendText('during reply'), false);
  assert.equal(controller.state.mode, TERMINAL_MODE.READ_ONLY); assert.equal(controller.state.error, '');
  controller.select('xiaoke');
  const key = controller.sendKey('Up'); await settle(); await clock.flush(); assert.equal(await key, true);
  assert.deepEqual(adapter.calls.filter(([kind]) => kind === 'key'), [['key', 'cctg', 'Up']]);
});

test('viewport resize is debounced, deduped and only for interactive Kimi/Kiro', async () => {
  const clock = fakeClock();
  const adapter = fakeAdapter({ capture: (session) => ({ content: `${session}\n`, state: 'ready' }) });
  const controller = createTerminalController({ adapter, ...clock });
  controller.start(); await clock.flush();
  controller.reportViewport(120, 40); await clock.flush();
  assert.equal(adapter.calls.filter(([kind]) => kind === 'resize').length, 0, 'never resize cctg');
  controller.select('kimi');
  controller.reportViewport(120, 40); // still CONNECTING: remembered, not sent
  await clock.flush(); // first capture → interactive → replays pending grid
  await clock.flush(); // 250ms debounce
  controller.reportViewport(120, 40); await clock.flush();
  assert.deepEqual(adapter.calls.filter(([kind]) => kind === 'resize'), [['resize', 'kimi', 120, 40]]);
  controller.reportViewport(5, 9999); await clock.flush(); await clock.flush();
  assert.deepEqual(adapter.calls.filter(([kind]) => kind === 'resize').at(-1), ['resize', 'kimi', 20, 160]);
});

test('terminal tab source contracts: markup, lifecycle, IME, focus and precache', () => {
  const html = read('../index.html'); const app = read('../src/app.js'); const css = read('../src/styles.css'); const sw = read('../sw.js');
  for (const id of ['terminal-targets', 'terminal-output', 'terminal-keybar', 'terminal-form', 'terminal-input', 'terminal-send', 'terminal-suggestions', 'terminal-banner', 'terminal-error', 'terminal-wrap', 'terminal-refresh', 'terminal-measure']) assert.match(html, new RegExp(`id="${id}"`), id);
  assert.match(html, /class="page-view terminal-view" id="terminal-page"/);
  assert.doesNotMatch(html, /READ-ONLY OBSERVER|terminal-page-list/, 'the old observer cards are gone');
  assert.match(app, /if \(view === 'terminal'\) startTerminalPolling\(\); else stopTerminalPolling\(\);/);
  assert.match(app, /if \(state\.view === 'terminal'\) \{ if \(document\.visibilityState === 'hidden'\) stopTerminalPolling\(\); else startTerminalPolling\(\); \}/, 'hidden page stops polling (LifecycleResumeEffect)');
  assert.match(app, /if \(event\.isComposing \|\| event\.keyCode === 229\) return;/, 'Chinese IME Enter never sends');
  assert.match(app, /terminal\.clear\(\);/, 'logout forgets the pane');
  assert.doesNotMatch(app, /terminalOutput\.innerHTML|insertAdjacentHTML/, 'terminal text is textContent only');
  assert.match(css, /\.page-view\.terminal-view\{--term-green:#39d353;/);
  assert.match(sw, /'\.\/src\/terminal\.js\?v=34'/);
  assert.match(sw, /stickers\|tmux\|terminal\)/, 'terminal routes are never served from Cache Storage');
});
