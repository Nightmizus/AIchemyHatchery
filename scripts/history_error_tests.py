"""Synthetic regressions for failures found in account history; no private data or AI calls."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deepseek_harness_adapter as adapter
import server
from local_postgres import LocalPostgres


class HistoryErrorTests(unittest.TestCase):
    def test_embedded_json_does_not_replace_the_actual_report(self):
        response = '{"summary":"## 已检查\\n\\n接口返回 `{"ok":true}`。", "risk":"low", "checks":[]}'
        report = adapter._extract_json_object(response)
        self.assertEqual(report['summary'], '## 已检查\n\n接口返回 `{"ok":true}`。')
        self.assertNotIn('ok', report)

    def test_nested_native_tool_result_finishes_the_matching_call(self):
        progress = []
        projector = adapter._EventProjector(progress.append)
        for call_id in ('one', 'two'):
            projector.accept({'type': 'tool/call', 'data': {'callId': call_id, 'name': 'read_file', 'arguments': {'path': 'index.html'}}})
        projector.accept({'type': 'tool/result', 'data': {'message': {'content': [
            {'type': 'tool-result', 'toolCallId': 'two', 'isError': True, 'content': [{'type': 'text', 'text': 'Error: ENOENT private contents'}]},
        ]}}})
        self.assertEqual(progress[-1]['id'], 'dsh-two')
        self.assertEqual(progress[-1]['status'], 'failed')
        self.assertEqual([item['status'] for item in projector.trace], ['running', 'failed'])
        self.assertNotIn('private contents', str(progress))

    def test_reasoning_only_final_never_falls_back_to_an_earlier_reply(self):
        events = [
            {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': '{"summary":"starting"}'}]}}},
            {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'reasoning', 'text': 'private thinking'}]}}},
        ]
        self.assertEqual(adapter._final_response(events), '')

    def test_database_bursts_wait_instead_of_killing_accepted_tasks(self):
        with LocalPostgres() as pg:
            pool = server.pg_pool.ThreadedConnectionPool(1, 8, host=str(pg.root), user='postgres', dbname='postgres')
            ready = threading.Barrier(24)
            try:
                with patch.object(server, '_DB_POOL', pool), patch.object(server, '_DB_POOL_SLOTS', threading.BoundedSemaphore(8)):
                    def read(number):
                        ready.wait(timeout=5)
                        with server.neon_db() as conn, conn.cursor() as cur:
                            cur.execute('SELECT pg_sleep(0.03), %s', (number,))
                            return cur.fetchone()[1]
                    with ThreadPoolExecutor(max_workers=24) as executor:
                        self.assertEqual(sorted(executor.map(read, range(24))), list(range(24)))
                    with self.assertRaisesRegex(ValueError, 'rollback'):
                        with server.neon_db():
                            raise ValueError('rollback')
                    with server.neon_db() as conn, conn.cursor() as cur:
                        cur.execute('SELECT 1')
                        self.assertEqual(cur.fetchone()[0], 1)
            finally:
                pool.closeall()

    def test_database_slot_is_returned_on_connection_failure_and_wait_is_bounded(self):
        class Pool:
            def getconn(self): raise OSError('connect failed')
        slots = threading.BoundedSemaphore(1)
        with patch.object(server, '_DB_POOL_SLOTS', slots), patch.object(server, '_DB_POOL_WAIT_SECONDS', 0.01):
            for _ in range(2):
                with self.assertRaises(OSError):
                    with server.pooled_connection(Pool()): pass
            slots.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, '数据库连接繁忙'):
                    with server.pooled_connection(Pool()): pass
            finally: slots.release()

    def test_tool_execution_after_commentary_is_not_a_final_answer(self):
        events = [
            {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': '{"summary":"starting"}'}]}}},
            {'type': 'tool/call', 'data': {'callId': 'one'}},
        ]
        self.assertEqual(adapter._final_response(events), '')

    def test_upstream_errors_keep_status_and_do_not_invent_quota_or_retry_400(self):
        cases = [(400, 'INVALID_REQUEST', False, 'upstream_invalid_request'),
                 (401, 'AUTH', False, 'upstream_auth'), (429, 'RATE_LIMIT', False, 'upstream_rate_limit'),
                 (503, 'SERVER', True, 'upstream_transient')]
        for status, code, retry, reason in cases:
            with self.subTest(status=status):
                events = [{'type': 'turn/end', 'data': {'reason': {'kind': 'error', 'error': {
                    'message': 'openai_error sk-never-echo-private-value', 'code': code, 'status': status}}}}]
                error = adapter._upstream_error(events)
                self.assertIn(str(status), str(error))
                self.assertIn(code, str(error))
                self.assertEqual(error.recoverable, retry)
                self.assertEqual(error.recovery_reason, reason)
                self.assertNotIn('sk-never', str(error))
                self.assertNotIn('余额耗尽', str(error).split('不代表')[0])

    def test_invalid_outer_report_cannot_be_replaced_by_an_inner_object(self):
        for text in ('unfinished {"ok":true}', '{"summary":"broken', '{"site":{"summary":"not the report"}'):
            self.assertIsNone(adapter._extract_json_object(text))
        valid = {'summary': '正文 "引号" 和 \\ 路径\n下一段', 'risk': 'low'}
        self.assertEqual(adapter._extract_json_object('```json\n' + json.dumps(valid) + '\n```'), valid)

    def test_dispatcher_terminal_state_closes_heartbeat_but_does_not_invent_tool_success(self):
        import ai_jobs
        from unittest.mock import Mock
        store = Mock()
        def execute(job, progress):
            progress({'id': 'tool', 'tool': 'write_file', 'status': 'running', 'detail': 'index.html'})
            progress({'id': 'heartbeat', 'tool': '', 'status': 'running', 'detail': 'waiting'})
            raise adapter.HarnessRuntimeError('fixture upstream failure')
        ai_jobs.Dispatcher(store, execute, 1).worker({'id': 'job-synthetic'})
        final = store.progress.call_args.args[1]
        self.assertEqual([e['status'] for e in final], ['interrupted', 'failed'])
        self.assertIn('结果未确认', final[0]['detail'])


if __name__ == '__main__': unittest.main()
