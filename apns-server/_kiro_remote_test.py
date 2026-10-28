"""Kiro on a remote host (kiro 搬东京, 2026-10-28).

* remote_stdio: the ssh argv it builds, popen/run/stop/put_file plumbing.
* KiroACPClient with ``remote``: spawn through the transport, remote cwd
  verbatim, remote stop on close; local mode untouched.
* KiroTerminalBridge with ``remote``: tmux, session-file reads, /proc and
  signals all go to the remote host; never a local os.kill.
* push.py: config switch, remote delegate inventory, attachment copies.

No test opens a real ssh connection.  The remote python helpers are executed
locally against temp files so their parsing is exercised for real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kiro_terminal  # noqa: E402
from kiro_acp import KiroACPClient  # noqa: E402
from kiro_terminal import (  # noqa: E402
    KIRO_TERMINAL_OWNER_OPTION,
    KIRO_TERMINAL_OWNER_VALUE,
    KIRO_TERMINAL_SESSION_OPTION,
    KiroTerminalBridge,
    KiroTerminalUnavailable,
    RemoteKiroSessionTurnProbe,
)
import push  # noqa: E402
from remote_stdio import RemoteStdio, RemoteStdioTarget  # noqa: E402

SID = "0f20582f-6fdb-43da-98cc-a3e83c032cf7"


def _done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _target(**overrides):
    values = dict(
        host="tokyo", user="kiro", cwd="/home/kiro/Kiro-Workspace",
        env={"HOME": "/home/kiro", "LANG": "C.UTF-8"},
        pidfile="/home/kiro/.cache/ccc/kiro-acp.pid",
        control_path="/root/.ssh/ccc-kiro-%C",
    )
    values.update(overrides)
    return RemoteStdioTarget(**values)


def _remote_words(argv):
    """The remote command words of a local ``ssh ... host <cmd>`` argv."""
    host_index = argv.index("tokyo")
    return shlex.split(" ".join(argv[host_index + 1:]))


class RemoteStdioTest(unittest.TestCase):
    def test_launch_is_a_dedicated_connection_with_remote_cwd_env_and_pidfile(self):
        calls = []
        remote = RemoteStdio(_target(), popen=lambda argv, **kw: calls.append((argv, kw)) or "proc")
        self.assertEqual("proc", remote.popen(
            ["/home/kiro/.local/bin/kiro-cli", "acp"], cwd="/root/Karami-Workspace",
            stdin=subprocess.PIPE, start_new_session=True,
        ))
        argv, kwargs = calls[0]
        self.assertNotIn("cwd", kwargs)  # a local cwd means nothing remotely
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(["ssh", "-T"], argv[:2])
        self.assertIn("ControlPath=none", argv)  # never multiplexed
        self.assertEqual(["-l", "kiro", "tokyo"], argv[argv.index("tokyo") - 2:argv.index("tokyo") + 1])
        words = _remote_words(argv)
        self.assertEqual(["sh", "-c"], words[:2])
        self.assertEqual(
            ["ccc-remote-stdio", "/home/kiro/.cache/ccc/kiro-acp.pid", "/home/kiro/Kiro-Workspace",
             "/usr/bin/env", "HOME=/home/kiro", "LANG=C.UTF-8",
             "/home/kiro/.local/bin/kiro-cli", "acp"],
            words[3:],
        )
        self.assertIn("setsid -w", words[2])

    def test_launch_script_really_records_its_group_and_execs(self):
        """Run the launch script locally (no ssh) and stop it like a remote would."""
        tmp = Path(tempfile.mkdtemp(prefix="remote-stdio-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pidfile = tmp / "run" / "agent.pid"
        target = _target(cwd=str(tmp), pidfile=str(pidfile), env={"CCC_TEST": "1"})
        remote = RemoteStdio(target)
        words = _remote_words(remote.launch_argv(["sh", "-c", 'echo "$CCC_TEST $(pwd)"; exec cat']))
        proc = subprocess.Popen(words, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stdin.close)
        try:
            self.assertEqual(f"1 {tmp}", proc.stdout.readline().strip())
            pid, start = pidfile.read_text().split()
            self.assertEqual(int(pid), os.getpgid(int(pid)))  # its own process group
            # The stop script (run locally) ends exactly that group.
            local = RemoteStdio(target, runner=lambda argv, **kw: subprocess.run(
                _remote_words(argv), **kw))
            self.assertTrue(local.stop(timeout=10))
            self.assertFalse(pidfile.exists())
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_stop_ignores_a_recycled_pid(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-stdio-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pidfile = tmp / "agent.pid"
        # Our own pid with a wrong starttime: must not be signalled.
        pidfile.write_text(f"{os.getpid()} 1\n")
        remote = RemoteStdio(_target(pidfile=str(pidfile)), runner=lambda argv, **kw: subprocess.run(
            _remote_words(argv), **kw))
        self.assertTrue(remote.stop(timeout=10))
        self.assertFalse(pidfile.exists())

    def test_run_multiplexes_and_sudo_mode_wraps(self):
        seen = []
        remote = RemoteStdio(_target(user="", sudo_user="pi"),
                             runner=lambda argv, **kw: seen.append((argv, kw)) or _done())
        remote.run(["tmux", "has-session", "-t", "=x y"])
        argv, kwargs = seen[0]
        self.assertIn("ControlMaster=auto", argv)
        self.assertNotIn("-l", argv)
        self.assertEqual(["sudo", "-n", "-u", "pi", "--", "tmux", "has-session", "-t", "=x y"],
                         _remote_words(argv))
        self.assertEqual("utf-8", kwargs["encoding"])

    def test_target_validation(self):
        for bad in ({"host": "to kyo"}, {"user": "Kiro;x"}, {"cwd": "relative"},
                    {"env": {"A B": "1"}}):
            with self.assertRaises(ValueError):
                _target(**bad)

    def test_put_file_streams_binary_stdin(self):
        tmp = Path(tempfile.mkdtemp(prefix="remote-stdio-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = tmp / "src.bin"
        src.write_bytes(b"\x00\x01payload")
        dest = tmp / "deep" / "dest.bin"
        remote = RemoteStdio(_target(), runner=lambda argv, **kw: subprocess.run(_remote_words(argv), **kw))
        self.assertTrue(remote.put_file(str(src), str(dest)))
        self.assertEqual(b"\x00\x01payload", dest.read_bytes())
        self.assertEqual(0o600, dest.stat().st_mode & 0o777)
        self.assertFalse(remote.put_file(str(tmp / "missing"), str(dest)))


    def test_get_file_reads_only_regular_files_under_the_roots(self):
        """Run the get script locally (no ssh) against real files and symlinks."""
        tmp = Path(tempfile.mkdtemp(prefix="remote-stdio-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        (home / "pics").mkdir(parents=True)
        pic = home / "pics" / "a b.png"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
        outside = tmp / "secret.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
        (home / "link.png").symlink_to(outside)
        remote = RemoteStdio(_target(), runner=lambda argv, **kw: subprocess.run(_remote_words(argv), **kw))
        data, why = remote.get_file(str(pic), max_bytes=1000, roots=[str(home)])
        self.assertEqual(("", pic.read_bytes()), (why, data))
        self.assertEqual((None, "outside"), remote.get_file(str(outside), max_bytes=1000, roots=[str(home)]))
        self.assertEqual((None, "outside"), remote.get_file(str(home / "link.png"), max_bytes=1000, roots=[str(home)]))
        self.assertEqual((None, "outside"), remote.get_file(str(home / ".." / "secret.png"), max_bytes=1000, roots=[str(home)]))
        self.assertEqual((None, "missing"), remote.get_file(str(home / "nope.png"), max_bytes=1000, roots=[str(home)]))
        self.assertEqual((None, "missing"), remote.get_file(str(home / "pics"), max_bytes=1000, roots=[str(home)]))
        self.assertEqual((None, "too_large"), remote.get_file(str(pic), max_bytes=10, roots=[str(home)]))
        self.assertEqual((None, "outside"), remote.get_file(str(pic), max_bytes=1000, roots=[]))
        with self.assertRaises(ValueError):
            remote.get_file("relative.png", max_bytes=10, roots=[str(home)])
        broken = RemoteStdio(_target(), runner=lambda argv, **kw: _done(returncode=255))
        self.assertEqual((None, "failed"), broken.get_file(str(pic), max_bytes=1000, roots=[str(home)]))


class _FakeProc:
    def __init__(self):
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.pid = 999999
        self.returncode = 0

    def poll(self):
        return 0


class KiroACPRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiro-acp-remote-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "kiro_acp_session.json"

    def _remote(self):
        remote = mock.Mock(spec=RemoteStdio)
        remote.stop.return_value = True
        return remote

    def test_remote_paths_are_verbatim_and_pointer_matches_without_resolve(self):
        remote = self._remote()
        client = KiroACPClient(command="/home/kiro/.local/bin/kiro-cli", cwd="/home/kiro/Kiro-Workspace/",
                               state_path=self.state, remote=remote)
        self.assertEqual("/home/kiro/Kiro-Workspace", str(client.cwd))
        self.assertEqual(["/home/kiro/.local/bin/kiro-cli", "acp"], client.argv)
        self.state.write_text(json.dumps({"version": 2, "session_id": SID, "cwd": "/home/kiro/Kiro-Workspace"}))
        self.assertEqual(SID, client.load_session_id())
        self.state.write_text(json.dumps({"version": 2, "session_id": SID, "cwd": "/root/Karami-Workspace"}))
        self.assertEqual("", client.load_session_id())

    def test_spawn_goes_through_the_transport_with_configured_argv(self):
        remote = self._remote()
        remote.popen.side_effect = RuntimeError("no ssh in tests")
        factory = mock.Mock()
        client = KiroACPClient(command="/x/kiro-cli", cwd="/home/kiro/w", state_path=self.state,
                               remote=remote, popen_factory=factory, argv=["/x/kiro-cli", "acp", "--trust"])
        with self.assertRaises(Exception):
            client.prepare_session()
        factory.assert_not_called()
        argv = remote.popen.call_args.args[0]
        self.assertEqual(["/x/kiro-cli", "acp", "--trust"], argv)

    def test_close_stops_the_remote_group_even_after_ssh_died(self):
        remote = self._remote()
        client = KiroACPClient(command="/x/kiro-cli", cwd="/home/kiro/w", state_path=self.state, remote=remote)
        client._process = _FakeProc()  # ssh already exited (connection drop)
        with mock.patch("os.killpg") as killpg:
            client.close()
        killpg.assert_not_called()
        remote.stop.assert_called_once()
        remote.stop.reset_mock()
        client.close()  # nothing running: no remote call
        remote.stop.assert_not_called()

    def test_local_mode_is_unchanged(self):
        factory = mock.Mock(side_effect=RuntimeError("boom"))
        client = KiroACPClient(command="/x/kiro-cli", cwd=str(self.tmp), state_path=self.state,
                               popen_factory=factory)
        self.assertIsNone(client.remote)
        with self.assertRaises(Exception):
            client.prepare_session()
        self.assertEqual(["/x/kiro-cli", "acp"], factory.call_args.args[0])
        self.assertEqual(str(self.tmp.resolve()), factory.call_args.kwargs["cwd"])


class FakeRemoteHost:
    """A remote host for the terminal bridge: fake tmux, real helper script.

    ``run(argv)`` receives the *remote* argv (what RemoteStdio would quote
    into ssh).  python3 helper calls run locally against ``self.root``.
    """

    def __init__(self, root: Path):
        self.root = root
        self.sessions_dir = root / "sessions"
        self.sessions_dir.mkdir()
        self.target = types.SimpleNamespace(env={"HOME": "/home/kiro", "LANG": "C.UTF-8"})
        self.calls: list[list[str]] = []
        self.session: dict | None = None
        self.foreign_name: str | None = None
        self.killed: list[list[str]] = []
        self.transport_down = False
        self.paths_ok = True
        self.lock = threading.Lock()

    def run(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        with self.lock:
            self.calls.append(argv)
        if self.transport_down:
            return _done(255, stderr="ssh: connect to host tokyo port 22: Connection timed out")
        if argv[0] == "python3":
            script_args = argv[3:]
            if script_args[0] == "paths":
                return _done(stdout=json.dumps({"ok": self.paths_ok}))
            if script_args[0] == "proc" and self.session is not None:
                pid = int(script_args[1])
                chain = [pid, self.session["pid"], 1] if pid == self.session.get("lock_owner") else [pid, 1]
                return _done(stdout=json.dumps({"alive": not self.session["dead"], "chain": chain}))
            return subprocess.run([sys.executable, "-c", argv[2], *script_args],
                                  capture_output=True, text=True, timeout=10)
        if argv[0] == "kill":
            self.killed.append(argv)
            if self.session is not None:
                self.session["dead"] = True
                (self.sessions_dir / f"{SID}.lock").unlink(missing_ok=True)
            return _done()
        assert argv[0] == "tmux", argv
        verb = argv[1]
        if verb == "display-message":
            if self.session is None:
                if self.foreign_name:  # tmux 3.6: unknown -t falls back to another session
                    return _done(stdout=f"{self.foreign_name}\t\t\t%9\t0\t77\n")
                return _done(1, stderr="no server running on /tmp/tmux-1002/default")
            s = self.session
            return _done(stdout="\t".join([
                "ccc-kiro-terminal", s["options"].get(KIRO_TERMINAL_OWNER_OPTION, ""),
                s["options"].get(KIRO_TERMINAL_SESSION_OPTION, ""), s["pane"],
                "1" if s["dead"] else "0", str(s["pid"]),
            ]) + "\n")
        if verb == "new-session":
            self.session = {"pane": "%5", "pid": 5000, "dead": False, "options": {}, "argv": argv}
            return _done()
        if verb == "set-option":
            self.session["options"][argv[4]] = argv[5]
            return _done()
        if verb == "respawn-pane":
            self.session["argv"] = argv
            self.session["pid"] = 5001
            self.session["lock_owner"] = 5002
            (self.sessions_dir / f"{SID}.lock").write_text(json.dumps({"pid": 5002}))
            return _done()
        if verb == "kill-session":
            self.session = None
            return _done()
        if verb in {"capture-pane", "send-keys", "set-buffer", "paste-buffer", "delete-buffer",
                    "resize-window"}:
            return _done(stdout="›  ask a question\n")
        raise AssertionError(argv)


class KiroTerminalRemoteTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kiro-term-remote-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.host = FakeRemoteHost(self.root)
        self.local_runner = mock.Mock(side_effect=AssertionError("local tmux must not run"))
        self.bridge = KiroTerminalBridge(
            command="/home/kiro/.local/bin/kiro-cli", cwd="/home/kiro/Kiro-Workspace",
            sessions_dir=self.host.sessions_dir, runner=self.local_runner,
            remote=self.host, shutdown_wait_seconds=0.05, resume_wait_seconds=2,
            lock_release_wait_seconds=0.5, sleep=lambda _s: None,
        )
        self.addCleanup(self._clear)

    def _clear(self):
        with self.bridge._lock:
            self.bridge._clear_lease_locked()

    def test_ensure_runs_remote_tui_with_remote_env_and_paths(self):
        with mock.patch("os.kill", side_effect=AssertionError("local os.kill of a remote pid")):
            pane = self.bridge.ensure(SID, model="claude-opus-5.5")
        self.assertEqual("%5", pane)
        respawn = self.host.session["argv"]
        self.assertEqual(
            ["/usr/bin/env", "CCC_KIRO_TERMINAL_BRIDGE=1", "HOME=/home/kiro", "LANG=C.UTF-8",
             "/home/kiro/.local/bin/kiro-cli", "chat", "--resume-id", SID, "--model", "claude-opus-5.5"],
            respawn[respawn.index("/home/kiro/Kiro-Workspace") + 1:],
        )
        self.assertEqual(KIRO_TERMINAL_OWNER_VALUE, self.host.session["options"][KIRO_TERMINAL_OWNER_OPTION])
        self.local_runner.assert_not_called()
        self.assertIsInstance(self.bridge.turn_probe, RemoteKiroSessionTurnProbe)

    def test_release_signals_the_remote_pane_group_never_locally(self):
        self.bridge.ensure(SID)
        (self.host.sessions_dir / f"{SID}.json").write_text(json.dumps(
            {"session_state": {"conversation_metadata": {"user_turn_metadatas": [{}]}}}))
        (self.host.sessions_dir / f"{SID}.jsonl").write_text(
            json.dumps({"kind": "Prompt"}) + "\n" + json.dumps({"kind": "Other"}) + "\n")
        with mock.patch("os.kill", side_effect=AssertionError("local os.kill of a remote pid")):
            self.assertTrue(self.bridge.release_for_writer(SID))
        self.assertIsNone(self.host.session)
        self.assertEqual(["kill", "-15", "--", "-5001"], self.host.killed[0])

    def test_running_remote_turn_blocks_the_handoff(self):
        self.bridge.ensure(SID)
        (self.host.sessions_dir / f"{SID}.json").write_text(json.dumps(
            {"session_state": {"conversation_metadata": {"user_turn_metadatas": []}}}))
        (self.host.sessions_dir / f"{SID}.jsonl").write_text(json.dumps({"kind": "Prompt"}) + "\n")
        with self.assertRaises(kiro_terminal.KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.assertEqual([], self.host.killed)

    def test_probe_is_incremental_and_transport_failure_reads_busy(self):
        probe = RemoteKiroSessionTurnProbe(self.host, self.host.sessions_dir)
        jsonl = self.host.sessions_dir / f"{SID}.jsonl"
        jsonl.write_text(json.dumps({"kind": "Prompt"}) + "\n")
        self.assertEqual((1, 0), probe.counts(SID))
        with jsonl.open("a") as handle:
            handle.write(json.dumps({"kind": "Prompt"}) + "\n" + '{"kind": "Pro')  # partial line
        self.assertEqual((2, 0), probe.counts(SID))
        self.assertEqual(jsonl.stat().st_size - len('{"kind": "Pro'), probe._cache[SID][1])
        self.host.transport_down = True
        self.assertIsNone(probe.counts(SID))
        self.assertIsNone(probe.busy(SID))

    def test_foreign_fallback_session_is_absent_and_transport_failure_raises(self):
        self.host.foreign_name = "kiro-own-bg-session"
        self.assertFalse(self.bridge.has_live_pane())
        self.bridge._identity_cache = None
        self.host.transport_down = True
        with self.assertRaises(KiroTerminalUnavailable):
            with self.bridge._lock:
                self.bridge._has_session_locked()

    def test_missing_remote_paths_refuse_before_tmux(self):
        self.host.paths_ok = False
        with self.assertRaises(KiroTerminalUnavailable):
            self.bridge.ensure(SID)
        self.assertFalse(any(c[:2] == ["tmux", "new-session"] for c in self.host.calls))


class PushKiroRemoteConfigTest(unittest.TestCase):
    def test_unset_host_keeps_local_defaults(self):
        remote, kiro_bin, kiro_cwd, _home = push._kiro_remote_from_config({})
        self.assertIsNone(remote)
        self.assertEqual(push.DEFAULT_KIRO_CWD, kiro_cwd)
        self.assertTrue(kiro_bin.endswith("/.local/bin/kiro-cli"))

    def test_remote_defaults(self):
        remote, kiro_bin, kiro_cwd, home = push._kiro_remote_from_config(
            {"kiro_ssh_host": "tokyo", "kiro_remote_user": "kiro"})
        self.assertEqual("/home/kiro", home)
        self.assertEqual("/home/kiro/.local/bin/kiro-cli", kiro_bin)
        self.assertEqual("/home/kiro/Kiro-Workspace", kiro_cwd)
        self.assertEqual("tokyo", remote.target.host)
        self.assertEqual("kiro", remote.target.user)
        self.assertEqual("/home/kiro/.cache/ccc/kiro-acp.pid", remote.target.pidfile)
        self.assertEqual("/home/kiro", remote.target.env["HOME"])


class PushKiroRemoteDelegateScanTest(unittest.TestCase):
    def test_remote_inventory_uses_remote_pid_liveness(self):
        tmp = Path(tempfile.mkdtemp(prefix="kiro-subagents-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data = tmp / "data" / ".subagents"
        data.mkdir(parents=True)
        workspace = tmp / "ws"
        (workspace / ".kiro" / ".subagents").mkdir(parents=True)
        dead_pid = 2 ** 22 + 12345  # beyond pid_max on test hosts: never alive
        (data / "a.json").write_text(json.dumps(
            {"agent": "a", "task": "dead run", "status": "running", "pid": dead_pid, "cwd": str(workspace)}))
        (data / "b.json").write_text(json.dumps(
            {"agent": "b", "task": "live run", "status": "running", "pid": os.getpid(), "cwd": "/elsewhere"}))
        (workspace / ".kiro" / ".subagents" / "c.json").write_text(json.dumps(
            {"agent": "c", "task": "done", "status": "completed"}))

        class Remote:
            def run(self, argv, **kwargs):
                return subprocess.run([sys.executable, "-c", argv[2], *argv[3:]],
                                      capture_output=True, text=True, timeout=10)

        with mock.patch("os.kill", side_effect=AssertionError("liveness must come from the remote")):
            result = push._scan_kiro_session_tasks_remote(
                Remote(), SID, workspace=str(workspace), data_dir=str(tmp / "data"))
        by_id = {entry["task_id"]: entry for entry in result["finished"] + result["pending"]}
        self.assertEqual("failed", by_id["a"]["status"])
        self.assertEqual("running", by_id["b"]["status"])
        self.assertTrue(by_id["b"]["description"].startswith("[来自 /elsewhere]"))
        self.assertEqual("completed", by_id["c"]["status"])

    def test_remote_failure_is_an_empty_inventory(self):
        remote = types.SimpleNamespace(run=mock.Mock(return_value=_done(255, stderr="down")))
        self.assertEqual({"finished": [], "pending": []},
                         push._scan_kiro_session_tasks_remote(remote, SID, workspace="/w", data_dir="/d"))


class PushKiroRemoteAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiro-attach-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.attachments = self.tmp / "attachments"
        self.attachments.mkdir()
        self.file = self.attachments / "abc123.pdf"
        self.file.write_bytes(b"%PDF")

    def _handler(self, remote):
        handler = push.PushHandler.__new__(push.PushHandler)
        handler.state = types.SimpleNamespace(
            attachments_dir=str(self.attachments), kiro_remote=remote, kiro_remote_home="/home/kiro")
        return handler

    def test_remote_kiro_gets_a_copy_on_its_host(self):
        remote = mock.Mock()
        remote.put_file.return_value = True
        remote.run.return_value = _done()
        hint, images = self._handler(remote)._kiro_attachment_prompt(
            [{"type": "file", "filename": "报告.pdf", "stored_path": str(self.file)}])
        remote.put_file.assert_called_once_with(
            str(self.file), "/home/kiro/.cache/ccc/attachments/abc123.pdf")
        self.assertIn("本地路径: /home/kiro/.cache/ccc/attachments/abc123.pdf", hint)
        self.assertNotIn(str(self.attachments), hint)
        self.assertEqual([], images)

    def test_failed_copy_says_so_and_local_mode_keeps_the_216_path(self):
        remote = mock.Mock()
        remote.put_file.return_value = False
        hint, _images = self._handler(remote)._kiro_attachment_prompt(
            [{"type": "file", "filename": "x.pdf", "stored_path": str(self.file)}])
        self.assertIn("没能同步", hint)
        hint, _images = self._handler(None)._kiro_attachment_prompt(
            [{"type": "file", "filename": "x.pdf", "stored_path": str(self.file)}])
        self.assertIn(f"本地路径: {self.file}", hint)



PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class PushKiroOutboundImageTest(unittest.TestCase):
    """发图 (2026-10-28): [[CCC_IMAGE:<path>]] -> fetched, sniffed, stored."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiro-outimg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.attachments = self.tmp / "attachments"
        self.attachments.mkdir()

    def _handler(self, remote, **extra):
        handler = push.PushHandler.__new__(push.PushHandler)
        handler.state = types.SimpleNamespace(
            attachments_dir=str(self.attachments), kiro_remote=remote, kiro_remote_home="/home/kiro", **extra)
        return handler

    def test_remote_marker_becomes_a_stored_image(self):
        remote = mock.Mock()
        remote.get_file.return_value = (PNG_BYTES, "")
        text, stored = self._handler(remote)._kiro_outbound_images(
            "给你画好了\n\n[[CCC_IMAGE:/home/kiro/out/猫 猫.png]]\n\n喜欢吗")
        self.assertEqual("给你画好了\n\n喜欢吗", text)
        remote.get_file.assert_called_once_with(
            "/home/kiro/out/猫 猫.png", max_bytes=10 * 1024 * 1024, roots=["/home/kiro", "/tmp"])
        self.assertEqual(1, len(stored))
        item = stored[0]
        path = Path(item["stored_path"])
        self.assertEqual(self.attachments.resolve(), path.parent)
        self.assertTrue(path.name.startswith("kiro-") and path.name.endswith(".png"))
        self.assertEqual(PNG_BYTES, path.read_bytes())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(("image", "image/png", f"/attachments/{path.name}"),
                         (item["type"], item["media_type"], item["attachment_url"]))
        self.assertTrue(item["filename"].endswith(".png"))

    def test_failures_become_notes_and_the_text_survives(self):
        remote = mock.Mock()
        remote.get_file.side_effect = [(None, "missing"), (b"not an image at all", ""), (None, "outside")]
        text, stored = self._handler(remote)._kiro_outbound_images(
            "看\n[[CCC_IMAGE:/home/kiro/a.png]]\n[[CCC_IMAGE:/home/kiro/b.png]]\n`[[CCC_IMAGE:/etc/c.png]]`\n"
            "[[CCC_IMAGE:relative.png]]")
        self.assertEqual([], stored)
        self.assertEqual([], list(self.attachments.iterdir()))
        self.assertTrue(text.startswith("看\n\n"))
        self.assertIn("a.png 没发出去：找不到这个文件", text)
        self.assertIn("b.png 没发出去：不是 jpg/png/gif/webp 图片", text)
        self.assertIn("c.png 没发出去：只能发家目录或 /tmp 下的文件", text)
        self.assertIn("relative.png 没发出去：路径必须是绝对路径", text)
        self.assertNotIn("[[CCC_IMAGE", text)
        self.assertEqual(3, remote.get_file.call_count)  # the relative path never reaches ssh

    def test_at_most_four_images_and_duplicates_once(self):
        remote = mock.Mock()
        remote.get_file.return_value = (PNG_BYTES, "")
        markers = "".join(f"[[CCC_IMAGE:/tmp/{i}.png]]\n" for i in (1, 1, 2, 3, 4, 5, 6))
        text, stored = self._handler(remote)._kiro_outbound_images(markers)
        self.assertEqual(4, len(stored))
        self.assertIn("一次最多发 4 张图，另外 2 张没发", text)

    def test_local_kiro_reads_only_its_workspace_or_tmp(self):
        workspace = self.tmp / "ws"
        workspace.mkdir()
        good = workspace / "ok.png"
        good.write_bytes(PNG_BYTES)
        elsewhere = Path("/etc/passwd")  # outside both workspace and /tmp
        handler = self._handler(None, kiro_acp=types.SimpleNamespace(cwd=workspace))
        text, stored = handler._kiro_outbound_images(f"[[CCC_IMAGE:{good}]]\n[[CCC_IMAGE:{elsewhere}]]")
        self.assertEqual(1, len(stored))
        self.assertIn("passwd 没发出去：只能发家目录或 /tmp 下的文件", text)

    def test_no_marker_is_untouched(self):
        remote = mock.Mock()
        self.assertEqual(("普通回复 [bqb:摸摸头]", []),
                         self._handler(remote)._kiro_outbound_images("普通回复 [bqb:摸摸头]"))
        remote.get_file.assert_not_called()

    def test_marker_helpers(self):
        import agent_images

        self.assertEqual("写到一半", agent_images.strip_markers("写到一半[[CCC_IMA"))
        self.assertEqual("写到一半", agent_images.strip_markers("写到一半[[CCC_IMAGE:/home/kiro/x"))
        self.assertEqual("a\nb", agent_images.strip_markers("a\n[[CCC_IMAGE:/tmp/x.png]]b"))
        self.assertEqual("[bqb:摸摸头]", agent_images.strip_markers("[bqb:摸摸头]"))
        self.assertEqual(("", ["/tmp/x.png"], 0), agent_images.extract_markers("[[CCC_IMAGE: /tmp/x.png ]]"))
        for header, ext in ((b"\xff\xd8\xff\xe0", ".jpg"), (PNG_BYTES, ".png"), (b"GIF89a", ".gif"),
                            (b"RIFF\x00\x00\x00\x00WEBP", ".webp")):
            self.assertEqual(ext, agent_images.sniff(header)[0])
        self.assertIsNone(agent_images.sniff(b"<svg"))


if __name__ == "__main__":
    unittest.main()
