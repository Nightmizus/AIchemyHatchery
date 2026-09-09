import { spawn } from 'node:child_process'
import { mkdtemp, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
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
function sitePath(value) {
  if (typeof value !== 'string' || !value || value.includes('\0') || value.includes('\\')) {
    throw new Error('path must be a relative POSIX path inside the website folder')
  }
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value)) {
    throw new Error('absolute paths are outside the website folder')
  }
  const target = resolve(workspace, value)
  if (target !== workspace && !target.startsWith(`${workspace}${sep}`)) {
    throw new Error('path is outside the website folder')
  }
  return target
}

async function listSiteFiles(directory = workspace, prefix = '', limit = { count: 200 }) {
  if (limit.count <= 0) return []
  const entries = await readdir(directory, { withFileTypes: true }).catch(() => [])
  const files = []
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) {
      files.push(...await listSiteFiles(join(directory, entry.name), relative, limit))
    } else if (entry.isFile()) {
      files.push(relative)
      limit.count -= 1
    }
    if (limit.count <= 0) break
  }
  return files
}

function localUrl(value) {
  const path = typeof value === 'string' && value.trim() ? value.trim() : '/'
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || path.includes('\0')) {
    throw new Error('browser tools only accept a local path beginning with /')
  }
  const url = new URL(path, `http://127.0.0.1:${previewPort}`)
  if (url.hostname !== '127.0.0.1' || Number(url.port || '80') !== previewPort || url.protocol !== 'http:') {
    throw new Error('browser tools can only open the current local preview')
  }
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

function register(ctx, definition) {
  ctx.tools.register(definition)
}

export function apply(ctx) {
  register(ctx, {
    name: 'list_files',
    description: 'List the files inside the current website folder that this agent is allowed to inspect and edit.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: stringOutput(),
    async execute() {
      const files = await listSiteFiles()
      return files.length ? files.join('\n') : '(empty website folder)'
    },
  })

  register(ctx, {
    name: 'read_file',
    description: 'Read a line range from one file inside the current website folder.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        startLine: { type: 'integer', minimum: 1 },
        endLine: { type: 'integer', minimum: 1 },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: stringOutput(),
    async execute(value) {
      const args = objectArgs(value)
      const path = sitePath(args.path)
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
        const lines = (await readFile(sitePath(file), 'utf8')).split(/\r?\n/u)
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
      if (typeof args.search !== 'string' || !args.search || args.search.length > 60000) throw new Error('search must be 1-60000 characters')
      if (typeof args.replace !== 'string' || args.replace.length > 60000) throw new Error('replace must be at most 60000 characters')
      const path = sitePath(args.path)
      const before = await readFile(path, 'utf8')
      const occurrences = before.split(args.search).length - 1
      if (occurrences !== 1) throw new Error(`search text must occur exactly once; found ${occurrences}`)
      const after = before.replace(args.search, args.replace)
      const info = await stat(path)
      const temporary = join(dirname(path), `.${basename(path)}.miaoda-${process.pid}-${Date.now()}.tmp`)
      await writeFile(temporary, after, { encoding: 'utf8', mode: info.mode })
      await rename(temporary, path)
      return `Updated ${basename(path)}${typeof args.reason === 'string' && args.reason.trim() ? `: ${args.reason.trim().slice(0, 240)}` : ''}`
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

