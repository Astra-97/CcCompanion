import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import net from 'node:net'
import { spawn } from 'node:child_process'
import test from 'node:test'

const TOKEN = 'test-token-not-secret'

async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolve(port))
    })
  })
}

async function startServer(state, port, envOverrides = {}) {
  const env = { ...process.env, XIA_CHANNEL_HOST: '127.0.0.1', XIA_CHANNEL_PORT: String(port),
    XIA_CHANNEL_TOKEN: TOKEN, XIA_CHANNEL_STATE_DIR: state,
    XIA_CHANNEL_GENERATION: '1', XIA_CHANNEL_SESSION_ID: '00000000-0000-4000-8000-000000000001',
    XIA_CHANNEL_TEST_NO_PARENT_KILL: '1' }
  for (const [key, value] of Object.entries(envOverrides)) {
    if (value === null) delete env[key]
    else env[key] = value
  }
  const child = spawn(process.execPath, ['server.mjs'], {
    cwd: new URL('.', import.meta.url),
    env,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  child.stderr.on('data', () => {})
  const base = `http://127.0.0.1:${port}`
  for (let i = 0; i < 100; i += 1) {
    if (child.exitCode != null) break
    try {
      const response = await fetch(`${base}/health`, { headers: { 'X-Auth-Token': TOKEN } })
      if (response.ok) return { child, base }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  child.kill('SIGKILL')
  throw new Error('server did not start')
}

async function stopServer(child, signal = 'SIGTERM') {
  if (child.exitCode != null) return
  child.kill(signal)
  await Promise.race([
    new Promise(resolve => child.once('exit', resolve)),
    new Promise(resolve => setTimeout(resolve, 2000)),
  ])
  if (child.exitCode == null) child.kill('SIGKILL')
}

async function request(base, path, { method = 'GET', body, token = TOKEN } = {}) {
  const response = await fetch(base + path, {
    method, headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value = await response.json()
  return { status: response.status, value }
}

function message(overrides = {}) {
  return {
    request_id: 'request-1', client_id: 'client-1', contact_id: 'ai-custom', provider: 'claude',
    epoch: 7, lease: 'lease-1', generation: 1, text: 'hello', handoff: 'history', ...overrides,
  }
}

test('durable exact-grant ledger rejects wrong routes and completes only once', async () => {
  const state = mkdtempSync(join(tmpdir(), 'xia-channel-'))
  const port = await freePort()
  const { child, base } = await startServer(state, port)
  try {
    assert.equal((await request(base, '/health', { token: 'wrong' })).status, 401)
    assert.equal((await request(base, '/messages', { method: 'POST', body: message({ contact_id: 'xiaoke' }) })).status, 409)
    assert.equal((await request(base, '/messages', { method: 'POST', body: message({ provider: 'codex' }) })).status, 409)
    assert.equal((await request(base, '/messages', { method: 'POST', body: message({ generation: 2 }) })).status, 409)
    assert.equal((await request(base, '/messages', { method: 'POST', body: message() })).status, 202)
    assert.equal((await request(base, '/messages', { method: 'POST', body: message({ client_id: 'wrong' }) })).status, 409)
    assert.equal((await request(base, '/fallback', { method: 'POST', body: {
      request_id: 'request-1', epoch: 7, lease: 'wrong', text: 'reply',
    } })).status, 409)
    const first = await request(base, '/fallback', { method: 'POST', body: {
      request_id: 'request-1', epoch: 7, lease: 'lease-1', text: 'reply',
    } })
    assert.equal(first.status, 200); assert.equal(first.value.reply, 'reply'); assert.equal(first.value.duplicate, false)
    const duplicate = await request(base, '/fallback', { method: 'POST', body: {
      request_id: 'request-1', epoch: 7, lease: 'lease-1', text: 'ignored second',
    } })
    assert.equal(duplicate.status, 200); assert.equal(duplicate.value.reply, 'reply'); assert.equal(duplicate.value.duplicate, true)
    const retry = await request(base, '/messages', { method: 'POST', body: message() })
    assert.equal(retry.status, 200); assert.equal(retry.value.status, 'completed'); assert.equal(retry.value.reply, 'reply')
    await request(base, '/revoke', { method: 'POST', body: { epoch: 8 } })
    assert.equal((await request(base, '/messages', { method: 'POST', body: message({ request_id: 'old', lease: 'old', epoch: 7 }) })).status, 409)
    assert.equal((await request(base, '/fallback', { method: 'POST', body: {
      request_id: 'missing', epoch: 8, lease: 'none', text: 'x',
    } })).status, 409)
    assert.equal((await request(base, '/fallback', { method: 'POST', body: {
      request_id: 'request-1', epoch: 7, lease: 'lease-1', text: 'x'.repeat(128 * 1024 + 1),
    } })).status, 500)
    assert.equal(statSync(state).mode & 0o777, 0o700)
    assert.equal(statSync(join(state, 'ledger.json')).mode & 0o777, 0o600)
  } finally {
    await stopServer(child); rmSync(state, { recursive: true, force: true })
  }
})

test('rotate drains old process until exact startup token handshakes new generation', async () => {
  const state = mkdtempSync(join(tmpdir(), 'xia-channel-'))
  const port = await freePort()
  let running = await startServer(state, port)
  try {
    const rotated = await request(running.base, '/rotate', { method: 'POST', body: { generation: 2, model: 'opus' } })
    assert.equal(rotated.status, 202)
    const draining = await request(running.base, '/health')
    assert.equal(draining.value.ready, false); assert.equal(draining.value.requires_fresh, true); assert.equal(draining.value.draining, true)
    assert.equal((await request(running.base, '/messages', { method: 'POST', body: message({ generation: 2 }) })).status, 503)
    const control = JSON.parse(readFileSync(join(state, 'control.json'), 'utf8'))
    await stopServer(running.child, 'SIGKILL')
    running = await startServer(state, port, {
      XIA_CHANNEL_GENERATION: '2', XIA_CHANNEL_SESSION_ID: control.session_id,
      XIA_CHANNEL_MODEL: 'opus', XIA_CHANNEL_BOOTSTRAP_TOKEN: control.bootstrap_token,
    })
    const ready = await request(running.base, '/health')
    assert.equal(ready.value.ready, true); assert.equal(ready.value.generation, 2); assert.equal(ready.value.model, 'opus')
  } finally {
    await stopServer(running.child); rmSync(state, { recursive: true, force: true })
  }
})

test('existing corrupt or schema-invalid durable state fails closed', async () => {
  for (const [name, file, contents] of [
    ['corrupt-ledger', 'ledger.json', '{bad'],
    ['invalid-ledger', 'ledger.json', JSON.stringify({ version: 1, epoch_floor: -1, records: {} })],
    ['invalid-control', 'control.json', JSON.stringify({ version: 1, generation: 0 })],
  ]) {
    const state = mkdtempSync(join(tmpdir(), `xia-${name}-`))
    writeFileSync(join(state, file), contents, { mode: 0o600 })
    const port = await freePort()
    await assert.rejects(startServer(state, port))
    rmSync(state, { recursive: true, force: true })
  }
})

test('token file must be private regular and non-symlink', async () => {
  const state = mkdtempSync(join(tmpdir(), 'xia-token-'))
  const token = join(state, 'token')
  writeFileSync(token, TOKEN, { mode: 0o644 })
  await assert.rejects(startServer(state, await freePort(), { XIA_CHANNEL_TOKEN: null, XIA_CHANNEL_TOKEN_FILE: token }))
  chmodSync(token, 0o600)
  const link = join(state, 'token-link'); symlinkSync(token, link)
  await assert.rejects(startServer(state, await freePort(), { XIA_CHANNEL_TOKEN: null, XIA_CHANNEL_TOKEN_FILE: link }))
  rmSync(state, { recursive: true, force: true })
})

test('restart turns admitted running request uncertain while completed result survives', async () => {
  const state = mkdtempSync(join(tmpdir(), 'xia-channel-'))
  const port = await freePort()
  let running = await startServer(state, port)
  try {
    await request(running.base, '/messages', { method: 'POST', body: message({ request_id: 'pending', lease: 'pending-lease' }) })
    await request(running.base, '/messages', { method: 'POST', body: message({ request_id: 'done', lease: 'done-lease' }) })
    await request(running.base, '/fallback', { method: 'POST', body: {
      request_id: 'done', epoch: 7, lease: 'done-lease', text: 'durable',
    } })
    await stopServer(running.child, 'SIGKILL')
    running = await startServer(state, port)
    const health = await request(running.base, '/health')
    assert.equal(health.value.requires_fresh, true)
    const uncertain = await request(running.base, '/result', { method: 'POST', body: {
      request_id: 'pending', client_id: 'client-1', epoch: 7, lease: 'pending-lease',
    } })
    assert.equal(uncertain.status, 200); assert.equal(uncertain.value.status, 'uncertain')
    const completed = await request(running.base, '/result', { method: 'POST', body: {
      request_id: 'done', client_id: 'client-1', epoch: 7, lease: 'done-lease',
    } })
    assert.equal(completed.status, 200); assert.equal(completed.value.status, 'completed'); assert.equal(completed.value.reply, 'durable')
  } finally {
    await stopServer(running.child); rmSync(state, { recursive: true, force: true })
  }
})
