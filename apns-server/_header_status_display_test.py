import types
import unittest

from push import PushHandler


class HeaderStatusDisplayTest(unittest.TestCase):
    def test_instrument_header_is_bounded_model_and_percentage_only(self):
        text, model, percent = PushHandler._header_text_from_instrument(
            {
                "available": True,
                "model": "Fable 5",
                "context": {"used_percent": 12.5},
                "cwd": "/not-exposed",
            },
            loading_text="小克状态加载中",
        )
        display = PushHandler._header_status_display(
            text=text,
            loading_text="小克状态加载中",
            unavailable_text="小克状态暂不可用",
            model=model,
            context_percent=percent,
        )
        self.assertEqual("Fable 5 · 12%", display["text"])
        self.assertEqual("Fable 5", display["model"])
        self.assertEqual(12.5, display["context_percent"])
        self.assertNotIn("cwd", display)
        self.assertNotIn("not-exposed", str(display))

    def test_xiaoke_placeholder_ci_never_reaches_a_cold_header(self):
        handler = object.__new__(PushHandler)
        handler._pwa_xiaoke_instrument_snapshot = lambda: {
            "available": False,
            "model": "",
            "context": {"used_percent": None},
        }
        # The persisted compatibility map is patched at its read boundary, so
        # no runtime status or filesystem is required for this contract.
        import push
        original = push._read_ai_status
        push._read_ai_status = lambda _contact: "ci"
        try:
            display = handler._chat_header_status_display("xiaoke")
        finally:
            push._read_ai_status = original
        self.assertEqual("小克状态加载中", display["text"])
        self.assertEqual("小克状态加载中", display["loading_text"])

    def test_saved_xiaoke_unicode_status_preserves_emoji(self):
        handler = object.__new__(PushHandler)
        import push
        original = push._read_ai_status
        push._read_ai_status = lambda _contact: "全世界都在为白昼的诞生送上祝福🍎"
        try:
            display = handler._chat_header_status_display("xiaoke")
        finally:
            push._read_ai_status = original
        self.assertEqual("全世界都在为白昼的诞生送上祝福🍎", display["text"])

    def test_kimi_model_label_strips_provider_prefix(self):
        self.assertEqual("K3-256k", PushHandler._kimi_header_model("kimi-code/k3-256k"))
        self.assertEqual("K3", PushHandler._kimi_header_model("kimi-code/k3"))

    def test_readonly_approval_accepts_native_read_tools_but_not_shell(self):
        allowed = {"Read", "ReadMediaFile", "Glob", "Grep"}
        for tool in allowed:
            with self.subTest(tool=tool):
                self.assertTrue(PushHandler._kimi_is_routine_readonly_approval({"tool_name": tool}))
        self.assertFalse(PushHandler._kimi_is_routine_readonly_approval({"tool_name": "Bash"}))
        self.assertFalse(PushHandler._kimi_is_routine_readonly_approval({"tool_name": "Read", "command": "write file"}))
