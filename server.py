from __future__ import annotations

import argparse
import getpass
import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
DATABASE = ROOT / "alchemy_sites.db"
LEGACY_DATABASE = ROOT / "miaoda.db"
HOST = "127.0.0.1"
PORT = 4173
RUNTIME_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
PASSWORD_ITERATIONS = 310_000
CONSOLE_SESSION_DAYS = 7
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
INVITE_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")
AI_LOCK = threading.Lock()
AI_PROPOSALS: dict[str, dict] = {}
AI_BACKUPS: dict[str, dict[str, str]] = {}
AI_SOURCE_FILES = ("index.html", "styles.css", "auth.js", "script.js", "viewer.html", "viewer.js", "server.py")
PUBLIC_STATIC_PATHS = frozenset(("/index.html", "/styles.css", "/auth.js", "/script.js", "/viewer.js"))


def read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


def write_text_exact(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        target.write(content)


def message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in value
        )
    return "" if value is None else str(value)


def extract_json_object(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


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


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def password_digest(password: str, salt: bytes | None = None, iterations: int = PASSWORD_ITERATIONS) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex()


def password_matches(password: str, salt_hex: str, expected_hex: str, iterations: int) -> bool:
    try:
        _, actual = password_digest(password, bytes.fromhex(salt_hex), iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected_hex)


def initialize_database() -> None:
    if not DATABASE.exists() and LEGACY_DATABASE.exists():
        shutil.copy2(LEGACY_DATABASE, DATABASE)
        print("已迁移旧版控制台数据库到 alchemy_sites.db")
    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_iterations INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                last_login_at TEXT,
                password_changed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY COLLATE NOCASE,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                used_by INTEGER REFERENCES users(id),
                used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS console_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT,
                ip_address TEXT,
                user_agent TEXT
            );
            CREATE TABLE IF NOT EXISTS site_drafts (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_console_sessions_user ON console_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_console_sessions_expiry ON console_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                event TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_user ON audit_events(user_id, created_at DESC);
            """
        )
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        for name, definition in (
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("last_login_at", "TEXT"),
            ("password_changed_at", "TEXT"),
        ):
            if name not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(console_sessions)")}
        for name, definition in (("last_seen_at", "TEXT"), ("ip_address", "TEXT"), ("user_agent", "TEXT")):
            if name not in session_columns:
                connection.execute(f"ALTER TABLE console_sessions ADD COLUMN {name} {definition}")


def console_user_count() -> int:
    with database() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_console_admin(username: str, password: str) -> None:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名必须为 3–32 位字母、数字、下划线或连字符")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度必须为 8–128 位")
    salt, digest = password_digest(password)
    with database() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO users(username,password_hash,password_salt,password_iterations,role,status,created_at,password_changed_at) VALUES(?,?,?,?,?,?,?,?)",
                (username, digest, salt, PASSWORD_ITERATIONS, "admin", "active", iso_time(), iso_time()),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"用户 {username} 已存在") from error
        audit_event(connection, int(cursor.lastrowid), "auth.admin_created_locally")


def audit_event(connection: sqlite3.Connection, user_id: int | None, event: str, detail: dict | None = None) -> None:
    connection.execute(
        "INSERT INTO audit_events(user_id,event,detail_json,created_at) VALUES(?,?,?,?)",
        (user_id, event, json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")), iso_time()),
    )


def site_database_path(site_username: str) -> Path:
    if not USERNAME_PATTERN.fullmatch(site_username):
        raise ValueError("站点用户名格式无效")
    return PUBLISHED / site_username / "site.db"


@contextmanager
def site_database(site_username: str):
    connection = sqlite3.connect(site_database_path(site_username), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_site_database(site_username: str, owner_username: str) -> dict | None:
    """Create an isolated account database and return first-login credentials once."""
    db_path = site_database_path(site_username)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    initial_password: str | None = None
    with site_database(site_username) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_iterations INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_site_sessions_expiry ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                code TEXT PRIMARY KEY COLLATE NOCASE,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                used_by INTEGER REFERENCES users(id),
                used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deployments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                published_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "status" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        connection.execute(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('registration_mode','open',?)",
            (iso_time(),),
        )
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            initial_password = secrets.token_urlsafe(12)
            salt, digest = password_digest(initial_password)
            connection.execute(
                "INSERT INTO users(username,password_hash,password_salt,password_iterations,role,created_at) VALUES(?,?,?,?,?,?)",
                (owner_username, digest, salt, PASSWORD_ITERATIONS, "owner", iso_time()),
            )
    if initial_password is None:
        return None
    return {"username": owner_username, "password": initial_password}


def record_site_audit(site_username: str, actor: str, action: str, target: str = "", detail: str = "") -> None:
    with site_database(site_username) as connection:
        connection.execute(
            "INSERT INTO audit_log(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
            (actor, action, target[:200], detail[:1000], iso_time()),
        )


def record_site_deployment(site_username: str, site_data: dict, published_by: str) -> None:
    serialized = json.dumps(site_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with site_database(site_username) as connection:
        connection.execute(
            "INSERT INTO deployments(content_hash,page_count,published_by,created_at) VALUES(?,?,?,?)",
            (content_hash, len(site_data.get("pages", [])), published_by, iso_time()),
        )
        connection.execute("DELETE FROM deployments WHERE id NOT IN (SELECT id FROM deployments ORDER BY id DESC LIMIT 100)")


def migrate_existing_site_databases() -> None:
    if not PUBLISHED.exists():
        return
    for site_file in PUBLISHED.glob("*/site.json"):
        site_username = site_file.parent.name
        if USERNAME_PATTERN.fullmatch(site_username) and site_database_path(site_username).exists():
            initialize_site_database(site_username, site_username)


load_env()
HOST = os.environ.get("ALCHEMY_SITES_HOST", "127.0.0.1").strip() or "127.0.0.1"
SECURE_COOKIES = os.environ.get("ALCHEMY_SITES_SECURE_COOKIES", "false").strip().lower() in ("1", "true", "yes", "on")
COOKIE_SECURITY_SUFFIX = "; Secure" if SECURE_COOKIES else ""
try:
    PORT = int(os.environ.get("ALCHEMY_SITES_PORT", "4173"))
except ValueError:
    PORT = 4173
try:
    KIMI_TIMEOUT_SECONDS = max(60, min(600, int(os.environ.get("KIMI_TIMEOUT_SECONDS", "240"))))
except ValueError:
    KIMI_TIMEOUT_SECONDS = 240


class AIchemySitesHandler(SimpleHTTPRequestHandler):
    server_version = "AIchemySitesLocal/0.1"

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
        token = cookie.get("alchemy_sites_console_session") or cookie.get("miaoda_console_session")
        return token.value if token else None

    def console_user(self) -> dict | None:
        token = self.console_token()
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = iso_time()
        with database() as connection:
            connection.execute("DELETE FROM console_sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT users.id, users.username, users.role, users.status, users.created_at,
                       users.last_login_at, console_sessions.expires_at, console_sessions.token_hash
                FROM console_sessions JOIN users ON users.id = console_sessions.user_id
                WHERE console_sessions.token_hash = ? AND console_sessions.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row and row["status"] != "active":
                connection.execute("DELETE FROM console_sessions WHERE token_hash = ?", (token_hash,))
                row = None
            elif row:
                connection.execute("UPDATE console_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))
        return dict(row) if row else None

    def require_console_user(self, admin: bool = False) -> dict | None:
        user = self.console_user()
        if not user:
            self.send_json({"error": "请先登录炼丹社Sites控制台"}, 401)
            return None
        if admin and user["role"] != "admin":
            self.send_json({"error": "只有管理员可以访问此功能"}, 403)
            return None
        return user

    def issue_console_session(self, user_id: int, remember: bool = True) -> dict[str, str]:
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created_at = utc_now()
        max_age = CONSOLE_SESSION_DAYS * 86400 if remember else 12 * 3600
        expires_at = created_at + (timedelta(days=CONSOLE_SESSION_DAYS) if remember else timedelta(hours=12))
        ip_address = self.client_address[0] if self.client_address else "unknown"
        user_agent = self.headers.get("User-Agent", "")[:300]
        with database() as connection:
            connection.execute(
                "INSERT INTO console_sessions(token_hash,user_id,created_at,expires_at,last_seen_at,ip_address,user_agent) VALUES(?,?,?,?,?,?,?)",
                (token_hash, user_id, iso_time(created_at), iso_time(expires_at), iso_time(created_at), ip_address, user_agent),
            )
        return {
            "Set-Cookie": (
                f"alchemy_sites_console_session={token}; Path=/; Max-Age={max_age}; "
                f"HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}"
            )
        }

    def public_user(self, user: dict | sqlite3.Row) -> dict:
        return {
            "id": int(user["id"]),
            "username": str(user["username"]),
            "role": str(user["role"]),
            "status": str(user["status"] if "status" in user.keys() else "active"),
            "createdAt": str(user["created_at"]),
            "lastLoginAt": user["last_login_at"] if "last_login_at" in user.keys() else None,
        }

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
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        remember = bool(data.get("remember", True))
        with database() as connection:
            row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not password_matches(password, row["password_salt"], row["password_hash"], row["password_iterations"]):
            self.record_login_failure()
            self.send_json({"error": "用户名或密码错误"}, 401)
            return
        if row["status"] != "active":
            self.send_json({"error": "此账号已被管理员停用"}, 403)
            return
        self.clear_login_failures()
        with database() as connection:
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (iso_time(), row["id"]))
            audit_event(connection, int(row["id"]), "auth.login", {"remember": remember})
            row = connection.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        headers = self.issue_console_session(int(row["id"]), remember)
        self.send_json({"ok": True, "user": self.public_user(row)}, headers=headers)

    def handle_auth_register(self) -> None:
        if self.login_is_limited():
            self.send_json({"error": "操作过于频繁，请 5 分钟后再试"}, 429)
            return
        data = self.read_json()
        username, password = self.validate_credentials(data.get("username"), data.get("password"))
        invite = str(data.get("invite", "")).strip().lower()
        if not INVITE_PATTERN.fullmatch(invite):
            raise ValueError("邀请码必须是 16 位 hex 字符")
        salt, digest = password_digest(password)
        try:
            connection = database()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                invite_row = connection.execute(
                    "SELECT code FROM invite_codes WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                    (invite,),
                ).fetchone()
                if not invite_row:
                    raise ValueError("邀请码无效、已使用或已撤销")
                cursor = connection.execute(
                    "INSERT INTO users(username,password_hash,password_salt,password_iterations,role,status,created_at,password_changed_at,last_login_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (username, digest, salt, PASSWORD_ITERATIONS, "user", "active", iso_time(), iso_time(), iso_time()),
                )
                user_id = int(cursor.lastrowid)
                changed = connection.execute(
                    "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                    (user_id, iso_time(), invite),
                ).rowcount
                if changed != 1:
                    raise ValueError("邀请码已被使用，请换一个邀请码")
                row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                audit_event(connection, user_id, "auth.register", {"invite": invite})
        except sqlite3.IntegrityError as exc:
            if "users.username" in str(exc):
                raise ValueError("用户名已存在") from exc
            raise ValueError("注册数据冲突，请重试") from exc
        finally:
            if "connection" in locals():
                connection.close()
        self.clear_login_failures()
        headers = self.issue_console_session(int(row["id"]), bool(data.get("remember", True)))
        self.send_json({"ok": True, "user": self.public_user(row)}, 201, headers)

    def handle_auth_logout(self) -> None:
        token = self.console_token()
        if token:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with database() as connection:
                connection.execute("DELETE FROM console_sessions WHERE token_hash = ?", (token_hash,))
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": [
                f"alchemy_sites_console_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
                f"miaoda_console_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
            ]},
        )

    def handle_change_password(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        old_password = str(data.get("oldPassword", ""))
        new_password = str(data.get("newPassword", ""))
        if len(new_password) < 8 or len(new_password) > 128:
            raise ValueError("新密码长度需为 8–128 位")
        with database() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not row or not password_matches(old_password, row["password_salt"], row["password_hash"], row["password_iterations"]):
                self.send_json({"error": "当前密码错误"}, 401)
                return
            salt, digest = password_digest(new_password)
            connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, password_iterations = ?, password_changed_at = ? WHERE id = ?",
                (digest, salt, PASSWORD_ITERATIONS, iso_time(), user["id"]),
            )
            connection.execute("DELETE FROM console_sessions WHERE user_id = ?", (user["id"],))
            audit_event(connection, int(user["id"]), "auth.password_changed")
        headers = self.issue_console_session(int(user["id"]), True)
        self.send_json({"ok": True}, headers=headers)

    def handle_generate_invites(self) -> None:
        user = self.require_console_user(admin=True)
        if not user:
            return
        data = self.read_json()
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("邀请码数量无效") from exc
        if count < 1 or count > 20:
            raise ValueError("每次可生成 1–20 个邀请码")
        codes: list[str] = []
        with database() as connection:
            while len(codes) < count:
                code = secrets.token_hex(8)
                try:
                    connection.execute(
                        "INSERT INTO invite_codes(code,created_by,created_at) VALUES(?,?,?)",
                        (code, user["id"], iso_time()),
                    )
                except sqlite3.IntegrityError:
                    continue
                codes.append(code)
            audit_event(connection, int(user["id"]), "invite.generated", {"count": len(codes)})
        self.send_json({"ok": True, "codes": codes}, 201)

    def handle_revoke_invite(self) -> None:
        user = self.require_console_user(admin=True)
        if not user:
            return
        code = str(self.read_json().get("code", "")).strip().lower()
        if not INVITE_PATTERN.fullmatch(code):
            raise ValueError("邀请码格式无效")
        with database() as connection:
            changed = connection.execute(
                "UPDATE invite_codes SET revoked_at = ? WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                (iso_time(), code),
            ).rowcount
            if changed:
                audit_event(connection, int(user["id"]), "invite.revoked", {"code": code})
        if not changed:
            raise ValueError("邀请码不存在、已使用或已撤销")
        self.send_json({"ok": True})

    def handle_revoke_other_sessions(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        token = self.console_token() or ""
        current_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with database() as connection:
            removed = connection.execute(
                "DELETE FROM console_sessions WHERE user_id = ? AND token_hash != ?",
                (user["id"], current_hash),
            ).rowcount
            audit_event(connection, int(user["id"]), "auth.sessions_revoked", {"count": removed})
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
        with database() as connection:
            target = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not target:
                raise ValueError("用户不存在")
            if int(target["id"]) == int(admin["id"]):
                raise ValueError("不能停用当前登录的管理员账号")
            if target["role"] == "admin" and status == "disabled":
                active_admins = connection.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'").fetchone()[0]
                if active_admins <= 1:
                    raise ValueError("系统必须至少保留一个可用管理员")
            connection.execute("UPDATE users SET status = ? WHERE id = ?", (status, target["id"]))
            if status == "disabled":
                connection.execute("DELETE FROM console_sessions WHERE user_id = ?", (target["id"],))
            audit_event(connection, int(admin["id"]), "admin.user_status", {"username": target["username"], "status": status})
        self.send_json({"ok": True, "username": target["username"], "status": status})

    def handle_save_draft(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        if not isinstance(data.get("pages"), list):
            raise ValueError("草稿缺少页面数据")
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with database() as connection:
            connection.execute(
                """
                INSERT INTO site_drafts(user_id,data_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (user["id"], serialized, iso_time()),
            )
        self.send_json({"ok": True, "updatedAt": iso_time()})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/auth/login":
                self.handle_auth_login()
            elif path == "/api/auth/register":
                self.handle_auth_register()
            elif path == "/api/auth/logout":
                self.handle_auth_logout()
            elif path == "/api/auth/change-password":
                self.handle_change_password()
            elif path == "/api/admin/invites":
                self.handle_generate_invites()
            elif path == "/api/admin/invites/revoke":
                self.handle_revoke_invite()
            elif path == "/api/admin/users/status":
                self.handle_admin_user_status()
            elif path == "/api/auth/sessions/revoke-others":
                self.handle_revoke_other_sessions()
            elif path == "/api/console/draft":
                self.handle_save_draft()
            elif path == "/api/site-account/reset-owner":
                self.handle_site_owner_reset()
            elif path in ("/api/ai", "/api/ai/propose"):
                if self.require_console_user():
                    self.handle_ai_propose()
            elif path == "/api/ai/apply":
                if self.require_console_user():
                    self.handle_ai_apply()
            elif path == "/api/ai/undo":
                if self.require_console_user():
                    self.handle_ai_undo()
            elif path == "/api/server/restart":
                if self.require_console_user():
                    self.handle_server_restart()
            elif path == "/api/publish":
                self.handle_publish()
            else:
                runtime_match = re.fullmatch(
                    r"/api/runtime/([A-Za-z0-9_-]{3,32})/(login|register|logout|forum/topics|forum/replies|registrations|join|admin/settings|admin/users/status|admin/invites/generate|admin/invites/revoke|admin/forum/moderate)",
                    path,
                )
                if not runtime_match:
                    self.send_json({"error": "接口不存在"}, 404)
                    return
                site_username, operation = runtime_match.groups()
                if not (PUBLISHED / site_username / "site.json").exists():
                    self.send_json({"error": "站点不存在"}, 404)
                elif operation == "login":
                    self.handle_runtime_login(site_username)
                elif operation == "logout":
                    self.handle_runtime_logout(site_username)
                elif operation == "register":
                    self.handle_runtime_register(site_username)
                elif operation == "forum/topics":
                    self.handle_forum_topic(site_username)
                elif operation == "forum/replies":
                    self.handle_forum_reply(site_username)
                elif operation == "registrations":
                    self.handle_registration(site_username)
                elif operation == "join":
                    self.handle_join(site_username)
                elif operation == "admin/settings":
                    self.handle_site_admin_settings(site_username)
                elif operation == "admin/users/status":
                    self.handle_site_admin_user_status(site_username)
                elif operation == "admin/invites/generate":
                    self.handle_site_admin_generate_invites(site_username)
                elif operation == "admin/invites/revoke":
                    self.handle_site_admin_revoke_invite(site_username)
                elif operation == "admin/forum/moderate":
                    self.handle_site_admin_forum_moderate(site_username)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 502)
        except Exception as exc:
            self.send_json({"error": f"本地服务错误：{exc}"}, 500)

    def kimi_request(self, prompt: str, context: dict, include_source: bool) -> dict:
        key = os.environ.get("KIMI_API_KEY", "")
        if not key:
            raise RuntimeError("本地服务未配置 Kimi API Key")
        source_bundle = {}
        if include_source:
            for name in AI_SOURCE_FILES:
                source_bundle[name] = read_text_exact(ROOT / name)
        allowed_elements = ["nav", "hero", "projects", "blog", "gallery", "stats", "team", "timeline", "forum", "account", "notice", "links", "cta", "footer", "detail"]
        system = (
            "你是炼丹社Sites（AIchemySites）项目的保守型全栈代码代理。你能看到完整站点数据和允许编辑的前后端源码。"
            "必须遵循最小修改原则：只改用户明确要求的内容；保留未提及的页面、元素、ID、文字、样式和功能。"
            "绝不因为改标题、配色或一个模块而重建整个页面；不确定时宁可不改并在 summary 说明。"
            "只输出一个 JSON 对象，不要 Markdown、代码围栏或额外文字。结构必须是："
            "{summary:string,risk:'low'|'medium'|'high',assumptions:string[],siteOperations:array,sourceChanges:array,checks:string[]}。"
            "siteOperations 支持："
            "set_site(field,value)，field 仅 siteName/description/theme/background/contentWidth/sectionGap；"
            "set_page(pageId,field,value)，field 仅 name/path；"
            "add_page(tempId,parentId,name,path)，remove_page(pageId)，"
            "add_element(tempId,pageId,type,index,settings)，update_element(pageId,elementId,settings)，"
            "remove_element(pageId,elementId)，move_element(pageId,elementId,index)，"
            "set_items(pageId,elementId,items)。新建页面或元素后若后续操作需要引用它，必须用唯一 tempId，"
            "后续 pageId/elementId 可填写该 tempId。"
            f"元素 type 只能从 {allowed_elements} 中选。settings 只放确实要改的字段。"
            "sourceChanges 每项结构为 {path,search,replace,reason}；path 只能是已提供源码文件；"
            "search 必须是源码中唯一存在的完整原文片段，replace 是替换后的完整片段。不要输出整文件。"
            "模块实例外观必须优先使用 update_element 修改 settings，绝不能为单个模块去改全局 CSS。"
            "实例样式约定：hero 标题字号用 settings.titleSize（如 clamp(38px,5.5vw,78px)）；"
            "notice 底色用 settings.background、文字色用 settings.color，其中页面主底色写 var(--page-bg)。"
            "只有用户明确说修改所有同类模块、组件默认值、底层实现或前后端功能时，才允许 sourceChanges。"
            "禁止请求或修改 .env、API Key、published、运行数据、用户文件；禁止删除文件、执行命令或新增依赖。"
            "若用户只要求页面内容，sourceChanges 必须为空；若只要求底层功能，不要顺手改页面内容。"
        )
        is_code_key = key.startswith("sk-kimi-")
        endpoint = "https://api.kimi.com/coding/v1/chat/completions" if is_code_key else "https://api.moonshot.cn/v1/chat/completions"
        model = "kimi-for-coding" if is_code_key else "kimi-k2.6"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"完整站点上下文：\n{json.dumps(context, ensure_ascii=False)}\n\n"
                f"允许编辑的源码：\n{json.dumps(source_bundle, ensure_ascii=False)}\n\n"
                f"用户要求：{prompt}"
            )},
        ]

        def request_completion(current_messages: list[dict]) -> dict:
            request_data = json.dumps({
                "model": model,
                "temperature": 1 if is_code_key else 0.2,
                "messages": current_messages,
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=request_data,
                method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=KIMI_TIMEOUT_SECONDS) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Kimi 返回 HTTP {exc.code}：{detail}") from exc
            except (TimeoutError, socket.timeout) as exc:
                raise RuntimeError(f"Kimi 在 {KIMI_TIMEOUT_SECONDS} 秒内未返回完整方案，请稍后重试；站点内容没有被修改") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"无法连接 Kimi：{exc.reason}") from exc

        result = request_completion(messages)
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        candidates = [message_text(message.get("content")), message_text(message.get("reasoning_content"))]
        for candidate in candidates:
            proposal = extract_json_object(candidate)
            if proposal is not None:
                return proposal

        first_reply = next((candidate for candidate in candidates if candidate.strip()), "")
        repair_messages = list(messages)
        if first_reply:
            repair_messages.append({"role": "assistant", "content": first_reply[:20_000]})
        repair_messages.append({"role": "user", "content": "上一条回复格式不合格。请重新输出一个严格可解析的 JSON 对象，只输出 JSON，不要解释、Markdown 或代码围栏。"})
        repaired = request_completion(repair_messages)
        repaired_message = ((repaired.get("choices") or [{}])[0].get("message") or {})
        for candidate in (message_text(repaired_message.get("content")), message_text(repaired_message.get("reasoning_content"))):
            proposal = extract_json_object(candidate)
            if proposal is not None:
                return proposal
        finish_reason = str((repaired.get("choices") or [{}])[0].get("finish_reason", "unknown"))
        raise RuntimeError(f"Kimi 连续两次未返回可解析的 JSON 方案（finish_reason={finish_reason}），站点内容没有被修改")

    def validate_ai_proposal(self, proposal: dict, include_source: bool) -> dict:
        if not isinstance(proposal, dict):
            raise ValueError("Kimi 返回的方案不是对象")
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
                raise ValueError("Kimi 返回了不支持的站点操作")
        normalized_changes = []
        for change in changes if include_source else []:
            if not isinstance(change, dict) or change.get("path") not in AI_SOURCE_FILES:
                raise ValueError("Kimi 尝试修改未授权文件")
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

    def handle_ai_propose(self) -> None:
        payload = self.read_json()
        prompt = str(payload.get("prompt", "")).strip()
        context = payload.get("context", {})
        include_site = bool(payload.get("includeSite", True))
        include_source = bool(payload.get("includeSource", True))
        if not prompt:
            raise ValueError("调整描述不能为空")
        if not isinstance(context, dict):
            raise ValueError("站点上下文格式错误")
        proposal = self.validate_ai_proposal(self.kimi_request(prompt, context, include_source), include_source)
        proposal = self.constrain_instance_intent(proposal, prompt, context)
        if not include_site:
            proposal["siteOperations"] = []
        proposal_id = f"proposal-{secrets.token_hex(10)}"
        proposal["id"] = proposal_id
        with AI_LOCK:
            AI_PROPOSALS[proposal_id] = proposal
            while len(AI_PROPOSALS) > 20:
                AI_PROPOSALS.pop(next(iter(AI_PROPOSALS)))
        self.send_json({"proposal": proposal, "editableFiles": list(AI_SOURCE_FILES)})

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

        threading.Thread(target=restart, name="alchemy-sites-restart", daemon=False).start()

    def handle_publish(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        username = str(user["username"])
        data["username"] = username
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
            html = str(page.get("html", ""))
            if 'class="forum-block"' in html and 'class="account-block"' not in html:
                page["html"] = html.replace('<section class="forum-block">', required_account + '<section class="forum-block">', 1)
        has_account = any('class="account-block"' in str(page.get("html", "")) for page in pages)
        data["features"] = {**(data.get("features") if isinstance(data.get("features"), dict) else {}), "account": has_account}
        user_dir = PUBLISHED / username
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "site.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        site_admin = initialize_site_database(username, username) if has_account else None
        if has_account:
            record_site_deployment(username, data, username)
            record_site_audit(username, username, "site.publish", f"/{username}", f"{len(pages)} pages")
        with RUNTIME_LOCK:
            self.load_runtime(username)
        self.send_json({"ok": True, "url": f"/{username}", "accountEnabled": has_account, "siteAdmin": site_admin})

    def handle_site_owner_reset(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        site_username = str(user["username"])
        if not self.site_account_enabled(site_username) or not site_database_path(site_username).exists():
            raise ValueError("当前发布站点没有启用账号系统")
        new_password = secrets.token_urlsafe(12)
        salt, digest = password_digest(new_password)
        with site_database(site_username) as connection:
            owner = connection.execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
            if owner:
                connection.execute(
                    "UPDATE users SET username = ?, password_hash = ?, password_salt = ?, password_iterations = ? WHERE id = ?",
                    (site_username, digest, salt, PASSWORD_ITERATIONS, owner["id"]),
                )
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (owner["id"],))
            else:
                connection.execute(
                    "INSERT INTO users(username,password_hash,password_salt,password_iterations,role,created_at) VALUES(?,?,?,?,?,?)",
                    (site_username, digest, salt, PASSWORD_ITERATIONS, "owner", iso_time()),
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
        return f"alchemy_sites_{hashlib.sha256(site_username.lower().encode('utf-8')).hexdigest()[:12]}"

    def legacy_site_cookie_name(self, site_username: str) -> str:
        return f"miaoda_site_{hashlib.sha256(site_username.lower().encode('utf-8')).hexdigest()[:12]}"

    def published_session_identity(self, site_username: str) -> dict | None:
        if not self.site_account_enabled(site_username) or not site_database_path(site_username).exists():
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get(self.site_cookie_name(site_username)) or cookie.get(self.legacy_site_cookie_name(site_username))
        if not token:
            return None
        token_hash = hashlib.sha256(token.value.encode("utf-8")).hexdigest()
        now = iso_time()
        with site_database(site_username) as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT users.id, users.username, users.role, users.status FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.status = 'active'",
                (token_hash, now),
            ).fetchone()
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
        with site_database(site_username) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = 'registration_mode'").fetchone()
        mode = str(row["value"]) if row else "open"
        return mode if mode in ("open", "invite", "closed") else "open"

    def issue_site_session(self, site_username: str, user_id: int) -> dict[str, str]:
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created_at = utc_now()
        expires_at = created_at + timedelta(days=CONSOLE_SESSION_DAYS)
        with site_database(site_username) as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (token_hash, user_id, iso_time(created_at), iso_time(expires_at)),
            )
        cookie_name = self.site_cookie_name(site_username)
        return {"Set-Cookie": f"{cookie_name}={token}; Path=/; Max-Age={CONSOLE_SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}"}

    def require_published_user(self, site_username: str) -> str | None:
        user = self.published_session_user(site_username)
        if not user:
            self.send_json({"error": "请先登录站点账号"}, 401)
        return user

    def handle_runtime_login(self, site_username: str) -> None:
        if not self.site_account_enabled(site_username) or not site_database_path(site_username).exists():
            self.send_json({"error": "该站点账号系统尚未初始化，请站长重新发布"}, 409)
            return
        if self.login_is_limited():
            self.send_json({"error": "登录尝试过于频繁，请稍后再试"}, 429)
            return
        data = self.read_json()
        username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
        with site_database(site_username) as connection:
            row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not password_matches(password, row["password_salt"], row["password_hash"], row["password_iterations"]):
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
        if not self.site_account_enabled(site_username) or not site_database_path(site_username).exists():
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
        salt, digest = password_digest(password)
        try:
            with site_database(site_username) as connection:
                if registration_mode == "invite":
                    connection.execute("BEGIN IMMEDIATE")
                    invite_row = connection.execute(
                        "SELECT code FROM invitations WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                        (invite,),
                    ).fetchone()
                    if not invite_row:
                        raise ValueError("本站邀请码无效、已使用或已撤销")
                cursor = connection.execute(
                    "INSERT INTO users(username,password_hash,password_salt,password_iterations,role,created_at) VALUES(?,?,?,?,?,?)",
                    (username, digest, salt, PASSWORD_ITERATIONS, "member", iso_time()),
                )
                user_id = int(cursor.lastrowid)
                if registration_mode == "invite":
                    connection.execute(
                        "UPDATE invitations SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
                        (user_id, iso_time(), invite),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该站点中已存在这个用户名") from exc
        record_site_audit(site_username, username, "auth.register", username, registration_mode)
        self.send_json({"ok": True, "user": username, "role": "member"}, 201, self.issue_site_session(site_username, user_id))

    def handle_runtime_logout(self, site_username: str) -> None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        cookie_name = self.site_cookie_name(site_username)
        token = cookie.get(cookie_name)
        if token and site_database_path(site_username).exists():
            token_hash = hashlib.sha256(token.value.encode("utf-8")).hexdigest()
            with site_database(site_username) as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        self.send_json({"ok": True}, headers={"Set-Cookie": [
            f"{cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
            f"{self.legacy_site_cookie_name(site_username)}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{COOKIE_SECURITY_SUFFIX}",
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
        with site_database(site_username) as connection:
            connection.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES('registration_mode',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (mode, iso_time()),
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
        with site_database(site_username) as connection:
            target = connection.execute("SELECT id,role FROM users WHERE username = ?", (username,)).fetchone()
            if not target:
                raise ValueError("成员不存在")
            if target["role"] == "owner":
                raise ValueError("不能停用站点所有者")
            connection.execute("UPDATE users SET status = ? WHERE id = ?", (status, target["id"]))
            if status == "suspended":
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))
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
        with site_database(site_username) as connection:
            while len(codes) < count:
                code = secrets.token_hex(8)
                try:
                    connection.execute(
                        "INSERT INTO invitations(code,created_by,created_at) VALUES(?,?,?)",
                        (code, owner["id"], iso_time()),
                    )
                except sqlite3.IntegrityError:
                    continue
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
        with site_database(site_username) as connection:
            changed = connection.execute(
                "UPDATE invitations SET revoked_at = ? WHERE code = ? AND used_by IS NULL AND revoked_at IS NULL",
                (iso_time(), code),
            ).rowcount
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
        if parsed.path in ("/", "/index.html", "/styles.css", "/auth.js", "/script.js", "/viewer.html", "/viewer.js"):
            for header in ("If-Modified-Since", "If-None-Match"):
                if header in self.headers:
                    del self.headers[header]
        if parsed.path == "/api/auth/me":
            user = self.console_user()
            if not user:
                self.send_json({"authenticated": False}, 401)
            else:
                self.send_json({"authenticated": True, "user": self.public_user(user)})
            return
        if parsed.path == "/api/auth/sessions":
            user = self.require_console_user()
            if not user:
                return
            token = self.console_token() or ""
            current_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with database() as connection:
                rows = connection.execute(
                    "SELECT token_hash,created_at,expires_at,last_seen_at,ip_address,user_agent FROM console_sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
                    (user["id"],),
                ).fetchall()
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
        if parsed.path == "/api/admin/invites":
            user = self.require_console_user(admin=True)
            if not user:
                return
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT invite_codes.code, invite_codes.created_at, invite_codes.used_at, invite_codes.revoked_at,
                           creator.username AS created_by, consumer.username AS used_by
                    FROM invite_codes
                    JOIN users AS creator ON creator.id = invite_codes.created_by
                    LEFT JOIN users AS consumer ON consumer.id = invite_codes.used_by
                    ORDER BY invite_codes.created_at DESC
                    LIMIT 200
                    """
                ).fetchall()
            invites = [
                {
                    "code": row["code"],
                    "createdAt": row["created_at"],
                    "createdBy": row["created_by"],
                    "usedAt": row["used_at"],
                    "usedBy": row["used_by"],
                    "revokedAt": row["revoked_at"],
                    "status": "used" if row["used_at"] else ("revoked" if row["revoked_at"] else "available"),
                }
                for row in rows
            ]
            self.send_json({"invites": invites})
            return
        if parsed.path == "/api/admin/users":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT users.id,users.username,users.role,users.status,users.created_at,users.last_login_at,
                           COUNT(DISTINCT console_sessions.token_hash) AS session_count,
                           site_drafts.updated_at AS draft_updated_at
                    FROM users
                    LEFT JOIN console_sessions ON console_sessions.user_id = users.id AND console_sessions.expires_at > ?
                    LEFT JOIN site_drafts ON site_drafts.user_id = users.id
                    GROUP BY users.id
                    ORDER BY users.created_at DESC
                    LIMIT 200
                    """,
                    (iso_time(),),
                ).fetchall()
            users = [
                {
                    "username": row["username"],
                    "role": row["role"],
                    "status": row["status"],
                    "createdAt": row["created_at"],
                    "lastLoginAt": row["last_login_at"],
                    "sessionCount": row["session_count"],
                    "draftUpdatedAt": row["draft_updated_at"],
                    "published": (PUBLISHED / row["username"] / "site.json").exists(),
                }
                for row in rows
            ]
            self.send_json({"users": users, "total": len(users)})
            return
        if parsed.path == "/api/console/draft":
            user = self.require_console_user()
            if not user:
                return
            with database() as connection:
                row = connection.execute("SELECT data_json, updated_at FROM site_drafts WHERE user_id = ?", (user["id"],)).fetchone()
            if not row:
                self.send_json({"draft": None})
            else:
                self.send_json({"draft": json.loads(row["data_json"]), "updatedAt": row["updated_at"]})
            return
        if parsed.path == "/api/ai/status":
            self.send_json({"configured": bool(os.environ.get("KIMI_API_KEY")), "provider": "Kimi", "model": "kimi-for-coding" if os.environ.get("KIMI_API_KEY", "").startswith("sk-kimi-") else "kimi-k2.6", "editableFiles": list(AI_SOURCE_FILES)})
            return
        site_admin_match = re.fullmatch(r"/api/runtime/([A-Za-z0-9_-]{3,32})/admin/overview", parsed.path)
        if site_admin_match:
            site_username = site_admin_match.group(1)
            owner = self.require_site_owner(site_username)
            if not owner:
                return
            with site_database(site_username) as connection:
                users = [
                    {"username": row["username"], "role": row["role"], "status": row["status"], "createdAt": row["created_at"]}
                    for row in connection.execute("SELECT username,role,status,created_at FROM users ORDER BY id").fetchall()
                ]
                invitations = [
                    {
                        "code": row["code"], "createdAt": row["created_at"], "usedAt": row["used_at"],
                        "usedBy": row["used_by_name"], "revokedAt": row["revoked_at"],
                        "status": "used" if row["used_at"] else ("revoked" if row["revoked_at"] else "available"),
                    }
                    for row in connection.execute(
                        "SELECT invitations.*, users.username AS used_by_name FROM invitations LEFT JOIN users ON users.id=invitations.used_by ORDER BY invitations.created_at DESC LIMIT 200"
                    ).fetchall()
                ]
                deployments = [
                    {"id": row["id"], "hash": row["content_hash"][:12], "pageCount": row["page_count"], "publishedBy": row["published_by"], "createdAt": row["created_at"]}
                    for row in connection.execute("SELECT * FROM deployments ORDER BY id DESC LIMIT 50").fetchall()
                ]
                audit = [
                    {"actor": row["actor"], "action": row["action"], "target": row["target"], "detail": row["detail"], "createdAt": row["created_at"]}
                    for row in connection.execute("SELECT actor,action,target,detail,created_at FROM audit_log ORDER BY id DESC LIMIT 80").fetchall()
                ]
                active_sessions = connection.execute("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (iso_time(),)).fetchone()[0]
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
            registration_mode = self.site_registration_mode(site_username) if self.site_account_enabled(site_username) and site_database_path(site_username).exists() else "closed"
            self.send_json({"user": user, "role": identity["role"] if identity else None, "registrationMode": registration_mode, "accountEnabled": self.site_account_enabled(site_username), "forumPosts": runtime["forumPosts"], "registrations": runtime["registrations"].get(user, []) if user else [], "joined": user in runtime["joinedUsers"] if user else False})
            return
        site_match = re.fullmatch(r"/api/site/([A-Za-z0-9_-]{3,32})", parsed.path)
        if site_match:
            site_username = site_match.group(1)
            site_file = PUBLISHED / site_username / "site.json"
            if not site_file.exists():
                self.send_json({"error": "尚未发布"}, 404)
            else:
                self.send_json(json.loads(site_file.read_text(encoding="utf-8")))
            return
        published_match = re.match(r"^/([A-Za-z0-9_-]{3,32})(?:/|$)", parsed.path)
        if published_match and (PUBLISHED / published_match.group(1) / "site.json").exists():
            self.serve_published(parsed.path, published_match.group(1))
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
    parser = argparse.ArgumentParser(description="炼丹社Sites（AIchemySites）服务")
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
    migrate_existing_site_databases()
    print(f"炼丹社Sites（AIchemySites）服务：http://{host}:{port}")
    ThreadingHTTPServer((host, port), AIchemySitesHandler).serve_forever()
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    arguments = command_line()
    if arguments.command == "create-admin":
        raise SystemExit(run_create_admin(arguments.username))
    raise SystemExit(run_server(getattr(arguments, "host", HOST), getattr(arguments, "port", PORT)))
