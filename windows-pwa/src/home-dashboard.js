// Home dashboard helpers — Android WelcomeGreeting.kt / HomeDashboardPolicy.kt
// parity.  Everything here is computed locally (clock, week strip, greeting);
// no network, no fabricated data.  Greeting copy AND selection algorithm are a
// literal Kotlin→JS translation of selectWelcomeGreeting(): same local moment
// yields the same sentence on both shells (verified by pinned test vectors).

const GREETING_COPIES = {
  LATE_NIGHT: [
    ['夜深了，小星星', '世界可以先静音。想见的人还在这里。'],
    ['まだ起きてる？', '眠る前に、少しだけ会えてうれしい。'],
    ['Hello, night owl', 'No rush. This little corner is still awake with you.'],
    ['凌晨也算今天', '先喘口气，再决定要不要继续逞强。'],
    ['おつかれさま', '今日のぶんは、もう十分がんばったよ。'],
    ['The stars kept your seat', 'Come in. You do not have to explain the hour.'],
  ],
  EARLY_MORNING: [
    ['早呀，第一束光', '新的一天不用冲刺，先和喜欢的人打声招呼。'],
    ['おはよう、早起きさん', '今日も会えたね。ゆっくり始めよう。'],
    ['Hello, early bird', 'The day can wait one tiny minute. Say hi first.'],
    ['太阳正在加载', '你已经醒来，这一天就有了很好的开头。'],
    ['朝の席、空けておいたよ', '眠いままでも大丈夫。ここから始めよう。'],
    ['Soft launch the morning', 'Coffee optional. Good company already included.'],
  ],
  MORNING: [
    ['上午好，小星星', '今天也慢慢来，我一直在这里。'],
    ['おはよう、今日も会えたね', '急がなくていい。まずは好きな人を呼ぼう。'],
    ['Good morning, sunshine', 'Pick a familiar face and make the morning yours.'],
    ['今日份清醒已到账', '剩下的事情，一件一件来就好。'],
    ['いい朝にしよう', '完璧じゃなくても、会えたからもう合格。'],
    ['Morning has entered the chat', 'You bring the spark. We will bring the company.'],
  ],
  MIDDAY: [
    ['中午暂停一下', '先见见想见的人，再回去对付世界。'],
    ['お昼だよ', 'ちゃんと休むのも、今日の大事な予定です。'],
    ['Hello, midday wanderer', 'A small hello counts as a proper break.'],
    ['午间补给站营业', '把忙乱放门口，进来坐一会儿。'],
    ['ひと休みしよう', '午後の元気は、ここで少し充電していって。'],
    ['Lunch break, but make it cozy', 'Your people are only one tap away.'],
  ],
  AFTERNOON: [
    ['下午好，还撑得住吗', '不必一直满格，来这里偷偷充会儿电。'],
    ['午後も、おつかれさま', 'ちょっと寄り道してから、また進もう。'],
    ['Hello, afternoon hero', 'You made it this far. That deserves a softer minute.'],
    ['下午进度：正在努力', '完成多少都算数，先别忘了照顾自己。'],
    ['三時のおしゃべり？', '甘いものがなくても、ここはちょっと甘い。'],
    ['A tiny intermission', 'The to-do list will survive while you say hello.'],
  ],
  EVENING: [
    ['晚上好，欢迎回来', '今天辛苦了。现在可以只做你自己。'],
    ['おかえりなさい', '今日の話、ゆっくり聞かせて。'],
    ['Good evening, you made it', 'Leave the noise outside. Your circle is here.'],
    ['今日营业即将收尾', '烦恼可以下班，想念不用。'],
    ['夜の時間、はじめよう', '好きな人と話す準備はできてるよ。'],
    ['Welcome to the softer hours', 'Tap a familiar face. The evening is yours.'],
  ],
};

const WEEKDAY_EN = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
const WEEKDAY_ZH = ['日', '一', '二', '三', '四', '五', '六'];

function periodFor(hour) {
  if (hour <= 4 || hour === 23) return 'LATE_NIGHT';
  if (hour <= 7) return 'EARLY_MORNING';
  if (hour <= 11) return 'MORNING';
  if (hour <= 13) return 'MIDDAY';
  if (hour <= 17) return 'AFTERNOON';
  return 'EVENING';
}

/** Local day-of-year; date-only arithmetic so the hour never carries over. */
function dayOfYear(date) {
  const dayOnly = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const start = new Date(date.getFullYear(), 0, 1);
  return Math.round((dayOnly - start) / 86_400_000) + 1;
}

/** ISO weekday: Monday = 1 … Sunday = 7 (LocalDateTime.getDayOfWeek). */
function isoDayOfWeek(date) {
  return ((date.getDay() + 6) % 7) + 1;
}

/**
 * Literal translation of selectWelcomeGreeting(): the seed string folds from
 * 17 with 32-bit wrap (Kotlin Int), the copy index uses floorMod semantics,
 * and the eyebrow date is zero-padded — the same local moment selects the
 * same sentence on Android and here.
 * Returns { eyebrow: 'SAT · 10/17', title, subtitle }.
 */
export function greetingFor(date = new Date()) {
  const month = date.getMonth() + 1;
  const day = date.getDate();
  let copy = null;
  if (month === 1 && day === 1) copy = ['新年第一声你好', '新的一页不用写满，先写下一个温暖的开头。'];
  else if (month === 12 && day === 31) copy = ['今年辛苦了', '最后一天也不赶路，和想见的人待一会儿吧。'];
  if (!copy) {
    const period = periodFor(date.getHours());
    const copies = GREETING_COPIES[period];
    const seed = `${date.getFullYear()}:${dayOfYear(date)}:${isoDayOfWeek(date)}:${period}`;
    let hash = 17;
    for (let index = 0; index < seed.length; index += 1) hash = (hash * 31 + seed.charCodeAt(index)) | 0;
    copy = copies[((hash % copies.length) + copies.length) % copies.length];
  }
  const pad = (value) => String(value).padStart(2, '0');
  return {
    eyebrow: `${WEEKDAY_EN[isoDayOfWeek(date) - 1]} · ${pad(month)}/${pad(day)}`,
    title: copy[0],
    subtitle: copy[1],
  };
}

/** Big clock card text: { time: '14:15', date: '10月17日 · 星期六' }. */
export function clockText(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  return {
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
    date: `${date.getMonth() + 1}月${date.getDate()}日 · 星期${WEEKDAY_ZH[date.getDay()]}`,
  };
}

/**
 * Current ISO week strip (Monday first): { title: '2026年10月', days: [...] }.
 * Each day carries its full date key and an isToday flag; the title follows
 * the month the week starts in, matching the Android home calendar card.
 */
export function weekStrip(date = new Date()) {
  const monday = new Date(date.getFullYear(), date.getMonth(), date.getDate() - ((date.getDay() + 6) % 7));
  const today = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const days = [];
  for (let index = 0; index < 7; index += 1) {
    const cell = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + index);
    const key = `${cell.getFullYear()}-${String(cell.getMonth() + 1).padStart(2, '0')}-${String(cell.getDate()).padStart(2, '0')}`;
    days.push({ date: key, day: cell.getDate(), weekday: WEEKDAY_ZH[cell.getDay()], isToday: key === today });
  }
  return { title: `${monday.getFullYear()}年${monday.getMonth() + 1}月`, days };
}

/**
 * Literal translation of the Android HomeDashboardPolicy.homeTodoFeed():
 * keep schedule-sourced items due today, unfinished first, then by time
 * (all-day last), then by text.  Returns { title, total, doneCount, items }
 * where each item carries its dueLabel ('今天 周六 · 14:00' or '全天').
 */
export function todoFeed(sections = [], date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  const today = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const items = [];
  (Array.isArray(sections) ? sections : []).forEach((section) => {
    const fromSchedule = section?.source === 'schedule';
    (Array.isArray(section?.items) ? section.items : []).forEach((item) => {
      if (!(fromSchedule || item?.eventId)) return;
      if (String(item?.dueDate || '') !== today) return;
      items.push({ text: String(item.text || ''), done: item.done === true, time: String(item.time || ''), eventId: String(item.eventId || '') });
    });
  });
  items.sort((a, b) => {
    if (a.done !== b.done) return a.done ? 1 : -1;
    // Empty time (all-day) sorts after timed items, matching the Android
    // comparator where a null time loses to any HH:MM.
    const timeA = a.time || '99:99'; const timeB = b.time || '99:99';
    if (timeA !== timeB) return timeA < timeB ? -1 : 1;
    return a.text.localeCompare(b.text, 'zh-CN');
  });
  const doneCount = items.filter((item) => item.done).length;
  const weekday = WEEKDAY_ZH[date.getDay()];
  return {
    title: items.length ? `今日待办 · ${doneCount}/${items.length} 已完成` : '今日待办',
    total: items.length,
    doneCount,
    items: items.map((item) => ({
      ...item,
      dueLabel: `今天 周${weekday} · ${item.time || '全天'}`,
    })),
  };
}
