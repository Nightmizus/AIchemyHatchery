// Account-owned Chat Completions adapter. Connection settings exist only in this child process.
import https from 'node:https'
import net from 'node:net'
import { serializeMessages, translate } from './llm-glm.mjs'

export const name = 'hatchery-llm-openai'
export const inject = ['llm']

export function pinnedRequest(url, options) {
  const target = new URL(url)
  const addresses = JSON.parse(process.env.HATCHERY_PERSONAL_API_IPS || '[]')
  if (target.protocol !== 'https:' || (target.port && target.port !== '443') || target.username || target.password || !addresses.length || addresses.some(ip => !net.isIP(ip))) {
    throw new Error('个人 API 连接配置无效')
  }
  return new Promise((resolve, reject) => {
    const request = https.request(target, {
      method: 'POST', headers: options.headers, signal: options.signal,
      // Addresses are resolved and checked by the server immediately before launch.
      // Do not perform another DNS lookup or follow redirects with the user's key.
      lookup: (_host, lookupOptions, done) => lookupOptions.all
        ? done(null, addresses.map(address => ({address, family: net.isIP(address)})))
        : done(null, addresses[0], net.isIP(addresses[0])),
    }, response => resolve(response))
    request.setTimeout(120000, () => request.destroy(new Error('个人 API 响应超时')))
    request.on('error', () => reject(new Error('无法连接个人 API，请检查地址和网络')))
    request.end(options.body)
  })
}

export function apply(ctx, pluginConfig = {}) {
  const modelInfo = (provider, model) => ({provider, id: model, name: model, contextWindow: 128000, defaultMaxTokens: 32768})
  async function* stream(options) {
    const model = options.model || process.env.HATCHERY_PERSONAL_API_MODEL
    const messages = options.system === undefined ? [] : [{role: 'system', content: options.system}]
    messages.push(...serializeMessages(options.messages))
    const tools = options.tools?.map(tool => ({type: 'function', function: {name: tool.name, description: tool.description, parameters: tool.parameters}}))
    const response = await (pluginConfig.request || pinnedRequest)(`${process.env.HATCHERY_PERSONAL_API_BASE}/chat/completions`, {
      headers: {authorization: `Bearer ${process.env.HATCHERY_PERSONAL_API_KEY}`, 'content-type': 'application/json', accept: 'text/event-stream'},
      signal: options.signal,
      body: JSON.stringify({model, messages, stream: true,
        ...(tools?.length ? {tools} : {}),
        ...(options.maxTokens !== undefined ? {max_tokens: options.maxTokens} : {}),
        ...(options.temperature !== undefined ? {temperature: options.temperature} : {}),
        ...(options.stop !== undefined ? {stop: options.stop} : {}),
      }),
    })
    if (response.statusCode < 200 || response.statusCode >= 300) {
      response.destroy()
      // Never echo a gateway's error body: it may contain credentials.
      throw new Error(`个人 API 返回 HTTP ${response.statusCode}，请检查 Key、模型和接口地址`)
    }
    if (!String(response.headers['content-type'] || '').includes('text/event-stream')) {
      response.destroy()
      throw new Error('个人 API 未返回流式数据，请使用兼容 Chat Completions 的接口')
    }
    // Decode across chunk boundaries so split Chinese UTF-8 characters remain intact.
    response.setEncoding('utf8')
    yield* translate(response)
  }
  const adapter = {
    providerInfo: provider => ({id: provider, name: '我的 API'}),
    providerRetryPolicy: () => undefined,
    listModels: async () => [{id: process.env.HATCHERY_PERSONAL_API_MODEL, name: process.env.HATCHERY_PERSONAL_API_MODEL}],
    resolveModel: async (provider, model) => modelInfo(provider, model),
    prepareCall: async (provider, model) => ({model: modelInfo(provider, model), stream}),
    stream,
  }
  ctx.llm.registerAdapter(['personal-openai'], adapter)
}
