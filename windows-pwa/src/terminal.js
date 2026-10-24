/**
 * Interactive terminal tab — a literal port of the Android TerminalScreen /
 * TerminalViewModel / TerminalInteractionPolicy / TerminalKeybar contract
 * (2026-10-24).  The server protocol is HTTP polling, not a stream:
 *   GET  /tmux/capture?session=…&lines=…  → {content, state}   (every 1.5s)
 *   POST /tmux/send       {session, keys}   text + Enter (paste-buffer)
 *   POST /terminal/key    {session, key}    one whitelisted tmux key name
 *   POST /terminal/resize {session, columns, rows}  Kimi/Kiro only
 *   POST /terminal/release {target:'kairos'}        leaving the Kairos console
 * The browser authenticates with the same-origin cookie (+CSRF on writes);
 * the server only lets that cookie reach the four fixed targets below.
 */

export const TERMINAL_TARGETS = Object.freeze([
  Object.freeze({ id: 'kairos', session: 'kairos', label: 'Kairos' }),
  Object.freeze({ id: 'xiaoke', session: 'cctg', label: '小克' }),
  Object.freeze({ id: 'kimi', session: 'kimi', label: 'Kimi' }),
  Object.freeze({ id: 'kiro', session: 'kiro', label: 'Kiro' }),
]);
export const DEFAULT_TERMINAL_TARGET = 'xiaoke';
export const TERMINAL_POLL_MS = 1_500;
export const TERMINAL_CAPTURE_LINES = 200;
const TERMINAL_MAX_BACKOFF_MS = 8_000;
const TERMINAL_CONTENT_LIMIT = 512 * 1024;

export function terminalTarget(id) {
  return TERMINAL_TARGETS.find((target) => target.id === id) || TERMINAL_TARGETS.find((target) => target.id === DEFAULT_TERMINAL_TARGET);
}
export function isTerminalTargetId(id) { return TERMINAL_TARGETS.some((target) => target.id === id); }

export const TERMINAL_MODE = Object.freeze({ CONNECTING: 'connecting', READ_ONLY: 'read-only', INTERACTIVE: 'interactive' });
const LEASED_TARGETS = new Set(['kairos', 'kimi', 'kiro']);

/** TerminalInteractionPolicy.initialMode */
export function initialMode(id) { return LEASED_TARGETS.has(id) ? TERMINAL_MODE.CONNECTING : TERMINAL_MODE.INTERACTIVE; }
/** TerminalInteractionPolicy.modeAfterCapture */
export function modeAfterCapture(id, serverState) {
  if (!LEASED_TARGETS.has(id)) return TERMINAL_MODE.INTERACTIVE;
  const value = String(serverState || '').trim().toLowerCase();
  if (value === 'ready') return TERMINAL_MODE.INTERACTIVE;
  // Kimi's `waiting` capture is its real TUI (input queues); Kairos/Kiro's
  // `waiting` is a prompt-free observer: read-only until the turn ends.
  if (value === 'waiting') return id === 'kimi' ? TERMINAL_MODE.INTERACTIVE : TERMINAL_MODE.READ_ONLY;
  return TERMINAL_MODE.CONNECTING;
}
/** TerminalInteractionPolicy.canSend — XiaoKe's cctg is always writable. */
export function canSend(id, mode) { return id === 'xiaoke' ? true : mode === TERMINAL_MODE.INTERACTIVE; }
/** TerminalInteractionPolicy.canResize — never resize XiaoKe's shared cctg window. */
export function canResize(id, mode) { return (id === 'kimi' || id === 'kiro') && mode === TERMINAL_MODE.INTERACTIVE; }
/** TerminalInteractionPolicy.modeAfterWaitingInputRejected (HTTP 423). */
export function modeAfterWaitingInputRejected(id) { return id === 'kairos' || id === 'kiro' ? TERMINAL_MODE.READ_ONLY : TERMINAL_MODE.INTERACTIVE; }

/** TerminalCaptureProtocol: missing/blank/unknown states stay unknown (fail closed). */
export function normalizeCaptureState(raw) {
  const value = String(raw ?? '').trim().toLowerCase();
  return value === 'ready' || value === 'waiting' ? value : '';
}
export function normalizeTerminalCapture(raw) {
  if (typeof raw === 'string') return { content: raw.replace(/\r/g, '').slice(-TERMINAL_CONTENT_LIMIT), state: '' };
  if (!raw || typeof raw !== 'object') return { content: '', state: '' };
  const content = typeof raw.content === 'string' ? raw.content.replace(/\r/g, '').slice(-TERMINAL_CONTENT_LIMIT) : '';
  return { content, state: normalizeCaptureState(raw.state) };
}

/** Server /terminal/key whitelist (push.py _handle_terminal_key_transaction). */
export const SERVER_TERMINAL_KEYS = new Set([
  'Escape', 'Tab', 'Enter', 'Space', 'BSpace', 'Up', 'Down', 'Left', 'Right',
  'Home', 'End', 'PageUp', 'PageDown', 'DC',
  'C-c', 'C-d', 'C-z', 'C-a', 'C-e', 'C-k', 'C-u', 'C-l', 'C-r', 'C-w', 'C-b', 'C-f', 'C-n', 'C-p',
  'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
]);

/**
 * TerminalKeybar keys.  ALT is omitted on purpose: the server whitelist has no
 * `M-…` names, so on Android it only ever produced a 400.  CTRL stays sticky
 * and also applies to the next letter typed in the input (C-a … C-w).
 */
export const TERMINAL_KEYBAR = Object.freeze([
  { label: 'ESC', key: 'Escape', title: 'Escape（Claude Code 里会打断当前回复）' },
  { label: 'CTRL', modifier: 'ctrl', title: 'Ctrl（粘滞：作用于下一个键或下一个字母）' },
  { label: 'TAB', key: 'Tab', title: 'Tab' },
  { label: 'ENTER', key: 'Enter', title: 'Enter' },
  { label: '⌫', key: 'BSpace', longPress: 'C-u', title: '退格（长按 = 清空整行 C-u）' },
  { label: '^C', key: 'C-c', title: 'Ctrl+C 中断' },
  { label: 'CLR', key: 'C-u', title: '清空远端输入行（C-u）' },
  { label: '↑', key: 'Up', title: '上' },
  { label: '↓', key: 'Down', title: '下' },
  { label: '←', key: 'Left', title: '左' },
  { label: '→', key: 'Right', title: '右' },
]);

/** Keybar key + sticky CTRL → tmux key name, or '' when the server would reject it. */
export function resolveKeybarKey(key, { ctrl = false } = {}) {
  const name = String(key || '');
  const resolved = name.includes('-') || !ctrl ? name : `C-${name}`;
  return SERVER_TERMINAL_KEYS.has(resolved) ? resolved : '';
}
/** Sticky CTRL + a typed letter → `C-x` when whitelisted. */
export function ctrlLetterKey(letter) {
  const value = String(letter || '');
  if (!/^[a-zA-Z]$/.test(value)) return '';
  const key = `C-${value.toLowerCase()}`;
  return SERVER_TERMINAL_KEYS.has(key) ? key : '';
}
/**
 * Desktop convenience: with an EMPTY input, navigation keys go straight to the
 * remote TUI (menus, history).  Escape / Ctrl+C are deliberately NOT mapped —
 * they would interrupt a running turn; use the ESC / ^C buttons for that.
 */
export function passthroughKey({ key, shiftKey = false, ctrlKey = false, altKey = false, metaKey = false } = {}) {
  if (shiftKey || ctrlKey || altKey || metaKey) return '';
  return ({ ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right', Backspace: 'BSpace', Tab: 'Tab', PageUp: 'PageUp', PageDown: 'PageDown' })[key] || '';
}

/** Visible character grid, clamped exactly like the server (20–320 × 8–160). */
export function terminalGrid({ width, height, cellWidth, cellHeight } = {}) {
  if (!(width > 0 && height > 0 && cellWidth > 0 && cellHeight > 0)) return null;
  const columns = Math.min(320, Math.max(20, Math.floor(width / cellWidth)));
  const rows = Math.min(160, Math.max(8, Math.floor(height / cellHeight)));
  return { columns, rows };
}

/** TerminalScreen slash-command autocomplete, per target. */
export const TERMINAL_SLASH_COMMANDS = Object.freeze({
  kairos: [['/status', '会话与上下文状态'], ['/model', '切换模型'], ['/compact', '压缩上下文'], ['/review', '开始代码审查']],
  kimi: [['/status', '查看 Kimi Code 会话状态']],
  kiro: [['/help', '查看 Kiro 命令'], ['/context show', '上下文用量'], ['/compact', '压缩上下文'], ['/usage', '额度与用量'], ['/model', '切换模型（仅本终端会话）'], ['/tools', '可用工具'], ['/stats', '本轮请求耗时'], ['/session-id', '当前会话 ID']],
  xiaoke: [['/statusbar', '模型/上下文/额度'], ['/model', '切换模型'], ['/sessions', '列出session'], ['/use', '切换session'], ['/new', '新建session'], ['/compact', '压缩上下文'], ['/resume', '恢复session'], ['/esc', '发送Escape'], ['/screenshot', '截取终端'], ['/vps', 'VPS运行状况'], ['/morning_on', '开启早安'], ['/morning_off', '关闭早安'], ['/diary_on', '开启日记提醒'], ['/diary_off', '关闭日记提醒'], ['/forge', '无损滑窗重启'], ['/stop', '停止当前任务']],
});
export function slashSuggestions(id, text, enabled = true) {
  const value = String(text || '');
  if (!enabled || !value.startsWith('/') || value.length >= 20) return [];
  const needle = value.toLowerCase(); const exact = value.trim().toLowerCase();
  return (TERMINAL_SLASH_COMMANDS[id] || []).filter(([command]) => command.toLowerCase().startsWith(needle) && command.toLowerCase() !== exact);
}

/** Placeholder lines shown before the first capture (TerminalScreen). */
export function emptyTerminalText(id, mode) {
  if (id === 'kimi') return '正在连接 Kimi 当前终端…';
  if (id === 'kiro') return mode === TERMINAL_MODE.READ_ONLY ? 'Kiro 正在回复；终端当前只读。' : '正在连接 Kiro 当前终端…';
  if (id === 'kairos') return mode === TERMINAL_MODE.READ_ONLY ? 'Kairos 正在回复；终端当前只读。' : '正在连接 Kairos 终端…';
  return '$ waiting for output...';
}
export function inputPlaceholder(id, mode) {
  const label = terminalTarget(id).label;
  if (id !== 'xiaoke' && mode === TERMINAL_MODE.READ_ONLY) return `${label} 正在回复，终端暂时只读`;
  if (id !== 'xiaoke' && mode === TERMINAL_MODE.CONNECTING) return id === 'kairos' ? '正在连接 Kairos 终端…' : `正在连接 ${label} 当前会话…`;
  return `发给 ${label} 或输入 /shortcut…`;
}
export function terminalStatusBanner(mode) {
  if (mode === TERMINAL_MODE.READ_ONLY) return ['正在回复 · 只读', '回复结束后自动恢复输入'];
  if (mode === TERMINAL_MODE.CONNECTING) return ['正在连接 · 只读', '正在安全接管当前会话，连接完成后即可输入'];
  return null;
}

function errorDetail(error) {
  const status = Number(error?.status || 0);
  if (status === 401) return '登录已过期，请刷新页面重新配对';
  if (status === 403) return '此浏览器会话没有终端权限';
  const serverText = typeof error?.payload?.error === 'string' ? error.payload.error.slice(0, 160) : '';
  if (status) return serverText ? `${serverText}（HTTP ${status}）` : `HTTP ${status}`;
  return '连接中断，正在重连…';
}
/** TerminalViewModel.terminalError */
export function terminalErrorText(id, error) {
  const detail = errorDetail(error);
  return id === 'xiaoke' ? detail : `${terminalTarget(id).label} 终端暂时不可用：${detail}`;
}

/**
 * TerminalViewModel port.  Every operation is serialized through one queue
 * (Android's terminalMutex) and fenced by (target, generation) so a late
 * reply can never paint one identity's pane while another is selected.
 */
export function createTerminalController({
  adapter, initialTarget = DEFAULT_TERMINAL_TARGET, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
  pollMs = TERMINAL_POLL_MS, lines = TERMINAL_CAPTURE_LINES, onChange = () => {}, onTargetChange = () => {},
} = {}) {
  const startTarget = isTerminalTargetId(initialTarget) ? initialTarget : DEFAULT_TERMINAL_TARGET;
  const view = { target: startTarget, mode: initialMode(startTarget), output: '', error: '', loading: false, sending: false, captured: false };
  let generation = 0; let pollingRequested = false; let pollTimer = null; let resizeTimer = null; let failures = 0;
  let lastGrid = { columns: 0, rows: 0 }; let pendingGrid = null;
  let queue = Promise.resolve();
  const emit = () => onChange({ ...view });
  const isCurrent = (target, gen) => view.target === target && generation === gen;
  const enqueue = (task) => { const run = queue.then(task, task); queue = run.catch(() => {}); return run; };
  const sleep = (ms) => new Promise((resolve) => setTimer(resolve, ms));
  const clearPoll = () => { if (pollTimer !== null) { clearTimer(pollTimer); pollTimer = null; } };
  const clearResize = () => { if (resizeTimer !== null) { clearTimer(resizeTimer); resizeTimer = null; } };

  async function fetchCapture(target, gen) {
    let result; let failure = null;
    await enqueue(async () => {
      if (!isCurrent(target, gen)) return;
      try { result = normalizeTerminalCapture(await adapter.getTerminalCapture(terminalTarget(target).session, { lines })); }
      catch (error) { failure = error; }
    });
    if (!isCurrent(target, gen)) return false;
    if (failure) {
      failures += 1;
      if (LEASED_TARGETS.has(target)) view.mode = TERMINAL_MODE.CONNECTING;
      view.error = terminalErrorText(target, failure); emit(); return false;
    }
    if (!result) return false;
    failures = 0;
    view.output = result.content; view.mode = modeAfterCapture(target, result.state); view.error = ''; view.captured = true;
    emit();
    if (pendingGrid && canResize(target, view.mode)) reportViewport(pendingGrid.columns, pendingGrid.rows);
    return true;
  }
  function schedulePoll(target, gen, delay) {
    // A stale loop (old target/generation) must not cancel the current one.
    if (!pollingRequested || !isCurrent(target, gen)) return;
    clearPoll();
    pollTimer = setTimer(async () => {
      pollTimer = null;
      if (!pollingRequested || !isCurrent(target, gen)) return;
      await fetchCapture(target, gen);
      const backoff = failures ? Math.min(TERMINAL_MAX_BACKOFF_MS, pollMs * 2 ** Math.min(failures - 1, 3)) : pollMs;
      schedulePoll(target, gen, backoff);
    }, delay);
  }
  function releaseKairosWhenStale(gen, { requireStopped }) {
    void enqueue(async () => {
      if (requireStopped ? (pollingRequested || generation !== gen) : generation !== gen) return;
      try { await adapter.releaseTerminal('kairos'); } catch { /* idle reaper owns it */ }
    });
  }
  function handleWriteFailure(target, error) {
    if (LEASED_TARGETS.has(target) && Number(error?.status) === 423) { view.mode = modeAfterWaitingInputRejected(target); view.error = ''; }
    else view.error = terminalErrorText(target, error);
    emit();
  }
  async function write(kind, payload, settleMs) {
    const target = view.target; const gen = generation;
    if (!canSend(target, view.mode)) return false;
    let ok = false; let failure = null;
    if (kind === 'text') { view.sending = true; emit(); }
    try {
      await enqueue(async () => {
        if (!isCurrent(target, gen)) return;
        const session = terminalTarget(target).session;
        try { if (kind === 'text') await adapter.sendTerminalText(session, payload); else await adapter.sendTerminalKey(session, payload); ok = true; }
        catch (error) { failure = error; }
      });
      if (!isCurrent(target, gen)) return false;
      if (failure) { handleWriteFailure(target, failure); return false; }
      if (ok) { if (view.error) { view.error = ''; emit(); } await sleep(settleMs); if (isCurrent(target, gen)) await fetchCapture(target, gen); }
      return ok;
    } finally {
      if (kind === 'text' && isCurrent(target, gen)) { view.sending = false; emit(); }
    }
  }
  function reportViewport(columns, rows) {
    pendingGrid = { columns, rows };
    const target = view.target; const gen = generation;
    if (!pollingRequested || !canResize(target, view.mode)) return;
    const safeColumns = Math.min(320, Math.max(20, Math.floor(columns))); const safeRows = Math.min(160, Math.max(8, Math.floor(rows)));
    if (safeColumns === lastGrid.columns && safeRows === lastGrid.rows) return;
    lastGrid = { columns: safeColumns, rows: safeRows };
    clearResize();
    resizeTimer = setTimer(() => {
      resizeTimer = null;
      void enqueue(async () => {
        if (!isCurrent(target, gen) || !pollingRequested || !canResize(target, view.mode)) return;
        try { await adapter.resizeTerminal(terminalTarget(target).session, safeColumns, safeRows); } catch { /* visual nicety only */ }
      });
    }, 250);
  }

  return {
    get state() { return { ...view }; },
    start() {
      if (pollingRequested) return;
      pollingRequested = true; failures = 0;
      schedulePoll(view.target, generation, 0);
    },
    stop() {
      if (!pollingRequested) return;
      pollingRequested = false; clearPoll(); clearResize(); lastGrid = { columns: 0, rows: 0 };
      const target = view.target; const gen = ++generation;
      view.loading = false; view.sending = false;
      if (LEASED_TARGETS.has(target)) view.mode = TERMINAL_MODE.CONNECTING;
      if (target === 'kairos') releaseKairosWhenStale(gen, { requireStopped: true });
      emit();
    },
    select(id) {
      if (!isTerminalTargetId(id) || id === view.target) return;
      const previous = view.target;
      clearPoll(); clearResize(); lastGrid = { columns: 0, rows: 0 }; failures = 0;
      const gen = ++generation;
      Object.assign(view, { target: id, output: '', mode: initialMode(id), error: '', loading: false, sending: false, captured: false });
      onTargetChange(id); emit();
      if (previous === 'kairos') releaseKairosWhenStale(gen, { requireStopped: false });
      if (pollingRequested) schedulePoll(id, gen, 0);
    },
    async refresh() {
      const target = view.target; const gen = generation;
      view.loading = true; emit();
      try { await fetchCapture(target, gen); } finally { if (isCurrent(target, gen)) { view.loading = false; emit(); } }
      if (pollingRequested && isCurrent(target, gen)) schedulePoll(target, gen, pollMs);
    },
    sendText(text) { return String(text || '').trim() ? write('text', String(text), 500) : Promise.resolve(false); },
    sendKey(key) { return SERVER_TERMINAL_KEYS.has(key) ? write('key', key, 300) : Promise.resolve(false); },
    reportViewport,
    /** Logout: forget the visible pane entirely (fenced like a target switch). */
    clear() {
      this.stop(); clearPoll(); generation += 1; failures = 0; pendingGrid = null;
      Object.assign(view, { output: '', mode: initialMode(view.target), error: '', loading: false, sending: false, captured: false });
      emit();
    },
  };
}
