# Xia-only Claude Development Channel

This is a production-oriented, still feature-flagged transport for the
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
- corrupt durable state aborts startup instead of resetting dedupe fences;
- fixed private tmux state and a genuinely read-only bound persona workspace.

`prepare-runtime.sh` creates only private directories and a token. It does not
copy credentials, install/start a service, change the backend flag, or touch
Xiaoke/Kairos state. Deployment must follow `docs/xia-relay-control.md` and an
independent review. The default backend transport remains `relay`.

Tests:

```bash
npm ci --ignore-scripts
npm test
cd ..
python3 -m unittest -v _xia_claude_channel_test.py _ai_session_relay_test.py
```
