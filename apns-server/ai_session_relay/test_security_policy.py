from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import security_policy as policy


class SecurityPolicyTests(unittest.TestCase):
    def make_env(self) -> tuple[tempfile.TemporaryDirectory[str], dict[str, str]]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "xia-relay"
        bin_dir = Path(tmp.name) / "global-bin"
        workspace = root / "workspace"
        state = root / "state"
        for path in (
            workspace,
            state / "claude-home",
            state / "codex-home",
            state / "runtime-home",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (workspace / "CLAUDE.md").write_text("persona", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("persona", encoding="utf-8")
        (state / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
        bin_dir.mkdir(mode=0o755)
        for name in ("claude", "codex"):
            binary = bin_dir / name
            binary.write_bytes(b"test executable")
            os.chmod(binary, 0o755)
        for path in (root, workspace, state, state / "claude-home",
                     state / "codex-home", state / "runtime-home"):
            os.chmod(path, 0o700)
        for path in (workspace / "CLAUDE.md", workspace / "AGENTS.md",
                     state / "empty-mcp.json"):
            os.chmod(path, 0o600)
        env = {
            "AI_RELAY_EXECUTION_MODE": "chat_only",
            "AI_RELAY_INSTANCE_ROOT": str(root),
            "AI_RELAY_WORKSPACE": str(workspace),
            "AI_RELAY_STATE_DIR": str(state),
            "AI_RELAY_HOST": "127.0.0.1",
            "CLAUDE_CONFIG_DIR": str(state / "claude-home"),
            "CODEX_HOME": str(state / "codex-home"),
            "HOME": str(state / "runtime-home"),
            "AI_RELAY_CLAUDE_BIN": str(bin_dir / "claude"),
            "AI_RELAY_CODEX_BIN": str(bin_dir / "codex"),
            "PATH": "/usr/bin:/bin",
            "SECRET_SHOULD_NOT_LEAK": "private",
        }
        return tmp, env

    @staticmethod
    def load(env: dict[str, str]) -> policy.RelayPolicy:
        with mock.patch.object(policy, "_mount_is_read_only", return_value=True):
            return policy.load_policy(env)

    def test_unknown_or_missing_mode_fails_closed(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        for value in ("", "autonomous", "CHAT_ONLY", "chat-only"):
            env["AI_RELAY_EXECUTION_MODE"] = value
            with self.assertRaises(policy.SecurityPolicyError):
                self.load(env)

    def test_paths_must_be_disjoint_children_and_loopback(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        outside = Path(tmp.name) / "outside"
        outside.mkdir()
        env["AI_RELAY_WORKSPACE"] = str(outside)
        with self.assertRaises(policy.SecurityPolicyError):
            self.load(env)
        env["AI_RELAY_WORKSPACE"] = str(Path(env["AI_RELAY_INSTANCE_ROOT"]) / "workspace")
        env["AI_RELAY_HOST"] = "0.0.0.0"
        with self.assertRaises(policy.SecurityPolicyError):
            self.load(env)

    def test_policy_validates_read_only_root_workspace_and_tightens_state(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        root = Path(env["AI_RELAY_INSTANCE_ROOT"])
        os.chmod(root, 0o755)
        with self.assertRaises(policy.SecurityPolicyError):
            self.load(env)
        os.chmod(root, 0o700)
        state = Path(env["AI_RELAY_STATE_DIR"])
        os.chmod(state, 0o755)
        loaded = self.load(env)
        self.assertEqual(stat.S_IMODE(loaded.state_dir.stat().st_mode), 0o700)
        with mock.patch.object(policy, "_mount_is_read_only", return_value=False):
            with self.assertRaises(policy.SecurityPolicyError):
                policy.load_policy(env)

    def test_child_environment_is_allowlisted_and_isolated(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        loaded = self.load(env)
        child = policy.child_env(loaded, env)
        self.assertNotIn("SECRET_SHOULD_NOT_LEAK", child)
        self.assertEqual(child["CODEX_HOME"], str(loaded.codex_home))
        self.assertEqual(child["CLAUDE_CONFIG_DIR"], str(loaded.claude_config_dir))
        self.assertEqual(child["CLAUDE_CODE_SAFE_MODE"], "1")

    def test_claude_command_has_hard_tool_and_mcp_denials(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        loaded = self.load(env)
        args = policy.claude_args(loaded, "hello", "sid", "opus", "high", {"high"})
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertIn("--safe-mode", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(
            args[args.index("--system-prompt-file") + 1],
            str(loaded.workspace / "CLAUDE.md"),
        )
        self.assertEqual(stat.S_IMODE((loaded.workspace / "CLAUDE.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((loaded.workspace / "AGENTS.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((loaded.state_dir / "empty-mcp.json").stat().st_mode), 0o600)

    def test_codex_command_and_thread_are_read_only(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        loaded = self.load(env)
        args = policy.codex_args(loaded)
        joined = " ".join(args)
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertIn('approval_policy="never"', args)
        self.assertIn('web_search="disabled"', args)
        self.assertIn("mcp_servers={}", args)
        self.assertNotIn("workspace-write", joined)
        self.assertNotIn("danger-full-access", joined)
        self.assertEqual(
            policy.codex_thread_security(),
            {"approvalPolicy": "never", "sandbox": "read-only"},
        )

    def test_chat_only_rejects_tool_events_and_approvals(self) -> None:
        with self.assertRaises(policy.ToolUseBlocked):
            policy.assert_no_claude_tool("tool_use")
        for item_type in ("commandExecution", "mcpToolCall", "futureToolType"):
            with self.assertRaises(policy.ToolUseBlocked):
                policy.assert_no_codex_tool({"type": item_type})
        for item_type in ("agentMessage", "reasoning", "plan", "userMessage"):
            policy.assert_no_codex_tool({"type": item_type})
        policy.assert_safe_codex_notification(
            "item/started", {"item": {"type": "agentMessage"}}
        )
        policy.assert_safe_codex_notification(
            "item/completed", {"item": {"type": "reasoning"}}
        )
        policy.assert_safe_codex_notification("item/agentMessage/delta", {})
        for method, params in (
            ("item/completed", {"item": {"type": "commandExecution"}}),
            ("item/started", {}),
            ("item/commandExecution/outputDelta", {"itemId": "x"}),
            ("item/futureLifecycle", {}),
        ):
            with self.assertRaises(policy.ToolUseBlocked):
                policy.assert_safe_codex_notification(method, params)
        for method, params in (
            ("item/started", {"item": {"type": "mcpToolCall"}}),
            ("item/completed", {"item": {"type": "commandExecution"}}),
            ("item/futureToolProgress", {}),
        ):
            with mock.patch.object(
                policy, "terminate_process_group", new=mock.AsyncMock()
            ) as terminate:
                with self.assertRaises(policy.ToolUseBlocked):
                    asyncio.run(policy.guard_codex_notification(object(), method, params))
                terminate.assert_awaited_once()
        self.assertEqual(
            policy.denial_for_server_request("mcpServer/elicitation/request"),
            {"action": "cancel"},
        )
        self.assertEqual(
            policy.denial_for_server_request("item/commandExecution/requestApproval"),
            {"decision": "cancel"},
        )

    def test_request_mode_is_exact(self) -> None:
        policy.assert_request_mode("chat_only")
        for value in (None, "", "autonomous", "CHAT_ONLY"):
            with self.assertRaises(policy.SecurityPolicyError):
                policy.assert_request_mode(value)

    def test_systemd_template_keeps_root_source_behind_read_only_bind(self) -> None:
        unit = (Path(__file__).with_name("cc-xia-ai-session-relay.service.in")
                .read_text(encoding="utf-8"))
        self.assertIn("User=cc-xia-relay", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("ProtectHome=yes", unit)
        self.assertIn(
            "BindReadOnlyPaths=/root/CcCompanion/apns-server/state/ai_relay_workspace:"
            "/var/lib/cc-xia-relay/workspace",
            unit,
        )
        self.assertNotIn("ReadWritePaths=/root", unit)

        prepare = (Path(__file__).with_name("prepare-runtime.sh")
                   .read_text(encoding="utf-8"))
        self.assertIn("BACKEND_WORKSPACE_SOURCE=", prepare)
        self.assertIn('install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700', prepare)
        self.assertIn('chmod 0600 "$BACKEND_WORKSPACE_SOURCE/$persona"', prepare)
        self.assertIn('chmod 0755 "$DEST"', prepare)
        self.assertIn('-o root -g root -m 0644 "$HERE/security_policy.py"', prepare)
        self.assertIn('chown -R root:root "$DEST/source"', prepare)
        self.assertIn('[[ "$resolved" != /root/* ]]', prepare)
        self.assertIn('runuser -u "$SERVICE_USER" -- test -x "$candidate"', prepare)
        self.assertIn("CODEX_SOURCE_BIN=", prepare)
        self.assertIn(
            'install -o root -g root -m 0755 "$CODEX_SOURCE_BIN" "$DEST/bin/codex"',
            prepare,
        )
        self.assertIn("/root/.codex/packages/standalone/releases/*/bin/codex", prepare)
        self.assertIn('"$DEST/bin/codex" login status', prepare)
        self.assertIn('"$CLAUDE_CREDENTIAL_SOURCE" "$INSTANCE_ROOT/state/claude-home/.credentials.json"', prepare)
        self.assertIn('"$CODEX_AUTH_SOURCE" "$INSTANCE_ROOT/state/codex-home/auth.json"', prepare)
        self.assertNotIn("cp -R", prepare)

        smoke = (Path(__file__).with_name("smoke-codex-app-server.py")
                 .read_text(encoding="utf-8"))
        self.assertIn("module.codex_args", smoke)
        self.assertIn('"method": "model/list"', smoke)
        self.assertNotIn('"method": "thread/start"', smoke)
        self.assertNotIn('"method": "turn/start"', smoke)

        patch = (Path(__file__).with_name("upstream-chat-only.patch")
                 .read_text(encoding="utf-8"))
        self.assertIn("request_id = security_policy.validate_request_id", patch)
        self.assertIn("_EMPTY_RETRY_MAX = 0", patch)
        self.assertIn("_ENGINE_RECOVERY_RETRIES = 0", patch)

    def test_durable_json_replace_fsyncs_and_preserves_old_on_pre_rename_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"epoch":1}', encoding="utf-8")
            os.chmod(path, 0o600)
            with mock.patch.object(policy.os, "fsync", wraps=os.fsync) as fsync:
                policy.durable_json_replace(path, {"epoch": 2, "pending_switch": True})
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn('"epoch": 2', path.read_text(encoding="utf-8"))

            old = path.read_bytes()
            with mock.patch.object(policy.os, "replace", side_effect=OSError("crash")):
                with self.assertRaises(OSError):
                    policy.durable_json_replace(path, {"epoch": 3})
            self.assertEqual(path.read_bytes(), old)

            real_fsync = os.fsync
            calls = 0

            def fail_parent_fsync(fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("power loss after rename")
                real_fsync(fd)

            with mock.patch.object(policy.os, "fsync", side_effect=fail_parent_fsync):
                with self.assertRaises(OSError):
                    policy.durable_json_replace(
                        path, {"epoch": 4, "pending_switch": True}
                    )
            # Rename already happened; retrying the same idempotent state is safe.
            self.assertIn('"epoch": 4', path.read_text(encoding="utf-8"))
            policy.durable_json_replace(path, {"epoch": 4, "pending_switch": True})

    def test_refresh_pending_epoch_is_idempotent_until_consumed(self) -> None:
        state = {"epoch": 4, "pending_switch": False}
        policy.mark_refresh_pending(state)
        self.assertEqual(state, {"epoch": 5, "pending_switch": True})
        policy.mark_refresh_pending(state)
        self.assertEqual(state, {"epoch": 5, "pending_switch": True})
        state["pending_switch"] = False
        policy.mark_refresh_pending(state)
        self.assertEqual(state, {"epoch": 6, "pending_switch": True})

    def test_request_id_contract_matches_backend(self) -> None:
        for value in ("a", "msg-123_ABC:4.5", "x" * 200):
            self.assertEqual(policy.validate_request_id(value), value)
        for value in (None, "", "-bad", "has space", "x" * 201, "换行\n"):
            with self.assertRaises(policy.SecurityPolicyError):
                policy.validate_request_id(value)

    def test_request_ledger_replays_only_authoritative_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = policy.RequestLedger(Path(tmp) / "requests.json")
            data = ledger.load()
            rec = ledger.accept(data, "claude", 7, "msg-1", "process-a")
            self.assertEqual(ledger.find_request(ledger.load(), "msg-1")["status"], "accepted")
            data = ledger.load()
            rec = ledger.find_request(data, "msg-1")
            self.assertFalse(ledger.transition(data, rec, "running"))
            done = {"done": True, "full": "answer", "parts": [], "provider": "claude"}
            data = ledger.load()
            rec = ledger.find_request(data, "msg-1")
            self.assertTrue(ledger.transition(data, rec, "completed", done=done))
            loaded = ledger.load()
            replay = ledger.find_request(loaded, "msg-1")
            self.assertEqual(replay["status"], "completed")
            self.assertEqual(replay["done"], done)
            with self.assertRaises(policy.SecurityPolicyError):
                ledger.transition(loaded, replay, "completed", done=done)

    def test_crashed_running_is_terminal_uncertain_and_never_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = policy.RequestLedger(Path(tmp) / "requests.json")
            data = ledger.load()
            ledger.accept(data, "codex", 3, "msg-crash", "old-process")
            data = ledger.load()
            rec = ledger.find_request(data, "msg-crash")
            ledger.transition(data, rec, "running")

            after_restart = ledger.load()
            crashed = ledger.crashed_active(after_restart, "new-process")
            self.assertEqual([x["request_id"] for x in crashed], ["msg-crash"])
            ledger.mark_crashed_uncertain(after_restart, crashed)
            terminal = ledger.find_request(ledger.load(), "msg-crash")
            self.assertEqual(terminal["status"], "uncertain")
            self.assertNotIn("done", terminal)
            self.assertEqual(ledger.crashed_active(ledger.load(), "third-process"), [])

    def test_crash_recovery_arms_fresh_epoch_before_terminalizing_ledger(self) -> None:
        tmp, env = self.make_env()
        self.addCleanup(tmp.cleanup)
        loaded_policy = self.load(env)
        for name in ("headless_last_session", "codex_last_thread", "codex_thread_epoch"):
            (loaded_policy.state_dir / name).write_text("stale", encoding="utf-8")
        state = {"provider": "claude", "epoch": 9, "pending_switch": False}
        policy.mark_refresh_pending(state)
        policy.invalidate_provider_resume(loaded_policy, "claude")
        self.assertEqual(state, {"provider": "claude", "epoch": 10, "pending_switch": True})
        self.assertFalse((loaded_policy.state_dir / "headless_last_session").exists())
        # Simulate a second crash before ledger terminalization. Repeating the
        # conservative fence is safe and still cannot resume the stale session.
        policy.mark_refresh_pending(state)
        policy.invalidate_provider_resume(loaded_policy, "claude")
        self.assertEqual(state["epoch"], 10)
        self.assertTrue(state["pending_switch"])

        policy.invalidate_provider_resume(loaded_policy, "codex")
        self.assertFalse((loaded_policy.state_dir / "codex_last_thread").exists())
        self.assertFalse((loaded_policy.state_dir / "codex_thread_epoch").exists())

    def test_corrupt_or_duplicate_request_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.json"
            ledger = policy.RequestLedger(path)
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(policy.SecurityPolicyError):
                ledger.load()
            rec = {
                "provider": "claude", "epoch": 1, "request_id": "same",
                "status": "uncertain", "owner": "old", "seq": 1,
            }
            path.write_text(json.dumps({
                "version": 1, "next_seq": 3, "records": [rec, {**rec, "seq": 2}],
            }), encoding="utf-8")
            with self.assertRaises(policy.SecurityPolicyError):
                ledger.load()

    def test_smoke_helper_executes_dynamically_loaded_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_policy = root / "security_policy.py"
            fake_policy.write_text(
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Marker:\n    value: str = 'ok'\n"
                "def codex_args(policy): return [str(policy.codex_bin)]\n",
                encoding="utf-8",
            )
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "for line in sys.stdin:\n"
                " m=json.loads(line)\n"
                " if m.get('id') == 1: print(json.dumps({'id':1,'result':{}}), flush=True)\n"
                " if m.get('id') == 2: print(json.dumps({'id':2,'result':{}}), flush=True)\n",
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o755)
            smoke = Path(__file__).with_name("smoke-codex-app-server.py")
            result = subprocess.run(
                [sys.executable, str(smoke), str(fake_policy), str(fake_codex)],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": str(root), "CODEX_HOME": str(root)},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
