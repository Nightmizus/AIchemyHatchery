import assert from 'node:assert/strict'
import http from 'node:http'
const requests = []
const server = http.createServer(async (req, res) => {
  let body = ''; for await (const part of req) body += part
  requests.push({ path: req.url, auth: req.headers.authorization, body: JSON.parse(body) })
  res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"active":false}')
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
try {
  process.env.MIAODA_PREVIEW_PORT = String(server.address().port)
  process.env.MIAODA_SITE_RUNTIME_TOKEN = 'only-this-site-capability'
  for (const mode of ['full', 'chat', 'plan']) {
    process.env.MIAODA_TOOLS_MODE = mode
    const registered = new Map()
    const plugin = await import(`../deepseek_harness/site-tools.mjs?runtime=${mode}`)
    plugin.apply({ tools: { register: tool => registered.set(tool.name, tool) } })
    const tool = registered.get('site_service'); assert(tool)
    assert.deepEqual(JSON.parse(await tool.execute({ action: 'status' })), { active: false })
    if (mode !== 'full') await assert.rejects(tool.execute({ action: 'exec', command: 'echo no' }))
    assert.equal(tool.parameters.additionalProperties, false)
    assert(!('workspace' in tool.parameters.properties)); assert(!('siteKey' in tool.parameters.properties))
  }
  assert.equal(requests.length, 3)
  assert(requests.every(req => req.path === '/api/ai/site-runtime' && req.auth === 'Bearer only-this-site-capability'))
  assert(requests.every(req => !('workspace' in req.body) && !('siteKey' in req.body)))
  console.log('PASS: service tool bound to server-selected site; chat/plan cannot mutate; no client-selectable workspace or identity')
} finally { server.close() }
