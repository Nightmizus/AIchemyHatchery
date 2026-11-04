// DSH v3 persists complete assistant attempts. Its WebUI also subscribes to
// agent/assistant-stream for transient progress; expose that same source to our
// JSON-RPC adapter without enabling a shell or publishing raw reasoning/tool args.
export const inject = ['agents'];
export function apply(ctx) {
  ctx.on('agent/assistant-stream', ({agent, frame}) => {
    if (!agent?.session?.id || !frame || !['start', 'chunk', 'end'].includes(frame.type)) return;
    const value = {...frame};
    if (frame.type === 'chunk') {
      const chunk = frame.chunk || {};
      value.chunk = {type: String(chunk.type || '')};
      if (chunk.type === 'text-delta' && typeof chunk.text === 'string') value.chunk.text = chunk.text.slice(0, 32768);
    }
    process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'hatchery.assistant',
      params:{sessionId:String(agent.session.id),frame:value}}) + '\n');
  });
}
