import { spawn } from 'node:child_process'
import { mkdtemp, mkdir, lstat, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { basename, dirname, join, resolve, sep } from 'node:path'
import { tmpdir } from 'node:os'

export const name = 'miaoda-site-tools'
export const inject = ['tools']

const workspace = resolve(process.env.MIAODA_AI_WORKSPACE || '.miaoda-ai-workspace-missing')
const previewPort = Number.parseInt(process.env.MIAODA_PREVIEW_PORT || '4173', 10)

function objectArgs(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('arguments must be an object')
  }
  return value
}

// The agent may only touch files inside the current website's own folder.
// Everything else on the host is out of reach by construction.
async function sitePath(value) {
  if (typeof value !== 'string' || !value || value.includes('\0') || value.includes('\\')) {
    throw new Error('path must be a relative POSIX path inside the website folder')
  }
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.split('/').some(p => !p || p === '.' || p === '..')) {
    throw new Error('absolute paths are outside the website folder')
  }
  if (value.startsWith('site.json/')) throw new Error('site.json is a metadata file, not a directory')
  const target = resolve(workspace, value)
  if (target !== workspace && !target.startsWith(`${workspace}${sep}`)) {
    throw new Error('path is outside the website folder')
  }
  const rootInfo = await lstat(workspace)
  if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) throw new Error('invalid website root')
  let current = workspace
  for (const part of value.split('/')) {
    current = join(current, part)
    const info = await lstat(current).catch(error => { if (error.code === 'ENOENT') return null; throw error })
    if (info?.isSymbolicLink() || (info?.isFile() && info.nlink !== 1)) throw new Error('links are outside website permissions')
  }
  return target
}

async function listSiteFiles(directory = workspace, prefix = '', limit = { count: 2000 }) {
  if (limit.count <= 0) return []
  const entries = await readdir(directory, { withFileTypes: true }).catch(() => [])
  const files = []
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name.startsWith('.hatchery-write-')) continue
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) {
      files.push(...await listSiteFiles(join(directory, entry.name), relative, limit))
    } else if (entry.isFile()) {
      try { await sitePath(relative) } catch { continue }
      files.push(relative)
      limit.count -= 1
    }
    if (limit.count <= 0) break
  }
  return files
}

function localUrl(value) {
  const path = typeof value === 'string' && value.trim() ? value.trim() : (process.env.MIAODA_PREVIEW_PATH || '/')
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || path.includes('\0')) {
    throw new Error('browser tools only accept a local path beginning with /')
  }
  const url = new URL(path, `http://127.0.0.1:${previewPort}`)
  if (url.hostname !== '127.0.0.1' || Number(url.port || '80') !== previewPort || url.protocol !== 'http:') {
    throw new Error('browser tools can only open the current local preview')
  }
  const allowed = process.env.MIAODA_PREVIEW_PATH || '/__preview_unavailable__/'
  if (!url.pathname.startsWith(allowed)) throw new Error('browser path must stay inside this website preview')
  return url.toString()
}

function edgeExecutable() {
  const candidates = [
    process.env.MIAODA_EDGE_BIN,
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    join(process.env['PROGRAMFILES(X86)'] || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    join(process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    join(process.env.LOCALAPPDATA || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  ]
  const found = candidates.find(candidate => candidate && existsSync(candidate))
  if (!found) throw new Error('no usable browser was found on this host (chromium/chrome/edge)')
  return found
}

async function runEdge(args, signal, timeoutMs = 45000) {
  const edge = edgeExecutable()
  const profile = await mkdtemp(join(tmpdir(), 'miaoda-harness-edge-'))
  const fullArgs = [
    '--headless=new',
    '--disable-gpu',
    '--disable-extensions',
    '--no-first-run',
    '--no-default-browser-check',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    `--user-data-dir=${profile}`,
    ...args,
  ]
  try {
    return await new Promise((resolvePromise, reject) => {
      const child = spawn(edge, fullArgs, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
      const stdout = []
      const stderr = []
      let size = 0
      let settled = false
      const finish = (error, value) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        signal?.removeEventListener('abort', abort)
        if (error) reject(error)
        else resolvePromise(value)
      }
      const abort = () => {
        child.kill()
        finish(new Error('browser operation was cancelled'))
      }
      const timer = setTimeout(() => {
        child.kill()
        finish(new Error('browser operation timed out'))
      }, timeoutMs)
      signal?.addEventListener('abort', abort, { once: true })
      child.stdout.on('data', chunk => {
        size += chunk.length
        if (size <= 2_000_000) stdout.push(chunk)
      })
      child.stderr.on('data', chunk => {
        if (Buffer.concat(stderr).length < 64_000) stderr.push(chunk)
      })
      child.once('error', error => finish(error))
      child.once('exit', code => {
        if (code === 0) finish(null, Buffer.concat(stdout).toString('utf8'))
        else finish(new Error(`browser exited with code ${code}: ${Buffer.concat(stderr).toString('utf8').slice(-1000)}`))
      })
    })
  } finally {
    await rm(profile, { recursive: true, force: true }).catch(() => {})
  }
}

function stringOutput(render) {
  return {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: render ? render(value) : value }],
  }
}

// 权限模式（MIAODA_TOOLS_MODE）：security 仅源码读取；chat 只读查询及本站服务状态；plan 追加浏览器预览查看；
// full 注册全部（含 replace_file）。未识别按 full，与系统提示词和服务器端兜底共同生效。
const toolsMode = (process.env.MIAODA_TOOLS_MODE || 'full').toLowerCase()
const TOOL_ALLOW = {
  security: new Set(['list_files', 'read_file', 'search_files']),
  chat: new Set(['list_files', 'read_file', 'search_files', 'site_service']),
  plan: new Set(['list_files', 'read_file', 'search_files', 'browser_open', 'browser_screenshot', 'site_service']),
}

function register(ctx, definition) {
  const allow = TOOL_ALLOW[toolsMode]
  if (allow && !allow.has(definition.name)) return
  ctx.tools.register(definition)
}


const metadataKeys = new Set(['format', 'siteName', 'description', 'slug', 'url', 'createdAt', 'updatedAt'])
function validateMetadata(path, data) {
  if (path !== 'site.json') return
  const meta = JSON.parse(data.toString('utf8'))
  if (!meta || Array.isArray(meta) || typeof meta !== 'object' || Object.keys(meta).some(key => !metadataKeys.has(key)) || Object.values(meta).some(value => typeof value !== 'string')) {
    throw new Error('site.json stores only string metadata: format, siteName, description, slug, url, createdAt, updatedAt. Write page content directly into HTML/CSS/JS files.')
  }
  if (meta.format !== 'files') throw new Error('site.json format must be files')
}
async function saveFile(name, data) {
  const path = await sitePath(name)
  validateMetadata(name, data)
  await mkdir(dirname(path), { recursive: true })
  await sitePath(name)
  const temporary = join(dirname(path), `.hatchery-write-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  try {
    await writeFile(temporary, data, { flag: 'wx' })
    await rename(temporary, path)
  } finally { await rm(temporary, { force: true }) }
}

export function apply(ctx) {
  if (process.env.MIAODA_SITE_RUNTIME_TOKEN) register(ctx, {
    name: 'site_service',
    description: 'Manage ONLY this website’s isolated backend. Source is /app (read-only to commands); edit source with website file tools. Private persistent data/SQLite: /data, dependencies: /data/python or /data/node/node_modules. Backend must listen on SOCKET_PATH; the platform serves it at this website’s /_api/. No host shell, host ports, platform credentials or other websites are available.',
    parameters: { type: 'object', properties: {
      action: { type: 'string', enum: TOOL_ALLOW[toolsMode] ? ['status', 'logs', 'secret_names'] : ['status', 'logs', 'secret_names', 'exec', 'install', 'deploy', 'stop', 'set_secrets', 'delete_secrets'] },
      command: { type: 'string' }, manager: { type: 'string', enum: ['pip', 'npm'] }, packages: { type: 'array', items: { type: 'string' }, maxItems: 20 },
      runtime: { type: 'string', enum: ['python', 'node'] }, entry: { type: 'string' }, healthPath: { type: 'string' },
      values: { type: 'object', additionalProperties: { type: 'string' } },
      names: { type: 'array', items: { type: 'string' }, maxItems: 32 },
    }, required: ['action'], additionalProperties: false }, output: stringOutput(),
    async execute(value, execution) {
      const { action, ...args } = objectArgs(value)
      const readonly = TOOL_ALLOW[toolsMode]
      if (readonly && !['status', 'logs', 'secret_names'].includes(action)) throw new Error('This mode cannot change website services')
      if (!['status', 'logs', 'secret_names', 'exec', 'install', 'deploy', 'stop', 'set_secrets', 'delete_secrets'].includes(action)) throw new Error('Unknown website service action')
      const response = await fetch(`http://127.0.0.1:${previewPort}/api/ai/site-runtime`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.MIAODA_SITE_RUNTIME_TOKEN}` },
        body: JSON.stringify({ operation: action, args }), signal: execution?.signal,
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.error || 'Website service request failed')
      return JSON.stringify(result)
    },
  })
  register(ctx, {
    name: 'list_files',
    description: 'List the files inside the current website folder that this agent is allowed to inspect and edit.',
    parameters: { type: 'object', properties: { offset: { type: 'integer', minimum: 0 }, limit: { type: 'integer', minimum: 1, maximum: 2000 } }, additionalProperties: false },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value), offset = Number.isInteger(args.offset) ? Math.max(0, args.offset) : 0
      const limit = Number.isInteger(args.limit) ? Math.max(1, Math.min(2000, args.limit)) : 500
      const files = await listSiteFiles(workspace, '', { count: offset + limit + 1 })
      const shown = files.slice(offset, offset + limit)
      return (shown.length ? shown.join('\n') : '(no files)') + (files.length > offset + limit ? `\nMore files: call list_files with offset=${offset + limit}` : '')
    },
  })

  register(ctx, {
    name: 'read_file',
    description: 'Read a line range from one file inside the current website folder.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        encoding: { type: 'string', enum: ['utf8', 'base64'] },
        startLine: { type: 'integer', minimum: 1 },
        endLine: { type: 'integer', minimum: 1 },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value)
      const path = await sitePath(args.path)
      if (args.encoding === 'base64') return (await readFile(path)).toString('base64')
      const text = await readFile(path, 'utf8')
      const lines = text.split(/\r?\n/u)
      const start = Number.isInteger(args.startLine) ? Math.max(1, args.startLine) : 1
      const end = Number.isInteger(args.endLine) ? Math.min(lines.length, Math.max(start, args.endLine)) : Math.min(lines.length, start + 399)
      return lines.slice(start - 1, end).map((line, index) => `${start + index}: ${line}`).join('\n')
    },
  })

  register(ctx, {
    name: 'search_files',
    description: 'Search files inside the current website folder for a literal text fragment.',
    parameters: {
      type: 'object',
      properties: { query: { type: 'string' }, path: { type: 'string' } },
      required: ['query'],
      additionalProperties: false,
    },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value)
      if (typeof args.query !== 'string' || !args.query || args.query.length > 500) throw new Error('query must be 1-500 characters')
      const names = args.path === undefined ? await listSiteFiles() : [args.path]
      const matches = []
      for (const file of names) {
        const lines = (await readFile(await sitePath(file), 'utf8')).split(/\r?\n/u)
        lines.forEach((line, index) => {
          if (line.includes(args.query) && matches.length < 200) matches.push(`${file}:${index + 1}: ${line.slice(0, 500)}`)
        })
      }
      return matches.length ? matches.join('\n') : 'No matches found'
    },
  })

  register(ctx, {
    name: 'replace_file',
    description: 'Apply one exact, unique text replacement to one file inside the current website folder.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        search: { type: 'string' },
        replace: { type: 'string' },
        reason: { type: 'string' },
      },
      required: ['path', 'search', 'replace'],
      additionalProperties: false,
    },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value)
      if (typeof args.search !== 'string' || !args.search || args.search.length > 1_000_000) throw new Error('search must be 1-1000000 characters')
      if (typeof args.replace !== 'string' || args.replace.length > 4_000_000) throw new Error('replace must be at most 4000000 characters')
      const path = await sitePath(args.path)
      const before = await readFile(path, 'utf8')
      const occurrences = before.split(args.search).length - 1
      if (occurrences !== 1) throw new Error(`search text must occur exactly once; found ${occurrences}`)
      const after = before.replace(args.search, args.replace)
      await saveFile(args.path, Buffer.from(after))
      return `Updated ${basename(path)}${typeof args.reason === 'string' && args.reason.trim() ? `: ${args.reason.trim().slice(0, 240)}` : ''}`
    },
  })

  register(ctx, {
    name: 'write_file',
    description: 'Create or overwrite any file inside this website. Parent directories are created. Read existing files first. HTML, CSS, JavaScript and assets are real files; site.json holds metadata only.',
    parameters: { type: 'object', properties: { path: { type: 'string' }, content: { type: 'string' }, encoding: { type: 'string', enum: ['utf8', 'base64'] } }, required: ['path', 'content'], additionalProperties: false },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value)
      if (typeof args.content !== 'string' || args.content.length > 8_000_000) throw new Error('content must be at most 8 MB')
      if (args.encoding === 'base64' && !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(args.content)) throw new Error('invalid base64')
      await saveFile(args.path, Buffer.from(args.content, args.encoding === 'base64' ? 'base64' : 'utf8'))
      return `Saved ${args.path}`
    },
  })
  register(ctx, {
    name: 'make_directory', description: 'Create a directory and parents inside this website.',
    parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'], additionalProperties: false }, output: stringOutput(),
    async execute(value) { const args = objectArgs(value); if (args.path === 'site.json') throw new Error('site.json must be a metadata file'); await mkdir(await sitePath(args.path), { recursive: true }); return `Created ${args.path}` },
  })
  register(ctx, {
    name: 'delete_file', description: 'Delete a file or directory inside this website. To remove a nonempty directory set recursive=true. The website root cannot be removed.',
    parameters: { type: 'object', properties: { path: { type: 'string' }, recursive: { type: 'boolean' } }, required: ['path'], additionalProperties: false }, output: stringOutput(),
    async execute(value) { const args = objectArgs(value); await rm(await sitePath(args.path), { recursive: args.recursive === true }); return `Deleted ${args.path}` },
  })
  register(ctx, {
    name: 'move_file', description: 'Move or rename a file or directory within this website. Destination must not exist.',
    parameters: { type: 'object', properties: { path: { type: 'string' }, destination: { type: 'string' } }, required: ['path', 'destination'], additionalProperties: false }, output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value), from = await sitePath(args.path), to = await sitePath(args.destination)
      if (existsSync(to)) throw new Error('destination already exists')
      if (args.destination === 'site.json') validateMetadata(args.destination, await readFile(from))
      await mkdir(dirname(to), { recursive: true }); await rename(from, to)
      return `Moved ${args.path} to ${args.destination}`
    },
  })

  register(ctx, {
    name: 'browser_open',
    description: 'Open only the current local website preview in a headless browser and inspect its rendered DOM.',
    parameters: {
      type: 'object',
      properties: { path: { type: 'string' } },
      additionalProperties: false,
    },
    output: stringOutput(),
    async execute(value, exec) {
      const args = objectArgs(value)
      const url = localUrl(args.path)
      const dom = await runEdge(['--dump-dom', url], exec.signal)
      return `Opened ${url}\n${dom.slice(0, 30000)}`
    },
  })

  register(ctx, {
    name: 'browser_screenshot',
    description: 'Capture and visually inspect only the current local website preview.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        width: { type: 'integer', minimum: 320, maximum: 1920 },
        height: { type: 'integer', minimum: 240, maximum: 3000 },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          attachment: {
            type: 'object',
            properties: {
              attachmentId: { type: 'string' },
              mediaType: { type: 'string' },
              bytes: { type: 'integer' },
              width: { type: 'integer' },
              height: { type: 'integer' },
              name: { type: 'string' },
            },
            required: ['attachmentId', 'mediaType', 'bytes', 'width', 'height'],
            additionalProperties: false,
          },
        },
        required: ['url', 'attachment'],
        additionalProperties: false,
      },
      render: (_args, value) => [
        { type: 'text', text: `Screenshot captured for ${value.url}` },
        { type: 'image', attachment: value.attachment },
      ],
    },
    async execute(value, exec) {
      const args = objectArgs(value)
      const url = localUrl(args.path)
      const width = Number.isInteger(args.width) ? args.width : 1440
      const height = Number.isInteger(args.height) ? args.height : 1000
      const directory = await mkdtemp(join(tmpdir(), 'miaoda-harness-shot-'))
      const screenshot = join(directory, 'preview.png')
      try {
        await runEdge([`--window-size=${width},${height}`, '--hide-scrollbars', `--screenshot=${screenshot}`, url], exec.signal)
        const data = await readFile(screenshot)
        const attachments = ctx.get('attachments')
        if (!attachments) throw new Error('screenshot attachment storage is unavailable')
        const ref = await attachments.saveImage({ data, mediaType: 'image/png', name: 'preview.png' })
        return {
          url,
          attachment: {
            attachmentId: ref.attachmentId,
            mediaType: ref.mediaType,
            bytes: ref.bytes,
            width: ref.width,
            height: ref.height,
            name: ref.name || 'preview.png',
          },
        }
      } finally {
        await rm(directory, { recursive: true, force: true }).catch(() => {})
      }
    },
  })
}

