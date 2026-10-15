"""Local TLS, publication and certificate-retry regressions; no CA/model calls."""
from socketserver import BaseRequestHandler, ThreadingTCPServer
import importlib.util
import json
from pathlib import Path
import runpy
import ssl
import subprocess
import sys
import tempfile
import threading
from types import ModuleType
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from site_files_tests import FileFixture, USER

spec = importlib.util.spec_from_file_location('hatchery_cert_sync', ROOT / 'ops/hatchery-cert-sync.py')
sync_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_module)


class ServedTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hatchery-tls-')
        root = Path(cls.tmp.name)
        key, cert = root / 'key.pem', root / 'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cert), '-subj', '/CN=existing.aichemy.test',
                        '-addext', 'subjectAltName=DNS:existing.aichemy.test'],
                       check=True, capture_output=True)
        cls.client_context = ssl.create_default_context(cafile=str(cert))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.http = ThreadingTCPServer(('127.0.0.1', 0), BaseRequestHandler)
        cls.http.server_port = cls.http.server_address[1]
        cls.http.socket = context.wrap_socket(cls.http.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join(3); cls.tmp.cleanup()

    def setUp(self):
        server._SITE_HTTPS_CACHE.clear()
        self.patches = [patch.object(server, 'SITE_SUBDOMAINS_ENABLED', True),
                        patch.object(server, 'SITE_BASE_DOMAIN', 'aichemy.test'),
                        patch.object(server, 'SITE_TLS_PROBE_HOST', '127.0.0.1'),
                        patch.object(server, 'SITE_TLS_PROBE_PORT', self.http.server_port),
                        patch.object(server.ssl, 'create_default_context', return_value=self.client_context)]
        for item in self.patches: item.start(); self.addCleanup(item.stop)
        self.addCleanup(server._SITE_HTTPS_CACHE.clear)

    def test_actual_hostname_match_is_required(self):
        self.assertEqual(server.public_site_url('existing'), 'https://existing.aichemy.test/')
        self.assertEqual(server.public_site_url('new-site'), '/pages/new-site/')

    def test_pending_probe_is_cached_briefly_then_rechecked(self):
        with patch.object(server.socket, 'create_connection', wraps=server.socket.create_connection) as connect:
            self.assertFalse(server.site_https_ready('new-site'))
            self.assertFalse(server.site_https_ready('new-site'))
            self.assertEqual(connect.call_count, 1)
            server._SITE_HTTPS_CACHE['new-site.aichemy.test'] = (0, False)
            self.assertFalse(server.site_https_ready('new-site'))
            self.assertEqual(connect.call_count, 2)

    def test_unavailable_tls_proxy_keeps_a_working_path(self):
        with patch.object(server.socket, 'create_connection', side_effect=TimeoutError):
            self.assertEqual(server.public_site_url('existing'), '/pages/existing/')


class PublicationTests(unittest.TestCase):
    def test_publish_never_returns_an_unverified_https_link_then_promotes(self):
        with FileFixture() as fixture, patch.object(server, 'SITE_SUBDOMAINS_ENABLED', True), \
                patch.object(server, 'SITE_BASE_DOMAIN', 'aichemy.test'), \
                patch.object(server, 'CERT_SYNC_TRIGGER', fixture.root / 'var/request'), \
                patch.object(server, 'site_https_ready', return_value=False) as ready:
            status, payload = fixture.publish('new-site')
            self.assertEqual(status, 200)
            self.assertTrue(server.CERT_SYNC_TRIGGER.is_file())
            self.assertEqual(payload['publicUrl'], '/pages/new-site/')
            self.assertTrue(payload['httpsPending'])
            self.assertEqual(payload['siteFiles']['publicUrl'], '/pages/new-site/')
            self.assertEqual(fixture.request(payload['publicUrl'])[0], 200)
            ready.return_value = True
            info = fixture.handler.site_files_info(USER['id'], fixture.published / 'new-site', 'new-site')
            self.assertEqual(info['publicUrl'], 'https://new-site.aichemy.test/')
            self.assertFalse(info['httpsPending'])


class CertbotTimeoutTests(unittest.TestCase):
    def test_actual_network_default_changes_and_explicit_values_are_preserved(self):
        acme = ModuleType('acme')
        client = ModuleType('acme.client')
        class Network:
            def __init__(self, key, timeout=45):
                self.timeout = timeout
        client.ClientNetwork = Network
        client.DEFAULT_NETWORK_TIMEOUT = 45
        acme.client = client
        with patch.dict(sys.modules, {'acme': acme, 'acme.client': client}):
            runpy.run_path(str(ROOT / 'ops/certbot-sitecustomize.py'))
            self.assertEqual(Network('fixture-key').timeout, 180)
            self.assertEqual(Network('fixture-key', timeout=240).timeout, 240)
            self.assertEqual(Network('fixture-key', 90).timeout, 90)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='hatchery-cert-retry-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.sync = sync_module.CertSync(root / 'app', root / 'state', root / 'live', 'aichemy.test')
        self.sync.state.mkdir()
        self.sync.trigger.parent.mkdir(parents=True)
        self.sync.trigger.touch()
        site = self.sync.app / 'published/new-site'
        site.mkdir(parents=True); (site / 'site.json').write_text('{}')
        self.fixed = {'aichemy.test', 'hatchery.aichemy.test'}

    def test_ca_failure_is_retried_without_another_publish(self):
        with patch.object(self.sync, 'certificate_names', return_value=self.fixed), \
                patch.object(sync_module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertEqual(self.sync.reconcile(), 1)
            self.assertFalse(self.sync.trigger.exists())
            self.assertEqual(self.sync.retry_state()['attempts'], 1)
            self.assertEqual(self.sync.reconcile(), 0)
            self.assertEqual(run.call_count, 1)
        state = self.sync.retry_state(); state['retryAt'] = 0
        self.sync.retry.write_text(json.dumps(state))
        with patch.object(self.sync, 'certificate_names', return_value=self.fixed), \
                patch.object(sync_module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(self.sync, 'reload_nginx') as reload, patch.object(self.sync, 'served_ready', return_value=True):
            self.assertEqual(self.sync.reconcile(), 0)
            reload.assert_called_once()
            self.assertFalse(self.sync.retry.exists())

    def test_successful_issuance_with_bad_served_certificate_is_not_success(self):
        with patch.object(self.sync, 'certificate_names', return_value=self.fixed), \
                patch.object(sync_module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(self.sync, 'reload_nginx'), patch.object(self.sync, 'served_ready', return_value=False):
            self.assertEqual(self.sync.reconcile(), 1)
            self.assertIn('served TLS', self.sync.retry_state()['reason'])

    def test_network_timeout_schedules_an_automatic_retry(self):
        with patch.object(self.sync, 'certificate_names', return_value=self.fixed), \
                patch.object(sync_module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('certbot', 360)):
            self.assertEqual(self.sync.reconcile(), 1)
            state = self.sync.retry_state()
            self.assertIn('timed out', state['reason'])
            self.assertAlmostEqual(state['retryAt'] - sync_module.time.time(), 120, delta=2)

    def test_rate_limit_uses_a_longer_backoff(self):
        def limited(command, **kwargs):
            kwargs['stdout'].write(b'urn:ietf:params:acme:error:rateLimited')
            return subprocess.CompletedProcess(command, 1)
        with patch.object(self.sync, 'certificate_names', return_value=self.fixed), \
                patch.object(sync_module.subprocess, 'run', side_effect=limited):
            self.assertEqual(self.sync.reconcile(), 1)
            state = self.sync.retry_state()
            self.assertIn('rate limit', state['reason'])
            self.assertAlmostEqual(state['retryAt'] - sync_module.time.time(), 3600, delta=2)

    def test_complete_certificate_is_idempotent_and_does_not_call_ca(self):
        with patch.object(self.sync, 'certificate_names', return_value=self.sync.desired_names()), \
                patch.object(self.sync, 'served_ready', return_value=True), \
                patch.object(sync_module.subprocess, 'run') as run:
            self.assertEqual(self.sync.reconcile(), 0)
            run.assert_not_called()

    def test_certificate_on_disk_with_stale_nginx_is_reloaded(self):
        with patch.object(self.sync, 'certificate_names', return_value=self.sync.desired_names()), \
                patch.object(self.sync, 'served_ready', side_effect=[False, True]), \
                patch.object(self.sync, 'reload_nginx') as reload:
            self.assertEqual(self.sync.reconcile(), 0)
            reload.assert_called_once()

    def test_publication_during_issuance_is_not_lost(self):
        batches = []
        def issue(command, **kwargs):
            batches.append(command)
            if len(batches) == 1:
                site = self.sync.app / 'published/second-site'
                site.mkdir(); (site / 'site.json').write_text('{}')
                self.sync.trigger.touch()
            return subprocess.CompletedProcess(command, 0)
        with patch.object(self.sync, 'certificate_names', side_effect=[self.fixed, self.fixed | {'new-site.aichemy.test'}]), \
                patch.object(sync_module.subprocess, 'run', side_effect=issue), \
                patch.object(self.sync, 'reload_nginx'), patch.object(self.sync, 'served_ready', return_value=True):
            self.assertEqual(self.sync.reconcile(), 0)
            self.assertTrue(self.sync.trigger.exists())
            self.assertEqual(self.sync.reconcile(), 0)
            self.assertEqual(len(batches), 2)
            self.assertIn('second-site.aichemy.test', batches[1])

    def test_non_site_and_symlink_directories_are_excluded(self):
        (self.sync.app / 'published/not-published').mkdir()
        (self.sync.app / 'published/link-site').symlink_to(self.sync.app / 'published/new-site')
        self.assertEqual(self.sync.desired_names(), self.fixed | {'new-site.aichemy.test'})


if __name__ == '__main__':
    unittest.main()
