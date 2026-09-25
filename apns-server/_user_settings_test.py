import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from push import PushHandler


def _make_handler():
    handler = object.__new__(PushHandler)
    handler.responses = []
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    return handler


class UserSettingsPackingListTests(unittest.TestCase):
    def _patched(self, directory):
        return patch.object(
            PushHandler, "_USER_SETTINGS_PATH", Path(directory) / "user_settings.json"
        )

    def test_packing_list_round_trip_via_post_and_get(self):
        handler = _make_handler()
        items = [
            {"id": "seed_1", "name": "颈枕", "status": "PACKED", "packed": True},
            {"id": "item_9_x", "name": "插排", "status": "NOT_BRINGING", "packed": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self._patched(directory):
                handler._handle_user_settings_post(
                    {"packing_list": {"version": 3, "updated_at_ms": 123, "items": items}}
                )
                self.assertEqual(handler.responses[0][0], 200)

                handler._handle_user_settings_get()
                status, body = handler.responses[-1]
                self.assertEqual(status, 200)
                self.assertEqual(body["settings"]["packing_list"]["items"], items)
                self.assertEqual(body["settings"]["packing_list"]["updated_at_ms"], 123)

            # 持久化文件本身也落盘了
            stored = json.loads((Path(directory) / "user_settings.json").read_text())
            self.assertEqual(stored["packing_list"]["items"], items)

    def test_packing_list_is_replaced_wholesale_not_merged(self):
        handler = _make_handler()
        with tempfile.TemporaryDirectory() as directory:
            with self._patched(directory):
                handler._handle_user_settings_post(
                    {"packing_list": {"items": [{"id": "a", "name": "颈枕"}]}}
                )
                # 第二次推整份（用户删掉了 a、加了 b）：旧 key 不能复活
                handler._handle_user_settings_post(
                    {"packing_list": {"items": [{"id": "b", "name": "墨镜"}]}}
                )
                self.assertEqual(handler.responses[-1][0], 200)
                stored = json.loads((Path(directory) / "user_settings.json").read_text())
                self.assertEqual(
                    stored["packing_list"], {"items": [{"id": "b", "name": "墨镜"}]}
                )

    def test_packing_list_must_be_an_object(self):
        handler = _make_handler()
        with tempfile.TemporaryDirectory() as directory:
            with self._patched(directory):
                handler._handle_user_settings_post({"packing_list": [1, 2, 3]})
                self.assertEqual(handler.responses[-1][0], 400)
                self.assertIn("packing_list must be an object", handler.responses[-1][1]["error"])
                self.assertFalse((Path(directory) / "user_settings.json").exists())

    def test_unknown_keys_are_still_rejected(self):
        handler = _make_handler()
        with tempfile.TemporaryDirectory() as directory:
            with self._patched(directory):
                handler._handle_user_settings_post({"definitely_not_a_setting": 1})
                self.assertEqual(handler.responses[-1][0], 400)

    def test_appearance_deep_merge_unaffected_by_packing_list(self):
        handler = _make_handler()
        with tempfile.TemporaryDirectory() as directory:
            with self._patched(directory):
                handler._handle_user_settings_post({"appearance": {"aiName": "栖洲"}})
                handler._handle_user_settings_post(
                    {
                        "appearance": {"meName": "我"},
                        "packing_list": {"items": []},
                    }
                )
                self.assertEqual(handler.responses[-1][0], 200)
                stored = json.loads((Path(directory) / "user_settings.json").read_text())
                # appearance 深合并保留两个字段；packing_list 并存互不干扰
                self.assertEqual(stored["appearance"], {"aiName": "栖洲", "meName": "我"})
                self.assertEqual(stored["packing_list"], {"items": []})


if __name__ == "__main__":
    unittest.main()
