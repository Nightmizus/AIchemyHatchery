#!/usr/bin/python3
"""Provision published hostnames and verify the certificate nginx actually serves.

Installed root-owned at /usr/local/sbin/hatchery-cert-sync. The application only
touches a request file; it cannot supply command arguments or execute root code.
The timer reconciles every two minutes, including after a failed/lost request.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time

LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class CertSync:
    def __init__(self, app=Path('/opt/hatchery'), state=Path('/var/lib/hatchery-cert-sync'),
                 live=Path('/etc/letsencrypt/live/hatchery-aichemy'), domain='aichemy.club'):
        self.app, self.state, self.live, self.domain = app, state, live, domain
        self.trigger = app / 'var/cert-sync-request'
        self.retry = state / 'retry.json'

    def desired_names(self):
        names = {self.domain, 'hatchery.' + self.domain}
        published = self.app / 'published'
        if published.is_dir():
            names.update(p.name + '.' + self.domain for p in published.iterdir()
                         if not p.is_symlink() and p.is_dir() and LABEL.fullmatch(p.name)
                         and (p / 'site.json').is_file())
        return names

    def certificate_names(self):
        result = subprocess.run(['openssl', 'x509', '-in', str(self.live / 'fullchain.pem'),
                                 '-noout', '-ext', 'subjectAltName'], check=True,
                                capture_output=True, text=True, timeout=10)
        return set(re.findall(r'DNS:([^,\s]+)', result.stdout))

    def served_ready(self, names):
        context = ssl.create_default_context()
        for hostname in sorted(names):
            try:
                with socket.create_connection(('127.0.0.1', 443), timeout=2) as connection:
                    with context.wrap_socket(connection, server_hostname=hostname):
                        pass
            except OSError:
                return False
        return True

    def retry_state(self):
        try:
            return json.loads(self.retry.read_text())
        except (OSError, ValueError):
            return {}

    def defer(self, reason, *, rate_limited=False):
        previous = self.retry_state()
        attempts = min(10, int(previous.get('attempts', 0)) + 1)
        delay = 3600 if rate_limited else min(900, 120 * 2 ** (attempts - 1))
        record = {'attempts': attempts, 'retryAt': time.time() + delay, 'reason': reason}
        temporary = self.retry.with_suffix('.tmp')
        temporary.write_text(json.dumps(record))
        temporary.chmod(0o600)
        temporary.replace(self.retry)
        print(f'HTTPS pending: {reason}; retry in {delay}s', flush=True)
        return 1

    def reload_nginx(self):
        subprocess.run(['nginx', '-t'], check=True, capture_output=True, timeout=15)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True, timeout=20)

    def reconcile(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Consume only this request. A publish during signing writes another;
        # the path unit and timer will run again after this bounded batch.
        return self.reconcile_batch()

    def reconcile_batch(self):
        self.trigger.unlink(missing_ok=True)
        desired = self.desired_names()
        current = self.certificate_names()
        missing = desired - current
        if missing:
            if self.retry_state().get('retryAt', 0) > time.time():
                print('HTTPS pending: waiting for scheduled retry', flush=True)
                return 0
            command = ['certbot', 'certonly', '--webroot', '-w', '/var/www/letsencrypt',
                       '--cert-name', 'hatchery-aichemy', '--expand', '--non-interactive', '--agree-tos']
            for hostname in sorted(desired | current):
                command.extend(['-d', hostname])
            log_path = self.state / 'log'
            with log_path.open('ab') as log:
                log_path.chmod(0o600)
                start = log.tell()
                try:
                    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=360)
                except subprocess.TimeoutExpired:
                    return self.defer('certificate request timed out')
            if result.returncode:
                with log_path.open('rb') as log:
                    log.seek(start)
                    diagnostic = log.read()[-32000:].lower()
                limited = b'ratelimited' in diagnostic or b'too many certificates' in diagnostic or b'too many failed authorizations' in diagnostic
                return self.defer('certificate authority rate limit' if limited else 'certificate request failed', rate_limited=limited)
            try:
                self.reload_nginx()
            except (OSError, subprocess.SubprocessError):
                return self.defer('nginx could not load the new certificate')
        elif not self.served_ready(desired):
            # A previous issuance may have succeeded while nginx reload failed.
            try:
                self.reload_nginx()
            except (OSError, subprocess.SubprocessError):
                return self.defer('nginx reload failed')
        if not self.served_ready(desired):
            return self.defer('served TLS certificate is not ready')
        self.retry.unlink(missing_ok=True)
        (self.state / 'last-failure').unlink(missing_ok=True)
        if missing:
            print('HTTPS verified: ' + ', '.join(sorted(missing)), flush=True)
        return 0


def main():
    os.umask(0o077)
    # Preserve the existing Certbot network timeout configuration.
    extra = '/usr/local/lib/hatchery-certbot'
    os.environ['PYTHONPATH'] = extra + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
    with open('/var/lock/hatchery-cert-sync.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return CertSync().reconcile()


if __name__ == '__main__':
    raise SystemExit(main())
