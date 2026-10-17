"""Account-owned, encrypted OpenAI-compatible API settings and pinned HTTPS transport."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """CREATE TABLE IF NOT EXISTS hatchery_personal_api (
    user_id TEXT PRIMARY KEY, encrypted_config TEXT NOT NULL
)"""
_KEY_LOCK = threading.Lock()


class ConfigurationUnreadable(ValueError):
    """Stored credentials must be replaced or deleted, never silently ignored."""


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ValueError("请输入有效的 HTTPS Base URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("API 地址格式无效") from None
    if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment or port not in (None, 443):
        raise ValueError("Base URL 必须是公网 HTTPS 地址（443 端口），不能含账号、参数或片段")
    path = url.path.rstrip("/")
    if path.endswith(("/chat/completions", "/models", "/responses")):
        raise ValueError("请填写 Base URL（通常以 /v1 结尾），不要填写完整接口路径")
    return urlunsplit(("https", url.netloc.lower(), path, "", ""))


def public_addresses(base_url: str) -> list[str]:
    host = urlsplit(normalize_base_url(base_url)).hostname
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise ValueError("无法解析 API 域名，请检查 Base URL") from None
    if not addresses or any(not ipaddress.ip_address(ip).is_global or getattr(ipaddress.ip_address(ip), "ipv4_mapped", None) for ip in addresses):
        raise ValueError("API 地址必须解析到公网，不能访问本机、内网或保留地址")
    return addresses


def _cipher(root: Path) -> Fernet:
    configured = os.environ.get("HATCHERY_API_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("服务器 HATCHERY_API_ENCRYPTION_KEY 格式无效") from None
    key_path = root / ".private-api-config" / "key"
    with _KEY_LOCK:
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not key_path.exists():
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
        return Fernet(key_path.read_bytes())


class ConfigStore:
    def __init__(self, db, root: Path):
        self.db, self.root = db, root

    def load(self, user_id) -> dict | None:
        with self.db(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT encrypted_config FROM hatchery_personal_api WHERE user_id=%s", (str(user_id),))
            row = cur.fetchone()
        if not row:
            return None
        try:
            value = json.loads(_cipher(self.root).decrypt(row[0].encode()).decode())
            # Bind ciphertext to its owner as well as selecting by authenticated ID.
            if value.pop("user_id", None) != str(user_id):
                raise InvalidToken
            return value
        except (InvalidToken, ValueError, OSError):
            raise ConfigurationUnreadable("个人 API 配置无法解密，请重新填写地址、模型和 API Key 后保存，或清除配置恢复默认服务") from None

    def save(self, user_id, data: dict) -> dict:
        base = normalize_base_url(data.get("baseUrl", ""))
        model = data.get("model", "")
        key = data.get("apiKey", "")
        if not isinstance(model, str) or not model.strip() or len(model) > 160 or re.search(r"[\s\x00-\x1f\x7f]", model.strip()):
            raise ValueError("请填写模型 ID（不超过 160 字符，不能含空白）")
        if not isinstance(key, str):
            raise ValueError("API Key 格式无效")
        key = key.strip()
        if not key:
            old = self.load(user_id)
            if not old or old["base_url"] != base:
                raise ValueError("首次配置或更换 Base URL 时必须重新填写 API Key")
            key = old["api_key"]
        if not re.fullmatch(r"[!-~]{1,2048}", key):
            raise ValueError("API Key 只能包含可见 ASCII 字符，且不超过 2048 字符")
        public_addresses(base)
        config = {"base_url": base, "api_key": key, "model": model.strip()}
        encrypted = _cipher(self.root).encrypt(json.dumps({**config, "user_id": str(user_id)}).encode()).decode()
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO hatchery_personal_api(user_id,encrypted_config) VALUES(%s,%s) "
                        "ON CONFLICT(user_id) DO UPDATE SET encrypted_config=excluded.encrypted_config", (str(user_id), encrypted))
        return config

    def clear(self, user_id) -> None:
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM hatchery_personal_api WHERE user_id=%s", (str(user_id),))


def public_config(config: dict | None) -> dict:
    return {"configured": bool(config), "baseUrl": config["base_url"] if config else "",
            "model": config["model"] if config else "", "hasKey": bool(config)}


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once and connect to the checked IP; TLS still verifies the original hostname.
        host = f"[{self.host}]" if ":" in self.host else self.host
        addresses = public_addresses("https://" + host)
        self.sock = socket.create_connection((addresses[0], self.port or 443), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=ssl.create_default_context())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout=30):
    url = urlsplit(request.full_url)
    normalize_base_url(urlunsplit((url.scheme, url.netloc, "", "", "")))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTPSHandler(), _NoRedirect())
    return opener.open(request, timeout=timeout)


@contextmanager
def search_transport(env: dict, config: dict | None):
    """Keep native search parsing; route personal Messages through checked HTTPS.

    The native provider has no transport hook. Its loopback credential is random
    and task-local; the real upstream key never arrives from the client request.
    """
    if config is None:
        yield env
        return
    base = normalize_base_url(config["base_url"])
    credential = secrets.token_urlsafe(32)
    max_request, max_response = 1024 * 1024, 4 * 1024 * 1024

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass  # Neither credentials nor search payloads belong in access logs.

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, message):
            self.reply(status, json.dumps({"error": {"message": message}}, ensure_ascii=False).encode())

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("x-api-key", "").encode(), credential.encode()):
                self.error(403, "Search bridge credential is invalid")
                return
            if self.path != "/v1/messages":
                self.error(404, "Search bridge route does not exist")
                return
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isascii() or not length.isdigit() or not 0 < int(length) <= max_request:
                self.error(413, "Search request size is invalid")
                return
            try:
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    raise ValueError("Incomplete request")
                headers = {"x-api-key": config["api_key"], "Authorization": "Bearer " + config["api_key"],
                           "Content-Type": "application/json", "Accept": "application/json",
                           "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")}
                if self.headers.get("anthropic-beta"):
                    headers["anthropic-beta"] = self.headers["anthropic-beta"]
                request = urllib.request.Request(base + "/messages", data=body, headers=headers, method="POST")
                with open_request(request, timeout=60) as response:
                    payload = response.read(max_response + 1)
                    status = response.status
                if len(payload) > max_response:
                    raise ValueError("Search response is too large")
                self.reply(status, payload)
            except urllib.error.HTTPError as error:
                # Redirects are errors too; never forward Location or upstream bodies.
                error.close()
                self.error(502, f"Personal search upstream returned HTTP {error.code}")
            except (ValueError, OSError, urllib.error.URLError, http.client.HTTPException):
                self.error(502, "Personal search connection failed public HTTPS validation or could not be reached")

    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    bridge.daemon_threads = True
    thread = threading.Thread(target=lambda: bridge.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield {**env, "DEEPSEEK_SEARCH_BASE_URL": f"http://127.0.0.1:{bridge.server_port}/v1",
               "HATCHERY_DEEPSEEK_SEARCH_API_KEY": credential,
               "HATCHERY_DEEPSEEK_SEARCH_MODEL": config["model"]}
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join()


def test_connection(config: dict) -> None:
    """Read model metadata without issuing a billable generation request."""
    request = urllib.request.Request(config["base_url"] + "/models", headers={"Authorization": "Bearer " + config["api_key"]})
    try:
        with open_request(request, timeout=15) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("模型列表响应过大")
        payload = json.loads(body)
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ValueError("API 未返回 OpenAI 兼容的模型列表")
        if not any(isinstance(item, dict) and item.get("id") == config["model"] for item in models):
            raise ValueError("连接成功，但 /models 未列出所填模型；请核对模型 ID，仍可保存配置")
    except urllib.error.HTTPError as error:
        raise ValueError(f"API 连接测试失败（HTTP {error.code}），请检查地址、Key 和权限") from None
    except (urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("无法读取模型列表，请检查 API 地址或稍后重试；仍可保存配置") from None
