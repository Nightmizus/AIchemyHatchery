"""Queue/history integration checks with real isolated PostgreSQL, zero model calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_postgres import LocalPostgres
import ai_jobs
import server


class JobsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = LocalPostgres().__enter__()
        cls.store = ai_jobs.JobStore(cls.pg.db)
        with cls.pg.db() as conn, conn.cursor() as cur:
            cur.execute('''CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                title TEXT NOT NULL, messages_html TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        cls.store.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.pg.__exit__()

    def setUp(self):
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('TRUNCATE hatchery_ai_jobs, hatchery_ai_chat_sessions RESTART IDENTITY')

    def enqueue(self, uid='one', prompt='request', request=None):
        return self.store.enqueue({'id': uid}, {'prompt': prompt, 'mode': 'full'}, request or str(uuid.uuid4()))

    def test_conversation_id_is_stable_and_per_account(self):
        # 单会话模型的核心不变式：同一账号永远同一个会话，不同账号必不相同。
        self.assertEqual(ai_jobs.conversation_id('one'), ai_jobs.conversation_id('one'))
        self.assertNotEqual(ai_jobs.conversation_id('one'), ai_jobs.conversation_id('two'))
        first, second = self.enqueue(), self.enqueue(prompt='second')
        self.assertEqual(first['sessionId'], second['sessionId'])
        self.assertEqual(first['sessionId'], ai_jobs.conversation_id('one'))
        self.assertNotEqual(self.enqueue(uid='two')['sessionId'], first['sessionId'])

    def test_concurrent_idempotency_and_ownership(self):
        request = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=12) as pool:
            jobs = list(pool.map(lambda _: self.enqueue(request=request), range(24)))
        self.assertEqual(len({j['jobId'] for j in jobs}), 1)
        with self.assertRaises(ai_jobs.Conflict): self.enqueue(request=request, prompt='different')
        # 会话由账号派生，别的账号拿不到本账号的任务或会话。
        other = self.enqueue(uid='other')
        self.assertIsNone(self.store.get('other', jobs[0]['jobId']))
        self.assertIsNone(self.store.session('other', ai_jobs.conversation_id('one')))
        self.assertEqual(self.store.session('other', ai_jobs.conversation_id('other'))['jobs'][0]['jobId'], other['jobId'])

    def test_fifo_global_bound_one_queue_per_account(self):
        first, second = self.enqueue(), self.enqueue()
        other = self.enqueue(uid='two')
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = [r for r in pool.map(lambda _: self.store.claim(2), range(8)) if r]
        self.assertEqual({r['id'] for r in claimed}, {first['jobId'], other['jobId']})
        self.store.finish(first['jobId'], {'summary': 'one'})
        self.assertEqual(self.store.claim(2)['id'], second['jobId'])

    def test_queue_reason_distinguishes_same_account_and_global_capacity(self):
        first = self.enqueue(); self.enqueue()
        other = self.enqueue(uid='two')
        self.store.claim(1)
        own = self.store.queue_status('one', 1)
        waiting = self.store.queue_status('two', 1)
        self.assertEqual(own['reason'], 'same_account')
        self.assertEqual(waiting['reason'], 'capacity')
        self.assertEqual((waiting['running'], waiting['maxParallel'], waiting['ahead']), (1, 1, 1))
        self.assertEqual(self.store.queue_status('two', 2)['reason'], 'dispatching')
        self.assertNotIn(first['jobId'], json.dumps(waiting))
        self.assertNotIn(other['jobId'], json.dumps(waiting))

    def test_capacity_hot_reload_starts_another_account_without_interrupting_first(self):
        first = self.enqueue(); same_account = self.enqueue()
        other = self.enqueue(uid='two')
        capacity = [1]; entered = threading.Event(); release = threading.Event(); other_started = threading.Event()
        starts = []
        def execute(job, progress):
            starts.append(job['id'])
            if job['id'] == first['jobId']:
                entered.set(); release.wait(10)
            if job['id'] == other['jobId']:
                other_started.set()
            return {'summary': 'local capacity regression'}
        dispatcher = ai_jobs.Dispatcher(self.store, execute, lambda: capacity[0])
        dispatcher.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertEqual(self.store.get('two', other['jobId'])['status'], 'queued')
            capacity[0] = 2
            self.assertTrue(other_started.wait(4))
            self.assertEqual(self.store.get('one', first['jobId'])['status'], 'running')
            self.assertEqual(self.store.get('one', same_account['jobId'])['status'], 'queued')
        finally:
            release.set(); dispatcher.shutdown(4); dispatcher.thread.join(3)
        self.assertEqual(len(starts), len(set(starts)))

    def test_corrupt_spool_cannot_block_other_results_or_new_jobs(self):
        first = self.enqueue(); claimed = self.store.claim(2)
        with tempfile.TemporaryDirectory() as folder:
            dispatcher = ai_jobs.Dispatcher(self.store, lambda *args: None, 2, folder)
            broken = Path(folder) / 'job-invalid.json'; broken.write_text('{incomplete')
            dispatcher.save_result(claimed['id'], {'result': {'summary': 'saved'}, 'error': None, 'events': []})
            dispatcher.flush_results()
            self.assertFalse(broken.exists())
            self.assertEqual(len(list(Path(folder).glob('*.invalid'))), 1)
            self.assertEqual(self.store.get('one', first['jobId'])['status'], 'completed')
            next_job = self.enqueue(uid='two')
            self.assertEqual(self.store.claim(2)['id'], next_job['jobId'])

    def test_capacity_file_changes_are_bounded_and_do_not_reload_other_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'ROOT', Path(folder)), \
                patch.object(server, '_AI_CAPACITY_CACHE', None):
            env = Path(folder) / '.env'
            env.write_text('HATCHERY_AI_MAX_PARALLEL=1\nDEEPSEEK_API_KEY=not-exposed\n')
            self.assertEqual(server.ai_parallel_limit(), 1)
            env.write_text('HATCHERY_AI_MAX_PARALLEL=2\nDEEPSEEK_API_KEY=not-exposed\n')
            self.assertEqual(server.ai_parallel_limit(), 2)
            self.assertNotIn('not-exposed', str(server._AI_CAPACITY_CACHE))

    def test_full_results_survive_ack_store_restart(self):
        first = self.enqueue()
        self.store.claim(2)
        text = '长回复\n' * 5000 + '<script>unsafe()</script>'
        self.store.finish(first['jobId'], {'summary': text, 'plan': 'plan\n' * 3000})
        self.store.ack('one', first['jobId'])
        second = self.enqueue(prompt='second')
        self.store.claim(2); self.store.finish(second['jobId'], {'summary': 'second answer'})
        store = ai_jobs.JobStore(self.pg.db)
        session = ai_jobs.conversation_id('one')
        view = store.session('one', session)
        self.assertEqual([j['jobId'] for j in view['jobs']], [first['jobId'], second['jobId']])
        self.assertEqual(view['jobs'][0]['result']['summary'], text)
        self.assertIn('&lt;script&gt;', view['messagesHtml'])
        self.assertNotIn('<script>', view['messagesHtml'])
        self.assertIn('second answer', view['messagesHtml'])

    def test_restart_interrupts_expired_work_without_replay_preserves_queue(self):
        first, second = self.enqueue(), self.enqueue()
        self.store.claim(2)
        with self.pg.db() as conn, conn.cursor() as cur:
            # 超过宽限期（5 分钟）才算真的失去租约
            cur.execute("UPDATE hatchery_ai_jobs SET lease_until=now()-interval '6 minutes' WHERE id=%s", (first['jobId'],))
        self.store.recover_expired()
        self.assertEqual(self.store.get('one', first['jobId'])['status'], 'interrupted')
        self.assertEqual(self.store.claim(2)['id'], second['jobId'])

    def test_short_lease_lapse_does_not_interrupt_running_work(self):
        """续租偶发失败时租约会短暂过期；这不该把仍在正常运行的任务判死。"""
        job = self.enqueue()
        self.store.claim(2)
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET lease_until=now()-interval '10 seconds' WHERE id=%s", (job['jobId'],))
        self.store.recover_expired()
        self.assertEqual(self.store.get('one', job['jobId'])['status'], 'running')

    def test_renew_reports_unsuccessful_ids(self):
        """续租必须能报告哪些任务没续上，调用方才能立刻重试而不是静默放过。"""
        job = self.enqueue()
        self.store.claim(2)
        self.assertEqual(self.store.renew([job['jobId']]), set())
        self.assertEqual(self.store.renew(['missing-job']), {'missing-job'})

    def test_persistence_outage_spools_result_without_reexecuting(self):
        job = self.enqueue(); claimed = self.store.claim(2)
        executions = []
        def execute(job, progress):
            executions.append(job['id'])
            progress({'id': 'read', 'status': 'done', 'label': 'read'})
            return {'summary': 'durable result'}
        with tempfile.TemporaryDirectory() as spool:
            dispatcher = ai_jobs.Dispatcher(self.store, execute, 2, spool)
            dispatcher.stop.set()
            with patch.object(self.store, 'finish', side_effect=ConnectionError('offline')):
                dispatcher.worker(claimed)
            self.assertEqual(executions, [job['jobId']])
            self.assertEqual(len(list(Path(spool).glob('*.json'))), 1)
            restarted = ai_jobs.Dispatcher(self.store, execute, 2, spool)
            restarted.flush_results()
            self.assertEqual(self.store.get('one', job['jobId'])['result']['summary'], 'durable result')
            self.assertEqual(executions, [job['jobId']])
            self.assertEqual(list(Path(spool).iterdir()), [])

    def test_dispatcher_runs_exactly_once_and_never_overlaps_user(self):
        jobs = [self.enqueue(uid=str(i % 3), prompt=str(i)) for i in range(9)]
        current, maximum, starts = set(), [0], []
        lock = threading.Lock()
        def execute(job, progress):
            with lock:
                self.assertNotIn(job['user_id'], current)
                current.add(job['user_id']); maximum[0] = max(maximum[0], len(current)); starts.append(job['id'])
            time.sleep(.08)
            with lock: current.remove(job['user_id'])
            return {'summary': 'local executor'}
        dispatcher = ai_jobs.Dispatcher(self.store, execute, 2)
        dispatcher.start()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                statuses = [self.store.get(str(i % 3), j['jobId'])['status'] for i, j in enumerate(jobs)]
                if all(s == 'completed' for s in statuses): break
                time.sleep(.1)
            self.assertEqual(statuses, ['completed'] * 9)
            self.assertEqual(len(set(starts)), 9)
            self.assertLessEqual(maximum[0], 2)
        finally:
            dispatcher.stop.set(); dispatcher.thread.join(3)

    def test_legacy_context_excludes_tool_logs(self):
        parser = ai_jobs.LegacyContext()
        parser.feed('<article class="ai-message user"><p>user</p></article><section class="ai-run-archive"><p>tool noise</p><div class="ai-run-final"><p>answer</p></div></section>')
        self.assertEqual(parser.entries, [{'role': 'user', 'text': 'user'}, {'role': 'assistant', 'text': 'answer'}])


if __name__ == '__main__': unittest.main()
