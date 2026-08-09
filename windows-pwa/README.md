# CcCompanion for Windows — PWA shell

An installable, desktop-first shell for the private CcCompanion workspace. It deliberately contains no credentials and talks to the existing CcCompanion service through an injected request function.

## Run locally

Serve this directory with any static HTTPS-capable host. It starts in a fully functional mock mode, so no server or secrets are required for design work.

## Production adapter contract

At `/web/pwa/`, `src/bootstrap.js` first checks `GET /web/session`. A failed check shows a small login form that posts to `/web/session`; the server sets an opaque `HttpOnly`, same-site cookie. The PWA never receives, persists, or adds a shared secret/token. Every production `fetch` uses same-origin credentials. `?mock=1` is the explicit offline design/demo mode.

`src/api.js` exports `createHttpAdapter({ baseUrl, request })`. The host owns authentication: `request(path, options)` returns parsed JSON. No browser request may add a token header or persist a credential.

| UI need | Adapter method | Existing/proposed service payload |
| --- | --- | --- |
| Session & contacts | `getWebSession()` / `contacts()` | `GET /web/session`, then `GET /chat/contacts` → `{contacts: []}` |
| Per-contact history | `getHistory(contactId)` | `GET /chat/history?contact_id=…` → `{records: []}` |
| Live updates | `subscribe(listener, {contactId})` | Same-origin SSE `GET /chat/stream?contact_id=…` plus visibility-aware, bounded history/status polling fallback |
| Busy, streamed draft and worker cards | `getLiveState(contactId)` | `GET /chat/status?contact_id=…` → `busy`, `draft`, `stop_request`, `worker_activity_items` |
| Send | `sendMessage(contactId, body)` | `POST /chat/send` with `{contact_id,text,attachment_ids}` → accepted message / turn metadata |
| Stop | `stop(contactId, stopRequest)` | `POST /chat/stop` with the server-provided opaque `status.stop_request.body`; client code never guesses Xiaoke/Kairos turn fences |
| Dynamic memory taxonomy | `getTaxonomy()` | `GET /memory/taxonomy` → `{categories:[{key,label,subcategories:[]}]}` |
| Memory list | `listMemories(scope)` | `GET /memory/list?category=…&subcategory=…` |
| Upload | `uploadAttachments(contactId, files)` | Raw bytes to `POST /chat/upload?contact_id=…&filename=…`; stages `{attachment_id,…}` only, then one `/chat/send` consumes those IDs. `/chat/upload/cancel` removes staged pre-send files. `/web/session` advertises the current per-file, count, and pending-total-byte limits; the client preflights those dynamic values (safe total fallback: 64 MiB). |

`subscribe(listener, {contactId})` delivers contact-scoped SSE deltas, connection state, and polling snapshots. It closes/aborts cleanly on a contact switch, hidden tab, offline event, or logout; state must never cross Xiaoke/Kairos histories, drafts, busy flags, or stop fences.

## Verification

`npm test` runs adapter and source-contract tests using only Node’s built-in test runner. No packages are installed or required.
