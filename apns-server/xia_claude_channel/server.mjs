#!/usr/bin/env node
import http from 'node:http'
import { randomUUID, timingSafeEqual } from 'node:crypto'
import { chmodSync, fsyncSync, lstatSync, mkdirSync, openSync, closeSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { URL } from 'node:url'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'

const HOST = process.env.XIA_CHANNEL_HOST || '127.0.0.1'
const PORT = Number.parseInt(process.env.XIA_CHANNEL_PORT || '8821', 10)
const TOKEN = process.env.XIA_CHANNEL_TOKEN || readSecret(process.env.XIA_CHANNEL_TOKEN_FILE)
const STATE_DIR = resolve(process.env.XIA_CHANNEL_STATE_DIR || './state')
const LEDGER_PATH = join(STATE_DIR, 'ledger.json')
const CONTROL_PATH = join(STATE_DIR, 'control.json')
const CONTACT = 'ai-custom'
const PROVIDER = 'claude'
const MAX_BODY = 1024 * 1024
const MAX_RECORDS = 2000
const MAX_LEDGER_BYTES = 8 * 1024 * 1024
const MAX_REPLY_BYTES = 128 * 1024
const BOOT_ID = randomUUID()
const STARTUP = {
  generation: Number(process.env.XIA_CHANNEL_GENERATION || 1),
  session_id: process.env.XIA_CHANNEL_SESSION_ID || '',
  model: process.env.XIA_CHANNEL_MODEL || '',
  bootstrap_token: process.env.XIA_CHANNEL_BOOTSTRAP_TOKEN || '',
}
const waiters = new Map()
let mcpConnected = false
let shuttingDown = false

if (!['127.0.0.1', 'localhost', '::1'].includes(HOST) || !TOKEN || TOKEN.length > 4096 || /[\r\n]/.test(TOKEN)) {
  throw new Error('Xia channel requires loopback bind and a non-empty token')
}
privateDir(STATE_DIR)
let ledger = loadState(LEDGER_PATH, { version: 1, epoch_floor: 0, records: {} }, validateLedger)
let control = loadState(CONTROL_PATH, {
  version: 1, generation: STARTUP.generation, session_id: STARTUP.session_id,
  model: STARTUP.model, requires_fresh: false, draining: false, bootstrap_token: '',
}, validateControl)
const bootstrapCandidate = validateStartupHandshake()
const recoveredInterrupted = recoverInterruptedRequests()
if (bootstrapCandidate && !recoveredInterrupted) {
  control = { ...control, requires_fresh: false, draining: false, bootstrap_token: '', stale_reason: '' }
  atomicJson(CONTROL_PATH, control)
}

const mcp = new Server(
  { name: 'cc-xia-claude-channel', version: '1.0.0' },
  {
    capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions: [
      'This channel serves only the isolated Xia Yizhou ai-custom conversation.',
      'For every inbound message, call reply exactly once with the exact request_id, epoch, and lease from channel metadata.',
      'Do not invent or alter routing metadata. Do not send raw thinking. No other contact or outbound route exists.',
    ].join('\n'),
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [replyToolDefinition()] }))
mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name !== 'reply') return toolError('unknown tool')
  try {
    const args = object(req.params.arguments)
    const result = completeRecord({
      request_id: simple(args.request_id, 'request_id'),
      epoch: integer(args.epoch, 'epoch'),
      lease: simple(args.lease, 'lease'),
      text: requiredText(args.text, 'text', MAX_REPLY_BYTES),
      source: 'reply',
    })
    return toolText(result.duplicate ? 'duplicate reply already recorded' : 'reply recorded')
  } catch (error) {
    return toolError(publicError(error))
  }
})

const httpServer = http.createServer((req, res) => {
  void route(req, res).catch(error => sendJson(res, error.statusCode || 500, {
    ok: false, code: error.code || 'internal_error', error: publicError(error),
  }))
})
httpServer.listen(PORT, HOST)
await mcp.connect(new StdioServerTransport())
mcpConnected = true

process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
process.stdin.on('end', shutdown)

async function route(req, res) {
  const url = new URL(req.url || '/', `http://${req.headers.host || `${HOST}:${PORT}`}`)
  if (!authorized(req)) return sendJson(res, 401, { ok: false, code: 'unauthorized', error: 'unauthorized' })

  if (req.method === 'GET' && url.pathname === '/health') {
    return sendJson(res, 200, {
      ok: true, ready: isReady(),
      mcp_connected: mcpConnected, generation: Number(control.generation || 0),
      session_id: String(control.session_id || ''), model: String(control.model || ''),
      requires_fresh: Boolean(control.requires_fresh),
      draining: Boolean(control.draining),
      epoch_floor: Number(ledger.epoch_floor || 0),
    })
  }
  if (req.method === 'POST' && url.pathname === '/messages') {
    if (!isReady()) throw httpError(503, 'generation_stale', 'Claude channel is not ready for this generation')
    const body = await readJson(req)
    const normalized = normalizeMessage(body)
    const existing = ledger.records[normalized.request_id]
    if (existing) {
      exactGrant(existing, normalized)
      if (existing.status === 'uncertain') throw httpError(409, 'request_uncertain', existing.error || 'request completion is uncertain')
      return sendJson(res, 200, publicRecord(existing, true))
    }
    if (normalized.epoch < Number(ledger.epoch_floor || 0)) throw httpError(409, 'stale', 'provider epoch is stale')
    if (normalized.generation !== Number(control.generation || 0)) throw httpError(409, 'generation_stale', 'Claude generation is stale')
    const record = {
      ...normalized, status: 'accepted', created_at: now(), updated_at: now(),
      boot_id: BOOT_ID, reply: '', source: '', error: '',
    }
    ledger.records[record.request_id] = record
    pruneAndSave()
    try {
      await notifyClaude(record)
      if (record.status === 'accepted') {
        record.status = 'running'; record.updated_at = now(); saveLedger()
      }
    } catch (error) {
      record.status = 'uncertain'; record.error = 'channel notification failed after durable admission'
      record.updated_at = now(); saveLedger(); wake(record.request_id)
      throw httpError(409, 'request_uncertain', record.error)
    }
    return sendJson(res, 202, publicRecord(record, false))
  }
  if (req.method === 'POST' && url.pathname === '/result') {
    const body = await readJson(req)
    const request_id = simple(body.request_id, 'request_id')
    const expected = {
      request_id, client_id: simple(body.client_id, 'client_id'),
      epoch: integer(body.epoch, 'epoch'), lease: simple(body.lease, 'lease'),
    }
    let record = ledger.records[request_id]
    if (!record) throw httpError(404, 'not_found', 'request not found')
    exactGrant(record, expected)
    const waitMs = Math.max(0, Math.min(Number.parseInt(String(body.wait_ms || '0'), 10) || 0, 25000))
    if (waitMs && ['accepted', 'running'].includes(record.status)) {
      await waitFor(request_id, waitMs)
      record = ledger.records[request_id]
    }
    return sendJson(res, 200, publicRecord(record, true))
  }
  if (req.method === 'POST' && url.pathname === '/fallback') {
    const body = await readJson(req)
    const result = completeRecord({
      request_id: simple(body.request_id, 'request_id'), epoch: integer(body.epoch, 'epoch'),
      lease: simple(body.lease, 'lease'), text: requiredText(body.text, 'text', MAX_REPLY_BYTES), source: 'stop_fallback',
    })
    return sendJson(res, 200, publicRecord(result.record, result.duplicate))
  }
  if (req.method === 'POST' && url.pathname === '/revoke') {
    const body = await readJson(req)
    const floor = integer(body.epoch, 'epoch')
    ledger.epoch_floor = Math.max(Number(ledger.epoch_floor || 0), floor)
    let revoked = 0
    for (const record of Object.values(ledger.records)) {
      if (['accepted', 'running'].includes(record.status) && Number(record.epoch) < ledger.epoch_floor) {
        record.status = 'uncertain'; record.error = 'request revoked by provider epoch change'; record.updated_at = now()
        record.text = ''; record.handoff = ''
        revoked += 1; wake(record.request_id)
      }
    }
    if (revoked) {
      control = { ...control, requires_fresh: true, draining: true, bootstrap_token: '', stale_reason: 'provider epoch revoke' }
      atomicJson(CONTROL_PATH, control)
    }
    saveLedger()
    return sendJson(res, 200, { ok: true, epoch_floor: ledger.epoch_floor, revoked })
  }
  if (req.method === 'POST' && url.pathname === '/rotate') {
    const body = await readJson(req)
    const generation = integer(body.generation, 'generation')
    const model = optionalModel(body.model)
    if (generation < Number(control.generation || 0)) throw httpError(409, 'generation_stale', 'generation cannot move backward')
    if (generation === Number(control.generation || 0) && model === String(control.model || '') && mcpConnected && !control.requires_fresh) {
      return sendJson(res, 200, { ok: true, changed: false, generation, model })
    }
    for (const record of Object.values(ledger.records)) {
      if (['accepted', 'running'].includes(record.status)) {
        record.status = 'uncertain'; record.error = 'request interrupted by required fresh Claude generation'; record.updated_at = now(); wake(record.request_id)
        record.text = ''; record.handoff = ''
      }
    }
    control = {
      version: 1, generation, model, session_id: randomUUID(),
      requires_fresh: true, draining: true, bootstrap_token: randomUUID(),
      rotate_requested_at: now(),
    }
    atomicJson(CONTROL_PATH, control); saveLedger()
    sendJson(res, 202, { ok: true, changed: true, generation, model })
    if (process.env.XIA_CHANNEL_TEST_NO_PARENT_KILL !== '1') {
      setTimeout(() => { try { process.kill(process.ppid, 'SIGTERM') } catch {} }, 100).unref?.()
    }
    return
  }
  throw httpError(404, 'not_found', 'not found')
}

function normalizeMessage(body) {
  body = object(body)
  if (body.contact_id !== CONTACT || body.provider !== PROVIDER) throw httpError(409, 'stale', 'wrong fixed route')
  return {
    request_id: simple(body.request_id, 'request_id'), client_id: simple(body.client_id, 'client_id'),
    contact_id: CONTACT, provider: PROVIDER, epoch: integer(body.epoch, 'epoch'),
    lease: simple(body.lease, 'lease'), generation: integer(body.generation, 'generation'),
    text: requiredText(body.text, 'text', 256 * 1024), handoff: optionalText(body.handoff, 24000),
  }
}

async function notifyClaude(record) {
  if (!mcpConnected) throw new Error('Claude channel MCP is not connected')
  const content = record.handoff ? `${record.handoff}\n\n[Current user message]\n${record.text}` : record.text
  const grantMeta = {
    contact_id: CONTACT, provider: PROVIDER, request_id: record.request_id,
    client_id: record.client_id, epoch: record.epoch, lease: record.lease,
    generation: record.generation,
  }
  await mcp.notification({
    method: 'notifications/claude/channel',
    params: { content, meta: { ...grantMeta, metadata_json: JSON.stringify(grantMeta) } },
  })
}

function completeRecord({ request_id, epoch, lease, text, source }) {
  const record = ledger.records[request_id]
  if (!record) throw httpError(409, 'stale', 'no active grant')
  exactGrant(record, { request_id, epoch, lease })
  if (record.status === 'completed') return { record, duplicate: true }
  if (!['accepted', 'running'].includes(record.status)) throw httpError(409, 'stale', 'grant is no longer active')
  if (Number(record.epoch) < Number(ledger.epoch_floor || 0)) throw httpError(409, 'stale', 'provider epoch is stale')
  record.status = 'completed'; record.reply = text.trim(); record.source = source; record.updated_at = now(); record.error = ''
  record.text = ''; record.handoff = ''
  saveLedger(); wake(request_id)
  return { record, duplicate: false }
}

function exactGrant(record, expected) {
  for (const key of ['request_id', 'client_id', 'epoch', 'lease']) {
    if (expected[key] !== undefined && String(record[key]) !== String(expected[key])) throw httpError(409, 'stale', `wrong ${key}`)
  }
  if (record.contact_id !== CONTACT || record.provider !== PROVIDER) throw httpError(409, 'stale', 'wrong route')
}

function publicRecord(record, duplicate) {
  const result = { ok: record.status === 'completed', request_id: record.request_id, status: record.status, duplicate: Boolean(duplicate) }
  if (record.status === 'completed') result.reply = record.reply
  if (record.error) result.error = record.error
  return result
}

function recoverInterruptedRequests() {
  let changed = false
  for (const record of Object.values(ledger.records || {})) {
    if (['accepted', 'running'].includes(record.status)) {
      record.status = 'uncertain'; record.error = 'channel process restarted during an admitted request'; record.updated_at = now(); changed = true
      record.text = ''; record.handoff = ''
    }
  }
  if (changed) {
    control = { ...control, requires_fresh: true, draining: true, bootstrap_token: '', stale_reason: 'interrupted admitted request' }
    atomicJson(CONTROL_PATH, control)
    saveLedger()
  }
  return changed
}

function pruneAndSave() {
  const entries = Object.entries(ledger.records)
  if (entries.length > MAX_RECORDS) {
    const removable = entries.filter(([, r]) => ['completed', 'uncertain', 'revoked', 'stale'].includes(r.status))
      .sort((a, b) => String(a[1].updated_at).localeCompare(String(b[1].updated_at)))
    for (const [key] of removable.slice(0, Math.max(0, entries.length - MAX_RECORDS))) delete ledger.records[key]
  }
  saveLedger()
}

function saveLedger() { compactLedger(); atomicJson(LEDGER_PATH, ledger) }
function compactLedger() {
  const terminals = () => Object.entries(ledger.records)
    .filter(([, record]) => ['completed', 'uncertain', 'revoked', 'stale'].includes(record.status))
    .sort((a, b) => String(a[1].updated_at).localeCompare(String(b[1].updated_at)))
  while (Object.keys(ledger.records).length > MAX_RECORDS) {
    const oldest = terminals()[0]
    if (!oldest) throw new Error('ledger has too many active records')
    delete ledger.records[oldest[0]]
  }
  while (Buffer.byteLength(JSON.stringify(ledger)) > MAX_LEDGER_BYTES) {
    const oldest = terminals()[0]
    if (!oldest) throw new Error('active ledger exceeds safe byte budget')
    delete ledger.records[oldest[0]]
  }
}
function atomicJson(path, value) {
  privateDir(dirname(path)); const temp = `${path}.tmp-${process.pid}`
  writeFileSync(temp, `${JSON.stringify(value)}\n`, { mode: 0o600 }); chmodSync(temp, 0o600)
  const fd = openSync(temp, 'r'); try { fsyncSync(fd) } finally { closeSync(fd) }
  renameSync(temp, path); chmodSync(path, 0o600)
  const dfd = openSync(dirname(path), 'r'); try { fsyncSync(dfd) } finally { closeSync(dfd) }
}
function privateDir(path) { mkdirSync(path, { recursive: true, mode: 0o700 }); chmodSync(path, 0o700) }
function loadState(path, fallback, validator) {
  if (!exists(path)) {
    validator(fallback); atomicJson(path, fallback); return fallback
  }
  let value
  try { value = JSON.parse(readFileSync(path, 'utf8')) } catch { throw new Error(`corrupt state file: ${path}`) }
  validator(value)
  return value
}
function exists(path) { try { readFileSync(path); return true } catch (error) { if (error?.code === 'ENOENT') return false; throw error } }
function validateLedger(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 ||
      !Number.isSafeInteger(value.epoch_floor) || value.epoch_floor < 0 ||
      !value.records || typeof value.records !== 'object' || Array.isArray(value.records)) {
    throw new Error('invalid ledger schema')
  }
  if (Buffer.byteLength(JSON.stringify(value)) > MAX_LEDGER_BYTES) throw new Error('ledger exceeds safe byte budget')
  for (const [key, record] of Object.entries(value.records)) {
    if (!record || typeof record !== 'object' || Array.isArray(record) || key !== record.request_id ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(String(record.request_id || '')) ||
        !['accepted', 'running', 'completed', 'uncertain', 'revoked', 'stale'].includes(record.status) ||
        record.contact_id !== CONTACT || record.provider !== PROVIDER ||
        !Number.isSafeInteger(Number(record.epoch)) || Number(record.epoch) < 0 ||
        !Number.isSafeInteger(Number(record.generation)) || Number(record.generation) < 1 ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(String(record.client_id || '')) ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(String(record.lease || '')) ||
        Buffer.byteLength(String(record.reply || '')) > MAX_REPLY_BYTES ||
        Buffer.byteLength(String(record.text || '')) > 256 * 1024 ||
        Buffer.byteLength(String(record.handoff || '')) > 96 * 1024) {
      throw new Error('invalid ledger record schema')
    }
  }
}
function validateControl(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 ||
      !Number.isSafeInteger(Number(value.generation)) || Number(value.generation) < 1 ||
      !/^[0-9a-fA-F-]{36}$/.test(String(value.session_id || '')) ||
      (value.model && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(String(value.model))) ||
      typeof value.requires_fresh !== 'boolean' || typeof value.draining !== 'boolean' ||
      typeof value.bootstrap_token !== 'string') {
    throw new Error('invalid control schema')
  }
  if (value.bootstrap_token && !/^[0-9a-fA-F-]{36}$/.test(value.bootstrap_token)) throw new Error('invalid control bootstrap token')
}
function validateStartupHandshake() {
  if (Number(control.generation) !== STARTUP.generation || String(control.session_id) !== STARTUP.session_id || String(control.model || '') !== STARTUP.model) {
    throw new Error('startup generation/model/session does not match durable control')
  }
  if (control.draining || control.requires_fresh) {
    if (!control.bootstrap_token) return false
    if (control.bootstrap_token !== STARTUP.bootstrap_token) throw new Error('startup bootstrap token mismatch')
    return true
  }
  if (STARTUP.bootstrap_token) throw new Error('unexpected bootstrap token for ready control')
  return false
}
function isReady() {
  return mcpConnected && !shuttingDown && !control.requires_fresh && !control.draining &&
    Number(control.generation) === STARTUP.generation && String(control.session_id) === STARTUP.session_id && String(control.model || '') === STARTUP.model
}
function readSecret(path) {
  if (!path) return ''
  const info = lstatSync(path)
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) || info.size > 4096) throw new Error('unsafe Xia channel token file')
  return readFileSync(path, 'utf8').trim()
}

function replyToolDefinition() { return {
  name: 'reply', description: 'Complete the active Xia Yizhou request. Routing grant fields must exactly match channel metadata.',
  inputSchema: { type: 'object', additionalProperties: false, properties: {
    text: { type: 'string' }, request_id: { type: 'string' }, epoch: { type: 'integer' }, lease: { type: 'string' },
  }, required: ['text', 'request_id', 'epoch', 'lease'] },
} }
function toolText(text) { return { content: [{ type: 'text', text }] } }
function toolError(text) { return { ...toolText(text), isError: true } }
function object(value) { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('JSON object required'); return value }
function simple(value, name) { const s = String(value ?? ''); if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(s)) throw new Error(`invalid ${name}`); return s }
function integer(value, name) { const n = Number(value); if (!Number.isSafeInteger(n) || n < 0) throw new Error(`invalid ${name}`); return n }
function requiredText(value, name, maxBytes = 256 * 1024) { if (typeof value !== 'string' || !value.trim() || Buffer.byteLength(value) > maxBytes) throw new Error(`invalid ${name}`); return value.trim() }
function optionalText(value, max) { if (value == null) return ''; if (typeof value !== 'string' || Buffer.byteLength(value) > max * 4) throw new Error('invalid handoff'); return value.slice(-max) }
function optionalModel(value) { const s = String(value ?? '').trim(); if (s && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(s)) throw new Error('invalid model'); return s }
function publicError(error) { return String(error?.message || error || 'error').replace(TOKEN, '[redacted]').slice(0, 500) }
function httpError(statusCode, code, message) { const e = new Error(message); e.statusCode = statusCode; e.code = code; return e }
function authorized(req) { const value = String(req.headers['x-auth-token'] || '').trim(); const a = Buffer.from(value); const b = Buffer.from(TOKEN); return a.length === b.length && a.length > 0 && timingSafeEqual(a, b) }
async function readJson(req) { let size = 0; const chunks = []; for await (const chunk of req) { size += chunk.length; if (size > MAX_BODY) throw httpError(413, 'too_large', 'body too large'); chunks.push(chunk) } try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') } catch { throw httpError(400, 'bad_json', 'bad JSON') } }
function sendJson(res, status, value) { if (res.writableEnded) return; res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)) }
function now() { return new Date().toISOString() }
function waitFor(requestId, ms) { return new Promise(resolveWait => { const list = waiters.get(requestId) || new Set(); const cleanup = done => { list.delete(done); if (list.size === 0 && waiters.get(requestId) === list) waiters.delete(requestId) }; const timer = setTimeout(() => { cleanup(done); resolveWait() }, ms); timer.unref?.(); const done = () => { clearTimeout(timer); cleanup(done); resolveWait() }; list.add(done); waiters.set(requestId, list) }) }
function wake(requestId) { const list = waiters.get(requestId); if (!list) return; waiters.delete(requestId); for (const fn of list) fn() }
function shutdown() { if (shuttingDown) return; shuttingDown = true; mcpConnected = false; httpServer.close(() => process.exit(0)); setTimeout(() => process.exit(0), 1000).unref?.() }

export { completeRecord, exactGrant, normalizeMessage, publicRecord }
