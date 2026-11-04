"""Real sockets, replay/isolation, slow persistence, and cold DB handshakes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys
import threading
import time
import unittest
from unittest.mock import Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_jobs
from db_connections import RetainedConnectionPool
from job_events import EventHub, LiveServer, StreamCapacity
from local_postgres import LocalPostgres
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed


class HubTests(unittest.TestCase):
    def test_replay_gap_epoch_and_account_isolation(self):
        hub = EventHub(max_events=2)
        _, first, _ = hub.read('one', timeout=0)
        hub.publish('one', 'progress', {'text':'private-one'})
        reset, second, frames = hub.read('one', first, 0)
        self.assertFalse(reset); self.assertEqual(len(frames), 1)
        reset, _, frames = hub.read('two', first, 0)
        self.assertTrue(reset); self.assertFalse(frames)
        self.assertEqual(hub.read('one', second, 0)[2], [])
        for i in range(3): hub.publish('one', 'progress', {'index':i})
        self.assertTrue(hub.read('one', first, 0)[0])
        self.assertTrue(EventHub().read('one', second, 0)[0])

    def test_limits_release_and_journal_memory_is_bounded(self):
        hub = EventHub(max_events=5, max_bytes=300, max_connections=1)
        with hub.attach('one'):
            with self.assertRaises(StreamCapacity):
                with hub.attach('two'): pass
            for _ in range(30): hub.publish('one','job',{'data':'x'*200})
            self.assertLessEqual(hub.channels['one']['bytes'], 300)
        with hub.attach('two'): self.assertEqual(hub.connections, 1)
        self.assertEqual(hub.connections, 0)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {'session-one':{'id':'one'}, 'session-two':{'id':'two'}}
        self.hub = EventHub()
        self.server = LiveServer(self.hub, lambda headers:self.accounts.get(headers.get('Cookie')), 0, heartbeat=0.05)
        self.server.start(); self.addCleanup(self.server.close)
        self.host = f'127.0.0.1:{self.server.port}'

    def open(self, cookie='session-one', origin=None):
        return connect('ws://'+self.host+'/api/ai/events', origin=origin or 'http://'+self.host,
                       additional_headers={'Cookie':cookie}, proxy=None, open_timeout=2)

    def test_origin_auth_live_reconnect_and_revoke(self):
        for cookie, origin, expected in [('invalid',None,401),('session-one','http://evil.test',403)]:
            with self.assertRaises(InvalidStatus) as raised: self.open(cookie,origin)
            self.assertEqual(raised.exception.response.status_code, expected)
        with self.open() as ws:
            ws.send('{"type":"subscribe","cursor":null}')
            ready = json.loads(ws.recv(timeout=1))
            self.assertTrue(ready['payload']['reset'])
            self.hub.publish('two','progress',{'secret':'other-account'})
            start = time.monotonic()
            self.hub.publish('one','progress',{'jobId':'own'})
            while True:
                event = json.loads(ws.recv(timeout=1))
                if event['type'] != 'heartbeat': break
            self.assertEqual(event['payload'], {'jobId':'own'})
            self.assertLess(time.monotonic()-start, 0.3)
            cursor = event['id']
        self.hub.publish('one','progress',{'jobId':'missed'})
        with self.open() as ws:
            ws.send(json.dumps({'type':'subscribe','cursor':cursor}))
            self.assertFalse(json.loads(ws.recv(timeout=1))['payload']['reset'])
            self.assertEqual(json.loads(ws.recv(timeout=1))['payload']['jobId'], 'missed')
            del self.accounts['session-one']
            deadline = time.monotonic()+2
            with self.assertRaises(ConnectionClosed):
                while time.monotonic()<deadline: ws.recv(timeout=1)


class PoolTests(unittest.TestCase):
    def test_slow_connect_does_not_block_return_or_reuse(self):
        entered, release, reused = threading.Event(), threading.Event(), threading.Event()
        count = 0
        def factory():
            nonlocal count
            count += 1
            if count > 1: entered.set(); release.wait(3)
            conn = Mock(closed=False)
            conn.get_transaction_status.return_value = 0
            return conn
        pool = RetainedConnectionPool(1,2,connector=factory)
        first = pool.getconn()
        with ThreadPoolExecutor(2) as workers:
            pending = workers.submit(pool.getconn)
            self.assertTrue(entered.wait(1))
            def reuse():
                pool.putconn(first)
                value = pool.getconn()
                self.assertIs(value, first)
                pool.putconn(value); reused.set()
            check = workers.submit(reuse)
            try: self.assertTrue(reused.wait(0.5), 'Cold handshake held the pool mutex')
            finally: release.set()
            check.result(); pool.putconn(pending.result())
        pool.closeall()

    def test_parallel_growth_respects_cap_and_failed_handshake_releases_reservation(self):
        attempts = 0
        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1: raise OSError('synthetic connect failure')
            conn = Mock(closed=False); conn.get_transaction_status.return_value = 0
            return conn
        pool = RetainedConnectionPool(0,2,connector=factory,wait_timeout=0.02)
        with self.assertRaises(OSError): pool.getconn()
        with ThreadPoolExecutor(2) as workers: values = list(workers.map(lambda _:pool.getconn(), range(2)))
        with self.assertRaises(Exception): pool.getconn()
        self.assertEqual(attempts, 3)
        for value in values: pool.putconn(value)
        pool.closeall()


class ProgressTests(unittest.TestCase):
    def test_slow_storage_does_not_block_runtime_events_or_publish_an_early_completion(self):
        with LocalPostgres() as pg:
            with pg.db() as conn, conn.cursor() as cur:
                cur.execute('''CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                    title TEXT NOT NULL,messages_html TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            store = ai_jobs.JobStore(pg.db); store.initialize()
            queued = store.enqueue({'id':'one'}, {'prompt':'local fixture','mode':'chat'}, str(uuid.uuid4()))
            job = store.claim(1)
            entered, release, produced = threading.Event(), threading.Event(), threading.Event()
            original = store.progress
            def slow_save(*args): entered.set(); release.wait(4); return original(*args)
            store.progress = slow_save
            def execute(job, progress):
                progress({'id':'tool','kind':'tool','detail':'start','status':'running'})
                if not entered.wait(2): raise AssertionError('Progress writer did not start')
                for i in range(30): progress({'id':'tool','kind':'tool','detail':str(i),'status':'running'})
                produced.set()
                return {'summary':'finished once'}
            worker = ai_jobs.Dispatcher(store, execute, 1)
            thread = threading.Thread(target=worker.worker,args=(job,)); thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(produced.wait(0.5), 'Runtime reader waited for the remote database')
                visible = store.get('one',queued['jobId'])
                self.assertEqual(visible['status'], 'running')
                self.assertEqual(visible['events'][0]['detail'], '29')
                self.assertIsNone(store.get('two', queued['jobId']))
            finally: release.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(store.get('one',queued['jobId'])['status'], 'completed')


if __name__ == '__main__': unittest.main()
