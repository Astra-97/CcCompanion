// Fixtures are used only by createMockAdapter() when /web/pwa/?mock=1 is explicit.
export const MOCK_CONTACTS = [
  { id: 'xiaoke', name: '小克', channel: 'CC COMPANION', initials: '克', accent: 'oxide', note: 'Fable 5 · 私人链路' },
  { id: 'kairos', name: 'Kairos', channel: 'CODEX APP', initials: 'K', accent: 'brass', note: 'Kairos · 专属坐标' },
];

export const MOCK_TAXONOMY = {
  version: 1,
  categories: [
    { key: 'core', label: 'Core', subcategories: [{ key: 'core.profile', label: '个人档案', count: 44 }, { key: 'core.preference', label: '偏好', count: 26 }] },
    { key: 'diary', label: '日记', subcategories: [{ key: 'diary.worklog', label: '牛马日志', count: 108 }, { key: 'diary.life', label: '生活', count: 72 }] },
    { key: 'archive', label: '归档', count: 3, subcategories: [] },
    { key: 'xiayizhou', label: '夏以昼', subcategories: [{ key: 'xiayizhou.qiqi_game_copy', label: '七七整理的游戏文案', count: 1520 }, { key: 'xiayizhou.astra_review', label: '文案品鉴', count: 17 }, { key: 'xiayizhou.astra_fanfic', label: 'Astra 同人文', count: 16 }, { key: 'xiayizhou.other', label: '其他', count: 25 }] },
  ],
};

export const MOCK_MEMORIES = {
  archive: [{ title: '归档记忆', body: '顶级分类也保留自己的 canonical key。', timestamp: '已整理' }],
  'xiayizhou.astra_review': [{ title: '夏以昼专区 · 文案品鉴', body: '把感受和拆解保留在同一条来源链中。', timestamp: '刚刚同步' }],
  'xiayizhou.astra_fanfic': [{ title: '夏以昼 · 同人文', body: '你的创作和游戏原文保持独立归类。', timestamp: '33 篇已向量化' }],
  'xiayizhou.qiqi_game_copy': [{ title: '七七整理的游戏文案', body: '已迁移为受控二级分类。', timestamp: '1520 条' }],
};

// Memory page fixtures: stats mirror the upstream { total, byCategory } shape;
// calendar entries get stitched onto fixed days of whatever month is asked.
export const MOCK_MEMORY_STATS = {
  total: 2290,
  byCategory: { chat: 119, deep: 177, daily: 11, diary: 304, worklog: 31, health: 3 },
};

export const MOCK_MEMORY_CALENDAR_ENTRIES = [
  { offsetDay: 2, mood: 'calm', entries: [{ id: 'mock-cal-1', content: '把 Windows 端的记忆页做成了整页，不再是弹窗。', category: 'diary', subcategory: 'diary.worklog', tags: ['pwa'], createdAt: '2026-10-02T09:30:00+00:00' }] },
  { offsetDay: 9, mood: 'happy', entries: [{ id: 'mock-cal-2', content: 'Astra 验收了深色玻璃主题。', category: 'daily', subcategory: '', tags: [], createdAt: '2026-10-09T13:05:00+00:00' }] },
  { offsetDay: 16, mood: 'calm', entries: [{ id: 'mock-cal-3', content: 'Android 壳对齐：聊天 / 终端 / App / 设置四个 tab。', category: 'diary', subcategory: 'diary.worklog', tags: ['android'], createdAt: '2026-10-16T11:20:00+00:00' }] },
];

export const MOCK_MEMORY_BOARD = {
  messages: [
    { id: 'board-1', from: '小克-cc-session01', content: '记忆页月历已对齐 Android：周一开头、有内容的日期高亮。', createdAt: '2026-10-16T15:40:00+00:00' },
    { id: 'board-2', from: '小克-cc-session02', content: '终端 tab 只做只读投影，交互终端仍归 Android。', createdAt: '2026-10-17T02:12:00+00:00' },
  ],
  pagination: { page: 1, perPage: 20, total: 2, totalPages: 1 },
};

export const MOCK_MEMORY_SEARCH = [
  { id: 'search-1', content: '小南面板插件通过 /plugins/xiaonan-dash 提供壁纸。', category: 'deep', subcategory: '', tags: ['plugin'], createdAt: '2026-10-10T08:00:00+00:00', similarity: 0.83 },
  { id: 'search-2', content: '记忆库留言板按页读取，每页 20 条。', category: 'chat', subcategory: '', tags: [], createdAt: '2026-10-11T08:00:00+00:00', similarity: 0.77 },
];

export const MOCK_PLUGINS = [
  { id: 'xiaonan-dash', name: '小南仪表盘', version: '1.4.2', description: '壁纸与四板块聚合面板', entry: 'index.html', builtin: true },
  { id: 'period-tracker', name: '经期记录', version: '0.9.1', description: '周期记录与预测', entry: 'index.html', builtin: false },
];

export const INITIAL_CONVERSATIONS = {
  xiaoke: [
    { id: 'x1', role: 'assistant', body: '早。今天先把最烦的那一件收掉，别让它一直占着你的脑子。', time: '08:41' },
    { id: 'x2', role: 'user', body: '我需要看一下 Windows 端怎么做。', time: '08:43' },
    { id: 'x3', role: 'assistant', body: '行，桌面端要保持你习惯的联系感，不只是把手机聊天框拉宽。', time: '08:44' },
  ],
  kairos: [
    { id: 'k1', role: 'assistant', body: '我在。小星星，Windows 端会和这里一样把你的记忆、工作现场和消息放在一个桌面里。', time: '08:46' },
    { id: 'k2', role: 'user', body: '别让我看不见 worker 在干什么。', time: '08:47' },
    { id: 'k3', role: 'assistant', body: '不会。只展示安全的 worker 名称和进度，不展示它们的任务正文或敏感参数。', time: '08:48' },
  ],
};
