"""Capability, filesystem, process and HTTP boundaries; no AI/provider calls."""
from contextlib import ExitStack
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import site_runtime
import server
from site_files_tests import FileFixture, USER

spec = importlib.util.spec_from_file_location('site_runtime_broker', ROOT / 'ops/site-runtime-broker.py')
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class CapabilityTests(unittest.TestCase):
    def test_site_is_bound_by_the_server_and_client_cannot_override_it(self):
        key = site_runtime.site_key('one')
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': key}), \
                patch.object(site_runtime, 'call', return_value={'active': False}) as call:
            with site_runtime.capability('one', Path('/current/site'), 'full') as token:
                site_runtime.authorized_call(token, 'status', {'workspace': '/other/site', 'siteKey': 'other'})
                self.assertEqual(call.call_args.args[:3], ('one', Path('/current/site'), 'status'))
                with self.assertRaises(ValueError):
                    site_runtime.authorized_call(token, 'relocate', {})
            with self.assertRaises(ValueError):
                site_runtime.authorized_call(token, 'status', {})

    def test_other_accounts_do_not_receive_capabilities(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': site_runtime.site_key('one')}):
            with site_runtime.capability('two', '/other', 'full') as token:
                self.assertEqual(token, '')

    def test_chat_and_plan_cannot_execute_or_change_services(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': site_runtime.site_key('one')}), \
                patch.object(site_runtime, 'call', return_value={}) as call:
            for mode in ('chat', 'plan'):
                with site_runtime.capability('one', '/current', mode) as token:
                    site_runtime.authorized_call(token, 'status', {})
                    for operation in ('exec', 'install', 'deploy', 'stop', 'set_secrets'):
                        with self.assertRaises(ValueError):
                            site_runtime.authorized_call(token, operation, {})
            self.assertEqual(call.call_count, 2)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='hatchery-runtime-test-')))
        self.app = root / 'app'
        self.workspace = self.app / 'published/one'
        self.workspace.mkdir(parents=True); (self.workspace / 'site.json').write_text('{}')
        self.key = site_runtime.site_key('one')
        keys = root / 'allowed-sites.json'
        stat = self.workspace.stat()
        keys.write_text(json.dumps({self.key: [stat.st_dev, stat.st_ino]}))
        for name, value in (('APP', self.app), ('STATE', root / 'state'), ('KEYS', keys)):
            self.stack.enter_context(patch.object(broker, name, value))
        self.site = broker.Site(self.key, str(self.workspace))
        self.site.root.mkdir(parents=True)
        self.site.identity = SimpleNamespace(pw_uid=31001, pw_gid=31001)

    def test_same_key_cannot_choose_another_website_directory(self):
        other = self.app / 'published/two'; other.mkdir(); (other / 'site.json').write_text('{}')
        for path in (other, self.workspace / '..' / 'two', ROOT, '/etc'):
            with self.assertRaises(ValueError):
                broker.Site(self.key, str(path))

    def test_moving_the_same_directory_preserves_its_authorization(self):
        renamed = self.workspace.with_name('renamed')
        self.workspace.rename(renamed)
        self.assertEqual(broker.Site(self.key, str(renamed)).workspace, renamed)

    def test_workspace_links_and_other_identifiers_are_rejected(self):
        link = self.workspace.with_name('linked'); link.symlink_to(self.workspace)
        with self.assertRaises(ValueError): broker.Site(self.key, str(link))
        with self.assertRaises(ValueError): broker.Site(site_runtime.site_key('two'), str(self.workspace))

    def test_execution_properties_isolate_processes_and_do_not_inherit_platform_keys(self):
        with patch.dict(os.environ, {'NEON_DATABASE_URL': 'platform-db-secret', 'DEEPSEEK_API_KEY': 'platform-model-secret'}):
            properties = self.site.properties()
            command = self.site.command(['/bin/sh', '-c', 'pwd'])
        self.assertEqual(properties['PrivateNetwork'], 'yes')
        self.assertEqual(properties['NoNewPrivileges'], 'yes')
        self.assertEqual(properties['CapabilityBoundingSet'], '')
        self.assertEqual(properties['User'], '31001')
        self.assertIn(str(self.workspace) + ':/app', properties['BindReadOnlyPaths'])
        self.assertNotIn(str(self.app) + ':', properties['BindPaths'])
        self.assertIn('ReadWritePaths=/data /run/site', command)
        self.assertNotIn('platform-', (self.site.root / 'environment').read_text())
        self.assertNotIn('platform-', repr(command))
        self.assertNotIn('AF_NETLINK', properties['RestrictAddressFamilies'])
        self.assertIn('39.106.77.105/32', self.site.properties(network=True)['IPAddressDeny'])

    def test_invalid_entries_and_reserved_secrets_fail_before_provisioning(self):
        for entry in ('../server.py', '/app/backend.py', 'backend/../server.py', 'server.py'):
            with self.assertRaises(ValueError): self.site.deploy({'entry': entry, 'runtime': 'python'})
        for name in ('PATH', 'NEON_DATABASE_URL', 'MIAODA_SITE_RUNTIME_TOKEN', 'LD_PRELOAD'):
            with self.assertRaises(ValueError): self.site.dispatch('set_secrets', {'values': {name: 'forbidden'}})

    def test_secret_values_are_not_exposed_by_status_or_logs(self):
        self.site.secret_path.write_text(json.dumps({'OWN_KEY': 'private-value'}))
        self.assertEqual(self.site.redact('test private-value'), 'test ***')
        self.assertEqual(self.site.dispatch('secret_names', {}), {'names': ['OWN_KEY']})


class HttpBoundaryTests(unittest.TestCase):
    def test_backend_source_is_not_public_but_owner_can_read_it(self):
        with FileFixture() as fixture:
            (fixture.directory / 'backend').mkdir()
            (fixture.directory / 'backend/app.py').write_text('private backend source')
            fixture.publish('one')
            self.assertEqual(fixture.request('/pages/one/backend/app.py')[0], 404)
            self.assertEqual((fixture.published / 'one/backend/app.py').read_text(), 'private backend source')

    def test_public_proxy_is_bound_to_site_owner_and_strips_console_credentials(self):
        with FileFixture() as fixture, patch.object(site_runtime, 'enabled', return_value=True), \
                patch.object(site_runtime, 'call', return_value={'status': 200, 'headers': [['Content-Type', 'application/json']],
                                                               'body': base64.b64encode(b'{"ok":true}').decode()}) as call:
            fixture.publish('one')
            status, headers, body = fixture.request('/pages/one/_api/records?limit=2', headers={'Cookie': 'console-secret', 'Authorization': 'Bearer console-private', 'Origin': 'null'})
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"ok":true}')
            self.assertIn('sandbox', headers['Content-Security-Policy'])
            args = call.call_args.args
            self.assertEqual(args[:3], (USER['id'], fixture.published / 'one', 'proxy'))
            self.assertEqual(args[3]['path'], '/records?limit=2')
            self.assertNotIn('console-', str(args[3]))
            self.assertEqual(fixture.request('/pages/one/_api/records', headers={'Origin': 'https://other.example'})[0], 403)


if __name__ == '__main__':
    unittest.main()
