"""Kimi terminal on a remote tmux server (卡拉米 on Tokyo, 2026-10-28).

Every tmux call and the pane SIGTERM must go through ``ssh <host> sudo -n``;
pane ids belong to the remote tmux server and must never reach 216's local
tmux or ``os.kill``.
"""

from pathlib import Path
import shlex
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from push import (  # noqa: E402
    KIMI_TERMINAL_OWNER_VALUE,
    KimiTerminalBridge,
    KimiTerminalUnavailable,
    PushHandler,
)


BINDING = "a" * 64


def _done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRemote:
    """Minimal remote host: records the remote argv of each ssh call."""

    def __init__(self, host="tokyo"):
        self.host = host
        self.calls: list[list[str]] = []
        self.session = None  # (owner, binding, pane, dead) or None
        self.pid = 4242
        self.killed: list[list[str]] = []
        self.transport_down = False

    def remote_argv(self, argv):
        assert argv[0] == "ssh", argv
        host_index = argv.index(self.host)
        assert argv[host_index + 1:host_index + 5] == ["sudo", "-n", "/usr/bin/env", "LANG=C.UTF-8"], argv
        # ssh joins the remaining words into one remote shell command line.
        return shlex.split(" ".join(argv[host_index + 5:]))

    def __call__(self, argv, **kwargs):
        remote = self.remote_argv(argv)
        self.calls.append(remote)
        if self.transport_down:
            return _done(255, stderr="ssh: connect to host tokyo: Connection timed out")
        if remote[0] == "kill":
            self.killed.append(remote)
            if self.session is not None:
                owner, binding, pane, _dead = self.session
                self.session = (owner, binding, pane, "1")
            return _done()
        if remote[0] == "test":
            return _done()
        assert remote[0] == "tmux", remote
        sub = remote[1]
        if sub == "display-message":
            target, fmt = remote[4], remote[5]
            if target.startswith("%"):
                if self.session is None or self.session[2] != target:
                    return _done(1, stderr=f"can't find pane: {target}")
                return _done(stdout=f"{target}|{self.pid}\n")
            if self.session is None:
                return _done(1, stderr="no server running on /tmp/tmux-0/default")
            owner, binding, pane, dead = self.session
            assert fmt.count("\t") == 4
            return _done(stdout="\t".join(("ccc-kimi-terminal", owner, binding, pane, dead)) + "\n")
        if sub == "kill-pane":
            if self.session is not None and self.session[2] == remote[3]:
                self.session = None
                return _done()
            return _done(1, stderr="can't find pane")
        return _done()


class RemoteKimiTerminalBridgeTest(unittest.TestCase):
    def bridge(self, remote, **kwargs):
        def forbidden_kill(*_args):
            raise AssertionError("remote pane pid must never be os.kill'ed locally")

        return KimiTerminalBridge(
            command=Path("/root/.kimi-code/bin/kimi"),
            cwd=Path("/root/Karami-Workspace"),
            runner=remote,
            process_killer=forbidden_kill,
            remote_host="tokyo",
            shutdown_wait_seconds=0.2,
            **kwargs,
        )

    def test_every_tmux_call_goes_through_ssh_sudo_with_exact_quoting(self):
        remote = FakeRemote()
        bridge = self.bridge(remote)
        text = "héllo 你好 -n '$(rm -rf /)'\nline2"
        bridge.run_tmux(["tmux", "set-buffer", "-b", "ccc-kimi-x", "--", text])
        self.assertEqual(["tmux", "set-buffer", "-b", "ccc-kimi-x", "--", text], remote.calls[-1])

    def test_identity_requires_exact_session_name(self):
        remote = FakeRemote()
        bridge = self.bridge(remote)

        def other_session(argv, **kwargs):
            remote.calls.append(remote.remote_argv(argv))
            # tmux 3.6 display-message falls back to another session for an
            # unknown -t; that must read as "absent", not as ours.
            return _done(stdout=f"other\t{KIMI_TERMINAL_OWNER_VALUE}\t{BINDING}\t%1\t0\n")

        bridge._run = other_session
        with bridge._lock:
            self.assertFalse(bridge._has_session_locked())
            self.assertFalse(bridge._owns_session_locked())
            with self.assertRaises(KimiTerminalUnavailable):
                bridge._pane_status_locked()

    def test_transport_failure_is_an_error_not_an_absent_session(self):
        remote = FakeRemote()
        remote.transport_down = True
        bridge = self.bridge(remote)
        with bridge._lock, self.assertRaises(KimiTerminalUnavailable):
            bridge._has_session_locked()

    def test_identity_is_fetched_once_and_dropped_on_mutation(self):
        remote = FakeRemote()
        remote.session = (KIMI_TERMINAL_OWNER_VALUE, BINDING, "%3", "0")
        bridge = self.bridge(remote)
        with bridge._lock:
            self.assertTrue(bridge._has_session_locked())
            self.assertTrue(bridge._owns_session_locked())
            self.assertEqual(BINDING, bridge._bound_session_locked())
            self.assertEqual(("%3", False), bridge._pane_status_locked())
        self.assertEqual(1, len(remote.calls))
        bridge.run_tmux(["tmux", "send-keys", "-t", "%3", "Enter"])
        with bridge._lock:
            bridge._has_session_locked()
        self.assertEqual("display-message", remote.calls[-1][1])
        self.assertEqual(3, len(remote.calls))

    def test_ensure_launches_kimi_on_the_remote_host_and_release_kills_remotely(self):
        remote = FakeRemote()
        bridge = self.bridge(remote, identity_cache_seconds=0)
        real_call = remote.__call__

        def runner(argv, **kwargs):
            result = real_call(argv, **kwargs)
            cmd = remote.calls[-1]
            if cmd[:2] == ["tmux", "set-option"]:
                owner, binding, pane, dead = remote.session or ("", "", "%7", "0")
                if cmd[4] == "@ccc_kimi_terminal_owner":
                    owner = cmd[5]
                else:
                    binding = cmd[5]
                remote.session = (owner, binding, pane, dead)
            return result

        bridge._run = runner
        pane = bridge.ensure("session_1")
        self.assertEqual("%7", pane)
        launch = next(c for c in remote.calls if c[:2] == ["tmux", "respawn-pane"])
        self.assertIn("/bin/sh", launch)
        self.assertEqual(
            ["/root/.kimi-code/bin/kimi", "/root/Karami-Workspace", "session_1"], launch[-3:],
        )
        self.assertTrue(any(c[0] == "test" for c in remote.calls))
        lease = bridge.lease_for_pane(pane)
        self.assertTrue(bridge.release(lease))
        self.assertEqual([["kill", "-15", "4242"]], remote.killed)
        self.assertIsNone(remote.session)


class RemoteKimiTerminalHandlerTest(unittest.TestCase):
    def test_kimi_pane_commands_never_touch_local_tmux(self):
        remote = FakeRemote()
        remote.session = (KIMI_TERMINAL_OWNER_VALUE, BINDING, "%3", "0")
        bridge = KimiTerminalBridge(runner=remote, remote_host="tokyo")
        handler = PushHandler.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_terminal=bridge)
        with mock.patch("push.subprocess.run", side_effect=AssertionError("local tmux")), \
                mock.patch("push.subprocess.Popen", side_effect=AssertionError("local tmux")):
            result = handler._kimi_pane_tmux(["tmux", "capture-pane", "-t", "%3", "-p"])
            self.assertEqual(0, result.returncode)
            sent = []
            handler._send_json = lambda status, payload: sent.append((status, payload))
            handler._tmux_send_remote_kimi(bridge, "%3", "hi", True)
        self.assertEqual(200, sent[-1][0])
        self.assertEqual(
            ["load-buffer", "paste-buffer", "send-keys"],
            [c[1] for c in remote.calls if c[1] in {"load-buffer", "paste-buffer", "send-keys"}],
        )

    def test_local_bridge_keeps_local_subprocess_path(self):
        handler = PushHandler.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_terminal=KimiTerminalBridge())
        with mock.patch("push.subprocess.run", return_value=_done()) as run:
            handler._kimi_pane_tmux(["tmux", "capture-pane", "-t", "%3", "-p"])
        run.assert_called_once_with(
            ["tmux", "capture-pane", "-t", "%3", "-p"],
            capture_output=True, text=True, timeout=3,
        )


if __name__ == "__main__":
    unittest.main()
