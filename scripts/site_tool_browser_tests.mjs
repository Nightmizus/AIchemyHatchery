// Real Chromium + local HTTP. No model, network service or production writes.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const root = await mkdtemp(join(tmpdir(), 'hatchery-browser-tool-'))
const sockets = new Set()
const server = createServer((req, res) => {
  if (process.env.HATCHERY_BROWSER_TEST_DEBUG) console.log('fixture request', req.url)
  if (req.url.endsWith('/slow.svg')) return // Deliberately never completes.
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
  res.end('<!doctype html><title>Own preview</title><h1>Own site loaded</h1>' +
    (req.url.endsWith('/slow.html') ? '<img src="slow.svg">' : '') +
    '<script>document.body.dataset.executed="yes"</script>')
})
server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
if (process.env.HATCHERY_BROWSER_TEST_DEBUG) console.log('fixture port', server.address().port)
try {
  const workspace = join(root, 'website'); await mkdir(workspace)
  await writeFile(join(workspace, 'index.html'), '<h1>Own site</h1>')
  await writeFile(join(workspace, 'slow.html'), '<h1>Slow site</h1>')
  process.env.MIAODA_AI_WORKSPACE = workspace
  process.env.MIAODA_PREVIEW_PORT = String(server.address().port)
  process.env.MIAODA_PREVIEW_PATH = '/preview/' + 'Az09_-'.repeat(5) + 'QZ/'
  process.env.MIAODA_TOOLS_MODE = 'full'
  const definitions = new Map()
  const plugin = await import('../deepseek_harness/site-tools.mjs?browser-regression')
  plugin.apply({ tools: { register: tool => definitions.set(tool.name, tool) }, get: name => name === 'attachments' ? {
    saveImage: async value => { assert(value.data.length > 100); return { attachmentId: 'fixture-image', mediaType: value.mediaType } },
  } : undefined })
  const cancelled = new AbortController(); cancelled.abort()
  await assert.rejects(definitions.get('browser_open').execute({ path: 'index.html' }, { signal: cancelled.signal }), /cancelled/)
  const opened = await definitions.get('browser_open').execute({ path: 'index.html' })
  assert.match(opened, /Own site loaded/)
  assert.match(opened, /data-executed="yes"/)
  console.log('PASS: relative preview DOM')
  const started = Date.now()
  const partial = await definitions.get('browser_open').execute({ path: '/slow.html' })
  assert.match(partial, /Own site loaded/)
  assert.match(partial, /resources may still be loading/)
  assert(Date.now() - started < 30000, 'slow resources must not reach the former 45-second hard failure')
  console.log('PASS: bounded slow-resource DOM')
  const shot = await definitions.get('browser_screenshot').execute({ path: 'index.html', width: 390, height: 844 })
  assert.match(shot.url, /\/preview\//)
  assert.equal(shot.attachment.attachmentId, 'fixture-image')
  console.log(JSON.stringify({ passed: 4, checks: ['cancelled browser call does not start a process', 'relative preview DOM runs JavaScript', 'never-ending resource still returns bounded DOM snapshot', 'scoped screenshot still works'], realModelCalls: 0 }))
} finally {
  for (const socket of sockets) socket.destroy()
  await new Promise(resolve => server.close(resolve))
  await rm(root, { recursive: true, force: true })
}
