"""Checkpoint continuation and runtime mutation guards. No external AI calls."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_files_tests import FileFixture, USER
import server
import site_runtime
from deepseek_harness_adapter import HarnessRuntimeError


class RecoveryTests(unittest.TestCase):
    def run_fixture(self, fixture, execute, *, mode='full', recoveries='1'):
        with patch.object(server, 'ROOT', fixture.root), \
                patch.object(server, 'run_deepseek_harness', side_effect=execute), \
                patch.dict(os.environ, {'HATCHERY_AI_AUTO_RECOVERIES': recoveries, 'HATCHERY_DYNAMIC_SITE_KEYS': '*'}):
            return fixture.handler.run_ai_harness(USER, 'finish the requested site', {}, True, [], [], mode=mode)

    def test_recovery_keeps_changed_files_and_continues_once_in_same_task(self):
        with FileFixture() as fixture:
            calls = []
            def execute(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    (fixture.directory / 'index.html').write_text('<h1>already saved</h1>')
                    raise HarnessRuntimeError('session stalled', recoverable=True, reason='stalled_session')
                self.assertIn('already saved', (fixture.directory / 'index.html').read_text())
                self.assertIn('安全续作', kwargs['prompt'])
                self.assertIn('index.html', kwargs['context']['recovery']['changedFiles'])
                self.assertNotEqual(kwargs['runtime_token'], calls[0]['runtime_token'])
                return {'summary': 'continued and completed', 'finishReason': 'completed'}
            result = self.run_fixture(fixture, execute)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result['recoveries'], ['stalled_session'])
            self.assertEqual((fixture.directory / 'index.html').read_text(), '<h1>already saved</h1>')
            saved = list((fixture.root / '.ai-job-results/recoveries').glob('*/attempt-1.json'))
            self.assertEqual(len(saved), 1)
            self.assertIn('index.html', json.loads(saved[0].read_text())['files'])
            with self.assertRaises(ValueError):
                site_runtime.authorized_call(calls[0]['runtime_token'], 'status', {})

    def test_recovery_is_bounded_and_keeps_checkpoint_after_repeated_failure(self):
        with FileFixture() as fixture:
            calls = []
            def execute(**kwargs):
                calls.append(kwargs)
                raise HarnessRuntimeError('runtime died', recoverable=True, reason='runtime_exit')
            with self.assertRaisesRegex(HarnessRuntimeError, '自动恢复 1 次'):
                self.run_fixture(fixture, execute)
            self.assertEqual(len(calls), 2)

    def test_quota_auth_and_normal_errors_are_not_retried(self):
        with FileFixture() as fixture:
            calls = []
            def execute(**kwargs):
                calls.append(kwargs)
                raise HarnessRuntimeError('quota or authentication failure')
            with self.assertRaisesRegex(HarnessRuntimeError, 'quota'):
                self.run_fixture(fixture, execute)
            self.assertEqual(len(calls), 1)

    def test_unconfirmed_backend_write_prevents_automatic_replay(self):
        with FileFixture() as fixture:
            calls = []
            def execute(**kwargs):
                calls.append(kwargs)
                with patch.object(site_runtime, 'call', side_effect=RuntimeError('lost reply')):
                    with self.assertRaises(RuntimeError):
                        site_runtime.authorized_call(kwargs['runtime_token'], 'exec', {'command': 'a private database write'})
                raise HarnessRuntimeError('runtime died', recoverable=True, reason='runtime_exit')
            with self.assertRaisesRegex(HarnessRuntimeError, '后台操作结果尚未确认'):
                self.run_fixture(fixture, execute)
            self.assertEqual(len(calls), 1)

    def test_readonly_recovery_never_obtains_mutating_runtime_rights(self):
        with FileFixture() as fixture:
            before = server.site_files.file_manifest(fixture.directory); calls = []
            def execute(**kwargs):
                calls.append(kwargs)
                with self.assertRaises(ValueError):
                    site_runtime.authorized_call(kwargs['runtime_token'], 'exec', {'command': 'not allowed'})
                if len(calls) == 1:
                    raise HarnessRuntimeError('session conflict', recoverable=True, reason='session_conflict')
                return {'summary': 'read only recovered'}
            self.run_fixture(fixture, execute, mode='chat')
            self.assertEqual(before, server.site_files.file_manifest(fixture.directory))
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
