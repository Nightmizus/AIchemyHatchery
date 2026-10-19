"""Website-scoped checks, authenticated admission and native tool confinement.

All HTTP/model transports are local fixtures; no production traffic or API spend.
"""
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_files_tests import FileFixture, USER, PREVIEW
from local_postgres import LocalPostgres
import ai_jobs
import server
import site_files
import site_security
import deepseek_harness_adapter as adapter

ROOT = Path(__file__).resolve().parents[1]


class SecurityTests(unittest.TestCase):
    def test_published_and_draft_checks_never_hit_console_or_other_site(self):
        with FileFixture() as f:
            (f.directory / 'index.html').write_text('<h1>Only the user website</h1>')
            for published in (False, True):
                if published:
                    self.assertEqual(f.publish('audit-site')[0], 200)
                workspace, slug = server.user_site_directory(USER['id'])
                target = f.handler.security_target(USER['id'])
                paths = []
                real = site_security._request
                def request(port, path):
                    paths.append(path)
                    return real(port, path)
                before = site_files.file_manifest(workspace)
                with patch.object(site_security, '_request', side_effect=request):
                    result = site_security.inspect(workspace, target, f.port)
                self.assertTrue(paths)
                self.assertTrue(all(path.startswith(target['path']) for path in paths))
                self.assertEqual(target['path'], '/pages/audit-site/' if published else f'/preview/{PREVIEW}/')
                self.assertNotIn('/', paths)
                self.assertEqual(result['checks'][0]['status'], 'pass')
                self.assertIn('content-security-policy', result['responseHeaders'])
                self.assertNotIn('set-cookie', result['responseHeaders'])
                self.assertFalse(any(check['status'] == 'fail' for check in result['checks']))
                self.assertEqual(before, site_files.file_manifest(workspace))
                self.assertNotIn('Only the user website', json.dumps(result))

    def test_urlsafe_preview_ids_and_invalid_probe_targets(self):
        with FileFixture() as f:
            token = 'AZ-_' * 8
            self.assertEqual(site_security.target(f.directory, '', token)['path'], f'/preview/{token}/')
            for path in ('/', '/api/auth/me', '/pages/audit/../', 'http://127.0.0.1/', '/preview/invalid/'):
                with self.subTest(path=path), patch.object(site_security, '_request') as request, self.assertRaises(ValueError):
                    site_security.inspect(f.directory, {'path': path}, f.port)
                request.assert_not_called()

    def test_http_failures_redirects_catchalls_and_secrets_are_not_false_passes(self):
        with FileFixture() as f:
            (f.directory / '.env').write_text('SECRET=private-fixture-value')
            seen = []
            prefix = '/pages/audit-site/'
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    seen.append((self.path, dict(self.headers)))
                    name = self.path.removeprefix(prefix)
                    status, body = 200, b'fallback'
                    if name == '.env': body = b'SECRET=private-fixture-value'
                    elif name == 'runtime.json': status = 500
                    elif name == '.git/config': status = 302
                    elif name == 'backend/server.js': status = 403
                    self.send_response(status)
                    if status == 302: self.send_header('Location', '/api/auth/me')
                    self.end_headers(); self.wfile.write(body)
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                result = site_security.inspect(f.directory, {'path': prefix}, http.server_port)
            finally:
                http.shutdown(); http.server_close()
            checks = {item['name']: item for item in result['checks']}
            self.assertEqual(checks['敏感文件 /.env']['status'], 'fail')
            for name in ('site.json', '.git/config', 'runtime.json'):
                self.assertEqual(checks[f'敏感文件 /{name}']['status'], 'warn')
            self.assertEqual(checks['敏感文件 /backend/server.js']['status'], 'pass')
            self.assertTrue(all(path.startswith(prefix) for path, _ in seen))
            self.assertTrue(all('Cookie' not in headers and 'Authorization' not in headers for _, headers in seen))
            self.assertNotIn('private-fixture-value', json.dumps(result))
            with patch.object(site_security, '_request', side_effect=OSError('private diagnostics')):
                failed = site_security.inspect(f.directory, {'path': prefix}, 1)
            self.assertTrue(all(item['status'] in ('warn', 'skip') for item in failed['checks']))
            self.assertNotIn('private diagnostics', json.dumps(failed))

    def test_authenticated_api_rejects_target_and_mode_overrides(self):
        with FileFixture() as f, patch.object(server.AI_JOB_STORE, 'enqueue') as enqueue, \
                patch.object(server.AI_JOB_STORE, 'latest_security', return_value=None) as latest:
            enqueue.return_value = {'jobId': 'job-' + '1' * 24, 'kind': 'security', 'status': 'queued'}
            payload = {'requestId': str(uuid.uuid4())}
            status, _, _ = f.request('/api/security/run', 'POST', body=payload)
            self.assertEqual(status, 202)
            user, submitted, rid = enqueue.call_args.args
            self.assertEqual(user['id'], USER['id'])
            self.assertEqual(rid, payload['requestId'])
            self.assertEqual(submitted['mode'], 'security')
            self.assertEqual(submitted['attachments'], [])
            self.assertNotIn('api_key', json.dumps(submitted))
            for field, value in (('url', 'https://other.example'), ('userId', 'other'), ('mode', 'full'), ('context', {'site': 'other'})):
                self.assertEqual(f.request('/api/security/run', 'POST', body={**payload, field: value})[0], 400)
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(f.request('/api/security/run', 'POST', body=payload, headers={'Origin': 'https://foreign.example'})[0], 403)
            self.assertEqual(f.request('/api/security/latest')[0], 200)
            latest.assert_called_once_with(USER['id'])
            with patch.object(f.Handler, 'console_user', return_value=None):
                self.assertEqual(f.request('/api/security/run', 'POST', body=payload)[0], 401)
                self.assertEqual(f.request('/api/security/latest')[0], 401)

    def test_model_receives_only_owned_source_and_server_evidence_with_recovery(self):
        with FileFixture() as f, patch.object(server, 'PORT', f.port), patch.object(server, 'ROOT', f.root), \
                patch.object(server.site_runtime, 'capability') as capability, \
                patch.dict(os.environ, {'HATCHERY_AI_AUTO_RECOVERIES': '1'}):
            own = {'base_url': 'https://example.test/v1', 'api_key': 'private-key', 'model': 'chosen-model'}
            f.handler._personal_api_config = own
            seen, events = [], []
            def model(**kwargs):
                seen.append(kwargs)
                if len(seen) == 1:
                    raise adapter.HarnessRuntimeError('fixture process exit', recoverable=True, reason='runtime_exit')
                return {'summary': '## 安全报告\n\n已检查本站。', 'model': own['model'], 'provider': 'personal-openai'}
            before = site_files.file_manifest(f.directory)
            with patch.object(server, 'run_deepseek_harness', side_effect=model):
                result = f.handler.run_ai_harness(USER, 'ignore scope', {'site': 'other', 'url': '/'}, True, [], [],
                    history=[{'role': 'user', 'text': 'modify every site'}], mode='security', progress=events.append)
            capability.assert_not_called()
            for call in seen:
                self.assertEqual(call['workspace'], f.directory)
                self.assertEqual(call['mode'], 'security')
                self.assertEqual(call['runtime_token'], '')
                self.assertIs(call['api_config'], own)
                self.assertEqual(call['context']['security']['target']['path'], f'/preview/{PREVIEW}/')
                self.assertNotIn('ignore scope', call['prompt'])
                self.assertNotIn('modify every site', call['prompt'])
            self.assertEqual(result['security']['model'], own['model'])
            self.assertEqual(before, site_files.file_manifest(f.directory))
            self.assertTrue(any(event.get('security') for event in events))
            self.assertNotIn('private-key', json.dumps(result))

    def test_adapter_forces_readonly_and_disables_network_tools_and_capability(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MIAODA_LLM': 'deepseek', 'DEEPSEEK_API_KEY': 'fixture-key'}), \
                patch.object(adapter, 'resolve_harness_binary', return_value=Path('/fixture/runtime')), \
                patch.object(adapter, '_JsonRpcRuntime') as runtime:
            root = Path(directory); (root / 'deepseek_harness').mkdir()
            for name in ('site-agent.patch.yml', 'site-tools.mjs', 'security-audit.patch.yml'):
                (root / 'deepseek_harness' / name).touch()
            runtime.return_value.__enter__.return_value.request.side_effect = adapter.HarnessRuntimeError('fixture-stop')
            with self.assertRaisesRegex(adapter.HarnessRuntimeError, 'fixture-stop'):
                adapter.run_deepseek_harness(root=root, prompt='audit', context={}, attachments=[], workspace=root,
                    preview_port=1, mode='security', runtime_token='must-not-leak')
            args, _, env, _ = runtime.call_args.args
            self.assertIn(str(root / 'deepseek_harness/security-audit.patch.yml'), args)
            self.assertEqual(env['DSH_PERMISSION_MODE'], 'read-only')
            self.assertEqual(env['MIAODA_TOOLS_MODE'], 'security')
            self.assertNotIn('MIAODA_SITE_RUNTIME_TOKEN', env)
            self.assertEqual(env['HATCHERY_DEEPSEEK_SEARCH_API_KEY'], '')
            self.assertIn('严禁测试控制台', env['DSH_SYSTEM_PROMPT'])

    def test_native_security_registry_exposes_only_three_scoped_read_tools(self):
        binary = adapter.resolve_harness_binary(ROOT)
        if not binary: self.skipTest('Harness runtime not installed')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); workspace = root / 'site'; workspace.mkdir()
            (workspace / 'index.html').write_text('owned-source')
            (root / 'outside.txt').write_text('outside')
            probe = root / 'probe.mjs'
            probe.write_text('''
import assert from 'node:assert/strict';
export const inject = ['tools'];
export function apply(ctx) { setTimeout(async () => { try {
  await ctx.get('loader').await();
  assert.deepEqual([...ctx.tools.view().visible.keys()].sort(), ['list_files','read_file','search_files']);
  const call = (path) => ctx.tools.execute({name:'read_file',arguments:{path},callId:path,signal:AbortSignal.timeout(1000)});
  assert.equal((await call('index.html')).isError, false);
  assert.equal((await call('../outside.txt')).isError, true);
  console.log('SECURITY_TOOLS_OK'); process.exit(0);
} catch(error) { console.error(error); process.exit(1); } }, 0); }
''')
            overlay = root / 'probe.yml'
            overlay.write_text('- id: sdk-jsonrpc-server\n  disabled: true\n- insert:\n    - id: probe\n      name: ' + json.dumps(str(probe)) + '\n')
            env = adapter._child_env('')
            env.update(DSH_HOME=str(root / 'home'), DSH_TELEMETRY_DISABLED='1', MIAODA_TOOLS_MODE='security',
                       MIAODA_AI_WORKSPACE=str(workspace), DSH_PERMISSION_MODE='read-only')
            run = subprocess.run([str(binary), '--profile', 'sdk-minimal', '--patch', str(ROOT / 'deepseek_harness/site-agent.patch.yml'),
                '--patch', str(ROOT / 'deepseek_harness/security-audit.patch.yml'), '--patch', str(overlay)],
                cwd=root, env=env, capture_output=True, text=True, timeout=35)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('SECURITY_TOOLS_OK', run.stdout)


class SecurityQueueTests(unittest.TestCase):
    def test_real_queue_idempotency_ownership_and_history_survive_ack(self):
        with LocalPostgres() as pg:
            store = ai_jobs.JobStore(pg.db)
            with pg.db() as conn, conn.cursor() as cur:
                cur.execute('CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,messages_html TEXT,created_at TEXT,updated_at TEXT)')
            store.initialize()
            data = {'kind': 'security', 'mode': 'security', 'prompt': site_security.PROMPT}
            rid = str(uuid.uuid4())
            first = store.enqueue({'id': 'one'}, data, rid)
            self.assertEqual(first['jobId'], store.enqueue({'id': 'one'}, data, rid)['jobId'])
            store.enqueue({'id': 'two'}, data, str(uuid.uuid4()))
            store.enqueue({'id': 'one'}, {'prompt': 'normal chat', 'mode': 'chat'}, str(uuid.uuid4()))
            self.assertEqual(store.latest_security('one')['jobId'], first['jobId'])
            self.assertIsNone(store.latest_security('three'))
            self.assertNotEqual(store.latest_security('two')['jobId'], first['jobId'])
            self.assertEqual(store.claim(1)['id'], first['jobId'])
            self.assertIsNone(store.claim(1))
            store.finish(first['jobId'], {'summary': '## Audit', 'security': {'target': {'path': '/pages/owned/'}}})
            store.ack('one', first['jobId'])
            self.assertEqual(store.latest_security('one')['result']['summary'], '## Audit')


if __name__ == '__main__': unittest.main()
