"""Connection retention and overload isolation; uses disposable local PostgreSQL."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from local_postgres import LocalPostgres


class DatabaseResilienceTests(unittest.TestCase):
    def test_connection_timeouts_preserve_dsn_and_avoid_eager_pool_growth(self):
        pool = Mock(maxconn=8)
        with patch.object(server, '_DB_POOL', None), \
                patch.object(server, 'NEON_DATABASE_URL', 'postgresql://fixture/database'), \
                patch.object(server.db_connections, 'RetainedConnectionPool', return_value=pool) as factory:
            self.assertIs(server.db_pool(), pool)
            self.assertIs(server.db_pool(), pool)
            factory.assert_called_once_with(
                1, 8, 'postgresql://fixture/database', connect_timeout=3, keepalives=1,
                keepalives_idle=5, keepalives_interval=2, keepalives_count=3, tcp_user_timeout=8000)
            pool.warm.assert_called_once_with(4)

    def test_burst_connections_are_retained_and_reused(self):
        with LocalPostgres() as pg, patch.object(server, '_DB_POOL', None), \
                patch.object(server, 'NEON_DATABASE_URL', f'host={pg.root} user=postgres dbname=postgres'):
            pool = server.db_pool()
            try:
                first = [pool.getconn() for _ in range(8)]
                for conn in first: pool.putconn(conn)
                self.assertTrue(all(not conn.closed for conn in first))
                second = [pool.getconn() for _ in range(8)]
                self.assertEqual({id(conn) for conn in first}, {id(conn) for conn in second})
                for conn in second: pool.putconn(conn)
            finally:
                pool.closeall()

    def handler(self, path):
        handler = object.__new__(server.AIchemyHatcheryHandler)
        handler.path, handler.headers = path, {'Host': 'localhost'}
        handler.send_json = Mock()
        handler._proxy_site_backend = Mock(return_value=True)
        return handler

    def test_full_public_lane_does_not_block_login_or_touch_database(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(server, '_PUBLIC_BACKEND_SLOTS', slots), \
                patch.object(server, 'neon_db', side_effect=AssertionError('No database access on overload')):
            public = self.handler('/pages/test-site/_api/health')
            self.assertTrue(public.proxy_site_backend())
            self.assertEqual(public.send_json.call_args.args[1], 503)
            public._proxy_site_backend.assert_not_called()
            for path in ('/api/auth/login', '/api/auth/me', '/hatchery', '/styles.css'):
                console = self.handler(path)
                self.assertFalse(console.proxy_site_backend())
                console.send_json.assert_not_called()
        slots.release()

    def test_public_lane_returns_capacity_after_success_and_failure(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(server, '_PUBLIC_BACKEND_SLOTS', slots):
            handler = self.handler('/pages/test-site/_api/state')
            self.assertTrue(handler.proxy_site_backend())
            handler._proxy_site_backend.side_effect = OSError('unreachable upstream')
            with self.assertRaises(OSError): handler.proxy_site_backend()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()


if __name__ == '__main__': unittest.main(verbosity=2)
