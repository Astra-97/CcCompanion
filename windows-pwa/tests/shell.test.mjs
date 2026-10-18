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
  assert.match(app, /WEB_SHELL_VERSION = 'desk-v28 · 2026-10-18'/);
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
  assert.match(sw, /src\/memory-calendar\.js\?v=28/);
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
  assert.match(app, /from '\.\/home-dashboard\.js\?v=28'/);
  assert.match(app, /function renderDashboard\(\)/);
  assert.match(app, /\$\('#dash-full-calendar'\)\.addEventListener\('click', openMemory\)/);
  assert.match(sw, /src\/home-dashboard\.js\?v=28/);
  assert.doesNotMatch(css, /--serif/, 'the shell drops serif typography entirely');
  assert.match(css, /\.eyebrow\{[^}]*font:500 10px var\(--sans\);letter-spacing:\.07em/);
  assert.match(css, /--glass:rgba\(24,17,12,\.42\)/);
  assert.match(css, /backdrop-filter:blur\(26px\) saturate\(1\.3\)/);
  assert.match(css, /\.dash-week-day\.is-today\{background:linear-gradient/);
  assert.match(css, /\.chat-card-face\{position:relative/);
  assert.match(css, /\.composer \.send-button\{display:grid;place-items:center;width:48px;height:48px;min-height:48px;border-radius:50%;padding:0;background:#d4725c;color:#2c1608/, 'the composer send key is the App 48px circle in the synced themeColor #D4725C at every shell size');
  assert.match(css, /\.stop-glyph\{display:block;width:14px;height:14px;border-radius:3px;background:currentColor\}/, 'busy swaps in the stop square on the same circle');
  assert.match(css, /\.composer\{[^}]*grid-template-columns:auto minmax\(0,1fr\) auto/, 'composer row is [+] · pill · send');
  assert.match(css, /\.composer-pill\{[^}]*border-radius:22px;background:rgba\(255,255,255,\.1\)/, 'the input pill mirrors ChatInputBarShell');
  assert.match(css, /\.composer-pill \.sticker-button\{[^}]*border:0/, 'the emoji key lives inside the pill');
  assert.match(css, /\.message\{position:relative;display:flex;min-width:0/, 'message rows are flex rows with 30px avatars; grid items may shrink below code-block min-content: no horizontal scrollbar under the message column');
  assert.match(css, /\*\{scrollbar-width:thin;scrollbar-color:rgba\(255,240,222,\.26\) transparent\}/, 'thin translucent scrollbars everywhere');
  assert.match(css, /@media \(min-width:768px\)\{[^]*?\.dash-stack\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/, 'tablet/desktop dashboard uses a two-column card grid');
  assert.match(css, /@media \(min-width:768px\)\{[^]*?\.dash-contacts-card\{grid-column:1 \/ -1\}/);
  assert.match(app, /const homeQuery = window\.matchMedia\('\(max-width:1199px\)'\)/, 'the narrow-shell media query still gates the drawer and back key');
  assert.match(app, /function chatSurface\(\) \{ return state\.mobileChatOpen \? 'conversation' : 'chat-list-page'; \}/, 'the dashboard is the chat landing on every shell size');
  assert.match(app, /if \(!state\.mobileChatOpen\) \{ state\.mobileChatOpen = true; syncView\(\); \}/, 'picking a contact opens the conversation on any shell');
});

test('v28 Android skin alignment: no contact LEDs, message avatars, time-only meta, App veil/composer/header values', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const sw = readFileSync(new URL('../sw.js', import.meta.url), 'utf8');
  // Astra 2026-10-18: the gray status dots on contact rows / dashboard avatar
  // row are gone (Android ChatListScreen has no such LED); the unread badge stays.
  assert.doesNotMatch(app, /contact-led/, 'no contact-led element is rendered anywhere');
  assert.doesNotMatch(css, /contact-led/, 'no contact-led CSS survives');
  assert.match(app, /class: 'unread-badge'/, 'the unread badge stays');
  // Message rows carry 30px avatars on both sides like Android MessageBubble
  // (ChatScreen.kt:13047-13095), the user side falling back to the bundled
  // default_avatar_user.jpg (ContactDisplay.DEFAULT_USER_AVATAR_URI).
  assert.match(app, /function messageAvatar\(role\)/);
  assert.match(app, /const USER_AVATAR_URI = '\.\/assets\/default_avatar_user\.jpg'/);
  assert.match(app, /item\.replaceChildren\(stack, messageAvatar\('user'\)\)/, 'user rows put the avatar on the right');
  assert.match(app, /item\.replaceChildren\(messageAvatar\('assistant'\), stack\)/, 'assistant rows put the avatar on the left');
  assert.match(css, /\.message-avatar\{width:30px;height:30px/, '30px row avatars (Android 30dp)');
  assert.match(css, /\.message\{[^}]*gap:8px/, '8px avatar-bubble gap (Android 8dp)');
  assert.match(sw, /'\.\/assets\/default_avatar_user\.jpg'/, 'the user default avatar is precached');
  // The meta line is the bare timestamp (Android: no sender name in 1:1
  // windows), 10.5px white@52%, 3px under the bubble.
  assert.match(app, /el\('div', \{ class: 'message-meta' \}, \[el\('time', \{ text: message\.time \|\| '刚刚' \}\)\]\)/, 'meta carries the time only');
  assert.doesNotMatch(app, /ASTRA' :/, 'the uppercase sender name is gone from the meta line');
  assert.match(css, /\.message-meta\{display:flex;gap:8px;margin:3px 0 0 2px;color:rgba\(255,255,255,\.52\);font-size:10\.5px/, 'meta is 10.5px white@52% 3px under the bubble');
  // List rhythm: Android AppearanceSettings gap default 4 (Astra's synced
  // value), LazyColumn spacedBy(settings.gap).
  assert.match(css, /\.messages\{display:grid;gap:4px;/, 'the message list uses the Android 4px gap');
  assert.match(css, /\.from-assistant \.message-stack\{max-width:min\(calc\(100% - 88px\),600px\)\}/, 'assistant bubble column caps at screen-112dp minus the avatar gutter');
  assert.match(css, /\.from-user \.message-stack\{[^}]*max-width:min\(76%,600px\)/, 'user bubble column caps at 76% (ChatScreen.kt:12802)');
  // Date separator: centered black@22% pill with "10月18日" (formatDatePill).
  assert.match(css, /\.date-rule>span\{border-radius:999px;padding:3px 11px;background:rgba\(0,0,0,\.22\)\}/, 'the date separator is the Android pill');
  assert.match(app, /textContent = `\$\{now\.getMonth\(\) \+ 1\}月\$\{now\.getDate\(\)\}日`/, 'date pill text mirrors formatDatePill');
  assert.doesNotMatch(css, /\.date-rule::before/, 'the old rule lines beside the date are gone');
  // Wallpaper veil mirrors chatWallpaperOverlayStops (dark): #1E1A16 at
  // 88/40/36/70/94% so the wallpaper shows through the middle of the chat.
  assert.match(css, /linear-gradient\(180deg,rgba\(30,26,22,\.88\) 0%,rgba\(30,26,22,\.4\) 14%,rgba\(30,26,22,\.36\) 56%,rgba\(30,26,22,\.7\) 84%,rgba\(30,26,22,\.94\) 100%\)/, 'the veil uses the Android overlay stops');
  // Header: 16.5px semibold white name, 12px Clay status, static 6px online
  // dot #8FD08F, 11px online badge on the avatar (ChatHeaderBar).
  assert.match(css, /\.contact-heading h1\{margin:0;font:600 16\.5px\/1\.2 var\(--sans\);letter-spacing:\.01em;color:#fff\}/, 'the header name matches ChatHeaderBar 16.5sp semibold');
  assert.match(css, /\.contact-sub\{display:flex;align-items:center;gap:5px;margin:3px 0 0;color:var\(--dim\);font-size:12px\}/, 'the status line is 12px with a 5px gap');
  assert.match(css, /\.signal-dot\{width:6px;height:6px;flex:0 0 auto;border-radius:50%;background:#8fd08f\}/, 'the status dot is the static OnlineGreen dot');
  assert.doesNotMatch(css, /\.signal-dot\.is-busy/, 'the dot no longer pulses on busy');
  assert.doesNotMatch(app, /#signal-dot'\)\.classList/, 'renderHeader no longer toggles the dot');
  assert.match(css, /\.contact-heading \.avatar\.contact-avatar::after\{position:absolute;right:1px;bottom:1px;width:11px;height:11px;border:2px solid #1c1815;border-radius:50%;background:#8fd08f/, 'the avatar carries the 11px online badge');
  // Composer: flat bar in inputBarBg #1C1815@58%, 9/11 vertical padding,
  // 16px field text, white@52% placeholder (ChatInputBarShell).
  assert.match(css, /\.composer\{[^}]*padding:9px clamp\(18px,3vw,30px\) 11px;border-radius:0 0 23px 23px;background:rgba\(28,24,21,\.58\)/, 'the composer bar is the App inputBarBg');
  assert.match(css, /textarea\{[^}]*padding:8px 0;background:transparent;color:#fff;font-size:16px;line-height:1\.375\}/, 'the field text is 16px like the App composer');
  assert.match(css, /textarea::placeholder\{color:rgba\(255,255,255,\.52\)/, 'the placeholder is white@52% (textFaint)');
  assert.match(app, /input\.placeholder = `跟\$\{name\}说点什么…`/, 'the placeholder mirrors the Android default 跟X说点什么…');
  // The phone composer is the flat App bar, not a floating pill.
  const phone = css.slice(css.indexOf('@media (max-width:767px)'));
  assert.match(phone, /\.composer\{gap:9px;margin:0;padding:9px 12px 11px;border:0;border-radius:0;background:rgba\(28,24,21,\.58\)/, 'phone composer is the flat App input bar');
  assert.match(phone, /\.conversation-head\{min-height:64px;padding:11px 16px;gap:10px;border-radius:0;background:rgba\(28,24,21,\.55\)/, 'phone header is the App headerBg bar');
  assert.doesNotMatch(html, /id="signal-dot"[^>]*class="signal-dot is-busy"/, 'the header dot starts in the plain online state');
});

test('desktop reskin (>=1200px): nav rail + contacts column + dashboard landing, instrument beside conversation', () => {
  const app = readFileSync(new URL('../src/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /class="eyebrow rail-contacts-head"/, 'the contacts column gets its own header');
  assert.match(app, /shell\.dataset\.view = state\.view;/);
  assert.match(app, /shell\.dataset\.surface = state\.view === 'chat' \? \(state\.mobileChatOpen \? 'conversation' : 'dashboard'\) : '';/);
  assert.match(css, /@media \(min-width:1200px\)\{/);
  const desktop = css.slice(css.indexOf('@media (min-width:1200px)'));
  assert.match(desktop, /\.desk-shell\{grid-template-columns:208px 272px minmax\(0,1fr\) 312px/, 'rail + contacts + main + instrument');
  assert.match(desktop, /\.rail\{display:grid;grid-column:1 \/ 3/, 'the rail splits into nav and contacts columns');
  assert.match(desktop, /\.rail-contacts-head\{display:block;grid-column:2;grid-row:1/, 'contacts header sits atop the contacts column');
  assert.match(desktop, /\.contact-list\{grid-column:2;grid-row:2 \/ -1/, 'contacts occupy their own column below the header');
  assert.match(desktop, /\.rail-contacts-head\{display:block/, 'contacts column header is desktop-only');
  assert.match(desktop, /\.desk-shell\[data-view='chat'\]\[data-surface='dashboard'\] \.side-notes\{display:none\}/, 'dashboard hides the instrument panel');
  assert.match(desktop, /#chat-list-page\{grid-column:3\}/, 'dashboard sits beside the contacts column');
  assert.match(desktop, /\.desk-shell:not\(\[data-view='chat'\]\) \.contact-list\{display:none\}/, 'non-chat views collapse the rail to nav only');
  assert.match(css, /\.contact-heading \.avatar\.contact-avatar\{display:grid;overflow:visible;width:40px/, 'the header shows the round avatar on every shell (App parity), unclipped so the online dot can straddle the edge');
  assert.match(css, /\.contact-sub\{display:flex/, 'the header shows the single status line on every shell');
  assert.match(desktop, /\.contact-heading \.avatar\.contact-avatar\{width:44px/, 'desktop keeps the larger 44px header avatar');
  assert.doesNotMatch(html, /id="contact-mode"/, 'the KIMI-WEB-style eyebrow above the contact name is gone');
  assert.match(desktop, /\.terminal-page-list\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/, 'terminal cards go two-column on wide screens');
  assert.match(desktop, /#memory-browse,#memory-board\{max-width:880px;margin-inline:auto\}/, 'memory page content is width-capped and centered');
  assert.match(css, /body\{[^]*?url\('\/plugins\/xiaonan-dash\/appearance-asset'\)/, 'the wallpaper channel backs the desktop glass too');
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
  assert.match(app, /import \{ clockText, greetingFor, todoFeed, weekStrip \} from '\.\/home-dashboard\.js\?v=28'/);
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
  assert.match(css, /\.avatar-img\{position:absolute;inset:0;display:block;width:100%;height:100%;border-radius:50%;object-fit:cover\}/, 'the avatar image is pinned to the circle so tall photos cannot cover the name');
  assert.match(css, /\.avatar\{position:relative;[^}]*overflow:hidden;[^}]*font:500 13px var\(--sans\)/, 'fallback discs clip their image and render initials in the sans stack');
  assert.match(css, /\.avatar\.rose\{[^}]*#c97b6e[^}]*\}/);
  assert.match(css, /\.avatar\.lavender\{[^}]*#7b7199[^}]*\}/);
  assert.match(css, /\.avatar\.steel\{[^}]*#6b8e9e[^}]*\}/);
  assert.match(css, /\.avatar\.moss\{[^}]*#748a5b[^}]*\}/);
  assert.match(app, /const AVATAR_ACCENTS = \{ xiaoke: 'oxide', apples: 'rose', kairos: 'brass', kimi: 'lavender', toolbot: 'steel' \}/, 'fixed accents mirror Android toChatContact');
  assert.match(app, /const AVATAR_ACCENT_PALETTE = \['oxide', 'brass', 'rose', 'lavender', 'steel', 'moss'\]/, 'palette order mirrors Android dynamicContactColor');
  assert.match(app, /Math\.imul\(hash, 31\) \+ text\.charCodeAt\(i\)\) \| 0/, 'Java String.hashCode parity');
  assert.match(app, /\(javaStringHash\(id\) & 0x7fffffff\) % AVATAR_ACCENT_PALETTE\.length/);
  assert.doesNotMatch(app, /=== 'kairos' \? 'brass' : 'oxide'/, 'the single-color fallback ternary is gone');
  assert.match(app, /function avatarUriOf\(id\) \{ return state\.dash\.appearance\.avatars\[id\] \|\| DEFAULT_AVATAR_URIS\[id\] \|\| ''; \}/, 'server custom avatars win, bundled defaults are the fallback');
  for (const [id, file] of [['xiaoke', 'default_avatar_xiaoke.webp'], ['kairos', 'default_avatar_kairos.jpg'], ['kimi', 'default_avatar_ai.jpg'], ['apples', 'default_avatar_group.jpg'], ['toolbot', 'default_avatar_toolbot.jpg']]) {
    assert.match(app, new RegExp(`${id}: '\\./assets/${file}'`), `bundled default avatar for ${id}`);
  }
  assert.match(app, /hakimi: '\.\/assets\/default_avatar_hakimi\.jpg',\n  hajimi: '\.\/assets\/default_avatar_hakimi\.jpg',\n  hajiki: '\.\/assets\/default_avatar_hakimi\.jpg',/, 'hakimi aliases mirror Android ContactDisplay');
  assert.doesNotMatch(app, /kiro: '\.\/assets\/default_avatar/, 'Android ships no kiro default avatar, so the PWA must not invent one');
  const swAssets = readFileSync(new URL('../sw.js', import.meta.url), 'utf8');
  for (const file of ['default_avatar_xiaoke.webp', 'default_avatar_kairos.jpg', 'default_avatar_ai.jpg', 'default_avatar_hakimi.jpg', 'default_avatar_group.jpg', 'default_avatar_toolbot.jpg']) {
    assert.match(swAssets, new RegExp(`'\\./assets/${file}'`), `${file} is precached`);
  }
  const fixtures = readFileSync(new URL('../src/data.js', import.meta.url), 'utf8');
  for (const id of ['hakimi', 'apples', 'toolbot']) assert.match(fixtures, new RegExp(`\\{ id: '${id}'`), `mock contact ${id}`);
  assert.match(css, /#contact-status\{color:#d97757\}/, 'the header status line is the App Clay #D97757');
  assert.match(css, /\.chat-list\{display:flex;flex-wrap:nowrap;gap:8px;margin-top:13px;overflow-x:auto/, 'the avatar row scrolls sideways instead of clipping');
  assert.match(css, /\.chat-list\{[^}]*min-width:0/, 'a long avatar row never stretches the mobile dashboard');
  assert.match(css, /\.dash-contacts-card\{min-width:0\}/, 'the contacts grid item yields its track on narrow shells');
  assert.match(css, /\.chat-list \.chat-card\{display:grid;flex:0 0 auto/, 'cards keep their width while scrolling');
});
