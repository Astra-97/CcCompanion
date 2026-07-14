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
