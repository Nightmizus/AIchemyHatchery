"""Disposable real PostgreSQL for local regressions; never reads application DB settings."""
from contextlib import contextmanager
from pathlib import Path
import os
import pwd
import shutil
import subprocess
import tempfile

import psycopg2


class LocalPostgres:
    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix='hatchery-pg-test-'))
        self.bin = Path('/usr/lib/postgresql/18/bin')
        self.identity = pwd.getpwnam('postgres') if os.geteuid() == 0 else None
        if self.identity:
            os.chown(self.root, self.identity.pw_uid, self.identity.pw_gid)
        self.started = False
        try:
            self.run('initdb', '-D', str(self.root / 'data'), '-A', 'trust', '--no-locale', '-E', 'UTF8')
            self.run('pg_ctl', '-D', str(self.root / 'data'), '-l', str(self.root / 'postgres.log'),
                     '-o', f"-k {self.root} -c listen_addresses='' -c max_connections=48", '-w', 'start')
            self.started = True
            return self
        except Exception:
            self.__exit__()
            raise

    def demote(self):
        os.setgroups([])
        os.setgid(self.identity.pw_gid)
        os.setuid(self.identity.pw_uid)

    def run(self, name, *args):
        result = subprocess.run([str(self.bin / name), *args], cwd='/tmp', capture_output=True, text=True,
                                preexec_fn=self.demote if self.identity else None, timeout=30)
        if result.returncode:
            raise RuntimeError(f'Local PostgreSQL {name} failed: {result.stderr[-2000:]} {result.stdout[-2000:]}')

    @contextmanager
    def db(self, autocommit=False):
        conn = psycopg2.connect(host=str(self.root), user='postgres' if self.identity else pwd.getpwuid(os.getuid()).pw_name,
                                dbname='postgres', connect_timeout=3)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def __exit__(self, *args):
        try:
            if self.started:
                self.run('pg_ctl', '-D', str(self.root / 'data'), '-m', 'immediate', '-w', 'stop')
        finally:
            shutil.rmtree(self.root)
