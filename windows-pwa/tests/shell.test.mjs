import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createHttpAdapter, createMockAdapter, normalizeBoardMessage, normalizeMemoryItem, normalizePlugin } from '../src/api.js';
import { buildMonthGrid, monthKey, shiftMonth, todayKey } from '../src/memory-calendar.js';
import { clockText, greetingFor, todoFeed, weekStrip } from '../src/home-dashboard.js';

test('month keys and shifting follow the YYYY-MM contract across year edges', () => {
  assert.match(monthKey(new Date(2026, 9, 17)), /^2026-10$/);
  assert.match(todayKey(new Date(2026, 9, 7)), /^2026-10-07$/);
  assert.equal(shiftMonth('2026-10', 1), '2026-11');
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(shiftMonth('2026-12', 2), '2027-02');
  assert.match(shiftMonth('not-a-month', 1), /^\d{4}-(0[1-9]|1[0-2])$/, 'bad input falls back to the current month');
});

test('month grids start on Monday and pad with marked out-of-month cells', () => {
  const grid = buildMonthGrid('2026-10'); // 2026-10-01 is a Thursday.
  assert.equal(grid.weeks[0][0].date, '2026-09-28', 'the first cell is the Monday of the first week');
  assert.equal(grid.weeks[0][0].inMonth, false);
  assert.equal(grid.weeks[0][3].date, '2026-10-01');
  assert.equal(grid.weeks[0][3].inMonth, true);
  assert.equal(grid.weeks.flat().filter((cell) => cell.inMonth).length, 31);
  assert.ok(grid.weeks.length >= 4 && grid.weeks.length <= 6);
  assert.equal(grid.weeks.at(-1).length, 7);
  assert.equal(buildMonthGrid('bogus').weeks.length, 0, 'invalid keys render nothing rather than a wrong month');
});

test('memory item normalization bounds fields and accepts upstream aliases', () => {
  const item = normalizeMemoryItem({ id: 'm1', display_markdown: '**正文**', category: 'diary', subcategory: 'diary.worklog', tags: ['a', 'b'], created_at: '2026-10-16T11:20:00+00:00', similarity: 0.8333 });
  assert.deepEqual(item, { id: 'm1', content: '**正文**', category: 'diary', subcategory: 'diary.worklog', tags: ['a', 'b'], createdAt: '2026-10-16T11:20:00+00:00', similarity: 0.833 });
  assert.equal(normalizeMemoryItem({}), null);
  assert.equal(normalizeMemoryItem('nope'), null);
  assert.equal(normalizeBoardMessage({ id: 'b1', from: '小克', content: '留言', created_at: '2026-10-17T02:12:00+00:00' }).createdAt, '2026-10-17T02:12:00+00:00');
  assert.equal(normalizeBoardMessage({ id: 'b2' }), null);
});

test('plugin ids must be flat slugs before they become URL path segments', () => {
  assert.equal(normalizePlugin({ id: 'xiaonan-dash', name: '小南仪表盘', version: '1.4.2', builtin: true }).id, 'xiaonan-dash');
  for (const bad of ['../etc', 'a/b', 'UPPER', '', 'a b', 'x'.repeat(70)]) {
    assert.equal(normalizePlugin({ id: bad }), null, bad);
  }
});

test('memory and plugin adapters build only allowlisted GET query strings', async () => {
  const calls = [];
  const adapter = createHttpAdapter({ request: async (path) => { calls.push(path); return { ok: true }; } });
  await adapter.getMemoryStats();
  await adapter.getMemoryCalendar('2026-10');
  await adapter.searchMemories('壁纸', { limit: 20 });
  await adapter.searchMemories('壁纸', { limit: 9999 });
  await adapter.getMemoryBoard({ page: 2, perPage: 20 });
  await adapter.listPlugins();
  assert.equal(calls[0], '/memory/stats');
  assert.equal(calls[1], '/memory/calendar?month=2026-10');
  assert.equal(calls[2], `/memory/semantic-search?query=${encodeURIComponent('壁纸')}&limit=20`);
  assert.equal(calls[3], `/memory/semantic-search?query=${encodeURIComponent('壁纸')}&limit=200`, 'limit is capped at the server contract');
  assert.equal(calls[4], '/memory/board?page=2&per_page=20');
  assert.equal(calls[5], '/plugins');
  await assert.rejects(() => adapter.getMemoryCalendar('2026-13'), /YYYY-MM/);
  await assert.rejects(() => adapter.getMemoryCalendar('2026-10-01'), /YYYY-MM/);
});

test('calendar accepts both the wrapped days shape and the bare date map', async () => {
  const wrapped = createHttpAdapter({ request: async () => ({ days: { '2026-10-09': { entries: [{ id: 'a', content: '一条' }], mood: 'happy' }, 'bad-date': { entries: [{ id: 'x', content: '丢' }] } } }) });
  assert.deepEqual(Object.keys(await wrapped.getMemoryCalendar('2026-10').then(({ days }) => days)), ['2026-10-09']);
  const bare = createHttpAdapter({ request: async () => ({ '2026-10-02': { entries: [{ id: 'b', content: '裸表' }] } }) });
  const { days } = await bare.getMemoryCalendar('2026-10');
  assert.equal(days['2026-10-02'].entries[0].content, '裸表');
  const arraySearch = createHttpAdapter({ request: async () => [{ id: 's1', content: '搜索结果', similarity: 0.9 }] });
  assert.equal((await arraySearch.searchMemories('搜索'))[0].id, 's1');
  const bareBoard = createHttpAdapter({ request: async () => [{ id: 'm1', from: '小克', content: '裸数组留言' }] });
  const board = await bareBoard.getMemoryBoard({ page: 3 });
  assert.equal(board.messages[0].content, '裸数组留言');
  assert.deepEqual(board.pagination, { page: 3, totalPages: 1 });
});

test('mock adapter serves memory stats, calendar, board, search, and plugins', async () => {
  const adapter = createMockAdapter();
  const stats = await adapter.getMemoryStats();
  assert.ok(stats.total > 0 && Object.keys(stats.byCategory).length > 0);
  const { days } = await adapter.getMemoryCalendar('2026-10');
  assert.deepEqual(Object.keys(days), ['2026-10-02', '2026-10-09', '2026-10-16']);
  assert.ok((await adapter.searchMemories('壁纸')).length > 0);
  const board = await adapter.getMemoryBoard({ page: 1 });
  assert.ok(board.messages.length > 0 && board.pagination.totalPages >= 1);
  const plugins = await adapter.listPlugins();
  assert.ok(plugins.some((plugin) => plugin.id === 'xiaonan-dash'));
  await assert.rejects(() => adapter.getMemoryCalendar('2026-1'), /YYYY-MM/);
});

test('Android-shell source contracts: four tabs, full pages, view switching, mock-only deep links', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sw = readFileSync(new URL('../sw.js', import.meta.url), 'utf8');
  for (const view of ['chat', 'terminal', 'apps', 'settings']) {
    assert.ok((html.match(new RegExp(`class="tab-item[^"]*" data-view="${view}"`, 'g')) || []).length === 2, `${view} appears in both the rail tabs and the bottom tab bar`);
  }
  for (const id of ['chat-list-page', 'terminal-page', 'apps-page', 'settings-page', 'memory-page']) {
    assert.match(html, new RegExp(`class="page-view[^"]*" id="${id}"`), id);
  }
  assert.match(html, /id="chat-back-button"[^>]*hidden/);
  assert.match(html, /id="memory-search"[^>]*placeholder="语义搜索记忆（回车检索）"/);
  assert.match(html, /id="memory-tab-memories"[^>]*aria-selected="true"/);
  assert.match(html, /id="calendar-prev"/);
  assert.match(html, /id="calendar-today"[^>]*>回到今天/);
  assert.match(html, /id="board-prev"/);
  assert.match(html, /id="settings-version"/);
  assert.match(html, /symbol id="icon-bell-off"/);
  assert.doesNotMatch(html, /🔔|🔕/, 'the notification button uses SVG icons, not emoji');
  assert.match(app, /const VIEW_IDS = \{ chat: 'conversation', terminal: 'terminal-page', apps: 'apps-page', settings: 'settings-page', memory: 'memory-page' \}/);
  assert.match(app, /function switchView\(view\)/);
  assert.match(app, /function syncView\(\)/);
  assert.match(app, /startTerminalPolling\(\); else stopTerminalPolling\(\)/, 'terminal polling runs only while the terminal view is active');
  assert.match(app, /setInterval\(\(\) => \{ void pollTerminalPage\(\); \}, 4_000\)/);
  assert.match(app, /adapter\.getMemoryStats\(\)/);
  assert.match(app, /adapter\.getMemoryCalendar\(month\)/);
  assert.match(app, /adapter\.searchMemories\(query, \{ limit: 20 \}\)/);
  assert.match(app, /adapter\.getMemoryBoard\(\{ page, perPage: 20 \}\)/);
  assert.match(app, /adapter\.listPlugins\(\)/);
  assert.match(app, /WEB_SHELL_VERSION = 'desk-v23 · 2026-10-17'/);
  const mockHook = app.slice(app.indexOf('if (bootstrap.mock)'));
  assert.match(mockHook, /params\.get\('view'\)/, 'the deep-link hook stays mock-gated');
  assert.match(css, /\.tab-bar\{[^}]*display:none/, 'the bottom tab bar is phone-only');
  assert.match(css, /@media \(max-width:767px\)\{[^]*?\.tab-bar\{display:grid\}/);
  assert.match(css, /\.tab-bar \.tab-item\.is-active::before\{[^}]*height:2px/, 'active tab keeps the Android-style top indicator');
  assert.match(css, /\.page-view\{grid-column:2 \/ -1/, 'pages span the conversation + side columns on desktop');
  assert.match(css, /\.memory-segment button\[aria-selected='true'\]\{background:linear-gradient/);
  assert.match(css, /\.calendar-day\.has-entries\{background:linear-gradient/);
  assert.match(css, /\.calendar-weekdays,\.calendar-grid\{display:grid;grid-template-columns:repeat\(7,1fr\)/);
  assert.match(css, /\.side-notes\.view-hidden\{display:none\}/);
  assert.match(sw, /src\/memory-calendar\.js\?v=23/);
});

test('home dashboard helpers: deterministic greeting, clock text, Monday-first week strip', () => {
  const saturday = new Date(2026, 9, 17, 14, 15); // 2026-10-17 is a Saturday.
  const greeting = greetingFor(saturday);
  assert.equal(greeting.eyebrow, 'SAT · 10/17');
  assert.ok(greeting.title.length > 0 && greeting.subtitle.length > 0);
  assert.deepEqual(greetingFor(saturday), greeting, 'the same moment always yields the same copy');
  for (const hour of [0, 5, 9, 12, 15, 19, 23]) assert.ok(greetingFor(new Date(2026, 9, 17, hour)).title.length > 0, `hour ${hour}`);
  const clock = clockText(saturday);
  assert.equal(clock.time, '14:15');
  assert.equal(clock.date, '10月17日 · 星期六');
  const week = weekStrip(saturday);
  assert.equal(week.title, '2026年10月');
  assert.equal(week.days.length, 7);
  assert.equal(week.days[0].date, '2026-10-12', 'the strip starts on the Monday of the current week');
  assert.equal(week.days[5].date, '2026-10-17');
  assert.ok(week.days[5].isToday && week.days.filter((day) => day.isToday).length === 1);
});

test('greeting selection is a literal Kotlin translation: pinned cross-platform vectors', () => {
  // Expected titles computed from WelcomeGreeting.kt selectWelcomeGreeting()
  // (seed "${year}:${dayOfYear}:${isoDayOfWeek}:${PERIOD_NAME}" folded from 17
  // with Int overflow, index = floorMod(seed, copies.size)).
  const vectors = [
    [new Date(2026, 9, 17, 2), '凌晨也算今天'], // 2026:290:6:LATE_NIGHT
    [new Date(2026, 9, 17, 6), '早呀，第一束光'], // 2026:290:6:EARLY_MORNING
    [new Date(2026, 9, 17, 9), 'Good morning, sunshine'], // 2026:290:6:MORNING
    [new Date(2026, 9, 17, 12), '中午暂停一下'], // 2026:290:6:MIDDAY
    [new Date(2026, 9, 17, 15), '三時のおしゃべり？'], // 2026:290:6:AFTERNOON
    [new Date(2026, 9, 17, 20), '夜の時間、はじめよう'], // 2026:290:6:EVENING
    [new Date(2026, 9, 18, 8), 'Good morning, sunshine'], // Sunday ISO=7: 2026:291:7:MORNING
    [new Date(2026, 0, 5, 10), 'Morning has entered the chat'], // 2026:5:1:MORNING
    [new Date(2026, 5, 30, 18), '今日营业即将收尾'], // 2026:181:2:EVENING
    [new Date(2026, 2, 9, 23), '凌晨也算今天'], // 2026:68:1:LATE_NIGHT
  ];
  for (const [moment, title] of vectors) assert.equal(greetingFor(moment).title, title, moment.toString());
  // Special days bypass the period library on both platforms.
  assert.equal(greetingFor(new Date(2026, 0, 1, 10)).title, '新年第一声你好');
  assert.equal(greetingFor(new Date(2026, 11, 31, 23)).title, '今年辛苦了');
  // The eyebrow zero-pads like Android ("01/05", not "1/5").
  assert.equal(greetingFor(new Date(2026, 0, 5, 10)).eyebrow, 'MON · 01/05');
  // The hour of the moment must never leak into dayOfYear (date-only math).
  assert.equal(greetingFor(new Date(2026, 9, 17, 12)).title, greetingFor(new Date(2026, 9, 17, 13)).title);
});

test('visual parity pass 2: dashboard cards, sans-only typography, transparent glass, circular send key', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sw = readFileSync(new URL('../sw.js', import.meta.url), 'utf8');
  for (const id of ['dash-clock-time', 'dash-clock-date', 'dash-quote-eyebrow', 'dash-quote-title', 'dash-quote-sub', 'dash-week-title', 'dash-week-days', 'dash-todo-card', 'dash-full-calendar', 'contact-avatar', 'contact-status']) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.match(app, /from '\.\/home-dashboard\.js\?v=23'/);
  assert.match(app, /function renderDashboard\(\)/);
  assert.match(app, /\$\('#dash-full-calendar'\)\.addEventListener\('click', openMemory\)/);
  assert.match(sw, /src\/home-dashboard\.js\?v=23/);
  assert.doesNotMatch(css, /--serif/, 'the shell drops serif typography entirely');
  assert.match(css, /\.eyebrow\{[^}]*font:500 10px var\(--sans\);letter-spacing:\.07em/);
  assert.match(css, /--glass:rgba\(24,17,12,\.42\)/);
  assert.match(css, /backdrop-filter:blur\(26px\) saturate\(1\.3\)/);
  assert.match(css, /\.dash-week-day\.is-today\{background:linear-gradient/);
  assert.match(css, /\.chat-card-face\{position:relative/);
  assert.match(css, /@media \(max-width:767px\)\{[^]*?\.send-button\{min-width:48px;width:48px;height:48px;padding:0;justify-content:center;border-radius:50%\}/);
  assert.match(css, /@media \(min-width:768px\)\{[^]*?\.dash-stack\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/, 'tablet/desktop dashboard uses a two-column card grid');
  assert.match(css, /@media \(min-width:768px\)\{[^]*?\.dash-contacts-card\{grid-column:1 \/ -1\}/);
  assert.match(app, /const homeQuery = window\.matchMedia\('\(max-width:1199px\)'\)/, 'phones and tablets land on the dashboard');
  assert.match(app, /function chatSurface\(\) \{ return homeQuery\.matches/);
  assert.match(app, /if \(homeQuery\.matches && !state\.mobileChatOpen\) \{ state\.mobileChatOpen = true; syncView\(\); \}/, 'picking a contact on phone/tablet opens the conversation');
});

test('todoFeed is a literal homeTodoFeed() port: schedule-only, today-only, done/time/text order', () => {
  const saturday = new Date(2026, 9, 17, 14, 15); // 2026-10-17, Saturday.
  const sections = [
    { section: '日程', source: 'schedule', items: [
      { text: '已完成的事', done: true, time: '08:00', dueDate: '2026-10-17', eventId: 'e1' },
      { text: '下午的事', done: false, time: '14:00', dueDate: '2026-10-17', eventId: 'e2' },
      { text: '全天的事', done: false, time: '', dueDate: '2026-10-17', eventId: 'e3' },
      { text: '明天的事', done: false, time: '09:00', dueDate: '2026-10-18', eventId: 'e4' },
      { text: '上午的事', done: false, time: '09:30', dueDate: '2026-10-17', eventId: 'e5' },
    ] },
    { section: '随手记', source: 'note', items: [
      { text: '非日程来源不进主页', done: false, time: '10:00', dueDate: '2026-10-17', eventId: '' },
    ] },
  ];
  const feed = todoFeed(sections, saturday);
  assert.equal(feed.title, '今日待办 · 1/4 已完成');
  assert.equal(feed.total, 4);
  assert.equal(feed.doneCount, 4 - 3);
  assert.deepEqual(feed.items.map((item) => item.text), ['上午的事', '下午的事', '全天的事', '已完成的事'], 'undone first, time ascending, all-day (99:99) last, done tail');
  assert.equal(feed.items[0].dueLabel, '今天 周六 · 09:30');
  assert.equal(feed.items[2].dueLabel, '今天 周六 · 全天');
  const empty = todoFeed([], saturday);
  assert.equal(empty.title, '今日待办');
  assert.equal(empty.total, 0);
  assert.deepEqual(todoFeed(sections, new Date(2026, 9, 18)).items.map((item) => item.text), ['明天的事'], 'switching the day never borrows another day\'s tasks');
});

test('dashboard real-data wiring: weather chip/line, todo list, icon-keyed head actions, orange status', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  for (const id of ['dash-weather-place', 'dash-weather', 'dash-todo-summary', 'dash-todo-list']) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.match(html, /id="dash-weather-place"[^>]*hidden/, 'the city chip starts hidden until weather arrives');
  for (const symbol of ['icon-panel', 'icon-atlas']) assert.match(html, new RegExp(`symbol id="${symbol}"`), symbol);
  assert.match(html, /id="worker-toggle"[^>]*aria-controls="side-notes"[^>]*aria-expanded="false"[^>]*><svg class="icon"[^>]*><use href="#icon-panel"/, 'worker toggle is an icon button but keeps its drawer contract');
  assert.match(html, /id="memory-button"[^>]*><svg class="icon"[^>]*><use href="#icon-atlas"/);
  assert.doesNotMatch(html, /现场 <span aria-hidden="true">↗/, 'text buttons with arrows are gone from the head');
  assert.match(app, /import \{ clockText, greetingFor, todoFeed, weekStrip \} from '\.\/home-dashboard\.js\?v=23'/);
  assert.match(app, /state\.dash = \{ appearance: \{ nicknames: \{\}, avatars: \{\} \}, todos: null, weather: null, weatherFailed: false \}/, 'logout resets the dashboard channel');
  assert.match(app, /adapter\.getTodos\(\)/);
  assert.match(app, /adapter\.getDashAppearance\(\)/);
  assert.match(app, /adapter\.getWeather\(\{ city: HOME_DEFAULT_CITY \}\)/);
  assert.match(app, /const HOME_DEFAULT_CITY = '北京'/);
  assert.match(app, /setInterval\(\(\) => void loadDashData\(\), DASH_DATA_INTERVAL_MS\)/);
  assert.match(app, /const DASH_DATA_INTERVAL_MS = 15 \* 60_000/);
  assert.match(app, /state\.dash\.appearance\.nicknames\[item\?\.id\] \|\| item\?\.name/, 'server nickname overrides the contacts name');
  assert.match(css, /\.dash-place-chip\{position:absolute;top:14px;right:14px/);
  assert.match(css, /\.dash-todo-item\.is-done \.dash-todo-text\{color:var\(--dim\);text-decoration:line-through\}/);
  assert.match(css, /\.avatar-img\{display:block;width:100%;height:100%;border-radius:50%;object-fit:cover\}/);
  assert.match(css, /#contact-status\{color:var\(--oxide\)\}/, 'the header status line is oxide orange');
  assert.match(css, /\.chat-list\{display:flex;flex-wrap:nowrap;gap:8px;margin-top:13px;overflow-x:auto/, 'the avatar row scrolls sideways instead of clipping');
  assert.match(css, /\.chat-list \.chat-card\{display:grid;flex:0 0 auto/, 'cards keep their width while scrolling');
});
