# Xia-only Claude Development Channel — archived experiment

> **Archived checkpoint updated on 2026-07-15. Do not enable this transport in
> production.**
>
> Astra chose to stop this experiment and keep the existing Claude `-p`
> transport. Production must remain `claude_transport=relay`; do not start
> `cc-xia-claude-channel.service` or switch the flag to `channel` without a new
> implementation review and a complete disposable shadow test.

The production/rollback implementation is already preserved in this
repository. It uses official Claude CLI print mode with `-p`, `stream-json`,
partial-message events and native session `--resume`; Codex continues to use
its persistent app-server path. YAML persona-file support is also part of that
relay implementation. See `../docs/xia-relay-control.md` and keep the default
`claude_transport=relay`.

## Why this branch was archived

The Development Channel path requires owning a long-lived interactive TUI,
tmux/MCP lifecycles, exact epoch/lease fencing, crash recovery, Stop fallback,
model/persona generation rotation, Android keepalives, workspace trust and
strict isolation from Xiaoke. Real shadow testing found several differences
that unit mocks did not expose, including string-only channel metadata, MCP
stdio disconnects, first-run confirmation state, and residual pane processes
after a tmux session disappears.

The checked-in code is a research checkpoint, not a release. Commit `d28cddb`
completed an independently reviewed stable-runtime and process/port drain gate.
The review reproduced and fixed mixed tmux query errors that could otherwise
hide a hard failure, and then passed 63 Python tests, 9 Node tests, and repeated
real-tmux normal-exit, stubborn-process timeout, and generation-change-during-
drain scenarios. The full real-service smoke matrix is still incomplete. No
successful App end-to-end Channel turn, Stop fallback, restart/resume,
model/persona rotation and rollback cycle was accepted as production evidence.

If work resumes later, begin from the repository checkpoint rather than the
VPS runtime directory. Re-run all Python and Node tests, request a fresh
independent review, and complete every shadow gate in
`../docs/xia-relay-control.md` while production remains on `relay`. Do not copy
Xiaoke/root configuration or reuse its tmux/session.

## 2026-07-15 shadow checkpoint

The reviewed checkpoint was installed once on the disposable Xia-only shadow
while production remained on `claude_transport=relay`. Dependency installation
reported zero vulnerabilities. Re-running `prepare-runtime.sh` left the private
token and onboarding file unchanged. The old Xia-only stale tmux socket was
removed only after verifying its owner/mode, that no dedicated process existed,
and that port 8821 was closed.

The fixed `channel-state/runtime` workspace trust page was reviewed and
accepted. Only `mcp__xia-companion__reply` was pre-approved, only the explicit
`xia-companion` MCP was selected, and the local Development Channel warning
named only `server:xia-companion`. The service then passed unauthenticated 401
and authenticated `ready=true`, `mcp_connected=true`, exact generation/model
health checks. A disposable message reached the TUI through the Channel, but
the TUI reported `Not logged in` before model generation and never called the
reply tool. The admitted request was durably marked terminal-uncertain and the
shadow service was stopped. Port 8821 is closed and production remains on the
existing `-p` relay.

The authentication failure exposed an important migration rule. The current
`-p` credential snapshot contained an expired access token. The
`claude auth status` command still described the account as logged in, while
the first real model request attempted refresh and cleared the copied shadow
credential. A raw copy
of one OAuth refresh credential into two independently writable Claude config
directories is not an accepted deployment design: refresh state can diverge or
invalidate one side.

The intended final state still has only one active Xia Claude transport and one
credential owner: Channel after cutover. During parallel shadow validation,
use a separately authorized Channel login/session rather than copying the live
relay refresh credential. At the idle cutover boundary, make Channel the sole
credential owner and stop using `-p`; the relay implementation remains in Git
as rollback code, but rollback must include an explicit credential handback or
fresh login. Never remove or invalidate the production relay credential before
Channel has completed the full shadow matrix and the backend transport switch
has been separately approved.

Resume in this order:

1. Keep `cc-xia-claude-channel.service` stopped and
   `claude_transport=relay` selected.
2. Re-run the committed Python/Node suites and independent review if code or
   the installed Claude CLI changed.
3. Provision a fresh, independently authorized Channel credential without
   printing login URLs, codes, tokens, account data, or credential contents in
   logs or commits.
4. Re-run 401/authenticated health, exact reply, Stop fallback/dedupe,
   disconnect-to-uncertain recovery, resume/fresh, stable-runtime rotation with
   unchanged `NRestarts`, App contract, and rollback tests.
5. Only after every gate passes, perform an idle, fenced transport cutover and
   retire `-p` credential use. Otherwise stop the shadow and retain `relay`.

## Experimental design checkpoint

This is a feature-flagged transport prototype for the
`ai-custom` contact. It is deliberately separate from Xiaoke's channel/TUI.

Safety invariants:

- loopback HTTP plus a required private token;
- fixed `ai-custom` / `claude` route;
- durable atomic request/result ledger;
- exact request, client, epoch, lease, and generation grants;
- one final reply only; no raw thinking and no direct CcCompanion history write;
- dedicated HOME, state, credentials, tmux socket, MCP config, and Stop hook;
- built-in tools disabled; only the exact reply MCP is allowed;
- model/persona changes require a fresh generation; ordinary restarts resume;
- interrupted accepted/running work becomes terminal-uncertain.
- rotations drain until a new process completes an exact startup handshake;
- every generation reuses stable `channel-state/runtime`, so workspace trust is
  a one-time preflight; old `runtime-N` directories are cleaned only after the
  dedicated tmux process is confirmed stopped;
- corrupt durable state aborts startup instead of resetting dedupe fences;
- fixed private tmux state and a genuinely read-only bound persona workspace.

`prepare-runtime.sh` creates only private directories, a token, and the minimal
non-secret `hasCompletedOnboarding` Claude HOME marker needed by an isolated
TUI at `.claude/.claude.json` beneath the dedicated `CLAUDE_CONFIG_DIR`. It
never overwrites an existing marker and never copies root/Xiaoke settings or
credentials. A fresh independently authorized Channel credential must still be
provisioned manually at
`claude-channel-home/.claude/.credentials.json`; never commit it or copy an
active relay/root/Xiaoke refresh credential into that location. The script does
not install/start a service or change the backend flag.
Run it only while the optional channel service is stopped; unsafe links,
ownership, or permissions fail closed.
Deployment must follow `docs/xia-relay-control.md` and an independent review.
The default backend transport remains `relay`.

Tests:

```bash
npm ci --ignore-scripts
npm test
cd ..
python3 -m unittest -v _xia_claude_channel_test.py _ai_session_relay_test.py
```
