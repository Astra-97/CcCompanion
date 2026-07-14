# Xia-only Claude Development Channel — archived experiment

> **Archived on 2026-07-14. Do not enable this transport in production.**
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

The checked-in code is a research checkpoint, not a release. The last local
changes added a stable runtime and a process/port drain gate, including tests
for post-kill tmux socket transitions and control changes during shutdown, but
those final changes were intentionally archived before independent re-review
and the full real-service smoke matrix. No successful App end-to-end Channel
turn, Stop fallback, restart/resume, model/persona rotation and rollback cycle
was accepted as production evidence.

If work resumes later, begin from the repository checkpoint rather than the
VPS runtime directory. Re-run all Python and Node tests, request a fresh
independent review, and complete every shadow gate in
`../docs/xia-relay-control.md` while production remains on `relay`. Do not copy
Xiaoke/root configuration or reuse its tmux/session.

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
credentials. An isolated OAuth credential snapshot must still be provisioned
manually at
`claude-channel-home/.claude/.credentials.json`; never commit or automate that
copy. The script does not install/start a service or change the backend flag.
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
