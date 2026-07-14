# Restricted Xia Yizhou relay overlay

This directory pins [ai-session-relay](https://github.com/blanchexxxxx/ai-session-relay)
at commit `783e01455e2719962d09839d08a9254441cae478` and carries the
deployment-only `chat_only` enforcement needed by CcCompanion. The upstream
checkout in `/tmp` is research material only; production must be prepared from
this tracked patch and policy module.

Nothing in this directory has been installed, enabled, or started merely by
being present in the repository.

## Enforced boundary

- Startup fails unless `AI_RELAY_EXECUTION_MODE=chat_only` exactly.
- The instance root, workspace, relay state, Claude config, Codex home, and
  runtime home are explicit absolute paths. Workspace and state must be
  disjoint children of the instance root. It validates the read-only instance/
  persona boundary as service-owned `0700`/`0600` and only tightens the writable
  private state subtree to `0700`.
- Relay HTTP binds only to numeric loopback (`127.0.0.1` or `::1`).
- Engine subprocesses receive an allowlisted environment. They do not inherit
  arbitrary service secrets.
- Claude uses `--safe-mode`, an empty built-in tool set, strict empty MCP
  config, `dontAsk`, disabled slash commands, and an explicit
  `--system-prompt-file` pointing to the isolated `CLAUDE.md`. It never uses
  `--dangerously-skip-permissions`. A streamed `tool_use` kills the entire
  Claude process group and fails the turn.
- Codex uses its own `CODEX_HOME`, `sandbox_mode="read-only"`,
  `approval_policy="never"`, disabled web search/apps/browser/computer/image/
  multi-agent surfaces, and no MCP servers. New and resumed threads repeat
  `sandbox=read-only` and `approvalPolicy=never`. Every server approval request
  is cancelled. Any non-message/reasoning/plan item kills the app-server
  process group and fails the turn.
- State JSON, the request ledger, and policy/persona files are `0600`; the systemd template also uses
  `UMask=0077` and OS service hardening.
- Claude transcripts resolve under the isolated `CLAUDE_CONFIG_DIR`; Codex
  pointers resolve under the isolated relay state/Codex home. No Kairos or
  Xiaoke pointer is imported or consulted.

The official Codex security model treats `read-only` sandboxing and approval
policy as separate controls. With `never`, an action that cannot run inside the
read-only boundary fails instead of being elevated. The overlay additionally
cancels every approval request and terminates on tool events.

### Residual limit

Claude exposes a documented empty tool set, so its tool capability is disabled
at CLI construction. Codex app-server does not expose an equivalent documented
"no built-in tools at all" switch. This overlay therefore combines isolated
config, read-only/no-network sandbox behavior, no approvals, disabled optional
tools, and kill-on-tool-event. It prevents writes/elevation and fails observed
tool attempts, but must not be marketed as a formal absolute text-only Codex
security boundary across future CLI versions. Re-run the contract tests after
every Codex update; if the flags or event schema drift, leave the service down.

## Persona refresh contract

After a persona transaction has atomically updated both `CLAUDE.md` and
`AGENTS.md`, the authenticated CcCompanion backend calls the loopback relay:

```http
POST /refresh
Content-Type: application/json

{"reason":"persona_updated"}
```

The only accepted body is that exact object. A successful response includes
`{"ok":true,"epoch":<integer>,"pending":true}`. The relay increments the
current provider epoch and preserves a pending handoff. On the next turn:

- Claude is invoked with `resume_sid=""`, creating a fresh isolated session.
- Codex sees the newer epoch and creates a fresh isolated thread.
- The backend-supplied authoritative Xia conversation handoff is consumed by
  that fresh session before the current user message.

If `/refresh` fails, the backend must roll back the persona transaction. The
endpoint neither accepts nor reads a session id and cannot target the main
Kairos/Xiaoke pointers.

## Model and content contract

`GET /models?provider=codex` uses the same isolated restricted app-server, so
model discovery does not fall back to the operator's main Codex config.
Claude returns documented CLI aliases plus `default`; validated custom model
strings remain a backend concern.

The relay request schema carries text only. Persona `.md`/`.txt` uploads are
safe only after the authenticated backend decodes, size-checks, composes, and
writes them as private text. Ordinary chat attachments are **not supported** by
this relay contract; do not claim image/file attachment support unless a later
protocol adds a separately validated content input.

Every `/chat_stream` request must also carry the backend's stable
`request_id`, matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,199}`. IDs are globally
unique and must never be reused for different messages. Before an engine is
called, the relay durably records `(provider, epoch, request_id)` as accepted,
then running. An authoritative `done` event is durably cached before it is sent
to the client; repeating a completed ID replays that cached event without
calling either engine. Repeating an accepted/running/uncertain ID returns HTTP
409 with `code=request_uncertain` and `retryable=false`.

If the relay process dies with accepted/running work, before the first
subsequent control/chat admission the relay treats the result as
terminal-uncertain: it first advances the affected active provider epoch,
arms a fresh authoritative handoff, and clears only that provider's isolated
resume pointers, then marks the ledger record uncertain. It never automatically
resubmits a zero-output or unknown turn. The ledger is bounded (512 entries and
64 MiB by default); oldest terminal records are pruned, so the backend must use
never-reused IDs rather than relying on an expired ID forever.

## Prepare without deploying

The script requires the dedicated `cc-xia-relay` user to exist. It creates the
backend persona source and isolated `/var/lib` layout with matching ownership,
clones the pinned commit, verifies it, checks/applies the tracked patch, copies
the policy module, and creates the Python environment. `/opt` runtime
directories remain root-owned `0755`; source modules are root-owned `0644` and
executables `0755`, so the service identity can traverse/import/execute but
cannot alter code. Secrets and mutable engine state remain under the dedicated
`0700` `/var/lib` tree with `0600` files. The script does not install or start
systemd.

Claude must resolve from a root-owned globally traversable candidate in
`/usr/local/bin`, `/usr/bin`, or `/bin`; a symlink is accepted only when its
final executable is outside `/root`, root-owned, `0755`, and executable by the
service identity. The current Claude installation satisfies this through
`/usr/bin/claude` to its global `/usr/lib` runtime.

Codex is different on this host: its only executable is a standalone static-PIE
ELF below `/root`, which `ProtectHome=yes` correctly hides. The prepare command
therefore requires that exact regular source file explicitly. The current
standalone release file is `1001:1001` mode `0755`; the script accepts that
non-root owner only for the canonical `/root/.codex/packages/standalone/releases/*/bin/codex`
shape behind root-owned mode-`0700` `/root`, then validates executable/ELF/
static-PIE metadata and copies its bytes (never a symlink) to
root-owned `0755` `/opt/cc-xia-ai-session-relay/bin/codex`. The service PATH
and `AI_RELAY_CODEX_BIN` point only to that isolated copy. No file from the
source Codex home accompanies the executable except the one explicitly supplied
auth file described below.

Run only after review. Create the dedicated identity first, then prepare into a
clean destination. Do not copy the existing Kairos/Xiaoke config directories:

```bash
sudo useradd --system --home /var/lib/cc-xia-relay --shell /usr/sbin/nologin cc-xia-relay
sudo ./prepare-runtime.sh \
  /opt/cc-xia-ai-session-relay \
  /absolute/path/to/standalone-codex-ELF \
  /absolute/path/to/claude-.credentials.json \
  /absolute/path/to/codex-auth.json
```

The two credential source arguments must use basenames `.credentials.json` and
`auth.json` and be non-empty JSON objects in regular, non-symlink, root-owned
`0600` files. The script copies only Claude `.credentials.json` and
Codex `auth.json`, installing them as `cc-xia-relay:cc-xia-relay` `0600` under
the isolated config homes. It never copies `.claude.json`, `config.toml`,
history, sessions, hooks, skills, plugins, MCP config, or any pointer database;
it never prints credential contents. Sharing a credential snapshot means the
isolated agents use the same account entitlement, not the same session. When
the source account credentials rotate, use the separately reviewed atomic
credential-refresh procedure; `prepare-runtime.sh` intentionally cannot be
rerun over a populated runtime. Do not bind either main credential path into
the service. Two copied OAuth snapshots may refresh independently and one may
eventually become stale or invalidate the other; an isolated service login is
the safer long-term replacement when the providers support it.

For an approved snapshot refresh, stop the isolated unit, run
`./refresh-credentials.sh /reviewed/.credentials.json /reviewed/auth.json`,
rerun the silent Claude/Codex auth preflights, and only then start the unit.
The helper refuses to run while the unit is active, validates the same private
source contract, atomically replaces each `0600` service-owned file, fsyncs its
directory, prints no credential data, and leaves the unit stopped.

The backend remains the atomic writer of its tracked persona directory at
`/root/CcCompanion/apns-server/state/ai_relay_workspace`. The service template
does **not** grant the relay traversal of `/root`. Instead, systemd creates a
read-only bind inside the relay's private mount namespace:

```ini
BindReadOnlyPaths=/root/CcCompanion/apns-server/state/ai_relay_workspace:/var/lib/cc-xia-relay/workspace
ProtectHome=yes
```

The source directory must be `0700`, owned by `cc-xia-relay`, and each
atomically replaced `CLAUDE.md`/`AGENTS.md` must be `0600` and owned by that
same identity. The root backend may stage and `fsync` the files, but it must
`fchown`/`chmod` the staged inode **before** `os.replace`; otherwise the
dedicated relay user cannot read the new persona through the bind. Do not make
the directory group/world-readable as a workaround.

The mount is read-only only inside the relay service namespace. The backend's
original source path remains writable to the backend, so later persona atomic
replacements appear at the destination without granting the relay write access
or access to sibling `/root` data. Verify the bind after installation with a
disposable `stat`/write-denial service before starting the relay.

Preparation validates the isolated copies without displaying their status:
Claude `--version` and `auth status`, Codex `--version`, and an app-server
`initialize` + `model/list` smoke all run as `cc-xia-relay` with isolated HOME
and config directories. The Codex smoke starts no thread or turn.

Only after tests and review, install the template as
`/etc/systemd/system/cc-xia-ai-session-relay.service`, run
`systemd-analyze verify`, then explicitly enable it. Deployment/restart is a
separate authorized operation.

## Verification

Policy tests do not start Claude, Codex, or any real session:

```bash
python3 -m unittest -v test_security_policy.py
bash -n prepare-runtime.sh
```

Patch applicability check against the pinned checkout:

```bash
git -C /path/to/pristine-pinned-checkout apply --check \
  "$PWD/upstream-chat-only.patch"
```

Before enabling production, additionally verify the installed CLI help still
contains Claude's `--safe-mode`, `--tools`, `--strict-mcp-config`,
`--system-prompt-file`, and Codex's `--strict-config`/app-server interfaces.
Then run a disposable isolated smoke conversation asking both engines to use a
tool: the turn must fail without a write, MCP call, web result, or approval.

The patch is a modification of GPL-3.0-only upstream source and is intended to
be distributed and operated under that upstream license.
