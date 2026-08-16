from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from push import PushHandler


class KimiBillingProjectionTest(unittest.TestCase):
    def test_documented_userinfo_and_usage_are_bounded_and_rendered(self):
        userinfo = {
            "kind": "ok",
            "userInfo": {
                "userLevel": 25,
                "userLevelName": "Allegretto",
                "userId": "must-not-leave-server",
                "nickname": "must-not-leave-server",
                "phone": {"number": "must-not-leave-server"},
            },
        }
        usage = {
            "kind": "ok",
            "summary": {
                "window": {"duration": 1, "unit": "week"},
                "used": 3,
                "limit": 10,
                "reset_at": "2030-01-01T00:00:00Z",
            },
            "limits": [{
                "window": {"duration": 5, "unit": "hour"},
                "used": 4,
                "limit": 20,
                "reset_at": "2030-01-02T00:00:00Z",
            }],
            "extra_usage": {
                "balance_cents": 500,
                "total_cents": 1000,
                "monthly_charge_limit_enabled": True,
                "monthly_charge_limit_cents": 2000,
                "monthly_used_cents": 125,
                "currency": "CNY",
                "wallet_id": "must-not-leave-server",
            },
        }

        windows = PushHandler._kimi_quota_windows(usage)
        billing = PushHandler._kimi_billing_projection(userinfo, usage)

        self.assertEqual(["7 天 Code", "5 小时 Code"], [item["label"] for item in windows])
        self.assertEqual("已用 3/10 · 剩余 70.0%", windows[0]["text"])
        self.assertEqual("Allegretto", billing["membership"]["tier"])
        self.assertEqual(25, billing["membership"]["level"])
        self.assertEqual("CNY 5.00 / 10.00", billing["extra_usage"]["balance_text"])
        rendered = str({"windows": windows, "billing": billing})
        self.assertNotIn("userId", rendered)
        self.assertNotIn("nickname", rendered)
        self.assertNotIn("phone", rendered)
        self.assertNotIn("wallet_id", rendered)

    def test_missing_extra_usage_and_malformed_userinfo_fail_closed(self):
        billing = PushHandler._kimi_billing_projection(
            {"kind": "ok", "userInfo": {"userLevelName": "\x00bad"}},
            {"kind": "ok", "summary": None, "limits": [], "extra_usage": None},
        )
        self.assertEqual("bad", billing["membership"]["tier"])
        self.assertFalse(billing["extra_usage"]["available"])
        self.assertEqual([], PushHandler._kimi_quota_windows({"limits": ["not-a-row"]}))


if __name__ == "__main__":
    unittest.main()
