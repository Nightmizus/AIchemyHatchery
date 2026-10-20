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
  const literal = 'const $$ = selector => document.querySelectorAll(selector); // $& $` $\' $$'
  await call('write_file', { path: 'literal.js', content: 'BEFORE REPLACE AFTER' })
  await call('replace_file', { path: 'literal.js', search: 'REPLACE', replace: literal })
  assert.equal(await readFile(join(workspace, 'literal.js'), 'utf8'), `BEFORE ${literal} AFTER`)
  await assert.rejects(call('replace_file', { path: 'literal.js', search: 'BEFORE' }), /replace is required/)
  await assert.rejects(call('replace_file', { path: 'literal.js', replace: 'NO' }), /search is required/)
  await assert.rejects(call('replace_file', { path: 'literal.js', search: 'MISSING', replace: 'NO' }), /Read the current file again/)
  assert.match(await call('search_files', { path: '.', query: 'querySelectorAll' }), /literal.js:1:/)
  assert.match(await call('search_files', { path: 'assets', query: 'dataset.ready' }), /assets\/app.js:1:/)
  assert.match(await call('search_files', { path: './', query: 'querySelectorAll' }), /literal.js:1:/)
  assert.match(await call('search_files', { path: 'assets/', query: 'dataset.ready' }), /assets\/app.js:1:/)
  assert.match(await call('read_file', JSON.stringify({ path: 'literal.js' })), /querySelectorAll/)
  await assert.rejects(call('write_file', '{"path":"index.html","content":"broken'), /complete JSON/)
  assert.equal(await readFile(join(workspace, 'index.html'), 'utf8'), '<h1>Updated</h1>')
  const previewBase = 'http://127.0.0.1:4173' + process.env.MIAODA_PREVIEW_PATH
  for (const path of ['index.html', '/index.html', process.env.MIAODA_PREVIEW_PATH + 'index.html']) {
    assert.equal(await plugin.localUrl(path), previewBase + 'index.html')
  }
  for (const path of ['/api/auth/me', '/preview/other/index.html', '../index.html', '%2e%2e/index.html', 'https://example.com/', '//example.com/', '/index.html/../../api/auth/me', '%2fetc/passwd']) {
    await assert.rejects(plugin.localUrl(path), /website|preview|outside/)
  }
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
    await assert.rejects(call('search_files', { path, query: 'untouched' }))
    await assert.rejects(plugin.localUrl(path))
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
