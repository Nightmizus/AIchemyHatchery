"""Isolated account/HTTP/transport regressions. No real credentials or model calls."""
from contextlib import contextmanager, ExitStack
import functools
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import personal_api
import server
import study_engine
import deepseek_harness_adapter as adapter

DATA = {'baseUrl': 'https://api.example.com/v1', 'model': 'example-model', 'apiKey': 'test-key-one'}
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
REAL_GETADDRINFO = socket.getaddrinfo


class Cursor:
    def __init__(self, conn): self.cursor = conn.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, params=()): return self.cursor.execute(sql.replace('%s', '?'), params)
    def fetchone(self): return self.cursor.fetchone()


class Connection:
    def __init__(self, conn): self.conn = conn
    def cursor(self): return Cursor(self.conn)


class Fixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.conn = sqlite3.connect(self.root / 'test.db', check_same_thread=False)
        self.stack.callback(self.conn.close)
        self.conn.execute(personal_api.SCHEMA)
        lock = threading.RLock()
        @contextmanager
        def database(autocommit=False):
            with lock:
                with self.conn:
                    yield Connection(self.conn)
        self.store = personal_api.ConfigStore(database, self.root)
        self.stack.enter_context(patch.dict(os.environ, {'HATCHERY_API_ENCRYPTION_KEY': ''}))
        self.stack.enter_context(patch.object(server, 'PERSONAL_API_STORE', self.store))
        return self
    def __exit__(self, *args): self.stack.close()


class PersonalApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture().__enter__()
        self.addCleanup(self.fixture.__exit__)
        self.store = self.fixture.store
        self.dns = patch.object(socket, 'getaddrinfo', side_effect=lambda host, *args, **kwargs:
                               REAL_GETADDRINFO(host, *args, **kwargs) if host == '127.0.0.1' else PUBLIC_DNS)
        self.dns.start(); self.addCleanup(self.dns.stop)

    def test_encrypted_account_isolation_restart_and_clear(self):
        self.store.save('one', DATA)
        self.store.save('two', {**DATA, 'apiKey': 'other-user-key'})
        raw = self.fixture.conn.execute('SELECT encrypted_config FROM hatchery_personal_api').fetchall()
        self.assertNotIn(DATA['apiKey'], str(raw))
        self.assertNotIn(DATA['baseUrl'], str(raw))
        fresh = personal_api.ConfigStore(self.store.db, self.fixture.root)
        self.assertEqual(fresh.load('one')['api_key'], DATA['apiKey'])
        self.assertEqual(fresh.load('two')['api_key'], 'other-user-key')
        self.assertIsNone(fresh.load('three'))
        # Copying ciphertext to a different row still cannot transfer ownership.
        self.fixture.conn.execute("INSERT INTO hatchery_personal_api SELECT 'three', encrypted_config FROM hatchery_personal_api WHERE user_id='one'")
        with self.assertRaisesRegex(ValueError, '解密'): fresh.load('three')
        self.store.clear('one')
        self.assertIsNone(fresh.load('one'))
        self.assertEqual(fresh.load('two')['api_key'], 'other-user-key')

    def test_blank_key_preserves_only_same_endpoint(self):
        with self.assertRaises(ValueError): self.store.save('one', {**DATA, 'apiKey': ''})
        self.store.save('one', DATA)
        saved = self.store.save('one', {**DATA, 'model': 'next-model', 'apiKey': ''})
        self.assertEqual(saved['api_key'], DATA['apiKey'])
        with self.assertRaisesRegex(ValueError, '重新填写'): self.store.save('one', {**DATA, 'baseUrl': 'https://other.example/v1', 'apiKey': ''})

    def test_invalid_values_and_private_dns(self):
        for url in ['http://example.com/v1', 'https://user:pass@example.com/v1', 'https://example.com:444/v1',
                    'https://example.com/v1?key=a', 'https://example.com/v1#x', 'https://example.com/v1/chat/completions',
                    'https://example.com\\@127.0.0.1/v1']:
            with self.subTest(url=url), self.assertRaises(ValueError): self.store.save('one', {**DATA, 'baseUrl': url})
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1']:
            with self.subTest(ip=ip), patch.object(socket, 'getaddrinfo', return_value=[(2, 1, 6, '', (ip, 443))]), self.assertRaises(ValueError):
                self.store.save('one', DATA)
        for field, value in [('apiKey', 'secret\r\nInjected: yes'), ('model', ''), ('model', 'bad model')]:
            with self.assertRaises(ValueError): self.store.save('one', {**DATA, field: value})

    def test_python_transport_pins_ip_and_tls_hostname(self):
        connection = personal_api._PinnedHTTPSConnection('api.example.com', timeout=10)
        connection._context = MagicMock()
        with patch.object(socket, 'create_connection') as create:
            connection.connect()
        self.assertEqual(create.call_args.args[0], ('93.184.216.34', 443))
        self.assertEqual(connection._context.wrap_socket.call_args.kwargs['server_hostname'], 'api.example.com')
        self.assertIsNone(personal_api._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_connection_test_and_error_does_not_echo_key(self):
        config = self.store.save('one', DATA)
        reply = MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({'data': [{'id': DATA['model']}]}).encode()
        with patch.object(personal_api, 'open_request', return_value=reply) as request:
            personal_api.test_connection(config)
            self.assertEqual(request.call_args.args[0].full_url, DATA['baseUrl'] + '/models')
        error = urllib.error.HTTPError(DATA['baseUrl'], 401, DATA['apiKey'], {}, io.BytesIO(DATA['apiKey'].encode()))
        with patch.object(personal_api, 'open_request', side_effect=error), self.assertRaises(ValueError) as caught:
            personal_api.test_connection(config)
        self.assertNotIn(DATA['apiKey'], str(caught.exception))

    def test_study_uses_personal_stream(self):
        config = self.store.save('one', DATA)
        response = MagicMock()
        response.__enter__.return_value.__iter__.return_value = iter([
            b'data: {"choices":[{"delta":{"content":"lesson"}}]}\n', b'data: [DONE]\n'])
        with patch.object(personal_api, 'open_request', return_value=response) as request:
            self.assertEqual(list(study_engine.stream_completion([], api_config=config)), [{'content': 'lesson'}])
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header('Authorization'), 'Bearer ' + DATA['apiKey'])
        self.assertEqual(json.loads(sent.data)['model'], DATA['model'])

    def test_http_auth_csrf_save_read_clear_and_status(self):
        class Handler(server.AIchemyHatcheryHandler):
            def console_user(self):
                uid = self.headers.get('X-Test-User')
                return {'id': uid, 'role': 'user'} if uid else None
            def log_message(self, *args): pass
        http_server = server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True); thread.start()
        self.addCleanup(http_server.server_close); self.addCleanup(http_server.shutdown)
        def call(method='GET', data=None, uid='one', origin=True, path='/api/account/api-config'):
            conn = http.client.HTTPConnection('127.0.0.1', http_server.server_port)
            headers = {'Content-Type': 'application/json'}
            if uid: headers['X-Test-User'] = uid
            headers['Origin'] = f'http://127.0.0.1:{http_server.server_port}' if origin else 'https://foreign.example'
            conn.request(method, path, None if data is None else json.dumps(data), headers)
            response = conn.getresponse(); payload = json.loads(response.read()); status = response.status
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            conn.close()
            return status, payload
        self.assertEqual(call(uid=None)[0], 401)
        self.assertEqual(call('POST', DATA, origin=False)[0], 403)
        status, payload = call('POST', {**DATA, 'userId': 'two'})
        self.assertEqual(status, 200); self.assertTrue(payload['configured'])
        self.assertNotIn(DATA['apiKey'], json.dumps(payload))
        self.assertFalse(call(uid='two')[1]['configured'])
        self.assertTrue(call()[1]['configured'])
        with patch.object(adapter, 'resolve_harness_binary', return_value=Path('fake-runtime')):
            self.assertEqual(call(path='/api/ai/status')[1]['provider'], '我的 API')
        self.assertFalse(call('POST', {'action': 'clear'})[1]['configured'])
        self.assertEqual(call('POST', {'action': 'test'})[0], 400)
        # Loss/rotation of encryption key must expose recovery, not a dead-end 400.
        call('POST', DATA)
        (self.fixture.root / '.private-api-config' / 'key').unlink()
        status, payload = call()
        self.assertEqual(status, 200)
        self.assertTrue(payload['needsReconfiguration'])
        self.assertFalse(payload['hasKey'])
        self.assertNotIn(DATA['apiKey'], json.dumps(payload))
        self.assertEqual(call('POST', {**DATA, 'apiKey': 'replacement-key'})[0], 200)
        self.assertEqual(self.store.load('one')['api_key'], 'replacement-key')
        (self.fixture.root / '.private-api-config' / 'key').unlink()
        self.assertTrue(call()[1]['needsReconfiguration'])
        self.assertEqual(call('POST', {'action': 'clear'})[0], 200)
        self.assertIsNone(self.store.load('one'))

    def test_encryption_key_never_in_child_env(self):
        with patch.dict(os.environ, {'HATCHERY_API_ENCRYPTION_KEY': 'secret', 'DEEPSEEK_API_KEY': 'platform-secret'}):
            child = adapter._child_env('')
        self.assertNotIn('HATCHERY_API_ENCRYPTION_KEY', child)
        self.assertNotIn('DEEPSEEK_API_KEY', child)

    def test_runtime_configuration_is_per_invocation_without_global_mutation(self):
        root = self.fixture.root
        (root / 'deepseek_harness').mkdir()
        for name in ('site-agent.patch.yml', 'site-tools.mjs'):
            (root / 'deepseek_harness' / name).touch()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'platform-key', 'HATCHERY_API_ENCRYPTION_KEY': ''}), \
             patch.object(adapter, 'resolve_harness_binary', return_value=Path('runtime')), \
             patch.object(adapter, '_JsonRpcRuntime') as runtime:
            runtime.return_value.__enter__.return_value.request.side_effect = adapter.HarnessRuntimeError('fixture-stop')
            for uid, key in [('one', 'one-key'), ('two', 'two-key')]:
                config = {**DATA, 'apiKey': key}
                own = self.store.save(uid, config)
                with self.assertRaisesRegex(adapter.HarnessRuntimeError, 'fixture-stop'):
                    adapter.run_deepseek_harness(root=root, prompt='hello', context={}, attachments=[], workspace=root,
                                                preview_port=4173, session_key=uid, api_config=own)
                env = runtime.call_args.args[2]
                self.assertEqual(env['HATCHERY_PERSONAL_API_KEY'], key)
                self.assertNotEqual(env['HATCHERY_DEEPSEEK_SEARCH_API_KEY'], key)
                self.assertTrue(env['DEEPSEEK_SEARCH_BASE_URL'].startswith('http://127.0.0.1:'))
                self.assertNotIn('DEEPSEEK_API_KEY', env)
                initialize = runtime.return_value.__enter__.return_value.request.call_args.args
                self.assertEqual(initialize[1]['provider'], 'personal-openai')
                self.assertEqual(initialize[1]['model'], DATA['model'])
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'platform-key')

    def test_worker_usage_and_degraded_state_are_not_shared_with_personal_runs(self):
        own = self.store.save('one', DATA)
        degraded = {'message': 'platform quota', 'since': 'fixture'}
        job = {'user_id': 'one', 'id': 'job', 'session_id': 'session', 'seq': 1,
               'payload_json': json.dumps({'prompt': 'hello', 'context': {}, 'includeSite': True,
                                           'attachments': [], 'chosenPresets': [], 'presetSnippets': [], 'mode': 'chat'})}
        with patch.object(server.AI_JOB_STORE, 'context', return_value=[]), \
             patch.object(server, 'AI_UPSTREAM_DEGRADED', degraded), \
             patch.object(server, 'record_ai_usage') as usage, \
             patch.object(server.AIchemyHatcheryHandler, 'run_ai_harness', return_value={'usage': None}):
            server.execute_durable_ai_job(job, None)
            self.assertEqual(usage.call_args.args[4:6], ('personal-openai', own['model']))
            self.assertIs(server.AI_UPSTREAM_DEGRADED, degraded)

    def test_native_runtime_initializes_personal_adapter_without_model_call(self):
        project = Path(__file__).resolve().parents[1]
        binary = adapter.resolve_harness_binary(project)
        if binary is None:
            self.skipTest('Optional Harness runtime is not installed')
        env = adapter._child_env('')
        env.update({'DSH_HOME': str(self.fixture.root / 'home'), 'DSH_TELEMETRY_DISABLED': '1',
                    'MIAODA_AI_WORKSPACE': str(self.fixture.root), 'MIAODA_TOOLS_MODE': 'chat',
                    'MIAODA_PREVIEW_PORT': '1', 'MIAODA_PREVIEW_PATH': '/preview/fixture/',
                    'HATCHERY_PERSONAL_API_BASE': DATA['baseUrl'], 'HATCHERY_PERSONAL_API_MODEL': DATA['model'],
                    'HATCHERY_PERSONAL_API_KEY': DATA['apiKey'], 'HATCHERY_PERSONAL_API_IPS': '["93.184.216.34"]'})
        with adapter._JsonRpcRuntime([str(binary), '--profile', 'sdk-minimal', '--patch',
                                     str(project / 'deepseek_harness/site-agent.patch.yml')], self.fixture.root, env, 30,
                                    secrets=(DATA['apiKey'],)) as runtime:
            response = runtime.request('initialize', {'cwd': str(self.fixture.root), 'provider': 'personal-openai',
                                                     'model': DATA['model'], 'maxTokens': 1024}, 25)
            self.assertIsInstance(response, dict)


if __name__ == '__main__': unittest.main(verbosity=2)
