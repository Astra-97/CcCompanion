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
    { key: 'xiayizhou', label: '夏以昼', subcategories: [{ key: 'xiayizhou.qiqi_game_copy', label: '七七整理的游戏文案', count: 1520 }, { key: 'xiayizhou.astra_review', label: '文案品鉴', count: 17 }, { key: 'xiayizhou.astra_fanfic', label: 'Astra 同人文', count: 16 }, { key: 'xiayizhou.other', label: '其他', count: 25 }] },
  ],
};

export const MOCK_MEMORIES = {
  'xiayizhou.astra_review': [{ title: '夏以昼专区 · 文案品鉴', body: '把感受和拆解保留在同一条来源链中。', timestamp: '刚刚同步' }],
  'xiayizhou.astra_fanfic': [{ title: '夏以昼 · 同人文', body: '你的创作和游戏原文保持独立归类。', timestamp: '33 篇已向量化' }],
  'xiayizhou.qiqi_game_copy': [{ title: '七七整理的游戏文案', body: '已迁移为受控二级分类。', timestamp: '1520 条' }],
};

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
