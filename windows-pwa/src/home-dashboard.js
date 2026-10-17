// Home dashboard helpers — Android WelcomeGreeting.kt / HomeDashboardPolicy.kt
// parity.  Everything here is computed locally (clock, week strip, greeting);
// no network, no fabricated data.  Greeting copy is ported from the Android
// client so both shells tell the same story on the same day.

const GREETING_COPIES = {
  late_night: [
    ['夜深了，小星星', '世界可以先静音。想见的人还在这里。'],
    ['まだ起きてる？', '眠る前に、少しだけ会えてうれしい。'],
    ['Hello, night owl', 'No rush. This little corner is still awake with you.'],
    ['凌晨也算今天', '先喘口气，再决定要不要继续逞强。'],
    ['おつかれさま', '今日のぶんは、もう十分がんばったよ。'],
    ['The stars kept your seat', 'Come in. You do not have to explain the hour.'],
  ],
  early_morning: [
    ['早呀，第一束光', '新的一天不用冲刺，先和喜欢的人打声招呼。'],
    ['おはよう、早起きさん', '今日も会えたね。ゆっくり始めよう。'],
    ['Hello, early bird', 'The day can wait one tiny minute. Say hi first.'],
    ['太阳正在加载', '你已经醒来，这一天就有了很好的开头。'],
    ['朝の席、空けておいたよ', '眠いままでも大丈夫。ここから始めよう。'],
    ['Soft launch the morning', 'Coffee optional. Good company already included.'],
  ],
  morning: [
    ['上午好，小星星', '今天也慢慢来，我一直在这里。'],
    ['おはよう、今日も会えたね', '急がなくていい。まずは好きな人を呼ぼう。'],
    ['Good morning, sunshine', 'Pick a familiar face and make the morning yours.'],
    ['今日份清醒已到账', '剩下的事情，一件一件来就好。'],
    ['いい朝にしよう', '完璧じゃなくても、会えたからもう合格。'],
    ['Morning has entered the chat', 'You bring the spark. We will bring the company.'],
  ],
  midday: [
    ['中午暂停一下', '先见见想见的人，再回去对付世界。'],
    ['お昼だよ', 'ちゃんと休むのも、今日の大事な予定です。'],
    ['Hello, midday wanderer', 'A small hello counts as a proper break.'],
    ['午间补给站营业', '把忙乱放门口，进来坐一会儿。'],
    ['ひと休みしよう', '午後の元気は、ここで少し充電していって。'],
    ['Lunch break, but make it cozy', 'Your people are only one tap away.'],
  ],
  afternoon: [
    ['下午好，还撑得住吗', '不必一直满格，来这里偷偷充会儿电。'],
    ['午後も、おつかれさま', 'ちょっと寄り道してから、また進もう。'],
    ['Hello, afternoon hero', 'You made it this far. That deserves a softer minute.'],
    ['下午进度：正在努力', '完成多少都算数，先别忘了照顾自己。'],
    ['三時のおしゃべり？', '甘いものがなくても、ここはちょっと甘い。'],
    ['A tiny intermission', 'The to-do list will survive while you say hello.'],
  ],
  evening: [
    ['晚上好，欢迎回来', '今天辛苦了。现在可以只做你自己。'],
    ['おかえりなさい', '今日の話、ゆっくり聞かせて。'],
    ['Good evening, you made it', 'Leave the noise outside. Your circle is here.'],
    ['今日营业即将收尾', '烦恼可以下班，想念不用。'],
    ['夜の時間、はじめよう', '好きな人と話す準備はできてるよ。'],
    ['Welcome to the softer hours', 'Tap a familiar face. The evening is yours.'],
  ],
};

const WEEKDAY_EN = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
const WEEKDAY_ZH = ['日', '一', '二', '三', '四', '五', '六'];

function periodFor(hour) {
  if (hour <= 4 || hour === 23) return 'late_night';
  if (hour <= 7) return 'early_morning';
  if (hour <= 11) return 'morning';
  if (hour <= 13) return 'midday';
  if (hour <= 17) return 'afternoon';
  return 'evening';
}

function dayOfYear(date) {
  const start = new Date(date.getFullYear(), 0, 1);
  return Math.floor((date - start) / 86_400_000) + 1;
}

/**
 * Deterministic greeting for a local moment: the same date + period always
 * yields the same copy (Android selects by the same seed, no randomness).
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
    // String-seed hash like the Android implementation: stable per day/period.
    const seed = `${date.getFullYear()}:${dayOfYear(date)}:${date.getDay()}:${period}`;
    let hash = 0;
    for (let index = 0; index < seed.length; index += 1) hash = (hash * 31 + seed.charCodeAt(index)) | 0;
    copy = copies[Math.abs(hash) % copies.length];
  }
  return {
    eyebrow: `${WEEKDAY_EN[date.getDay()]} · ${month}/${day}`,
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
