"""Application-side capabilities for one website's isolated dynamic runtime."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time

SOCKET = '/run/hatchery-sites/control.sock'
_capabilities = {}
_lock = threading.Lock()
READ_ONLY = frozenset({'status', 'logs', 'secret_names'})
OPERATIONS = READ_ONLY | {'exec', 'install', 'deploy', 'stop', 'set_secrets'}


def site_key(user_id):
    return hashlib.sha256(str(user_id).encode()).hexdigest()


def enabled(user_id):
    allowed = os.environ.get('HATCHERY_DYNAMIC_SITE_KEYS', '').split(',')
    return site_key(user_id) in {value.strip() for value in allowed}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path=SOCKET, timeout=120):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def call(user_id, workspace, operation, args=None):
    if not enabled(user_id):
        raise ValueError('当前网站尚未开通动态服务权限')
    if operation not in OPERATIONS | {'proxy', 'relocate'}:
        raise ValueError('未知的网站运行操作')
    connection = UnixConnection()
    try:
        body = json.dumps({'siteKey': site_key(user_id), 'workspace': str(Path(workspace).absolute()),
                           'operation': operation, 'args': args or {}}).encode()
        connection.request('POST', '/', body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read(3_000_000))
        if response.status != 200:
            raise RuntimeError(data.get('error') or '网站运行服务请求失败')
        return data
    except OSError:
        raise RuntimeError('网站隔离运行服务暂不可用') from None
    finally:
        connection.close()


@contextmanager
def capability(user_id, workspace, mode):
    """Bind a short-lived, opaque credential; the model cannot choose its site."""
    if not enabled(user_id):
        yield ''
        return
    token = secrets.token_urlsafe(40)
    with _lock:
        _capabilities[token] = (str(user_id), Path(workspace), mode, time.monotonic() + 2000)
    try:
        yield token
    finally:
        with _lock:
            _capabilities.pop(token, None)


def authorized_call(token, operation, args):
    with _lock:
        capability = _capabilities.get(token)
    if not capability or capability[3] < time.monotonic():
        raise ValueError('网站运行权限已失效')
    user_id, workspace, mode, _ = capability
    if operation not in OPERATIONS:
        raise ValueError('未知的网站运行操作')
    if mode != 'full' and operation not in READ_ONLY:
        raise ValueError('当前模式只允许查看本网站服务状态')
    return call(user_id, workspace, operation, args)
