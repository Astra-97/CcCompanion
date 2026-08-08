import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from deploy import codex_remote_control_service as service


class CodexRemoteControlServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex-home"
        (self.home / "app-server-daemon").mkdir(parents=True)
        self.lock = self.root / "service.lock"
        self.log = self.root / "cli.log"
        self.fake_codex = self.home / "packages/standalone/current/bin/codex"
        self.fake_codex.parent.mkdir(parents=True)
        self.fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_codex.chmod(0o755)
        os.chown(self.fake_codex.parent, 1001, 1001)
        os.chown(self.fake_codex, 1001, 1001)
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_pid(self, pid):
        (self.home / "app-server-daemon/app-server.pid").write_text(
            json.dumps({"pid": pid}), encoding="utf-8"
        )

    def argv(self, action):
        return [
            action,
            "--codex-home", str(self.home),
            "--codex", str(self.fake_codex),
            "--lock", str(self.lock),
            "--proc-root", str(self.proc_root),
            "--command-timeout", "1",
            "--state-timeout", "1",
        ]

    @staticmethod
    def write_fake_proc(proc_root, pid, *, executable, remote, cgroup, starttime):
        process_dir = proc_root / str(pid)
        process_dir.mkdir(parents=True, exist_ok=True)
        tail = ["S", *(["0"] * 18), str(starttime)]
        (process_dir / "stat").write_text(
            f"{pid} (fake process) " + " ".join(tail) + "\n", encoding="utf-8"
        )
        argv = [b"fake"]
        if remote:
            argv += [b"app-server", b"--remote-control"]
        (process_dir / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
        (process_dir / "cgroup").write_text(f"0::{cgroup}\n", encoding="utf-8")
        (process_dir / "exe").symlink_to(executable)

    def install_proc(self, pid, *, executable=None, remote=True, cgroup="/service", starttime=10):
        self.write_fake_proc(
            self.proc_root,
            pid,
            executable=executable or self.fake_codex,
            remote=remote,
            cgroup=cgroup,
            starttime=starttime,
        )

    def test_stop_managed_remote_after_second_full_identity_check(self):
        pid = 4101
        self.install_proc(pid)
        self.write_pid(pid)
        calls = []
        def cli(_codex, _home, action, _timeout):
            calls.append(action)
            service.daemon_pid_path(self.home).unlink(missing_ok=True)
            return True
        with mock.patch.object(service, "run_cli", side_effect=cli):
            self.assertEqual(service.main(self.argv("stop")), 0)
        self.assertEqual(calls, ["stop"])

    def test_stop_never_invokes_cli_for_matching_argv_with_wrong_executable(self):
        wrong = self.root / "wrong-binary"
        wrong.write_bytes(b"wrong")
        pid = 4102
        self.install_proc(pid, executable=wrong)
        self.write_pid(pid)
        with mock.patch.object(service, "run_cli") as cli:
            self.assertEqual(service.main(self.argv("stop")), 0)
        cli.assert_not_called()

    def test_stop_never_invokes_cli_for_matching_executable_with_wrong_owner(self):
        pid = 4106
        self.install_proc(pid)
        os.chown(self.proc_root / str(pid), 65534, -1)
        self.write_pid(pid)
        with mock.patch.object(service, "run_cli") as cli:
            self.assertEqual(service.main(self.argv("stop")), 0)
        cli.assert_not_called()

    def test_installer_owned_binary_accepts_root_owned_daemon_process(self):
        pid = 4108
        self.install_proc(pid)
        self.write_pid(pid)
        managed = service.trusted_executable_identity(self.fake_codex, self.home)
        record = service.read_pid_record(self.home, self.proc_root)
        self.assertEqual(managed.uid, 1001)
        self.assertEqual(record.process_uid, 0)
        self.assertTrue(record.is_live_remote(managed, 0))

    def test_group_writable_release_chain_is_rejected_before_cli(self):
        self.fake_codex.parent.chmod(0o775)
        pid = 4109
        self.install_proc(pid)
        self.write_pid(pid)
        with mock.patch.object(service, "run_cli") as cli:
            self.assertEqual(service.main(self.argv("stop")), 1)
        cli.assert_not_called()

    def test_start_retires_reused_pid_record_without_signalling_process(self):
        pid = 4103
        self.install_proc(pid, remote=False)
        self.write_pid(pid)
        record = service.read_pid_record(self.home, self.proc_root)
        self.assertIsNotNone(record)
        managed = service.trusted_executable_identity(self.fake_codex, self.home)
        self.assertIsNotNone(managed)
        self.assertFalse(record.is_live_remote(managed, 0))
        self.assertTrue(
            service.retire_stale_pid_record(record, managed, 0, self.home, self.proc_root)
        )
        self.assertFalse(service.daemon_pid_path(self.home).exists())

    def test_identity_change_fails_closed_before_cli_stop(self):
        pid = 4104
        self.install_proc(pid, starttime=11)
        self.write_pid(pid)
        expected = service.read_pid_record(self.home, self.proc_root)
        self.assertIsNotNone(expected)
        (self.proc_root / str(pid) / "stat").write_text(
            f"{pid} (changed) " + " ".join(["S", *(["0"] * 18), "12"]) + "\n",
            encoding="utf-8",
        )
        self.assertFalse(service.record_is_unchanged(expected, self.home, self.proc_root))

    def test_identity_change_in_main_never_reaches_cli_stop(self):
        pid = 4107
        self.install_proc(pid)
        self.write_pid(pid)
        with mock.patch.object(service, "record_is_unchanged", return_value=False), mock.patch.object(
            service, "run_cli"
        ) as cli:
            self.assertEqual(service.main(self.argv("stop")), 1)
        cli.assert_not_called()

    def test_start_takes_over_managed_remote_from_other_cgroup(self):
        old_pid = 4105
        self.write_pid(old_pid)
        managed_pid = 7777
        service_group = "/system.slice/codex-remote-control.service"
        self.install_proc(old_pid, cgroup="/system.slice/cc-companion.service", starttime=11)
        self.install_proc(os.getpid(), remote=False, cgroup=service_group, starttime=22)
        self.install_proc(managed_pid, cgroup=service_group, starttime=33)
        calls = []
        def cli(_codex, _home, action, _timeout):
            calls.append(action)
            if action == "stop":
                service.daemon_pid_path(self.home).unlink(missing_ok=True)
            else:
                self.write_pid(managed_pid)
            return True
        with mock.patch.object(service, "run_cli", side_effect=cli):
            self.assertEqual(service.main(self.argv("start")), 0)
        self.assertEqual(calls, ["stop", "start"])
        self.assertEqual(json.loads(service.daemon_pid_path(self.home).read_text())["pid"], managed_pid)

    def test_symlink_service_lock_fails_closed(self):
        target = self.root / "target"
        target.write_text("", encoding="utf-8")
        self.lock.symlink_to(target)
        with mock.patch.object(service, "run_cli") as cli:
            self.assertEqual(service.main(self.argv("status")), 1)
        cli.assert_not_called()

    def test_permissive_service_lock_fails_closed(self):
        self.lock.write_text("", encoding="utf-8")
        self.lock.chmod(0o644)
        self.assertEqual(service.main(self.argv("status")), 1)

    def test_tracked_unit_uses_fail_closed_wrapper_and_is_enableable(self):
        unit = (Path(__file__).parent / "deploy" / "codex-remote-control.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ExecStart=/usr/local/libexec/codex-remote-control-service start --service-uid 0", unit
        )
        self.assertIn(
            "ExecStop=/usr/local/libexec/codex-remote-control-service stop --service-uid 0", unit
        )
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn("WantedBy=multi-user.target", unit)


if __name__ == "__main__":
    unittest.main()
