"""中国法定工作日判断 (2026-10-23, 门口 NFC「工作日早上贴一下 = 出门」用).

工作日 = 周一到周五, 但排除法定节假日 (含调休放假的工作日), 加上调休补班的周末.

数据来源: 国务院办公厅《关于2026年部分节假日安排的通知》; 逐日核对过开源库
LKI/chinese-calendar (chinese_calendar/constants.py, 2026-10-23 拉取) 的 2026 年
holidays / workdays 表, 两者一致.

2027 年安排国务院一般在 2026 年 11 月前后公布, 公布后照格式在下面补一年. 表里没有
的年份退回「周一到周五」(coverage() 会标出来, 调用方可据此记日志).
"""
from __future__ import annotations

from datetime import date

# 法定节假日 (放假日, 含调休放假的工作日).
_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset(
        [date(2026, 1, d) for d in (1, 2, 3)]                       # 元旦 1/1–1/3
        + [date(2026, 2, d) for d in range(15, 24)]                  # 春节 2/15–2/23
        + [date(2026, 4, d) for d in (4, 5, 6)]                      # 清明 4/4–4/6
        + [date(2026, 5, d) for d in range(1, 6)]                    # 劳动节 5/1–5/5
        + [date(2026, 6, d) for d in (19, 20, 21)]                   # 端午 6/19–6/21
        + [date(2026, 9, d) for d in (25, 26, 27)]                   # 中秋 9/25–9/27
        + [date(2026, 10, d) for d in range(1, 8)]                   # 国庆 10/1–10/7
    ),
}

# 调休补班 (周末上班).
_MAKEUP_WORKDAYS: dict[int, frozenset[date]] = {
    2026: frozenset([
        date(2026, 1, 4),    # 周日, 元旦补班
        date(2026, 2, 14),   # 周六, 春节补班
        date(2026, 2, 28),   # 周六, 春节补班
        date(2026, 5, 9),    # 周六, 劳动节补班
        date(2026, 9, 20),   # 周日, 国庆补班
        date(2026, 10, 10),  # 周六, 国庆补班
    ]),
}

KNOWN_YEARS = frozenset(_HOLIDAYS) & frozenset(_MAKEUP_WORKDAYS)


def coverage(day: date) -> bool:
    """这一年的官方安排在表里 (False = 只按周一到周五猜)."""
    return day.year in KNOWN_YEARS


def is_holiday(day: date) -> bool:
    return day in _HOLIDAYS.get(day.year, frozenset())


def is_makeup_workday(day: date) -> bool:
    return day in _MAKEUP_WORKDAYS.get(day.year, frozenset())


def is_workday(day: date) -> bool:
    if is_holiday(day):
        return False
    if is_makeup_workday(day):
        return True
    return day.weekday() < 5
