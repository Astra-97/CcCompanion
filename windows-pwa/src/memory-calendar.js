// Pure calendar helpers for the memory page month grid.  Weeks start on
// Monday to match the Android memory library layout; no Date state leaks in
// from callers — every function takes its own reference point.

/** 'YYYY-MM' for a Date (or today). */
export function monthKey(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  return `${year}-${month}`;
}

/** 'YYYY-MM-DD' for a Date (or today). */
export function todayKey(date = new Date()) {
  const day = String(date.getDate()).padStart(2, '0');
  return `${monthKey(date)}-${day}`;
}

/** Move a 'YYYY-MM' key by whole months, tolerant of bad input. */
export function shiftMonth(key, delta = 0) {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(String(key || ''));
  if (!match) return monthKey();
  const date = new Date(Number(match[1]), Number(match[2]) - 1 + Math.trunc(delta), 1);
  return monthKey(date);
}

/**
 * Week rows for a 'YYYY-MM' key.  Each cell is { date, day, inMonth } where
 * `date` is the full 'YYYY-MM-DD' key (padding cells carry their own month,
 * so entry lookups never mis-fire) and `inMonth` marks padding from the
 * neighbouring months.
 */
export function buildMonthGrid(key) {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(String(key || ''));
  if (!match) return { key: monthKey(), year: 0, month: 0, weeks: [] };
  const year = Number(match[1]);
  const month = Number(match[2]);
  const first = new Date(year, month - 1, 1);
  const daysInMonth = new Date(year, month, 0).getDate();
  const lead = (first.getDay() + 6) % 7; // Monday-first offset of the 1st.
  const totalCells = Math.ceil((lead + daysInMonth) / 7) * 7;
  const weeks = [];
  for (let cell = 0; cell < totalCells; cell += 1) {
    const offset = cell - lead; // 0-based day inside the month; may pad out.
    const date = new Date(year, month - 1, 1 + offset);
    const entry = { date: todayKey(date), day: date.getDate(), inMonth: offset >= 0 && offset < daysInMonth };
    if (cell % 7 === 0) weeks.push([]);
    weeks[weeks.length - 1].push(entry);
  }
  return { key: `${match[1]}-${match[2]}`, year, month, weeks };
}
