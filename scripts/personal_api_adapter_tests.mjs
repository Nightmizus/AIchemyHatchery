import test from 'node:test'
import assert from 'node:assert/strict'
import { Readable } from 'node:stream'
import { apply } from '../deepseek_harness/llm-openai.mjs'

test('generic adapter sends selected model/key and converts Unicode/tool-call streams', async () => {
  const previous = {...process.env}
  try {
    Object.assign(process.env, {HATCHERY_PERSONAL_API_BASE: 'https://api.example.com/v1', HATCHERY_PERSONAL_API_KEY: 'user-key', HATCHERY_PERSONAL_API_MODEL: 'user-model'})
    let adapter, sent
    const frame = value => `data: ${JSON.stringify(value)}\n\n`
    const bytes = Buffer.from(frame({choices: [{delta: {content: '你好'}}]}) +
      frame({choices: [{delta: {tool_calls: [{index: 0, id: 'call-1', function: {name: 'read_file', arguments: '{"path":'}}]}}]}) +
      frame({choices: [{delta: {tool_calls: [{index: 0, function: {arguments: '"index.html"}'}}]}, finish_reason: 'tool_calls'}], usage: {prompt_tokens: 10, completion_tokens: 20}}) + 'data: [DONE]\n\n')
    const request = async (url, options) => {
      sent = {url, ...options}
      const chunks = Array.from({length: bytes.length}, (_, i) => bytes.subarray(i, i + 1))
      const response = Readable.from(chunks)
      response.statusCode = 200; response.headers = {'content-type': 'text/event-stream'}
      return response
    }
    apply({llm: {registerAdapter(routes, value) { assert.deepEqual(routes, ['personal-openai']); adapter = value }}}, {request})
    const chunks = []
    for await (const chunk of adapter.stream({model: 'user-model', messages: [{role: 'user', content: [{type: 'text', text: 'hello'}]}], tools: [{name: 'read_file', description: 'read', parameters: {type: 'object'}}]})) chunks.push(chunk)
    assert.equal(sent.url, 'https://api.example.com/v1/chat/completions')
    assert.equal(sent.headers.authorization, 'Bearer user-key')
    const payload = JSON.parse(sent.body)
    assert.equal(payload.model, 'user-model')
    assert.equal(payload.thinking, undefined); assert.equal(payload.tool_stream, undefined)
    assert.equal(chunks.filter(c => c.type === 'text-delta').map(c => c.text).join(''), '你好')
    assert.equal(chunks.find(c => c.type === 'block-end' && c.block.type === 'tool-call').block.arguments, '{"path":"index.html"}')
    assert.equal(chunks.at(-1).reason.kind, 'tool-calls')
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in previous)) delete process.env[key]
    Object.assign(process.env, previous)
  }
})

test('gateway errors do not echo credential-bearing bodies', async () => {
  let adapter
  apply({llm: {registerAdapter(_routes, value) {adapter = value}}}, {request: async () => ({statusCode: 401, destroy() {}, headers: {}, body: 'secret-key'})})
  await assert.rejects(async () => {
    for await (const _chunk of adapter.stream({model: 'model', messages: []})) { /* consume */ }
  }, error => error.message.includes('HTTP 401') && !error.message.includes('secret-key'))
})
