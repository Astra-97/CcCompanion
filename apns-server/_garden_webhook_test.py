"""双猫花园聊天 webhook 测试 (2026-10-19).

覆盖:
  1. do_POST 路由: /garden/webhook 不要求 X-Auth-Token, 先于 _require_write_auth
  2. 鉴权: 缺密钥 401 / 错密钥 403 / 未配置密钥文件 fail-closed 401 / 权限不是 0600 拒
  3. 正常投递: 历史 role=user + ［🌱方小南在花园说:］前缀 + metadata.source=garden,
     注入文本走 CCC_APP_TURN 直注管线并在末尾带回复提示, 历史里不带提示
  4. 按 id 去重 (Cloudflare 重试), 失败且未入历史时释放 id
  5. 忙时走 channel 排队, 排队文本同样带提示
  6. text 截断 500 字, body 超限 413

全部直接调 handler, 不碰线上 8291 服务。
"""
import io
import json
import os
import re
import tempfile
import threading
import types
import unittest
from pathlib import Path

import push
from push import PushHandler, TmuxInjectionResult

SECRET = "garden-test-secret-0123456789abcdef"
INJECTED_RE = r"^\[CCC_APP_TURN:[0-9a-f]{32}:cctg\]\n\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
PREFIX = "［🌱方小南在花园说:］"


class _Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else default


class GardenWebhookBase(unittest.TestCase):
    def setUp(self) -> None:
        push._GARDEN_WEBHOOK_SEEN.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secret_file = Path(tmp.name) / "webhook_secret"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        os.chmod(self.secret_file, 0o600)
        old = os.environ.get(PushHandler._GARDEN_WEBHOOK_SECRET_FILE_ENV)
        os.environ[PushHandler._GARDEN_WEBHOOK_SECRET_FILE_ENV] = str(self.secret_file)
        self.addCleanup(self._restore_env, old)

    @staticmethod
    def _restore_env(old) -> None:
        if old is None:
            os.environ.pop(PushHandler._GARDEN_WEBHOOK_SECRET_FILE_ENV, None)
        else:
            os.environ[PushHandler._GARDEN_WEBHOOK_SECRET_FILE_ENV] = old

    def handler(self, body, *, secret=SECRET, active=False, channel=False) -> PushHandler:
        handler = object.__new__(PushHandler)
        typing = {
            "is_typing": active,
            "since": "turn-0" if active else None,
            "session": "cctg" if active else "",
            "transport": "tmux" if active else "",
            "turn_token": "a" * 32 if active else "",
        }
        handler.append_calls = []
        handler.injection_calls = []
        handler.channel_calls = []
        handler.responses = []
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret="app-shared-secret",
            strict_auth=True,
            allow_remote_control=True,
            active_session="cctg",
            default_session="cctg",
            typing_state=typing,
            contact_typing_states={"xiaoke": typing},
            xiaoke_stop_lock=threading.RLock(),
            xiaoke_stop_tombstone={},
            xiaoke_stopping_claim={},
            xiaoke_send_reservation={},
            contact_chats={"xiaoke": object()},
            channel_transport_enabled=channel,
            channel_transport_contacts=["xiaoke"],
            settings={},
        )
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: handler.append_calls.append(record) or {**record, "ts": "turn-1"}
        )
        handler._source_for_request = lambda *a: ("android-app:" + a[0]) if a and a[0] else "android-app"
        handler._channel_transport_enabled_for = lambda _contact: channel
        handler._send_to_channel_transport = lambda **kwargs: (
            handler.channel_calls.append(kwargs) or (True, "", {"queued": True})
        )
        handler._inject_to_session = lambda *args, **kwargs: (
            handler.injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )
        handler._send_json = lambda status, payload, **_kw: handler.responses.append((status, payload))
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = _Headers({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        if secret is not None:
            headers["X-Garden-Webhook-Secret"] = secret
        handler.headers = headers
        handler.rfile = io.BytesIO(raw)
        handler.path = "/garden/webhook"
        return handler


class GardenWebhookAuthTest(GardenWebhookBase):
    BODY = {"scene": "garden", "text": "喵", "at": 1, "id": "1-1"}

    def test_missing_secret_is_401_without_delivery(self) -> None:
        h = self.handler(self.BODY, secret=None)
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 401)
        self.assertEqual(h.append_calls, [])
        self.assertEqual(h.injection_calls, [])

    def test_wrong_secret_is_403_without_delivery(self) -> None:
        h = self.handler(self.BODY, secret="wrong-secret-0123456789abcdef")
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 403)
        self.assertEqual(h.append_calls, [])
        self.assertEqual(h.injection_calls, [])

    def test_unconfigured_secret_file_fails_closed(self) -> None:
        self.secret_file.unlink()
        h = self.handler(self.BODY)
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 401)
        self.assertEqual(h.append_calls, [])

    def test_group_readable_secret_file_rejected(self) -> None:
        os.chmod(self.secret_file, 0o644)
        h = self.handler(self.BODY)
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 401)
        self.assertEqual(h.append_calls, [])

    def test_app_shared_secret_is_not_accepted(self) -> None:
        h = self.handler(self.BODY, secret="app-shared-secret")
        h.headers["X-Auth-Token"] = "app-shared-secret"
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 403)

    def test_do_post_routes_without_x_auth_token(self) -> None:
        h = self.handler(self.BODY)
        routed = []
        h._handle_garden_webhook = lambda: routed.append(True)
        h._require_write_auth = lambda: self.fail("garden webhook must not need X-Auth-Token")
        h.do_POST()
        self.assertEqual(routed, [True])


class GardenWebhookDeliveryTest(GardenWebhookBase):
    def test_delivers_user_bubble_and_injects_with_hint(self) -> None:
        h = self.handler({"scene": "cathome", "text": "  小克在吗  ", "at": 1760000000000, "id": "1760000000000-7"})
        h._handle_garden_webhook()
        status, payload = h.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(h.append_calls), 1)
        rec = h.append_calls[0]
        self.assertEqual(rec["role"], "user")
        self.assertEqual(rec["text"], PREFIX + "小克在吗")
        self.assertEqual(rec["source"], "garden")
        self.assertEqual(rec["metadata"]["source"], "garden")
        self.assertEqual(
            rec["metadata"]["garden"],
            {"id": "1760000000000-7", "scene": "cathome", "at": 1760000000000},
        )
        self.assertNotIn("garden_cli", rec["text"])
        # 同 App 用户消息: CCC_APP_TURN 直注 tmux + turn 登记
        self.assertEqual(len(h.injection_calls), 1)
        args, kwargs = h.injection_calls[0]
        self.assertEqual(args[0], "cctg")
        self.assertTrue(kwargs["force_direct_tmux"])
        self.assertRegex(args[1], INJECTED_RE + re.escape(PREFIX + "小克在吗") + r"\n（回复请用 python3 /root/.rainholm/garden_cli.py garden_action ")
        self.assertTrue(args[1].endswith("会自动同步到 app）"))
        self.assertTrue(h.state.typing_state["is_typing"])
        self.assertEqual(payload["turn"]["transport"], "tmux")
        # 提示只在本次委托里生效
        self.assertEqual(getattr(h, "_garden_injection_hint", ""), "")

    def test_duplicate_id_delivered_once(self) -> None:
        body = {"scene": "garden", "text": "重复", "at": 5, "id": "5-1"}
        first = self.handler(body)
        first._handle_garden_webhook()
        self.assertEqual(first.responses[-1][0], 200)
        retry = self.handler(body)
        retry._handle_garden_webhook()
        status, payload = retry.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["duplicate"])
        self.assertEqual(retry.append_calls, [])
        self.assertEqual(retry.injection_calls, [])
        other = self.handler({**body, "id": "5-2"})
        other._handle_garden_webhook()
        self.assertEqual(len(other.append_calls), 1)

    def test_numeric_id_accepted(self) -> None:
        h = self.handler({"text": "数字id", "id": 42})
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 200)
        self.assertEqual(h.append_calls[0]["metadata"]["garden"], {"id": "42", "scene": "garden"})

    def test_busy_without_channel_releases_id_for_retry(self) -> None:
        body = {"text": "忙", "id": "9-9"}
        busy = self.handler(body, active=True, channel=False)
        busy._handle_garden_webhook()
        self.assertEqual(busy.responses[-1][0], 409)
        self.assertEqual(busy.append_calls, [])
        again = self.handler(body)
        again._handle_garden_webhook()
        self.assertEqual(again.responses[-1][0], 200)
        self.assertEqual(len(again.append_calls), 1)

    def test_busy_queues_via_channel_with_hint(self) -> None:
        h = self.handler({"text": "排队", "id": "8-8"}, active=True, channel=True)
        h._handle_garden_webhook()
        status, payload = h.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["queued"])
        self.assertEqual(h.append_calls[0]["text"], PREFIX + "排队")
        self.assertEqual(h.injection_calls, [])
        queued = h.channel_calls[0]["text"]
        self.assertTrue(queued.startswith(PREFIX + "排队\n（回复请用 python3 /root/.rainholm/garden_cli.py"))

    def test_text_truncated_to_500(self) -> None:
        h = self.handler({"text": "花" * 900, "id": "t-1"})
        h._handle_garden_webhook()
        self.assertEqual(h.append_calls[0]["text"], PREFIX + "花" * 500)

    def test_empty_text_and_missing_id_rejected(self) -> None:
        h = self.handler({"text": "   ", "id": "e-1"})
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 400)
        h = self.handler({"text": "x"})
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 400)
        self.assertEqual(h.append_calls, [])

    def test_oversized_body_rejected(self) -> None:
        h = self.handler({"text": "x" * 9000, "id": "big"})
        h._handle_garden_webhook()
        self.assertEqual(h.responses[-1][0], 413)
        self.assertEqual(h.append_calls, [])


class RecentIdSetTest(unittest.TestCase):
    def test_bounded(self) -> None:
        seen = push._RecentIdSet(max_items=2)
        self.assertTrue(seen.reserve("a"))
        self.assertTrue(seen.reserve("b"))
        self.assertTrue(seen.reserve("c"))  # evicts "a"
        self.assertFalse(seen.reserve("c"))
        self.assertTrue(seen.reserve("a"))


if __name__ == "__main__":
    unittest.main()
