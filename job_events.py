"""Account-scoped live events; durable jobs remain the source of truth.

Transport uses the BSD-licensed `websockets` library. Snapshot/replay generations
follow the separation used by Kimi Wire and DeepSeek Harness Remote journals.
"""
from collections import deque
from contextlib import contextmanager
from http import HTTPStatus
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit


class StreamCapacity(RuntimeError): pass


class EventHub:
    def __init__(self, max_events=256, max_bytes=2_000_000, max_channels=128,
                 max_connections=64, per_user=4):
        self.condition = threading.Condition()
        self.channels = {}
        self.max_events, self.max_bytes, self.max_channels = max_events, max_bytes, max_channels
        self.max_connections, self.per_user = max_connections, per_user
        self.connections = 0

    def _channel(self, uid):
        if uid not in self.channels:
            idle = sorted((c['seen'], key) for key, c in self.channels.items() if not c['subscribers'])
            while len(self.channels) >= self.max_channels and idle:
                self.channels.pop(idle.pop(0)[1], None)
            if len(self.channels) >= self.max_channels: raise StreamCapacity('Live channel capacity')
            self.channels[uid] = dict(epoch=secrets.token_hex(8), sequence=0,
                                     events=deque(), bytes=0, subscribers=0, seen=time.monotonic())
        channel = self.channels[uid]
        channel['seen'] = time.monotonic()
        return channel

    @staticmethod
    def cursor(channel): return f"{channel['epoch']}:{channel['sequence']}"

    def publish(self, user_id, kind, payload):
        # Serialize once, before taking the hub lock. Never send on a socket here.
        data = json.dumps({'type': kind, 'payload': payload}, ensure_ascii=False, separators=(',', ':'))
        with self.condition:
            channel = self._channel(str(user_id))
            channel['sequence'] += 1
            if len(data.encode()) > self.max_bytes:
                # A huge report is recovered through the existing authenticated API.
                data = '{"type":"resync","payload":{}}'
            frame = '{"id":' + json.dumps(self.cursor(channel)) + ',' + data[1:]
            size = len(frame.encode())
            channel['events'].append((channel['sequence'], frame, size))
            channel['bytes'] += size
            while len(channel['events']) > self.max_events or channel['bytes'] > self.max_bytes:
                channel['bytes'] -= channel['events'].popleft()[2]
            self.condition.notify_all()

    @contextmanager
    def attach(self, user_id):
        uid = str(user_id)
        with self.condition:
            channel = self._channel(uid)
            if self.connections >= self.max_connections or channel['subscribers'] >= self.per_user:
                raise StreamCapacity('Live connection capacity')
            channel['subscribers'] += 1
            self.connections += 1
        try: yield
        finally:
            with self.condition:
                channel['subscribers'] -= 1
                channel['seen'] = time.monotonic()
                self.connections -= 1

    def read(self, user_id, cursor=None, timeout=15.0, stop=None):
        """Return reset + current cursor, or ordered immutable frames after cursor."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                channel = self._channel(str(user_id))
                try:
                    epoch, raw = str(cursor).split(':')
                    sequence = int(raw)
                    earliest = channel['events'][0][0] if channel['events'] else channel['sequence'] + 1
                    valid = epoch == channel['epoch'] and earliest - 1 <= sequence <= channel['sequence']
                except (ValueError, TypeError): valid = False
                if not valid: return True, self.cursor(channel), []
                frames = [frame for seq, frame, _ in channel['events'] if seq > sequence]
                if frames or time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                    return False, self.cursor(channel), frames
                self.condition.wait(max(0, deadline - time.monotonic()))


class LiveServer:
    """A loopback-only WebSocket downlink; commands keep their idempotent HTTP API."""
    def __init__(self, hub, authenticate, port, *, heartbeat=15.0):
        self.hub, self.authenticate = hub, authenticate
        self.heartbeat = heartbeat
        self.stopping = threading.Event()
        self.sockets, self.lock = set(), threading.Lock()
        from websockets.sync.server import serve
        # Library logs can contain handshake paths/headers; application diagnostics
        # must not expose cookies or credentials.
        logger = logging.getLogger('hatchery.live')
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.server = serve(self.handle, '127.0.0.1', port, process_request=self.authorize,
                            server_header=None, max_size=2048, max_queue=2,
                            open_timeout=5, close_timeout=2, ping_interval=20, ping_timeout=20,
                            logger=logger)
        self.port = self.server.socket.getsockname()[1]

    def authorize(self, connection, request):
        if request.path != '/api/ai/events':
            return connection.respond(HTTPStatus.NOT_FOUND, 'Not found\n')
        try:
            # Cookies alone aren't sufficient for a WebSocket upgrade: bind the
            # Origin to the proxy-supplied Host, rejecting missing/null origins.
            origin = urlsplit(request.headers.get('Origin', ''))
            host = request.headers.get('Host', '')
            target = urlsplit('//'+host)
            development = (origin.scheme == 'http' and origin.hostname in ('localhost','127.0.0.1','::1')
                           and target.hostname == origin.hostname and target.port == self.port
                           and origin.port == self.port - 1)
            if origin.scheme not in ('http', 'https') or (origin.netloc != host and not development) or origin.path or origin.query or origin.fragment or origin.username:
                return connection.respond(HTTPStatus.FORBIDDEN, 'Origin rejected\n')
            user = self.authenticate(request.headers)
            if not user: return connection.respond(HTTPStatus.UNAUTHORIZED, 'Authentication required\n')
            connection.hatchery_user = str(user['id'])
        except Exception:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, 'Temporarily unavailable\n')

    def handle(self, socket):
        from websockets.exceptions import ConnectionClosed
        with self.lock: self.sockets.add(socket)
        try:
            with self.hub.attach(socket.hatchery_user):
                opening = json.loads(socket.recv(timeout=5))
                if not isinstance(opening, dict) or opening.get('type') != 'subscribe' or set(opening) - {'type','cursor'}:
                    socket.close(1008, 'Invalid subscription'); return
                cursor = opening.get('cursor')
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 80):
                    socket.close(1008, 'Invalid cursor'); return
                reset, latest, frames = self.hub.read(socket.hatchery_user, cursor, 0)
                socket.send(json.dumps({'type':'ready','payload':{'reset':reset,'cursor':latest,'protocol':1}}))
                for frame in frames: socket.send(frame)
                cursor = latest
                checked = time.monotonic()
                while not self.stopping.is_set():
                    # Reject application commands on this read channel. WebSocket
                    # ping/pong frames are handled by the library independently.
                    try:
                        socket.recv(timeout=0)
                        socket.close(1008, 'Use the authenticated command API'); return
                    except TimeoutError: pass
                    if time.monotonic() - checked >= self.heartbeat:
                        user = self.authenticate(socket.request.headers)
                        if not user or str(user['id']) != socket.hatchery_user:
                            socket.close(1008, 'Session expired'); return
                        checked = time.monotonic()
                    reset, latest, frames = self.hub.read(socket.hatchery_user, cursor, self.heartbeat, self.stopping)
                    if reset:
                        socket.send(json.dumps({'type':'ready','payload':{'reset':True,'cursor':latest,'protocol':1}}))
                    elif frames:
                        for frame in frames: socket.send(frame)
                    else: socket.send('{"type":"heartbeat","payload":{}}')
                    cursor = latest
        except StreamCapacity:
            socket.close(1013, 'Live connection limit')
        except (ConnectionClosed, TimeoutError, ValueError, TypeError):
            socket.close(1008, 'Connection ended')
        except Exception:
            socket.close(1011, 'Temporarily unavailable')
        finally:
            with self.lock: self.sockets.discard(socket)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, name='hatchery-live', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        with self.hub.condition: self.hub.condition.notify_all()
        self.server.shutdown(code=1012, reason='Server restarting')
