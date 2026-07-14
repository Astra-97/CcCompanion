# Xia Yizhou isolated relay control

`ai-custom` now has one isolated Claude Code session and one isolated Codex
thread behind `ai-session-relay`. Kairos and Xiaoke routes, pointers, workspaces,
and sessions are not read or changed by this integration.

## Optional long-lived Claude channel transport

Claude may use either transport while Codex continues to use the existing
long-lived app-server relay:

- `claude_transport=relay` (default and rollback): official `-p + stream-json`
  driver already shipped with the isolated relay.
- `claude_transport=channel`: a dedicated long-lived interactive Claude TUI in
  tmux socket `cc-xia-claude`, connected only to the Xia Development Channel on
  loopback port 8821. The App intentionally receives one final `done` and no
  Claude text deltas. This does not change the Android API contract.

The channel is not the Xiaoke POC on port 8810. It fixes contact/provider to
`ai-custom/claude`, has no CcCompanion global write token, and cannot call
`/chat/append`. `AIChatManager` remains the only writer of
`ai_chat_history.jsonl`: it durably records the user, grants one request with a
stable backend client id plus provider epoch and random lease, waits for the
channel's durable result, then appends exactly one assistant row.

Each grant is matched on request id, client id, epoch, lease, provider, contact,
and Claude generation. The model cannot choose a contact. Wrong/stale/revoked
grants fail closed. A reply-tool/Stop-hook race completes the ledger only once;
the Stop hook accepts only exact metadata from its own current channel turn and
extracts assistant text blocks, never raw thinking. A completed result survives
a backend/channel restart. An accepted/running request interrupted by a channel
crash becomes terminal-uncertain and is never automatically replayed.

During rotation the old process first persists `draining` and
`requires_fresh`, immediately reports `ready=false`, and refuses messages.
Only a newly launched process whose generation, model, session id, and
one-time bootstrap token exactly match durable control may become ready.
Existing malformed ledger/control files abort startup; only missing files may
be initialized. Replies and total ledger bytes are bounded and old terminal
entries are compacted.

Normal service restarts resume the same explicit Claude session. Model or
persona changes increment a durable generation and require a fresh native
session before another Claude turn; the old generation cannot answer. Provider
round trips retain the session but add one bounded authoritative handoff on the
first turn back, covering conversation that occurred in Codex. Persona remains
read-only in the isolated workspace and is compiled into both `CLAUDE.md` and
`AGENTS.md`.

Tracked deployment templates live in `xia_claude_channel/` and
`deploy/cc-xia-claude-channel.service`. The launcher uses the existing dedicated
`cc-xia-relay` account but separate `channel-state`, `claude-channel-home`, tmux
socket, MCP config, token, and credential snapshot. It loads no Telegram/root
configuration, disables built-in tools and slash commands, strictly loads only
the dedicated reply MCP, and has a Xia-only Stop hook. The systemd unit uses
private paths and memory/OOM limits. The operator view helper is capture-only.

Development Channels are experimental. The launcher never sends blind tmux
keystrokes to accept a changed confirmation page: failure to reach authenticated
MCP readiness within the bounded startup window exits fail-closed. A one-time
operator preflight must confirm that the installed Claude version keeps the
dedicated MCP reply tool available when built-ins are disabled. Credential
snapshots are supplied explicitly; `prepare-runtime.sh` never copies
`/root/.claude`. It writes only a minimal non-secret
`claude-channel-home/.claude/.claude.json` onboarding marker (the location
selected by the launcher's `CLAUDE_CONFIG_DIR`) and never overwrites an
existing one. The dedicated OAuth credential snapshot remains a separate,
manual `0600` provision at `claude-channel-home/.claude/.credentials.json`.
The root preparation pass validates every managed directory/file with no-follow
opens, exact owner/mode checks, and no-replace publication. Stop the optional
channel service before rerunning it; any symlink, unexpected owner, or relaxed
permission is an operator-visible failure, not something the script repairs.

The unit reuses the relay Linux UID only for operational simplicity. Its mount
namespace makes `/var/lib/cc-xia-relay/state` (the old relay credentials and
pointers) inaccessible. The only persona view is a real read-only bind mount
from `state/ai_relay_workspace` to the channel workspace. Channel HOME/state
remain separate, and `TMUX_TMPDIR` is fixed inside channel state so
`PrivateTmp` cannot hide the socket from the capture-only view helper.

## State boundaries

- Authoritative chat archive: `state/ai_chat_history.jsonl` (unchanged).
- New relay config: `state/ai_relay_config.json` (private, mode `0600`).
- Relay pointer/state: `/var/lib/cc-xia-relay/state/`, owned and used only by
  the dedicated `cc-xia-relay` service account. The backend proxy never reads
  or writes that directory.
- Persona source workspace: `state/ai_relay_workspace/`. The backend resolves
  only the fixed `cc-xia-relay` account name (never a caller-supplied UID/path),
  writes directories `0700` and files `0600`, and sets that owner before each
  temp-file rename. Development hosts without the account retain current-process
  ownership. Production exposes this directory to the relay through a read-only
  bind mount at `/var/lib/cc-xia-relay/workspace`.
- Ordered persona composition: `state/ai_persona/current/` (private files and
  manifest). The compiled result is atomically synchronized to both
  `CLAUDE.md` and `AGENTS.md`; typed custom text is always appended last.
- Retired `state/ai_chat_config.json` is deliberately neither read, migrated,
  overwritten, nor deleted. It may contain private legacy credentials.

The persona API accepts ordered `.md`, `.txt`, `.yaml`, and `.yml` UTF-8 plain
text files and has no file-count limit. YAML is preserved as text and is never
parsed or executed. Each file is limited to 256 KiB, custom text to 512 KiB,
and the combined UTF-8 text to 2 MiB. These byte limits bound resource use while
allowing any number of small files.

Android accepts the registered/common YAML MIME variants `application/yaml`,
`text/yaml`, and `application/x-yaml`. Providers that report a missing or
generic MIME fall back to the allowlisted filename suffix, strict UTF-8 decode,
and binary-control rejection; an arbitrary binary MIME or suffix is not
accepted.

## Execution safety prerequisite

Do not point production at the upstream repository unchanged. Its current
Claude driver uses `--dangerously-skip-permissions`, and its Codex driver uses
approval `never` plus auto-approval. The backend defaults Xia to
`relay_execution_mode = "chat_only"`, but the operator must run a restricted
relay build that actually enforces that mode:

- Claude: no tools/MCP and no permission bypass in `chat_only`.
- Codex: read-only sandbox, no MCP/tool approvals in `chat_only`.
- Both engines: the dedicated Xia workspace and dedicated relay state dir.
- Loopback bind only. The authenticated CcCompanion backend is the sole remote
  boundary.

Full autonomous mode is separate and remains unavailable unless
`relay_autonomous_tools_opt_in = true` is explicitly written to the new relay
config. Never copy tokens into the workspace or persona files.

Suggested environment for the restricted relay process:

```text
AI_RELAY_HOST=127.0.0.1
AI_RELAY_PORT=8900
AI_RELAY_WORKSPACE=/var/lib/cc-xia-relay/workspace
AI_RELAY_STATE_DIR=/var/lib/cc-xia-relay/state
AI_RELAY_PROVIDER=claude
AI_RELAY_EXECUTION_MODE=chat_only
```

The last variable is a required downstream restriction contract; current
upstream does not enforce it and must be patched before service installation.

## Authenticated control API

- `GET/POST /ai-chat/provider` — backend-authoritative provider. POST accepts
  only `claude` or `codex` and returns `409` during an active turn.
- `GET/POST /ai-chat/relay-model` — model list/selection scoped per provider.
  Codex is queried only through the dedicated restricted relay's isolated
  `codex app-server` `model/list`; the root backend never spawns Codex or reads
  the main `CODEX_HOME`. Claude shows
  CLI default plus the stable `fable`, `opus`, and `sonnet` aliases, the
  persisted value, and an explicitly entered validated custom alias. No
  version is guessed.
- `GET/POST /ai-chat/persona` — ordered composition status and one atomic apply.
  Existing file IDs retain server-private content; new files include UTF-8 text
  only. Omitting an existing ID deletes it.

Persona apply synchronizes the staged workspace, then calls the restricted
relay's loopback `POST /refresh` endpoint before changing the active manifest.
That refresh discards only Xia's same-provider Claude/Codex session pointer,
increments its isolated epoch, and makes the next turn consume the
authoritative handoff. A refresh failure rolls the workspace and staged
persona back; once refresh succeeds, local backup/journal cleanup cannot turn
the apply into a reported failure or restore the old persona.

Provider, model, and persona mutations all share turn admission locks. A switch
sets relay handoff pending; the next turn includes a bounded handoff built from
the authoritative CcCompanion history. Every relay request includes that
handoff defensively, and only the final upstream `done.full` is persisted as
the authoritative assistant reply.

Client message IDs use a private durable backend ledger and are also sent as a
validated stable `request_id` in `/chat_stream`. A zero-visible-output failure
is *UX-retryable best effort*: the same ID does not duplicate the local user
history row, but this alone does not prove that the isolated engine never
accepted the earlier turn. Once any delta/activity is durably marked, or once
the proxy returns authoritative `done`, a failed history commit is terminal and
the backend does not replay that ID.

Exactly-once is not claimed until the restricted relay persists and deduplicates
`request_id`. Without that relay-side ledger, replay remains possible from
relay acceptance until the first visible event is durably marked (including a
disk failure while marking it), and again if the engine emits `done` but the
backend dies before `stream_turn` returns. `chat_only` prevents tool side
effects, but it cannot by itself prevent a duplicate model/session turn in
those crash windows.

If the restricted relay has durably admitted an ID but restarts without a
cached final response, it must fail closed with HTTP `409` and JSON
`{"code":"request_uncertain"}`. The proxy maps this to a distinct terminal
condition; the backend persists the ID as non-retryable and tells the user to
send the content as a new message. Generic pre-admission transport errors remain
best-effort retryable and must not use this code.

The streamed equivalent is an HTTP `200` terminal `done` carrying
`request_status:"uncertain"` and `retryable:false`; it has the same non-replay
semantics. A cached `request_status:"completed"` entry with an error or empty
final is also terminal (`request_terminal`), not a reason to execute the model
again with the same ID.

## Deployment order

1. Build/install the restricted relay variant and verify both session/state
   paths are the Xia-only paths above.
2. Start it on loopback and verify `/health`, `/provider`, Claude text-only, and
   Codex read-only behavior without using the CcCompanion service.
3. Privately create `ai_relay_config.json` if non-default relay URL/mode/models
   are needed. Do not reuse or print the legacy API key.
4. Run backend tests, then restart CcCompanion with the normal delayed-restart
   procedure.
5. Build Android only in GitHub Actions. From Xia chat, open `⋮` →
   `夏以昼控制`, verify provider/model/persona controls, then make one turn on
each provider and confirm Kairos/Xiaoke session pointers are unchanged.

For the optional channel, install dependencies from the committed lockfile,
prepare private state, provision only the isolated Claude credential snapshot,
and start the channel service while `claude_transport` is still `relay`. Verify
unauthenticated requests are 401, exact-grant reply and Stop fallback dedupe,
fresh/resume behavior, and the capture-only TUI view. Then, while no Xia turn is
active, atomically select `channel` and restart only the CcCompanion backend by
the normal delayed procedure. Rollback is the reverse idle transition to
`relay`, followed by epoch revoke; retain both channel state and authoritative
history for audit/recovery.

Reproducible channel installation sequence (only after review):

```bash
install -d -o root -g root -m 0755 /opt/cc-xia-claude-channel
cp -a apns-server/xia_claude_channel/. /opt/cc-xia-claude-channel/
cd /opt/cc-xia-claude-channel
npm ci --omit=dev --ignore-scripts
chown -R root:root /opt/cc-xia-claude-channel
chmod 0755 launcher.sh prepare-runtime.sh prepare_runtime.py stop_hook.py runtime_state.py view.sh server.mjs
./prepare-runtime.sh
# prepare-runtime.sh installs only non-secret onboarding state. Separately
# provision the dedicated Claude credential snapshot as 0600 owned by
# cc-xia-relay. Never copy root/Xiaoke settings, hooks, or full config.
install -o root -g root -m 0644 \
  /root/CcCompanion/apns-server/deploy/cc-xia-claude-channel.service \
  /etc/systemd/system/cc-xia-claude-channel.service
systemctl daemon-reload
systemctl start cc-xia-claude-channel.service
```

Keep `claude_transport=relay` during smoke. Verify unauthenticated health is
401; authenticated health reports the exact generation/session and ready;
`metadata_json` appears as the escaped opening-tag attribute consumed by the
Stop hook; reply and fallback dedupe; the reply MCP remains available with
`--tools ""`; and the read-only view reaches the fixed private socket. At an
idle boundary, use the operator-only config helper to select `channel`; it
persists an epoch fence, revoke, and `needs_handoff` before the normal delayed
backend restart.

As a disconnect smoke, terminate only the channel MCP child while watching the
dedicated tmux session. The request admitted before the disconnect must become
terminal-uncertain, authenticated health must disappear, the isolated parent
Claude TUI must exit, and the launcher must create a new healthy session. Do
not kill or inspect Xiaoke's tmux socket during this check.

Transport configuration is copy-on-write. A config-file fsync/replace failure
restores the in-process transport selection; the already persisted external
epoch fence is intentionally retained as a conservative revoke/handoff signal
and cannot activate the candidate transport by itself.

Rollback uses the same idle helper to select `relay`. It refuses unresolved
channel requests, raises/revokes the epoch fence, and makes the next engine
turn consume authoritative history. Stop the channel service only after the
flag is confirmed as relay. Never delete channel state or chat history.

## Current attachment boundary

New images/files are deliberately rejected for Xia text-only relay turns. A
server-local attachment path is not readable by the restricted Claude/Codex
driver and must never be presented as if the model received the bytes. Existing
attachment records in `ai_chat_history.jsonl` remain visible/downloadable in
the app. Enable new uploads only after a separate bounded byte bridge is
designed and tested.
