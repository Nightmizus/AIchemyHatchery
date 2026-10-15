#!/usr/bin/python3
"""Root-owned broker: never execute website code in the platform's namespace."""
from __future__ import annotations

import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import socket
from socketserver import ThreadingUnixStreamServer
import stat
import struct
import subprocess
import threading
import time
import uuid

APP = Path('/opt/hatchery')
STATE = Path('/var/lib/hatchery-sites')
CONTROL = Path('/run/hatchery-sites/control.sock')
KEYS = Path('/etc/hatchery-sites/allowed-sites.json')
KEY = re.compile(r'[0-9a-f]{64}')
LABEL = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
PRIVATE_IPS = '0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8 39.106.77.105/32'
HOP_HEADERS = {'connection', 'keep-alive', 'transfer-encoding', 'upgrade', 'proxy-authorization', 'proxy-authenticate', 'te', 'trailer', 'content-length'}
_locks = {}
_guard = threading.Lock()
_executions = threading.BoundedSemaphore(2)


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30, **kwargs)


def save_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)


def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {} if default is None else default


def validate_workspace(key, value):
    allowed = load_json(KEYS)
    if not isinstance(key, str) or not KEY.fullmatch(key) or key not in allowed:
        raise ValueError('This website is not authorized for a runtime')
    path = Path(value)
    if not path.is_absolute() or any(part in ('.', '..') for part in PurePosixPath(value).parts):
        raise ValueError('Invalid website workspace')
    published = APP / 'published'
    relative = path.relative_to(published)
    if not ((len(relative.parts) == 1 and LABEL.fullmatch(relative.name)) or
            relative.parts == ('.drafts', key)):
        raise ValueError('Invalid website workspace')
    current = published
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError('Workspace links are forbidden')
    if not path.is_dir() or not (path / 'site.json').is_file():
        raise ValueError('Website workspace is not initialized')
    identity = path.stat()
    if [identity.st_dev, identity.st_ino] != allowed[key]:
        raise ValueError('Workspace does not belong to this authorized website')
    return path


class Site:
    def __init__(self, key, workspace):
        self.key, self.workspace = key, validate_workspace(key, workspace)
        self.root = STATE / key
        self.username = 'hsite-' + key[:16]
        self.unit = 'hatchery-site-' + key[:24] + '.service'
        self.spec_path = self.root / 'service.json'
        self.secret_path = self.root / 'secrets.json'

    def redact(self, value):
        for secret in load_json(self.secret_path).values():
            if secret:
                value = value.replace(secret, '***')
        return value

    def provision(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            identity = pwd.getpwnam(self.username)
        except KeyError:
            run(['useradd', '--system', '--no-create-home', '--home-dir', '/data',
                 '--shell', '/usr/sbin/nologin', self.username])
            identity = pwd.getpwnam(self.username)
        self.identity = identity
        for name in ('rootfs', 'data', 'socket'):
            (self.root / name).mkdir(exist_ok=True, mode=0o755)
        # A bounded filesystem contains all runtime writes. Website source is RO
        # to processes and remains editable through the confined website tools.
        volume = self.root / 'data.ext4'
        if not volume.exists():
            with volume.open('xb') as stream:
                stream.truncate(512 * 1024 * 1024)
            run(['mkfs.ext4', '-q', '-F', '-m', '0', str(volume)])
        if not os.path.ismount(self.root / 'data'):
            run(['mount', '-o', 'loop,nodev,nosuid', str(volume), str(self.root / 'data')])
        for name in ('data', 'socket'):
            os.chown(self.root / name, identity.pw_uid, identity.pw_gid)
            (self.root / name).chmod(0o700)
        rootfs = self.root / 'rootfs'
        for name in ('usr/bin', 'usr/lib', 'usr/lib64', 'usr/share', 'etc/ssl/certs',
                     'app', 'data', 'run/site', 'tmp', 'var/tmp', 'proc', 'dev'):
            (rootfs / name).mkdir(parents=True, exist_ok=True)
        for name, destination in (('bin', 'usr/bin'), ('lib', 'usr/lib'), ('lib64', 'usr/lib64')):
            if not (rootfs / name).exists():
                (rootfs / name).symlink_to(destination)
        (rootfs / 'etc/passwd').write_text(f'{self.username}:x:{identity.pw_uid}:{identity.pw_gid}:Website:/data:/bin/sh\n')
        (rootfs / 'etc/group').write_text(f'{self.username}:x:{identity.pw_gid}:\n')
        (rootfs / 'etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nhosts: files dns\n')
        (rootfs / 'etc/hosts').write_text('127.0.0.1 localhost\n::1 localhost\n')
        (rootfs / 'etc/resolv.conf').write_text('nameserver 1.1.1.1\nnameserver 8.8.8.8\n')
        rootfs.chmod(0o755)
        for path in rootfs.rglob('*'):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        # Files written by the editor use a private umask. Grant this unique UID
        # read/traverse access without adding it to the platform's group.
        run(['setfacl', '-R', '-P', '-m', f'u:{identity.pw_uid}:r-X', str(self.workspace)])
        return identity

    def properties(self, *, network=False):
        identity = self.identity
        rootfs = self.root / 'rootfs'
        ro = [f'{self.workspace}:/app']
        ro += [f'{name}:{name}' for name in ('/usr/bin', '/usr/lib', '/usr/lib64', '/usr/share', '/etc/ssl/certs') if Path(name).exists()]
        props = {
            'User': str(identity.pw_uid), 'Group': str(identity.pw_gid),
            'RootDirectory': str(rootfs), 'WorkingDirectory': '/app',
            'BindReadOnlyPaths': ' '.join(ro),
            'BindPaths': f'{self.root / "data"}:/data {self.root / "socket"}:/run/site',
            'MountAPIVFS': 'yes', 'PrivateDevices': 'yes', 'PrivateNetwork': 'no' if network else 'yes',
            'ProtectSystem': 'strict', 'ReadWritePaths': '/data /run/site',
            'TemporaryFileSystem': '/tmp:rw,size=64M,mode=1777 /var/tmp:rw,size=16M,mode=1777',
            'NoNewPrivileges': 'yes', 'CapabilityBoundingSet': '', 'AmbientCapabilities': '',
            'RestrictNamespaces': 'yes', 'RestrictSUIDSGID': 'yes',
            'ProtectKernelTunables': 'yes', 'ProtectKernelModules': 'yes', 'ProtectKernelLogs': 'yes',
            'ProtectControlGroups': 'yes', 'ProtectProc': 'invisible', 'ProcSubset': 'pid',
            'LockPersonality': 'yes', 'UMask': '0077', 'TasksMax': '64',
            'MemoryMax': '192M', 'MemorySwapMax': '128M', 'CPUQuota': '50%',
            'LimitFSIZE': '128M', 'LimitNOFILE': '256', 'Slice': 'hatchery-sites.slice',
            'RestrictAddressFamilies': 'AF_UNIX AF_INET AF_INET6',
            'LogRateLimitIntervalSec': '1s', 'LogRateLimitBurst': '30',
            'TimeoutStopSec': '5', 'KillMode': 'control-group',
        }
        if network:
            # Only the package installer has egress. Never allow private hosts,
            # cloud metadata, this machine, or platform loopback services.
            props['IPAddressDeny'] = PRIVATE_IPS
        return props

    def command(self, argv, *, network=False, background=False):
        unit = self.unit if background else 'hatchery-site-exec-' + uuid.uuid4().hex[:20]
        command = ['systemd-run', '--quiet', '--collect', '--unit', unit]
        if not background:
            command += ['--wait', '--pipe']
        props = self.properties(network=network)
        props.update({'Restart': 'on-failure', 'RestartSec': '3', 'StartLimitBurst': '3'} if background else {'RuntimeMaxSec': '90'})
        for name, value in props.items():
            command += ['--property', name + '=' + value]
        env = {'PATH': '/usr/bin:/bin', 'SOCKET_PATH': '/run/site/app.sock', 'SITE_DATA_DIR': '/data',
               'DATABASE_PATH': '/data/site.sqlite3', 'PYTHONPATH': '/data/python',
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
               'NODE_PATH': '/data/node/node_modules', 'NODE_OPTIONS': '--max-old-space-size=96',
               'NPM_CONFIG_CACHE': '/data/npm-cache', 'PIP_CACHE_DIR': '/data/pip-cache'}
        env.update(load_json(self.secret_path))
        # Secrets are read by PID 1 from a root-only file, never command arguments.
        envfile = self.root / 'environment'
        envfile.write_text('\n'.join(name + '=' + json.dumps(value) for name, value in env.items()) + '\n')
        envfile.chmod(0o600)
        command += ['--property', 'EnvironmentFile=' + str(envfile), '--', *argv]
        return command

    def status(self):
        result = subprocess.run(['systemctl', 'show', self.unit, '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result'],
                                capture_output=True, text=True, timeout=10)
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        spec = load_json(self.spec_path)
        return {'configured': bool(spec), 'active': state.get('ActiveState') == 'active',
                'state': state.get('SubState', 'not-deployed'), 'runtime': spec.get('runtime'),
                'entry': spec.get('entry'), 'apiPrefix': '/_api/', 'dataDirectory': '/data',
                'databasePath': '/data/site.sqlite3', 'secrets': sorted(load_json(self.secret_path)),
                'sourceWritableFromCommands': False, 'privateNetwork': True}

    def execute(self, args, *, install=False):
        self.provision()
        if install:
            manager = args.get('manager')
            packages = args.get('packages')
            if not isinstance(packages, list) or not 1 <= len(packages) <= 20:
                raise ValueError('Provide 1–20 package names')
            pattern = r'[a-zA-Z0-9][a-zA-Z0-9_.-]*(?:==[a-zA-Z0-9.*+_-]+)?' if manager == 'pip' else r'(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9][a-zA-Z0-9_.-]*(?:@[a-zA-Z0-9.*^~+_-]+)?'
            if any(not isinstance(p, str) or not re.fullmatch(pattern, p) or len(p) > 160 for p in packages):
                raise ValueError('Only registry package names and versions are allowed')
            if manager == 'pip':
                argv = ['/usr/bin/python3', '-m', 'pip', 'install', '--target', '/data/python', '--only-binary=:all:', '--disable-pip-version-check', '--', *packages]
            elif manager == 'npm':
                argv = ['/usr/bin/npm', 'install', '--prefix', '/data/node', '--no-fund', '--no-audit', '--ignore-scripts', '--', *packages]
            else:
                raise ValueError('Package manager must be pip or npm')
        else:
            text = args.get('command')
            if not isinstance(text, str) or not 1 <= len(text) <= 8000 or '\0' in text:
                raise ValueError('Command must be 1–8000 characters')
            argv = ['/bin/sh', '-c', text]
        if not _executions.acquire(blocking=False):
            raise ValueError('Website command slots are busy; retry later')
        try:
            # File-backed output prevents an untrusted process exhausting broker RAM.
            output = self.root / ('command-' + uuid.uuid4().hex + '.log')
            try:
                with output.open('wb') as stream:
                    command = self.command(argv, network=install)
                    result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=105)
                with output.open('rb') as stream:
                    text = stream.read(16000).decode('utf-8', 'replace')
                if result.returncode and not text.strip():
                    unit = command[command.index('--unit') + 1]
                    journal = subprocess.run(['journalctl', '-u', unit, '-n', '12', '--no-pager', '-o', 'cat'],
                                             capture_output=True, text=True, timeout=10)
                    text = journal.stdout[-16000:]
                return {'exitCode': result.returncode, 'output': self.redact(text)}
            finally:
                output.unlink(missing_ok=True)
        finally:
            _executions.release()

    def connect_backend(self):
        identity = pwd.getpwnam(self.username)
        connection = http.client.HTTPConnection('localhost', timeout=15)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.sock.settimeout(15)
            connection.sock.connect(str(self.root / 'socket/app.sock'))
            _, peer_uid, _ = struct.unpack('3i', connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_uid != identity.pw_uid:
                raise ValueError('Backend socket does not belong to this website')
            return connection
        except Exception:
            connection.close()
            raise

    def proxy(self, args):
        path = args.get('path', '/')
        method = args.get('method', 'GET')
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n\0'):
            raise ValueError('Invalid backend request path')
        if method not in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            raise ValueError('Unsupported request method')
        body = base64.b64decode(args.get('body', ''), validate=True)
        if len(body) > 1_000_000:
            raise ValueError('Backend request exceeds 1 MB')
        headers = {name: value for name, value in args.get('headers', {}).items()
                   if isinstance(name, str) and isinstance(value, str) and name.lower() not in HOP_HEADERS
                   and not any(c in name + value for c in '\r\n\0')}
        connection = self.connect_backend()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Backend response exceeds 2 MB')
            return {'status': response.status, 'headers': [[k, v] for k, v in response.getheaders() if k.lower() not in HOP_HEADERS],
                    'body': base64.b64encode(data).decode()}
        finally:
            connection.close()

    def deploy(self, args):
        entry, runtime = args.get('entry'), args.get('runtime')
        if not isinstance(entry, str) or not entry.startswith('backend/') or any(p in ('', '.', '..') for p in entry.split('/')) or '\\' in entry:
            raise ValueError('Entry must be a relative source path under backend/')
        target = self.workspace
        for part in entry.split('/'):
            target /= part
            if target.is_symlink():
                raise ValueError('Entry links are forbidden')
        if not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError('Backend entry file is missing or linked')
        if runtime not in ('python', 'node'):
            raise ValueError('Runtime must be python or node')
        self.provision()
        subprocess.run(['systemctl', 'stop', self.unit], capture_output=True, timeout=20)
        (self.root / 'socket/app.sock').unlink(missing_ok=True)
        command = ['/usr/bin/python3' if runtime == 'python' else '/usr/bin/node', '/app/' + entry]
        run(self.command(command, background=True))
        deadline = time.monotonic() + 20
        health = args.get('healthPath', '/')
        while time.monotonic() < deadline:
            try:
                result = self.proxy({'path': health})
                if result['status'] < 500 and (not args.get('healthPath') or result['status'] < 400):
                    spec = {'runtime': runtime, 'entry': entry, 'workspace': str(self.workspace), 'enabled': True}
                    save_json(self.spec_path, spec)
                    return {**self.status(), 'healthStatus': result['status']}
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(.25)
        subprocess.run(['systemctl', 'stop', self.unit], capture_output=True, timeout=20)
        raise RuntimeError('Backend did not become healthy. Listen on os.environ["SOCKET_PATH"] / process.env.SOCKET_PATH; inspect site_service logs.')

    def dispatch(self, operation, args):
        if operation == 'status':
            return self.status()
        if operation == 'secret_names':
            return {'names': sorted(load_json(self.secret_path))}
        if operation == 'logs':
            result = run(['journalctl', '-u', self.unit, '-n', '60', '--no-pager', '-o', 'cat'])
            return {'output': self.redact(result.stdout[-16000:])}
        if operation in ('exec', 'install'):
            return self.execute(args, install=operation == 'install')
        if operation == 'deploy':
            return self.deploy(args)
        if operation == 'proxy':
            return self.proxy(args)
        if operation == 'stop':
            subprocess.run(['systemctl', 'stop', self.unit], capture_output=True, timeout=20)
            spec = load_json(self.spec_path)
            if spec:
                save_json(self.spec_path, {**spec, 'enabled': False})
            return self.status()
        if operation == 'relocate':
            spec = load_json(self.spec_path)
            if not spec:
                return {'configured': False}
            save_json(self.spec_path, {**spec, 'workspace': str(self.workspace)})
            return self.deploy(spec) if spec.get('enabled') else self.status()
        if operation == 'set_secrets':
            values = args.get('values')
            if not isinstance(values, dict) or not 1 <= len(values) <= 32:
                raise ValueError('Provide 1–32 website secrets')
            reserved = {'PATH', 'HOME', 'SHELL', 'SOCKET_PATH', 'DATABASE_PATH', 'SITE_DATA_DIR', 'NEON_DATABASE_URL', 'SSO_SECRET', 'MIAODA_SITE_RUNTIME_TOKEN'}
            for name, value in values.items():
                if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', name) or name in reserved or name.startswith(('LD_', 'PYTHON', 'NODE_', 'NPM_', 'PIP_', 'MIAODA_', 'HATCHERY_')):
                    raise ValueError('Reserved or invalid secret name')
                if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
                    raise ValueError('Secret must be a single-line string up to 8192 characters')
            self.provision()
            combined = {**load_json(self.secret_path), **values}
            if len(combined) > 32 or sum(map(len, combined.values())) > 65536:
                raise ValueError('Website secret storage is full')
            save_json(self.secret_path, combined)
            return {'names': sorted(combined), 'restartRequired': self.status()['active']}
        if operation == 'delete_secrets':
            names = args.get('names')
            if not isinstance(names, list) or not 1 <= len(names) <= 32 or any(not isinstance(name, str) for name in names):
                raise ValueError('Provide 1–32 website secret names')
            values = load_json(self.secret_path)
            for name in names:
                values.pop(name, None)
            if self.secret_path.exists():
                save_json(self.secret_path, values)
            return {'names': sorted(values), 'restartRequired': self.status()['active']}
        raise ValueError('Unknown website runtime operation')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log tool arguments, commands, request bodies or secrets.

    def do_POST(self):
        try:
            _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, pwd.getpwnam('hatchery').pw_uid):
                raise ValueError('Runtime caller is not authorized')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1_500_000:
                raise ValueError('Invalid runtime request size')
            request = json.loads(self.rfile.read(length))
            key = request.get('siteKey', '')
            with _guard:
                lock = _locks.setdefault(key, threading.RLock())
            site = Site(key, request.get('workspace', ''))
            operation = request.get('operation')
            # Public requests must not be held behind a package installation.
            if operation == 'proxy':
                result = site.dispatch(operation, request.get('args', {}))
            else:
                with lock:
                    result = site.dispatch(operation, request.get('args', {}))
            status, data = 200, result
        except (ValueError, RuntimeError) as exc:
            status, data = 400, {'error': str(exc)}
        except Exception as exc:
            status, data = 502, {'error': 'Website runtime failed: ' + type(exc).__name__}
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONTROL.parent.mkdir(parents=True, exist_ok=True)
    CONTROL.unlink(missing_ok=True)
    http = ThreadingUnixStreamServer(str(CONTROL), Handler)
    os.chown(CONTROL, 0, pwd.getpwnam('hatchery').pw_gid); CONTROL.chmod(0o660)
    os.chown(CONTROL.parent, 0, pwd.getpwnam('hatchery').pw_gid); CONTROL.parent.chmod(0o750)
    # Persistent specs survive application/server restarts; start only authorized sites.
    for key in load_json(KEYS):
        if KEY.fullmatch(key):
            spec = load_json(STATE / key / 'service.json')
            if spec.get('enabled'):
                try:
                    site = Site(key, spec['workspace'])
                    if not site.status()['active']:
                        site.deploy(spec)
                except Exception as exc:
                    print('Website restore failed: ' + type(exc).__name__, flush=True)
    http.serve_forever()


if __name__ == '__main__':
    main()
