// Filesystem tool tests. No model is called or simulated.
import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile, symlink, link, rm, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const root = await mkdtemp(join(tmpdir(), 'hatchery-file-tools-'))
try {
  const workspace = join(root, 'site'); await mkdir(workspace)
  process.env.MIAODA_AI_WORKSPACE = workspace
  process.env.MIAODA_PREVIEW_PATH = '/preview/' + 'a'.repeat(32) + '/'
  process.env.MIAODA_TOOLS_MODE = 'full'
  const tools = new Map()
  const plugin = await import('../deepseek_harness/site-tools.mjs?full')
  plugin.apply({ tools: { register: tool => tools.set(tool.name, tool) } })
  const call = (name, args) => tools.get(name).execute(args)
  await call('write_file', { path: 'index.html', content: '<h1>Real file</h1>' })
  await call('write_file', { path: 'assets/app.js', content: "document.body.dataset.ready='yes'" })
  await call('replace_file', { path: 'index.html', search: 'Real file', replace: 'Updated' })
  assert.equal(await readFile(join(workspace, 'index.html'), 'utf8'), '<h1>Updated</h1>')
  await call('write_file', { path: 'assets/logo.bin', content: 'AAECA/8=', encoding: 'base64' })
  assert.equal(await call('read_file', { path: 'assets/logo.bin', encoding: 'base64' }), 'AAECA/8=')
  await call('move_file', { path: 'assets/app.js', destination: 'scripts/main.js' })
  await call('make_directory', { path: 'empty/nested' })
  assert.match(await call('list_files', {}), /scripts\/main.js/)
  for (const path of ['../secret', '/etc/passwd', 'a/../../secret', 'a/../site.json', '.', 'a\\b']) {
    await assert.rejects(call('write_file', { path, content: 'NO' }))
  }
  await writeFile(join(root, 'secret'), 'untouched')
  await symlink(join(root, 'secret'), join(workspace, 'escape'))
  await symlink(root, join(workspace, 'directory-escape'))
  await link(join(root, 'secret'), join(workspace, 'hardlink'))
  for (const path of ['escape', 'directory-escape/secret', 'hardlink']) {
    await assert.rejects(call('read_file', { path }))
    await assert.rejects(call('write_file', { path, content: 'NO' }))
  }
  assert.equal(await readFile(join(root, 'secret'), 'utf8'), 'untouched')
  await assert.rejects(call('write_file', { path: 'site.json', content: '{"format":"files","pages":[{"code":"bad"}]}' }))
  await assert.rejects(call('make_directory', { path: 'site.json' }))
  await assert.rejects(call('write_file', { path: 'site.json/index.html', content: 'NO' }))
  await call('write_file', { path: 'site.json', content: '{"format":"files","siteName":"网站"}' })
  await call('delete_file', { path: 'assets', recursive: true })
  for (const mode of ['chat', 'plan']) {
    process.env.MIAODA_TOOLS_MODE = mode
    const registered = []
    const readonly = await import(`../deepseek_harness/site-tools.mjs?${mode}`)
    readonly.apply({ tools: { register: tool => registered.push(tool.name) } })
    assert(!registered.some(name => ['write_file', 'replace_file', 'delete_file', 'move_file', 'make_directory'].includes(name)))
  }
  await assert.rejects(call('browser_open', { path: '/api/auth/me' }, { signal: undefined }))
  console.log('PASS: real file operations, binary assets, metadata schema, traversal/link isolation, readonly modes, preview URL boundary')
} finally { await rm(root, { recursive: true, force: true }) }
