"""Run the installed native runtime against a deterministic local adapter, never an API."""
import collections
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import deepseek_harness_adapter as adapter

PROVIDER = r'''
export const inject = ['llm'];
export function apply(ctx) {
  const info = (provider, id) => ({provider,id,name:id,contextWindow:128000,defaultMaxTokens:2048});
  let turn = 0;
  async function* stream() {
    turn++;
    const block = turn === 1 ? {type:'tool-call',id:'fixture-call',name:'list_files',arguments:'{}'} :
      {type:'text',text:'{"summary":"## Native fixture\\n\\nCompleted"}'};
    yield {type:'block-start',index:0,blockType:block.type};
    await new Promise(resolve => setTimeout(resolve, 30));
    if (turn > 1) {
      yield {type:'text-delta',index:0,text:block.text.slice(0, 28)};
      await new Promise(resolve => setTimeout(resolve, 250));
      yield {type:'text-delta',index:0,text:block.text.slice(28)};
    }
    yield {type:'block-end',index:0,block};
    yield {type:'finish',reason:{kind:turn === 1 ? 'tool-calls' : 'stop'}};
  }
  ctx.llm.registerAdapter(['history-fixture'], {
    providerInfo: id => ({id,name:id}), providerRetryPolicy:()=>undefined,
    listModels:async()=>[{id:'fixture',name:'fixture'}], resolveModel:async(p,m)=>info(p,m),
    prepareCall:async(p,m)=>({model:info(p,m),stream}), stream,
  });
}
'''


class NativeProtocolTests(unittest.TestCase):
    def test_native_events_complete_tool_steps_and_close_all_pipes(self):
        binary = adapter.resolve_harness_binary(ROOT)
        if binary is None:
            self.skipTest('Native runtime not installed')
        with tempfile.TemporaryDirectory(prefix='hatchery-protocol-') as folder:
            root = Path(folder)
            (root / 'site').mkdir(); (root / 'site/index.html').write_text('<h1>fixture</h1>')
            (root / 'provider.mjs').write_text(PROVIDER)
            overlay = root / 'fixture.yml'
            overlay.write_text('- insert:\n    - id: fixture-provider\n      name: ' + json.dumps(str(root / 'provider.mjs')) + '\n')
            env = adapter._child_env('')
            env.update({'DSH_HOME': str(root / 'home'), 'DSH_TELEMETRY_DISABLED': '1',
                        'MIAODA_AI_WORKSPACE': str(root / 'site'), 'MIAODA_TOOLS_MODE': 'chat'})
            events, progress, methods = [], [], collections.Counter()
            first_stream = completed = None
            projector = adapter._EventProjector(progress.append)
            with adapter._JsonRpcRuntime([str(binary), '--profile', 'sdk-minimal', '--patch',
                    str(ROOT / 'deepseek_harness/site-agent.patch.yml'), '--patch', str(overlay)], root, env, 15, ()) as runtime:
                process = runtime.process
                runtime.request('initialize', {'cwd': str(root), 'provider': 'history-fixture', 'model': 'fixture'})
                runtime.request('session/prompt', {'sessionId': 'native-fixture', 'contentBlocks': [{'type': 'text', 'text': 'Inspect fixture'}]})
                deadline = time.monotonic() + 20
                while True:
                    notice = runtime.next_notification(deadline)
                    methods[notice.get('method')] += 1
                    params = notice.get('params', {})
                    if params.get('sessionId') != 'native-fixture': continue
                    if notice.get('method') == 'hatchery.assistant':
                        projector.accept_stream(params['frame'])
                        if any(p['kind']=='message' for p in progress) and first_stream is None:
                            first_stream = time.monotonic()
                    if notice.get('method') == 'session.event':
                        event = params['event']; events.append(event); projector.accept(event)
                    if notice.get('method') == 'session.status' and params.get('status') == 'idle' and adapter._finish_reason(events):
                        completed = time.monotonic(); break
            self.assertEqual(adapter._finish_reason(events), 'completed')
            self.assertIn('Native fixture', adapter._final_response(events))
            self.assertIsNotNone(first_stream, 'Native WebUI stream did not reach the adapter')
            self.assertGreater(completed - first_stream, 0.15, 'Text appeared only after completion')
            self.assertFalse(any(p['detail'].startswith('{') for p in progress if p['kind']=='message'))
            self.assertTrue(any(p['kind']=='message' and 'Native fixture' in p['detail'] for p in progress))
            self.assertEqual(projector.trace, [{'tool': 'list_files', 'status': 'done', 'detail': '读取获准的网站源码清单'}])
            self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
            print(json.dumps({'nativeNotifications': dict(methods), 'realModelCalls': 0}))


if __name__ == '__main__': unittest.main()
