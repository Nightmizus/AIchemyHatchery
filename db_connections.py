"""Bounded PostgreSQL pool: network handshakes never hold the pool lock."""
from collections import deque
import threading
import time

import psycopg2
from psycopg2 import extensions
from psycopg2.pool import PoolError


class RetainedConnectionPool:
    def __init__(self, minconn, maxconn, *args, connector=None, wait_timeout=8.0, **kwargs):
        if not 0 <= minconn <= maxconn or maxconn < 1:
            raise ValueError('Invalid connection pool bounds')
        self.minconn, self.maxconn = minconn, maxconn
        self._connect = lambda: (connector or psycopg2.connect)(*args, **kwargs)
        self._condition = threading.Condition()
        self._idle, self._used = deque(), set()
        self._pending = 0
        self.closed = False
        self.wait_timeout = wait_timeout
        try:
            for _ in range(minconn): self._idle.append(self._connect())
        except BaseException:
            self.closeall()
            raise

    def getconn(self):
        deadline = time.monotonic() + self.wait_timeout
        with self._condition:
            while True:
                if self.closed: raise PoolError('Connection pool is closed')
                if self._idle:
                    conn = self._idle.pop()
                    self._used.add(conn)
                    return conn
                if len(self._used) + self._pending < self.maxconn:
                    self._pending += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise PoolError('Connection pool wait timed out')
                self._condition.wait(remaining)
        return self._finish_connect(borrow=True)

    def _finish_connect(self, borrow):
        # DNS/TCP/TLS may take seconds. Existing sockets must remain available.
        conn = None
        try:
            conn = self._connect()
            with self._condition:
                if self.closed:
                    conn.close()
                    raise PoolError('Connection pool is closed')
                if borrow: self._used.add(conn)
                else: self._idle.append(conn)
            return conn
        finally:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()

    def putconn(self, conn, close=False):
        try:
            if not close and not conn.closed and conn.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE:
                conn.rollback()
        except psycopg2.Error:
            close = True
        with self._condition:
            if conn not in self._used and not self.closed:
                raise PoolError('Connection was not borrowed from this pool')
            self._used.discard(conn)
            if self.closed or close or conn.closed: conn.close()
            else: self._idle.append(conn)
            self._condition.notify_all()

    def warm(self, target=4):
        """Best-effort background warmup; failures leave capacity for later retry."""
        with self._condition:
            count = max(0, min(target, self.maxconn) - len(self._idle) - len(self._used) - self._pending)
            if self.closed: return
            self._pending += count
        def connect():
            try: self._finish_connect(borrow=False)
            except Exception: pass  # Later borrower retries; never log a DSN.
        for _ in range(count):
            try: threading.Thread(target=connect, name='hatchery-db-warm', daemon=True).start()
            except Exception:
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()

    def closeall(self):
        with self._condition:
            self.closed = True
            connections = list(self._idle) + list(self._used)
            self._idle.clear()
            self._used.clear()
            self._condition.notify_all()
        for conn in connections: conn.close()
