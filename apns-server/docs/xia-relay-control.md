# Xia Yizhou isolated relay control

`ai-custom` now has one isolated Claude Code session and one isolated Codex
thread behind `ai-session-relay`. Kairos and Xiaoke routes, pointers, workspaces,
and sessions are not read or changed by this integration.

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

The persona API has no file-count limit. Each text file is limited to 256 KiB,
custom text to 512 KiB, and the combined UTF-8 text to 2 MiB. These byte limits
bound resource use while allowing any number of small files.

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

## Current attachment boundary

New images/files are deliberately rejected for Xia text-only relay turns. A
server-local attachment path is not readable by the restricted Claude/Codex
driver and must never be presented as if the model received the bytes. Existing
attachment records in `ai_chat_history.jsonl` remain visible/downloadable in
the app. Enable new uploads only after a separate bounded byte bridge is
designed and tested.
