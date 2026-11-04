"""Durable cancellation and runtime shutdown regressions; no paid model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_jobs
import deepseek_harness_adapter as adapter
import server
import site_runtime
from local_postgres import LocalPostgres
from site_files_tests import FileFixture, USER


class CancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = LocalPostgres().__enter__()
        cls.store = ai_jobs.JobStore(cls.pg.db)
        with cls.pg.db() as conn, conn.cursor() as cur:
            cur.execute('''CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                title TEXT NOT NULL,messages_html TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        cls.store.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.pg.__exit__()

    def setUp(self):
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('TRUNCATE hatchery_ai_jobs,hatchery_ai_chat_sessions RESTART IDENTITY')

    def enqueue(self, uid='one', prompt='build', request_id=None, **extra):
        return self.store.enqueue({'id': uid}, {'prompt': prompt, 'mode': 'full', **extra}, request_id or str(uuid.uuid4()))

    def test_stop_bypasses_full_queue_and_idempotent_retry_cannot_cancel_later_work(self):
        first = self.enqueue(); self.store.claim(1)
        waiting = self.enqueue(); other = self.enqueue(uid='two')
        audit = self.enqueue(kind='security', mode='security')
        stop_id = str(uuid.uuid4())
        payload = {'prompt': '停停停！', 'mode': 'full'}
        stopped = self.store.enqueue({'id':'one'}, payload, stop_id, limit=1)
        self.assertEqual(stopped['status'], 'completed')
        self.assertTrue(self.store.get('one', first['jobId'])['cancelRequested'])
        self.assertEqual(self.store.get('one', first['jobId'])['status'], 'running')
        self.assertEqual(self.store.get('one', waiting['jobId'])['status'], 'cancelled')
        self.assertEqual(self.store.get('two', other['jobId'])['status'], 'queued')
        self.assertEqual(self.store.get('one', audit['jobId'])['status'], 'queued')
        later = self.enqueue(prompt='new confirmed request')
        self.assertEqual(self.store.enqueue({'id':'one'}, payload, stop_id)['jobId'], stopped['jobId'])
        self.assertEqual(self.store.get('one', later['jobId'])['status'], 'queued')
        self.assertIsNone(self.store.claim(1), 'Must retain slot until the worker actually exits')

    def test_control_detection_does_not_capture_normal_tasks_or_attachments(self):
        for prompt in ('停止网站服务', '不要停止', '添加一个停止按钮', '“停停停”是什么意思', 'stopwatch'):
            self.assertFalse(ai_jobs.is_stop_request({'prompt':prompt}))
        self.assertFalse(ai_jobs.is_stop_request({'prompt':'stop','attachments':[{'name':'file.txt'}]}))
        self.assertTrue(ai_jobs.is_stop_request({'prompt':'等一下我要添加文件'}))

    def test_cancel_is_owned_durable_and_late_result_cannot_revive_a_cancelled_job(self):
        job = self.enqueue(); self.store.claim(1)
        self.assertIsNone(self.store.cancel('two', job['jobId']))
        fresh = ai_jobs.JobStore(self.pg.db)
        fresh.cancel('one', job['jobId'])
        event = threading.Event()
        with self.store._live_lock: self.store._cancel_signals[job['jobId']] = event
        try:
            self.store.poll_cancellations({job['jobId']})
            self.assertTrue(event.is_set())
        finally:
            with self.store._live_lock: self.store._cancel_signals.pop(job['jobId'])
        self.store.finish(job['jobId'], {'summary':'late success'})
        self.store.finish(job['jobId'], {'summary':'spool replay'})
        self.assertEqual(fresh.get('one',job['jobId'])['status'], 'cancelled')
        self.assertIn('已停止', fresh.session('one',job['sessionId'])['messagesHtml'])

    def test_worker_keeps_account_locked_until_cleanup_and_preserves_new_request(self):
        first = self.enqueue()
        entered, closing, release, later_started = (threading.Event() for _ in range(4))
        def execute(job, progress):
            if job['id'] == first['jobId']:
                entered.set()
                try:
                    while True:
                        progress.check_cancelled(); time.sleep(.01)
                finally:
                    closing.set(); release.wait(5)
            later_started.set()
            return {'summary':'new work finished'}
        dispatcher = ai_jobs.Dispatcher(self.store, execute, 1)
        dispatcher.start()
        try:
            self.assertTrue(entered.wait(4))
            self.store.cancel('one',first['jobId'])
            self.assertTrue(closing.wait(1))
            later = self.enqueue(prompt='new confirmed request')
            self.assertEqual(self.store.get('one',first['jobId'])['status'],'running')
            self.assertFalse(later_started.wait(.15))
            release.set()
            self.assertTrue(later_started.wait(4))
            self.assertEqual(self.store.get('one',first['jobId'])['status'],'cancelled')
        finally:
            release.set(); dispatcher.shutdown(5); dispatcher.thread.join(3)

    def test_cancel_http_is_owned_and_cross_origin_requests_are_rejected(self):
        own = self.enqueue(uid=USER['id']); other = self.enqueue(uid='other')
        with FileFixture() as fixture, patch.object(server,'AI_JOB_STORE',self.store):
            self.assertEqual(fixture.request('/api/ai/run/cancel','POST',body={'jobId':other['jobId']})[0],404)
            self.assertEqual(fixture.request('/api/ai/run/cancel','POST',body={'jobId':own['jobId']},headers={'Origin':'https://evil.test'})[0],403)
            status,_,body = fixture.request('/api/ai/run/cancel','POST',body={'jobId':own['jobId']})
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)['status'],'cancelled')
            self.assertEqual(fixture.request('/api/ai/run/cancel','POST',body={'jobId':'invalid'})[0],400)

    def test_cancellation_between_claim_and_worker_start_never_calls_model(self):
        job=self.enqueue();claimed=self.store.claim(1)
        self.store.cancel('one',job['jobId'])
        calls=[]
        ai_jobs.Dispatcher(self.store,lambda *args:calls.append(args),1).worker(claimed)
        self.assertFalse(calls)
        self.assertEqual(self.store.get('one',job['jobId'])['status'],'cancelled')

    def test_expired_cancel_request_survives_store_restart_without_replay(self):
        job=self.enqueue();self.store.claim(1);self.store.cancel('one',job['jobId'])
        with self.pg.db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET lease_until=now()-interval '6 minutes' WHERE id=%s",(job['jobId'],))
        fresh=ai_jobs.JobStore(self.pg.db);fresh.recover_expired()
        self.assertEqual(fresh.get('one',job['jobId'])['status'],'cancelled')
        self.assertIsNone(fresh.claim(1))


class RuntimeCancellationTests(unittest.TestCase):
    def test_cancel_keeps_saved_files_without_automatic_retry_or_undo(self):
        with FileFixture() as fixture:
            calls=[]
            def execute(**kwargs):
                calls.append(kwargs)
                (fixture.directory/'index.html').write_text('<h1>already saved</h1>')
                raise ai_jobs.JobCancelled()
            with patch.object(server,'ROOT',fixture.root),patch.object(server,'run_deepseek_harness',side_effect=execute):
                with self.assertRaises(ai_jobs.JobCancelled):
                    fixture.handler.run_ai_harness(USER,'continue',{},True,[],[],history=[{'role':'assistant','text':'a prior unconfirmed suggestion'}])
            self.assertEqual(len(calls),1)
            self.assertIn('不等于用户已确认的事实',calls[0]['prompt'])
            self.assertIn('already saved',(fixture.directory/'index.html').read_text())
            self.assertFalse(list((fixture.root/'.ai-job-results').rglob('attempt-*.json')))

    def test_cancellation_interrupts_silent_rpc_and_kills_process_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); marker = root/'late-write'
            child = 'import time,pathlib;time.sleep(9);pathlib.Path('+repr(str(marker))+').write_text("bad")'
            code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(30)'
            signal = threading.Event()
            def check():
                if signal.is_set(): raise ai_jobs.JobCancelled()
            timer = threading.Timer(.25,signal.set); timer.start()
            started = time.monotonic()
            with self.assertRaises(ai_jobs.JobCancelled):
                with adapter._JsonRpcRuntime([sys.executable,'-c',code],root,os.environ.copy(),30) as runtime:
                    process = runtime.process
                    runtime.cancel_check = check
                    runtime.request('initialize',{})
            timer.join()
            self.assertLess(time.monotonic()-started,8)
            self.assertIsNotNone(process.poll())
            self.assertFalse(marker.exists())
            self.assertFalse(runtime.responses)

    def test_capability_revoked_before_waiting_for_accepted_backend_operation(self):
        entered, release, closed = (threading.Event() for _ in range(3))
        token_ready = threading.Event(); token = []
        def call(*a): entered.set(); release.wait(3); return {'ok':True}
        def task():
            with site_runtime.capability('test',Path('/test'),'full') as value:
                token.append(value); token_ready.set(); entered.wait(3)
            closed.set()
        with patch.object(site_runtime,'enabled',return_value=True),patch.object(site_runtime,'call',call):
            worker=threading.Thread(target=task);worker.start();self.assertTrue(token_ready.wait(2))
            operation=threading.Thread(target=lambda:site_runtime.authorized_call(token[0],'exec',{}));operation.start()
            self.assertTrue(entered.wait(2))
            deadline=time.monotonic()+2
            while token[0] in site_runtime._capabilities and time.monotonic()<deadline:time.sleep(.01)
            with self.assertRaises(ValueError):site_runtime.authorized_call(token[0],'exec',{})
            self.assertFalse(closed.is_set())
            release.set();operation.join(2);worker.join(2)
            self.assertTrue(closed.is_set())

    def test_fenced_json_stream_never_exposes_report_envelope_or_private_fields(self):
        for fence in ('```json\n','```\n','```JSON\n'):
            updates=[];projector=adapter._EventProjector(updates.append)
            projector.accept_stream({'type':'start','attemptId':'a','revision':0})
            report=fence+json.dumps({'summary':'## 正文\n已保存','secretField':'PRIVATE'},ensure_ascii=False)+'\n```'
            for i,char in enumerate(report):
                projector.stream_last_emit=-100
                projector.accept_stream({'type':'chunk','attemptId':'a','revision':i+1,'index':i,'chunk':{'type':'text-delta','text':char}})
            self.assertTrue(updates)
            self.assertEqual(updates[-1]['detail'],'## 正文\n已保存')
            self.assertTrue(all('PRIVATE' not in p['detail'] and not p['detail'].startswith(('`','{')) for p in updates))

    def test_new_native_tool_messages_report_failures_by_call_id(self):
        updates=[];projector=adapter._EventProjector(updates.append)
        projector.accept({'type':'tool/call','data':{'callId':'a','name':'read_file','arguments':'{}'}})
        projector.accept({'type':'tool/result','data':{'message':{'role':'tool','toolCallId':'a','isError':True,'content':[{'type':'text','text':'private failure'}]}}})
        self.assertEqual(projector.trace[0]['status'],'failed')
        self.assertNotIn('private failure',json.dumps(updates))


if __name__=='__main__':unittest.main()
