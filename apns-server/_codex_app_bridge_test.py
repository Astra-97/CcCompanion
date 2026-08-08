import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from websockets.sync.client import unix_connect as websocket_unix_connect
from websockets.sync.server import unix_serve

import codex_app_bridge as bridge_module
from codex_app_bridge import (
    CodexActiveTurnError,
    CodexAppBridge,
    CodexAppBridgeError,
    CodexPromptLockBusy,
    prompt_lock_is_busy,
    prompt_lock_path,
)


FAKE_APP_SERVER = r'''
import json
from pathlib import Path
import os
import sys
import time

log_path = Path(sys.argv[1])
thread_id = "thread-test"
turn_number = 0
active_turn = None
active_stubborn = False

def write(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def log(method, params):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "method": method, "params": params}) + "\n")

def token_usage(input_tokens):
    breakdown = {
        "inputTokens": input_tokens,
        "cachedInputTokens": 100,
        "outputTokens": 20,
        "reasoningOutputTokens": 5,
        "totalTokens": input_tokens + 25,
    }
    return {"total": breakdown, "last": breakdown, "modelContextWindow": 100000}

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params") or {}
    log(method, params)
    if request_id is None:
        continue
    if method == "initialize":
        write({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"thread": {"id": thread_id, "turns": []}},
        })
    elif method == "thread/resume":
        write({
            "jsonrpc": "2.0",
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "turnId": "old-turn",
                "tokenUsage": token_usage(99999),
            },
        })
        write({
            "jsonrpc": "2.0",
            "method": "thread/compacted",
            "params": {"threadId": thread_id, "turnId": "old-turn"},
        })
        write({
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": "old-turn",
                "item": {"id": "old-message", "type": "agentMessage", "text": "stale-answer", "phase": "final_answer"},
            },
        })
        write({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": "old-turn", "items": [], "status": "completed"},
            },
        })
        write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"thread": {"id": thread_id, "turns": []}},
        })
    elif method == "turn/start":
        turn_number += 1
        active_turn = f"turn-{turn_number}"
        text = "".join(
            item.get("text", "") for item in params.get("input", [])
            if item.get("type") == "text"
        )
        if text == "delayed-start":
            time.sleep(0.2)
        active_stubborn = text == "stubborn"
        write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"turn": {"id": active_turn, "items": [], "status": "inProgress"}},
        })
        for usage_thread_id, usage_turn_id, input_tokens in [
            ("wrong-thread", active_turn, 99000),
            (thread_id, "wrong-turn", 98000),
            (thread_id, active_turn, 70000),
            (thread_id, active_turn, 80000),
        ]:
            write({
                "jsonrpc": "2.0",
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": usage_thread_id,
                    "turnId": usage_turn_id,
                    "tokenUsage": token_usage(input_tokens),
                },
            })
        if text == "compacted":
            write({
                "jsonrpc": "2.0",
                "method": "thread/compacted",
                "params": {"threadId": thread_id, "turnId": active_turn},
            })
        write({
            "jsonrpc": "2.0",
            "method": "item/started",
            "params": {
                "threadId": thread_id,
                "turnId": active_turn,
                "item": {
                    "id": f"cmd-{turn_number}",
                    "type": "commandExecution",
                    "command": "printf SUPER_SECRET_OBSERVER_PAYLOAD",
                    "cwd": "/private/observer/path",
                },
            },
        })
        if text in {"slow", "stubborn", "delayed-start"}:
            continue
        item_id = f"msg-{turn_number}"
        for delta in ["bridge", "-ok"]:
            write({
                "jsonrpc": "2.0",
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": active_turn,
                    "itemId": item_id,
                    "delta": delta,
                },
            })
        write({
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": active_turn,
                "item": {"id": item_id, "type": "agentMessage", "text": "bridge-ok", "phase": None},
            },
        })
        write({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": active_turn, "items": [], "status": "completed"},
            },
        })
    elif method == "turn/interrupt":
        write({"jsonrpc": "2.0", "id": request_id, "result": {}})
        if active_stubborn:
            continue
        write({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": active_turn, "items": [], "status": "interrupted"},
            },
        })
    else:
        write({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "unsupported"},
        })
'''

CHILD_FAKE_APP_SERVER = r'''
import subprocess
import signal
import sys
from pathlib import Path
child = subprocess.Popen(
    [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(str(sys.argv[1]) + ".child").write_text(str(child.pid), encoding="utf-8")
''' + FAKE_APP_SERVER


class PromptLockCompatibilityTest(unittest.TestCase):
    """The shared daemon may coexist only with qiaokairos's marked lock."""

    def _held_lock(self, root: Path, payload: object):
        path = prompt_lock_path("shared-thread", root)
        lock_file = bridge_module._open_trusted_prompt_lock(path)
        bridge_module.fcntl.flock(lock_file.fileno(), bridge_module.fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps(payload, separators=(",", ":")))
        lock_file.flush()
        self.addCleanup(lock_file.close)
        return path

    @staticmethod
    def _metadata() -> dict[str, object]:
        return {
            "pid": 101,
            "pid_starttime": 202,
            "uid": os.geteuid(),
            "owner": "qiaokairos-interactive",
            "started_at": int(time.time()),
            "session_id": "shared-thread",
            "cwd": "/safe/cwd",
            "codex_bin": "/safe/codex",
            "supervisor_cwd": "/safe/supervisor",
            "supervisor_exe": "/safe/python",
            "supervisor_argv": ["/safe/python", "/safe/qiaokairos.py"],
            "process_identity": "101:202",
        }

    def test_exact_qiaokairos_owner_is_the_only_compatible_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_PROMPT_LOCK_DIR": tmp}, clear=False,
        ):
            root = Path(tmp)
            self._held_lock(root, self._metadata())
            with mock.patch.object(
                bridge_module, "_qiaokairos_lock_holder_is_verified", return_value=True,
            ) as verified:
                self.assertFalse(prompt_lock_is_busy(
                    "shared-thread", root, ignore_owner="qiaokairos-interactive",
                    expected_codex_bin="/safe/codex",
                ))
            verified.assert_called_once()
            self.assertTrue(prompt_lock_is_busy(
                "shared-thread", root, ignore_owner="some-other-owner",
            ))
            self.assertTrue(prompt_lock_is_busy("shared-thread", root))

    def test_unknown_or_legacy_locked_metadata_fails_closed(self) -> None:
        cases = [
            {"pid": os.getpid(), "started_at": int(time.time())},
            {**self._metadata(), "owner": "someone-else"},
            {**self._metadata(), "pid": "not-an-int"},
            {"owner": "qiaokairos-interactive"},
            "not-a-json-object",
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"CODEX_PROMPT_LOCK_DIR": tmp}, clear=False,
            ):
                root = Path(tmp)
                self._held_lock(root, payload)
                self.assertTrue(prompt_lock_is_busy(
                    "shared-thread", root, ignore_owner="qiaokairos-interactive",
                ))

    def test_untrusted_lock_directory_fails_closed_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_PROMPT_LOCK_DIR": tmp}, clear=False,
        ):
            root = Path(tmp)
            root.chmod(0o777)
            self.assertTrue(prompt_lock_is_busy(
                "shared-thread", root, ignore_owner="qiaokairos-interactive",
            ))

    def test_idle_legacy_permissions_are_safely_migrated_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_PROMPT_LOCK_DIR": tmp}, clear=False,
        ):
            root = Path(tmp)
            path = prompt_lock_path("shared-thread", root)
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            root.chmod(0o755)
            self.assertFalse(prompt_lock_is_busy(
                "shared-thread", root, ignore_owner="qiaokairos-interactive",
            ))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_held_legacy_permissions_migrate_then_verify_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"CODEX_PROMPT_LOCK_DIR": tmp}, clear=False,
        ):
            root = Path(tmp)
            path = self._held_lock(root, self._metadata())
            root.chmod(0o755)
            path.chmod(0o644)
            with mock.patch.object(
                bridge_module, "_qiaokairos_lock_holder_is_verified", return_value=True,
            ):
                self.assertFalse(prompt_lock_is_busy(
                    "shared-thread", root, ignore_owner="qiaokairos-interactive",
                    expected_codex_bin="/safe/codex",
                ))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_missing_fcntl_fails_closed(self) -> None:
        with mock.patch.object(bridge_module, "fcntl", None):
            self.assertTrue(prompt_lock_is_busy("shared-thread", Path("/tmp")))


class QiaokairosHolderVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.script = self.root / "qiaokairos.py"
        self.binary = self.root / "codex"
        self.interpreter = self.root / "python"
        for path in (self.script, self.binary, self.interpreter):
            path.write_text("trusted", encoding="utf-8")
            path.chmod(0o700)
        self.pid = 101
        self.child_pid = 202
        self.metadata = {
            "pid": self.pid,
            "pid_starttime": 303,
            "uid": os.geteuid(),
            "owner": "qiaokairos-interactive",
            "started_at": 1,
            "session_id": "shared-thread",
            "cwd": str(self.cwd),
            "codex_bin": str(self.binary),
            "supervisor_cwd": str(self.root),
            "supervisor_exe": str(self.interpreter),
            "supervisor_argv": [str(self.interpreter), str(self.script)],
            "process_identity": f"{self.pid}:303",
        }
        self.supervisor = bridge_module._ProcSnapshot(
            pid=self.pid,
            starttime=303,
            uid=os.geteuid(),
            argv=(str(self.interpreter), str(self.script)),
            executable=str(self.interpreter),
            cwd=str(self.root),
        )
        self.child = bridge_module._ProcSnapshot(
            pid=self.child_pid,
            starttime=404,
            uid=os.geteuid(),
            argv=(
                str(self.binary), "resume", "--remote", "unix://", "--include-non-interactive",
                "--cd", str(self.cwd), "shared-thread",
            ),
            executable=str(self.binary),
            cwd=str(self.cwd),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _verify(self, *, holders: object = None, supervisor: object = None, child: object = None,
                children: object = None, metadata: object = None) -> bool:
        holder_values = holders if holders is not None else [[self.pid], [self.pid]]
        with mock.patch.object(bridge_module, "QIAOKAIROS_REMOTE_SCRIPT", self.script), mock.patch.object(
            bridge_module, "_flock_holder_pids", side_effect=holder_values,
        ), mock.patch.object(
            bridge_module, "_trusted_codex_release_executable",
            return_value=(str(self.binary), 11, 12),
        ), mock.patch.object(
            bridge_module, "_read_proc_snapshot",
            side_effect=[supervisor if supervisor is not None else self.supervisor,
                         child if child is not None else self.child],
        ), mock.patch.object(
            bridge_module, "_direct_child_pids",
            return_value=children if children is not None else [self.child_pid],
        ):
            return bridge_module._qiaokairos_lock_holder_is_verified(
                mock.sentinel.lock,
                metadata=metadata if metadata is not None else self.metadata,
                session_id="shared-thread",
                cwd=self.cwd,
                expected_codex_bin=self.binary,
            )

    def test_accepts_only_kernel_bound_real_qiaokairos_remote_child(self) -> None:
        self.assertTrue(self._verify())

    def test_accepts_env_shebang_and_verified_python3_basename_forms(self) -> None:
        # A normal env/shebang launch exposes the resolved interpreter path.
        self.assertTrue(self._verify())
        metadata = dict(self.metadata)
        metadata["supervisor_argv"] = ["python3", str(self.script)]
        supervisor = dataclasses.replace(self.supervisor, argv=("python3", str(self.script)))
        self.assertTrue(self._verify(metadata=metadata, supervisor=supervisor))
        metadata["supervisor_argv"] = ["python3", str(self.script), "--no-wait"]
        supervisor = dataclasses.replace(
            self.supervisor, argv=("python3", str(self.script), "--no-wait"),
        )
        self.assertTrue(self._verify(metadata=metadata, supervisor=supervisor))

    def test_rejects_owner_pid_mismatch_and_proc_race(self) -> None:
        self.assertFalse(self._verify(holders=[[999], [999]]))
        self.assertFalse(self._verify(holders=[[self.pid], [999]]))
        self.assertFalse(self._verify(holders=[None]))

    def test_rejects_wrong_supervisor_or_child_identity(self) -> None:
        fake_supervisor = dataclasses.replace(self.supervisor, argv=("python", "fake.py"))
        self.assertFalse(self._verify(supervisor=fake_supervisor))
        fake_remote = dataclasses.replace(self.child, argv=(str(self.binary), "resume"))
        self.assertFalse(self._verify(child=fake_remote))
        wrong_session = dataclasses.replace(
            self.child, argv=(*self.child.argv[:-1], "other-thread"),
        )
        self.assertFalse(self._verify(child=wrong_session))
        wrong_cwd = dataclasses.replace(self.child, cwd=str(self.root))
        self.assertFalse(self._verify(child=wrong_cwd))
        self.assertFalse(self._verify(children=[]))

    def test_rejects_tampered_supervisor_argv_interpreter_and_script_order(self) -> None:
        extra_arg = dataclasses.replace(
            self.supervisor, argv=(*self.supervisor.argv, "--unexpected"),
        )
        self.assertFalse(self._verify(supervisor=extra_arg))
        reversed_args = dataclasses.replace(
            self.supervisor, argv=(str(self.script), str(self.interpreter)),
        )
        self.assertFalse(self._verify(supervisor=reversed_args))
        other_interpreter = self.root / "other-python"
        other_interpreter.write_text("trusted", encoding="utf-8")
        other_interpreter.chmod(0o700)
        different_interpreter = dataclasses.replace(
            self.supervisor,
            argv=(str(other_interpreter), str(self.script)),
            executable=str(other_interpreter),
        )
        self.assertFalse(self._verify(supervisor=different_interpreter))

    def test_rejects_fake_basename_and_metadata_consistent_wrong_executable(self) -> None:
        fake_basename = dict(self.metadata)
        fake_basename["supervisor_argv"] = ["sh", str(self.script)]
        fake_supervisor = dataclasses.replace(self.supervisor, argv=("sh", str(self.script)))
        self.assertFalse(self._verify(metadata=fake_basename, supervisor=fake_supervisor))
        wrong_exe = self.root / "not-python"
        wrong_exe.write_text("trusted", encoding="utf-8")
        wrong_exe.chmod(0o700)
        wrong_metadata = dict(self.metadata)
        wrong_metadata["supervisor_exe"] = str(wrong_exe)
        wrong_metadata["supervisor_argv"] = [str(wrong_exe), str(self.script)]
        wrong_supervisor = dataclasses.replace(
            self.supervisor,
            executable=str(wrong_exe),
            argv=(str(wrong_exe), str(self.script)),
        )
        self.assertFalse(self._verify(metadata=wrong_metadata, supervisor=wrong_supervisor))


class CodexReleaseChainTest(unittest.TestCase):
    def test_real_readonly_shims_resolve_to_one_trusted_release_inode(self) -> None:
        configured = bridge_module._trusted_codex_release_executable("/usr/bin/codex")
        self.assertIsNotNone(configured)
        self.assertTrue(bridge_module._same_trusted_codex_release("/root/.local/bin/codex", configured))
        self.assertFalse(bridge_module._same_trusted_codex_release("/bin/true", configured))

    def test_group_writable_release_chain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / "codex-home"
            target = anchor / "packages" / "release" / "bin" / "codex"
            target.parent.mkdir(parents=True)
            target.write_text("trusted", encoding="utf-8")
            target.chmod(0o700)
            with mock.patch.object(bridge_module, "CODEX_RELEASE_TRUST_ANCHOR", anchor):
                self.assertIsNotNone(bridge_module._trusted_codex_release_executable(target))
                (anchor / "packages").chmod(0o775)
                self.assertIsNone(bridge_module._trusted_codex_release_executable(target))


class CodexAppBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_path = self.root / "fake_app_server.py"
        self.server_path.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.log_path = self.root / "rpc.jsonl"
        self.bridge = CodexAppBridge(
            command=[sys.executable, str(self.server_path), str(self.log_path)],
            codex_home=str(self.root / "codex-home"),
            request_timeout_sec=2.0,
        )

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def methods(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line)["method"] for line in self.log_path.read_text().splitlines()]

    def rpc_entries(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def run_normal(self, thread_id=None, marker_provider=None):
        updates = []
        activities = []
        result = self.bridge.run_turn(
            thread_id=thread_id,
            cwd=self.root,
            prompt="normal",
            model="gpt-test",
            effort="high",
            on_update=updates.append,
            on_activity=activities.append,
            marker_provider=marker_provider,
            max_runtime_sec=3.0,
        )
        return result, updates, activities

    def test_start_resume_stream_and_activity(self):
        first, updates, activities = self.run_normal()
        self.assertEqual(first.thread_id, "thread-test")
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.text, "bridge-ok")
        self.assertIsNotNone(first.token_usage)
        self.assertEqual(first.token_usage.last.input_tokens, 80000)
        self.assertEqual(first.token_usage.model_context_window, 100000)
        self.assertIn("bridge-ok", updates)
        self.assertIn("运行命令", activities)

        second, _, _ = self.run_normal(first.thread_id)
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.text, "bridge-ok")
        self.assertEqual(second.token_usage.last.input_tokens, 80000)
        self.assertFalse(second.context_compacted)
        methods = self.methods()
        self.assertEqual(methods.count("initialize"), 1)
        self.assertIn("thread/start", methods)
        self.assertIn("thread/resume", methods)
        self.assertEqual(methods.count("turn/start"), 2)
        thread_entries = [
            entry for entry in self.rpc_entries()
            if entry["method"] in {"thread/start", "thread/resume"}
        ]
        self.assertTrue(all(
            "model_auto_compact_token_limit" not in entry["params"]["config"]
            for entry in thread_entries
        ))

    def test_default_transport_uses_shared_unix_websocket(self):
        socket_path = self.root / "daemon.sock"
        methods = []
        turn_start_params = []
        daemon_thread_id = "thread-daemon"

        def handler(websocket):
            for raw in websocket:
                message = json.loads(raw)
                method = message.get("method", "")
                methods.append(method)
                request_id = message.get("id")
                if request_id is None:
                    continue
                if method == "initialize":
                    # Real remote-control daemons emit this before the RPC
                    # response; the bridge must continue matching by id.
                    websocket.send(json.dumps({
                        "method": "remoteControl/status/changed",
                        "params": {"status": "connected"},
                    }))
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"userAgent": "fake-daemon"},
                    }))
                elif method == "thread/start":
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"thread": {"id": daemon_thread_id, "turns": []}},
                    }))
                elif method == "turn/start":
                    turn_start_params.append(message.get("params") or {})
                    turn_id = "turn-daemon"
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
                    }))
                    websocket.send(json.dumps({
                        "method": "item/completed",
                        "params": {
                            "threadId": daemon_thread_id,
                            "turnId": turn_id,
                            "item": {
                                "id": "message-daemon",
                                "type": "agentMessage",
                                "text": "daemon-ok",
                            },
                        },
                    }))
                    websocket.send(json.dumps({
                        "method": "turn/completed",
                        "params": {
                            "threadId": daemon_thread_id,
                            "turn": {"id": turn_id, "status": "completed", "items": []},
                        },
                    }))

        server = unix_serve(handler, path=str(socket_path), compression=None)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        daemon_bridge = CodexAppBridge(
            codex_home=str(self.root / "daemon-home"),
            daemon_socket_path=str(socket_path),
            daemon_autostart=False,
            request_timeout_sec=2.0,
        )
        try:
            with mock.patch("codex_app_bridge.subprocess.run") as starter, mock.patch(
                "codex_app_bridge._PromptProcessLock.acquire",
                side_effect=AssertionError("shared daemon must not take the legacy prompt lock"),
            ), mock.patch(
                "codex_app_bridge.unix_connect", wraps=websocket_unix_connect,
            ) as connector:
                result = daemon_bridge.run_turn(
                    thread_id=None,
                    cwd=self.root,
                    prompt="normal",
                    model="gpt-test",
                    effort="high",
                )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.text, "daemon-ok")
            self.assertEqual(result.thread_id, daemon_thread_id)
            self.assertIsNone(daemon_bridge.snapshot()["pid"])
            starter.assert_not_called()
            self.assertIs(connector.call_args.kwargs["compression"], None)
            self.assertEqual(methods.count("initialize"), 1)
            self.assertIn("initialized", methods)
            self.assertEqual(len(turn_start_params), 1)
            uuid.UUID(turn_start_params[0]["clientUserMessageId"])
        finally:
            daemon_bridge.close()
            # Closing a bridge connection must not stop the shared daemon.
            self.assertTrue(server_thread.is_alive())
            server.shutdown()
            server_thread.join(timeout=2.0)

    def test_post_submit_cancel_is_bounded_uncertain_and_never_resubmits(self):
        for submit_method in ("turn/start", "turn/steer"):
            with self.subTest(submit_method=submit_method):
                socket_path = self.root / f"never-reply-{submit_method.split('/')[-1]}.sock"
                methods = []
                submit_seen = threading.Event()
                daemon_thread_id = f"thread-never-{submit_method.split('/')[-1]}"
                existing_turn_id = "turn-existing"

                def handler(websocket):
                    for raw in websocket:
                        message = json.loads(raw)
                        method = str(message.get("method") or "")
                        methods.append(method)
                        request_id = message.get("id")
                        if request_id is None:
                            continue
                        if method == "initialize":
                            websocket.send(json.dumps({
                                "id": request_id,
                                "result": {"userAgent": "fake-daemon"},
                            }))
                        elif method == "thread/resume":
                            turns = []
                            if submit_method == "turn/steer":
                                turns = [{
                                    "id": existing_turn_id,
                                    "status": "inProgress",
                                    "items": [],
                                }]
                            websocket.send(json.dumps({
                                "id": request_id,
                                "result": {
                                    "thread": {
                                        "id": daemon_thread_id,
                                        "turns": turns,
                                    }
                                },
                            }))
                        elif method == submit_method:
                            # The request was accepted by the transport, but
                            # the daemon never reveals whether it took effect.
                            submit_seen.set()
                        elif method == "turn/interrupt":
                            websocket.send(json.dumps({
                                "id": request_id,
                                "result": {},
                            }))

                server = unix_serve(handler, path=str(socket_path), compression=None)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                bridge = CodexAppBridge(
                    codex_home=str(self.root / f"never-home-{submit_method.split('/')[-1]}"),
                    daemon_socket_path=str(socket_path),
                    daemon_autostart=False,
                    request_timeout_sec=30.0,
                )
                holder = {}

                def run():
                    holder["result"] = bridge.run_turn(
                        thread_id=daemon_thread_id,
                        cwd=self.root,
                        prompt="execute at most once",
                        model="gpt-test",
                        effort="high",
                    )

                worker = threading.Thread(target=run)
                try:
                    worker.start()
                    self.assertTrue(submit_seen.wait(1.0))
                    cancelled_at = time.monotonic()
                    self.assertTrue(bridge.interrupt_active(timeout=0.5))
                    worker.join(timeout=1.0)
                    elapsed = time.monotonic() - cancelled_at
                    self.assertFalse(worker.is_alive())
                    self.assertLess(elapsed, 0.8)
                    self.assertEqual(holder["result"].status, "uncertain")
                    self.assertEqual(methods.count(submit_method), 1)
                    self.assertEqual(
                        methods.count("turn/start") + methods.count("turn/steer"),
                        1,
                    )
                finally:
                    bridge.close()
                    worker.join(timeout=1.0)
                    server.shutdown()
                    server_thread.join(timeout=2.0)

    def test_cancel_event_during_unanswered_steer_interrupts_exact_turn_once(self):
        socket_path = self.root / "never-reply-steer-cancel-event.sock"
        methods = []
        interrupt_params = []
        steer_seen = threading.Event()
        daemon_thread_id = "thread-steer-cancel-event"
        existing_turn_id = "turn-steer-cancel-event"

        def handler(websocket):
            for raw in websocket:
                message = json.loads(raw)
                method = str(message.get("method") or "")
                methods.append(method)
                request_id = message.get("id")
                if request_id is None:
                    continue
                if method == "initialize":
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"userAgent": "fake-daemon"},
                    }))
                elif method == "thread/resume":
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {
                            "thread": {
                                "id": daemon_thread_id,
                                "turns": [{
                                    "id": existing_turn_id,
                                    "status": "inProgress",
                                    "items": [],
                                }],
                            }
                        },
                    }))
                elif method == "turn/steer":
                    steer_seen.set()
                    # Never expose whether the steer was accepted.
                elif method == "turn/interrupt":
                    interrupt_params.append(message.get("params"))
                    # Also never answer the interrupt: the bridge must bound
                    # this best-effort exact-turn stop before returning.

        server = unix_serve(handler, path=str(socket_path), compression=None)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        bridge = CodexAppBridge(
            codex_home=str(self.root / "never-steer-cancel-event-home"),
            daemon_socket_path=str(socket_path),
            daemon_autostart=False,
            request_timeout_sec=30.0,
        )
        cancel_event = threading.Event()
        holder = {}

        def run():
            holder["result"] = bridge.run_turn(
                thread_id=daemon_thread_id,
                cwd=self.root,
                prompt="steer exactly once",
                model="gpt-test",
                effort="high",
                cancel_event=cancel_event,
            )

        worker = threading.Thread(target=run)
        try:
            worker.start()
            self.assertTrue(steer_seen.wait(1.0))
            cancelled_at = time.monotonic()
            cancel_event.set()
            worker.join(timeout=1.0)
            elapsed = time.monotonic() - cancelled_at
            self.assertFalse(worker.is_alive())
            self.assertLess(elapsed, 0.8)
            self.assertEqual(holder["result"].status, "uncertain")
            self.assertEqual(methods.count("turn/steer"), 1)
            self.assertEqual(methods.count("turn/start"), 0)
            self.assertEqual(methods.count("turn/interrupt"), 1)
            self.assertEqual(interrupt_params, [{
                "threadId": daemon_thread_id,
                "turnId": existing_turn_id,
            }])
        finally:
            bridge.close()
            worker.join(timeout=1.0)
            server.shutdown()
            server_thread.join(timeout=2.0)

    def test_unreachable_daemon_without_autostart_is_fallback_safe(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "unreachable-home"),
            daemon_autostart=False,
            request_timeout_sec=0.2,
        )
        try:
            with mock.patch(
                "codex_app_bridge.unix_connect", side_effect=OSError("missing socket"),
            ), mock.patch("codex_app_bridge.subprocess.run") as starter, mock.patch.object(
                bridge, "_run_supervisor_action_locked"
            ) as supervisor:
                with self.assertRaises(CodexAppBridgeError) as raised:
                    bridge.run_turn(
                        thread_id=None,
                        cwd=self.root,
                        prompt="normal",
                        model="gpt-test",
                        effort="high",
                    )
            self.assertTrue(raised.exception.fallback_safe)
            starter.assert_not_called()
            supervisor.assert_not_called()
        finally:
            bridge.close()

    def test_daemon_recovery_survives_stale_startup_lock_timeout(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "recover-home"),
            daemon_socket_path=str(self.root / "recover.sock"),
            daemon_autostart=True,
            daemon_recovery_timeout_sec=2.0,
            daemon_start_timeout_sec=0.1,
            daemon_connect_retry_sec=0.01,
            request_timeout_sec=0.2,
            daemon_supervisor_command=["fake-supervisor"],
        )
        websocket = mock.Mock()
        # First connect sees the restart gap. The start command then times out
        # behind Codex's startup lock, but the next bounded connect succeeds
        # after the independent restart owner releases/recreates the socket.
        with mock.patch(
            "codex_app_bridge.unix_connect",
            side_effect=[OSError("socket absent"), websocket],
        ) as connector, mock.patch.object(
            bridge,
            "_run_daemon_start_interruptible",
            side_effect=subprocess.TimeoutExpired(["codex", "remote-control", "start"], 0.1),
        ) as starter, mock.patch.object(
            bridge,
            "_initialize_connection",
        ), mock.patch.object(
            bridge,
            "_websocket_reader_loop",
        ):
            bridge._start_daemon_connection_locked(self.root)
        self.assertIs(bridge._websocket, websocket)
        self.assertEqual(connector.call_count, 2)
        starter.assert_called_once()
        bridge.close()

    def test_initialize_then_resume_eof_repairs_dead_daemon_once_before_submit(self):
        socket_path = self.root / "initialize-then-eof.sock"
        connection_count = [0]
        daemon_state = {"value": "live"}

        def handler(websocket):
            connection_count[0] += 1
            connection = connection_count[0]
            for raw in websocket:
                message = json.loads(raw)
                method = message.get("method")
                request_id = message.get("id")
                if method == "initialize":
                    websocket.send(json.dumps({"id": request_id, "result": {"userAgent": "fake"}}))
                elif method == "thread/resume" and connection == 1:
                    daemon_state["value"] = "dead"
                    websocket.close()
                    return
                elif method == "thread/resume":
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"thread": {"id": "thread-repaired", "turns": []}},
                    }))
                elif method == "turn/start":
                    websocket.send(json.dumps({
                        "id": request_id,
                        "result": {"turn": {"id": "turn-repaired", "status": "inProgress", "items": []}},
                    }))
                    websocket.send(json.dumps({
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-repaired",
                            "turnId": "turn-repaired",
                            "item": {"id": "answer", "type": "agentMessage", "text": "repaired"},
                        },
                    }))
                    websocket.send(json.dumps({
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-repaired",
                            "turn": {"id": "turn-repaired", "status": "completed", "items": []},
                        },
                    }))

        server = unix_serve(handler, path=str(socket_path), compression=None)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        bridge = CodexAppBridge(
            codex_home=str(self.root / "eof-home"),
            daemon_socket_path=str(socket_path),
            daemon_autostart=True,
            request_timeout_sec=1.0,
        )
        try:
            with mock.patch.object(bridge, "_daemon_pid_state", side_effect=lambda: daemon_state["value"]), mock.patch.object(
                bridge, "_systemd_takeover_verified", return_value=True
            ), mock.patch.object(
                bridge,
                "_run_supervisor_action_locked",
                return_value=True,
            ) as repair:
                result = bridge.run_turn(
                    thread_id="thread-repaired",
                    cwd=self.root,
                    prompt="repair without submitting twice",
                    model="gpt-test",
                    effort="high",
                )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.text, "repaired")
            self.assertEqual(connection_count[0], 2)
            repair.assert_called_once()
            self.assertEqual(repair.call_args.args[0], "restart")
        finally:
            bridge.close()
            server.shutdown()
            server_thread.join(timeout=2.0)

    def test_pre_submit_recovery_scope_spends_at_most_one_restart_on_total_failure(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "restart-budget-home"),
            daemon_supervisor_command=["fake-supervisor"],
            pre_submit_reconnect_attempts=2,
        )
        starts = []

        def fail_connect(_cwd, *, recovery_deadline, cancel_requested, recovery_budget):
            for _ in range(2):
                bridge._run_supervisor_action_locked(
                    "restart",
                    cwd=self.root,
                    env={},
                    deadline=recovery_deadline,
                    cancel_requested=cancel_requested,
                    recovery_budget=recovery_budget,
                )
            raise bridge_module._TransportError("connect/initialize/resume failed")

        bridge._daemon_pid_state = lambda: "dead"
        bridge._run_daemon_start_interruptible = lambda *_args, **_kwargs: starts.append("restart")
        bridge._ensure_connected = fail_connect
        try:
            with self.assertRaises(bridge_module._TransportError):
                bridge._prepare_thread_resilient(
                    "thread-budget",
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    recovery_deadline=time.monotonic() + 1.0,
                )
            self.assertEqual(starts, ["restart"])
        finally:
            bridge.close()

    def test_dead_pid_restarts_independent_supervisor(self):
        home = self.root / "dead-pid-home"
        daemon_dir = home / "app-server-daemon"
        daemon_dir.mkdir(parents=True)
        (daemon_dir / "app-server.pid").write_text('{"pid":99999999}', encoding="utf-8")
        bridge = CodexAppBridge(
            codex_home=str(home),
            daemon_socket_path=str(self.root / "dead-pid.sock"),
            daemon_recovery_timeout_sec=1.0,
            daemon_supervisor_command=["fake-supervisor"],
        )
        websocket = mock.Mock()
        try:
            with mock.patch("codex_app_bridge.unix_connect", side_effect=[OSError("stale"), websocket]), mock.patch.object(
                bridge, "_run_supervisor_action_locked", return_value=True
            ) as supervisor, mock.patch.object(bridge, "_initialize_connection"), mock.patch.object(
                bridge, "_websocket_reader_loop"
            ):
                bridge._start_daemon_connection_locked(self.root)
            supervisor.assert_called_once()
            self.assertEqual(supervisor.call_args.args[0], "restart")
        finally:
            bridge.close()

    def test_reused_pid_that_is_not_remote_daemon_restarts_independent_supervisor(self):
        home = self.root / "reused-pid-home"
        daemon_dir = home / "app-server-daemon"
        daemon_dir.mkdir(parents=True)
        # This test process is alive, but its argv is not Codex's
        # ``app-server --remote-control``.  A reused pid record must not
        # suppress recovery forever or cause the bridge to signal this process.
        (daemon_dir / "app-server.pid").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        bridge = CodexAppBridge(
            codex_home=str(home),
            daemon_socket_path=str(self.root / "reused-pid.sock"),
            daemon_recovery_timeout_sec=1.0,
            daemon_supervisor_command=["fake-supervisor"],
        )
        websocket = mock.Mock()
        try:
            self.assertEqual(bridge._daemon_pid_state(), "dead")
            with mock.patch("codex_app_bridge.unix_connect", side_effect=[OSError("stale"), websocket]), mock.patch.object(
                bridge, "_run_supervisor_action_locked", return_value=True
            ) as supervisor, mock.patch.object(bridge, "_initialize_connection"), mock.patch.object(
                bridge, "_websocket_reader_loop"
            ):
                bridge._start_daemon_connection_locked(self.root)
            supervisor.assert_called_once()
            self.assertEqual(supervisor.call_args.args[0], "restart")
        finally:
            bridge.close()

    def test_live_or_unknown_pid_never_requests_supervisor_restart(self):
        for state, expected_actions in (("live", []), ("unknown", ["start"])):
            with self.subTest(state=state):
                bridge = CodexAppBridge(
                    codex_home=str(self.root / f"{state}-pid-home"),
                    daemon_socket_path=str(self.root / f"{state}-pid.sock"),
                    daemon_recovery_timeout_sec=1.0,
                    daemon_supervisor_command=["fake-supervisor"],
                )
                websocket = mock.Mock()
                try:
                    with mock.patch("codex_app_bridge.unix_connect", side_effect=[OSError("missing"), websocket]), mock.patch.object(
                        bridge, "_daemon_pid_state", return_value=state
                    ), mock.patch.object(bridge, "_run_supervisor_action_locked", return_value=True) as supervisor, mock.patch.object(
                        bridge, "_initialize_connection"
                    ), mock.patch.object(bridge, "_websocket_reader_loop"):
                        bridge._start_daemon_connection_locked(self.root)
                    self.assertEqual(
                        [call.args[0] for call in supervisor.call_args_list],
                        expected_actions,
                    )
                finally:
                    bridge.close()

    def test_competing_dead_pid_repairs_are_serialized(self):
        home = self.root / "competing-repair-home"
        daemon_dir = home / "app-server-daemon"
        daemon_dir.mkdir(parents=True)
        lock_path = self.root / "competing-repair.lock"
        first = CodexAppBridge(
            codex_home=str(home),
            daemon_recovery_lock_path=str(lock_path),
            daemon_supervisor_command=["fake-supervisor"],
        )
        second = CodexAppBridge(
            codex_home=str(home),
            daemon_recovery_lock_path=str(lock_path),
            daemon_supervisor_command=["fake-supervisor"],
        )
        starts = []
        start_lock = threading.Lock()
        daemon_state = {"value": "dead"}

        def starter(*_args, **_kwargs):
            with start_lock:
                starts.append("restart")
                daemon_state["value"] = "live"
            time.sleep(0.05)

        first._run_daemon_start_interruptible = starter
        second._run_daemon_start_interruptible = starter
        first._daemon_pid_state = lambda: daemon_state["value"]
        second._daemon_pid_state = lambda: daemon_state["value"]
        results = []

        def repair(bridge):
            results.append(bridge._run_supervisor_action_locked(
                "restart",
                cwd=self.root,
                env={},
                deadline=time.monotonic() + 1.0,
                cancel_requested=None,
            ))

        workers = [threading.Thread(target=repair, args=(bridge,)) for bridge in (first, second)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2.0)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(starts, ["restart"])
            self.assertEqual(sorted(results), [True, True])
        finally:
            first.close()
            second.close()

    def test_bridge_close_never_stops_shared_daemon(self):
        bridge = CodexAppBridge(codex_home=str(self.root / "close-does-not-stop-home"))
        with mock.patch.object(bridge, "_run_supervisor_action_locked") as supervisor:
            bridge.close()
        supervisor.assert_not_called()

    def test_disabled_systemd_supervisor_fails_closed_without_action(self):
        bridge = CodexAppBridge(codex_home=str(self.root / "disabled-unit-home"))
        budget = bridge_module._DaemonRecoveryBudget()
        bridge._daemon_pid_state = lambda: "dead"
        disabled = subprocess.CompletedProcess([], 1, "", "")
        try:
            with mock.patch.object(bridge, "_query_systemd_unit", return_value=disabled), mock.patch.object(
                bridge, "_run_daemon_start_interruptible"
            ) as starter:
                repaired = bridge._run_supervisor_action_locked(
                    "restart",
                    cwd=self.root,
                    env={},
                    deadline=time.monotonic() + 1.0,
                    cancel_requested=None,
                    recovery_budget=budget,
                )
            self.assertFalse(repaired)
            starter.assert_not_called()
        finally:
            bridge.close()

    def test_systemd_takeover_requires_live_daemon_inside_unit_cgroup(self):
        bridge = CodexAppBridge(codex_home=str(self.root / "takeover-unit-home"))
        shown = subprocess.CompletedProcess(
            [],
            0,
            "ActiveState=active\nUnitFileState=enabled\n"
            "ControlGroup=/system.slice/codex-remote-control.service\n",
            "",
        )
        identity = bridge_module._DaemonProcessIdentity(
            pid=321,
            pidfile_device=1,
            pidfile_inode=2,
            starttime=3,
            argv=(b"codex", b"app-server", b"--remote-control"),
            executable_device=4,
            executable_inode=5,
            executable_uid=0,
            process_uid=0,
            cgroups=("/system.slice/codex-remote-control.service/app-server",),
        )
        try:
            with mock.patch.object(bridge, "_query_systemd_unit", return_value=shown), mock.patch.object(
                bridge, "_read_daemon_identity", return_value=identity
            ) as reader, mock.patch.object(bridge, "_daemon_identity_is_managed", return_value=True):
                self.assertTrue(bridge._systemd_takeover_verified(time.monotonic() + 1.0))
                reader.assert_called_once_with()
            outside = bridge_module._DaemonProcessIdentity(
                **{**identity.__dict__, "cgroups": ("/system.slice/cc-companion.service",)}
            )
            with mock.patch.object(bridge, "_query_systemd_unit", return_value=shown), mock.patch.object(
                bridge, "_read_daemon_identity", return_value=outside
            ), mock.patch.object(bridge, "_daemon_identity_is_managed", return_value=True):
                self.assertFalse(bridge._systemd_takeover_verified(time.monotonic() + 1.0))
        finally:
            bridge.close()

    def test_managed_identity_separates_installer_binary_uid_from_unit_process_uid(self):
        home = self.root / "uid-separated-home"
        binary = home / "packages/standalone/releases/v1/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"codex")
        binary.chmod(0o755)
        os.chown(binary.parent, 1001, 1001)
        os.chown(binary, 1001, 1001)
        current = home / "packages/standalone/current"
        current.symlink_to(home / "packages/standalone/releases/v1")
        bridge = CodexAppBridge(codex_home=str(home))
        managed = bridge._managed_daemon_executable()
        identity = bridge_module._DaemonProcessIdentity(
            321, 1, 2, 3, (b"codex", b"app-server", b"--remote-control"),
            managed[0], managed[1], 1001, 0, ("/system.slice/codex-remote-control.service",),
        )
        try:
            self.assertEqual(managed[2], 1001)
            self.assertTrue(bridge._daemon_identity_is_managed(identity))
            wrong_process_owner = bridge_module._DaemonProcessIdentity(
                **{**identity.__dict__, "process_uid": 1001}
            )
            self.assertFalse(bridge._daemon_identity_is_managed(wrong_process_owner))
        finally:
            bridge.close()

    def test_managed_identity_rejects_group_writable_release_chain(self):
        home = self.root / "writable-chain-home"
        binary = home / "packages/standalone/current/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"codex")
        binary.chmod(0o755)
        binary.parent.chmod(0o775)
        bridge = CodexAppBridge(codex_home=str(home))
        try:
            self.assertIsNone(bridge._managed_daemon_executable())
        finally:
            bridge.close()

    def test_systemd_takeover_rejects_unit_state_change_around_identity_snapshot(self):
        bridge = CodexAppBridge(codex_home=str(self.root / "changing-unit-home"))
        active = subprocess.CompletedProcess(
            [], 0, "ActiveState=active\nUnitFileState=enabled\nControlGroup=/system.slice/x\n", ""
        )
        inactive = subprocess.CompletedProcess(
            [], 0, "ActiveState=inactive\nUnitFileState=enabled\nControlGroup=/system.slice/x\n", ""
        )
        identity = bridge_module._DaemonProcessIdentity(
            1, 1, 1, 1, (b"app-server", b"--remote-control"), 1, 1, 0, 0,
            ("/system.slice/x",),
        )
        try:
            with mock.patch.object(
                bridge, "_query_systemd_unit", side_effect=[active, inactive]
            ), mock.patch.object(bridge, "_read_daemon_identity", return_value=identity):
                self.assertFalse(bridge._systemd_takeover_verified(time.monotonic() + 1.0))
        finally:
            bridge.close()

    def test_recovery_lock_rejects_symlink_and_permissive_file(self):
        target = self.root / "lock-target"
        target.write_text("", encoding="utf-8")
        symlink = self.root / "lock-link"
        symlink.symlink_to(target)
        permissive = self.root / "permissive.lock"
        permissive.write_text("", encoding="utf-8")
        permissive.chmod(0o644)
        for path in (symlink, permissive):
            with self.subTest(path=path.name):
                lock = bridge_module._DaemonRecoveryLock(path)
                self.assertFalse(lock.acquire(deadline=time.monotonic() + 0.1))

    def test_live_daemon_outside_systemd_unit_is_rejected_before_socket_use(self):
        bridge = CodexAppBridge(codex_home=str(self.root / "wrong-cgroup-home"))
        bridge._daemon_pid_state = lambda: "live"
        try:
            with mock.patch.object(bridge, "_systemd_takeover_verified", return_value=False), mock.patch(
                "codex_app_bridge.unix_connect"
            ) as connector:
                with self.assertRaisesRegex(bridge_module._TransportError, "not owned"):
                    bridge._start_daemon_connection_locked(
                        self.root,
                        recovery_deadline=time.monotonic() + 1.0,
                    )
            connector.assert_not_called()
        finally:
            bridge.close()

    def test_daemon_recovery_budget_is_global_across_prepare_retries(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "global-budget-home"),
            daemon_socket_path=str(self.root / "global-budget.sock"),
            daemon_recovery_timeout_sec=1.0,
            daemon_start_timeout_sec=0.4,
            daemon_connect_retry_sec=0.1,
            pre_submit_reconnect_attempts=2,
            daemon_supervisor_command=["fake-supervisor"],
        )
        clock = [100.0]
        start_deadlines = []

        def fake_start(_command, *, deadline, **_kwargs):
            start_deadlines.append(deadline)
            clock[0] = deadline
            raise subprocess.TimeoutExpired(["codex"], deadline)

        def fake_wait(delay, _cancel_requested):
            clock[0] += delay

        try:
            with mock.patch(
                "codex_app_bridge.time.monotonic",
                side_effect=lambda: clock[0],
            ), mock.patch(
                "codex_app_bridge.unix_connect",
                side_effect=OSError("socket absent"),
            ), mock.patch.object(
                bridge,
                "_run_daemon_start_interruptible",
                side_effect=fake_start,
            ), mock.patch.object(
                bridge,
                "_interruptible_recovery_wait",
                side_effect=fake_wait,
            ):
                with self.assertRaises(CodexAppBridgeError):
                    bridge._prepare_thread_resilient(
                        "thread-budget",
                        cwd=self.root,
                        model="gpt-test",
                        effort="high",
                    )
            self.assertEqual(len(start_deadlines), 2)
            self.assertAlmostEqual(start_deadlines[0], 100.4)
            self.assertAlmostEqual(start_deadlines[1], 100.9)
            self.assertAlmostEqual(clock[0], 101.0)
            self.assertTrue(all(deadline <= 101.0 for deadline in start_deadlines))
        finally:
            bridge.close()

    def test_cancel_during_stale_daemon_start_reaps_helper_immediately(self):
        fake_codex = self.root / "fake-codex-wait"
        fake_codex.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        bridge = CodexAppBridge(
            codex_bin=str(fake_codex),
            codex_home=str(self.root / "cancel-recovery-home"),
            daemon_socket_path=str(self.root / "cancel-recovery.sock"),
            daemon_recovery_timeout_sec=5.0,
            daemon_start_timeout_sec=5.0,
            daemon_connect_retry_sec=0.01,
            daemon_supervisor_command=[str(fake_codex)],
        )
        cancel_event = threading.Event()
        started = threading.Event()
        processes = []
        errors = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def recover():
            try:
                bridge._ensure_connected(
                    self.root,
                    recovery_deadline=time.monotonic() + 5.0,
                    cancel_requested=cancel_event.is_set,
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "codex_app_bridge.unix_connect",
            side_effect=OSError("socket absent"),
        ), mock.patch(
            "codex_app_bridge.subprocess.Popen",
            side_effect=capture_popen,
        ):
            worker = threading.Thread(target=recover)
            worker.start()
            self.assertTrue(started.wait(1.0))
            cancelled_at = time.monotonic()
            cancel_event.set()
            worker.join(timeout=1.0)
            elapsed = time.monotonic() - cancelled_at
        self.assertFalse(worker.is_alive())
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], bridge_module._RecoveryCancelled)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))
        bridge.close()

    def test_close_wakes_held_connect_lock_and_reaps_start_helper(self):
        fake_codex = self.root / "fake-codex-close"
        fake_codex.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        bridge = CodexAppBridge(
            codex_bin=str(fake_codex),
            codex_home=str(self.root / "close-recovery-home"),
            daemon_socket_path=str(self.root / "close-recovery.sock"),
            daemon_recovery_timeout_sec=5.0,
            daemon_start_timeout_sec=5.0,
            daemon_supervisor_command=[str(fake_codex)],
        )
        started = threading.Event()
        processes = []
        errors = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def recover():
            try:
                bridge._ensure_connected(self.root)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "codex_app_bridge.unix_connect",
            side_effect=OSError("socket absent"),
        ), mock.patch(
            "codex_app_bridge.subprocess.Popen",
            side_effect=capture_popen,
        ):
            worker = threading.Thread(target=recover)
            worker.start()
            self.assertTrue(started.wait(1.0))
            closed_at = time.monotonic()
            bridge.close()
            elapsed = time.monotonic() - closed_at
            worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], bridge_module._RecoveryCancelled)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_repeated_daemon_start_timeouts_reap_every_helper(self):
        fake_codex = self.root / "fake-codex-timeout"
        fake_codex.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        bridge = CodexAppBridge(
            codex_bin=str(fake_codex),
            codex_home=str(self.root / "timeout-recovery-home"),
            daemon_socket_path=str(self.root / "timeout-recovery.sock"),
            daemon_recovery_timeout_sec=0.28,
            daemon_start_timeout_sec=0.08,
            daemon_connect_retry_sec=0.01,
            daemon_supervisor_command=[str(fake_codex)],
        )
        processes = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        started_at = time.monotonic()
        try:
            with mock.patch(
                "codex_app_bridge.unix_connect",
                side_effect=OSError("socket absent"),
            ), mock.patch(
                "codex_app_bridge.subprocess.Popen",
                side_effect=capture_popen,
            ):
                with self.assertRaises(CodexAppBridgeError):
                    bridge._start_daemon_connection_locked(self.root)
            elapsed = time.monotonic() - started_at
            self.assertGreaterEqual(len(processes), 2)
            self.assertLess(elapsed, 0.8)
            self.assertTrue(all(process.poll() is not None for process in processes))
        finally:
            bridge.close()

    def test_disconnect_before_prompt_submission_reconnects_without_duplicate_turn(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "pre-submit-home"),
            daemon_autostart=False,
            pre_submit_reconnect_attempts=2,
        )
        transport_error = bridge_module._TransportError("resume connection lost")
        completed_turn = {
            "turn": {
                "id": "turn-one",
                "status": "completed",
                "items": [],
            }
        }
        try:
            with mock.patch.object(
                bridge,
                "_ensure_connected",
            ) as ensure, mock.patch.object(
                bridge,
                "_prepare_thread",
                side_effect=[
                    transport_error,
                    ("thread-pre-submit", []),
                ],
            ) as prepare, mock.patch.object(
                bridge,
                "_close_process_locked",
            ) as close, mock.patch.object(
                bridge,
                "_rpc_request",
                return_value=completed_turn,
            ) as rpc:
                result = bridge.run_turn(
                    thread_id="thread-pre-submit",
                    cwd=self.root,
                    prompt="execute exactly once",
                    model="gpt-test",
                    effort="high",
                )
            self.assertEqual(result.status, "completed")
            self.assertEqual(ensure.call_count, 2)
            self.assertEqual(prepare.call_count, 2)
            close.assert_called_once()
            turn_calls = [
                call for call in rpc.call_args_list
                if call.args and call.args[0] in {"turn/start", "turn/steer"}
            ]
            self.assertEqual(len(turn_calls), 1)
            self.assertEqual(
                turn_calls[0].args[1]["input"][0]["text"],
                "execute exactly once",
            )
            uuid.UUID(turn_calls[0].args[1]["clientUserMessageId"])
        finally:
            bridge.close()

    def test_shared_daemon_existing_active_turn_is_atomically_steered(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "busy-home"),
            daemon_autostart=False,
        )
        try:
            with mock.patch.object(bridge, "_ensure_connected"), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=(
                    "thread-busy",
                    [{"id": "turn-remote", "status": "inProgress", "items": []}],
                ),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                return_value={"turnId": "turn-remote"},
            ) as rpc, mock.patch.object(
                bridge,
                "_wait_for_turn",
                return_value=mock.sentinel.steered_result,
            ):
                result = bridge.run_turn(
                    thread_id="thread-busy",
                    cwd=self.root,
                    prompt="steer-me",
                    model="gpt-test",
                    effort="high",
                )
            self.assertIs(result, mock.sentinel.steered_result)
            rpc.assert_called_once()
            method, params = rpc.call_args.args
            self.assertEqual(method, "turn/steer")
            self.assertEqual(params["threadId"], "thread-busy")
            self.assertEqual(params["expectedTurnId"], "turn-remote")
            self.assertEqual(params["input"][0]["text"], "steer-me")
            uuid.UUID(params["clientUserMessageId"])
        finally:
            bridge.close()

    def test_shared_daemon_ignores_rollout_marker_reconnects(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "marker-home"),
            daemon_autostart=False,
        )
        try:
            with mock.patch.object(
                bridge,
                "_rollout_changed",
                return_value=True,
            ) as changed, mock.patch.object(
                bridge,
                "_close_process_locked",
            ) as close, mock.patch.object(
                bridge,
                "_ensure_connected",
            ), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=("thread-marker", []),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                return_value={
                    "turn": {
                        "id": "turn-marker",
                        "status": "completed",
                        "items": [],
                    }
                },
            ):
                result = bridge.run_turn(
                    thread_id="thread-marker",
                    cwd=self.root,
                    prompt="normal",
                    model="gpt-test",
                    effort="high",
                )
                self.assertEqual(result.status, "completed")
                changed.assert_not_called()
                close.assert_not_called()
        finally:
            bridge.close()

    def test_shared_daemon_nonsteerable_turn_stays_queued(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "nonsteerable-home"),
            daemon_autostart=False,
        )
        busy_error = bridge_module._RPCError(
            "turn/steer",
            {
                "code": -32000,
                "message": "active turn cannot accept steering",
                "data": {
                    "codexErrorInfo": {
                        "activeTurnNotSteerable": {"turnKind": "compact"},
                    }
                },
            },
        )
        try:
            with mock.patch.object(bridge, "_ensure_connected"), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=(
                    "thread-compact",
                    [{"id": "turn-compact", "status": "inProgress", "items": []}],
                ),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                side_effect=busy_error,
            ), self.assertRaises(CodexPromptLockBusy):
                bridge.run_turn(
                    thread_id="thread-compact",
                    cwd=self.root,
                    prompt="queue-me",
                    model="gpt-test",
                    effort="high",
                )
        finally:
            bridge.close()

    def test_shared_daemon_turn_start_busy_race_stays_queued(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "busy-race-home"),
            daemon_autostart=False,
        )
        busy_error = bridge_module._RPCError(
            "turn/start",
            {"code": -32000, "message": "thread already has an active turn"},
        )
        try:
            with mock.patch.object(bridge, "_ensure_connected"), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=("thread-busy-race", []),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                side_effect=busy_error,
            ), self.assertRaises(CodexPromptLockBusy):
                bridge.run_turn(
                    thread_id="thread-busy-race",
                    cwd=self.root,
                    prompt="queue-race",
                    model="gpt-test",
                    effort="high",
                )
        finally:
            bridge.close()

    def test_shared_daemon_steer_disconnect_reconciles_by_client_message_id(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "steer-disconnect-home"),
            daemon_autostart=False,
        )
        transport_error = bridge_module._TransportError("connection lost")
        try:
            with mock.patch.object(bridge, "_ensure_connected"), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=(
                    "thread-steer-disconnect",
                    [{"id": "turn-existing", "status": "inProgress", "items": []}],
                ),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                side_effect=transport_error,
            ) as rpc, mock.patch.object(
                bridge,
                "_reconnect_and_reconcile",
                return_value=False,
            ) as reconcile:
                result = bridge.run_turn(
                    thread_id="thread-steer-disconnect",
                    cwd=self.root,
                    prompt="maybe-steered",
                    model="gpt-test",
                    effort="high",
                )
            self.assertEqual(result.status, "uncertain")
            self.assertEqual(rpc.call_args.args[0], "turn/steer")
            client_id = rpc.call_args.args[1]["clientUserMessageId"]
            uuid.UUID(client_id)
            self.assertEqual(
                reconcile.call_args.kwargs["required_client_user_message_id"],
                client_id,
            )
        finally:
            bridge.close()

    def test_shared_daemon_start_disconnect_reconciles_by_client_message_id(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "start-disconnect-home"),
            daemon_autostart=False,
        )
        transport_error = bridge_module._TransportError("connection lost")
        try:
            with mock.patch.object(bridge, "_ensure_connected"), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=("thread-start-disconnect", []),
            ), mock.patch.object(
                bridge,
                "_rpc_request",
                side_effect=transport_error,
            ) as rpc, mock.patch.object(
                bridge,
                "_reconnect_and_reconcile",
                return_value=False,
            ) as reconcile:
                result = bridge.run_turn(
                    thread_id="thread-start-disconnect",
                    cwd=self.root,
                    prompt="maybe-started",
                    model="gpt-test",
                    effort="high",
                )
            self.assertEqual(result.status, "uncertain")
            self.assertEqual(rpc.call_args.args[0], "turn/start")
            client_id = rpc.call_args.args[1]["clientUserMessageId"]
            uuid.UUID(client_id)
            self.assertEqual(
                reconcile.call_args.kwargs["required_client_user_message_id"],
                client_id,
            )
        finally:
            bridge.close()

    def test_reconcile_rejects_turn_without_exact_client_message_id(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "reconcile-home"),
            daemon_autostart=False,
        )
        active = bridge_module._ActiveTurn(
            thread_id="thread-reconcile",
            on_update=None,
            on_activity=None,
            turn_id="turn-reconcile",
        )
        wrong_turn = {
            "id": "turn-reconcile",
            "status": "inProgress",
            "items": [{
                "id": "user-wrong",
                "type": "userMessage",
                "clientId": "other-client-id",
                "content": [],
            }],
        }
        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ), mock.patch.object(
                bridge,
                "_prepare_thread_resilient",
                return_value=("thread-reconcile", [wrong_turn]),
            ):
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids={"turn-reconcile"},
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    required_client_user_message_id="our-client-id",
                )
            self.assertFalse(reconciled)
        finally:
            bridge.close()

    def test_reconcile_accepts_one_exact_client_message_id(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "reconcile-exact-home"),
            daemon_autostart=False,
        )
        active = bridge_module._ActiveTurn(
            thread_id="thread-exact",
            on_update=None,
            on_activity=None,
            connection_lost=True,
        )
        exact_turn = {
            "id": "turn-exact-client",
            "status": "inProgress",
            "items": [{
                "id": "user-exact",
                "type": "userMessage",
                "clientId": "our-client-id",
                "content": [],
            }],
        }
        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ), mock.patch.object(
                bridge,
                "_prepare_thread_resilient",
                return_value=("thread-exact", [exact_turn]),
            ):
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids=set(),
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    required_client_user_message_id="our-client-id",
                )
            self.assertTrue(reconciled)
            self.assertEqual(active.turn_id, "turn-exact-client")
            self.assertFalse(active.connection_lost)
        finally:
            bridge.close()

    def test_reconcile_rejects_same_client_id_from_wrong_thread(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "reconcile-wrong-thread-home"),
            daemon_autostart=False,
        )
        active = bridge_module._ActiveTurn(
            thread_id="thread-expected",
            on_update=None,
            on_activity=None,
        )
        matching_turn = {
            "id": "turn-wrong-thread",
            "status": "inProgress",
            "items": [{
                "id": "user-same-id",
                "type": "userMessage",
                "clientId": "our-client-id",
                "content": [],
            }],
        }
        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ), mock.patch.object(
                bridge,
                "_prepare_thread_resilient",
                return_value=("thread-other", [matching_turn]),
            ):
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids=set(),
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    required_client_user_message_id="our-client-id",
                )
            self.assertFalse(reconciled)
            self.assertIsNone(active.turn_id)
        finally:
            bridge.close()

    def test_reconcile_rejects_duplicate_exact_client_id_candidates(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "reconcile-duplicate-home"),
            daemon_autostart=False,
        )
        active = bridge_module._ActiveTurn(
            thread_id="thread-duplicate",
            on_update=None,
            on_activity=None,
        )

        def candidate(turn_id):
            return {
                "id": turn_id,
                "status": "inProgress",
                "items": [{
                    "id": f"user-{turn_id}",
                    "type": "userMessage",
                    "clientId": "duplicate-client-id",
                    "content": [],
                }],
            }

        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ), mock.patch.object(
                bridge,
                "_prepare_thread_resilient",
                return_value=(
                    "thread-duplicate",
                    [candidate("turn-one"), candidate("turn-two")],
                ),
            ):
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids=set(),
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    required_client_user_message_id="duplicate-client-id",
                )
            self.assertFalse(reconciled)
            self.assertIsNone(active.turn_id)
        finally:
            bridge.close()

    def test_real_reader_disconnect_is_cleared_by_prepared_new_generation(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "generation-recovery-home"),
            daemon_autostart=False,
        )
        old_connection = mock.Mock()
        active = bridge_module._ActiveTurn(
            thread_id="thread-generation",
            on_update=None,
            on_activity=None,
            turn_id="turn-generation",
            connection_generation=1,
        )
        bridge._generation = 1
        bridge._websocket = old_connection
        bridge._initialized = True
        with bridge._state_lock:
            bridge._active = active

        bridge._reader_disconnected(old_connection, 1)
        self.assertTrue(active.connection_lost)
        exact_turn = {
            "id": "turn-generation",
            "status": "inProgress",
            "items": [],
        }

        def connect_new_generation(*_args, **_kwargs):
            bridge._generation = 2

        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ), mock.patch.object(
                bridge,
                "_ensure_connected",
                side_effect=connect_new_generation,
            ), mock.patch.object(
                bridge,
                "_prepare_thread",
                return_value=("thread-generation", [exact_turn]),
            ):
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids={"turn-generation"},
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    require_known_turn=True,
                )
            self.assertTrue(reconciled)
            self.assertFalse(active.connection_lost)
            self.assertEqual(active.connection_generation, 2)

            def finish_turn():
                time.sleep(0.02)
                with active.condition:
                    active.status = "completed"
                    active.condition.notify_all()

            finisher = threading.Thread(target=finish_turn)
            finisher.start()
            with mock.patch.object(
                bridge,
                "_reconnect_and_reconcile",
            ) as second_reconnect:
                result = bridge._wait_for_turn(
                    active,
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    cancel_event=None,
                    max_runtime_sec=1.0,
                )
            finisher.join(timeout=1.0)
            self.assertEqual(result.status, "completed")
            second_reconnect.assert_not_called()
        finally:
            with bridge._state_lock:
                bridge._active = None
            bridge.close()

    def test_active_resume_disconnect_retries_then_matches_exact_turn_id(self):
        bridge = CodexAppBridge(
            codex_home=str(self.root / "resume-recovery-home"),
            daemon_autostart=False,
            pre_submit_reconnect_attempts=2,
        )
        active = bridge_module._ActiveTurn(
            thread_id="thread-resume",
            on_update=None,
            on_activity=None,
            turn_id="turn-exact",
            connection_lost=True,
        )
        exact_turn = {
            "id": "turn-exact",
            "status": "inProgress",
            "items": [],
        }
        try:
            with mock.patch.object(
                bridge,
                "_close_process_locked",
            ) as close, mock.patch.object(
                bridge,
                "_ensure_connected",
            ), mock.patch.object(
                bridge,
                "_prepare_thread",
                side_effect=[
                    bridge_module._TransportError("resume disconnected"),
                    ("thread-resume", [exact_turn]),
                ],
            ) as prepare:
                reconciled = bridge._reconnect_and_reconcile(
                    active,
                    prior_turn_ids={"turn-exact"},
                    cwd=self.root,
                    model="gpt-test",
                    effort="high",
                    require_known_turn=True,
                )
            self.assertTrue(reconciled)
            self.assertEqual(prepare.call_count, 2)
            self.assertGreaterEqual(close.call_count, 2)
            self.assertEqual(active.turn_id, "turn-exact")
            self.assertFalse(active.connection_lost)
        finally:
            bridge.close()

    def test_turn_accepted_callback_runs_once_after_real_turn_id_exists(self):
        accepted = []
        result = self.bridge.run_turn(
            thread_id=None,
            cwd=self.root,
            prompt="normal",
            model="gpt-test",
            effort="high",
            on_turn_accepted=accepted.append,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(accepted, ["thread-test"])

    def test_turn_accepted_callback_does_not_run_on_pre_start_failure(self):
        accepted = []
        error = CodexAppBridgeError("prepare failed", fallback_safe=True)
        with mock.patch.object(self.bridge, "_prepare_thread", side_effect=error):
            with self.assertRaises(CodexAppBridgeError) as raised:
                self.bridge.run_turn(
                    thread_id=None,
                    cwd=self.root,
                    prompt="normal",
                    model="gpt-test",
                    effort="high",
                    on_turn_accepted=accepted.append,
                )
        self.assertTrue(raised.exception.fallback_safe)
        self.assertEqual(accepted, [])

    def test_current_turn_compaction_is_returned(self):
        result = self.bridge.run_turn(
            thread_id=None,
            cwd=self.root,
            prompt="compacted",
            model="gpt-test",
            effort="high",
        )
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.context_compacted)

    def test_optional_auto_compact_limit_is_sent_to_start_and_resume(self):
        self.bridge.close()
        self.bridge = CodexAppBridge(
            command=[sys.executable, str(self.server_path), str(self.log_path)],
            codex_home=str(self.root / "codex-home"),
            request_timeout_sec=2.0,
            model_auto_compact_token_limit=123456,
        )
        first, _, _ = self.run_normal()
        self.run_normal(first.thread_id)
        entries = [
            entry for entry in self.rpc_entries()
            if entry["method"] in {"thread/start", "thread/resume"}
        ]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(
                entry["params"]["config"]["model_auto_compact_token_limit"],
                123456,
            )

    def test_external_rollout_change_restarts_app_server(self):
        marker = [str(self.root / "rollout.jsonl"), 1, 10]

        def marker_provider(_thread_id):
            return tuple(marker)

        first, _, _ = self.run_normal(marker_provider=marker_provider)
        marker[1:] = [2, 20]
        second, _, _ = self.run_normal(first.thread_id, marker_provider=marker_provider)
        self.assertEqual(second.status, "completed")
        self.assertEqual(self.methods().count("initialize"), 2)

    def test_cancel_interrupts_active_turn_and_single_turn_gate(self):
        cancel_event = threading.Event()
        holder = {}

        def run_slow():
            holder["result"] = self.bridge.run_turn(
                thread_id=None,
                cwd=self.root,
                prompt="slow",
                model="gpt-test",
                effort="high",
                cancel_event=cancel_event,
                max_runtime_sec=3.0,
            )

        worker = threading.Thread(target=run_slow)
        worker.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            snapshot = self.bridge.snapshot()
            if snapshot.get("turn_id"):
                break
            time.sleep(0.01)
        else:
            self.fail("fake turn did not start")

        with self.assertRaises(CodexActiveTurnError):
            self.bridge.run_turn(
                thread_id="thread-test",
                cwd=self.root,
                prompt="normal",
                model="gpt-test",
                effort="high",
            )
        cancel_event.set()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["result"].status, "interrupted")
        self.assertIn("turn/interrupt", self.methods())

    def test_observer_snapshot_is_live_bounded_and_contains_no_raw_payload(self):
        cancel_event = threading.Event()
        holder = {}

        def run_slow():
            holder["result"] = self.bridge.run_turn(
                thread_id=None,
                cwd=self.root,
                prompt="slow",
                model="gpt-test",
                effort="high",
                cancel_event=cancel_event,
                max_runtime_sec=3.0,
            )

        worker = threading.Thread(target=run_slow)
        worker.start()
        deadline = time.time() + 2.0
        observer = {}
        while time.time() < deadline:
            observer = self.bridge.observer_snapshot()
            if any(
                event.get("label") == "运行命令（参数与输出已隐藏）"
                for event in observer.get("events", [])
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("observer did not receive the fake command event")

        encoded = json.dumps(observer, ensure_ascii=False)
        self.assertTrue(observer["busy"])
        self.assertEqual(observer["phase"], "正在处理")
        self.assertNotIn("SUPER_SECRET_OBSERVER_PAYLOAD", encoded)
        self.assertNotIn("/private/observer/path", encoded)
        self.assertNotIn("thread-test", encoded)
        self.assertNotIn("turn-", encoded)

        with self.bridge._state_lock:
            active = self.bridge._active
        self.assertIsNotNone(active)
        for index in range(60):
            self.bridge._handle_item(
                active,
                {"id": f"extra-command-{index}", "type": "commandExecution"},
                completed=False,
            )
        self.assertEqual(len(self.bridge.observer_snapshot()["events"]), 40)

        cancel_event.set()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["result"].status, "interrupted")
        self.assertEqual(
            self.bridge.observer_snapshot(),
            {"busy": False, "phase": None, "events": []},
        )

    def test_zero_runtime_limit_survives_wall_clock_jump_until_explicit_cancel(self):
        cancel_event = threading.Event()
        holder = {}
        monotonic_now = [100.0]

        def run_slow():
            with mock.patch(
                "codex_app_bridge.time.monotonic",
                side_effect=lambda: monotonic_now[0],
            ):
                holder["result"] = self.bridge.run_turn(
                    thread_id=None,
                    cwd=self.root,
                    prompt="slow",
                    model="gpt-test",
                    effort="high",
                    cancel_event=cancel_event,
                    max_runtime_sec=0,
                )

        worker = threading.Thread(target=run_slow)
        worker.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self.bridge.snapshot().get("turn_id"):
                break
            time.sleep(0.01)
        else:
            self.fail("fake turn did not start")

        # Simulate far more than the former 900-second wall-clock ceiling.
        monotonic_now[0] += 901.0
        time.sleep(0.3)
        self.assertTrue(worker.is_alive())
        self.assertNotIn("turn/interrupt", self.methods())

        cancel_event.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["result"].status, "interrupted")
        self.assertEqual(self.methods().count("turn/interrupt"), 1)

    def test_server_requests_receive_nonblocking_responses(self):
        responses = []
        self.bridge._write_json = responses.append
        self.bridge._respond_to_server_request({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "item/tool/requestUserInput",
        })
        self.bridge._respond_to_server_request({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "mcpServer/elicitation/request",
        })
        self.bridge._respond_to_server_request({
            "jsonrpc": "2.0",
            "id": 12,
            "method": "unknown/request",
        })
        self.assertEqual(responses[0]["result"], {"answers": {}})
        self.assertEqual(responses[1]["result"]["action"], "cancel")
        self.assertEqual(responses[2]["error"]["code"], -32601)

    def test_cancelled_before_start_does_not_create_turn(self):
        cancel_event = threading.Event()
        cancel_event.set()
        accepted = []
        result = self.bridge.run_turn(
            thread_id=None,
            cwd=self.root,
            prompt="normal",
            model="gpt-test",
            effort="high",
            cancel_event=cancel_event,
            on_turn_accepted=accepted.append,
        )
        self.assertEqual(result.status, "interrupted")
        self.assertNotIn("turn/start", self.methods())
        self.assertEqual(accepted, [])

    def test_cancel_during_unanswered_turn_start_stays_uncertain_without_retry(self):
        cancel_event = threading.Event()
        holder = {}

        def run_delayed_start():
            holder["result"] = self.bridge.run_turn(
                thread_id=None,
                cwd=self.root,
                prompt="delayed-start",
                model="gpt-test",
                effort="high",
                cancel_event=cancel_event,
                max_runtime_sec=3.0,
            )

        worker = threading.Thread(target=run_delayed_start)
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            snapshot = self.bridge.snapshot()
            if snapshot.get("phase") == "starting" and snapshot.get("turn_id") is None:
                break
            time.sleep(0.01)
        else:
            self.fail("bridge did not enter in-flight turn/start state")
        cancel_event.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["result"].status, "uncertain")
        methods = self.methods()
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/interrupt", methods)

    def test_timeout_forces_process_stop_before_unlock(self):
        self.bridge.close()
        self.bridge = CodexAppBridge(
            command=[sys.executable, str(self.server_path), str(self.log_path)],
            codex_home=str(self.root / "codex-home"),
            request_timeout_sec=2.0,
            interrupt_grace_sec=0.1,
        )
        result = self.bridge.run_turn(
            thread_id=None,
            cwd=self.root,
            prompt="stubborn",
            model="gpt-test",
            effort="high",
            max_runtime_sec=0.1,
        )
        self.assertEqual(result.status, "uncertain")
        self.assertIsNone(self.bridge.snapshot()["pid"])
        self.assertIn("turn/interrupt", self.methods())

        recovered, _, _ = self.run_normal(result.thread_id)
        self.assertEqual(recovered.status, "completed")

    def test_close_kills_descendants_that_ignore_sigterm(self):
        child_server = self.root / "child_app_server.py"
        child_server.write_text(CHILD_FAKE_APP_SERVER, encoding="utf-8")
        log_path = self.root / "child-rpc.jsonl"
        bridge = CodexAppBridge(
            command=[sys.executable, str(child_server), str(log_path)],
            codex_home=str(self.root / "child-home"),
            request_timeout_sec=2.0,
        )
        try:
            result = bridge.run_turn(
                thread_id=None,
                cwd=self.root,
                prompt="normal",
                model="gpt-test",
                effort="high",
            )
            self.assertEqual(result.status, "completed")
            child_pid = int(Path(str(log_path) + ".child").read_text())
        finally:
            bridge.close()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("app-server descendant survived bridge.close()")


if __name__ == "__main__":
    unittest.main()
