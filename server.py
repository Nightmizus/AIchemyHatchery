from __future__ import annotations

import argparse
import base64
import getpass
import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from email.mime.text import MIMEText
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
import urllib.request
import urllib.error
import urllib.parse as urlparse_mod

from deepseek_harness_adapter import harness_status, resolve_llm_provider, run_deepseek_harness

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
HOST = "127.0.0.1"
PORT = 4173

# Neon PostgreSQL connection (shared with sdszwebsite)
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL", "")
if not NEON_DATABASE_URL:
    # Try reading from sdszwebsite .env
    sdsz_env = Path.home() / "Project" / "sdszwebsite" / ".env"
    if sdsz_env.exists():
        for line in sdsz_env.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                NEON_DATABASE_URL = line.split("=", 1)[1].strip().strip('"')
                break

@contextmanager
def neon_db():
    """Yield a Neon PostgreSQL connection with RealDictCursor."""
    conn = psycopg2.connect(NEON_DATABASE_URL)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
RUNTIME_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
PASSWORD_ITERATIONS = 310_000
CONSOLE_SESSION_DAYS = 7
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
INVITE_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")
PREVIEW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
AI_LOCK = threading.Lock()
AI_RUN_LOCK = threading.Lock()
AI_RUN_JOBS_LOCK = threading.Lock()
AI_PROPOSALS: dict[str, dict] = {}
AI_BACKUPS: dict[str, dict[str, str]] = {}
AI_RUN_JOBS: dict[str, dict] = {}
AI_SOURCE_FILES = ("index.html", "styles.css", "mica.css", "ai-chat.css", "auth.js", "script.js", "viewer.html", "viewer.js", "server.py")
AI_EDITABLE_SOURCE_FILES = tuple(name for name in AI_SOURCE_FILES if name not in {"server.py", "auth.js"})
PUBLIC_STATIC_PATHS = frozenset(("/index.html", "/styles.css", "/mica.css", "/ai-chat.css", "/auth.js", "/script.js", "/viewer.js"))

# 写死的站长账号：数字校园号为 20264689 的用户始终是站长（管理员），
# 不依赖数据库里的 isAdmin 标记，也不能被停用。
SITE_OWNER_CAMPUS_ID = "20264689"


def is_site_owner_campus_id(campus_id) -> bool:
    return str(campus_id or "").strip() == SITE_OWNER_CAMPUS_ID


# 共享 "User" 表属于 sdszwebsite，禁止 CREATE/ALTER；不同环境的列可能不同
# （例如生产库没有 lastLoginAt）。所有可选列读写前先按真实列名探测。
USER_COLUMNS: frozenset | None = None


def user_columns() -> frozenset:
    global USER_COLUMNS
    if USER_COLUMNS is None:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'User'")
                USER_COLUMNS = frozenset(str(row[0]) for row in cur.fetchall())
    return USER_COLUMNS


def user_column_name(name: str) -> str | None:
    """返回 "User" 表中与 name 匹配（忽略大小写）的真实列名；不存在返回 None。"""
    lowered = name.lower()
    for column in user_columns():
        if column.lower() == lowered:
            return column
    return None


def row_value(row: dict, name: str, default=None):
    """按忽略大小写的方式从 RealDict 行里取可选列的值。"""
    if name in row:
        return row[name]
    lowered = name.lower()
    for key in row:
        if key.lower() == lowered:
            return row[key]
    return default

# SSO (shared with sdszwebsite)
SSO_SECRET = os.environ.get("SSO_SECRET", "")
SDSZ_BASE_URL = os.environ.get("SDSZ_BASE_URL", "https://sdsz.groovin.cn")


def read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


def write_text_exact(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        target.write(content)


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def json_time(value):
    """Format a DB timestamp (datetime or TEXT) for JSON output."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def password_digest(password: str, salt: bytes | None = None, iterations: int = 0) -> tuple[str, str]:
    """bcrypt hash. Salt/iterations args kept for signature compat but unused."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    return "", hashed.decode("ascii")


def password_matches(password: str, salt_hex: str, expected_hex: str, iterations: int = 0) -> bool:
    """Verify password against stored bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), expected_hex.encode("ascii"))
    except (ValueError, TypeError):
        return False
        return False
    return hmac.compare_digest(actual, expected_hex)


def new_preview_id(cur) -> str:
    while True:
        preview_id = secrets.token_urlsafe(24)
        cur.execute("SELECT 1 FROM hatchery_user_extras WHERE preview_id = %s", (preview_id,))
        if not cur.fetchone():
            return preview_id


def initialize_database() -> None:
    if not NEON_DATABASE_URL:
        sys.exit("缺少 Neon 连接串：请设置 NEON_DATABASE_URL 环境变量，或在 ~/Project/sdszwebsite/.env 写入 DATABASE_URL=...")
    # "User" / "CampusUser" 属于 sdszwebsite，禁止 CREATE/ALTER。
    ddl = """
    CREATE TABLE IF NOT EXISTS hatchery_user_extras(
        user_id TEXT PRIMARY KEY,
        preview_id TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_drafts(
        user_id TEXT PRIMARY KEY,
        data_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_previews(
        user_id TEXT PRIMARY KEY,
        data_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_audit_events(
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT,
        event TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_users(
        id BIGSERIAL PRIMARY KEY,
        site_username TEXT NOT NULL,
        username TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(site_username, username)
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_sessions(
        token_hash TEXT PRIMARY KEY,
        site_username TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_settings(
        site_username TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(site_username, key)
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_invitations(
        code TEXT PRIMARY KEY,
        site_username TEXT NOT NULL,
        created_by BIGINT NOT NULL,
        created_at TEXT NOT NULL,
        used_by BIGINT,
        used_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_audit_log(
        id BIGSERIAL PRIMARY KEY,
        site_username TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_site_deployments(
        id BIGSERIAL PRIMARY KEY,
        site_username TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        page_count INTEGER NOT NULL,
        published_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS console_sessions(
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT,
        ip_address TEXT,
        user_agent TEXT
    );
    CREATE TABLE IF NOT EXISTS email_verifications(
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        code TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hatchery_ai_usage(
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        job_id TEXT,
        prompt TEXT NOT NULL,
        attachments_json TEXT NOT NULL DEFAULT '[]',
        provider TEXT,
        model TEXT,
        input_tokens BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        total_tokens BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS hatchery_ai_usage_user_idx ON hatchery_ai_usage(user_id, created_at);
    """
    with neon_db() as conn:
        with conn.cursor() as cur:
            for statement in ddl.split(";"):
                if statement.strip():
                    cur.execute(statement)


def console_user_count() -> int:
    with neon_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM "User"')
            return int(cur.fetchone()[0])


def create_console_admin(username: str, password: str) -> None:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名必须为 3–32 位字母、数字、下划线或连字符")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度必须为 8–128 位")
    _, digest = password_digest(password)
    with neon_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    'INSERT INTO "User"(id,email,name,password,grade,classGroup,initials,avatarColor,"isAdmin","isOwner","createdAt") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (f"local_{secrets.token_hex(8)}", f"{username}@local", username, digest, "", "", "", "#E8622A", True, False, datetime.now(timezone.utc).replace(tzinfo=None)),
                )
            except psycopg2.IntegrityError as error:
                raise ValueError(f"用户 {username} 已存在") from error


def audit_event(cur, user_id: str | None, event: str, detail: dict | None = None) -> None:
    cur.execute(
        "INSERT INTO hatchery_audit_events(user_id,event,detail_json,created_at) VALUES(%s,%s,%s,%s)",
        (user_id, event, json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")), iso_time()),
    )


def record_ai_usage(user_id: str, job_id: str | None, prompt: str, attachments: list[dict], provider: str, model: str, usage: dict | None, status: str) -> None:
    """归档一次 AI 输入及其 token 用量；统计写库失败不影响主流程。"""
    try:
        input_tokens = max(0, int((usage or {}).get("inputTokens") or 0))
        output_tokens = max(0, int((usage or {}).get("outputTokens") or 0))
        attachment_meta = [
            {"name": str(item.get("name", "附件"))[:180], "kind": str(item.get("kind", "text")), "size": len(str(item.get("content", "")))}
            for item in (attachments or [])
            if isinstance(item, dict)
        ]
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hatchery_ai_usage(user_id,job_id,prompt,attachments_json,provider,model,input_tokens,output_tokens,total_tokens,status,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(user_id),
                        job_id,
                        prompt[:20000],
                        json.dumps(attachment_meta, ensure_ascii=False, separators=(",", ":")),
                        provider or None,
                        model or None,
                        input_tokens,
                        output_tokens,
                        input_tokens + output_tokens,
                        status,
                        iso_time(),
                    ),
                )
    except Exception as error:
        print(f"[ai-usage] 归档失败：{error}", file=sys.stderr)


def current_ai_provider_model() -> tuple[str, str]:
    try:
        provider_name, provider = resolve_llm_provider()
        return provider_name, os.environ.get(provider["model_env"], "").strip() or provider["default_model"]
    except Exception:
        return "", ""


def initialize_site_account(site_username: str, owner_username: str) -> dict | None:
    """Ensure the Neon-backed site account exists; return first-login credentials once."""
    if not USERNAME_PATTERN.fullmatch(site_username):
        raise ValueError("站点用户名格式无效")
    initial_password: str | None = None
    with neon_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hatchery_site_settings(site_username,key,value,updated_at) VALUES(%s,'registration_mode','open',%s)"
                " ON CONFLICT (site_username,key) DO NOTHING",
                (site_username, iso_time()),
            )
            cur.execute("SELECT COUNT(*) FROM hatchery_site_users WHERE site_username = %s", (site_username,))
            if int(cur.fetchone()[0]) == 0:
                initial_password = secrets.token_urlsafe(12)
                _, digest = password_digest(initial_password)
                cur.execute(
                    "INSERT INTO hatchery_site_users(site_username,username,password_hash,role,created_at) VALUES(%s,%s,%s,'owner',%s)",
                    (site_username, owner_username, digest, iso_time()),
                )
    if initial_password is None:
        return None
    return {"username": owner_username, "password": initial_password}


def site_account_initialized(site_username: str) -> bool:
    with neon_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM hatchery_site_settings WHERE site_username = %s LIMIT 1", (site_username,))
            return cur.fetchone() is not None


def record_site_audit(site_username: str, actor: str, action: str, target: str = "", detail: str = "") -> None:
    with neon_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hatchery_site_audit_log(site_username,actor,action,target,detail,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                (site_username, actor, action, target[:200], detail[:1000], iso_time()),
            )


def record_site_deployment(site_username: str, site_data: dict, published_by: str) -> None:
    serialized = json.dumps(site_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with neon_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hatchery_site_deployments(site_username,content_hash,page_count,published_by,created_at) VALUES(%s,%s,%s,%s,%s)",
                (site_username, content_hash, len(site_data.get("pages", [])), published_by, iso_time()),
            )
            cur.execute(
                "DELETE FROM hatchery_site_deployments WHERE site_username=%s AND id NOT IN (SELECT id FROM hatchery_site_deployments WHERE site_username=%s ORDER BY id DESC LIMIT 100)",
                (site_username, site_username),
            )


def migrate_existing_site_accounts() -> None:
    if not PUBLISHED.exists():
        return
    for site_file in PUBLISHED.glob("*/site.json"):
        site_username = site_file.parent.name
        if USERNAME_PATTERN.fullmatch(site_username):
            initialize_site_account(site_username, site_username)


load_env()
if not NEON_DATABASE_URL:
    NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL", "")
HOST = os.environ.get("ALCHEMY_HATCHERY_HOST", "127.0.0.1").strip() or "127.0.0.1"
SECURE_COOKIES = os.environ.get("ALCHEMY_HATCHERY_SECURE_COOKIES", "false").strip().lower() in ("1", "true", "yes", "on")
COOKIE_SECURITY_SUFFIX = "; Secure" if SECURE_COOKIES else ""
try:
    PORT = int(os.environ.get("ALCHEMY_HATCHERY_PORT", "4173"))
except ValueError:
    PORT = 4173
class AIchemyHatcheryHandler(SimpleHTTPRequestHandler):
    server_version = "AIchemyHatcheryLocal/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def end_headers(self) -> None:
        static_path = urlparse(self.path).path
        if static_path == "/" or static_path in PUBLIC_STATIC_PATHS:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/index.html"
            super().do_HEAD()
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self.path = parsed.path
            super().do_HEAD()
            return
        self.send_error(404, "Not found")

    def send_json(self, value: object, status: int = 200, headers: dict[str, str | list[str]] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, header_value in (headers or {}).items():
            for item in header_value if isinstance(header_value, list) else [header_value]:
                self.send_header(key, item)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 25_000_000:
            raise ValueError("请求内容为空或过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def console_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get("alchemy_hatchery_console_session")
        return token.value if token else None

    def console_user(self) -> dict | None:
        token = self.console_token()
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = iso_time()
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("DELETE FROM console_sessions WHERE expires_at <= %s", (now,))
                cur.execute(
                    """
                    SELECT u.*, cs.expires_at, cs.token_hash
                    FROM console_sessions cs JOIN "User" u ON u.id = cs.user_id
                    WHERE cs.token_hash = %s AND cs.expires_at > %s
                    """,
                    (token_hash, now),
                )
                row = cur.fetchone()
                if row and row_value(row, "bannedUntil") is not None and not is_site_owner_campus_id(row.get("campusId")):
                    cur.execute("DELETE FROM console_sessions WHERE token_hash = %s", (token_hash,))
                    row = None
                elif row:
                    cur.execute("UPDATE console_sessions SET last_seen_at = %s WHERE token_hash = %s", (now, token_hash))
                    row = dict(row)
                    row["username"] = row.get("name")
                    if is_site_owner_campus_id(row.get("campusId")):
                        row["isAdmin"] = True
                    row["role"] = "admin" if row.get("isAdmin") else "user"
                    row["status"] = "active"
                return row

    def require_console_user(self, admin: bool = False) -> dict | None:
        user = self.console_user()
        if not user:
            self.send_json({"error": "请先登录炼丹社Hatchery控制台"}, 401)
            return None
        if admin and user["role"] != "admin":
            self.send_json({"error": "只有管理员可以访问此功能"}, 403)
            return None
        return user

    def issue_console_session(self, user_id: str | int, remember: bool = True) -> dict[str, str]:
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created_at = utc_now()
        max_age = CONSOLE_SESSION_DAYS * 86400 if remember else 12 * 3600
        expires_at = created_at + (timedelta(days=CONSOLE_SESSION_DAYS) if remember else timedelta(hours=12))
        ip_address = self.client_address[0] if self.client_address else "unknown"
        user_agent = self.headers.get("User-Agent", "")[:300]
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO console_sessions(token_hash,user_id,created_at,expires_at,last_seen_at,ip_address,user_agent) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (token_hash, str(user_id), iso_time(created_at), iso_time(expires_at), iso_time(created_at), ip_address, user_agent),
                )
        return {
            "Set-Cookie": (
                f"alchemy_hatchery_console_session={token}; Path=/; Max-Age={max_age}; "
                f"HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}"
            )
        }

    def public_user(self, user: dict) -> dict:
        # user 必须来自 SELECT * FROM "User"（列名以数据库实际为准）
        def get(name, default=None):
            return row_value(user, name, default)
        is_admin = bool(get("isAdmin")) or is_site_owner_campus_id(get("campusId"))
        return {
            "id": get("id"),
            "username": get("name"),
            "role": is_admin and "admin" or "user",
            "status": get("bannedUntil") and "disabled" or "active",
            "createdAt": json_time(get("createdAt", "")),
            "lastLoginAt": json_time(get("lastLoginAt")),
            "email": get("email"),
            "campusId": get("campusId"),
            "realName": get("realName"),
            "nameEn": get("nameEn"),
            "grade": get("grade", ""),
            "classGroup": get("classGroup", ""),
            "initials": get("initials", ""),
            "avatarColor": get("avatarColor", "#E8622A"),
            "avatarUrl": get("avatarUrl"),
            "bio": get("bio", ""),
            "gender": get("gender"),
            "identityType": get("identityType"),
            "currentGrade": get("currentGrade"),
            "currentClass": get("currentClass"),
            "graduationYear": get("graduationYear"),
        }

    def attach_preview_id(self, payload: dict, user_id: str) -> dict:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT preview_id FROM hatchery_user_extras WHERE user_id = %s", (str(user_id),))
                row = cur.fetchone()
        payload["previewId"] = row[0] if row else None
        return payload

    def validate_credentials(self, username: object, password: object) -> tuple[str, str]:
        normalized_username = str(username or "").strip()
        normalized_password = str(password or "")
        if not USERNAME_PATTERN.fullmatch(normalized_username):
            raise ValueError("用户名需为 3–32 位字母、数字、下划线或短横线")
        if len(normalized_password) < 8 or len(normalized_password) > 128:
            raise ValueError("密码长度需为 8–128 位")
        return normalized_username, normalized_password

    def login_is_limited(self) -> bool:
        address = self.client_address[0] if self.client_address else "unknown"
        cutoff = time.time() - 300
        with LOGIN_ATTEMPTS_LOCK:
            recent = [item for item in LOGIN_ATTEMPTS.get(address, []) if item > cutoff]
            LOGIN_ATTEMPTS[address] = recent
            return len(recent) >= 10

    def record_login_failure(self) -> None:
        address = self.client_address[0] if self.client_address else "unknown"
        with LOGIN_ATTEMPTS_LOCK:
            LOGIN_ATTEMPTS.setdefault(address, []).append(time.time())

    def clear_login_failures(self) -> None:
        address = self.client_address[0] if self.client_address else "unknown"
        with LOGIN_ATTEMPTS_LOCK:
            LOGIN_ATTEMPTS.pop(address, None)

    def handle_auth_login(self) -> None:
        if self.login_is_limited():
            self.send_json({"error": "登录尝试过于频繁，请 5 分钟后再试"}, 429)
            return
        data = self.read_json()
        identifier = str(data.get("identifier", "") or data.get("username", "")).strip()
        password = str(data.get("password", ""))
        remember = bool(data.get("remember", True))
        row = None
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    'SELECT * FROM "User" WHERE name = %s OR email = %s OR "campusId" = %s',
                    (identifier, identifier, identifier),
                )
                row = cur.fetchone()
        # Always verify against bcrypt even when user doesn't exist (timing safety)
        _dummy_hash = "$2a$12$za1.vQf.3iQH5HltnMbzqOfFBZdLmew8nOKJWJaq7IhqcjZzSyXhy"
        stored_hash = row["password"] if row else _dummy_hash
        if not password_matches(password, "", stored_hash, 0):
            self.record_login_failure()
            self.send_json({"error": "用户名或密码错误"}, 401)
            return
        if not row:
            self.record_login_failure()
            self.send_json({"error": "用户名或密码错误"}, 401)
            return
        if row_value(row, "bannedUntil") is not None:
            self.send_json({"error": "此账号已被管理员停用"}, 403)
            return
        self.clear_login_failures()
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                last_login_col = user_column_name("lastLoginAt")
                if last_login_col:
                    cur.execute(f'UPDATE "User" SET "{last_login_col}" = %s WHERE id = %s', (iso_time(), row["id"]))
                # Fetch updated user for public_user
                cur.execute('SELECT * FROM "User" WHERE id = %s', (row["id"],))
                row = cur.fetchone()
        headers = self.issue_console_session(row["id"], remember)
        self.send_json({"ok": True, "user": self.attach_preview_id(self.public_user(row), row["id"])}, headers=headers)

    def handle_auth_register(self) -> None:
        if self.login_is_limited():
            self.send_json({"error": "操作过于频繁，请 5 分钟后再试"}, 429)
            return
        data = self.read_json()
        username, password = self.validate_credentials(data.get("username"), data.get("password"))
        campus_id = str(data.get("campusId", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        code = str(data.get("code", "")).strip()
        grade = str(data.get("grade", "")).strip()
        class_group = str(data.get("classGroup", "")).strip()
        real_name = str(data.get("realName", "")).strip()

        if not campus_id:
            raise ValueError("请输入数字校园号")
        if not email:
            raise ValueError("请输入邮箱")
        if not code:
            raise ValueError("请输入邮箱验证码")

        # Validate campus ID & email verification
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM "CampusUser" WHERE "campusId" = %s', (campus_id,))
                campus = cur.fetchone()
                if not campus:
                    raise ValueError("数字校园号无效")
                if campus["registered"]:
                    raise ValueError("该校园号已注册，请直接登录")

                cur.execute(
                    "SELECT * FROM email_verifications WHERE email = %s AND code = %s ORDER BY created_at DESC LIMIT 1",
                    (email, code),
                )
                verification = cur.fetchone()
                if not verification:
                    raise ValueError("验证码错误")
                if verification["expires_at"] < iso_time():
                    raise ValueError("验证码已过期，请重新发送")

                cur.execute('SELECT id FROM "User" WHERE email = %s', (email,))
                if cur.fetchone():
                    raise ValueError("该邮箱已注册")

        _, hashed = password_digest(password)
        initials = username[:2].upper() if len(username) >= 2 else username.upper()
        colors = ["#E8622A", "#3B82F6", "#22C55E", "#A855F7", "#EC4899", "#F59E0B", "#06B6D4"]
        avatar_color = colors[hash(username) % len(colors)]

        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                try:
                    candidate_fields = {
                        "id": f"local_{secrets.token_hex(8)}",
                        "email": email,
                        "campusId": campus_id,
                        "name": username,
                        "password": hashed,
                        "grade": grade,
                        "classGroup": class_group,
                        "initials": initials,
                        "avatarColor": avatar_color,
                        "isAdmin": False,
                        "isOwner": False,
                        "createdAt": datetime.now(timezone.utc).replace(tzinfo=None),
                    }
                    fields = {}
                    for field_name, field_value in candidate_fields.items():
                        actual = user_column_name(field_name)
                        if actual:
                            fields[actual] = field_value
                    missing = [field_name for field_name in ("id", "email", "campusId", "name", "password") if not user_column_name(field_name)]
                    if missing:
                        raise ValueError(f"数据库 User 表缺少必要列：{', '.join(missing)}")
                    columns_sql = ",".join(f'"{name}"' for name in fields)
                    placeholders = ",".join(["%s"] * len(fields))
                    cur.execute(f'INSERT INTO "User"({columns_sql}) VALUES({placeholders})', tuple(fields.values()))
                    cur.execute(
                        'UPDATE "CampusUser" SET registered = TRUE WHERE "campusId" = %s AND registered = FALSE',
                        (campus_id,),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("该校园号已被注册")
                    cur.execute("DELETE FROM email_verifications WHERE email = %s", (email,))
                    cur.execute('SELECT * FROM "User" WHERE email = %s', (email,))
                    row = cur.fetchone()
                    cur.execute(
                        "INSERT INTO hatchery_user_extras(user_id,preview_id,created_at) VALUES(%s,%s,%s)",
                        (row["id"], new_preview_id(cur), iso_time()),
                    )
                except psycopg2.IntegrityError as exc:
                    if "User_name_key" in str(exc) or "name" in str(exc):
                        raise ValueError("用户名已存在") from exc
                    if "User_email_key" in str(exc) or "email" in str(exc):
                        raise ValueError("该邮箱已注册") from exc
                    if "User_campusId_key" in str(exc) or "campusId" in str(exc):
                        raise ValueError("该校园号已注册") from exc
                    raise ValueError("注册数据冲突，请重试") from exc
        self.clear_login_failures()
        headers = self.issue_console_session(row["id"], bool(data.get("remember", True)))
        self.send_json({"ok": True, "user": self.attach_preview_id(self.public_user(row), row["id"])}, 201, headers)

    def handle_send_otp(self) -> None:
        if self.login_is_limited():
            self.send_json({"error": "操作过于频繁，请 5 分钟后再试"}, 429)
            return
        data = self.read_json()
        email = str(data.get("email", "")).strip().lower()
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱地址")

        # Generate 6-digit code
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        expires = iso_time(datetime.now(timezone.utc) + timedelta(minutes=10))

        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM email_verifications WHERE email = %s", (email,))
                cur.execute(
                    "INSERT INTO email_verifications(email, code, expires_at, created_at) VALUES(%s,%s,%s,%s)",
                    (email, code, expires, iso_time()),
                )

        # Send via SMTP
        smtp_host = os.environ.get("EMAIL_HOST", "")
        smtp_port = int(os.environ.get("EMAIL_PORT", "465"))
        smtp_user = os.environ.get("EMAIL_USER", "")
        smtp_pass = os.environ.get("EMAIL_PASS", "")
        smtp_from = os.environ.get("EMAIL_FROM", smtp_user)

        if not smtp_host:
            self.send_json({"error": "邮件服务未配置"}, 500)
            return

        try:
            msg = MIMEText(f"你的验证码是：{code}\n\n10 分钟内有效。", "plain", "utf-8")
            msg["Subject"] = "AIchemy Hatchery 注册验证码"
            msg["From"] = smtp_from
            msg["To"] = email

            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_from, [email], msg.as_string())
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_from, [email], msg.as_string())
        except Exception as exc:
            self.send_json({"error": f"发送邮件失败：{exc}"}, 500)
            return

        self.send_json({"ok": True, "message": "验证码已发送，10 分钟内有效"})

    def handle_auth_logout(self) -> None:
        token = self.console_token()
        if token:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM console_sessions WHERE token_hash = %s", (token_hash,))
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": [
                f"alchemy_hatchery_console_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
            ]},
        )

    # ── SSO (via sdsz) ──────────────────────────────────────────────

    def handle_sso_authorize(self) -> None:
        """Redirect user to sdsz login page. After login, sdsz redirects back with a code."""
        if not SSO_SECRET:
            self.send_json({"error": "SSO 未配置"}, 503)
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        return_to = params.get("returnTo", ["/"])[0]
        # Sanitize: only allow relative paths
        if not return_to.startswith("/"):
            return_to = "/"
        state = secrets.token_urlsafe(16)
        # Store state in a short-lived cookie for CSRF protection
        callback_url = f"{self._base_url()}/api/auth/sso/callback"
        sdsz_login = (
            f"{SDSZ_BASE_URL}/login"
            f"?intent=sso"
            f"&redirect={urlparse_mod.quote(callback_url)}"
            f"&state={state}"
        )
        self.send_response(302)
        self.send_header("Location", sdsz_login)
        self.send_header("Set-Cookie",
            f"sso_state={state}; Path=/api/auth/sso; Max-Age=300; HttpOnly; SameSite=Lax{COOKIE_SECURITY_SUFFIX}")
        # Also store returnTo
        self.send_header("Set-Cookie",
            f"sso_return_to={urlparse_mod.quote(return_to)}; Path=/api/auth/sso; Max-Age=300; HttpOnly; SameSite=Lax{COOKIE_SECURITY_SUFFIX}")
        self.end_headers()

    def handle_sso_callback(self) -> None:
        """Receive code from sdsz, verify it, create local session, redirect to console."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if not code:
            self.send_json({"error": "Missing code"}, 400)
            return

        # Verify state (CSRF protection)
        cookie_header = self.headers.get("Cookie", "")
        cookies = SimpleCookie(cookie_header)
        stored_state = cookies.get("sso_state")
        if not stored_state or not state or stored_state.value != state:
            self.send_json({"error": "Invalid state (CSRF)"}, 403)
            return

        # Verify code with sdsz
        verify_url = f"{SDSZ_BASE_URL}/api/auth/sso/verify?code={urlparse_mod.quote(code)}"
        try:
            req = urllib.request.Request(verify_url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.send_json({"error": f"SSO 验证失败：{exc}"}, 502)
            return

        if not data.get("ok") or not data.get("user"):
            self.send_json({"error": "SSO 验证失败"}, 401)
            return

        sso_user = data["user"]
        sdsz_user_id = sso_user["id"]

        # Look up user in shared Neon DB by sdsz user id
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM "User" WHERE id = %s', (sdsz_user_id,))
                row = cur.fetchone()

        if not row:
            self.send_json({"error": "用户不存在"}, 404)
            return

        if row_value(row, "bannedUntil") is not None:
            self.send_json({"error": "此账号已被管理员停用"}, 403)
            return

        # Create session
        headers = self.issue_console_session(row["id"], remember=True)

        # Determine redirect target
        return_to = "/"
        return_to_cookie = cookies.get("sso_return_to")
        if return_to_cookie:
            decoded = urlparse_mod.unquote(return_to_cookie.value)
            if decoded.startswith("/"):
                return_to = decoded

        # Clear SSO cookies, set session cookie, redirect
        self.send_response(302)
        self.send_header("Location", return_to)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Set-Cookie",
            f"sso_state=; Path=/api/auth/sso; Max-Age=0; HttpOnly{COOKIE_SECURITY_SUFFIX}")
        self.send_header("Set-Cookie",
            f"sso_return_to=; Path=/api/auth/sso; Max-Age=0; HttpOnly{COOKIE_SECURITY_SUFFIX}")
        self.end_headers()

    def _base_url(self) -> str:
        """Best-effort base URL from request headers."""
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", f"localhost:{PORT}")
        return f"{proto}://{host}"

    def handle_change_password(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        old_password = str(data.get("oldPassword", ""))
        new_password = str(data.get("newPassword", ""))
        if len(new_password) < 8 or len(new_password) > 128:
            raise ValueError("新密码长度需为 8–128 位")
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM "User" WHERE id = %s', (user["id"],))
                row = cur.fetchone()
                if not row or not password_matches(old_password, "", row["password"], 0):
                    self.send_json({"error": "当前密码错误"}, 401)
                    return
                _, digest = password_digest(new_password)
                changed_col = user_column_name("passwordChangedAt")
                if changed_col:
                    cur.execute(
                        f'UPDATE "User" SET password = %s, "{changed_col}" = %s WHERE id = %s',
                        (digest, iso_time(), user["id"]),
                    )
                else:
                    cur.execute('UPDATE "User" SET password = %s WHERE id = %s', (digest, user["id"]))
                cur.execute("DELETE FROM console_sessions WHERE user_id = %s", (user["id"],))
        headers = self.issue_console_session(user["id"], True)
        self.send_json({"ok": True}, headers=headers)

    def handle_revoke_other_sessions(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        token = self.console_token() or ""
        current_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM console_sessions WHERE user_id = %s AND token_hash != %s",
                    (str(user["id"]), current_hash),
                )
                removed = cur.rowcount
                audit_event(cur, str(user["id"]), "auth.sessions_revoked", {"count": removed})
        self.send_json({"ok": True, "removed": removed})

    def handle_admin_user_status(self) -> None:
        admin = self.require_console_user(admin=True)
        if not admin:
            return
        data = self.read_json()
        username = str(data.get("username", "")).strip()
        status = str(data.get("status", "")).strip()
        if status not in ("active", "disabled"):
            raise ValueError("账号状态无效")
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM "User" WHERE LOWER(name) = LOWER(%s)', (username,))
                target = cur.fetchone()
                if not target:
                    raise ValueError("用户不存在")
                if is_site_owner_campus_id(target.get("campusId")) and status == "disabled":
                    raise ValueError("站长账号（数字校园号 20264689）不能被停用")
                if str(target["id"]) == str(admin["id"]):
                    raise ValueError("不能停用当前登录的管理员账号")
                ban_col = user_column_name("bannedUntil")
                if not ban_col:
                    raise ValueError("当前数据库的 User 表缺少 bannedUntil 列，不支持停用/启用账号")
                if row_value(target, "isAdmin") and status == "disabled":
                    cur.execute(f'SELECT COUNT(*) AS n FROM "User" WHERE "isAdmin" = TRUE AND "{ban_col}" IS NULL')
                    if int(cur.fetchone()["n"]) <= 1:
                        raise ValueError("系统必须至少保留一个可用管理员")
                cur.execute(
                    f'UPDATE "User" SET "{ban_col}" = CASE WHEN %s = \'disabled\' THEN \'9999-12-31T00:00:00\' ELSE NULL END WHERE id = %s',
                    (status, target["id"]),
                )
                if status == "disabled":
                    cur.execute("DELETE FROM console_sessions WHERE user_id = %s", (target["id"],))
                audit_event(cur, str(admin["id"]), "admin.user_status", {"username": target["name"], "status": status})
        self.send_json({"ok": True, "username": target["name"], "status": status})

    def handle_save_draft(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        if not isinstance(data.get("pages"), list):
            raise ValueError("草稿缺少页面数据")
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_site_drafts(user_id,data_json,updated_at) VALUES(%s,%s,%s)"
                    " ON CONFLICT (user_id) DO UPDATE SET data_json=EXCLUDED.data_json, updated_at=EXCLUDED.updated_at",
                    (str(user["id"]), serialized, iso_time()),
                )
        self.send_json({"ok": True, "updatedAt": iso_time()})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/auth/login":
                self.handle_auth_login()
            elif path == "/api/auth/register":
                self.handle_auth_register()
            elif path == "/api/auth/send-otp":
                self.handle_send_otp()
            elif path == "/api/auth/logout":
                self.handle_auth_logout()
            elif path == "/api/auth/change-password":
                self.handle_change_password()
            elif path == "/api/admin/users/status":
                self.handle_admin_user_status()
            elif path == "/api/auth/sessions/revoke-others":
                self.handle_revoke_other_sessions()
            elif path == "/api/console/draft":
                self.handle_save_draft()
            elif path == "/api/site-account/reset-owner":
                if self.require_console_user():
                    self.send_json({"error": "正式发布功能暂未开放"}, 501)
            elif path in ("/api/ai", "/api/ai/propose"):
                if self.require_console_user():
                    self.handle_ai_propose()
            elif path == "/api/ai/run":
                user = self.require_console_user()
                if user:
                    if (parse_qs(parsed.query).get("async") or [""])[0] == "1":
                        self.handle_ai_run_start(user)
                    else:
                        self.handle_ai_run(user)
            elif path == "/api/ai/apply":
                if self.require_console_user():
                    self.handle_ai_apply()
            elif path == "/api/ai/undo":
                if self.require_console_user():
                    self.handle_ai_undo()
            elif path == "/api/server/restart":
                if self.require_console_user():
                    self.handle_server_restart()
            elif path == "/api/preview":
                self.handle_preview()
            elif path == "/api/publish":
                self.handle_publish()
            elif path.startswith("/api/runtime/"):
                self.send_json({"error": "正式发布功能暂未开放"}, 404)
            else:
                self.send_json({"error": "接口不存在"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 502)
        except Exception as exc:
            self.send_json({"error": f"本地服务错误：{exc}"}, 500)
    def validate_harness_change(self, name: str, before: str, after: str) -> None:
        if name not in AI_EDITABLE_SOURCE_FILES:
            raise ValueError("AI 尝试修改未授权文件")
        forbidden_growth = {
            r"sqlite[3]\s*\.\s*connect\s*\(": "禁止新增数据库连接",
            r"indexedDB\s*\.\s*open\s*\(": "禁止新增浏览器数据库",
            r"CREATE\s+TABLE[^;]{0,300}(?:user|account|member|session|auth)": "禁止新增独立账号数据表",
            r"[\"'][^\"']+\.db(?:-[^\"']+)?[\"']": "禁止新增数据库文件",
            r"subprocess\s*\.\s*(?:run|Popen|call|check_output)\s*\(": "禁止新增命令执行能力",
            r"os\s*\.\s*system\s*\(": "禁止新增系统命令能力",
            r"\b(?:eval|exec)\s*\(": "禁止新增动态代码执行能力",
            r"localStorage[^\n;]{0,160}(?:user|account|session|auth|password)": "禁止新增独立浏览器账号存储",
        }
        for pattern, message in forbidden_growth.items():
            if len(re.findall(pattern, after, flags=re.I | re.S)) > len(re.findall(pattern, before, flags=re.I | re.S)):
                raise ValueError(message)
    # DeepSeek Harness is the only automatic execution engine.
    def run_ai_harness(self, prompt: str, context: dict, include_site: bool, include_source: bool, attachments: list[dict], progress=None) -> dict:
        run_id = f"run-{secrets.token_hex(10)}"
        editable_files = AI_EDITABLE_SOURCE_FILES if include_source else ()
        originals = {name: read_text_exact(ROOT / name) for name in editable_files}
        backup_dir = ROOT / ".ai-backups" / run_id

        def restore_sources() -> None:
            for name, content in originals.items():
                if read_text_exact(ROOT / name) != content:
                    write_text_exact(ROOT / name, content)
            shutil.rmtree(backup_dir, ignore_errors=True)

        try:
            result = run_deepseek_harness(
                root=ROOT,
                prompt=prompt,
                context=context,
                attachments=attachments,
                readable_files=AI_SOURCE_FILES,
                editable_files=editable_files,
                preview_port=PORT,
                progress=progress,
            )
            changed_files = [name for name, before in originals.items() if read_text_exact(ROOT / name) != before]
            for name in changed_files:
                self.validate_harness_change(name, originals[name], read_text_exact(ROOT / name))
            if changed_files:
                self.validate_changed_sources({name: read_text_exact(ROOT / name) for name in changed_files})

            proposal = self.validate_ai_proposal(
                {
                    "summary": result.get("summary", "网站修改已完成"),
                    "risk": result.get("risk", "medium"),
                    "assumptions": [],
                    "siteOperations": result.get("siteOperations", []),
                    "sourceChanges": [],
                    "checks": result.get("checks", []),
                },
                False,
            )
            proposal = self.constrain_instance_intent(proposal, prompt, context)
            if not include_site:
                proposal["siteOperations"] = []

            # A narrowly scoped block request is data-only. If the model edited
            # global source despite that boundary, discard those source edits.
            if proposal.get("summary", "").startswith("仅修改当前") and changed_files:
                restore_sources()
                changed_files = []

            if changed_files:
                backup_dir.mkdir(parents=True, exist_ok=True)
                for name in changed_files:
                    write_text_exact(backup_dir / name, originals[name])
            with AI_LOCK:
                AI_BACKUPS[run_id] = {name: originals[name] for name in changed_files}

            return {
                "runId": run_id,
                "summary": proposal["summary"],
                "risk": proposal["risk"],
                "siteOperations": proposal["siteOperations"],
                "checks": proposal["checks"],
                "trace": result.get("trace", []),
                "changedFiles": changed_files,
                "restartRequired": False,
                "undoAvailable": bool(changed_files),
                "engine": "deepseek-harness",
                "finishReason": result.get("finishReason"),
                "usage": result.get("usage"),
            }
        except Exception:
            restore_sources()
            raise

    def validate_ai_proposal(self, proposal: dict, include_source: bool) -> dict:
        if not isinstance(proposal, dict):
            raise ValueError("DeepSeek Harness 返回的方案不是对象")
        proposal["summary"] = str(proposal.get("summary", "AI 已生成修改方案"))[:500]
        if proposal.get("risk") not in ("low", "medium", "high"):
            proposal["risk"] = "medium"
        operations = proposal.get("siteOperations", [])
        changes = proposal.get("sourceChanges", [])
        if not isinstance(operations, list) or len(operations) > 40:
            raise ValueError("站点修改操作格式错误或数量过多")
        if not isinstance(changes, list) or len(changes) > 12:
            raise ValueError("源码修改格式错误或数量过多")
        allowed_ops = {"set_site", "set_page", "add_page", "remove_page", "add_element", "update_element", "remove_element", "move_element", "set_items"}
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") not in allowed_ops:
                raise ValueError("DeepSeek Harness 返回了不支持的站点操作")
        normalized_changes = []
        for change in changes if include_source else []:
            if not isinstance(change, dict) or change.get("path") not in AI_EDITABLE_SOURCE_FILES:
                raise ValueError("网站代理尝试修改未授权文件")
            search = str(change.get("search", ""))
            replace = str(change.get("replace", ""))
            if not search or len(search) > 60_000 or len(replace) > 60_000:
                raise ValueError("源码修改片段为空或过大")
            normalized_changes.append({"path": change["path"], "search": search, "replace": replace, "reason": str(change.get("reason", "修改源码"))[:300]})
        proposal["siteOperations"] = operations
        proposal["sourceChanges"] = normalized_changes
        proposal["assumptions"] = [str(item)[:300] for item in proposal.get("assumptions", []) if str(item).strip()][:10]
        proposal["checks"] = [str(item)[:300] for item in proposal.get("checks", []) if str(item).strip()][:10]
        return proposal

    def constrain_instance_intent(self, proposal: dict, prompt: str, context: dict) -> dict:
        """Keep narrow visual requests on one block instance, even if the model proposes global CSS."""
        text = prompt.lower()
        edit_words = r"修改|调整|改动|编辑|重写|优化|统一"
        global_words = r"所有|全部|全局|同类|默认|底层|源码|代码|前端|后端|css|组件实现"
        global_intent = re.search(
            rf"(?:{edit_words}).{{0,12}}(?:{global_words})|(?:{global_words}).{{0,12}}(?:{edit_words})",
            text,
        )
        target_type = None
        if re.search(r"首页大字|大标题|hero", text):
            target_type = "hero"
        elif re.search(r"公告栏|公告条|notice", text):
            target_type = "notice"
        if not target_type or global_intent:
            return proposal

        site = context.get("site", {}) if isinstance(context, dict) else {}
        pages = site.get("pages", []) if isinstance(site, dict) else []
        active_page_id = context.get("activePageId") if isinstance(context, dict) else None
        active_page = next((page for page in pages if page.get("id") == active_page_id), None)
        candidates = []
        if active_page:
            candidates = [element for element in active_page.get("elements", []) if element.get("type") == target_type]
        if not candidates:
            candidates = [element for page in pages for element in page.get("elements", []) if element.get("type") == target_type]
            if len(candidates) != 1:
                return proposal
            active_page = next((page for page in pages if candidates[0] in page.get("elements", [])), None)
        if not candidates or not active_page:
            return proposal

        element = candidates[0]
        settings = None
        if target_type == "hero" and re.search(r"小一点|调小|缩小|字号小|字体小", text):
            settings = {"titleSize": "clamp(38px,5.5vw,78px)"}
        elif target_type == "notice" and re.search(r"主底色|页面底色|页面背景|背景色|底色", text):
            settings = {"background": "var(--page-bg)", "color": "var(--page-fg)"}

        operations = [
            operation for operation in proposal.get("siteOperations", [])
            if operation.get("op") == "update_element"
            and operation.get("pageId") == active_page.get("id")
            and operation.get("elementId") == element.get("id")
        ]
        if settings:
            if operations:
                model_settings = operations[0].get("settings", {})
                operations[0]["settings"] = {**(model_settings if isinstance(model_settings, dict) else {}), **settings}
            else:
                operations = [{"op": "update_element", "pageId": active_page.get("id"), "elementId": element.get("id"), "settings": settings}]
        proposal["siteOperations"] = operations
        proposal["sourceChanges"] = []
        proposal["risk"] = "low"
        proposal["summary"] = f"仅修改当前{('首页大字' if target_type == 'hero' else '公告栏')}实例；已阻止全局样式和源码改动。"
        proposal["checks"] = ["检查当前模块实例立即变化", "检查同类模块与后续新增模块保持默认样式"]
        return proposal

    def prepare_ai_run(self) -> tuple[str, dict, bool, bool, list[dict]]:
        payload = self.read_json()
        prompt = str(payload.get("prompt", "")).strip()
        context = payload.get("context", {})
        include_site = bool(payload.get("includeSite", True))
        include_source = bool(payload.get("includeSource", True))
        raw_attachments = payload.get("attachments", [])
        if not prompt:
            raise ValueError("调整描述不能为空")
        if not isinstance(context, dict):
            raise ValueError("站点上下文格式错误")
        if not isinstance(raw_attachments, list) or len(raw_attachments) > 4:
            raise ValueError("一次最多添加 4 个附件")
        attachments = []
        total_attachment_size = 0
        for raw in raw_attachments:
            if not isinstance(raw, dict):
                raise ValueError("附件格式错误")
            name = Path(str(raw.get("name", "附件"))).name[:180]
            kind = str(raw.get("kind", "text"))
            mime_type = str(raw.get("type", "text/plain"))[:100]
            content = str(raw.get("content", ""))
            size = max(0, int(raw.get("size", 0)))
            if kind == "image":
                if not mime_type.startswith("image/") or not content.startswith("data:image/") or len(content) > 6_000_000:
                    raise ValueError(f"图片附件 {name} 格式错误或过大")
            elif kind == "text":
                if len(content.encode("utf-8")) > 600_000:
                    raise ValueError(f"文本附件 {name} 超过大小限制")
            else:
                raise ValueError(f"附件 {name} 类型不受支持")
            total_attachment_size += size
            if total_attachment_size > 8 * 1024 * 1024:
                raise ValueError("附件总大小不能超过 8 MB")
            attachments.append({"name": name, "kind": kind, "type": mime_type, "content": content})
        return prompt, context, include_site, include_source, attachments

    def handle_ai_run(self, user: dict) -> None:
        prompt, context, include_site, include_source, attachments = self.prepare_ai_run()
        if not AI_RUN_LOCK.acquire(blocking=False):
            raise ValueError("另一个 AI 任务尚未结束，请稍后再试")
        provider_name, model = current_ai_provider_model()
        try:
            result = self.run_ai_harness(prompt, context, include_site, include_source, attachments)
            record_ai_usage(user["id"], None, prompt, attachments, provider_name, model, result.get("usage"), "completed")
        except Exception:
            record_ai_usage(user["id"], None, prompt, attachments, provider_name, model, None, "failed")
            raise
        finally:
            AI_RUN_LOCK.release()
        self.send_json(result)

    def handle_ai_run_start(self, user: dict) -> None:
        prompt, context, include_site, include_source, attachments = self.prepare_ai_run()
        if not AI_RUN_LOCK.acquire(blocking=False):
            raise ValueError("另一个 AI 任务尚未结束，请稍后再试")
        job_id = f"job-{secrets.token_hex(12)}"
        now = time.time()
        with AI_RUN_JOBS_LOCK:
            for stale_id in [key for key, value in AI_RUN_JOBS.items() if now - float(value.get("createdAt", now)) > 3600]:
                AI_RUN_JOBS.pop(stale_id, None)
            AI_RUN_JOBS[job_id] = {"userId": str(user["id"]), "status": "running", "events": [], "createdAt": now, "result": None, "error": None}

        def progress(event: dict) -> None:
            with AI_RUN_JOBS_LOCK:
                job = AI_RUN_JOBS.get(job_id)
                if not job:
                    return
                event = dict(event)
                event["updatedAt"] = time.time()
                existing = next((item for item in job["events"] if item.get("id") == event.get("id")), None)
                if existing:
                    existing.update(event)
                else:
                    event["sequence"] = len(job["events"]) + 1
                    job["events"].append(event)

        def worker() -> None:
            provider_name, model = current_ai_provider_model()
            try:
                result = self.run_ai_harness(prompt, context, include_site, include_source, attachments, progress=progress)
                record_ai_usage(user["id"], job_id, prompt, attachments, provider_name, model, result.get("usage"), "completed")
                with AI_RUN_JOBS_LOCK:
                    job = AI_RUN_JOBS.get(job_id)
                    if job:
                        job.update({"status": "completed", "result": result})
            except Exception as error:
                record_ai_usage(user["id"], job_id, prompt, attachments, provider_name, model, None, "failed")
                with AI_RUN_JOBS_LOCK:
                    job = AI_RUN_JOBS.get(job_id)
                    if job:
                        job.update({"status": "failed", "error": str(error)[:1000]})
            finally:
                AI_RUN_LOCK.release()

        try:
            threading.Thread(target=worker, name=f"hatchery-ai-{job_id[-6:]}", daemon=True).start()
        except Exception:
            with AI_RUN_JOBS_LOCK:
                AI_RUN_JOBS.pop(job_id, None)
            AI_RUN_LOCK.release()
            raise
        self.send_json({"jobId": job_id, "status": "running"}, 202)

    def handle_ai_run_status(self, user: dict, parsed) -> None:
        job_id = str((parse_qs(parsed.query).get("id") or [""])[0])
        if not re.fullmatch(r"job-[0-9a-f]{24}", job_id):
            raise ValueError("AI 任务编号无效")
        with AI_RUN_JOBS_LOCK:
            job = AI_RUN_JOBS.get(job_id)
            if not job or str(job.get("userId", "")) != str(user["id"]):
                raise ValueError("AI 任务不存在或已过期")
            payload = {
                "jobId": job_id,
                "status": job["status"],
                "events": [dict(item) for item in job["events"]],
                "result": job.get("result"),
                "error": job.get("error"),
            }
        self.send_json(payload)
    def handle_ai_propose(self) -> None:
        raise ValueError("方案确认接口已停用；DeepSeek Harness 只通过 /api/ai/run 自动执行")

    def validate_changed_sources(self, contents: dict[str, str]) -> None:
        if "server.py" in contents:
            compile(contents["server.py"], str(ROOT / "server.py"), "exec")
        node = shutil.which("node")
        if not node:
            bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
            node = str(bundled) if bundled.exists() else None
        if node:
            for name in ("auth.js", "script.js", "viewer.js"):
                if name not in contents:
                    continue
                check_file = ROOT / f".{name}.ai-check.js"
                try:
                    write_text_exact(check_file, contents[name])
                    checked = subprocess.run([node, "--check", str(check_file)], capture_output=True, text=True, timeout=20)
                    if checked.returncode:
                        raise ValueError(f"{name} 语法检查失败：{checked.stderr.strip()[:500]}")
                finally:
                    check_file.unlink(missing_ok=True)

    def handle_ai_apply(self) -> None:
        proposal_id = str(self.read_json().get("proposalId", ""))
        with AI_LOCK:
            proposal = AI_PROPOSALS.get(proposal_id)
        if not proposal:
            raise ValueError("修改方案已失效，请重新生成")
        changes = proposal.get("sourceChanges", [])
        originals: dict[str, str] = {}
        updated: dict[str, str] = {}
        for change in changes:
            name = change["path"]
            if name not in originals:
                originals[name] = read_text_exact(ROOT / name)
                updated[name] = originals[name]
            if updated[name].count(change["search"]) != 1:
                raise ValueError(f"{name} 的目标代码已变化，未应用任何修改")
            updated[name] = updated[name].replace(change["search"], change["replace"], 1)
        self.validate_changed_sources(updated)
        if updated:
            backup_dir = ROOT / ".ai-backups" / proposal_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            try:
                for name, content in originals.items():
                    write_text_exact(backup_dir / name, content)
                for name, content in updated.items():
                    write_text_exact(ROOT / name, content)
            except Exception:
                for name, content in originals.items():
                    write_text_exact(ROOT / name, content)
                raise
        with AI_LOCK:
            AI_BACKUPS[proposal_id] = originals
        self.send_json({"ok": True, "changedFiles": list(updated), "restartRequired": "server.py" in updated, "undoAvailable": bool(updated)})

    def handle_ai_undo(self) -> None:
        proposal_id = str(self.read_json().get("proposalId", ""))
        with AI_LOCK:
            originals = AI_BACKUPS.pop(proposal_id, None)
        backup_dir = ROOT / ".ai-backups" / proposal_id
        if originals is None and backup_dir.is_dir():
            originals = {
                name: read_text_exact(backup_dir / name)
                for name in AI_SOURCE_FILES
                if (backup_dir / name).is_file()
            }
        if originals is None:
            raise ValueError("没有可撤销的源码修改")
        for name, content in originals.items():
            write_text_exact(ROOT / name, content)
        self.send_json({"ok": True, "restoredFiles": list(originals), "restartRequired": "server.py" in originals})

    def handle_server_restart(self) -> None:
        self.send_json({"ok": True, "message": "本地服务正在重启"})
        server = self.server

        def restart() -> None:
            time.sleep(0.35)
            server.shutdown()
            server.server_close()
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [sys.executable, str(ROOT / "server.py")],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )

        threading.Thread(target=restart, name="alchemy-hatchery-restart", daemon=False).start()

    def prepare_site_payload(self, data: dict) -> tuple[dict, bool]:
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("至少需要一个页面")
        required_account = (
            '<section class="account-block"><div class="account-copy"><span class="block-kicker">MEMBER ACCESS</span>'
            '<h2>登录你的账号</h2><p>登录后即可参与社区发帖与回复。</p></div>'
            '<div class="account-panel" data-login-label="登录账号"><div><small>站点成员</small><b>独立账号系统</b></div>'
            '<button data-preview-action="account-login">登录账号 →</button></div></section>'
        )
        for page in pages:
            if not isinstance(page, dict):
                raise ValueError("页面数据格式无效")
            html = str(page.get("html", ""))
            if 'class="forum-block"' in html and 'class="account-block"' not in html:
                page["html"] = html.replace('<section class="forum-block">', required_account + '<section class="forum-block">', 1)
        has_account = any('class="account-block"' in str(page.get("html", "")) for page in pages)
        data["features"] = {**(data.get("features") if isinstance(data.get("features"), dict) else {}), "account": has_account}
        return data, has_account

    def handle_preview(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        data, has_account = self.prepare_site_payload(data)
        user_id = str(user["id"])
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT preview_id FROM hatchery_user_extras WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                preview_id = str(row[0] or "") if row else ""
                if not PREVIEW_ID_PATTERN.fullmatch(preview_id):
                    preview_id = new_preview_id(cur)
                    cur.execute(
                        "INSERT INTO hatchery_user_extras(user_id,preview_id,created_at) VALUES(%s,%s,%s)"
                        " ON CONFLICT (user_id) DO UPDATE SET preview_id=EXCLUDED.preview_id",
                        (user_id, preview_id, iso_time()),
                    )
                data["username"] = "preview"
                data["previewMode"] = True
                data["previewId"] = preview_id
                data["basePath"] = f"/preview/{preview_id}"
                serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                cur.execute(
                    "INSERT INTO hatchery_site_previews(user_id,data_json,updated_at) VALUES(%s,%s,%s)"
                    " ON CONFLICT (user_id) DO UPDATE SET data_json=EXCLUDED.data_json, updated_at=EXCLUDED.updated_at",
                    (user_id, serialized, iso_time()),
                )
                audit_event(cur, user_id, "site.preview_updated")
        self.send_json({"ok": True, "url": f"/preview/{preview_id}", "previewId": preview_id, "accountEnabled": has_account})

    def handle_publish(self) -> None:
        if not self.require_console_user():
            return
        self.send_json({"error": "正式发布功能暂未开放"}, 501)

    def handle_site_owner_reset(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        site_username = str(user["username"])
        if not self.site_account_enabled(site_username) or not site_account_initialized(site_username):
            raise ValueError("当前发布站点没有启用账号系统")
        new_password = secrets.token_urlsafe(12)
        _, digest = password_digest(new_password)
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id FROM hatchery_site_users WHERE site_username = %s AND role = 'owner' ORDER BY id LIMIT 1",
                    (site_username,),
                )
                owner = cur.fetchone()
                if owner:
                    cur.execute(
                        "UPDATE hatchery_site_users SET username = %s, password_hash = %s WHERE id = %s AND site_username = %s",
                        (site_username, digest, owner["id"], site_username),
                    )
                    cur.execute(
                        "DELETE FROM hatchery_site_sessions WHERE user_id = %s AND site_username = %s",
                        (owner["id"], site_username),
                    )
                else:
                    cur.execute(
                        "INSERT INTO hatchery_site_users(site_username,username,password_hash,role,created_at) VALUES(%s,%s,%s,'owner',%s)",
                        (site_username, site_username, digest, iso_time()),
                    )
        self.send_json({"ok": True, "siteAdmin": {"username": site_username, "password": new_password}})

    def runtime_path(self, site_username: str) -> Path:
        return PUBLISHED / site_username / "runtime.json"

    def default_runtime(self, site_username: str) -> dict:
        site_file = PUBLISHED / site_username / "site.json"
        forum_posts = []
        if site_file.exists():
            forum_posts = json.loads(site_file.read_text(encoding="utf-8")).get("forumPosts", [])
        return {"forumPosts": forum_posts, "registrations": {}, "joinedUsers": []}

    def load_runtime(self, site_username: str) -> dict:
        runtime_file = self.runtime_path(site_username)
        if runtime_file.exists():
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        else:
            runtime = self.default_runtime(site_username)
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
        runtime.setdefault("forumPosts", [])
        runtime.setdefault("registrations", {})
        runtime.setdefault("joinedUsers", [])
        return runtime

    def save_runtime(self, site_username: str, runtime: dict) -> None:
        self.runtime_path(site_username).write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")

    def site_account_enabled(self, site_username: str) -> bool:
        site_file = PUBLISHED / site_username / "site.json"
        if not site_file.exists():
            return False
        data = json.loads(site_file.read_text(encoding="utf-8"))
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        return bool(features.get("account")) or any('class="account-block"' in str(page.get("html", "")) for page in data.get("pages", []))

    def site_cookie_name(self, site_username: str) -> str:
        return f"alchemy_hatchery_{hashlib.sha256(site_username.lower().encode('utf-8')).hexdigest()[:12]}"

    def published_session_identity(self, site_username: str) -> dict | None:
        if not self.site_account_enabled(site_username) or not site_account_initialized(site_username):
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get(self.site_cookie_name(site_username))
        if not token:
            return None
        token_hash = hashlib.sha256(token.value.encode("utf-8")).hexdigest()
        now = iso_time()
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "DELETE FROM hatchery_site_sessions WHERE site_username = %s AND expires_at <= %s",
                    (site_username, now),
                )
                cur.execute(
                    """
                    SELECT u.id, u.username, u.role, u.status
                    FROM hatchery_site_sessions s
                    JOIN hatchery_site_users u ON u.id = s.user_id AND u.site_username = s.site_username
                    WHERE s.token_hash = %s AND s.site_username = %s AND s.expires_at > %s AND u.status = 'active'
                    """,
                    (token_hash, site_username, now),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def published_session_user(self, site_username: str) -> str | None:
        identity = self.published_session_identity(site_username)
        return str(identity["username"]) if identity else None

    def require_site_owner(self, site_username: str) -> dict | None:
        identity = self.published_session_identity(site_username)
        if not identity:
            self.send_json({"error": "请先登录站点管理账号"}, 401)
            return None
        if identity["role"] != "owner":
            self.send_json({"error": "只有站点所有者可以访问后台"}, 403)
            return None
        return identity

    def site_registration_mode(self, site_username: str) -> str:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM hatchery_site_settings WHERE site_username = %s AND key = 'registration_mode'",
                    (site_username,),
                )
                row = cur.fetchone()
        mode = str(row[0]) if row else "open"
        return mode if mode in ("open", "invite", "closed") else "open"

    def issue_site_session(self, site_username: str, user_id: int) -> dict[str, str]:
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created_at = utc_now()
        expires_at = created_at + timedelta(days=CONSOLE_SESSION_DAYS)
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_site_sessions(token_hash,site_username,user_id,created_at,expires_at) VALUES(%s,%s,%s,%s,%s)",
                    (token_hash, site_username, user_id, iso_time(created_at), iso_time(expires_at)),
                )
        cookie_name = self.site_cookie_name(site_username)
        return {"Set-Cookie": f"{cookie_name}={token}; Path=/; Max-Age={CONSOLE_SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}"}

    def require_published_user(self, site_username: str) -> str | None:
        user = self.published_session_user(site_username)
        if not user:
            self.send_json({"error": "请先登录站点账号"}, 401)
        return user

    def handle_runtime_login(self, site_username: str) -> None:
        if not self.site_account_enabled(site_username) or not site_account_initialized(site_username):
            self.send_json({"error": "该站点账号系统尚未初始化，请站长重新发布"}, 409)
            return
        if self.login_is_limited():
            self.send_json({"error": "登录尝试过于频繁，请稍后再试"}, 429)
            return
        data = self.read_json()
        username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM hatchery_site_users WHERE LOWER(username) = LOWER(%s) AND site_username = %s",
                    (username, site_username),
                )
                row = cur.fetchone()
        if not row or not password_matches(password, "", row["password_hash"], 0):
            self.record_login_failure()
            self.send_json({"error": "账号或密码错误"}, 401)
            return
        if row["status"] != "active":
            self.send_json({"error": "该账号已被停用，请联系站点管理员"}, 403)
            return
        self.clear_login_failures()
        record_site_audit(site_username, str(row["username"]), "auth.login", str(row["username"]))
        self.send_json({"ok": True, "user": row["username"], "role": row["role"]}, headers=self.issue_site_session(site_username, int(row["id"])))

    def handle_runtime_register(self, site_username: str) -> None:
        if not self.site_account_enabled(site_username) or not site_account_initialized(site_username):
            self.send_json({"error": "该站点未启用账号系统"}, 404)
            return
        data = self.read_json()
        username, password = self.validate_credentials(data.get("username"), data.get("password"))
        registration_mode = self.site_registration_mode(site_username)
        invite = str(data.get("invite", "")).strip().lower()
        if registration_mode == "closed":
            self.send_json({"error": "该站点已关闭新用户注册"}, 403)
            return
        if registration_mode == "invite" and not INVITE_PATTERN.fullmatch(invite):
            raise ValueError("该站点仅限邀请注册，请输入 16 位邀请码")
        _, digest = password_digest(password)
        try:
            with neon_db() as conn:
                with conn.cursor() as cur:
                    if registration_mode == "invite":
                        cur.execute(
                            "SELECT code FROM hatchery_site_invitations WHERE code = %s AND site_username = %s AND used_by IS NULL AND revoked_at IS NULL",
                            (invite, site_username),
                        )
                        if not cur.fetchone():
                            raise ValueError("本站邀请码无效、已使用或已撤销")
                    cur.execute(
                        "SELECT 1 FROM hatchery_site_users WHERE site_username = %s AND LOWER(username) = LOWER(%s)",
                        (site_username, username),
                    )
                    if cur.fetchone():
                        raise ValueError("该站点中已存在这个用户名")
                    cur.execute(
                        "INSERT INTO hatchery_site_users(site_username,username,password_hash,role,created_at) VALUES(%s,%s,%s,'member',%s) RETURNING id",
                        (site_username, username, digest, iso_time()),
                    )
                    user_id = int(cur.fetchone()[0])
                    if registration_mode == "invite":
                        cur.execute(
                            "UPDATE hatchery_site_invitations SET used_by = %s, used_at = %s WHERE code = %s AND site_username = %s AND used_by IS NULL",
                            (user_id, iso_time(), invite, site_username),
                        )
        except psycopg2.IntegrityError as exc:
            raise ValueError("该站点中已存在这个用户名") from exc
        record_site_audit(site_username, username, "auth.register", username, registration_mode)
        self.send_json({"ok": True, "user": username, "role": "member"}, 201, self.issue_site_session(site_username, user_id))

    def handle_runtime_logout(self, site_username: str) -> None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        cookie_name = self.site_cookie_name(site_username)
        token = cookie.get(cookie_name)
        if token and site_account_initialized(site_username):
            token_hash = hashlib.sha256(token.value.encode("utf-8")).hexdigest()
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM hatchery_site_sessions WHERE token_hash = %s AND site_username = %s",
                        (token_hash, site_username),
                    )
        self.send_json({"ok": True}, headers={"Set-Cookie": [
            f"{cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
        ]})

    def handle_forum_topic(self, site_username: str) -> None:
        user = self.require_published_user(site_username)
        if not user:
            return
        data = self.read_json()
        title, body = str(data.get("title", "")).strip(), str(data.get("body", "")).strip()
        if not title or not body or len(title) > 120 or len(body) > 5000:
            raise ValueError("话题标题或内容为空、或长度超出限制")
        with RUNTIME_LOCK:
            runtime = self.load_runtime(site_username)
            runtime["forumPosts"].insert(0, {"id": f"post-{secrets.token_hex(8)}", "title": title, "author": user, "body": body, "status": "open", "createdAt": iso_time(), "replies": []})
            self.save_runtime(site_username, runtime)
        self.send_json({"ok": True, "forumPosts": runtime["forumPosts"]})

    def handle_forum_reply(self, site_username: str) -> None:
        user = self.require_published_user(site_username)
        if not user:
            return
        data = self.read_json()
        post_id, body = str(data.get("postId", "")), str(data.get("body", "")).strip()
        if not body or len(body) > 3000:
            raise ValueError("回复内容为空或过长")
        with RUNTIME_LOCK:
            runtime = self.load_runtime(site_username)
            post = next((item for item in runtime["forumPosts"] if item.get("id") == post_id), None)
            if not post:
                raise ValueError("话题不存在")
            if post.get("status", "open") == "locked":
                self.send_json({"error": "该话题已被管理员锁定"}, 403)
                return
            post.setdefault("replies", []).append({"author": user, "body": body, "createdAt": iso_time()})
            self.save_runtime(site_username, runtime)
        self.send_json({"ok": True, "forumPosts": runtime["forumPosts"]})

    def handle_registration(self, site_username: str) -> None:
        user = self.require_published_user(site_username)
        if not user:
            return
        event = str(self.read_json().get("event", "")).strip()
        if not event:
            raise ValueError("活动名称不能为空")
        with RUNTIME_LOCK:
            runtime = self.load_runtime(site_username)
            registrations = runtime["registrations"].setdefault(user, [])
            if event in registrations:
                registrations.remove(event)
                registered = False
            else:
                registrations.append(event)
                registered = True
            self.save_runtime(site_username, runtime)
        self.send_json({"ok": True, "registered": registered, "registrations": registrations})

    def handle_join(self, site_username: str) -> None:
        user = self.require_published_user(site_username)
        if not user:
            return
        with RUNTIME_LOCK:
            runtime = self.load_runtime(site_username)
            if user in runtime["joinedUsers"]:
                runtime["joinedUsers"].remove(user)
                joined = False
            else:
                runtime["joinedUsers"].append(user)
                joined = True
            self.save_runtime(site_username, runtime)
        self.send_json({"ok": True, "joined": joined})

    def handle_site_admin_settings(self, site_username: str) -> None:
        owner = self.require_site_owner(site_username)
        if not owner:
            return
        mode = str(self.read_json().get("registrationMode", ""))
        if mode not in ("open", "invite", "closed"):
            raise ValueError("注册策略只能是开放、邀请或关闭")
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_site_settings(site_username,key,value,updated_at) VALUES(%s,'registration_mode',%s,%s)"
                    " ON CONFLICT (site_username,key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                    (site_username, mode, iso_time()),
                )
        record_site_audit(site_username, owner["username"], "settings.registration", mode)
        self.send_json({"ok": True, "registrationMode": mode})

    def handle_site_admin_user_status(self, site_username: str) -> None:
        owner = self.require_site_owner(site_username)
        if not owner:
            return
        data = self.read_json()
        username = str(data.get("username", "")).strip()
        status = str(data.get("status", ""))
        if status not in ("active", "suspended"):
            raise ValueError("账号状态无效")
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id,role FROM hatchery_site_users WHERE LOWER(username) = LOWER(%s) AND site_username = %s",
                    (username, site_username),
                )
                target = cur.fetchone()
                if not target:
                    raise ValueError("成员不存在")
                if target["role"] == "owner":
                    raise ValueError("不能停用站点所有者")
                cur.execute(
                    "UPDATE hatchery_site_users SET status = %s WHERE id = %s AND site_username = %s",
                    (status, target["id"], site_username),
                )
                if status == "suspended":
                    cur.execute(
                        "DELETE FROM hatchery_site_sessions WHERE user_id = %s AND site_username = %s",
                        (target["id"], site_username),
                    )
        record_site_audit(site_username, owner["username"], f"user.{status}", username)
        self.send_json({"ok": True})

    def handle_site_admin_generate_invites(self, site_username: str) -> None:
        owner = self.require_site_owner(site_username)
        if not owner:
            return
        try:
            count = int(self.read_json().get("count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("邀请码数量无效") from exc
        if count < 1 or count > 20:
            raise ValueError("每次可生成 1–20 个邀请码")
        codes: list[str] = []
        with neon_db() as conn:
            with conn.cursor() as cur:
                while len(codes) < count:
                    code = secrets.token_hex(8)
                    cur.execute(
                        "INSERT INTO hatchery_site_invitations(code,site_username,created_by,created_at) VALUES(%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING",
                        (code, site_username, owner["id"], iso_time()),
                    )
                    if cur.rowcount:
                        codes.append(code)
        record_site_audit(site_username, owner["username"], "invite.generate", str(len(codes)))
        self.send_json({"ok": True, "codes": codes}, 201)

    def handle_site_admin_revoke_invite(self, site_username: str) -> None:
        owner = self.require_site_owner(site_username)
        if not owner:
            return
        code = str(self.read_json().get("code", "")).strip().lower()
        if not INVITE_PATTERN.fullmatch(code):
            raise ValueError("邀请码格式无效")
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE hatchery_site_invitations SET revoked_at = %s WHERE code = %s AND site_username = %s AND used_by IS NULL AND revoked_at IS NULL",
                    (iso_time(), code, site_username),
                )
                changed = cur.rowcount
        if not changed:
            raise ValueError("邀请码不存在、已使用或已撤销")
        record_site_audit(site_username, owner["username"], "invite.revoke", code)
        self.send_json({"ok": True})

    def handle_site_admin_forum_moderate(self, site_username: str) -> None:
        owner = self.require_site_owner(site_username)
        if not owner:
            return
        data = self.read_json()
        post_id = str(data.get("postId", ""))
        action = str(data.get("action", ""))
        if action not in ("lock", "unlock", "delete"):
            raise ValueError("论坛审核操作无效")
        with RUNTIME_LOCK:
            runtime = self.load_runtime(site_username)
            index = next((index for index, post in enumerate(runtime["forumPosts"]) if post.get("id") == post_id), -1)
            if index < 0:
                raise ValueError("话题不存在")
            if action == "delete":
                runtime["forumPosts"].pop(index)
            else:
                runtime["forumPosts"][index]["status"] = "locked" if action == "lock" else "open"
            self.save_runtime(site_username, runtime)
        record_site_audit(site_username, owner["username"], f"forum.{action}", post_id)
        self.send_json({"ok": True, "forumPosts": runtime["forumPosts"]})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/styles.css", "/mica.css", "/auth.js", "/script.js", "/viewer.html", "/viewer.js"):
            for header in ("If-Modified-Since", "If-None-Match"):
                if header in self.headers:
                    del self.headers[header]
        if parsed.path == "/api/auth/me":
            user = self.console_user()
            if not user:
                self.send_json({"authenticated": False}, 401)
            else:
                self.send_json({"authenticated": True, "user": self.attach_preview_id(self.public_user(user), user["id"])})
            return
        if parsed.path == "/api/auth/sso/authorize":
            self.handle_sso_authorize()
            return
        if parsed.path == "/api/auth/sso/callback":
            self.handle_sso_callback()
            return
        if parsed.path == "/api/auth/sessions":
            user = self.require_console_user()
            if not user:
                return
            token = self.console_token() or ""
            current_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT token_hash,created_at,expires_at,last_seen_at,ip_address,user_agent FROM console_sessions WHERE user_id = %s ORDER BY last_seen_at DESC",
                        (str(user["id"]),),
                    )
                    rows = cur.fetchall()
            sessions = [
                {
                    "current": row["token_hash"] == current_hash,
                    "createdAt": row["created_at"],
                    "expiresAt": row["expires_at"],
                    "lastSeenAt": row["last_seen_at"],
                    "ipAddress": row["ip_address"],
                    "userAgent": row["user_agent"],
                }
                for row in rows
            ]
            self.send_json({"sessions": sessions})
            return
        if parsed.path == "/api/admin/users":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT u.*,
                               COUNT(DISTINCT cs.token_hash) AS session_count,
                               MAX(d.updated_at) AS draft_updated_at
                        FROM "User" u
                        LEFT JOIN console_sessions cs ON cs.user_id = u.id AND cs.expires_at > %s
                        LEFT JOIN hatchery_site_drafts d ON d.user_id = u.id
                        GROUP BY u.id
                        ORDER BY u."createdAt" DESC
                        LIMIT 200
                        """,
                        (iso_time(),),
                    )
                    rows = cur.fetchall()
            users = [
                {
                    "username": row["name"],
                    "role": "admin" if (row_value(row, "isAdmin") or is_site_owner_campus_id(row.get("campusId"))) else "user",
                    "status": "disabled" if row_value(row, "bannedUntil") else "active",
                    "createdAt": json_time(row_value(row, "createdAt")),
                    "lastLoginAt": json_time(row_value(row, "lastLoginAt")),
                    "sessionCount": row["session_count"],
                    "draftUpdatedAt": row["draft_updated_at"],
                    "published": (PUBLISHED / row["name"] / "site.json").exists(),
                }
                for row in rows
            ]
            self.send_json({"users": users, "total": len(users)})
            return
        if parsed.path == "/api/admin/ai-usage":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            day_cutoff = iso_time(utc_now() - timedelta(hours=24))
            week_cutoff = iso_time(utc_now() - timedelta(days=7))
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT u.id AS user_id, u.name AS username, u."campusId",
                               COUNT(g.id) AS runs,
                               COALESCE(SUM(g.total_tokens), 0) AS total_tokens,
                               COALESCE(SUM(CASE WHEN g.created_at >= %s THEN g.total_tokens ELSE 0 END), 0) AS day_tokens,
                               COALESCE(SUM(CASE WHEN g.created_at >= %s THEN g.total_tokens ELSE 0 END), 0) AS week_tokens,
                               MAX(g.created_at) AS last_used_at
                        FROM hatchery_ai_usage g
                        JOIN "User" u ON u.id = g.user_id
                        GROUP BY u.id, u.name, u."campusId"
                        ORDER BY total_tokens DESC
                        LIMIT 500
                        """,
                        (day_cutoff, week_cutoff),
                    )
                    rows = cur.fetchall()
            usage = [
                {
                    "userId": row["user_id"],
                    "username": row["username"],
                    "campusId": row["campusId"],
                    "runs": int(row["runs"]),
                    "dayTokens": int(row["day_tokens"]),
                    "weekTokens": int(row["week_tokens"]),
                    "totalTokens": int(row["total_tokens"]),
                    "lastUsedAt": row["last_used_at"],
                }
                for row in rows
            ]
            totals = {
                "dayTokens": sum(item["dayTokens"] for item in usage),
                "weekTokens": sum(item["weekTokens"] for item in usage),
                "totalTokens": sum(item["totalTokens"] for item in usage),
                "runs": sum(item["runs"] for item in usage),
            }
            self.send_json({"usage": usage, "totals": totals})
            return
        if parsed.path == "/api/admin/ai-chats":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            user_id = str((parse_qs(parsed.query).get("userId") or [""])[0]).strip()
            where = "WHERE g.user_id = %s" if user_id else ""
            params: list = [user_id] if user_id else []
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT g.id, g.user_id, g.job_id, g.prompt, g.attachments_json,
                               g.provider, g.model, g.input_tokens, g.output_tokens, g.total_tokens,
                               g.status, g.created_at, u.name AS username
                        FROM hatchery_ai_usage g
                        LEFT JOIN "User" u ON u.id = g.user_id
                        {where}
                        ORDER BY g.created_at DESC
                        LIMIT 200
                        """,
                        params,
                    )
                    rows = cur.fetchall()
            chats = []
            for row in rows:
                try:
                    attachments = json.loads(row["attachments_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    attachments = []
                chats.append(
                    {
                        "id": row["id"],
                        "userId": row["user_id"],
                        "username": row["username"] or row["user_id"],
                        "jobId": row["job_id"],
                        "prompt": row["prompt"],
                        "attachments": attachments,
                        "provider": row["provider"],
                        "model": row["model"],
                        "inputTokens": int(row["input_tokens"]),
                        "outputTokens": int(row["output_tokens"]),
                        "totalTokens": int(row["total_tokens"]),
                        "status": row["status"],
                        "createdAt": row["created_at"],
                    }
                )
            self.send_json({"chats": chats})
            return
        if parsed.path == "/api/console/draft":
            user = self.require_console_user()
            if not user:
                return
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT data_json, updated_at FROM hatchery_site_drafts WHERE user_id = %s",
                        (str(user["id"]),),
                    )
                    row = cur.fetchone()
            if not row:
                self.send_json({"draft": None})
            else:
                self.send_json({"draft": json.loads(row["data_json"]), "updatedAt": row["updated_at"]})
            return
        if parsed.path == "/api/ai/run/status":
            user = self.require_console_user()
            if user:
                self.handle_ai_run_status(user, parsed)
            return
        if parsed.path == "/api/ai/status":
            status = harness_status(ROOT)
            status.update({"readableFiles": list(AI_SOURCE_FILES), "editableFiles": list(AI_EDITABLE_SOURCE_FILES)})
            self.send_json(status)
            return
        if parsed.path.startswith("/api/runtime/") or parsed.path.startswith("/api/site/"):
            self.send_json({"error": "正式发布功能暂未开放"}, 404)
            return
        preview_match = re.match(r"^/preview/([A-Za-z0-9_-]{32})(?:/|$)", parsed.path)
        if preview_match:
            preview_id = preview_match.group(1)
            ban_col = user_column_name("bannedUntil")
            ban_filter = f'AND u."{ban_col}" IS NULL' if ban_col else ""
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT sp.data_json
                        FROM hatchery_user_extras ue
                        JOIN hatchery_site_previews sp ON sp.user_id = ue.user_id
                        JOIN "User" u ON u.id = ue.user_id
                        WHERE ue.preview_id = %s {ban_filter}
                        """,
                        (preview_id,),
                    )
                    row = cur.fetchone()
            if not row:
                self.send_error(404, "Preview not found")
                return
            self.serve_preview(parsed.path, preview_id, row[0])
            return
        site_admin_match = re.fullmatch(r"/api/runtime/([A-Za-z0-9_-]{3,32})/admin/overview", parsed.path)
        if site_admin_match:
            site_username = site_admin_match.group(1)
            owner = self.require_site_owner(site_username)
            if not owner:
                return
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT username,role,status,created_at FROM hatchery_site_users WHERE site_username = %s ORDER BY id",
                        (site_username,),
                    )
                    users = [
                        {"username": row["username"], "role": row["role"], "status": row["status"], "createdAt": row["created_at"]}
                        for row in cur.fetchall()
                    ]
                    cur.execute(
                        "SELECT i.*, u.username AS used_by_name FROM hatchery_site_invitations i"
                        " LEFT JOIN hatchery_site_users u ON u.id = i.used_by AND u.site_username = i.site_username"
                        " WHERE i.site_username = %s ORDER BY i.created_at DESC LIMIT 200",
                        (site_username,),
                    )
                    invitations = [
                        {
                            "code": row["code"], "createdAt": row["created_at"], "usedAt": row["used_at"],
                            "usedBy": row["used_by_name"], "revokedAt": row["revoked_at"],
                            "status": "used" if row["used_at"] else ("revoked" if row["revoked_at"] else "available"),
                        }
                        for row in cur.fetchall()
                    ]
                    cur.execute(
                        "SELECT * FROM hatchery_site_deployments WHERE site_username = %s ORDER BY id DESC LIMIT 50",
                        (site_username,),
                    )
                    deployments = [
                        {"id": row["id"], "hash": row["content_hash"][:12], "pageCount": row["page_count"], "publishedBy": row["published_by"], "createdAt": row["created_at"]}
                        for row in cur.fetchall()
                    ]
                    cur.execute(
                        "SELECT actor,action,target,detail,created_at FROM hatchery_site_audit_log WHERE site_username = %s ORDER BY id DESC LIMIT 80",
                        (site_username,),
                    )
                    audit = [
                        {"actor": row["actor"], "action": row["action"], "target": row["target"], "detail": row["detail"], "createdAt": row["created_at"]}
                        for row in cur.fetchall()
                    ]
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM hatchery_site_sessions WHERE site_username = %s AND expires_at > %s",
                        (site_username, iso_time()),
                    )
                    active_sessions = cur.fetchone()["n"]
            with RUNTIME_LOCK:
                runtime = self.load_runtime(site_username)
            reply_count = sum(len(post.get("replies", [])) for post in runtime["forumPosts"])
            self.send_json({
                "site": site_username,
                "registrationMode": self.site_registration_mode(site_username),
                "stats": {"users": len(users), "activeUsers": sum(user["status"] == "active" for user in users), "sessions": active_sessions, "topics": len(runtime["forumPosts"]), "replies": reply_count},
                "users": users, "invitations": invitations, "deployments": deployments, "audit": audit, "forumPosts": runtime["forumPosts"],
            })
            return
        runtime_match = re.fullmatch(r"/api/runtime/([A-Za-z0-9_-]{3,32})/state", parsed.path)
        if runtime_match:
            site_username = runtime_match.group(1)
            if not (PUBLISHED / site_username / "site.json").exists():
                self.send_json({"error": "站点不存在"}, 404)
                return
            with RUNTIME_LOCK:
                runtime = self.load_runtime(site_username)
            identity = self.published_session_identity(site_username)
            user = identity["username"] if identity else None
            registration_mode = self.site_registration_mode(site_username) if self.site_account_enabled(site_username) and site_account_initialized(site_username) else "closed"
            self.send_json({"user": user, "role": identity["role"] if identity else None, "registrationMode": registration_mode, "accountEnabled": self.site_account_enabled(site_username), "forumPosts": runtime["forumPosts"], "registrations": runtime["registrations"].get(user, []) if user else [], "joined": user in runtime["joinedUsers"] if user else False})
            return
        if parsed.path == "/":
            self.path = "/index.html"
            super().do_GET()
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self.path = parsed.path
            super().do_GET()
            return
        self.send_error(404, "Not found")

    def serve_preview(self, path: str, preview_id: str, data_json: str) -> None:
        template = (ROOT / "viewer.html").read_text(encoding="utf-8")
        site = json.loads(data_json)
        site["username"] = "preview"
        site["previewMode"] = True
        site["previewId"] = preview_id
        site["basePath"] = f"/preview/{preview_id}"
        site_data = json.dumps(site, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        prefix = f"/preview/{preview_id}"
        page_path = path.removeprefix(prefix).strip("/") if path not in (prefix, f"{prefix}/") else ""
        html = template.replace("__SITE_DATA_JSON__", site_data).replace("__PAGE_PATH_JSON__", json.dumps(page_path, ensure_ascii=False))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def serve_published(self, path: str, site_username: str) -> None:
        site_file = PUBLISHED / site_username / "site.json"
        if not site_file.exists():
            body = f"<meta charset='utf-8'><title>尚未发布</title><p style='font:16px sans-serif;padding:40px'>{site_username} 目录尚未发布网站。</p>".encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        template = (ROOT / "viewer.html").read_text(encoding="utf-8")
        site_data = site_file.read_text(encoding="utf-8").replace("</", "<\\/")
        prefix = f"/{site_username}"
        page_path = path.removeprefix(prefix).strip("/") if path not in (prefix, f"{prefix}/") else ""
        html = template.replace("__SITE_DATA_JSON__", site_data).replace("__PAGE_PATH_JSON__", json.dumps(page_path, ensure_ascii=False))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def command_line() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="炼丹社Hatchery（AIchemyHatchery）服务")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="启动网站服务（默认命令）")
    serve.add_argument("--host", default=HOST, help=f"监听地址（默认：{HOST}）")
    serve.add_argument("--port", type=int, default=PORT, help=f"监听端口（默认：{PORT}）")
    create_admin = subparsers.add_parser("create-admin", help="在本机交互式创建控制台管理员")
    create_admin.add_argument("--username", help="管理员用户名；未提供时交互输入")
    return parser.parse_args()


def run_create_admin(username: str | None) -> int:
    initialize_database()
    username = (username or input("管理员用户名：")).strip()
    password = getpass.getpass("管理员密码（不会显示）：")
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        print("两次输入的密码不一致，未创建账号。", file=sys.stderr)
        return 2
    try:
        create_console_admin(username, password)
    except ValueError as error:
        print(f"创建失败：{error}", file=sys.stderr)
        return 2
    print(f"管理员 {username} 已创建。密码仅以加盐哈希写入本机数据库。")
    return 0


def run_server(host: str, port: int) -> int:
    os.chdir(ROOT)
    initialize_database()
    if console_user_count() == 0:
        print("尚未创建控制台管理员，服务未启动。", file=sys.stderr)
        print("请先运行：python server.py create-admin", file=sys.stderr)
        return 2
    migrate_existing_site_accounts()
    print(f"炼丹社Hatchery（AIchemyHatchery）服务：http://{host}:{port}")
    ThreadingHTTPServer((host, port), AIchemyHatcheryHandler).serve_forever()
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    arguments = command_line()
    if arguments.command == "create-admin":
        raise SystemExit(run_create_admin(arguments.username))
    raise SystemExit(run_server(getattr(arguments, "host", HOST), getattr(arguments, "port", PORT)))
