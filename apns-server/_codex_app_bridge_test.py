import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from codex_app_bridge import CodexActiveTurnError, CodexAppBridge


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
                "item": {"id": f"cmd-{turn_number}", "type": "commandExecution"},
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
        result = self.bridge.run_turn(
            thread_id=None,
            cwd=self.root,
            prompt="normal",
            model="gpt-test",
            effort="high",
            cancel_event=cancel_event,
        )
        self.assertEqual(result.status, "interrupted")
        self.assertNotIn("turn/start", self.methods())

    def test_cancel_during_turn_start_interrupts_as_soon_as_id_arrives(self):
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
        self.assertEqual(holder["result"].status, "interrupted")
        methods = self.methods()
        self.assertIn("turn/start", methods)
        self.assertIn("turn/interrupt", methods)

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
