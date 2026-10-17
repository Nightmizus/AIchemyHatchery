"""Personal native-search HTTPS/DNS regressions; local TLS fixtures, no provider calls."""
from contextlib import ExitStack, redirect_stderr
from datetime import datetime, timedelta, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from personal_api_tests import Fixture, DATA, REAL_GETADDRINFO
import personal_api
import deepseek_harness_adapter as adapter
from ai_web_tools_tests import PROBE

PROJECT = Path(__file__).resolve().parents[1]
REAL_CONNECT = socket.create_connection


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.fixture = self.stack.enter_context(Fixture())
        self.config = {'base_url': DATA['baseUrl'], 'api_key': DATA['apiKey'], 'model': DATA['model']}
        self.requests, self.connections = [], []
        self.dns_ip = '93.184.216.34'
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'api.example.com')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('api.example.com')]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        cert_path = self.fixture.root / 'fixture-cert.pem'; key_path = self.fixture.root / 'fixture-key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        client_context = ssl.create_default_context(cafile=str(cert_path))
        fixture = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append((self.path, dict(self.headers), body))
                if fixture.mode == 'redirect':
                    self.send_response(302); self.send_header('Location', 'http://127.0.0.1/private'); self.end_headers(); return
                if fixture.mode == 'error':
                    self.send_response(401); self.end_headers(); self.wfile.write(DATA['apiKey'].encode()); return
                query = body.get('messages', [{'content': [{'text': 'one'}]}])[0]['content'][0]['text']
                url = 'https://example.com/' + ('one' if query.endswith('one') else 'two')
                content = [{'type': 'text', 'text': 'fixture only', 'citations': [{'url': url, 'cited_text': 'fixture excerpt'}]}]
                if 'missing-native-results' not in query:
                    content.append({'type': 'web_search_tool_result', 'content': [{'type': 'web_search_result', 'url': url, 'title': 'Fixture source'}]})
                payload = json.dumps({'content': content}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        self.mode = 'normal'
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.socket = server_context.wrap_socket(self.upstream.socket, server_side=True)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.stack.callback(self.upstream.server_close); self.stack.callback(self.upstream.shutdown)
        def dns(host, *args, **kwargs):
            if host == 'api.example.com': return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (self.dns_ip, 443))]
            return REAL_GETADDRINFO(host, *args, **kwargs)
        def connect(address, *args, **kwargs):
            if address[0] == '93.184.216.34':
                self.connections.append(address)
                # Test-only routing: checked public socket → local TLS fixture.
                return REAL_CONNECT(('127.0.0.1', self.upstream.server_port), *args, **kwargs)
            return REAL_CONNECT(address, *args, **kwargs)
        self.stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=dns))
        self.stack.enter_context(patch.object(socket, 'create_connection', side_effect=connect))
        self.stack.enter_context(patch.object(ssl, 'create_default_context', return_value=client_context))

    def call(self, env, *, path='/messages', key=None):
        url = urlsplit(env['DEEPSEEK_SEARCH_BASE_URL'])
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        conn.request('POST', url.path + path, json.dumps({'model': DATA['model'], 'messages': [{'content': [{'text': 'one'}]}]}),
                     {'x-api-key': env['HATCHERY_DEEPSEEK_SEARCH_API_KEY'] if key is None else key,
                      'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
        response = conn.getresponse(); result = response.status, response.read(); conn.close()
        return result

    def test_checked_tls_preserves_messages_auth_and_blocks_rebinding_redirects(self):
        with personal_api.search_transport({}, self.config) as env:
            self.assertNotEqual(env['HATCHERY_DEEPSEEK_SEARCH_API_KEY'], DATA['apiKey'])
            self.assertEqual(self.call(env)[0], 200)
            self.assertEqual(self.connections, [('93.184.216.34', 443)])
            path, headers, body = self.requests[-1]
            self.assertEqual(path, '/v1/messages')
            self.assertEqual(headers['X-Api-Key'], DATA['apiKey'])
            self.assertEqual(headers['Authorization'], 'Bearer ' + DATA['apiKey'])
            self.assertEqual(body['model'], DATA['model'])
            # A public DNS check before the run is insufficient: recheck on each actual request.
            for address in ('127.0.0.1', '169.254.169.254', '::ffff:127.0.0.1'):
                self.dns_ip = address
                self.assertEqual(self.call(env)[0], 502)
            self.assertEqual(len(self.connections), 1)
            self.assertEqual(len(self.requests), 1)
            self.dns_ip = '93.184.216.34'
            self.mode = 'redirect'
            self.assertEqual(self.call(env)[0], 502)
            self.assertEqual(len(self.requests), 2)  # No redirected connection.
            self.mode = 'error'
            status, payload = self.call(env)
            self.assertEqual(status, 502); self.assertNotIn(DATA['apiKey'].encode(), payload)

    def test_bridge_auth_route_isolation_and_cleanup(self):
        other = {**self.config, 'api_key': 'second-account-key'}
        with personal_api.search_transport({}, self.config) as first, personal_api.search_transport({}, other) as second:
            self.assertEqual(self.call(first, key=second['HATCHERY_DEEPSEEK_SEARCH_API_KEY'])[0], 403)
            self.assertEqual(self.call(first, key='\u00e9')[0], 403)
            self.assertEqual(self.call(first, path='/../models')[0], 404)
            self.assertEqual(self.requests, [])
            self.assertEqual(self.call(first)[0], 200)
            self.assertEqual(self.call(second)[0], 200)
            self.assertEqual([r[1]['X-Api-Key'] for r in self.requests], [DATA['apiKey'], 'second-account-key'])
        with self.assertRaises(OSError): self.call(first)
        original = {'DEEPSEEK_SEARCH_BASE_URL': 'https://server-managed.example/v1', 'HATCHERY_DEEPSEEK_SEARCH_API_KEY': 'server-key'}
        with personal_api.search_transport(original, None) as result:
            self.assertIs(result, original)

    def test_malformed_upstream_protocol_never_prints_credentials(self):
        for error in (http.client.BadStatusLine(DATA['apiKey']), http.client.IncompleteRead(DATA['apiKey'].encode())):
            diagnostic = io.StringIO()
            with personal_api.search_transport({}, self.config) as env, redirect_stderr(diagnostic), \
                 patch.object(personal_api, 'open_request', side_effect=error):
                status, payload = self.call(env)
            self.assertEqual(status, 502)
            self.assertNotIn(DATA['apiKey'].encode(), payload)
            self.assertNotIn(DATA['apiKey'], diagnostic.getvalue())

    def test_actual_native_search_uses_checked_transport_in_all_modes(self):
        binary = adapter.resolve_harness_binary(PROJECT)
        if binary is None: self.skipTest('Harness runtime is not installed')
        root = self.fixture.root
        (root / 'site').mkdir()
        (root / 'probe.mjs').write_text(PROBE, encoding='utf-8')
        probe_patch = root / 'probe.yml'
        probe_patch.write_text('- id: sdk-jsonrpc-server\n  disabled: true\n- insert:\n    - id: native-web-check\n      name: ' + json.dumps(str(root / 'probe.mjs')) + '\n')
        for mode in ('chat', 'plan', 'full'):
            env = adapter._child_env('')
            env.update({'DSH_HOME': str(root / 'home'), 'DSH_TELEMETRY_DISABLED': '1',
                        'MIAODA_AI_WORKSPACE': str(root / 'site'), 'MIAODA_TOOLS_MODE': mode,
                        'MIAODA_PREVIEW_PORT': '1', 'MIAODA_PREVIEW_PATH': '/preview/fixture/',
                        'DSH_PERMISSION_MODE': 'workspace-write' if mode == 'full' else 'read-only'})
            with personal_api.search_transport(env, self.config) as native_env:
                result = subprocess.run([str(binary), '--profile', 'sdk-minimal', '--patch',
                                         str(PROJECT / 'deepseek_harness/site-agent.patch.yml'), '--patch', str(probe_patch)],
                                        cwd=root, env=native_env, capture_output=True, text=True, encoding='utf-8', timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('HATCHERY_NATIVE_WEB_CHECK=', result.stdout)
        self.assertEqual(len(self.requests), 9)
        self.assertTrue(all(address == ('93.184.216.34', 443) for address in self.connections))
        for path, headers, body in self.requests:
            self.assertEqual(path, '/v1/messages')
            self.assertEqual(headers['X-Api-Key'], DATA['apiKey'])
            self.assertEqual(body['tools'][0]['type'], 'web_search_20250305')


if __name__ == '__main__': unittest.main(verbosity=2)
