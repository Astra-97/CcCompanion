"""门口 NFC「工作日早上贴一下 = 出门」测试 (2026-10-23).

覆盖:
  1. cn_workday: 普通工作日 / 周末 / 调休补班周六周日 / 节假日里的工作日 / 表外年份退回周一到周五
  2. _nfc_is_leave_time: 北京 05:00–11:00 边界, UTC 输入换算, 只在法定工作日
  3. 门口 tag 端到端 (真实判定 + 固定时钟):
     普通工作日早上 → 出门; 周末早上 → 原逻辑; 调休补班周六早上 → 出门;
     节假日里的工作日早上 → 原逻辑; 工作日晚上 → 原「欢迎回家」关窗帘开灯
  4. 出门模式: 不动设备、胶囊「方小南出门了🚪」、页面「路上小心 / 我陪你去上班」、
     通知「[NFC·门口螃蟹·出门] HH:MM 方小南贴了一下门口螃蟹，出门上班了」、metadata leave=true
     且 nfc_action 仍是 home; test=1 带测试标记不冒胶囊; 床头 tag 不受影响

全部 mock, 不碰线上 8291 服务、不动米家。
"""
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import cn_workday
from push import PushHandler
from _nfc_tap_test import BEDSIDE, DOOR, NfcTapBase

BJ = ZoneInfo("Asia/Shanghai")
ORIG_LEAVE = PushHandler.__dict__["_nfc_is_leave_time"]


def bj(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=BJ)


class CnWorkdayTest(unittest.TestCase):
    def test_plain_weekday_and_weekend(self) -> None:
        self.assertTrue(cn_workday.is_workday(date(2026, 10, 23)))   # 周五
        self.assertFalse(cn_workday.is_workday(date(2026, 10, 24)))  # 周六
        self.assertFalse(cn_workday.is_workday(date(2026, 10, 25)))  # 周日

    def test_makeup_weekend_workdays(self) -> None:
        for d in (date(2026, 1, 4), date(2026, 2, 14), date(2026, 2, 28),
                  date(2026, 5, 9), date(2026, 9, 20), date(2026, 10, 10)):
            self.assertGreaterEqual(d.weekday(), 5, d)
            self.assertTrue(cn_workday.is_workday(d), d)
            self.assertTrue(cn_workday.is_makeup_workday(d), d)

    def test_holidays_on_weekdays(self) -> None:
        for d in (date(2026, 1, 1), date(2026, 1, 2), date(2026, 2, 16), date(2026, 2, 23),
                  date(2026, 4, 6), date(2026, 5, 4), date(2026, 5, 5), date(2026, 6, 19),
                  date(2026, 9, 25), date(2026, 10, 1), date(2026, 10, 7)):
            self.assertLess(d.weekday(), 5, d)
            self.assertFalse(cn_workday.is_workday(d), d)
            self.assertTrue(cn_workday.is_holiday(d), d)
        self.assertTrue(cn_workday.is_workday(date(2026, 10, 8)))  # 国庆后第一天

    def test_whole_2026_counts(self) -> None:
        days = [date.fromordinal(o) for o in range(date(2026, 1, 1).toordinal(), date(2027, 1, 1).toordinal())]
        self.assertEqual(sum(cn_workday.is_holiday(d) for d in days), 33)
        self.assertEqual(sum(cn_workday.is_makeup_workday(d) for d in days), 6)
        self.assertTrue(all(cn_workday.coverage(d) for d in days))

    def test_uncovered_year_falls_back_to_weekdays(self) -> None:
        self.assertFalse(cn_workday.coverage(date(2027, 1, 4)))
        self.assertTrue(cn_workday.is_workday(date(2027, 1, 4)))   # 周一
        self.assertFalse(cn_workday.is_workday(date(2027, 1, 9)))  # 周六


class LeaveTimeTest(unittest.TestCase):
    def leave(self, now) -> bool:
        return ORIG_LEAVE.__func__(PushHandler, now)

    def test_window_boundaries(self) -> None:
        self.assertFalse(self.leave(bj(2026, 10, 23, 4, 59)))
        self.assertTrue(self.leave(bj(2026, 10, 23, 5, 0)))
        self.assertTrue(self.leave(bj(2026, 10, 23, 8, 30)))
        self.assertTrue(self.leave(bj(2026, 10, 23, 10, 59)))
        self.assertFalse(self.leave(bj(2026, 10, 23, 11, 0)))
        self.assertFalse(self.leave(bj(2026, 10, 23, 19, 30)))

    def test_utc_input_is_converted_to_beijing(self) -> None:
        # UTC 00:30 = 北京 08:30 (周五)
        self.assertTrue(self.leave(datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc)))
        # UTC 周五 22:30 = 北京周六 06:30
        self.assertFalse(self.leave(datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc)))

    def test_only_on_cn_workdays(self) -> None:
        self.assertFalse(self.leave(bj(2026, 10, 24, 8)))  # 周六
        self.assertTrue(self.leave(bj(2026, 10, 10, 8)))   # 调休补班周六
        self.assertFalse(self.leave(bj(2026, 10, 2, 8)))   # 国庆假期里的周五


class DoorLeaveEndToEndTest(NfcTapBase):
    def setUp(self) -> None:
        super().setUp()
        self._patch_cls("_nfc_is_leave_time", ORIG_LEAVE)
        self.now = bj(2026, 10, 23, 8, 30)
        self._patch_cls("_nfc_now", staticmethod(lambda: self.now))
        self.night = False

    def assert_leave(self, h) -> None:
        self.assertEqual(self.home_calls, [])  # 不动任何设备
        call = self.channel_calls[0]
        meta = call["metadata"]
        self.assertEqual((meta["nfc_tag"], meta["nfc_action"]), ("door", "home"))
        self.assertIs(meta["leave"], True)
        self.assertIs(meta["night"], False)
        self.assertRegex(
            call["text"],
            r"^\[NFC·门口螃蟹·出门\] \d{2}:\d{2} 方小南贴了一下门口螃蟹，出门上班了"
            r"（她出门上班了，用 companion reply contact_id=xiaoke 跟她说路上小心）$",
        )
        self.assertEqual([rec["text"] for _, rec in self.bubbles], ["方小南出门了🚪"])
        body = self.body(h)
        self.assertIn("路上小心", body)
        self.assertIn("我陪你去上班", body)
        self.assertNotIn("欢迎回家", body)

    def assert_home_original(self, h, *, night: bool) -> None:
        meta = self.channel_calls[0]["metadata"]
        self.assertIs(meta["leave"], False)
        self.assertIn("·回家]", self.channel_calls[0]["text"])
        self.assertIn("欢迎回家", self.body(h))
        self.assertNotIn("路上小心", self.body(h))
        if night:
            self.assertEqual(self.home_calls, [False])
            self.assertIn("窗帘关好了，灯开了，我也来了", self.body(h))
        else:
            self.assertEqual(self.home_calls, [])
            self.assertIn("白天，没动设备", self.channel_calls[0]["text"])
            self.assertEqual([rec["text"] for _, rec in self.bubbles], ["方小南捏了捏🤏门口的🦀"])

    def test_plain_workday_morning_is_leave(self) -> None:
        self.assert_leave(self.tap(f"?k={DOOR}"))

    def test_leave_ignores_darkness(self) -> None:
        # 冬天 06:30 天还黑: 出门也不关窗帘开灯
        self.now = bj(2026, 12, 21, 6, 30)  # 周一
        self.night = True
        self.assert_leave(self.tap(f"?k={DOOR}"))

    def test_weekend_morning_keeps_original(self) -> None:
        self.now = bj(2026, 10, 24, 8, 30)
        self.assert_home_original(self.tap(f"?k={DOOR}"), night=False)

    def test_makeup_saturday_morning_is_leave(self) -> None:
        self.now = bj(2026, 10, 10, 8, 30)
        self.assert_leave(self.tap(f"?k={DOOR}"))

    def test_holiday_weekday_morning_keeps_original(self) -> None:
        self.now = bj(2026, 10, 2, 8, 30)
        self.assert_home_original(self.tap(f"?k={DOOR}"), night=False)

    def test_workday_evening_keeps_welcome_home(self) -> None:
        self.now = bj(2026, 10, 23, 19, 30)
        self.night = True
        self.assert_home_original(self.tap(f"?k={DOOR}"), night=True)

    def test_test_flag_marks_and_skips_bubble(self) -> None:
        h = self.tap(f"?k={DOOR}&test=1")
        self.assertEqual(self.home_calls, [])
        self.assertEqual(self.bubbles, [])
        self.assertRegex(self.channel_calls[0]["text"], r"^\[NFC·门口螃蟹·出门·测试\] \d{2}:\d{2} ")
        self.assertIn("不用回她", self.channel_calls[0]["text"])
        self.assertIn("路上小心", self.body(h))

    def test_bedside_unaffected_on_workday_morning(self) -> None:
        h = self.tap(f"?k={BEDSIDE}")
        meta = self.channel_calls[0]["metadata"]
        self.assertEqual(meta["nfc_action"], "summon")
        self.assertNotIn("leave", meta)
        self.assertIn("小克来了", self.body(h))

    def test_dedupe_second_tap_still_says_leave_title(self) -> None:
        self.tap(f"?k={DOOR}")
        h = self.tap(f"?k={DOOR}")
        self.assertEqual(len(self.channel_calls), 1)
        self.assertIn("路上小心", self.body(h))
        self.assertIn("我已经知道啦，在呢", self.body(h))


if __name__ == "__main__":
    unittest.main()
