import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { apply as applyHost } from '../lib/index.js'
import { apply as applyTools } from '../preset-tools.mjs'

async function checkProxy() {
  const seen = []
  const sidecar = createServer(async (req, res) => {
    if (req.url === '/healthz') {
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({ ok: true, version: '0.2.1' }))
      return
    }
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    seen.push({ method: req.method, path: req.url, body: Buffer.concat(chunks).toString('utf8') })
    res.writeHead(202, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ accepted: true }))
  })
  sidecar.listen(0, '127.0.0.1')
  await once(sidecar, 'listening')
  const routes = []
  const cleanups = []
  const ctx = {
    webServer: { register(route) { routes.push(route); return () => {} } },
    inject(_services, callback) { callback(ctx) },
    effect(callback) { cleanups.push(callback()) },
  }
  applyHost(ctx, { port: sidecar.address().port })
  const proxy = createServer((req, res) => routes.find((route) => req.url.startsWith(route.path)).handler(req, res))
  proxy.listen(0, '127.0.0.1')
  await once(proxy, 'listening')
  try {
    const body = JSON.stringify({ run_id: 'worker-a', text: '只修改指定文件，保留完整输出。' })
    const response = await fetch(`http://127.0.0.1:${proxy.address().port}/api/hcca/guidance?source=ui`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body,
    })
    assert.equal(response.status, 202)
    assert.deepEqual(await response.json(), { accepted: true })
    assert.deepEqual(seen[0], { method: 'POST', path: '/api/guidance?source=ui', body })
  } finally {
    cleanups.forEach((cleanup) => cleanup?.())
    await new Promise((resolve) => proxy.close(resolve))
    await new Promise((resolve) => sidecar.close(resolve))
  }
}

async function checkWaitWake() {
  const originalFetch = globalThis.fetch
  const calls = []
  const tools = new Map()
  const hooks = new Map()
  let resolveWaiting
  const waiting = new Promise((resolve) => { resolveWaiting = resolve })
  let waitCount = 0
  globalThis.fetch = async (url, options) => {
    const path = new URL(url).pathname
    calls.push({ url: String(url), method: options.method, body: options.body && JSON.parse(options.body) })
    if (path === '/api/delegate') return Response.json({ run_id: 'worker-a', status: 'RUNNING' })
    if (path === '/api/status') return Response.json({ run_id: 'worker-a', status: 'RUNNING', agents: [] })
    if (path === '/api/guidance') return Response.json({ run_id: 'worker-a', guidance_id: 'g1', status: 'queued' })
    if (path === '/api/wait') {
      waitCount += 1
      resolveWaiting()
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })
    }
    throw Error('Unexpected offline request ' + url)
  }
  const ctx = { tools: { register(tool) { tools.set(tool.name, tool) } },
    on(name, callback) { hooks.set(name, callback) }, inject() {} }
  applyTools(ctx, { port: 18766 })
  const agent = { session: { id: 'session-a' }, inbox: { nextTurn: [], nextStep: [] } }
  let concluded = 0
  const exec = { agent, concludeTurn() { concluded += 1 } }
  try {
    const pending = tools.get('hcca_delegate').execute({ objective: '阅读接口定义', role: 'reader' }, exec)
    await waiting
    hooks.get('agent/inbox/inserted')({ agent, message: { id: 'human-1', source: { kind: 'user' }, content: [{ type: 'text', text: '先重新规划' }] } })
    const result = await pending
    assert.match(result, /worker-a.*新的用户消息/)
    assert.equal(concluded, 1)
    assert.equal(calls.filter((call) => call.url.includes('/api/guidance')).length, 0)
    assert.equal(calls.filter((call) => call.url.includes('/api/cancel')).length, 0)
    assert.equal(calls[0].body.external_root, true)
    assert.equal(calls[0].body.role, 'reader')
    assert.ok(calls.every((call) => call.url.startsWith('http://127.0.0.1:18766/')))
    assert.equal(tools.get('hcca_delegate').isConcurrencySafe(), true)
    assert.deepEqual(tools.get('hcca_guidance').parameters.required, ['run_id', 'text'])
    await tools.get('hcca_guidance').execute({ run_id: 'worker-a', text: '聚焦接口参数' }, exec)
    assert.equal(calls.at(-1).body.run_id, 'worker-a')
    agent.inbox.nextTurn.push({ source: { kind: 'user' } })
    const queued = await tools.get('hcca_status').execute({ run_id: 'worker-a', timeout_seconds: 2400 }, exec)
    assert.match(queued, /新的用户消息/)
    assert.equal(waitCount, 1)
    assert.equal(concluded, 2)
  } finally {
    globalThis.fetch = originalFetch
  }
}

await checkProxy()
await checkWaitWake()
console.log('Offline checks passed: POST proxy, configured port, worker delegation, user-input wake, and directed guidance.')
