"""Run a stdio agent (ACP and friends) on another host over ssh (2026-10-28).

Kiro moved to Tokyo; the next ACP agents (pi-acp, ...) will live there too.
The backend still talks to them exactly like a local child process: the
Popen it gets back is a local ``ssh -T`` whose stdin/stdout are the agent's,
so JSON-RPC over stdio works unchanged.  What ssh changes:

* The local cwd means nothing remotely.  ``RemoteStdioTarget.cwd`` is the
  remote working directory; ``popen()`` drops any local ``cwd=`` kwarg.
* ``os.killpg`` on the local Popen only kills ssh.  A clean ssh exit closes
  the agent's stdin and kiro-cli exits on EOF (measured: < 1 s), but a
  dropped connection can leave the remote agent running (and holding its
  session lock) until TCP gives up.  So every remote agent runs as its own
  process group whose leader writes ``<pid> <starttime>`` to a remote
  pidfile, and ``stop()`` — called on close and again by the launch script
  itself before it starts a new instance — TERMs, then KILLs exactly that
  group.  The starttime check keeps a recycled pid from ever being hit.
* Short one-shot commands (tmux, /proc reads, kill, file copies) go through
  ``run()`` on a shared ControlMaster connection; the long-lived agent
  connection never multiplexes (killing it must not take others down).

Two ways to become the remote user: log in as that user (``user=``, key in
its authorized_keys) or log in as an admin and ``sudo -n -u <sudo_user>``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
import subprocess
from typing import Any, Callable, Mapping, Sequence

_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

SSH_COMMON_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
)

# Process-group stop: $1 = pidfile.  Only the group whose leader still has
# the recorded starttime is signalled; the pidfile is removed afterwards.
_STOP_SCRIPT = r"""
f=$1
[ -r "$f" ] || exit 0
read -r pid start < "$f" || { rm -f "$f"; exit 0; }
case "$pid" in ''|*[!0-9]*) rm -f "$f"; exit 0;; esac
cur=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -d' ' -f20)
if [ -z "$cur" ] || [ "$cur" != "$start" ]; then rm -f "$f"; exit 0; fi
kill -TERM "-$pid" 2>/dev/null
i=0
while kill -0 "-$pid" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i+1)); done
if kill -0 "-$pid" 2>/dev/null; then
  kill -KILL "-$pid" 2>/dev/null
  i=0
  while kill -0 "-$pid" 2>/dev/null && [ "$i" -lt 20 ]; do sleep 0.1; i=$((i+1)); done
fi
rm -f "$f"
! kill -0 "-$pid" 2>/dev/null
"""

# Launch: $1 = pidfile, $2 = remote cwd, then `env K=V ... argv`.  A stale
# instance from a dropped connection is stopped first; then the agent runs
# in a fresh session (pid == pgid == sid) that records itself and execs.
_LAUNCH_SCRIPT = (
    "pidfile=$1; cwd=$2; shift 2; "
    "if [ -n \"$pidfile\" ]; then "
    "sh -c " + shlex.quote(_STOP_SCRIPT) + " ccc-stop \"$pidfile\" || exit 98; "
    "mkdir -p \"$(dirname \"$pidfile\")\"; fi; "
    "exec setsid -w sh -c '"
    "if [ -n \"$0\" ]; then "
    "st=$(sed \"s/.*) //\" /proc/$$/stat | cut -d\" \" -f20); "
    "umask 077; echo \"$$ $st\" > \"$0\"; fi; "
    "cd \"$1\" || exit 97; shift; exec \"$@\"' \"$pidfile\" \"$cwd\" \"$@\""
)


@dataclass(frozen=True)
class RemoteStdioTarget:
    """Where and as whom a remote stdio agent runs."""

    host: str
    user: str = ""            # ssh login user (``ssh -l``); "" = ssh config default
    sudo_user: str = ""       # optional: then ``sudo -n -u <sudo_user>``
    cwd: str = ""             # remote working directory of the agent
    env: Mapping[str, str] = field(default_factory=dict)
    pidfile: str = ""         # remote pidfile of the agent's process group
    control_path: str = ""    # ControlMaster socket for run(); "" = no multiplexing

    def __post_init__(self) -> None:
        if not _HOST_RE.fullmatch(self.host or ""):
            raise ValueError("invalid remote host")
        for name in (self.user, self.sudo_user):
            if name and not _USER_RE.fullmatch(name):
                raise ValueError("invalid remote user")
        for key in self.env:
            if not _ENV_KEY_RE.fullmatch(str(key)):
                raise ValueError("invalid remote env key")
        for path in (self.cwd, self.pidfile):
            if path and not path.startswith("/"):
                raise ValueError("remote paths must be absolute")


class RemoteStdio:
    """ssh transport for one remote agent: spawn, stop, and one-shot commands."""

    def __init__(
        self,
        target: RemoteStdioTarget,
        *,
        runner: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.target = target
        self._run = runner
        self._popen = popen

    @property
    def host(self) -> str:
        return self.target.host

    def _ssh_prefix(self, *, multiplex: bool) -> list[str]:
        argv = ["ssh", "-T", *SSH_COMMON_OPTIONS]
        if multiplex and self.target.control_path:
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.target.control_path}",
                "-o", "ControlPersist=10m",
            ]
        else:
            argv += ["-o", "ControlPath=none"]
        if self.target.user:
            argv += ["-l", self.target.user]
        argv.append(self.target.host)
        return argv

    def remote_argv(self, argv: Sequence[str], *, multiplex: bool = True) -> list[str]:
        """Local argv that runs ``argv`` remotely (each word shell-quoted)."""
        words = [str(part) for part in argv]
        if self.target.sudo_user:
            words = ["sudo", "-n", "-u", self.target.sudo_user, "--", *words]
        return [*self._ssh_prefix(multiplex=multiplex), shlex.join(words)]

    def _env_words(self) -> list[str]:
        if not self.target.env:
            return []
        return ["/usr/bin/env", *(f"{key}={value}" for key, value in self.target.env.items())]

    def launch_argv(self, argv: Sequence[str]) -> list[str]:
        """Local ssh argv that starts ``argv`` as the remote agent."""
        return self.remote_argv(
            ["sh", "-c", _LAUNCH_SCRIPT, "ccc-remote-stdio",
             self.target.pidfile, self.target.cwd or "/", *self._env_words(), *argv],
            multiplex=False,
        )

    def popen(self, argv: Sequence[str], **kwargs: Any) -> Any:
        """``subprocess.Popen``-compatible factory for the remote agent.

        ``cwd`` is a local notion and is dropped; ``start_new_session`` stays
        so the caller's ``os.killpg`` on the returned pid still only hits ssh.
        """
        kwargs.pop("cwd", None)
        return self._popen(self.launch_argv(argv), **kwargs)

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | bytes | None = None,
        timeout: float = 10.0,
        binary: bool = False,
        stdin: Any = None,
    ) -> Any:
        """One short remote command over the shared connection."""
        kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout}
        if not binary:
            kwargs.update(encoding="utf-8", errors="replace")
        if input is not None:
            kwargs["input"] = input
        elif stdin is not None:
            kwargs["stdin"] = stdin
        return self._run(self.remote_argv(argv), **kwargs)

    def run_script(self, script: str, *args: str, timeout: float = 10.0, input: str | None = None) -> Any:
        return self.run(["sh", "-c", script, "ccc-remote", *args], timeout=timeout, input=input)

    def put_file(self, local_path: str, remote_path: str, *, timeout: float = 120.0) -> bool:
        """Copy one local file to ``remote_path`` (0600, atomic rename)."""
        if not remote_path.startswith("/"):
            raise ValueError("remote paths must be absolute")
        script = (
            'umask 077; mkdir -p "$(dirname "$1")" && cat > "$1.tmp.$$" '
            '&& mv -f "$1.tmp.$$" "$1" || { rm -f "$1.tmp.$$"; exit 1; }'
        )
        try:
            with open(local_path, "rb") as handle:
                result = self.run(
                    ["sh", "-c", script, "ccc-put", remote_path],
                    stdin=handle, timeout=timeout, binary=True,
                )
        except Exception:
            return False
        return getattr(result, "returncode", 1) == 0

    def stop(self, *, timeout: float = 15.0) -> bool:
        """Stop the recorded remote agent group; True once it is gone."""
        if not self.target.pidfile:
            return True
        try:
            result = self.run_script(_STOP_SCRIPT, self.target.pidfile, timeout=timeout)
        except Exception:
            return False
        return getattr(result, "returncode", 1) == 0
