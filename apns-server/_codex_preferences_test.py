from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import time
import threading
import types
import unittest
from unittest import mock

from codex_app_bridge import CodexAppBridge, CodexAppBridgeError
from codex_app_bridge import QIAOKAIROS_REMOTE_COMPAT_LOCK_OWNER
from codex_preferences import (
    CodexModelCapability,
    CodexPreferenceError,
    CodexPreferencePersistenceError,
    CodexPreferenceStore,
    parse_codex_model_catalog,
    validate_codex_selection,
)
from push import (
    PushHandler,
    TOOLBOT_EFFORT_LEVELS,
    TOOLBOT_MODEL_ALIASES,
    TOOLBOT_MODEL_ALLOWLIST,
    _STATIC_MODEL_MENU,
    _build_model_menu,
    _canonicalize_cached_model_menu,
    get_dynamic_model_menu,
)


def catalog_payload() -> dict:
    return {
        "data": [
            {
                "id": "gpt-real",
                "displayName": "GPT Real",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                ],
            },
            {
                "model": "gpt-model-field",
                "displayName": "Model Field",
                "defaultReasoningEffort": "xhigh",
                "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            },
            {
                "id": "hidden-model",
                "hidden": True,
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            },
        ],
        "nextCursor": None,
    }


class CatalogValidationTest(unittest.TestCase):
    def test_parser_uses_real_id_or_model_fields_without_guessed_entries(self) -> None:
        parsed = parse_codex_model_catalog(catalog_payload())
        self.assertEqual([item.id for item in parsed], ["gpt-real", "gpt-model-field"])
        self.assertEqual(parsed[0].supported_reasoning_efforts, ("low", "medium", "high"))
        self.assertEqual(parsed[1].default_reasoning_effort, "xhigh")

    def test_selection_is_model_specific_and_strict(self) -> None:
        parsed = parse_codex_model_catalog(catalog_payload())
        self.assertEqual(validate_codex_selection("gpt-real", "high", parsed), ("gpt-real", "high"))
        with self.assertRaises(CodexPreferenceError):
            validate_codex_selection("invented", "high", parsed)
        with self.assertRaises(CodexPreferenceError):
            validate_codex_selection("gpt-model-field", "high", parsed)

    def test_empty_or_malformed_catalog_fails_instead_of_guessing(self) -> None:
        with self.assertRaises(CodexPreferenceError):
            parse_codex_model_catalog({"data": []})
        with self.assertRaises(CodexPreferenceError):
            parse_codex_model_catalog({"models": ["gpt-guessed"]})


class PreferenceStoreTest(unittest.TestCase):
    def test_replace_failure_keeps_old_disk_and_memory_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex_preferences.json"
            store = CodexPreferenceStore(path, default_model="default", default_effort="low")
            store.save_validated("old-model", "medium")
            with mock.patch("codex_preferences.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(CodexPreferencePersistenceError):
                    store.save_validated("new-model", "high")
            self.assertEqual(store.snapshot(), ("old-model", "medium"))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((persisted["model"], persisted["reasoning_effort"]), ("old-model", "medium"))

    def test_success_does_not_need_a_fallible_post_replace_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex_preferences.json"
            store = CodexPreferenceStore(path, default_model="default", default_effort="low")
            with mock.patch.object(Path, "chmod", side_effect=AssertionError("late chmod")):
                store.save_validated("saved-model", "high")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(store.snapshot(), (persisted["model"], persisted["reasoning_effort"]))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_turn_snapshot_stays_immutable_after_next_selection_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CodexPreferenceStore(
                Path(tmp) / "codex_preferences.json",
                default_model="old-model",
                default_effort="low",
            )
            admitted_turn = store.snapshot()
            store.save_validated("next-model", "high")
            self.assertEqual(admitted_turn, ("old-model", "low"))
            self.assertEqual(store.snapshot(), ("next-model", "high"))

    def test_atomic_private_persistence_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens" / "codex_preferences.json"
            store = CodexPreferenceStore(path, default_model="default-model", default_effort="low")
            store.save_validated("gpt-real", "high")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            reloaded = CodexPreferenceStore(path, default_model="other", default_effort="medium")
            self.assertEqual(reloaded.snapshot(), ("gpt-real", "high"))
            self.assertEqual(list(path.parent.glob(".*.tmp-*")), [])

    def test_invalid_or_symlink_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = root / "invalid.json"
            invalid.write_text('{"version":1,"model":"bad model","reasoning_effort":"high"}')
            store = CodexPreferenceStore(invalid, default_model="safe", default_effort="medium")
            self.assertEqual(store.snapshot(), ("safe", "medium"))
            target = root / "target.json"
            target.write_text(json.dumps({"version": 1, "model": "stolen", "reasoning_effort": "high"}))
            link = root / "link.json"
            link.symlink_to(target)
            linked = CodexPreferenceStore(link, default_model="safe", default_effort="low")
            self.assertEqual(linked.snapshot(), ("safe", "low"))


class BridgeModelListTest(unittest.TestCase):
    def test_model_list_pages_without_thread_operations(self) -> None:
        bridge = CodexAppBridge(command=["unused"])
        bridge._ensure_connected = mock.Mock()
        bridge._rpc_request = mock.Mock(side_effect=[
            {"data": [{"id": "one"}], "nextCursor": "next"},
            {"data": [{"id": "one"}, {"model": "two"}], "nextCursor": None},
        ])
        result = bridge.list_models(cwd=Path("/tmp"), timeout=2)
        self.assertEqual([item.get("id") or item.get("model") for item in result["data"]], ["one", "two"])
        methods = [call.args[0] for call in bridge._rpc_request.call_args_list]
        self.assertEqual(methods, ["model/list", "model/list"])
        self.assertEqual(bridge._rpc_request.call_args_list[1].args[1]["cursor"], "next")

    def test_invalid_model_list_result_is_clear_error(self) -> None:
        bridge = CodexAppBridge(command=["unused"])
        bridge._ensure_connected = mock.Mock()
        bridge._rpc_request = mock.Mock(return_value={"models": []})
        with self.assertRaises(CodexAppBridgeError):
            bridge.list_models(cwd=Path("/tmp"), timeout=2)


class FakeBridge:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or catalog_payload()
        self.error = error
        self.calls = 0

    def list_models(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class PreferencesHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = CodexPreferenceStore(
            Path(self.tmp.name) / "tokens" / "codex_preferences.json",
            default_model="gpt-real",
            default_effort="medium",
        )
        self.handler = object.__new__(PushHandler)
        self.handler.state = types.SimpleNamespace(
            codex_preferences=store,
            codex_model="gpt-real",
            codex_reasoning_effort="medium",
            codex_model_catalog_lock=threading.Lock(),
            codex_model_catalog=(),
            codex_model_catalog_at=0.0,
            codex_model_catalog_ttl_sec=30.0,
            codex_app_bridge=FakeBridge(),
        )
        self.handler.path = "/codex/preferences"
        self.handler.responses = []
        self.handler._send_json = lambda status, payload: self.handler.responses.append((status, payload))
        self.handler._load_codex_target = lambda: (None, Path("/tmp"))
        self.handler._codex_allowed_cwd = lambda cwd: Path(cwd)
        self.handler._codex_busy_snapshot = lambda: {"busy": True}

    def test_get_and_post_return_agreed_full_contract(self) -> None:
        self.handler._handle_codex_preferences_get()
        status, payload = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["applies_from"], "next_turn")
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["models"][0], {
            "id": "gpt-real",
            "display_name": "GPT Real",
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": ["low", "medium", "high"],
        })
        self.handler._handle_codex_preferences_post({
            "model": "gpt-model-field", "reasoning_effort": "xhigh",
        })
        status, payload = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["selection"], {
            "model": "gpt-model-field", "reasoning_effort": "xhigh",
        })
        self.assertEqual(self.handler.state.codex_preferences.snapshot(), ("gpt-model-field", "xhigh"))

    def test_post_rejects_unknown_model_and_model_unsupported_effort(self) -> None:
        before = self.handler.state.codex_preferences.snapshot()
        self.handler._handle_codex_preferences_post({"model": "invented", "reasoning_effort": "high"})
        self.assertEqual(self.handler.responses[-1][0], 400)
        self.assertEqual(self.handler.responses[-1][1]["error"], "invalid_model")
        self.handler._handle_codex_preferences_post({"model": "gpt-model-field", "reasoning_effort": "high"})
        self.assertEqual(self.handler.responses[-1][0], 400)
        self.assertEqual(self.handler.responses[-1][1]["error"], "unsupported_reasoning_effort")
        self.assertEqual(self.handler.state.codex_preferences.snapshot(), before)

    def test_get_uses_labeled_stale_dynamic_cache_but_post_fails_closed(self) -> None:
        self.handler._handle_codex_preferences_get()
        self.handler.state.codex_model_catalog_at = 0.0
        self.handler.state.codex_app_bridge = FakeBridge(error=CodexAppBridgeError("offline"))
        self.handler._handle_codex_preferences_get()
        status, payload = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["catalog_cached"])
        self.assertEqual(payload["catalog_error"], "codex_model_catalog_refresh_failed")
        self.handler._handle_codex_preferences_post({"model": "gpt-real", "reasoning_effort": "high"})
        self.assertEqual(self.handler.responses[-1][0], 503)
        self.assertEqual(self.handler.responses[-1][1]["error"], "codex_model_catalog_unavailable")


class ToolbotEffortTest(unittest.TestCase):
    def test_exact_allowlist_and_fixed_injection(self) -> None:
        handler = object.__new__(PushHandler)
        calls = []
        handler._inject_to_session = lambda session, text, **kwargs: (calls.append((session, text, kwargs)) or (True, ""))
        ok, _text = handler._run_toolbot_command("effort", "xhigh")
        self.assertTrue(ok)
        self.assertEqual(calls[0][0:2], ("cctg", "/effort xhigh"))
        for invalid in ("minimal", "ultra", "high /model bad", ""):
            calls.clear()
            ok, _text = handler._run_toolbot_command("effort", invalid)
            self.assertFalse(ok)
            self.assertEqual(calls, [])

    def test_capabilities_and_preferences_routes_require_auth(self) -> None:
        def handler(path: str, method: str, headers: dict[str, str]):
            value = object.__new__(PushHandler)
            value.state = types.SimpleNamespace(
                allowed_ips=[], shared_secret="secret", strict_auth=True,
            )
            value.path = path
            value.command = method
            value.headers = headers
            value.responses = []
            value._send_json = lambda status, payload: value.responses.append((status, payload))
            value._read_body = mock.Mock(return_value={"model": "gpt-real", "reasoning_effort": "high"})
            value._handle_codex_preferences_get = mock.Mock()
            value._handle_codex_preferences_post = mock.Mock()
            return value

        unauth_get = handler("/toolbot/capabilities", "GET", {})
        unauth_get.do_GET()
        self.assertEqual(unauth_get.responses[-1][0], 401)
        authed_get = handler("/toolbot/capabilities", "GET", {"X-Auth-Token": "secret"})
        authed_get.do_GET()
        self.assertEqual(authed_get.responses[-1], (200, {
            "ok": True, "effort_levels": list(TOOLBOT_EFFORT_LEVELS),
        }))
        unauth_post = handler("/codex/preferences", "POST", {})
        unauth_post.do_POST()
        self.assertEqual(unauth_post.responses[-1][0], 401)
        unauth_post._read_body.assert_not_called()
        authed_post = handler("/codex/preferences", "POST", {"X-Auth-Token": "secret"})
        authed_post.do_POST()
        authed_post._handle_codex_preferences_post.assert_called_once()


class ToolbotFableModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = object.__new__(PushHandler)
        self.calls: list[tuple[str, str, dict]] = []
        self.handler._inject_to_session = lambda session, text, **kwargs: (
            self.calls.append((session, text, kwargs)) or (True, "")
        )

    def test_aliases_resolve_and_model_command_injects_fable(self) -> None:
        self.assertIn("fable", TOOLBOT_MODEL_ALLOWLIST)
        self.assertNotIn("claude-fable-5", TOOLBOT_MODEL_ALLOWLIST)
        for choice in ("fable", "fable5", "fable-5", "claude-fable-5"):
            with self.subTest(choice=choice):
                self.assertEqual(TOOLBOT_MODEL_ALIASES[choice], "fable")
                self.assertEqual(self.handler._resolve_toolbot_model(choice), "fable")
                ok, _result = self.handler._run_toolbot_command("model", choice)
                self.assertTrue(ok)
                self.assertEqual(self.calls.pop()[0:2], ("cctg", "/model fable"))

    def test_static_and_dynamic_menus_expose_only_fable(self) -> None:
        self.assertEqual(_STATIC_MODEL_MENU[0], {
            "alias": "fable", "label": "Fable 5", "id": "fable",
        })
        live_menu = _build_model_menu(["claude-fable-5", "claude-opus-5"])
        cached_menu = _canonicalize_cached_model_menu([
            {"alias": "fable5", "label": "Fable 5", "id": "claude-fable-5"},
            {"alias": "opus5", "label": "Opus 5", "id": "claude-opus-5"},
        ])
        for menu in (live_menu, cached_menu):
            fable_entries = [entry for entry in menu if entry["id"] == "fable"]
            self.assertEqual(fable_entries, [{"alias": "fable", "label": "Fable 5", "id": "fable"}])
            self.assertFalse(any(entry["id"] == "claude-fable-5" for entry in menu))

    def test_cached_dynamic_menu_is_canonicalized_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models_cache.json"
            cache.write_text(json.dumps({
                "fetched_at": time.time(),
                "menu": [{"alias": "fable5", "label": "Fable 5", "id": "claude-fable-5"}],
            }), encoding="utf-8")
            with mock.patch("push._MODELS_CACHE_PATH", cache):
                menu, source = get_dynamic_model_menu()
        self.assertEqual(source, "cache")
        self.assertEqual(menu, [{"alias": "fable", "label": "Fable 5", "id": "fable"}])


class ManualForgePreferenceTest(unittest.TestCase):
    def test_body_model_override_is_rejected_before_any_forge_work(self) -> None:
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace()
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._load_codex_target = mock.Mock(side_effect=AssertionError("must reject first"))
        handler._handle_codex_forge({"retain": 80, "model": "gpt-override"})
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(handler.responses[-1][1]["error"], "deprecated_model_override")
        handler._load_codex_target.assert_not_called()

    def test_manual_forge_worker_receives_store_snapshot_pair_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CodexPreferenceStore(
                root / "codex_preferences.json",
                default_model="stored-model",
                default_effort="xhigh",
            )
            handler = object.__new__(PushHandler)
            handler.state = types.SimpleNamespace(
                codex_preferences=store,
                codex_app_bridge=types.SimpleNamespace(snapshot=lambda: {"busy": False}),
            )
            handler.responses = []
            handler._send_json = lambda status, payload: handler.responses.append((status, payload))
            handler._load_codex_target = lambda: ("old-session", root)
            handler._codex_allowed_cwd = lambda cwd: Path(cwd)
            handler._codex_exec_processes = lambda **_kwargs: []
            fake_runs = types.SimpleNamespace(
                latest=lambda: None,
                start=mock.Mock(return_value=("run-id", threading.Event())),
            )
            with mock.patch("push.CODEX_RUNS", fake_runs), mock.patch("push.threading.Thread") as thread_cls:
                thread_cls.return_value.start = mock.Mock()
                handler._handle_codex_forge({"retain": 50})
            self.assertEqual(handler.responses[-1][0], 202)
            worker_args = thread_cls.call_args.kwargs["args"]
            self.assertEqual(worker_args[-1], ("stored-model", "xhigh"))
            self.assertEqual(len(worker_args), 6)


class SharedDaemonAdmissionTest(unittest.TestCase):
    """The App preflight must defer Remote arbitration to app-server."""

    @staticmethod
    def _handler(*, backend: str = "app-server", bridge_busy: bool = False) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            codex_kairos_backend=backend,
            codex_app_bridge=types.SimpleNamespace(snapshot=lambda: {"busy": bridge_busy}),
            codex_bin="/safe/codex",
        )
        handler._load_codex_target = lambda: ("shared-thread", Path("/safe/cwd"))
        handler._codex_exec_processes = lambda **_kwargs: []
        return handler

    def test_remote_tui_compat_lock_admits_app_server_even_when_observing_turn(self) -> None:
        handler = self._handler(bridge_busy=True)
        with mock.patch("push.prompt_lock_is_busy", return_value=False) as busy:
            self.assertFalse(handler._codex_session_busy("shared-thread"))
        busy.assert_called_once_with(
            "shared-thread", Path("/safe/cwd"),
            ignore_owner=QIAOKAIROS_REMOTE_COMPAT_LOCK_OWNER,
            expected_codex_bin="/safe/codex",
        )

    def test_standalone_or_malformed_lock_stays_queued(self) -> None:
        handler = self._handler()
        with mock.patch("push.prompt_lock_is_busy", return_value=True):
            self.assertTrue(handler._codex_session_busy("shared-thread"))

    def test_legacy_exec_backend_never_ignores_compat_lock(self) -> None:
        handler = self._handler(backend="legacy-exec")
        with mock.patch("push.prompt_lock_is_busy", return_value=True) as busy:
            self.assertTrue(handler._codex_session_busy("shared-thread"))
        busy.assert_called_once_with("shared-thread", Path("/safe/cwd"))

    def test_real_codex_exec_process_is_busy_even_for_remote_owner(self) -> None:
        handler = self._handler()
        handler._codex_exec_processes = lambda **_kwargs: [{"pid": 7}]
        with mock.patch("push.prompt_lock_is_busy", side_effect=AssertionError("must not ignore exec")):
            self.assertTrue(handler._codex_session_busy("shared-thread"))

    def test_real_codex_exec_in_shared_cwd_is_busy_when_session_scan_misses(self) -> None:
        handler = self._handler()
        handler._codex_exec_processes = lambda **kwargs: (
            [{"pid": 8}] if kwargs.get("cwd") is not None else []
        )
        with mock.patch("push.prompt_lock_is_busy", side_effect=AssertionError("must not ignore exec")):
            self.assertTrue(handler._codex_session_busy("shared-thread"))


if __name__ == "__main__":
    unittest.main()
