"""Deterministic runtime protocol regressions. These do not call an AI provider."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deepseek_harness_adapter as dsh


class WatchdogTests(unittest.TestCase):
    def run_protocol(self, notifications, *, step=0, timeout=600, exited=False):
        clock = [0]
        stream = iter(notifications)
        owner = self
        if not hasattr(self, 'sessions'): self.sessions = []
        class Runtime:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def exited(self): return exited
            def _diagnostics(self): return ''
            def request(self, method, params, *a):
                if method == 'session/prompt':
                    self.sid = params['sessionId']
                    owner.sessions.append(self.sid)
                    return {'messageId': 'message-1'}
            def next_notification(self, deadline):
                clock[0] += step
                item = next(stream, {'method': 'noise', 'params': {'sessionId': 'other'}})
                if isinstance(item, Exception): raise item
                item = json.loads(json.dumps(item))
                item.setdefault('params', {}).setdefault('sessionId', self.sid)
                return item
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'deepseek_harness').mkdir()
            for name in ('site-agent.patch.yml', 'site-tools.mjs'): (root / 'deepseek_harness' / name).touch()
            with patch.dict(os.environ, {'MIAODA_LLM': 'deepseek', 'DEEPSEEK_API_KEY': 'test-only',
                                         'HATCHERY_AI_STALL_SECONDS': '120', 'DEEPSEEK_HARNESS_TIMEOUT_SECONDS': str(timeout)}, clear=True), \
                 patch.object(dsh, 'resolve_harness_binary', return_value=Path('/test/runtime')), \
                 patch.object(dsh, '_JsonRpcRuntime', Runtime), patch.object(dsh.time, 'monotonic', lambda: clock[0]):
                return dsh.run_deepseek_harness(root=root, prompt='test', context={}, attachments=[], workspace=root, preview_port=1, session_key='same-account')

    def events(self, summary='ok', finish='completed'):
        return [
            {'method': 'session.event', 'params': {'event': {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': json.dumps({'summary': summary, 'plan': 'plan' * 3000})}]}}}}},
            {'method': 'session.event', 'params': {'event': {'type': 'turn/end', 'data': {'reason': {'kind': finish}}}}},
            {'method': 'session.status', 'params': {'status': 'idle'}},
        ]

    def test_no_splice_still_accepts_new_session_progress_and_full_reply(self):
        text = '失败判据不代表任务失败。\n' * 1200
        result = self.run_protocol(self.events(text))
        self.assertEqual(result['summary'], text)
        self.assertEqual(result['plan'], 'plan' * 3000)

    def test_stale_session_log_is_purged_before_prompting(self):
        """dsh 只在会话不存在时创建它：留下的磁盘会话会让下一次任务秒失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = dsh.dsh_session_id('user-1')
            slug = '--' + str(root.resolve()).strip('/').replace('/', '-') + '--'
            stale = root / '.deepseek-harness' / 'home' / 'sessions' / slug / session
            stale.mkdir(parents=True)
            (stale / 'session.v3.jsonl').write_text('{"type":"session/"}')
            (stale / 'session.lock').write_text('')
            self.assertTrue(dsh.reset_dsh_session(root / '.deepseek-harness' / 'home', root, session))
            self.assertFalse(stale.exists())
            self.assertFalse(dsh.reset_dsh_session(root / '.deepseek-harness' / 'home', root, session))

    def test_unrelated_notification_flood_cannot_disable_stall_deadline(self):
        with self.assertRaises(dsh.HarnessRuntimeError) as raised:
            self.run_protocol([], step=31)
        self.assertTrue(raised.exception.stall_no_progress)
        self.assertTrue(raised.exception.recoverable)

    def test_native_attempt_ids_are_fresh_while_account_scope_remains_identical(self):
        self.run_protocol(self.events())
        self.run_protocol(self.events())
        self.assertNotEqual(self.sessions[0], self.sessions[1])
        for session in self.sessions:
            self.assertTrue(session.startswith(dsh.dsh_session_id('same-account') + '-'))

    def test_dead_native_process_is_recoverable_without_waiting_for_stall_cap(self):
        with self.assertRaises(dsh.HarnessRuntimeError) as raised:
            self.run_protocol([dsh.NotificationTimeout('empty slice')], step=1, exited=True)
        self.assertTrue(raised.exception.recoverable)
        self.assertEqual(raised.exception.recovery_reason, 'runtime_exit')

    def test_native_attempt_stream_counts_as_real_progress(self):
        events = [{'method': 'session.event', 'params': {'event': {'type': 'assistant/attempt',
                    'data': {'stream': [{'type': 'reasoning-delta', 'text': 'working'}]}}}}] * 6
        self.assertEqual(self.run_protocol(events + self.events(), step=31)['summary'], 'ok')

    def test_total_deadline_even_with_useful_events(self):
        events = [{'method': 'session.event', 'params': {'event': {'type': 'assistant/chunk', 'data': {'chunk': {'type': 'text-delta', 'text': 'x'}}}}}] * 10
        with self.assertRaisesRegex(dsh.HarnessRuntimeError, '时间上限'):
            self.run_protocol(events, step=31, timeout=60)

    def test_protocol_error_is_not_swallowed_as_poll_timeout(self):
        with self.assertRaisesRegex(dsh.HarnessRuntimeError, 'protocol broke'):
            self.run_protocol([dsh.HarnessRuntimeError('protocol broke')])

    def test_max_tokens_does_not_claim_success(self):
        with self.assertRaisesRegex(dsh.HarnessRuntimeError, '未完整完成'):
            self.run_protocol(self.events(finish='max-tokens'))

    def test_missing_usage_is_unknown_explicit_zero_is_zero(self):
        self.assertIsNone(dsh._token_usage([]))
        self.assertEqual(dsh._token_usage([{'type': 'assistant/message', 'data': {'usage': {'inputTokens': 0, 'outputTokens': 0}}}]), {'inputTokens': 0, 'outputTokens': 0})


if __name__ == '__main__': unittest.main()
