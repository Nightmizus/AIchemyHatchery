"""Login-link security and atomicity checks against disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import login_links
from local_postgres import LocalPostgres


class LoginLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = LocalPostgres().__enter__()
        with cls.pg.db() as conn, conn.cursor() as cur:
            cur.execute('''CREATE TABLE "User" (id TEXT PRIMARY KEY, "bannedUntil" TIMESTAMPTZ,
                password TEXT NOT NULL DEFAULT 'original-password-hash', "isAdmin" BOOLEAN DEFAULT false);
                CREATE TABLE console_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,expires_at TEXT NOT NULL,last_seen_at TEXT,ip_address TEXT,user_agent TEXT);
                CREATE TABLE hatchery_audit_events(id BIGSERIAL PRIMARY KEY,user_id TEXT,event TEXT,
                detail_json TEXT,created_at TEXT)''')
            cur.execute(login_links.SCHEMA)

    @classmethod
    def tearDownClass(cls):
        cls.pg.__exit__()

    def setUp(self):
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('TRUNCATE hatchery_login_links,console_sessions,hatchery_audit_events,"User" CASCADE')
            cur.execute('INSERT INTO "User"(id) VALUES(%s)', ('authorized-user',))

    def issue(self, **kwargs):
        with self.pg.db() as conn:
            return login_links.issue(conn, 'authorized-user', 'Account holder authorized test login', **kwargs)

    def redeem(self, token):
        with self.pg.db() as conn:
            return login_links.redeem(conn, token, '127.0.0.1', 'local regression')

    def test_single_use_hash_storage_and_account_unchanged(self):
        link = self.issue()
        result = self.redeem(link['token'])
        self.assertEqual(result['user_id'], 'authorized-user')
        self.assertIsNone(self.redeem(link['token']))
        for flag in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/', 'Max-Age=7200'):
            self.assertIn(flag, result['cookie'])
        session_token = result['cookie'].split(';', 1)[0].split('=', 1)[1]
        self.assertNotEqual(link['token'], session_token)
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('SELECT token_hash,session_hash,consumed_at FROM hatchery_login_links')
            digest, session_digest, consumed = cur.fetchone()
            self.assertEqual(digest, login_links.token_hash(link['token']))
            self.assertEqual(session_digest, login_links.token_hash(session_token))
            self.assertIsNotNone(consumed)
            cur.execute('SELECT token_hash,expires_at::timestamptz-created_at::timestamptz FROM console_sessions')
            stored, lifetime = cur.fetchone()
            self.assertEqual(stored, session_digest)
            self.assertEqual(lifetime.total_seconds(), 7200)
            cur.execute('SELECT password,"isAdmin" FROM "User"')
            self.assertEqual(cur.fetchone(), ('original-password-hash', False))
            cur.execute('SELECT detail_json FROM hatchery_audit_events')
            audit = str(cur.fetchall())
            self.assertNotIn(link['token'], audit)
            self.assertNotIn(session_token, audit)

    def test_concurrent_redemption_creates_exactly_one_session(self):
        token = self.issue()['token']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.redeem, [token] * 8))
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM console_sessions')
            self.assertEqual(cur.fetchone()[0], 1)

    def test_expired_unknown_and_malformed_tokens_cannot_login(self):
        link = self.issue()
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_login_links SET expires_at=now()-interval '1 second'")
        for token in (link['token'], 'a' * 43, '', None, 'x' * 1000, '校' * 43, ['bad']):
            self.assertIsNone(self.redeem(token))
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM console_sessions')
            self.assertEqual(cur.fetchone()[0], 0)

    def test_banned_or_deleted_account_cannot_redeem(self):
        link = self.issue()
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE "User" SET "bannedUntil"=now()')
        self.assertIsNone(self.redeem(link['token']))
        with self.pg.db() as conn:
            with self.assertRaises(ValueError):
                login_links.issue(conn, 'authorized-user', 'No bypass for banned accounts')
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM "User"')
        self.assertIsNone(self.redeem(link['token']))

    def test_failed_session_write_does_not_consume_link(self):
        link = self.issue()
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute("""CREATE FUNCTION reject_test_session() RETURNS trigger LANGUAGE plpgsql AS
                $$BEGIN RAISE EXCEPTION 'simulated storage failure'; END$$;
                CREATE TRIGGER reject_session BEFORE INSERT ON console_sessions
                FOR EACH ROW EXECUTE FUNCTION reject_test_session()""")
        try:
            with self.assertRaises(Exception):
                self.redeem(link['token'])
            with self.pg.db() as conn, conn.cursor() as cur:
                cur.execute('SELECT consumed_at,session_hash FROM hatchery_login_links')
                self.assertEqual(cur.fetchone(), (None, None))
        finally:
            with self.pg.db() as conn, conn.cursor() as cur:
                cur.execute('DROP TRIGGER reject_session ON console_sessions; DROP FUNCTION reject_test_session()')
        self.assertIsNotNone(self.redeem(link['token']))

    def test_issuance_requires_known_account_and_bounded_expiry(self):
        for ttl in (0, 59, 901, 100000):
            with self.pg.db() as conn, self.assertRaises(ValueError):
                login_links.issue(conn, 'authorized-user', 'Authorized', ttl_seconds=ttl)
        with self.pg.db() as conn, self.assertRaises(ValueError):
            login_links.issue(conn, 'unknown-user', 'Authorized')
        with self.pg.db() as conn, self.assertRaises(ValueError):
            login_links.issue(conn, 'authorized-user', '')


class FakeHandler:
    def __init__(self, **headers):
        self.headers = {'Origin': login_links.ORIGIN, 'Host': 'aichemy.club',
                        'X-Forwarded-Proto': 'https', 'Content-Type': 'application/json', **headers}
        self.client_address = ('127.0.0.1', 0)
        self.wfile = BytesIO()
        self.response_headers = {}

    def request_host(self):
        return self.headers['Host']

    def send_json(self, value, status=200, headers=None):
        self.result = (status, value, headers)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class LoginLinkHttpTests(unittest.TestCase):
    def test_transport_and_origin_rejections_do_not_access_database(self):
        def forbidden_db():
            raise AssertionError('Rejected requests must not access the database')
        for headers in ({'Origin': 'https://taffy.aichemy.club'}, {'Origin': ''},
                        {'Origin': 'null'}, {'X-Forwarded-Proto': 'http'},
                        {'Host': 'evil.example'}, {'Content-Type': 'text/plain'}):
            handler = FakeHandler(**headers)
            login_links.handle_redeem(handler, forbidden_db)
            self.assertEqual(handler.result[0], 403)

    def test_landing_page_does_not_consume_link_and_uses_nonce_csp(self):
        handler = FakeHandler()
        login_links.serve_page(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response_headers['Cache-Control'], 'no-store')
        policy = handler.response_headers['Content-Security-Policy']
        self.assertIn("frame-ancestors 'none'", policy)
        nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
        self.assertIn(f'nonce="{nonce}"', handler.wfile.getvalue().decode())
        self.assertIn("history.replaceState", handler.wfile.getvalue().decode())
        self.assertNotIn('__NONCE__', handler.wfile.getvalue().decode())


if __name__ == '__main__':
    unittest.main(verbosity=2)
