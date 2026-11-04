"""Login atomicity/round trips and route ownership on disposable PostgreSQL."""
from contextlib import ExitStack, contextmanager
from http.cookies import SimpleCookie
from email.message import Message
from pathlib import Path
import hashlib
import sys
import unittest
from unittest.mock import Mock, patch
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from local_postgres import LocalPostgres


class LoginLatencyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pg = self.stack.enter_context(LocalPostgres())
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('''CREATE TABLE "User"(id TEXT PRIMARY KEY,name TEXT,password TEXT,
                "isAdmin" BOOLEAN DEFAULT false,"campusId" TEXT,"bannedUntil" TEXT,"lastLoginAt" TEXT);
                CREATE TABLE console_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT,
                created_at TEXT,expires_at TEXT,last_seen_at TEXT,ip_address TEXT,user_agent TEXT);
                CREATE TABLE hatchery_user_extras(user_id TEXT,preview_id TEXT);
                CREATE TABLE hatchery_published_sites(user_id TEXT,slug TEXT,listed_in_gallery BOOLEAN);
                INSERT INTO "User"(id,name,password) VALUES('one','fixture','verified-hash');
                INSERT INTO hatchery_user_extras VALUES('one','own-preview');
                INSERT INTO hatchery_published_sites VALUES('one','own-site',true);''')
        self.calls = []
        @contextmanager
        def db(autocommit=False):
            self.calls.append(autocommit)
            with self.pg.db() as conn:
                conn.autocommit = autocommit
                yield conn
        self.stack.enter_context(patch.object(server, 'neon_db', db))
        self.stack.enter_context(patch.object(server, 'user_column_name', return_value='lastLoginAt'))
        self.handler = object.__new__(server.AIchemyHatcheryHandler)
        self.handler.headers = Message()
        self.handler.headers['Host'] = 'localhost'
        self.handler.headers['User-Agent'] = 'fixture'
        self.handler.client_address = ('127.0.0.1', 1234)
        self.handler.send_json = Mock()
        self.handler.cookie_security = lambda: '; Secure'
        self.user = {'id': 'one', 'password': 'verified-hash'}
        server.invalidate_console_sessions()
        self.addCleanup(server.invalidate_console_sessions)

    def test_atomic_login_primes_cache_and_preserves_ownership(self):
        self.handler.complete_console_login(self.user, False)
        response = self.handler.send_json.call_args
        self.assertEqual(self.calls, [True])
        self.assertTrue(response.args[0]['ok'])
        self.assertEqual(response.args[0]['user']['previewId'], 'own-preview')
        self.assertEqual(response.args[0]['user']['publishSlug'], 'own-site')
        cookie = SimpleCookie(response.kwargs['headers']['Set-Cookie'])
        token = cookie['alchemy_hatchery_console_session'].value
        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = server.cached_console_session(digest)
        self.assertEqual(cached['id'], 'one')
        self.assertNotIn('password', cached)
        self.assertNotIn('password', response.args[0]['user'])
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('SELECT token_hash,user_id,expires_at::timestamptz-created_at::timestamptz FROM console_sessions')
            stored = cur.fetchone()
            self.assertEqual(stored[:2], (digest, 'one'))
            self.assertEqual(stored[2].total_seconds(), 12*3600)

    def test_concurrent_password_change_or_ban_prevents_session_creation(self):
        for assignment in ["password='changed-hash'", '"bannedUntil"=\'disabled\'']:
            with self.pg.db() as conn, conn.cursor() as cur:
                cur.execute('UPDATE "User" SET '+assignment+' WHERE id=\'one\'')
            self.handler.complete_console_login(self.user, True)
            self.assertEqual(self.handler.send_json.call_args.args[1], 401)
            with self.pg.db() as conn, conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM console_sessions')
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute('UPDATE "User" SET password=\'verified-hash\',"bannedUntil"=NULL')


if __name__ == '__main__': unittest.main()
