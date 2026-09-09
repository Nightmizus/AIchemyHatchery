// llm-kimi.mjs — minimal Kimi (Moonshot) LLM adapter for the DSH runtime.
// Text-only, OpenAI-compatible chat-completions over SSE, self-contained
// (node builtins only; the adapter is registered as a duck-typed LlmAdapter,
// no @deepseek-ai imports). Mount via site-agent.patch.yml next to site-tools.mjs.
//
// Configuration (all optional, patch-yml `config:` or environment):
//   apiKeyEnv        env var name of the key (default KIMI_API_KEY)
//   baseURL          endpoint base; /chat/completions is appended
//                    (default https://api.kimi.com/coding/v1 for sk-kimi- keys,
//                     otherwise https://api.moonshot.cn/v1)
//   model            advisory default model id (default k3)
//   reasoningEffort  default effort sent as reasoning_effort (default high)

export const name = 'miaoda-llm-kimi'
export const inject = ['llm']

const PROVIDER = 'kimi'
const CODING_BASE_URL = 'https://api.kimi.com/coding/v1'
const PUBLIC_BASE_URL = 'https://api.moonshot.cn/v1'
const DEFAULT_CONTEXT_WINDOW = 262144
const DEFAULT_MAX_TOKENS = 32768

function llmError(message, code, extra = {}) {
  const error = new Error(message)
  error.code = code
  error.failure = Object.freeze({ message, code, ...extra })
  if (extra.status !== undefined) error.status = extra.status
  return error
}

function trimKey(raw, ref) {
  const value = String(raw ?? '').trim()
  if (!value) throw llmError(`llm-kimi: no API key; export ${ref} in the server environment`, 'MISSING_CREDENTIAL')
  // reject characters no HTTP header can carry without leaking the secret
  // eslint-disable-next-line no-control-regex
  if (/[^\x21-\x7e]/.test(value)) throw llmError(`llm-kimi: the API key from ${ref} contains characters no HTTP header can carry`, 'INVALID_CREDENTIAL')
  return value
}

function flattenText(blocks) {
  return blocks.filter((block) => block.type === 'text').map((block) => block.text).join('')
}

function assertTextOnly(blocks) {
  if (blocks.some((block) => block.type === 'image')) {
    throw llmError('llm-kimi: this adapter is text-only; remove image attachments', 'UNSUPPORTED_CONTENT')
  }
}

function serializeAssistant(message) {
  const text = flattenText(message.content)
  const reasoning = message.content.filter((block) => block.type === 'reasoning').map((block) => block.text).join('')
  const toolCalls = message.content
    .filter((block) => block.type === 'tool-call')
    .map((block) => ({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } }))
  return {
    role: 'assistant',
    content: text,
    ...(reasoning.length > 0 ? { reasoning_content: reasoning } : {}),
    ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
  }
}

// tool-result blocks become standalone {role:'tool'} messages; any remaining
// user text stays a string user message, order preserved.
function serializeMessages(messages) {
  const wire = []
  for (const message of messages) {
    assertTextOnly(message.content)
    if (message.role === 'system') {
      wire.push({ role: 'system', content: flattenText(message.content) })
      continue
    }
    if (message.role === 'assistant') {
      wire.push(serializeAssistant(message))
      continue
    }
    const toolResults = message.content.filter((block) => block.type === 'tool-result')
    const text = flattenText(message.content)
    if (text.length > 0 || toolResults.length === 0) wire.push({ role: 'user', content: text })
    for (const result of toolResults) {
      wire.push({ role: 'tool', tool_call_id: result.toolCallId, content: flattenText(result.content) || '(no output)' })
    }
  }
  return wire
}

function serializeRequest(options, defaults) {
  const messages = []
  if (options.system !== undefined) messages.push({ role: 'system', content: options.system })
  messages.push(...serializeMessages(options.messages))
  const tools = options.tools?.map((tool) => ({
    type: 'function',
    function: { name: tool.name, description: tool.description, parameters: tool.parameters },
  }))
  const effort = options.reasoningEffort === undefined ? defaults.reasoningEffort : options.reasoningEffort
  const sendEffort = options.purpose === 'session-title' || effort === undefined || effort === 'off' ? undefined : effort
  return {
    model: options.model,
    messages,
    stream: true,
    stream_options: { include_usage: true },
    ...(sendEffort !== undefined ? { reasoning_effort: sendEffort } : {}),
    ...(tools !== undefined && tools.length > 0 ? { tools } : {}),
    ...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
    ...(options.maxTokens !== undefined ? { max_tokens: options.maxTokens } : {}),
    ...(options.stop !== undefined ? { stop: options.stop } : {}),
  }
}

// Minimal SSE data-payload parser: yields each event's data string and returns
// on the [DONE] sentinel; a truncated stream throws STREAM_CLOSED. OpenAI-style
// servers send single-line JSON data frames; comment/heartbeat lines are ignored.
async function* ssePayloads(body) {
  let buffer = ''
  for await (const chunk of body) {
    buffer += typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    let index
    while ((index = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).replace(/^ /, '')
      if (payload === '[DONE]') return
      if (payload.length > 0) yield payload
    }
  }
  throw llmError('llm-kimi: SSE stream ended without [DONE]', 'STREAM_CLOSED')
}

function mapFinishReason(reason) {
  switch (reason) {
    case 'stop': return { kind: 'stop' }
    case 'tool_calls': return { kind: 'tool-calls' }
    case 'length': return { kind: 'max-tokens' }
    default: return { kind: 'error', failure: { message: `model stopped: ${reason}`, code: String(reason).toUpperCase() } }
  }
}

function mapUsage(usage) {
  const cacheRead = usage.prompt_tokens_details?.cached_tokens ?? usage.prompt_cache_hit_tokens
  const reasoning = usage.completion_tokens_details?.reasoning_tokens
  return {
    inputTokens: (usage.prompt_tokens ?? 0) - (cacheRead ?? 0),
    outputTokens: usage.completion_tokens ?? 0,
    ...(cacheRead !== undefined ? { cacheReadTokens: cacheRead } : {}),
    ...(reasoning !== undefined ? { reasoningTokens: reasoning } : {}),
  }
}

// Translate OpenAI-compatible SSE chunks into harness StreamChunks.
// block-end/usage/finish are deferred until the [DONE] sentinel.
async function* translate(body) {
  let nextIndex = 0
  let textBlock
  let reasoningBlock
  const toolBlocks = new Map()
  const order = []
  let pendingFinish
  let pendingUsage
  const open = (kind) => {
    const block = { index: nextIndex++, kind, text: '' }
    order.push(block)
    return block
  }
  for await (const payload of ssePayloads(body)) {
    let chunk
    try {
      chunk = JSON.parse(payload)
    } catch {
      throw llmError(`llm-kimi: malformed SSE payload: ${payload.slice(0, 120)}`, 'MALFORMED_RESPONSE')
    }
    for (const choice of chunk.choices ?? []) {
      const delta = choice.delta
      const reasoning = delta?.reasoning_content
      if (typeof reasoning === 'string' && reasoning.length > 0) {
        if (!reasoningBlock) {
          reasoningBlock = open('reasoning')
          yield { type: 'block-start', index: reasoningBlock.index, blockType: 'reasoning' }
        }
        reasoningBlock.text += reasoning
        yield { type: 'reasoning-delta', index: reasoningBlock.index, text: reasoning }
      }
      const content = delta?.content
      if (typeof content === 'string' && content.length > 0) {
        if (!textBlock) {
          textBlock = open('text')
          yield { type: 'block-start', index: textBlock.index, blockType: 'text' }
        }
        textBlock.text += content
        yield { type: 'text-delta', index: textBlock.index, text: content }
      }
      for (const call of delta?.tool_calls ?? []) {
        let block = toolBlocks.get(call.index)
        if (!block) {
          block = open('tool-call')
          toolBlocks.set(call.index, block)
          yield { type: 'block-start', index: block.index, blockType: 'tool-call' }
        }
        if (call.id !== undefined) block.callId = call.id
        if (call.function?.name !== undefined) block.name = call.function.name
        const fragment = call.function?.arguments ?? ''
        block.text += fragment
        yield {
          type: 'tool-call-delta',
          index: block.index,
          id: block.callId ?? '',
          ...(block.name !== undefined ? { name: block.name } : {}),
          argumentsDelta: fragment,
        }
      }
      if (typeof choice.finish_reason === 'string') pendingFinish = mapFinishReason(choice.finish_reason)
    }
    if (chunk.usage) pendingUsage = mapUsage(chunk.usage)
  }
  for (const block of order) {
    yield {
      type: 'block-end',
      index: block.index,
      block:
        block.kind === 'tool-call'
          ? { type: 'tool-call', id: block.callId ?? '', name: block.name ?? '', arguments: block.text }
          : { type: block.kind, text: block.text },
    }
  }
  if (pendingUsage) yield { type: 'usage', usage: pendingUsage }
  const reason = pendingFinish ?? { kind: 'stop' }
  yield {
    type: 'finish',
    reason:
      reason.kind === 'stop' && order.length === 0
        ? { kind: 'error', failure: { message: 'model returned a completed response with no content', code: 'EMPTY_RESPONSE' } }
        : reason,
  }
}

function httpErrorCode(status) {
  if (status === 401 || status === 403) return 'AUTH'
  if (status === 429) return 'RATE_LIMIT'
  if (status === 400) return 'INVALID_REQUEST'
  if (status >= 500) return 'SERVER'
  return `HTTP_${status}`
}

async function* streamCall(options, connection, resolveApiKey, fetchImpl) {
  const apiKey = await resolveApiKey(connection)
  const body = serializeRequest(options, connection.defaults)
  let response
  try {
    response = await fetchImpl(`${connection.baseURL}/chat/completions`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${apiKey}`,
        'content-type': 'application/json',
        'user-agent': 'deepseek-harness miaoda-llm-kimi',
        accept: 'text/event-stream',
      },
      body: JSON.stringify(body),
      ...(options.signal !== undefined ? { signal: options.signal } : {}),
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw llmError(`llm-kimi: request to ${connection.baseURL} failed: ${error?.message || error}`, 'TRANSPORT', { cause: error })
  }
  if (!response.ok) {
    const text = (await response.text().catch(() => '')).slice(0, 2000)
    const code = httpErrorCode(response.status)
    const retryAfter = Number.parseFloat(response.headers.get('retry-after') ?? '')
    throw llmError(`llm-kimi: Kimi API error (HTTP ${response.status})${text ? `: ${text}` : ''}`, code, {
      status: response.status,
      ...(Number.isFinite(retryAfter) && retryAfter > 0 ? { providerRetryAfterMs: retryAfter * 1000 } : {}),
    })
  }
  if (!response.body) throw llmError('llm-kimi: Kimi API returned no response body', 'EMPTY_RESPONSE')
  const contentType = String(response.headers.get('content-type') ?? '').toLowerCase()
  if (!contentType.includes('text/event-stream')) {
    // HTTP 200 但正文不是 SSE（限流页、WAF 挑战页、透明代理错误页）：直接 JSON.parse 会得到
    // “Unexpected token '<'” 这种看不懂的报错，这里换成可读提示。
    const text = (await response.text().catch(() => '')).slice(0, 300)
    throw llmError(`llm-kimi: 模型服务没有返回流式数据（content-type: ${contentType || '未知'}），可能是限流、网关拦截或 API 地址错误。响应片段：${text}`, 'MALFORMED_RESPONSE')
  }
  yield* translate(response.body)
}

export function apply(ctx, pluginConfig = {}) {
  const apiKeyEnv = pluginConfig.apiKeyEnv || 'KIMI_API_KEY'
  const resolveConnection = () => {
    const keyPreview = String(process.env[apiKeyEnv] ?? '').trim()
    const isCodeKey = keyPreview.startsWith('sk-kimi-')
    return {
      baseURL: (pluginConfig.baseURL || process.env.KIMI_BASE_URL || (isCodeKey ? CODING_BASE_URL : PUBLIC_BASE_URL)).replace(/\/+$/, ''),
      defaults: {
        reasoningEffort: pluginConfig.reasoningEffort || process.env.KIMI_REASONING_EFFORT || 'high',
      },
    }
  }
  const resolveApiKey = async (connection) => {
    const credentials = ctx.get?.('credentials')
    if (credentials !== undefined) {
      try {
        const hit = await credentials.resolve(apiKeyEnv)
        if (hit !== undefined && String(hit.value ?? '').trim()) return trimKey(hit.value, apiKeyEnv)
      } catch { /* fall through to ambient env */ }
    }
    return trimKey(process.env[apiKeyEnv], apiKeyEnv)
  }

  const fetchImpl = pluginConfig.fetch ?? globalThis.fetch
  const reasoningInfo = () => ({
    efforts: [
      { id: 'off', name: '关闭' },
      { id: 'low', name: '低' },
      { id: 'high', name: '高' },
    ],
    defaultEffort: 'high',
  })
  const modelInfo = (provider, model) => ({
    provider,
    id: model,
    name: model,
    contextWindow: DEFAULT_CONTEXT_WINDOW,
    defaultMaxTokens: DEFAULT_MAX_TOKENS,
    reasoning: reasoningInfo(),
  })
  const adapter = {
    providerInfo: (provider) => ({ id: provider, name: 'Kimi' }),
    providerRetryPolicy: () => undefined,
    listModels: () =>
      Promise.resolve([
        { id: 'k3', name: 'Kimi K3', contextWindow: DEFAULT_CONTEXT_WINDOW, maxTokens: DEFAULT_MAX_TOKENS },
        { id: 'kimi-k2.6', name: 'Kimi K2.6', contextWindow: DEFAULT_CONTEXT_WINDOW, maxTokens: DEFAULT_MAX_TOKENS },
      ]),
    resolveModel: (provider, model) => Promise.resolve(modelInfo(provider, model)),
    prepareCall(provider, model) {
      return Promise.resolve({
        model: modelInfo(provider, model),
        stream: (options) => streamCall(options, resolveConnection(), resolveApiKey, fetchImpl),
      })
    },
    stream(options) {
      return streamCall(options, resolveConnection(), resolveApiKey, fetchImpl)
    },
  }
  ctx.llm.registerAdapter([PROVIDER], adapter)
}
