import threading
import types
import unittest

from chat_history import ChatStreamBus
from kimi_web_client import KimiWebError
from push import PushHandler, _kimi_web_boot_orphan_reconcile


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item

    def tail(self, limit):
        return list(self.records[-limit:])


class FakeBootWeb:
    """Minimal Kimi Web provider double for the boot reconcile path."""

    def __init__(self, *, lease=None, reconcile_error=None, start_error=None):
        self.calls = []
        self.lease = dict(lease or {})
        self.reconcile_error = reconcile_error
        self.start_error = start_error

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def reconcile_owned_idle_lease(self, **_kwargs):
        self.calls.append("reconcile")
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return dict(self.lease)


LEASE = {
    "session_id": "web-session-1",
    "prompt_id": "prompt-old",
    "user_ts": "2026-09-23T23:04:16Z",
    "state": "stream_lost",
    "created_at": "1",
}


class KimiBootOrphanReconcileTest(unittest.TestCase):
    def make_state(self, *, web, chat=None):
        chat = chat or FakeChat()
        return types.SimpleNamespace(
            contact_chats={"kimi": chat},
            kimi_web=web,
            kimi_turn_lock=threading.RLock(),
            kimi_active_turn={},
            kimi_prepare_token="",
            kimi_recovery_token="",
            kimi_terminal_acquire_token="",
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=ChatStreamBus(),
        ), chat

    def test_boot_marks_orphan_terminal_and_emits_lifecycle_sse(self):
        web = FakeBootWeb(lease=LEASE)
        state, chat = self.make_state(web=web)
        events = state.chat_stream_bus.subscribe()

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual(["start", "reconcile"], web.calls)
        rows = [row for row in chat.records if (row.get("metadata") or {}).get("orphan_recovery")]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("assistant", row["role"])
        self.assertEqual("kimi-web:failed", row["source"])
        self.assertEqual(LEASE["user_ts"], row["metadata"]["kimi_user_ts"])
        self.assertEqual("terminal_recovery", row["metadata"]["turn_message_kind"])
        self.assertTrue(row["metadata"]["turn_terminal"])

        reply_state = state.chat_reply_states["kimi"]
        self.assertEqual("failed", reply_state["reply_state"])
        self.assertEqual(LEASE["user_ts"], reply_state["user_ts"])
        self.assertEqual("kimi-web:failed", reply_state["source"])

        lifecycle = [event for event in events if event.get("event") == "lifecycle"]
        self.assertEqual(1, len(lifecycle))
        event = lifecycle[0]
        self.assertEqual("kimi", event["contact_id"])
        self.assertEqual("failed", event["reply_state"])
        self.assertEqual(LEASE["user_ts"], event["turn_id"])
        self.assertTrue(event["terminal"])
        self.assertTrue(event["refresh_history"])
        self.assertGreaterEqual(event["revision"], 1)

    def test_boot_without_lease_is_silent(self):
        web = FakeBootWeb(lease={})
        state, chat = self.make_state(web=web)
        events = state.chat_stream_bus.subscribe()

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual(["start", "reconcile"], web.calls)
        self.assertEqual([], chat.records)
        self.assertEqual({}, state.chat_reply_states)
        self.assertEqual([], list(events))

    def test_busy_provider_returning_no_lease_is_never_marked(self):
        # reconcile_owned_idle_lease 只在 provider 可证明 idle 时才清 lease 并
        # 返回；busy（含进行中回合）一律返回 {}，boot 对账必须跟着静默。
        web = FakeBootWeb(lease={})
        state, chat = self.make_state(web=web)
        state.chat_drafts["kimi"] = {"user_ts": LEASE["user_ts"], "text": "半截草稿", "is_active": True}

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual([], chat.records)
        self.assertEqual({}, state.chat_reply_states)
        self.assertEqual("半截草稿", state.chat_drafts["kimi"]["text"])

    def test_local_turn_ownership_skips_before_any_provider_call(self):
        web = FakeBootWeb(lease=LEASE)
        state, chat = self.make_state(web=web)
        state.kimi_active_turn = {"user_ts": "live", "session_id": "web-session-1"}

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual([], web.calls)
        self.assertEqual([], chat.records)

    def test_prepare_reservation_also_skips_reconcile(self):
        web = FakeBootWeb(lease=LEASE)
        state, chat = self.make_state(web=web)
        state.kimi_prepare_token = "prepare-token"

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual([], web.calls)
        self.assertEqual([], chat.records)

    def test_reconcile_error_fails_closed_without_history_or_sse(self):
        web = FakeBootWeb(lease=LEASE, reconcile_error=KimiWebError("provider unreachable"))
        state, chat = self.make_state(web=web)
        events = state.chat_stream_bus.subscribe()

        _kimi_web_boot_orphan_reconcile(state)  # must not raise

        self.assertEqual([], chat.records)
        self.assertEqual({}, state.chat_reply_states)
        self.assertEqual([], list(events))

    def test_provider_start_failure_skips_reconcile(self):
        web = FakeBootWeb(lease=LEASE, start_error=KimiWebError("spawn failed"))
        state, chat = self.make_state(web=web)

        _kimi_web_boot_orphan_reconcile(state)  # must not raise

        self.assertEqual(["start"], web.calls)
        self.assertEqual([], chat.records)

    def test_existing_terminal_row_dedups_history_and_sse(self):
        web = FakeBootWeb(lease=LEASE)
        chat = FakeChat()
        chat.append(
            role="assistant",
            text="上一轮 Kimi 生成未完成，已安全结束。",
            source="kimi-web:failed",
            metadata={
                "kimi_user_ts": LEASE["user_ts"],
                "turn_terminal": True,
                "turn_message_kind": "terminal_recovery",
                "orphan_recovery": True,
            },
        )
        state, chat = self.make_state(web=web, chat=chat)
        events = state.chat_stream_bus.subscribe()

        _kimi_web_boot_orphan_reconcile(state)

        self.assertEqual(1, len(chat.records))
        self.assertEqual({}, state.chat_reply_states)
        self.assertEqual([], list(events))

    def test_provider_without_reconcile_capability_is_noop(self):
        web = types.SimpleNamespace(start=lambda: None)
        state, chat = self.make_state(web=web)

        _kimi_web_boot_orphan_reconcile(state)  # must not raise

        self.assertEqual([], chat.records)


if __name__ == "__main__":
    unittest.main()
