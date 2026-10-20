"""Capability, filesystem, process and HTTP boundaries; no AI/provider calls."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
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
    def test_all_accounts_including_future_accounts_are_enabled_but_anonymous_is_not(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': ' * '}):
            for user_id in ('one', 'two', 'not-created-yet', 10001001):
                self.assertTrue(site_runtime.enabled(user_id))
            for user_id in (None, ''):
                self.assertFalse(site_runtime.enabled(user_id))
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': ''}):
            self.assertFalse(site_runtime.enabled('one'))

    def test_global_permission_keeps_each_token_bound_to_its_own_account(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': '*'}), \
                patch.object(site_runtime, 'call', return_value={}) as call:
            with site_runtime.capability('one', '/one', 'full') as first, \
                    site_runtime.capability('two', '/two', 'full') as second:
                self.assertNotEqual(first, second)
                site_runtime.authorized_call(first, 'status', {'ownerId': 'two', 'workspace': '/two'})
                self.assertEqual(call.call_args.args[:3], ('one', Path('/one'), 'status'))
                site_runtime.authorized_call(second, 'status', {'siteKey': site_runtime.site_key('one')})
                self.assertEqual(call.call_args.args[:3], ('two', Path('/two'), 'status'))
                with self.assertRaises(ValueError):
                    site_runtime.authorized_call(first, 'register', {})

    def test_broker_payload_uses_bound_owner_not_caller_arguments(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200; response.read.return_value = b'{}'
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': '*'}), \
                patch.object(site_runtime, 'UnixConnection', return_value=connection):
            site_runtime.call('one', '/one', 'status', {'ownerId': 'two', 'siteKey': 'forged', 'workspace': '/two'})
        body = json.loads(connection.request.call_args.args[2])
        self.assertEqual((body['ownerId'], body['siteKey'], body['workspace']), ('one', site_runtime.site_key('one'), '/one'))

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

    def test_long_task_retains_own_capability_and_revokes_it_on_exit(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': '*'}), \
                patch.object(site_runtime, 'call', return_value={}) as call, \
                patch.object(time, 'monotonic', return_value=0.0) as clock:
            with site_runtime.capability('one', '/current', 'full') as token:
                clock.return_value = 24 * 60 * 60
                site_runtime.authorized_call(token, 'status', {'workspace': '/other'})
                self.assertEqual(call.call_args.args[:3], ('one', Path('/current'), 'status'))
            with self.assertRaises(ValueError):
                site_runtime.authorized_call(token, 'status', {})

    def test_chat_and_plan_cannot_execute_or_change_services(self):
        with patch.dict(os.environ, {'HATCHERY_DYNAMIC_SITE_KEYS': '*'}), \
                patch.object(site_runtime, 'call', return_value={}) as call:
            for mode in ('chat', 'plan'):
                with site_runtime.capability('one', '/current', mode) as token:
                    site_runtime.authorized_call(token, 'status', {})
                    for operation in ('exec', 'install', 'deploy', 'stop', 'set_secrets', 'delete_secrets'):
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
        policy = root / 'policy.json'
        stat = self.workspace.stat()
        keys.write_text(json.dumps({self.key: [stat.st_dev, stat.st_ino]}))
        for name, value in (('APP', self.app), ('STATE', root / 'state'), ('KEYS', keys), ('POLICY', policy)):
            self.stack.enter_context(patch.object(broker, name, value))
        self.site = broker.Site(self.key, str(self.workspace))
        self.site.root.mkdir(parents=True)
        self.site.identity = SimpleNamespace(pw_uid=31001, pw_gid=31001)

    def new_site(self, owner, draft=False):
        key = site_runtime.site_key(owner)
        directory = self.app / 'published' / ('.drafts/' + key if draft else owner)
        directory.mkdir(parents=True); (directory / 'site.json').write_text('{}')
        return key, directory

    def test_all_accounts_enroll_only_after_database_ownership_verification(self):
        broker.POLICY.write_text('{"allowAllAccounts":true}')
        key, directory = self.new_site('two')
        with patch.object(broker, 'owner_workspace', return_value=directory) as lookup:
            self.assertEqual(broker.Site(key, str(directory), 'two').workspace, directory)
            lookup.assert_called_once_with('two')
        allowed = json.loads(broker.KEYS.read_text())
        self.assertIn(self.key, allowed)
        self.assertEqual(allowed[key], [directory.stat().st_dev, directory.stat().st_ino])
        self.assertEqual(broker.KEYS.stat().st_mode & 0o777, 0o600)
        broker.POLICY.unlink()
        renamed = directory.with_name('renamed'); directory.rename(renamed)
        self.assertEqual(broker.Site(key, str(renamed)).workspace, renamed)
        with self.assertRaises(ValueError): broker.Site(key, str(self.workspace), 'two')

    def test_new_accounts_can_enroll_their_initialized_drafts(self):
        broker.POLICY.write_text('{"allowAllAccounts":true}')
        key, directory = self.new_site('future-account', draft=True)
        with patch.object(broker, 'owner_workspace', return_value=directory):
            self.assertEqual(broker.Site(key, str(directory), 'future-account').workspace, directory)

    def test_enrollment_fails_closed_without_policy_and_verified_matching_owner(self):
        key, directory = self.new_site('two')
        before = broker.KEYS.read_bytes()
        with patch.object(broker, 'owner_workspace', return_value=directory) as lookup:
            with self.assertRaises(ValueError): broker.Site(key, str(directory), 'two')
            lookup.assert_not_called()
        broker.POLICY.write_text('{"allowAllAccounts":true}')
        with patch.object(broker, 'owner_workspace', return_value=directory) as lookup:
            for owner in (None, '', 'one', 'x' * 257):
                with self.assertRaises(ValueError): broker.Site(key, str(directory), owner)
            lookup.assert_not_called()
        with patch.object(broker, 'owner_workspace', return_value=self.workspace):
            with self.assertRaises(ValueError): broker.Site(key, str(directory), 'two')
        with patch.object(broker, 'owner_workspace', side_effect=ValueError('account missing')):
            with self.assertRaises(ValueError): broker.Site(key, str(directory), 'two')
        self.assertEqual(broker.KEYS.read_bytes(), before)

    def test_another_owner_cannot_claim_an_already_bound_inode(self):
        broker.POLICY.write_text('{"allowAllAccounts":true}')
        with patch.object(broker, 'owner_workspace', return_value=self.workspace):
            with self.assertRaises(ValueError): broker.Site(site_runtime.site_key('two'), str(self.workspace), 'two')

    def test_concurrent_enrollment_preserves_all_accounts(self):
        broker.POLICY.write_text('{"allowAllAccounts":true}')
        sites = {owner: self.new_site(owner) for owner in ('two', 'three', 'four')}
        def enroll(owner):
            key, directory = sites[owner]
            return broker.Site(key, str(directory), owner)
        with patch.object(broker, 'owner_workspace', side_effect=lambda owner: sites[owner][1]), \
                ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(enroll, sites))
        self.assertEqual(set(json.loads(broker.KEYS.read_text())), {self.key, *(key for key, _ in sites.values())})

    def test_ownership_lookup_runs_fixed_code_without_root_or_shell(self):
        with patch.object(broker, 'run', return_value=SimpleNamespace(stdout=json.dumps(str(self.workspace)))) as run:
            self.assertEqual(broker.owner_workspace('account;not-a-command'), self.workspace)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['runuser', '-u', 'hatchery', '--'])
        self.assertEqual(command[-1], 'account;not-a-command')
        self.assertNotIn('account;not-a-command', command[-2])
        self.assertIn('SELECT id FROM "User"', command[-2])
        self.assertEqual(run.call_args.kwargs, {'cwd': self.app})

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
        self.assertEqual(self.site.dispatch('delete_secrets', {'names': ['OWN_KEY']})['names'], [])


class HttpBoundaryTests(unittest.TestCase):
    def test_urlsafe_preview_ids_route_to_the_same_own_backend(self):
        with FileFixture() as fixture, patch.object(site_runtime, 'enabled', return_value=True), \
                patch.object(site_runtime, 'call', return_value={'status': 200, 'headers': [['Content-Type', 'application/json']],
                                                               'body': base64.b64encode(b'{"ok":true}').decode()}) as call:
            preview_id = 'Az09_-' * 5 + 'QZ'
            fixture.db.execute('UPDATE hatchery_user_extras SET preview_id=? WHERE user_id=?', (preview_id, USER['id']))
            fixture.db.commit()
            for prefix in (f'/preview/{preview_id}', f'/ai-preview/{preview_id}/run/fixture'):
                status, headers, body = fixture.request(prefix + '/_api/content', headers={'Origin': 'null', 'Cookie': 'console-private'})
                self.assertEqual(status, 200)
                self.assertEqual(body, b'{"ok":true}')
                self.assertEqual(call.call_args.args[:3], (USER['id'], fixture.directory, 'proxy'))
                self.assertEqual(call.call_args.args[3]['path'], '/content')
                self.assertNotIn('console-private', str(call.call_args))
            before = call.call_count
            self.assertEqual(fixture.request('/preview/' + 'Z' * 32 + '/_api/content')[0], 404)
            self.assertEqual(call.call_count, before)

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
            call.return_value['headers'].append(['Set-Cookie', 'own=value; Domain=.aichemy.club; Path=/'])
            status, headers, _ = fixture.request('/_api/records', host='one.' + server.SITE_BASE_DOMAIN,
                headers={'Cookie': 'alchemy_hatchery_console_session=console-private; own=website'})
            self.assertEqual(status, 200)
            self.assertNotIn('console-private', str(call.call_args.args[3]))
            self.assertIn('own=website', call.call_args.args[3]['headers']['Cookie'])
            self.assertNotIn('domain=', headers['Set-Cookie'].lower())


if __name__ == '__main__':
    unittest.main()
