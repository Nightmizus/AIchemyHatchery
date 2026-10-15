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
_effects = {}
_lock = threading.Lock()
READ_ONLY = frozenset({'status', 'logs', 'secret_names'})
OPERATIONS = READ_ONLY | {'exec', 'install', 'deploy', 'stop', 'set_secrets', 'delete_secrets'}


def site_key(user_id):
    return hashlib.sha256(str(user_id).encode()).hexdigest()


def enabled(user_id):
    if user_id is None or not str(user_id):
        return False
    allowed = {value.strip() for value in os.environ.get('HATCHERY_DYNAMIC_SITE_KEYS', '').split(',')}
    return '*' in allowed or site_key(user_id) in allowed


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
        body = json.dumps({'siteKey': site_key(user_id), 'ownerId': str(user_id),
                           'workspace': str(Path(workspace).absolute()),
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
        _effects[token] = {'pending': {}, 'completed': [], 'uncertain': []}
    try:
        yield token
    finally:
        with _lock:
            _capabilities.pop(token, None)
            _effects.pop(token, None)


def recovery_state(token):
    """No commands or secret values: only whether a backend write was confirmed."""
    with _lock:
        state = _effects.get(token, {})
        return {'pending': list(state.get('pending', {}).values()),
                'completed': list(state.get('completed', [])), 'uncertain': list(state.get('uncertain', []))}


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
    invocation = secrets.token_hex(12)
    changing = operation not in READ_ONLY
    if changing:
        with _lock:
            if token not in _effects:
                raise ValueError('网站运行权限已失效')
            _effects[token]['pending'][invocation] = operation
    try:
        result = call(user_id, workspace, operation, args)
    except Exception:
        if changing:
            with _lock:
                state = _effects.get(token)
                if state is not None:
                    state['uncertain'].append(operation)
        raise
    else:
        if changing:
            with _lock:
                state = _effects.get(token)
                if state is not None:
                    state['completed'].append(operation)
        return result
    finally:
        if changing:
            with _lock:
                state = _effects.get(token)
                if state is not None:
                    state['pending'].pop(invocation, None)
