"""Native Harness web integration checks. Search uses a loopback fixture, never a model.

Run: .venv/bin/python scripts/ai_web_tools_tests.py
Optional public-page fetch: add --live-fetch (still no real model/search requests).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import deepseek_harness_adapter as dsh

LIVE_FETCH = "--live-fetch" in sys.argv
if LIVE_FETCH:
    sys.argv.remove("--live-fetch")

PROBE = r'''
import assert from 'node:assert/strict';
export const inject = ['tools', 'web', 'systemPrompt'];
export function apply(ctx) {
  setTimeout(async () => {
    try {
      await ctx.get('loader').await();
      const mode = process.env.MIAODA_TOOLS_MODE;
      const names = [...ctx.tools.view().visible.keys()].sort();
      const expected = ['list_files', 'read_file', 'search_files', 'web_search', 'web_fetch'];
      if (mode !== 'chat') expected.push('browser_open', 'browser_screenshot');
      if (mode === 'full') expected.push('write_file', 'replace_file', 'make_directory', 'move_file', 'delete_file');
      assert.deepEqual(names, expected.sort());
      let sequence = 0;
      const call = (name, args) => ctx.tools.execute({
        name, arguments: args, callId: `web-check-${++sequence}`, signal: AbortSignal.timeout(35000),
      });
      const search = await call('web_search', {queries: ['hatchery fixture one', 'hatchery fixture two']});
      assert.equal(search.isError, false, JSON.stringify(search));
      assert.equal(search.value.sources.length, 2);
      assert.ok(search.value.sources.every(source => source.snippet === 'fixture excerpt'));
      assert.match(search.content[0].text, /untrusted data/);
      const missing = await call('web_search', {queries: ['missing-native-results']});
      assert.equal(missing.isError, true);
      assert.match(JSON.stringify(missing), /no web_search_tool_result/);
      const invalid = await call('web_search', {queries: []});
      assert.equal(invalid.isError, true);
      for (const url of ['file:///etc/passwd', 'http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/']) {
        const result = await call('web_fetch', {url});
        assert.equal(result.isError, true, url);
      }
      let liveFetch;
      if (process.env.HATCHERY_WEB_TEST_LIVE_FETCH === '1') {
        const result = await call('web_fetch', {url: 'https://example.com/'});
        assert.equal(result.isError, false, JSON.stringify(result));
        assert.equal(result.value.statusCode, 200);
        assert.match(result.value.body.content, /Example Domain/);
        assert.match(result.content[0].text, /untrusted data/);
        liveFetch = {url: result.value.url, status: result.value.statusCode};
      }
      console.log('HATCHERY_NATIVE_WEB_CHECK=' + JSON.stringify({mode, names, liveFetch}));
      process.exit(0);
    } catch (error) {
      console.error(error.stack || String(error));
      process.exit(1);
    }
  }, 0);
}
'''


class WebConfigTests(unittest.TestCase):
    def test_existing_gateway_and_explicit_search_override(self):
        with patch.dict(os.environ, {"HATCHERY_DEEPSEEK_BASE_URL": "https://gateway.example/v1/",
                                     "DEEPSEEK_MODEL": "gateway-model", "DEEPSEEK_API_KEY": "fixture-key"}, clear=True):
            self.assertEqual(dsh._web_search_env(), {
                "DEEPSEEK_SEARCH_BASE_URL": "https://gateway.example/v1",
                "HATCHERY_DEEPSEEK_SEARCH_MODEL": "gateway-model",
                "HATCHERY_DEEPSEEK_SEARCH_API_KEY": "fixture-key",
            })
            with patch.dict(os.environ, {"HATCHERY_DEEPSEEK_SEARCH_BASE_URL": "https://search.example/anthropic/v1/",
                                         "HATCHERY_DEEPSEEK_SEARCH_MODEL": "search-model",
                                         "HATCHERY_DEEPSEEK_SEARCH_API_KEY": "search-fixture-key"}):
                search = dsh._web_search_env()
                self.assertEqual(search["DEEPSEEK_SEARCH_BASE_URL"], "https://search.example/anthropic/v1")
                self.assertEqual(search["HATCHERY_DEEPSEEK_SEARCH_MODEL"], "search-model")
                self.assertEqual(search["HATCHERY_DEEPSEEK_SEARCH_API_KEY"], "search-fixture-key")

    def test_official_search_uses_anthropic_base(self):
        for base in ("", "https://api.deepseek.com", "https://api.deepseek.com/v1/"):
            with self.subTest(base=base), patch.dict(os.environ, {"HATCHERY_DEEPSEEK_BASE_URL": base}, clear=True):
                self.assertEqual(dsh._web_search_env()["DEEPSEEK_SEARCH_BASE_URL"], "https://api.deepseek.com/anthropic/v1")

    def test_only_chat_and_search_credentials_are_forwarded_and_redacted(self):
        with patch.dict(os.environ, {"KIMI_API_KEY": "chat-secret", "DEEPSEEK_API_KEY": "unused-search-secret",
                                     "HATCHERY_DEEPSEEK_SEARCH_API_KEY": "search-secret",
                                     "NEON_DATABASE_URL": "database-secret", "SSO_SECRET": "sso-secret"}, clear=True):
            env = dsh._child_env("KIMI_API_KEY")
            env.update(dsh._web_search_env())
            self.assertEqual(set(env), {"KIMI_API_KEY", "HATCHERY_DEEPSEEK_SEARCH_API_KEY",
                                        "DEEPSEEK_SEARCH_BASE_URL", "HATCHERY_DEEPSEEK_SEARCH_MODEL"})
            self.assertEqual(dsh._redact("chat-secret search-secret", (env['KIMI_API_KEY'], env['HATCHERY_DEEPSEEK_SEARCH_API_KEY'])), "*** ***")

    def test_progress_shows_query_url_and_failure(self):
        events = []
        projector = dsh._EventProjector(events.append)
        for name, args, detail in (("web_search", {"queries": ["网页设计", "CSS grid"]}, "网页设计；CSS grid"),
                                   ("web_fetch", {"url": "https://example.com/"}, "https://example.com/")):
            projector.accept({"type": "tool/call", "data": {"callId": name, "name": name, "arguments": json.dumps(args)}})
            self.assertEqual(events[-1]['detail'], detail)
            self.assertEqual(events[-1]['label'], dsh.TOOL_LABELS[name])
            projector.accept({"type": "tool/result", "data": {"callId": name, "isError": True}})
            self.assertEqual(events[-1]['status'], 'failed')


class NativeWebTests(unittest.TestCase):
    def test_real_profile_tools_and_native_search_protocol_in_all_modes(self):
        binary = dsh.resolve_harness_binary(ROOT)
        if binary is None:
            self.skipTest('Harness runtime not installed')
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('x-api-key'), body))
                query = body['messages'][0]['content'][0]['text']
                url = 'https://example.com/' + ('one' if query.endswith('one') else 'two')
                content = [{"type": "text", "text": "fixture only", "citations": [{"url": url, "cited_text": "fixture excerpt"}]}]
                if 'missing-native-results' not in query:
                    content.append({"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": url, "title": "Fixture source"}]})
                data = json.dumps({"content": content}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='hatchery-native-web-') as temp:
                work = Path(temp)
                (work/'site').mkdir()
                (work/'probe.mjs').write_text(PROBE)
                probe_patch = work/'probe.yml'
                # This probe calls the registry directly; disable the stdio server
                # so its stdin EOF cannot dispose plugins during an awaited call.
                probe_patch.write_text('- id: sdk-jsonrpc-server\n  disabled: true\n- insert:\n    - id: native-web-check\n      name: ' + json.dumps(str(work/'probe.mjs')) + '\n')
                for mode in ('chat', 'plan', 'full'):
                    with self.subTest(mode=mode):
                        env = {'PATH': os.environ.get('PATH', ''), 'DSH_HOME': str(work/'home'),
                               'DSH_TELEMETRY_DISABLED': '1', 'MIAODA_AI_WORKSPACE': str(work/'site'),
                               'MIAODA_TOOLS_MODE': mode, 'MIAODA_PREVIEW_PORT': '1',
                               'MIAODA_PREVIEW_PATH': '/preview/fixture/',
                               'DSH_PERMISSION_MODE': 'workspace-write' if mode == 'full' else 'read-only',
                               'DEEPSEEK_SEARCH_BASE_URL': f'http://127.0.0.1:{http.server_port}/v1',
                               'HATCHERY_DEEPSEEK_SEARCH_API_KEY': 'fixture-key',
                               'HATCHERY_DEEPSEEK_SEARCH_MODEL': 'fixture-model',
                               'HATCHERY_WEB_TEST_LIVE_FETCH': '1' if LIVE_FETCH and mode == 'full' else '0'}
                        result = subprocess.run([str(binary), '--profile', 'sdk-minimal', '--patch',
                                                 str(ROOT/'deepseek_harness/site-agent.patch.yml'), '--patch', str(probe_patch)],
                                                cwd=work, env=env, capture_output=True, text=True, timeout=55)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn('HATCHERY_NATIVE_WEB_CHECK=', result.stdout)
                self.assertEqual(len(requests), 9)
                for path, key, body in requests:
                    self.assertEqual(path, '/v1/messages')
                    self.assertEqual(key, 'fixture-key')
                    self.assertEqual(body['model'], 'fixture-model')
                    self.assertEqual(body['tools'][0]['type'], 'web_search_20250305')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
