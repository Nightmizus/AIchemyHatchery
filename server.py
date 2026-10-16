from __future__ import annotations

import argparse
import base64
import getpass
import html
import json
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import smtplib
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from email.mime.text import MIMEText
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
import urllib.request
import urllib.error
import urllib.parse as urlparse_mod

import site_files
import ai_jobs
import login_links
import study_engine
import site_runtime

from deepseek_harness_adapter import HarnessRuntimeError, harness_status, resolve_llm_provider, run_deepseek_harness

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
# 发布新子域名后要通知 root 的 hatchery-cert-sync 扩容 TLS 证书。子域名是灰云直连
# 源站，证书里没有的名字会直接报 ERR_CERT_COMMON_NAME_INVALID。
CERT_SYNC_TRIGGER = ROOT / "var" / "cert-sync-request"
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

_DB_POOL: pg_pool.ThreadedConnectionPool | None = None
_DB_POOL_LOCK = threading.Lock()
# 连接空闲超过这个秒数才补一次 SELECT 1 探活；每请求都探活等于每个接口多付一次
# 新加坡往返（约 90ms），而那正是登录态接口慢的主因。
_DB_IDLE_SINCE: dict[int, float] = {}
_DB_PROBE_AFTER_IDLE_SECONDS = 45.0


def db_pool() -> pg_pool.ThreadedConnectionPool:
    """惰性创建连接池，让 Neon 的建连（TLS 握手/冷启动）成本只付一次，
    而不是像从前那样每个请求都重新 psycopg2.connect（页面加载因此卡约 20 秒）。"""
    global _DB_POOL
    if _DB_POOL is None:
        with _DB_POOL_LOCK:
            if _DB_POOL is None:
                _DB_POOL = pg_pool.ThreadedConnectionPool(1, 8, NEON_DATABASE_URL)
    return _DB_POOL


@contextmanager
def neon_db(autocommit: bool = False):
    """Yield a pooled Neon PostgreSQL connection with RealDictCursor.

    空闲较久的连接才探活；Neon 断掉空闲连接时 rollback() 会立刻抛错，
    这里丢弃旧连接、换一条新连接重试一次，避免把偶发断连暴露给用户。

    只读接口传 autocommit=True：psycopg2 在事务模式下为一条 SELECT 要额外付
    BEGIN 与 COMMIT 两次新加坡往返（约 180ms），autocommit 下只付一次。
    """
    pool = db_pool()
    for attempt in (0, 1):
        conn = pool.getconn()
        try:
            idle_since = _DB_IDLE_SINCE.pop(id(conn), 0.0)
            if conn.closed:
                raise psycopg2.InterfaceError("连接已关闭")
            # Neon 连接池可能归还残留事务的会话；先回滚再设 autocommit，
            # 否则 set_session 会抛 "cannot be used inside a transaction"。
            conn.rollback()
            conn.autocommit = True
            if (time.monotonic() - idle_since) > _DB_PROBE_AFTER_IDLE_SECONDS:
                with conn.cursor() as probe:
                    probe.execute("SELECT 1")
            conn.autocommit = autocommit
        except psycopg2.Error:
            _DB_IDLE_SINCE.pop(id(conn), None)
            pool.putconn(conn, close=True)
            if attempt:
                raise
            continue
        try:
            yield conn
            if not autocommit:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except psycopg2.Error:
                pass
            raise
        finally:
            _DB_IDLE_SINCE[id(conn)] = time.monotonic()
            pool.putconn(conn)
        return


# 登录态缓存：一次页面启动要打 5～6 个接口，每个都重新校验会话意味着每个接口
# 都要付一次（甚至两次）新加坡往返。缓存只需覆盖这几秒的启动突发。
CONSOLE_SESSION_CACHE_SECONDS = 15.0
_CONSOLE_SESSION_CACHE: dict[str, tuple[float, dict]] = {}
_CONSOLE_SESSION_CACHE_LOCK = threading.Lock()
_LAST_SESSION_SWEEP = 0.0
SESSION_SWEEP_INTERVAL_SECONDS = 600.0


def invalidate_console_sessions(token_hash: str | None = None) -> None:
    """登出/改密/踢设备/停用账号后必须调用，否则旧缓存会继续放行。"""
    with _CONSOLE_SESSION_CACHE_LOCK:
        if token_hash:
            _CONSOLE_SESSION_CACHE.pop(token_hash, None)
        else:
            _CONSOLE_SESSION_CACHE.clear()


def cached_console_session(token_hash: str) -> dict | None:
    with _CONSOLE_SESSION_CACHE_LOCK:
        entry = _CONSOLE_SESSION_CACHE.get(token_hash)
    if not entry or entry[0] <= time.monotonic():
        return None
    return dict(entry[1])


def store_console_session(token_hash: str, user: dict) -> None:
    with _CONSOLE_SESSION_CACHE_LOCK:
        _CONSOLE_SESSION_CACHE[token_hash] = (time.monotonic() + CONSOLE_SESSION_CACHE_SECONDS, dict(user))


def sweep_expired_console_sessions(cur, now: str) -> None:
    """清理过期会话从每请求一次改为每 10 分钟一次，省掉绝大多数请求的一次往返。"""
    global _LAST_SESSION_SWEEP
    stamp = time.monotonic()
    if (stamp - _LAST_SESSION_SWEEP) < SESSION_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_SESSION_SWEEP = stamp
    cur.execute("DELETE FROM console_sessions WHERE expires_at <= %s", (now,))


# 一条语句完成：刷新 last_seen_at、取出用户、带上预览号与发布站信息，
# 替代原先「探活 + 删过期 + 查会话 + 更新 + 再开一条连接查两次」的 8 次往返。
CONSOLE_USER_QUERY = """
WITH touched AS (
    UPDATE console_sessions
       SET last_seen_at = %(now)s
     WHERE token_hash = %(token_hash)s AND expires_at > %(now)s
 RETURNING user_id, expires_at, token_hash
)
SELECT u.*,
       t.expires_at   AS session_expires_at,
       t.token_hash   AS session_token_hash,
       extras.preview_id      AS extra_preview_id,
       sites.slug             AS published_slug,
       sites.listed_in_gallery AS published_listed
  FROM touched t
  JOIN "User" u ON u.id = t.user_id
  LEFT JOIN hatchery_user_extras extras ON extras.user_id = u.id
  LEFT JOIN hatchery_published_sites sites ON sites.user_id = u.id
"""

PREVIEW_EXTRAS_QUERY = """
SELECT (SELECT preview_id FROM hatchery_user_extras WHERE user_id = %s),
       (SELECT slug FROM hatchery_published_sites WHERE user_id = %s),
       (SELECT listed_in_gallery FROM hatchery_published_sites WHERE user_id = %s)
"""


AI_JOB_STORE = ai_jobs.JobStore(lambda: neon_db(), read_db=lambda: neon_db(autocommit=True))
RUNTIME_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
# 通用限流桶（按 "用途:标识" 计数），供验证码发送/校验等敏感接口使用
RATE_BUCKETS: dict[str, list[float]] = {}
RATE_BUCKETS_LOCK = threading.Lock()
# 管理员在面板里改 AI Key 时，.env 写入与 os.environ 更新必须串行
AI_CONFIG_LOCK = threading.Lock()
AI_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{6,199}$")
PASSWORD_ITERATIONS = 310_000
CONSOLE_SESSION_DAYS = 7
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
INVITE_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")
PREVIEW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")


def rate_limit_hit(scope: str, identity: str, limit: int, window: float) -> bool:
    """记录一次调用；window 秒内超过 limit 次返回 True（漏洞：敏感接口缺少限流）。"""
    key = f"{scope}:{identity}"
    now = time.time()
    with RATE_BUCKETS_LOCK:
        recent = [item for item in RATE_BUCKETS.get(key, []) if item > now - window]
        recent.append(now)
        RATE_BUCKETS[key] = recent
        if len(RATE_BUCKETS) > 20_000:
            for stale in [name for name, items in RATE_BUCKETS.items() if not items or items[-1] <= now - window]:
                RATE_BUCKETS.pop(stale, None)
        return len(recent) > limit
# 发布路径会作为站点子域名（如 xxx.aichemy.club），必须是 DNS label 安全的小写形式（不允许连续短横线）
SITE_SLUG_PATTERN = re.compile(r"^(?=.{3,32}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_SITE_SLUGS = frozenset({
    "www", "api", "app", "mail", "smtp", "admin", "console", "pages", "preview", "gallery",
    "static", "assets", "hatchery", "mizusumi", "aichemy", "localhost", "ftp", "ns1", "ns2",
})


def site_slug_from_host(host: str) -> str | None:
    """从 Host 头解析发布站点子域名，如 campus-news.aichemy.club → campus-news。"""
    name = host.split(":", 1)[0].strip().lower()
    for pattern in SITE_HOST_PATTERNS:
        match = pattern.fullmatch(name)
        if match and match.group(1) not in RESERVED_SITE_SLUGS:
            return match.group(1)
    return None
AI_LOCK = threading.Lock()
# 跨账号并发上限；同一账号的任务由持久队列顺序执行。
AI_RUN_MAX_PARALLEL = 2
_AI_CAPACITY_CACHE = None


def ai_parallel_limit():
    """Reload only the capacity setting; do not restart or cancel running work."""
    global _AI_CAPACITY_CACHE
    path = ROOT / '.env'
    try:
        info = path.stat()
        identity = (info.st_ino, info.st_mtime_ns, info.st_size)
        if _AI_CAPACITY_CACHE and _AI_CAPACITY_CACHE[0] == identity:
            return _AI_CAPACITY_CACHE[1]
        value = os.environ.get('HATCHERY_AI_MAX_PARALLEL', str(AI_RUN_MAX_PARALLEL))
        for line in path.read_text().splitlines():
            match = re.fullmatch(r'\s*HATCHERY_AI_MAX_PARALLEL\s*=\s*([1-4])\s*', line)
            if match:
                value = match.group(1)
        limit = min(4, max(1, int(value)))
        _AI_CAPACITY_CACHE = (identity, limit)
        return limit
    except (OSError, ValueError):
        return _AI_CAPACITY_CACHE[1] if _AI_CAPACITY_CACHE else AI_RUN_MAX_PARALLEL
# HTTP synchronous compatibility uses the same durable queue as async clients.
AI_BACKUPS: dict[str, dict[str, str]] = {}
# 上游 AI 当前是否处于"额度耗尽"等降级状态：用户一打开编辑器就该知道，
# 而不是写完需求点发送、白等几秒才收到失败。任务成功即自动清除。
AI_UPSTREAM_DEGRADED: dict | None = None
# 前端静态文件统一放在 frontend/；对外 URL 仍是根路径（/styles.css…），由 handler 翻译
FRONTEND_DIR = "frontend"
AI_SOURCE_FILES = ("server.py", "frontend/index.html", "frontend/styles.css", "frontend/mica.css", "frontend/ai-chat.css", "frontend/motion.css", "frontend/auth.js", "frontend/script.js", "frontend/viewer.html", "frontend/viewer.js")
PUBLIC_STATIC_PATHS = frozenset(("/index.html", "/styles.css", "/mica.css", "/ai-chat.css", "/motion.css", "/auth.js", "/script.js", "/viewer.js", "/study.css", "/study.js", "/markdown-it.min.js", "/ai-markdown.js"))
# 这些资源在页面里一律带 ?v= 版本号引用，可以放心让浏览器/边缘长期缓存；
# HTML 外壳（/ 与 /index.html）必须每次校验，否则发布与登录态更新会滞后。
VERSIONED_STATIC_PATHS = PUBLIC_STATIC_PATHS - {"/index.html"}
_CONSOLE_ASSET_CACHE = {}


def console_asset_version(path):
    if path not in VERSIONED_STATIC_PATHS:
        return ''
    target = ROOT / FRONTEND_DIR / path.lstrip('/')
    info = target.stat()
    identity = (str(target), info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _CONSOLE_ASSET_CACHE.get(path)
    if not cached or cached[0] != identity:
        cached = (identity, hashlib.sha256(target.read_bytes()).hexdigest()[:20])
        _CONSOLE_ASSET_CACHE[path] = cached
    return cached[1]
# 站点图标：🐣（小鸡破壳，呼应 Hatchery）。SVG 文本emoji，所有现代浏览器可直接做 favicon
FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🐣</text></svg>'

# 写死的站长账号：数字校园号为 20264689 的用户始终是站长（管理员），
# 不依赖数据库里的 isAdmin 标记，也不能被停用。
SITE_OWNER_CAMPUS_ID = "20264689"


# 从零建站时注入的随机风格灵感（用户没给线索时用），保证每次生成的整体风格不重样
SITE_STYLE_SEEDS = [
    "深色科技感：暗底、荧光点缀、等宽字体氛围（theme 可选 terminal）",
    "极简留白：大量留白、细线分割、黑白灰（theme 可选 minimal）",
    "复古编辑部：米色纸感、衬线大标题、杂志分栏（theme 可选 editorial）",
    "活力撞色：高饱和对比色、圆角卡片、大字标语（background 用亮色）",
    "日系清新：浅色柔和、细字重、淡雅配色（theme 可选 soft）",
    "赛博霓虹：深色底、霓虹渐变、发光边框（background 用近黑色）",
    "学院网格：蓝白校色、正式网格、突出数据栏（theme 可选 editorial）",
    "自然户外：大地色系、粗边框、手作质感（theme 可选 soft）",
]


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


def user_site_directory(user_id) -> tuple[Path, str]:
    """Ownership is resolved by the server, never by client metadata or a tool path."""
    with neon_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM hatchery_published_sites WHERE user_id = %s", (str(user_id),))
            row = cur.fetchone()
    slug = str(row[0]) if row else ""
    if slug and not SITE_SLUG_PATTERN.fullmatch(slug):
        raise ValueError("网站发布路径无效")
    directory = PUBLISHED / slug if slug else site_files.draft_directory(PUBLISHED, user_id)
    site_files.confined_path(directory, "site.json")
    return directory, slug


def ai_site_workspace(user_id) -> Path:
    return user_site_directory(user_id)[0]


# ===== StudyPanel：每用户站点目录下的学习面板存储 =====
STUDY_OUTLINE_MAX_SIZE = 500_000   # 单份大纲文件上限 500 KB
STUDY_OUTLINE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9一-鿿_\-\.（）()【】 ]{1,120}\.[A-Za-z0-9]{1,10}$")


def study_panel_dir(user_id) -> Path:
    """返回该用户的 studypanel 根目录，不保证已创建。"""
    site_dir, _ = user_site_directory(user_id)
    return site_dir / "studypanel"


def ensure_study_panel_dirs(user_id) -> Path:
    """确保 studypanel/ outlines/ 和 progress.md 都存在，返回 studypanel 根目录。"""
    sp = study_panel_dir(user_id)
    outlines = sp / "outlines"
    outlines.mkdir(parents=True, exist_ok=True)
    progress_file = sp / "progress.md"
    if not progress_file.exists():
        progress_file.write_text("# 学习进度\n\n", encoding="utf-8")
    return sp


# ===== 用户上传给 AI 的附件：统一落到服务器临时目录，换取稳定 URL 供 AI 嵌入网页 =====
# 设计要点：
# 1) 附件（图片/视频/任意文件）不一定是要 AI「读」，更多是要放进网页里——AI 拿到公开 URL 即可嵌入；
# 2) 图片是否作为视觉内容块喂给模型由适配器能力决定（kimi/glm 均为纯文本，见 deepseek_harness_adapter._content_blocks），
#    纯文本适配器收到图片块会直接报 UNSUPPORTED_CONTENT，因此默认不发图片块，接入多模态后置 HATCHERY_AI_VISION=1；
# 3) 临时目录按用户/附件两级存放，保留 7 天后自动清理。
AI_ATTACHMENT_ROOT = ROOT / "tmp" / "ai-attachments"
AI_ATTACHMENT_MAX_AGE = 7 * 86400
AI_ATTACHMENT_URL_PATTERN = re.compile(r"^/attachments/ai/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{6,64})/(.+)$")


def parse_ai_attachments(raw_attachments: object) -> list[dict]:
    """校验并规范化请求里的附件列表：图片/文本喂给 AI 与网页，二进制（视频等）落盘换 URL 嵌入网页。"""
    if not isinstance(raw_attachments, list) or len(raw_attachments) > 4:
        raise ValueError("一次最多添加 4 个附件")
    attachments: list[dict] = []
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
        elif kind == "file":
            # 视频/音频/压缩包等二进制：AI 不必“读”它，落盘换 URL 后嵌入网页即可
            decode_data_url(content)  # 校验 data:…;base64, 合法且可解码，坏内容直接拒
            if len(content) > 8_500_000:
                raise ValueError(f"附件 {name} 超过大小限制")
        else:
            raise ValueError(f"附件 {name} 类型不受支持")
        total_attachment_size += size
        if total_attachment_size > 8 * 1024 * 1024:
            raise ValueError("附件总大小不能超过 8 MB")
        attachments.append({"name": name, "kind": kind, "type": mime_type, "content": content})
    return attachments


def ai_vision_enabled() -> bool:
    return os.environ.get("HATCHERY_AI_VISION", "").strip().lower() in ("1", "true", "yes", "on")


def sweep_stale_ai_attachments() -> None:
    """清理超过保留期的附件目录（发布出去的页面里 URL 失效属于预期：本来就是临时区）。"""
    now = time.time()
    try:
        for user_dir in AI_ATTACHMENT_ROOT.iterdir():
            if not user_dir.is_dir():
                continue
            for attach_dir in user_dir.iterdir():
                try:
                    if attach_dir.is_dir() and attach_dir.stat().st_mtime < now - AI_ATTACHMENT_MAX_AGE:
                        shutil.rmtree(attach_dir, ignore_errors=True)
                except OSError:
                    pass
    except OSError:
        pass


def decode_data_url(content: str) -> tuple[str, bytes]:
    """解析 data:<mime>;base64,<payload>，返回 (mime, 原始字节)；格式非法抛 ValueError。"""
    match = re.fullmatch(r"data:([A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*);base64,([A-Za-z0-9+/=\r\n]+)", str(content or ""))
    if not match:
        raise ValueError("附件内容不是合法的 base64 数据")
    try:
        return match.group(1), base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise ValueError("附件内容不是合法的 base64 数据") from exc


def save_ai_attachments(user_id: str, attachments: list[dict], workspace: Path | None = None) -> list[dict]:
    """新任务附件永久写入网站 assets/uploads；无 workspace 时兼容历史附件调用。"""
    manifest: list[dict] = []
    if not attachments:
        return manifest
    if workspace is None:
        sweep_stale_ai_attachments()
    safe_user = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))[:64] or "unknown"
    for item in attachments:
        name = Path(str(item.get("name", "附件")).replace("\\", "/")).name or "attachment"
        kind = str(item.get("kind", "text"))
        if kind == "text":
            mime_type, payload = str(item.get("type", "text/plain")), str(item.get("content", "")).encode("utf-8")
        else:
            try:
                mime_type, payload = decode_data_url(str(item.get("content", "")))
            except ValueError:
                continue  # 单个附件损坏不拖垮整个任务
        attach_id = secrets.token_hex(8)
        relative = f"assets/uploads/{attach_id}/{name}"
        target = site_files.confined_path(workspace, relative) if workspace else AI_ATTACHMENT_ROOT / safe_user / attach_id / name
        site_files.atomic_write(target, payload)
        manifest.append({"name": name, "kind": kind, "type": mime_type, "size": len(payload),
                         "url": quote(relative, safe="/") if workspace else f"/attachments/ai/{safe_user}/{attach_id}/{quote(name)}"})
    return manifest


def attachment_embed_hint(item: dict) -> str:
    mime_type = str(item.get("type", ""))
    if mime_type.startswith("image/"):
        return "<img src=…>"
    if mime_type.startswith("video/"):
        return "<video controls src=…>"
    if mime_type.startswith("audio/"):
        return "<audio controls src=…>"
    return '<a href=… download>下载链接</a>'


def build_attachment_prompt(manifest: list[dict]) -> str:
    """附件清单进提示词：所有附件给出 URL 与嵌入建议；看图能力未开启时明确告知模型图片用 URL 嵌入。"""
    if not manifest:
        return ""
    lines = ["用户上传的附件（已保存到服务器，下列 URL 公开可访问、可直接嵌入网页；图片也请用 URL 写 <img src>，不要转成 base64）："]
    for item in manifest:
        lines.append(f"- {item['name']}（{item['type']}，建议 {attachment_embed_hint(item)}）：{item['url']}")
    if not ai_vision_enabled() and any(item["kind"] == "image" for item in manifest):
        lines.append("当前模型不看图：图片内容请依据文件名与用户描述判断，用上面的 URL 嵌入网页即可。")
    return "\n\n" + "\n".join(lines)


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
    # 库里可能存着 NULL 或旧格式哈希；此处只能返回 False，不能抛异常（否则 500 泄露内部细节）
    if not isinstance(expected_hex, str) or not expected_hex:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), expected_hex.encode("ascii"))
    except (ValueError, TypeError):
        return False


def user_preview_id(user_id: str) -> str:
    """读取或创建用户的随机预览 id，供工作区实时预览地址使用。"""
    with neon_db(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT preview_id FROM hatchery_user_extras WHERE user_id = %s", (str(user_id),))
            row = cur.fetchone()
            preview_id = str(row[0] or "") if row else ""
            if PREVIEW_ID_PATTERN.fullmatch(preview_id):
                return preview_id
            preview_id = new_preview_id(cur)
            cur.execute(
                "INSERT INTO hatchery_user_extras(user_id,preview_id,created_at) VALUES(%s,%s,%s)"
                " ON CONFLICT (user_id) DO UPDATE SET preview_id=EXCLUDED.preview_id",
                (str(user_id), preview_id, iso_time()),
            )
            return preview_id


def new_preview_id(cur) -> str:
    while True:
        preview_id = secrets.token_urlsafe(24)
        cur.execute("SELECT 1 FROM hatchery_user_extras WHERE preview_id = %s", (preview_id,))
        if not cur.fetchone():
            return preview_id


_SITE_META_CACHE: dict[str, tuple[tuple[int, int], str, str]] = {}
_SITE_META_LOCK = threading.Lock()


def published_site_meta(slug: str) -> tuple[str, str]:
    """读取已发布站点的 site.json，返回 (站点名, 简介)；文件缺失或损坏时返回空串。
    兼容尚未迁移的旧版大 JSON；文件模式的 site.json 只有基本信息，
    这里按 (mtime, size) 缓存，文件一变立刻失效。"""
    site_file = PUBLISHED / slug / "site.json"
    try:
        stat = site_file.stat()
    except OSError:
        return "", ""
    key = (stat.st_mtime_ns, stat.st_size)
    with _SITE_META_LOCK:
        cached = _SITE_META_CACHE.get(slug)
    if cached and cached[0] == key:
        return cached[1], cached[2]
    try:
        data = json.loads(site_file.read_text(encoding="utf-8"))
        site_name, description = str(data.get("siteName") or "").strip(), str(data.get("description") or "").strip()
    except Exception:
        site_name, description = "", ""
    with _SITE_META_LOCK:
        if len(_SITE_META_CACHE) > 2000:
            _SITE_META_CACHE.clear()
        _SITE_META_CACHE[slug] = (key, site_name, description)
    return site_name, description


_SITE_HTTPS_CACHE: dict[str, tuple[float, bool]] = {}
_SITE_HTTPS_LOCK = threading.Lock()


def site_subdomain_url(slug: str) -> str:
    if SITE_SUBDOMAINS_ENABLED and SITE_SLUG_PATTERN.fullmatch(slug) and slug not in RESERVED_SITE_SLUGS:
        return f"https://{slug}.{SITE_BASE_DOMAIN}/"
    return ""


def site_https_ready(slug: str) -> bool:
    """Verify the certificate actually served by the TLS proxy, including its hostname.

    A certificate file on disk or a running signing service does not prove that
    nginx has loaded a valid certificate for this newly published hostname.
    Failed probes expire quickly so a completed issuance becomes visible promptly.
    """
    url = site_subdomain_url(slug)
    if not url:
        return False
    hostname = urlparse(url).hostname
    now = time.monotonic()
    with _SITE_HTTPS_LOCK:
        cached = _SITE_HTTPS_CACHE.get(hostname)
        if cached and now < cached[0]:
            return cached[1]
    ready = False
    try:
        context = ssl.create_default_context()
        with socket.create_connection((SITE_TLS_PROBE_HOST, SITE_TLS_PROBE_PORT), timeout=1.5) as connection:
            with context.wrap_socket(connection, server_hostname=hostname):
                ready = True
    except (OSError, ValueError):
        pass
    with _SITE_HTTPS_LOCK:
        if len(_SITE_HTTPS_CACHE) > 2000:
            _SITE_HTTPS_CACHE.clear()
        _SITE_HTTPS_CACHE[hostname] = (time.monotonic() + (30.0 if ready else 2.0), ready)
    return ready


def public_site_url(slug: str) -> str:
    """Only hand out a subdomain after its served TLS certificate is valid."""
    if site_https_ready(slug):
        return site_subdomain_url(slug)
    return f"/pages/{quote(slug, safe='')}/"


def request_cert_sync() -> None:
    """通知 root 的 hatchery-cert-sync 把新发布站点的子域名加进 TLS 证书。
    只写一个请求文件，真正的签发由 systemd path 单元在后台触发，发布流程不会变慢。
    失败也不影响文件发布：未就绪的域名不会作为访问链接返回，后台定时重试。"""
    try:
        CERT_SYNC_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        CERT_SYNC_TRIGGER.touch()
    except OSError as exc:
        print(f"[cert-sync] 无法写入证书同步请求：{exc}", file=sys.stderr)


def published_site_index() -> list[dict]:
    """全部已发布站点：数据库记录 ∪ 磁盘遗留目录，按更新时间倒序。
    Gallery 与管理员总览共用这一份索引，保证两边看到的站点一致。
    onDisk=False 表示数据库有行但磁盘没有 site.json（已删除 / 改名残留），这类站点不能进 Gallery。"""
    with neon_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT p.slug, p.created_at, p.updated_at, p.listed_in_gallery,
                       u.name AS owner_name, u."campusId" AS owner_campus_id
                FROM hatchery_published_sites p
                LEFT JOIN "User" u ON u.id = p.user_id
                ORDER BY p.updated_at DESC
                LIMIT 500
                """
            )
            rows = cur.fetchall()
    on_disk = published_site_slugs_on_disk()
    sites: list[dict] = []
    seen_slugs: set[str] = set()
    for row in rows:
        slug = str(row["slug"])
        seen_slugs.add(slug)
        site_name, description = published_site_meta(slug)
        sites.append({
            "slug": slug,
            "siteName": site_name,
            "description": description,
            "owner": row["owner_name"] or "",
            "ownerCampusId": row["owner_campus_id"] or "",
            "listed": bool(row["listed_in_gallery"]),
            "onDisk": slug in on_disk,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "publicUrl": public_site_url(slug),
        })
    # 服务以文件系统为准：磁盘上有但数据库缺行的历史站点也并入（默认收录）
    for slug, mtime in on_disk.items():
        if slug in seen_slugs:
            continue
        site_name, description = published_site_meta(slug)
        sites.append({
            "slug": slug,
            "siteName": site_name,
            "description": description,
            "owner": "",
            "ownerCampusId": "",
            "listed": True,
            "onDisk": True,
            "createdAt": mtime,
            "updatedAt": mtime,
            "publicUrl": public_site_url(slug),
        })
    sites.sort(key=lambda item: str(item["updatedAt"]), reverse=True)
    return sites


def published_site_slugs_on_disk() -> dict[str, str]:
    """磁盘上实际可访问的已发布站点：slug -> site.json 修改时间（ISO）。
    服务以文件系统为准，历史遗留站点可能没有数据库行，列表/Gallery 要并上它们。"""
    found: dict[str, str] = {}
    try:
        for entry in PUBLISHED.iterdir():
            if not entry.is_dir() or entry.name.startswith(".") or entry.is_symlink():
                continue
            site_file = entry / "site.json"
            if site_file.is_file():
                found[entry.name] = iso_time(datetime.fromtimestamp(site_file.stat().st_mtime, tz=timezone.utc))
    except Exception:
        pass
    return found


def initialize_database() -> None:
    if not NEON_DATABASE_URL:
        sys.exit("缺少 Neon 连接串：请设置 NEON_DATABASE_URL 环境变量，或在 ~/Project/sdszwebsite/.env 写入 DATABASE_URL=...")
    # "User" 属于 sdszwebsite，禁止 CREATE/ALTER。
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
    CREATE TABLE IF NOT EXISTS hatchery_published_sites(
        slug TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
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
    CREATE TABLE IF NOT EXISTS hatchery_ai_chat_sessions(
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '新聊天',
        messages_html TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS hatchery_ai_chat_sessions_user_idx ON hatchery_ai_chat_sessions(user_id, updated_at DESC);
    -- HatcheryStudy 学习对话：账号唯一会话，消息独立成行便于增量拉取
    CREATE TABLE IF NOT EXISTS hatchery_study_messages(
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        markdown TEXT NOT NULL DEFAULT '',
        raw_text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS hatchery_study_messages_user_idx ON hatchery_study_messages(user_id, id);
    -- 多会话：每条消息归属一个会话；会话本身由首条消息的 session_id 隐式定义
    ALTER TABLE hatchery_study_messages ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE hatchery_study_messages ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
    ALTER TABLE hatchery_study_messages ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS hatchery_study_messages_session_idx
        ON hatchery_study_messages(user_id, session_id, id);
    ALTER TABLE hatchery_published_sites ADD COLUMN IF NOT EXISTS listed_in_gallery BOOLEAN NOT NULL DEFAULT TRUE;
    -- 失败原因落库：任务失败时排障不必依赖转瞬即逝的内存与容器日志
    ALTER TABLE hatchery_ai_usage ADD COLUMN IF NOT EXISTS error TEXT;
    ALTER TABLE hatchery_ai_usage ALTER COLUMN input_tokens DROP NOT NULL;
    ALTER TABLE hatchery_ai_usage ALTER COLUMN output_tokens DROP NOT NULL;
    ALTER TABLE hatchery_ai_usage ALTER COLUMN total_tokens DROP NOT NULL;
    """
    with neon_db() as conn:
        with conn.cursor() as cur:
            for statement in ddl.split(";"):
                if statement.strip():
                    cur.execute(statement)
            cur.execute(login_links.SCHEMA)
    AI_JOB_STORE.initialize()


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


def record_ai_usage(user_id: str, job_id: str | None, prompt: str, attachments: list[dict], provider: str, model: str, usage: dict | None, status: str, error: str = "") -> None:
    """归档一次 AI 输入及其 token 用量；统计写库失败不影响主流程。"""
    try:
        input_tokens = max(0, int(usage["inputTokens"])) if usage and usage.get("inputTokens") is not None else None
        output_tokens = max(0, int(usage["outputTokens"])) if usage and usage.get("outputTokens") is not None else None
        attachment_meta = [
            {"name": str(item.get("name", "附件"))[:180], "kind": str(item.get("kind", "text")), "size": len(str(item.get("content", "")))}
            for item in (attachments or [])
            if isinstance(item, dict)
        ]
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hatchery_ai_usage(user_id,job_id,prompt,attachments_json,provider,model,input_tokens,output_tokens,total_tokens,status,error,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                        input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
                        status,
                        (error or "")[:500] or None,
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


def ai_config_snapshot() -> dict:
    """管理面板展示的 AI 配置状态；只回传 Key 末 4 位，绝不回传完整内容。"""
    provider_name, provider = resolve_llm_provider()
    kimi_key = os.environ.get("KIMI_API_KEY", "").strip()
    glm_key = os.environ.get("GLM_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return {
        "provider": provider_name,
        "providerLabel": provider["label"],
        "model": os.environ.get(provider["model_env"], "").strip() or provider["default_model"],
        "explicitProvider": os.environ.get("MIAODA_LLM", "").strip().lower(),
        "kimiKeySet": bool(kimi_key),
        "kimiKeyMask": f"···{kimi_key[-4:]}" if kimi_key else "",
        "glmKeySet": bool(glm_key),
        "glmKeyMask": f"···{glm_key[-4:]}" if glm_key else "",
        "deepseekKeySet": bool(deepseek_key),
        "deepseekKeyMask": f"···{deepseek_key[-4:]}" if deepseek_key else "",
    }


def persist_env_value(key: str, value: str) -> None:
    """原子更新项目 .env 中的某个键（保留其他行与注释）；值须已确认为无空白的安全字符。"""
    env_file = ROOT / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if not line.lstrip().startswith("#") and pattern.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    tmp_file = env_file.parent / (env_file.name + ".tmp")
    write_text_exact(tmp_file, "\n".join(out) + "\n")
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, env_file)


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


def delete_site_records(cur, site_username: str) -> None:
    """删除某个发布路径下的全部动态数据与占用记录（更改发布路径时清理旧站点）。"""
    for table in (
        "hatchery_site_users",
        "hatchery_site_sessions",
        "hatchery_site_settings",
        "hatchery_site_invitations",
        "hatchery_site_audit_log",
        "hatchery_site_deployments",
    ):
        cur.execute(f"DELETE FROM {table} WHERE site_username = %s", (site_username,))
    cur.execute("DELETE FROM hatchery_published_sites WHERE slug = %s", (site_username,))


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
# 发布站点的域名体系：SITE_BASE_DOMAIN 是新发布链接使用的主域名；
# SITE_HOST_DOMAINS 是接受“子域名即站点”路由的全部域名（旧域名保留其中，已发布链接不会断）。
SITE_BASE_DOMAIN = os.environ.get("ALCHEMY_HATCHERY_SITE_BASE_DOMAIN", "hatchery.mizusumi.com").strip().lower() or "hatchery.mizusumi.com"
SITE_SUBDOMAINS_ENABLED = os.environ.get('ALCHEMY_HATCHERY_SITE_SUBDOMAINS_ENABLED', '0').strip().lower() in ('1', 'true', 'yes')
SITE_TLS_PROBE_HOST = os.environ.get('ALCHEMY_HATCHERY_TLS_PROBE_HOST', '127.0.0.1').strip() or '127.0.0.1'
SITE_TLS_PROBE_PORT = int(os.environ.get('ALCHEMY_HATCHERY_TLS_PROBE_PORT', '443'))
SITE_HOST_DOMAINS = tuple(dict.fromkeys(
    item.strip().lower()
    for item in os.environ.get(
        "ALCHEMY_HATCHERY_SITE_HOST_DOMAINS", f"{SITE_BASE_DOMAIN},hatchery.mizusumi.com"
    ).split(",")
    if item.strip()
))
SITE_HOST_PATTERNS = tuple(
    re.compile(rf"^(?=.{{3,32}}\.{re.escape(domain)}$)([a-z0-9]+(?:-[a-z0-9]+)*)\.{re.escape(domain)}$")
    for domain in SITE_HOST_DOMAINS
)
# 允许被当作自身地址反射（SSO 回调 URL）的 Host，防止 Host 头注入把授权码送到攻击者域名
ALLOWED_HOSTS = tuple(
    item.strip().lower()
    for item in os.environ.get(
        "ALCHEMY_HATCHERY_ALLOWED_HOSTS", f"{SITE_BASE_DOMAIN},hatchery.mizusumi.com,localhost,127.0.0.1"
    ).split(",")
    if item.strip()
)


def host_is_allowed(host: str) -> bool:
    """Host 头（含泛域名子站）是否在允许列表内。"""
    name = host.split(":", 1)[0].strip().lower()
    return any(name == allowed or name.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS)


try:
    PORT = int(os.environ.get("ALCHEMY_HATCHERY_PORT", "4173"))
except ValueError:
    PORT = 4173
class AIchemyHatcheryHandler(SimpleHTTPRequestHandler):
    server_version = "AIchemyHatchery"
    sys_version = ""  # 不在 Server 响应头里暴露 Python 版本（指纹信息泄露）

    def log_message(self, fmt: str, *args) -> None:
        # 容器里 stdout 是块缓冲，不 flush 的话 docker logs 永远看不到请求日志
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def request_is_https(self) -> bool:
        return SECURE_COOKIES or self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"

    def cookie_security(self) -> str:
        """反向代理已是 HTTPS 时也补上 Secure，避免会话 Cookie 走明文（Cookie 属性缺陷）。"""
        return "; Secure" if self.request_is_https() else ""

    def request_host(self) -> str:
        return self.headers.get("Host", "").split(",")[0].strip().lower()

    def same_origin_request(self) -> bool:
        """CSRF 防护：Cookie 虽是 SameSite=Strict，但用户发布的站点位于站点子域名下，
        能借浏览器带上控制台 Cookie 发起写请求。"""
        host = self.request_host()
        origin = self.headers.get("Origin", "").strip()
        if origin:
            return origin.lower() != "null" and urlparse(origin).netloc.lower() == host
        referer = self.headers.get("Referer", "").strip()
        if referer:
            return urlparse(referer).netloc.lower() == host
        return True  # 非浏览器客户端不会带 Origin/Referer

    def end_headers(self) -> None:
        static_path = getattr(self, '_console_static_path', None) or urlparse(self.path).path
        self._console_static_path = None
        # serve_site_file 已经为发布站资源写好缓存策略，这里不要再盖一层 no-store。
        site_file_cacheable = getattr(self, "site_file_cacheable", False)
        self.site_file_cacheable = False
        if not site_file_cacheable and (static_path == "/" or static_path in PUBLIC_STATIC_PATHS):
            version = (parse_qs(urlparse(self.path).query).get('v') or [''])[0]
            if static_path in VERSIONED_STATIC_PATHS and version == console_asset_version(static_path):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if self.request_is_https():
            # 缺少 HSTS：HTTPS 部署下仍可被降级到明文
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        super().end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if (site_slug_from_host(self.headers.get("Host", "")) and not parsed.path.startswith("/api/")) or re.match(r"^/(pages|preview|ai-preview)/", parsed.path):
            self.do_GET()
            return
        if parsed.path in ('/', '/index.html') or re.fullmatch(r'/c/[0-9a-fA-F-]{32,36}/?', parsed.path):
            self.serve_console_page(head_only=True)
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self._console_static_path = parsed.path
            self.path = f"/{FRONTEND_DIR}{parsed.path}" + ('?' + parsed.query if parsed.query else '')
            super().do_HEAD()
            return
        if parsed.path in ("/favicon.svg", "/favicon.ico"):
            self.send_favicon(head_only=True)
            return
        self.send_error(404, "Not found")

    def serve_console_page(self, head_only=False):
        source = (ROOT / FRONTEND_DIR / 'index.html').read_text()
        def versioned(match):
            prefix, path, suffix = match.groups()
            return prefix + path + '?v=' + console_asset_version(path) + suffix if path in VERSIONED_STATIC_PATHS else match.group(0)
        source = re.sub(r'((?:src|href)=["\'])(/[^"\'?]+)(?:\?[^"\']*)?(["\'])', versioned, source)
        body = source.encode()
        self._console_static_path = '/index.html'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_favicon(self, head_only: bool = False) -> None:
        body = FAVICON_SVG.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_ai_attachment(self, path: str) -> None:
        """公开访问用户上传的 AI 附件（嵌入在预览/发布页里，匿名可读）。"""
        match = AI_ATTACHMENT_URL_PATTERN.match(path)
        if not match:
            self.send_error(404, "Not found")
            return
        user_token, attach_id, raw_name = match.group(1), match.group(2), unquote(match.group(3))
        name = Path(raw_name.replace("\\", "/")).name  # 双重保险：去掉任何路径成分
        root = AI_ATTACHMENT_ROOT.resolve()
        attach_dir = (root / user_token / attach_id).resolve()
        target = (attach_dir / name).resolve()
        if root not in attach_dir.parents or root not in target.parents or not target.is_file():
            self.send_error(404, "Not found")
            return
        body = target.read_bytes()
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if mime_type.startswith("text/") or mime_type in ("application/javascript", "image/svg+xml"):
            mime_type += "; charset=utf-8"  # 文本类附件按 UTF-8 展示，避免乱码
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800")  # 附件内容带随机 id，可放心长缓存
        self.end_headers()
        self.wfile.write(body)

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

    def read_json(self, max_bytes: int = 25_000_000) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        # 未登录接口用更小的上限，避免单请求吃掉内存（拒绝服务）
        if length <= 0 or length > max_bytes:
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
        cached = cached_console_session(token_hash)
        if cached is not None:
            return cached
        now = iso_time()
        with neon_db(autocommit=True) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                sweep_expired_console_sessions(cur, now)
                cur.execute(CONSOLE_USER_QUERY, {"token_hash": token_hash, "now": now})
                row = cur.fetchone()
                if row and row_value(row, "bannedUntil") is not None and not is_site_owner_campus_id(row.get("campusId")):
                    cur.execute("DELETE FROM console_sessions WHERE token_hash = %s", (token_hash,))
                    row = None
        if not row:
            return None
        user = dict(row)
        user["expires_at"] = user.pop("session_expires_at", None)
        user["token_hash"] = user.pop("session_token_hash", None)
        user["username"] = user.get("name")
        if is_site_owner_campus_id(user.get("campusId")):
            user["isAdmin"] = True
        user["role"] = "admin" if user.get("isAdmin") else "user"
        user["status"] = "active"
        store_console_session(token_hash, user)
        return user

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
                f"HttpOnly; SameSite=Strict{self.cookie_security()}"
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

    def attach_preview_id(self, payload: dict, user_id: str, user_row: dict | None = None) -> dict:
        # user_row（来自 console_user 的单条查询）已经带上了这两张表的数据，
        # 有它就不必再开一条 Neon 连接。
        if user_row is not None and "extra_preview_id" in user_row:
            preview_id = user_row.get("extra_preview_id")
            slug = user_row.get("published_slug")
            listed = user_row.get("published_listed")
        else:
            with neon_db(autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(PREVIEW_EXTRAS_QUERY, (str(user_id), str(user_id), str(user_id)))
                    row = cur.fetchone()
            preview_id = row[0] if row else None
            slug = row[1] if row else None
            listed = row[2] if row else None
        payload["previewId"] = preview_id
        payload["publishSlug"] = slug
        payload["publishListed"] = bool(listed) if listed is not None else None
        payload["publishDomain"] = SITE_BASE_DOMAIN
        payload['publishUrl'] = public_site_url(slug) if slug else ''
        payload['publishSubdomainsEnabled'] = SITE_SUBDOMAINS_ENABLED
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
        data = self.read_json(64_000)  # 未登录接口限制请求体大小，避免内存型拒绝服务
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

    def handle_send_otp(self) -> None:
        if self.login_is_limited():
            self.send_json({"error": "操作过于频繁，请 5 分钟后再试"}, 429)
            return
        data = self.read_json(64_000)
        email = str(data.get("email", "")).strip().lower()
        if not email or "@" not in email or len(email) > 254:
            raise ValueError("请输入有效邮箱地址")
        # 发信接口原先完全不限流：可被用来轰炸任意邮箱，也能无限刷新验证码
        address = self.client_address[0] if self.client_address else "unknown"
        if rate_limit_hit("otp-send-ip", address, 5, 600) or rate_limit_hit("otp-send-mail", email, 3, 600):
            self.send_json({"error": "验证码发送过于频繁，请 10 分钟后再试"}, 429)
            return

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
            # 不回显 SMTP 异常（会暴露邮件服务器地址、账号等内部信息）
            print(f"[otp] 发送验证码失败：{exc}", file=sys.stderr)
            self.send_json({"error": "发送邮件失败，请稍后再试"}, 500)
            return

        self.send_json({"ok": True, "message": "验证码已发送，10 分钟内有效"})

    def handle_auth_logout(self) -> None:
        token = self.console_token()
        if token:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM console_sessions WHERE token_hash = %s", (token_hash,))
            invalidate_console_sessions(token_hash)
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": [
                f"alchemy_hatchery_console_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{self.cookie_security()}",
            ]},
        )

    # ── SSO (via sdsz) ──────────────────────────────────────────────

    def safe_return_path(self, value: str) -> str:
        """只接受站内绝对路径；"//evil.com" 与 "/\\evil.com" 会被浏览器当作外站（开放重定向）。"""
        candidate = (value or "").strip()
        if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
            return "/"
        if "\r" in candidate or "\n" in candidate:  # 顺带挡住响应头注入
            return "/"
        return candidate

    def handle_sso_authorize(self) -> None:
        """Redirect user to sdsz login page. After login, sdsz redirects back with a code."""
        if not SSO_SECRET:
            self.send_json({"error": "SSO 未配置"}, 503)
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        # Sanitize: only allow relative paths
        return_to = self.safe_return_path(params.get("returnTo", ["/"])[0])
        base_url = self._base_url()
        if base_url is None:
            # Host 头不在允许列表内：拒绝，否则回调地址会被指向攻击者域名（Host 头注入）
            self.send_json({"error": "请求的站点地址不被允许"}, 400)
            return
        state = secrets.token_urlsafe(16)
        # Store state in a short-lived cookie for CSRF protection
        callback_url = f"{base_url}/api/auth/sso/callback"
        sdsz_login = (
            f"{SDSZ_BASE_URL}/login"
            f"?intent=sso"
            f"&redirect={urlparse_mod.quote(callback_url)}"
            f"&state={state}"
        )
        self.send_response(302)
        self.send_header("Location", sdsz_login)
        self.send_header("Set-Cookie",
            f"sso_state={state}; Path=/api/auth/sso; Max-Age=300; HttpOnly; SameSite=Lax{self.cookie_security()}")
        # Also store returnTo
        self.send_header("Set-Cookie",
            f"sso_return_to={urlparse_mod.quote(return_to)}; Path=/api/auth/sso; Max-Age=300; HttpOnly; SameSite=Lax{self.cookie_security()}")
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
            return_to = self.safe_return_path(urlparse_mod.unquote(return_to_cookie.value))

        # Clear SSO cookies, set session cookie, redirect
        self.send_response(302)
        self.send_header("Location", return_to)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Set-Cookie",
            f"sso_state=; Path=/api/auth/sso; Max-Age=0; HttpOnly; SameSite=Lax{self.cookie_security()}")
        self.send_header("Set-Cookie",
            f"sso_return_to=; Path=/api/auth/sso; Max-Age=0; HttpOnly; SameSite=Lax{self.cookie_security()}")
        self.end_headers()

    def _base_url(self) -> str | None:
        """Best-effort base URL from request headers；Host/X-Forwarded-Host 必须在允许列表内。"""
        proto = "https" if self.request_is_https() else "http"
        for candidate in (self.headers.get("X-Forwarded-Host", ""), self.headers.get("Host", "")):
            host = candidate.split(",")[0].strip()
            if host and host_is_allowed(host):
                return f"{proto}://{host}"
        return None

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
        invalidate_console_sessions()
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
        invalidate_console_sessions()
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
        if status == "disabled":
            invalidate_console_sessions()
        self.send_json({"ok": True, "username": target["name"], "status": status})

    def handle_admin_impersonate(self) -> None:
        admin = self.require_console_user(admin=True)
        if not admin:
            return
        data = self.read_json()
        username = str(data.get("username", "")).strip()
        if not username:
            raise ValueError("未指定用户名")
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM "User" WHERE LOWER(name) = LOWER(%s)', (username,))
                target = cur.fetchone()
                if not target:
                    raise ValueError("用户不存在")
                if row_value(target, "bannedUntil"):
                    raise ValueError("不能登录已停用的账号")
                if str(target["id"]) == str(admin["id"]):
                    raise ValueError("不能登录自己的账号")
            result = login_links.issue(
                conn,
                str(target["id"]),
                f"管理员 {admin['username']} 从后台登录用户 {target['name']}",
                ttl_seconds=300
            )
            with conn.cursor() as cur:
                audit_event(cur, str(admin["id"]), "admin.impersonate", {"target_username": target["name"], "target_user_id": str(target["id"])})
        self.send_json({"ok": True, "loginUrl": result["url"], "expiresAt": result["expires_at"].isoformat()})

    def require_study_access(self) -> dict | None:
        user = self.require_console_user()
        if not user:
            return None
        if not is_site_owner_campus_id(user.get("campusId")):
            self.send_json({"error": "HatcheryStudy 当前仅对站长账号开放"}, 403)
            return None
        return user

    def handle_study_outlines(self) -> None:
        user = self.require_study_access()
        if not user:
            return
        sp = ensure_study_panel_dirs(user["id"])
        outlines_dir = sp / "outlines"
        # POST — upload outline file
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type == "application/json":
            data = self.read_json()
            name = str(data.get("name", "")).strip()
            content = str(data.get("content", ""))
            if not name or not STUDY_OUTLINE_NAME_PATTERN.fullmatch(name):
                raise ValueError("大纲文件名格式无效（支持 txt/md，120 字以内）")
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > STUDY_OUTLINE_MAX_SIZE:
                raise ValueError("大纲文件超过 500 KB 限制")
            (outlines_dir / name).write_bytes(content_bytes)
            self.send_json({"ok": True, "name": name})
        else:
            raise ValueError("请发送 application/json 格式的请求")

    def handle_study_progress(self) -> None:
        user = self.require_study_access()
        if not user:
            return
        sp = ensure_study_panel_dirs(user["id"])
        progress_file = sp / "progress.md"
        if self.command == "GET":
            self.send_json({"content": progress_file.read_text(encoding="utf-8")})
            return
        data = self.read_json()
        content = str(data.get("content", ""))
        if len(content.encode("utf-8")) > 200_000:
            raise ValueError("进度内容超过 200 KB 限制")
        progress_file.write_text(content, encoding="utf-8")
        self.send_json({"ok": True})

    def study_history(self, user_id: str, session_id: str = "", limit: int = 100) -> list[dict]:
        """读取某会话的对话（时间正序）。session_id 为空时读最早的那个会话。"""
        sid = session_id.strip() or self.study_latest_session(user_id) or "default"
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT role, title, markdown, raw_text, session_id, provider, model, created_at FROM ("
                    "  SELECT id, role, title, markdown, raw_text, session_id, provider, model, created_at"
                    "  FROM hatchery_study_messages"
                    "  WHERE user_id = %s AND session_id = %s ORDER BY id DESC LIMIT %s"
                    ") t ORDER BY id ASC",
                    (str(user_id), sid, limit),
                )
                return [dict(row) for row in cur.fetchall()]

    def study_sessions(self, user_id: str) -> list[dict]:
        """列出该账号的全部学习会话（按最后消息时间倒序）。"""
        with neon_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT session_id,"
                    "       count(*) FILTER (WHERE role='user') AS turns,"
                    "       max(created_at) AS updated_at,"
                    "       (array_agg(title ORDER BY id DESC) FILTER (WHERE role='assistant' AND title <> ''))[1] AS last_title,"
                    "       (array_agg(raw_text ORDER BY id ASC) FILTER (WHERE role='user'))[1] AS first_prompt"
                    " FROM hatchery_study_messages WHERE user_id = %s"
                    " GROUP BY session_id ORDER BY max(id) DESC",
                    (str(user_id),),
                )
                return [dict(row) for row in cur.fetchall()]

    def study_latest_session(self, user_id: str) -> str:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id FROM hatchery_study_messages WHERE user_id = %s"
                    " ORDER BY id DESC LIMIT 1",
                    (str(user_id),),
                )
                row = cur.fetchone()
                return str(row[0]) if row else ""

    def study_progress_file(self, user_id, session_id: str):
        """进度文件按会话隔离：默认会话沿用旧的 studypanel/progress.md。"""
        sp = ensure_study_panel_dirs(user_id)
        sid = (session_id or "").strip()
        if not sid or sid == "default":
            return sp / "progress.md"
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:40] or "session"
        return sp / f"progress-{safe}.md"

    def handle_study_history(self) -> None:
        user = self.require_study_access()
        if not user:
            return
        parsed = urlparse(self.path)
        session_id = parse_qs(parsed.query).get("session", [""])[0]
        progress_file = self.study_progress_file(user["id"], session_id)
        try:
            has_progress = bool(progress_file.read_text(encoding="utf-8").strip())
        except OSError:
            has_progress = False
        resolved = session_id.strip() or self.study_latest_session(user["id"]) or "default"
        self.send_json({
            "messages": self.study_history(user["id"], session_id),
            "hasProgress": has_progress,
            "sessionId": resolved,
            "sessions": self.study_sessions(user["id"]),
        })

    def handle_study_session_delete(self) -> None:
        user = self.require_study_access()
        if not user:
            return
        data = self.read_json()
        session_id = str(data.get("sessionId", "")).strip()
        if not session_id:
            raise ValueError("未指定会话")
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM hatchery_study_messages WHERE user_id = %s AND session_id = %s",
                    (str(user["id"]), session_id),
                )
                removed = cur.rowcount
        try:
            progress_file = self.study_progress_file(user["id"], session_id)
            if progress_file.name != "progress.md" and progress_file.exists():
                progress_file.unlink()
        except OSError:
            pass
        self.send_json({"ok": True, "removed": removed})

    # ===== 自定义 provider 配置（简陋版，仅站长可用）=====
    def handle_study_providers(self) -> None:
        user = self.require_study_access()
        if not user:
            return
        if self.command == "GET":
            items = study_engine.load_providers()
            base, key, model = study_engine.gateway_config()
            self.send_json({
                "providers": [
                    {"id": "default", "label": "默认（Hatchery 网关）",
                     "base_url": base, "model": model, "hasKey": bool(key), "builtin": True}
                ] + [
                    {"id": p["id"], "label": p["label"], "base_url": p["base_url"],
                     "model": p["model"], "hasKey": bool(p["api_key"]), "builtin": False}
                    for p in items
                ]
            })
            return
        data = self.read_json()
        action = str(data.get("action", "")).strip()
        items = study_engine.load_providers()
        if action == "save":
            pid = str(data.get("id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid) or pid == "default":
                raise ValueError("provider id 需为 1-40 位字母数字（不能用 default）")
            label = str(data.get("label", "")).strip()[:80] or pid
            base_url = str(data.get("baseUrl", "")).strip()
            if not re.fullmatch(r"https?://\S+", base_url):
                raise ValueError("Base URL 需以 http:// 或 https:// 开头")
            model = str(data.get("model", "")).strip()[:120]
            api_key = str(data.get("apiKey", "")).strip()
            existing = next((p for p in items if p["id"] == pid), None)
            if existing is None:
                items.append({"id": pid, "label": label, "base_url": base_url,
                              "api_key": api_key, "model": model})
            else:
                existing.update({"label": label, "base_url": base_url, "model": model})
                # 留空表示不改动已存的 Key
                if api_key:
                    existing["api_key"] = api_key
            study_engine.save_providers(items)
            self.send_json({"ok": True})
            return
        if action == "delete":
            pid = str(data.get("id", "")).strip()
            study_engine.save_providers([p for p in items if p["id"] != pid])
            self.send_json({"ok": True})
            return
        if action == "models":
            # 拉取某个 endpoint 的模型列表，方便挑模型名
            pid = str(data.get("id", "")).strip()
            if pid and pid != "default":
                item = study_engine.find_provider(pid)
                if not item:
                    raise ValueError("provider 不存在")
                base_url, api_key = item["base_url"], item["api_key"]
            else:
                base_url, api_key, _ = study_engine.gateway_config()
            if not api_key:
                raise ValueError("该平台尚未配置 API Key")
            self.send_json({"models": study_engine.fetch_models(base_url, api_key)})
            return
        raise ValueError("未知操作")

    def handle_study_chat(self) -> None:
        """直连网关的流式学习对话；进度由模型通过 write_progress 工具自行维护。"""
        user = self.require_study_access()
        if not user:
            return
        data = self.read_json()
        prompt = str(data.get("prompt", "")).strip()
        if not prompt or len(prompt) > 20000:
            raise ValueError("学习问题不能为空，且不超过 20000 字")
        session_id = str(data.get("sessionId", "")).strip()[:64] or "default"
        provider_id = str(data.get("provider", "")).strip()[:40]
        model_override = str(data.get("model", "")).strip()[:120]
        history_limit = 30
        history = []
        for item in (data.get("history") or [])[-history_limit:]:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                continue
            text = str(item.get("text", "")).strip()[:4000]
            if text:
                history.append({"role": item["role"], "text": text})

        progress_file = self.study_progress_file(user["id"], session_id)
        try:
            progress = progress_file.read_text(encoding="utf-8")
        except OSError:
            progress = ""

        messages = study_engine.build_messages(progress, history, prompt)
        started_at = iso_time()

        # 先把用户消息落库，保证刷新后不丢
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_study_messages(user_id,role,raw_text,session_id,provider,model,created_at)"
                    " VALUES(%s,'user',%s,%s,%s,%s,%s)",
                    (str(user["id"]), prompt, session_id, provider_id, model_override, started_at),
                )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        # 流式响应一旦开始就不能再走 send_json 错误分支（会重复发响应头）
        self._streaming_response_started = True

        def emit(event: dict) -> None:
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        content_parts: list[str] = []
        try:
            # 第一步：只取课程正文。下发工具定义但 tool_choice=none——不给定义时
            # 模型会自己编造 <|DSML|> 伪调用文本混进正文，给定义但不许调用最稳。
            for chunk in study_engine.stream_completion(
                messages, tools=[study_engine.PROGRESS_TOOL], tool_choice="none",
                provider_id=provider_id, model_override=model_override,
            ):
                if "content" in chunk:
                    content_parts.append(chunk["content"])
                    emit({"type": "delta", "text": chunk["content"]})
        except study_engine.StudyGatewayError as error:
            emit({"type": "error", "message": str(error)})
            self.finish()
            return

        raw_text = "".join(content_parts)
        lesson = study_engine.parse_lesson_answer(raw_text)
        title, markdown = lesson["title"], lesson["markdown"]
        if not markdown and raw_text.strip():
            markdown = raw_text

        # 第二步：单独一轮让模型维护学习进度（用户不可见）
        progress_written = False
        try:
            summary_for_progress = (markdown or raw_text)[:4000]
            progress_messages = messages + [
                {"role": "assistant", "content": raw_text or "(无正文)"},
                {"role": "user", "content": study_engine.PROGRESS_UPDATE_INSTRUCTION.format(
                    question=prompt[:1500], answer=summary_for_progress)},
            ]
            tool_args: dict[int, dict] = {}
            for chunk in study_engine.stream_completion(
                progress_messages, tools=[study_engine.PROGRESS_TOOL], timeout=180,
                provider_id=provider_id, model_override=model_override,
            ):
                tool = chunk.get("tool")
                if not tool:
                    continue
                slot = tool_args.setdefault(tool["index"], {"name": "", "arguments": ""})
                if tool["name"]:
                    slot["name"] = tool["name"]
                slot["arguments"] += tool.get("arguments", "")
            for slot in tool_args.values():
                if slot["name"] != "write_progress":
                    continue
                try:
                    arguments = json.loads(slot["arguments"] or "{}")
                except json.JSONDecodeError:
                    continue
                body = arguments.get("markdown")
                if isinstance(body, str) and body.strip():
                    try:
                        progress_file.write_text(body, encoding="utf-8")
                        progress_written = True
                    except OSError:
                        pass
        except study_engine.StudyGatewayError:
            # 进度更新失败不影响已经拿到的课程内容
            pass

        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_study_messages(user_id,role,title,markdown,raw_text,session_id,provider,model,created_at)"
                    " VALUES(%s,'assistant',%s,%s,%s,%s,%s,%s,%s)",
                    (str(user["id"]), title, markdown, raw_text, session_id,
                     provider_id, model_override, iso_time()),
                )
        emit({"type": "done", "title": title, "markdown": markdown, "progressUpdated": progress_written})
        self.finish()

    def handle_study_outline_delete(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        name = str(data.get("name", "")).strip()
        if not name or not STUDY_OUTLINE_NAME_PATTERN.fullmatch(name):
            raise ValueError("大纲文件名无效")
        sp = ensure_study_panel_dirs(user["id"])
        target = sp / "outlines" / name
        if not target.exists() or not target.is_file():
            raise ValueError("大纲文件不存在")
        target.unlink()
        self.send_json({"ok": True})

    def handle_admin_ai_config_update(self) -> None:
        # 改 AI Key 影响全站 AI 服务，只有管理员可用；Key 本身永远不进日志与审计
        admin = self.require_console_user(admin=True)
        if not admin:
            return
        data = self.read_json()
        fields = {"deepseekApiKey": ("DEEPSEEK_API_KEY", "deepseekKeySet"),
                  "kimiApiKey": ("KIMI_API_KEY", "kimiKeySet"),
                  "glmApiKey": ("GLM_API_KEY", "glmKeySet")}
        supplied = [name for name in fields if name in data]
        if len(supplied) != 1:
            raise ValueError("请指定一个 AI 服务的 API Key")
        field = supplied[0]
        env_name, status_name = fields[field]
        key = str(data[field]).strip()
        if key and not AI_KEY_PATTERN.fullmatch(key):
            raise ValueError("Key 格式无效：应为 7 位以上的字母数字串（可含 -、_、.）")
        with AI_CONFIG_LOCK:
            persist_env_value(env_name, key)
            os.environ[env_name] = key
            with neon_db() as conn:
                with conn.cursor() as cur:
                    audit_event(cur, str(admin["id"]), "admin.ai_config", {status_name: bool(key)})
        self.send_json({"ok": True, **ai_config_snapshot()})

    def handle_save_draft(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        directory, _ = user_site_directory(user['id'])
        if site_files.read_metadata(directory).get('format') == 'files':
            self.send_json({'error': '网站已使用真实文件，旧页面草稿不能覆盖，请刷新页面'}, 409)
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
            if self.proxy_site_backend():
                return
            # CSRF：所有写操作都要求同源发起（原先仅依赖 SameSite=Strict，同站子域可绕过）
            if not self.same_origin_request():
                self.send_json({"error": "请求来源不被允许"}, 403)
                return
            if path == login_links.API_PATH:
                login_links.handle_redeem(self, neon_db)
            elif path == "/api/auth/login":
                self.handle_auth_login()
            elif path == "/api/auth/send-otp":
                self.handle_send_otp()
            elif path == "/api/auth/logout":
                self.handle_auth_logout()
            elif path == "/api/auth/change-password":
                self.handle_change_password()
            elif path == "/api/admin/users/status":
                self.handle_admin_user_status()
            elif path == "/api/admin/impersonate":
                self.handle_admin_impersonate()
            elif path == "/api/study/outlines":
                self.handle_study_outlines()
            elif path == "/api/study/outlines/delete":
                self.handle_study_outline_delete()
            elif path == "/api/study/progress":
                self.handle_study_progress()
            elif path == "/api/study/chat":
                self.handle_study_chat()
            elif path == "/api/study/session/delete":
                self.handle_study_session_delete()
            elif path == "/api/study/providers":
                self.handle_study_providers()
            elif path == "/api/admin/ai-config":
                self.handle_admin_ai_config_update()
            elif path == "/api/auth/sessions/revoke-others":
                self.handle_revoke_other_sessions()
            elif path == "/api/site/files":
                self.handle_site_files()
            elif path == "/api/ai/site-runtime":
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                data = self.read_json()
                args = data.get('args', {})
                if not isinstance(args, dict):
                    raise ValueError('运行参数必须是对象')
                self.send_json(site_runtime.authorized_call(token, data.get('operation'), args))
            elif path == "/api/console/draft":
                self.handle_save_draft()
            elif path == "/api/site-account/reset-owner":
                self.handle_site_owner_reset()
            elif path == "/api/ai/run":
                user = self.require_console_user()
                if user:
                    if (parse_qs(parsed.query).get("async") or [""])[0] == "1":
                        self.handle_ai_run_start(user)
                    else:
                        self.handle_ai_run(user)
            elif path == "/api/ai/run/ack":
                user = self.require_console_user()
                if user:
                    self.handle_ai_run_ack(user)
            elif path == "/api/ai/undo":
                # 越权：这个接口直接改写平台源码，只有管理员可用
                if self.require_console_user(admin=True):
                    self.handle_ai_undo()
            elif path == "/api/server/restart":
                # 越权：重启服务是全站影响，原先任何登录用户都能触发（拒绝服务）
                if self.require_console_user(admin=True):
                    self.handle_server_restart()
            elif path == "/api/preview":
                self.handle_preview()
            elif path == "/api/publish":
                self.handle_publish()
            elif path.startswith("/api/runtime/"):
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
            else:
                self.send_json({"error": "接口不存在"}, 404)
        except ValueError as exc:
            if getattr(self, "_streaming_response_started", False):
                print(f"[error] POST {self.path}（流式已开始）：{exc}", file=sys.stderr)
            else:
                self.send_json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            if getattr(self, "_streaming_response_started", False):
                print(f"[error] POST {self.path}（流式已开始）：{exc}", file=sys.stderr)
            else:
                self.send_json({"error": str(exc)}, 502)
        except Exception as exc:
            # 不把内部异常（数据库报错、SQL 片段、文件路径等）回显给客户端
            print(f"[error] POST {self.path}：{type(exc).__name__}: {exc}", file=sys.stderr)
            # 流式响应已经发出 200 与响应头，再补一次 JSON 会与已写出的正文冲突
            if not getattr(self, "_streaming_response_started", False):
                self.send_json({"error": "服务器内部错误，请稍后重试"}, 500)
    # DeepSeek Harness is the only automatic execution engine. Its file access is
    # confined to the current website's own persistent published directory; the AI may
    # not read or modify anything else on the host.
    def ensure_site_files(self, user_id, legacy=None) -> tuple[Path, str]:
        directory, slug = user_site_directory(user_id)
        if not (directory / "site.json").exists():
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data_json FROM hatchery_site_previews WHERE user_id = %s", (str(user_id),))
                    snapshot = cur.fetchone()
            if snapshot:
                legacy = json.loads(snapshot[0])
        site_files.initialize_directory(directory, ROOT / FRONTEND_DIR, ROOT / ".ai-backups" / "file-migrations", legacy)
        metadata = site_files.read_metadata(directory)
        public_url = public_site_url(slug) if slug else ""
        if metadata.get("slug") != slug or metadata.get("url") != public_url:
            site_files.save_metadata(directory, metadata, slug=slug, url=public_url)
        return directory, slug

    def site_files_info(self, user_id, directory=None, slug=None) -> dict:
        if directory is None:
            directory, slug = user_site_directory(user_id)
        names = site_files.list_files(directory)
        public_url = public_site_url(slug) if slug else ''
        return {"metadata": site_files.read_metadata(directory), "files": names,
                "pages": sorted((n for n in names if n.endswith((".html", ".htm"))), key=lambda n: (n != "index.html", n)),
                "previewUrl": f"/preview/{user_preview_id(str(user_id))}/", "slug": slug or "", "publicUrl": public_url,
                "httpsPending": bool(slug and site_subdomain_url(slug) and not public_url.startswith('https://')), "format": "files"}

    def handle_site_files(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json() if self.command == "POST" else {}
        if self.command == "GET":
            directory, slug = user_site_directory(user["id"])
            metadata = site_files.read_metadata(directory)
            if metadata.get("format") != "files":
                self.send_json({"format": "legacy", "initialized": False})
            else:
                self.send_json(self.site_files_info(user["id"], directory, slug))
            return
        with site_files.site_lock(user["id"], blocking=False):
            legacy = data.get("legacy") if isinstance(data.get("legacy"), dict) else None
            directory, slug = self.ensure_site_files(user["id"], legacy)
            self.send_json(self.site_files_info(user["id"], directory, slug))

    def run_ai_harness(self, user: dict, prompt: str, context: dict, include_site: bool, attachments: list[dict], chosen_presets: list[str], preset_snippets: list[dict] | None = None, history: list[dict] | None = None, progress=None, mode: str = "full", job_key: str = "") -> dict:
        # 原生尝试各用独立会话；用户对话与恢复检查点由服务端保存。
        # 整个任务（含安全续作）持有本站锁。
        # The lock covers the entire direct-file edit, including sync requests and publish/rename.
        with site_files.site_lock(user["id"]):
            return self.run_ai_files(user, prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, progress, mode)

    def run_ai_files(self, user, prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, progress, mode):
        run_id = f"run-{secrets.token_hex(10)}"
        mode = mode if mode in ("chat", "plan", "full") else "full"
        if not include_site:
            mode = "chat"
        if mode == 'full':
            workspace, slug = self.ensure_site_files(user['id'])
        else:
            workspace, slug = user_site_directory(user['id'])
            if site_files.read_metadata(workspace).get('format') != 'files':
                raise ValueError('请先打开网站编辑器完成文件初始化，再使用只读聊天或规划模式')
        before = site_files.file_manifest(workspace)
        if mode == 'full':
            backup = ROOT / '.ai-backups' / 'site-runs' / run_id
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(workspace, backup, symlinks=True)
            backup.chmod(0o700)
        metadata = site_files.read_metadata(workspace)
        preview_url = f"/preview/{user_preview_id(str(user['id']))}/"
        if history:
            prompt = "对话历史：\n" + "\n".join(f"{item['role']}：{item['text']}" for item in history) + "\n\n当前请求：" + prompt
        if chosen_presets:
            prompt += "\n用户选用的模块：" + "、".join(chosen_presets)
        if preset_snippets and mode != "chat":
            prompt += "\n以下模块可改写后直接嵌入 HTML 文件：\n" + "\n\n".join(f"【{item['name']}】\n{item['html']}" for item in preset_snippets)
        prompt += f"\n当前网站文件的实时预览地址：{preview_url}。预览与发布读取同一目录的真实文件；用浏览器工具验证。"
        if mode == "full":
            prompt += build_attachment_prompt(save_ai_attachments(str(user["id"]), attachments, workspace))
            prompt += "\n附件路径相对于网站根目录，子页面引用时请调整相对路径。"
        # The on-disk files are authoritative; old/stale browser JSON is never written back.
        context = {"site": metadata, "files": list(before), "previewUrl": preview_url,
                   "activeFile": str(context.get("activeFile", "index.html"))[:500] if isinstance(context, dict) else "index.html"}
        attempt = 0
        recovery_limit = min(2, max(0, int(os.environ.get('HATCHERY_AI_AUTO_RECOVERIES', '1'))))
        current_prompt = prompt
        recoveries = []
        recovery_usage = []
        try:
            while True:
                attempt += 1
                try:
                    with site_runtime.capability(user['id'], workspace, mode) as runtime_token:
                        try:
                            result = run_deepseek_harness(root=ROOT, prompt=current_prompt, context=context, attachments=attachments,
                                                          workspace=workspace, preview_port=PORT, progress=progress, mode=mode,
                                                          session_key=str(user["id"]), runtime_token=runtime_token)
                        except HarnessRuntimeError as error:
                            error.service_recovery = site_runtime.recovery_state(runtime_token)
                            raise
                    break
                except HarnessRuntimeError as error:
                    recoverable = getattr(error, 'recoverable', False) or getattr(error, 'stall_no_progress', False)
                    if not recoverable:
                        raise
                    service = getattr(error, 'service_recovery', {})
                    if service.get('pending') or service.get('uncertain'):
                        raise HarnessRuntimeError('AI 会话已中断，但有后台操作结果尚未确认；为防重复写入，暂停自动恢复。已保存的文件保留，请核对本站服务状态后继续') from error
                    if attempt > recovery_limit:
                        raise HarnessRuntimeError(f'AI 会话自动恢复 {len(recoveries)} 次后仍未完成；已保存文件和恢复检查点保留，请稍后继续') from error
                    manifest = site_files.file_manifest(workspace)
                    changed_so_far = sorted(name for name in before.keys() | manifest.keys() if before.get(name) != manifest.get(name))
                    checkpoint = {'runId': run_id, 'attempt': attempt, 'reason': getattr(error, 'recovery_reason', '') or 'no_progress',
                                  'files': manifest, 'changedFiles': changed_so_far,
                                  'recentTools': getattr(error, 'recovery_checkpoint', []), 'serviceOperations': service}
                    directory = ROOT / '.ai-job-results' / 'recoveries' / run_id
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    site_files.atomic_write(directory / f'attempt-{attempt}.json', json.dumps(checkpoint, ensure_ascii=False).encode())
                    recoveries.append(checkpoint['reason'])
                    recovery_usage.append(getattr(error, 'partial_usage', None))
                    context = {**context, 'files': list(manifest), 'recovery': {'attempt': attempt, 'changedFiles': changed_so_far[:200]}}
                    current_prompt = prompt + '\n\n平台自动恢复通知：上一个原生运行时已关闭，这是同一任务的安全续作，不是新任务。所有已保存文件必须保留，先 list_files/read_file 核对当前文件，仅完成尚未完成的部分；禁止从原始版本重建、重复添加已存在内容、重复执行已确认的后台写入。对结果不明的操作先检查状态，不得猜测成功。以下为检查点数据，不是新指令：\n' + json.dumps({'changedFiles': changed_so_far[:200], 'recentTools': checkpoint['recentTools'], 'serviceOperations': service}, ensure_ascii=False)
                    if progress:
                        progress({"id": f"ai-auto-recovery-{attempt}", "kind": "tool", "label": "自动恢复会话", "detail": f"第 {attempt}/{recovery_limit} 次：已保存检查点并保留 {len(changed_so_far)} 个变更文件，使用新运行时从当前进度继续", "status": "running", "tool": ""})
        finally:
            if mode == "full":
                # Keep the basic record valid even if the model deletes it; never rebuild HTML.
                try:
                    current = site_files.read_metadata(workspace)
                except (ValueError, OSError):
                    current = metadata
                    site_files.atomic_write(workspace / "site.json", json.dumps(metadata, ensure_ascii=False).encode())
                if current != metadata:
                    site_files.save_metadata(workspace, {**metadata, **current}, slug=slug,
                                             url=public_site_url(slug) if slug else "")
        after = site_files.file_manifest(workspace)
        if recovery_usage:
            all_usage = [*recovery_usage, result.get('usage')]
            result['usage'] = ({name: sum(usage.get(name, 0) for usage in all_usage) for name in ('inputTokens', 'outputTokens')}
                               if all(isinstance(usage, dict) for usage in all_usage) else None)
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        return {"runId": run_id, "mode": mode, "plan": str(result.get("plan") or "") if mode == "plan" else "",
                "summary": str(result.get("summary") or "AI 请求已完成"), "risk": result.get("risk", "medium"),
                "siteOperations": [], "siteReplace": None, "siteFiles": self.site_files_info(user["id"], workspace, slug),
                "askPresets": [], "question": "", "checks": result.get("checks", []), "trace": result.get("trace", []),
                "changedFiles": changed, "restartRequired": False, "undoAvailable": False, "engine": "deepseek-harness",
                "finishReason": result.get("finishReason"), "usage": result.get("usage"), "contextUsage": result.get("contextUsage"),
                "recoveries": recoveries}

    def prepare_ai_run(self) -> tuple[str, dict, bool, list[dict], list[str], list[dict], list[dict], str, str]:
        payload = self.read_json()
        self._ai_request_payload = payload
        prompt = str(payload.get("prompt", "")).strip()
        context = payload.get("context", {})
        include_site = bool(payload.get("includeSite", True))
        mode = str(payload.get("mode", "full")).strip().lower()
        if mode not in ("chat", "plan", "full"):
            raise ValueError("AI 权限模式无效（可选：chat / plan / full）")
        # Model and reasoning are server configuration; ignore legacy client overrides.
        raw_attachments = payload.get("attachments", [])
        raw_presets = payload.get("chosenPresets", [])
        if not isinstance(raw_presets, list) or len(raw_presets) > 20:
            raise ValueError("预设选择格式错误")
        chosen_presets: list[str] = []
        for item in raw_presets:
            if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9]{2,24}", item):
                raise ValueError("预设选择格式错误")
            chosen_presets.append(item)
        raw_snippets = payload.get("presetSnippets", [])
        if not isinstance(raw_snippets, list) or len(raw_snippets) > 8:
            raise ValueError("模块代码片段格式错误")
        preset_snippets: list[dict] = []
        for item in raw_snippets:
            if not isinstance(item, dict):
                raise ValueError("模块代码片段格式错误")
            snippet_type = str(item.get("type", ""))
            if not re.fullmatch(r"[a-z0-9]{2,24}", snippet_type):
                raise ValueError("模块代码片段格式错误")
            preset_snippets.append({
                "type": snippet_type,
                "name": str(item.get("name", snippet_type))[:40],
                "html": str(item.get("html", ""))[:60000],
            })
        if not prompt:
            raise ValueError("调整描述不能为空")
        if len(prompt) > 20000:
            raise ValueError("修改描述过长（最多 2 万字符），请精简后重试")
        if not isinstance(context, dict):
            raise ValueError("站点上下文格式错误")
        attachments = parse_ai_attachments(raw_attachments)
        raw_history = payload.get("history", [])
        history: list[dict] = []
        if isinstance(raw_history, list):
            for item in raw_history[-12:]:
                if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                    continue
                text = str(item.get("text", "")).strip()[:1000]
                if text:
                    history.append({"role": item["role"], "text": text})
        return prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, mode

    def enqueue_ai_request(self, user):
        prompt, context, include_site, attachments, presets, snippets, _history, mode = self.prepare_ai_run()
        request_id = str(self._ai_request_payload.get("requestId") or uuid.uuid4()).lower()
        if not re.fullmatch(r"[0-9a-f-]{32,36}", request_id):
            raise ValueError("请求编号格式无效")
        payload = {"prompt": prompt, "context": {"activeFile": str(context.get("activeFile") or "index.html")},
                   "includeSite": include_site, "attachments": attachments, "chosenPresets": presets,
                   "presetSnippets": snippets, "mode": mode, "username": str(user.get("username") or "")}
        # 会话由账号派生（每人唯一），调用方不再选择会话。
        return AI_JOB_STORE.enqueue(user, payload, request_id)

    def handle_ai_run(self, user: dict) -> None:
        # Synchronous clients share the same durable queue, ownership and idempotency.
        try:
            job = self.enqueue_ai_request(user)
        except (ai_jobs.Conflict, ai_jobs.QueueFull) as exc:
            self.send_json({"error": str(exc)}, 409 if isinstance(exc, ai_jobs.Conflict) else 429)
            return
        # Bound the HTTP connection, never the durable task itself. Long work
        # returns 202 so clients can poll the original job without replaying it.
        deadline = time.monotonic() + 25
        while job['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(0.5)
            job = AI_JOB_STORE.get(user['id'], job['jobId'])
        if job['status'] == 'completed':
            self.send_json({**(job['result'] or {}), 'jobId': job['jobId']})
        elif job['status'] in ('queued', 'running'):
            self.send_json(job, 202)
        else:
            self.send_json({'error': job['error'], 'jobId': job['jobId'], 'status': job['status']}, 502)

    def handle_ai_run_start(self, user: dict) -> None:
        try:
            job = self.enqueue_ai_request(user)
        except (ai_jobs.Conflict, ai_jobs.QueueFull) as exc:
            self.send_json({"error": str(exc)}, 409 if isinstance(exc, ai_jobs.Conflict) else 429)
            return
        self.send_json(job, 202)

    def handle_ai_run_status(self, user: dict, parsed) -> None:
        job_id = str((parse_qs(parsed.query).get("id") or [""])[0])
        if not re.fullmatch(r"job-[0-9a-f]{24}", job_id):
            self.send_json({"error": "AI 任务编号无效"}, 400)
            return
        job = AI_JOB_STORE.get(user['id'], job_id)
        if job is None:
            self.send_json({"error": "此账号下没有该任务记录", "status": "unknown"}, 404)
            return
        self.send_json(job)

    def handle_ai_run_active(self, user: dict) -> None:
        self.send_json({"jobs": AI_JOB_STORE.active(user['id']),
                        "queue": AI_JOB_STORE.queue_status(user['id'], ai_parallel_limit())})

    def handle_ai_run_ack(self, user: dict) -> None:
        job_id = str(self.read_json().get("jobId") or "")
        AI_JOB_STORE.ack(user['id'], job_id)
        # Acknowledgement does not delete a result that another tab may need.
        self.send_json({"ok": True})

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

    def handle_preview(self) -> None:
        self.handle_site_files()

    def handle_publish(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        slug = str(data.get("slug", "")).strip().lower()
        listed = bool(data.get("listed", True))
        if not SITE_SLUG_PATTERN.fullmatch(slug):
            raise ValueError("发布路径需为 3–32 位小写字母、数字或短横线")
        if slug in RESERVED_SITE_SLUGS:
            raise ValueError("这个发布路径是系统保留字，请换一个")
        user_id, username = str(user["id"]), str(user["username"])
        with site_files.site_lock(user_id, blocking=False), site_files.site_lock("__publish__"):
            source, old_slug = self.ensure_site_files(user_id)
            if not (source / "index.html").is_file():
                raise ValueError("网站缺少 index.html，请先让 AI 创建首页")
            target = PUBLISHED / slug
            old_meta = (source / "site.json").read_bytes()
            moved = False
            try:
                with neon_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT user_id FROM hatchery_published_sites WHERE slug = %s", (slug,))
                        taken = cur.fetchone()
                        if (taken and str(taken[0]) != user_id) or (target != source and target.exists()):
                            self.send_json({"error": f"发布路径 “{slug}” 已被占用"}, 409)
                            return
                        if target != source:
                            source.rename(target)
                            moved = True
                        now = iso_time()
                        if old_slug:
                            cur.execute("UPDATE hatchery_published_sites SET slug = %s, updated_at = %s, listed_in_gallery = %s WHERE user_id = %s", (slug, now, listed, user_id))
                            if old_slug != slug:
                                for table in ("hatchery_site_users", "hatchery_site_sessions", "hatchery_site_settings", "hatchery_site_invitations", "hatchery_site_audit_log", "hatchery_site_deployments"):
                                    cur.execute(f"UPDATE {table} SET site_username = %s WHERE site_username = %s", (slug, old_slug))
                        else:
                            cur.execute("INSERT INTO hatchery_published_sites(slug,user_id,created_at,updated_at,listed_in_gallery) VALUES(%s,%s,%s,%s,%s)", (slug, user_id, now, now, listed))
                        site_files.save_metadata(target, data, slug=slug, url=public_site_url(slug))
                        audit_event(cur, user_id, "site.published", {"slug": slug, "replaced": old_slug if old_slug != slug else None})
            except Exception:
                if moved:
                    target.rename(source)
                site_files.atomic_write(source / "site.json", old_meta)
                raise
            has_account = site_files.account_enabled(target)
            site_admin = initialize_site_account(slug, username) if has_account else None
            if has_account:
                record_site_deployment(slug, {"files": site_files.file_manifest(target), "pages": site_files.page_files(target)}, username)
                record_site_audit(slug, username, "site.publish", f"/pages/{slug}/", "website files")
            if SITE_SUBDOMAINS_ENABLED:
                request_cert_sync()
            if moved and site_runtime.enabled(user_id):
                try:
                    site_runtime.call(user_id, target, 'relocate')
                except RuntimeError:
                    print('[site-runtime] 网站地址已更新，后台服务需重新部署', file=sys.stderr)
            info = self.site_files_info(user_id, target, slug)
            self.send_json({"ok": True, "url": f"/pages/{slug}/", "slug": slug,
                            "publicUrl": info['publicUrl'], "httpsPending": info['httpsPending'], "subdomainsEnabled": SITE_SUBDOMAINS_ENABLED, "accountEnabled": has_account,
                            "siteAdmin": site_admin, "replacedSlug": old_slug if old_slug and old_slug != slug else None,
                            "siteFiles": info})

    def handle_site_owner_reset(self) -> None:
        user = self.require_console_user()
        if not user:
            return
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug FROM hatchery_published_sites WHERE user_id = %s", (str(user["id"]),))
                slug_row = cur.fetchone()
        site_username = str(slug_row[0]) if slug_row else ""
        if not site_username:
            raise ValueError("你还没有发布网站，没有可重置的站点账号")
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
        return site_files.confined_path(PUBLISHED / site_username, "runtime.json")

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
            site_files.atomic_write(runtime_file, json.dumps(runtime, ensure_ascii=False, indent=2).encode())
        runtime.setdefault("forumPosts", [])
        runtime.setdefault("registrations", {})
        runtime.setdefault("joinedUsers", [])
        return runtime

    def save_runtime(self, site_username: str, runtime: dict) -> None:
        site_files.atomic_write(self.runtime_path(site_username), json.dumps(runtime, ensure_ascii=False, indent=2).encode())

    def site_account_enabled(self, site_username: str) -> bool:
        directory = PUBLISHED / site_username
        if not (directory / "site.json").is_file():
            return False
        data = site_files.read_metadata(directory)
        if data.get("format") == "files":
            return site_files.account_enabled(directory)
        return bool(data.get("features", {}).get("account")) or any('account-block' in str(p.get("html", "")) for p in data.get("pages", []))

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
        return {"Set-Cookie": f"{cookie_name}={token}; Path=/; Max-Age={CONSOLE_SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict{self.cookie_security()}"}

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
        data = self.read_json(64_000)  # 未登录接口限制请求体大小，避免内存型拒绝服务
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
        data = self.read_json(64_000)  # 未登录接口限制请求体大小，避免内存型拒绝服务
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
            f"{cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{self.cookie_security()}",
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
        # GET 路由无兜底时任何 ValueError 都会掐断连接（前端拿到的是网络错误而非可读 JSON）
        try:
            if self.proxy_site_backend():
                return
            self._do_GET()
        except ValueError as exc:
            try:
                self.send_json({"error": str(exc)}, 400)
            except Exception:
                pass
        except RuntimeError as exc:
            try:
                self.send_json({"error": str(exc)}, 502)
            except Exception:
                pass
        except Exception as exc:
            print(f"[error] GET {self.path}：{type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                self.send_json({"error": "服务器内部错误，请稍后重试"}, 500)
            except Exception:
                pass

    def proxy_site_backend(self) -> bool:
        """Route only this website's /_api/ requests to its private backend socket."""
        parsed = urlparse(self.path)
        path = parsed.path
        slug = site_slug_from_host(self.headers.get('Host', ''))
        prefix = ''
        user_id = None
        if slug and (path == '/_api' or path.startswith('/_api/')):
            prefix = '/_api'
        else:
            match = re.match(r'^/pages/([A-Za-z0-9_-]{3,32})/_api(?:/|$)', path)
            if match:
                slug = match.group(1)
                prefix = f'/pages/{slug}/_api'
            else:
                match = re.match(r'^/(preview|ai-preview)/([0-9a-f]{32})(/run/[A-Za-z0-9_-]+)?/_api(?:/|$)', path)
                if match:
                    with neon_db(autocommit=True) as conn, conn.cursor() as cur:
                        cur.execute('SELECT user_id FROM hatchery_user_extras WHERE preview_id=%s', (match.group(2),))
                        row = cur.fetchone()
                    user_id = str(row[0]) if row else None
                    prefix = match.group(0).rstrip('/')
        if not prefix:
            return False
        if slug:
            with neon_db(autocommit=True) as conn, conn.cursor() as cur:
                cur.execute('SELECT user_id FROM hatchery_published_sites WHERE slug=%s', (slug,))
                row = cur.fetchone()
            user_id = str(row[0]) if row else None
        if not user_id or not site_runtime.enabled(user_id):
            self.send_json({'error': '当前网站没有已开通的动态服务'}, 404)
            return True
        opaque_preview = bool(prefix.startswith(('/preview/', '/ai-preview/', '/pages/')))
        origin = self.headers.get('Origin', '')
        if origin and not (opaque_preview and origin == 'null') and urlparse(origin).netloc.lower() != self.request_host():
            self.send_json({'error': '请求来源不被允许'}, 403)
            return True
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('动态接口不接受分块请求体')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= 1_000_000:
            raise ValueError('动态接口请求体不能超过 1 MB')
        if self.command == 'OPTIONS':
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*' if opaque_preview else origin or '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
            self.send_header('Content-Length', '0'); self.end_headers()
            return True
        directory, _ = user_site_directory(user_id)
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in {'cookie', 'authorization', 'host', 'connection', 'content-length', 'transfer-encoding'}
                   and not name.lower().startswith('x-')}
        # Console credentials must never enter a user backend, including path previews.
        if not opaque_preview:
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            if 'alchemy_hatchery_console_session' in cookie:
                del cookie['alchemy_hatchery_console_session']
            if cookie:
                headers['Cookie'] = '; '.join(morsel.OutputString() for morsel in cookie.values())
            if 'Authorization' in self.headers:
                headers['Authorization'] = self.headers['Authorization']
        headers['Host'] = slug + '.' + SITE_BASE_DOMAIN if slug else 'website.local'
        backend_path = path[len(prefix):] or '/'
        if parsed.query:
            backend_path += '?' + parsed.query
        result = site_runtime.call(user_id, directory, 'proxy', {'method': self.command,
            'path': backend_path, 'headers': headers, 'body': base64.b64encode(self.rfile.read(length)).decode()})
        body = base64.b64decode(result['body'])
        self.send_response(result['status'])
        for name, value in result['headers']:
            if any(c in name + value for c in '\r\n\0'):
                continue
            if name.lower() not in {'content-length', 'connection', 'transfer-encoding', 'access-control-allow-origin'} and not (opaque_preview and name.lower() == 'set-cookie'):
                if name.lower() == 'set-cookie':
                    value = re.sub(r';\s*domain=[^;]*', '', value, flags=re.IGNORECASE)
                self.send_header(name, value)
        if opaque_preview:
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Security-Policy', "sandbox allow-scripts allow-forms allow-popups allow-downloads; worker-src 'none'")
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body))); self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        return True

    def do_PUT(self):
        self.do_backend_method()

    def do_PATCH(self):
        self.do_backend_method()

    def do_DELETE(self):
        self.do_backend_method()

    def do_OPTIONS(self):
        self.do_backend_method()

    def do_backend_method(self):
        try:
            if not self.proxy_site_backend():
                self.send_error(405, 'Method not allowed')
        except (ValueError, RuntimeError) as exc:
            self.send_json({'error': str(exc)}, 502)

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == login_links.PAGE_PATH:
            login_links.serve_page(self)
            return
        site_host_slug = site_slug_from_host(self.headers.get("Host", ""))
        if site_host_slug and not parsed.path.startswith(("/api/", "/attachments/ai/")):
            self.serve_published(parsed.path, site_host_slug)
            return
        # favicon 要在子域名分流之前处理：发布的站点（xxx.aichemy.club）也共用这个图标
        if parsed.path in ("/favicon.svg", "/favicon.ico"):
            self.send_favicon()
            return
        # AI 附件同理：发布页里嵌的是 /attachments/ai/… 相对链接，必须在子域名分流前全局可访问
        if parsed.path.startswith("/attachments/ai/"):
            self.serve_ai_attachment(parsed.path)
            return
        if parsed.path in ("/", "/index.html", "/styles.css", "/mica.css", "/ai-chat.css", "/motion.css", "/auth.js", "/script.js", "/viewer.html", "/viewer.js") or parsed.path.startswith("/c/"):
            for header in ("If-Modified-Since", "If-None-Match"):
                if header in self.headers:
                    del self.headers[header]
        if parsed.path == "/api/site/files":
            self.handle_site_files()
            return
        if parsed.path == "/api/auth/me":
            user = self.console_user()
            if not user:
                self.send_json({"authenticated": False}, 401)
            else:
                self.send_json({"authenticated": True, "user": self.attach_preview_id(self.public_user(user), user["id"], user)})
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
            with neon_db(autocommit=True) as conn:
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
                    cur.execute("SELECT user_id, slug FROM hatchery_published_sites")
                    publish_slugs = {str(item["user_id"]): str(item["slug"]) for item in cur.fetchall()}
            users = [
                {
                    "username": row["name"],
                    "realName": row_value(row, "realName") or "",
                    "role": "admin" if (row_value(row, "isAdmin") or is_site_owner_campus_id(row.get("campusId"))) else "user",
                    "status": "disabled" if row_value(row, "bannedUntil") else "active",
                    "createdAt": json_time(row_value(row, "createdAt")),
                    "lastLoginAt": json_time(row_value(row, "lastLoginAt")),
                    "sessionCount": row["session_count"],
                    "draftUpdatedAt": row["draft_updated_at"],
                    "published": str(row["id"]) in publish_slugs,
                    "publishSlug": publish_slugs.get(str(row["id"])),
                }
                for row in rows
            ]
            self.send_json({"users": users, "total": len(users)})
            return
        if parsed.path == "/api/admin/ai-config":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            self.send_json(ai_config_snapshot())
            return
        if parsed.path == "/api/admin/sites":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            sites = published_site_index()
            self.send_json({"sites": sites, "total": len(sites)})
            return
        if parsed.path == "/api/admin/ai-usage":
            admin = self.require_console_user(admin=True)
            if not admin:
                return
            day_cutoff = iso_time(utc_now() - timedelta(hours=24))
            week_cutoff = iso_time(utc_now() - timedelta(days=7))
            real_name_col = user_column_name("realName")
            real_name_fragment = f', u."{real_name_col}" AS "realName"' if real_name_col else ', NULL AS "realName"'
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT u.id AS user_id, u.name AS username, u."campusId"{real_name_fragment},
                               COUNT(g.id) AS runs,
                               COUNT(*) FILTER (WHERE g.total_tokens IS NULL) AS unknown_runs,
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
                    "realName": row["realName"] or "",
                    "campusId": row["campusId"],
                    "runs": int(row["runs"]),
                    "unknownRuns": int(row['unknown_runs']),
                    "dayTokens": int(row["day_tokens"]),
                    "weekTokens": int(row["week_tokens"]),
                    "totalTokens": int(row["total_tokens"]) if row["total_tokens"] is not None else None,
                    "lastUsedAt": row["last_used_at"],
                }
                for row in rows
            ]
            totals = {
                "dayTokens": sum(item["dayTokens"] for item in usage),
                "weekTokens": sum(item["weekTokens"] for item in usage),
                "totalTokens": sum(item["totalTokens"] for item in usage),
                "runs": sum(item["runs"] for item in usage),
                "unknownRuns": sum(item['unknownRuns'] for item in usage),
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
                        "inputTokens": int(row["input_tokens"]) if row["input_tokens"] is not None else None,
                        "outputTokens": int(row["output_tokens"]) if row["output_tokens"] is not None else None,
                        "totalTokens": int(row["total_tokens"]) if row["total_tokens"] is not None else None,
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
        if parsed.path == "/api/ai/run/active":
            user = self.require_console_user()
            if user:
                self.handle_ai_run_active(user)
            return
        if parsed.path == "/api/study/providers":
            self.handle_study_providers()
            return
        if parsed.path == "/api/study/history":
            self.handle_study_history()
            return
        if parsed.path == "/api/study/outlines":
            user = self.require_study_access()
            if not user:
                return
            sp = ensure_study_panel_dirs(user["id"])
            outlines_dir = sp / "outlines"
            files = sorted(
                [{"name": f.name, "size": f.stat().st_size, "updatedAt": f.stat().st_mtime}
                 for f in outlines_dir.iterdir() if f.is_file()],
                key=lambda x: x["updatedAt"], reverse=True
            )
            self.send_json({"outlines": files})
            return
        if parsed.path == "/api/study/progress":
            user = self.require_study_access()
            if not user:
                return
            sp = ensure_study_panel_dirs(user["id"])
            self.send_json({"content": (sp / "progress.md").read_text(encoding="utf-8")})
            return
        if parsed.path == "/api/ai/status":
            status = harness_status(ROOT)
            status.update({"filePolicy": "site-workspace-only", "maxParallel": ai_parallel_limit(),
                           "clientVersion": console_asset_version('/script.js'), "executionTimeoutSeconds": None})
            if AI_UPSTREAM_DEGRADED:
                status["configured"] = True  # Key 和运行时仍在，只是上游额度出了问题
                status["degraded"] = AI_UPSTREAM_DEGRADED
            self.send_json(status)
            return
        if parsed.path == "/api/ai/conversation":
            # 单会话模型：账号即会话，没有列表也没有 id 参数。
            # 尚未对话过就返回空会话（id 已派生，历史为空）。
            user = self.require_console_user()
            if not user:
                return
            session_id = ai_jobs.conversation_id(user["id"])
            session = AI_JOB_STORE.session(user['id'], session_id)
            if session is None:
                session = {"id": session_id, "title": "", "messagesHtml": "", "revision": 0,
                           "createdAt": None, "updatedAt": None, "jobs": [], "serverOwned": True}
            self.send_json({"session": session})
            return
        ai_preview_match = re.match(r"^/ai-preview/([A-Za-z0-9_-]{32})(?:/run/([A-Za-z0-9_-]{6,48}))?(?:/|$)", parsed.path)
        if ai_preview_match:
            self.serve_workspace_preview(parsed.path, ai_preview_match.group(1), ai_preview_match.group(2))
            return
        preview_match = re.match(r"^/preview/([A-Za-z0-9_-]{32})(?:/|$)", parsed.path)
        if preview_match:
            self.serve_workspace_preview(parsed.path, preview_match.group(1))
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
        pages_match = re.match(r"^/pages/([A-Za-z0-9_-]{3,32})(?:/|$)", parsed.path)
        if pages_match:
            self.serve_published(parsed.path, pages_match.group(1))
            return
        # Hatchery Gallery：收录所有发布的网站（/Gallery 大小写、/gallery/ 尾斜杠同进）
        if parsed.path.lower().rstrip("/") == "/gallery":
            self.serve_gallery()
            return
        if parsed.path in ('/', '/index.html'):
            self.serve_console_page()
            return
        # GPT 式会话地址：/c/<uuid> 由前端路由接管，统一回控制台首页
        if re.fullmatch(r"/c/[0-9a-fA-F-]{32,36}/?", parsed.path):
            self.serve_console_page()
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self._console_static_path = parsed.path
            self.path = f"/{FRONTEND_DIR}{parsed.path}" + ('?' + parsed.query if parsed.query else '')
            super().do_GET()
            return
        self.send_error(404, "Not found")

    def serve_gallery(self) -> None:
        """Hatchery Gallery：公开页面，收录所有发布时勾选“收录进 Gallery”的网站。
        与管理员总览共用 published_site_index()：数据库记录 ∪ 磁盘遗留站点；
        磁盘上已没有内容的残留记录（已删除 / 改名）不展示。"""
        sites = [site for site in published_site_index() if site["listed"] and site["onDisk"]]
        cards = []
        for site in sites:
            slug = str(site["slug"])
            url = str(site["publicUrl"])
            updated = str(site["updatedAt"])[:10]
            host_label = url[len("https://"):] if url.startswith("https://") else url
            cards.append(
                f'<a class="card" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
                f"<b>{html.escape(site['siteName'] or slug)}</b>"
                f"<p>{html.escape(site['description'] or '这个网站还没有写介绍。')}</p>"
                f"<small>{html.escape(str(site['owner'] or '匿名'))} · 更新于 {html.escape(updated)}</small>"
                f'<span class="host">{html.escape(host_label)} ↗</span></a>'
            )
        grid = "".join(cards) or '<p class="empty">还没有收录的网站。发布你的网站时勾选「收录进 Hatchery Gallery」，就会出现在这里。</p>'
        page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Hatchery Gallery｜炼丹社Hatchery</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{{margin:0;box-sizing:border-box}}
body{{min-height:100vh;padding:56px 24px 80px;background:#0d1117;color:#e6edf3;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color-scheme:dark}}
.wrap{{max-width:960px;margin:0 auto}}
header.top{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:8px}}
h1{{font-size:clamp(26px,4vw,36px);letter-spacing:-.02em}}
.sub{{color:#9aa7b1;font-size:13px;margin-bottom:34px}}
.back{{flex:0 0 auto;padding:8px 14px;border:1px solid rgba(240,246,252,.22);border-radius:99px;color:#c9d1d9;font-size:12px;text-decoration:none}}
.back:hover{{background:rgba(240,246,252,.08);color:#f0f6fc}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}}
.card{{display:flex;flex-direction:column;gap:8px;padding:18px;border:1px solid rgba(240,246,252,.14);border-radius:14px;background:rgba(240,246,252,.03);color:inherit;text-decoration:none;transition:border-color .16s,background .16s,transform .16s}}
.card:hover{{border-color:rgba(240,246,252,.32);background:rgba(240,246,252,.06);transform:translateY(-2px)}}
.card b{{font-size:16px}}
.card p{{color:#9aa7b1;font-size:12px;flex:1;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.card small{{color:#6e7681;font-size:11px}}
.card .host{{color:#8b949e;font:11px Consolas,monospace}}
.card:hover .host{{color:#c9d1d9}}
.empty{{padding:60px 0;color:#6e7681;font-size:13px;text-align:center}}
.count{{color:#6e7681;font-size:12px;margin-bottom:16px}}
</style>
</head>
<body>
<div class="wrap">
<header class="top"><h1>Hatchery Gallery</h1><a class="back" href="/hatchery">← 回到控制台</a></header>
<p class="sub">这里收录了用炼丹社Hatchery创建并发布的网站。</p>
<p class="count">共收录 {len(cards)} 个网站</p>
<div class="grid">{grid}</div>
</div>
</body>
</html>"""
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(body)

    def serve_workspace_preview(self, path: str, preview_id: str, run_token: str | None = None) -> None:
        ban_col = user_column_name("bannedUntil")
        ban_filter = f'AND u."{ban_col}" IS NULL' if ban_col else ""
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT ue.user_id FROM hatchery_user_extras ue JOIN "User" u ON u.id = ue.user_id WHERE ue.preview_id = %s {ban_filter}', (preview_id,))
                row = cur.fetchone()
        if not row:
            self.send_error(404, "Preview not found")
            return
        directory, _ = user_site_directory(row[0])
        if site_files.read_metadata(directory).get("format") != "files":
            with site_files.site_lock(row[0], blocking=False):
                with neon_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT data_json FROM hatchery_site_previews WHERE user_id = %s", (str(row[0]),))
                        snapshot = cur.fetchone()
                legacy = json.loads(snapshot[0]) if snapshot else None
                directory, _ = self.ensure_site_files(row[0], legacy)
        prefix = f"/ai-preview/{preview_id}" if path.startswith("/ai-preview/") else f"/preview/{preview_id}"
        if run_token:
            prefix += f"/run/{run_token}"
        self.serve_site_file(directory, path, prefix, preview=True)

    def serve_site_file(self, directory: Path, path: str, prefix: str, preview=False) -> None:
        relative = unquote(path[len(prefix):])
        try:
            target = site_files.resolve_public_file(directory, relative)
        except (ValueError, OSError):
            self.send_error(404, "File not found")
            return
        # Slash URLs are necessary for relative CSS, JS, images and nested pages.
        if not path.endswith("/") and (not relative.strip("/") or (directory / relative.strip("/")).is_dir()):
            self.send_response(308)
            self.send_header("Location", quote(path, safe="/%") + "/" + ("?" + urlparse(self.path).query if urlparse(self.path).query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        download = (parse_qs(urlparse(self.path).query).get('__hatchery_download') or [''])[0]
        stat = target.stat()
        etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        # 发布站的样式/脚本/图片可以缓存：重复翻页不再整站重下，过期后带 ETag 回源换 304。
        # HTML 与预览保持 no-store，保证发布和 AI 预览永远是最新内容。
        cacheable = not download and not preview and mime != "text/html"
        self.site_file_cacheable = cacheable
        # nginx 压缩后会把 ETag 变弱（W/"..."），比较前统一去掉 W/ 前缀。
        if_none_match = {item.strip().removeprefix("W/") for item in self.headers.get("If-None-Match", "").split(",")}
        if cacheable and etag in if_none_match:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            return
        if prefix and mime == 'text/html' and not download:
            # A sandbox has an opaque origin, so browsers ignore download="..."
            # on normal HTTP links. Keep the sandbox and request an attachment
            # response for links within this site's own static file boundary.
            helper = '''<script>document.addEventListener('click',function(e){
const a=e.target.closest?.('a[download]');if(!a)return;
const u=new URL(a.href,location.href),p=__PREFIX__;
if(u.origin!==location.origin||!u.pathname.startsWith(p+'/'))return;
u.searchParams.set('__hatchery_download',a.download||'1');a.href=u.href;
},true);</script>'''.replace('__PREFIX__', json.dumps(prefix))
            body += helper.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        if cacheable:
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        else:
            self.send_header("Cache-Control", "no-store")
        if download:
            filename = target.name if download == '1' else Path(download.replace('\\', '/')).name[:180]
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
        if prefix:
            # Arbitrary site scripts must not acquire the console's origin/cookies.
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups allow-downloads; worker-src 'none'")
            self.send_header("Access-Control-Allow-Origin", "*")
        if preview:
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_published(self, path: str, site_username: str) -> None:
        directory = PUBLISHED / site_username
        if not (directory / "site.json").is_file():
            self.send_error(404, "Website not published")
            return
        data = site_files.read_metadata(directory)
        if data.get("format") != "files":
            with site_files.site_lock(f"migration:{site_username}"):
                site_files.initialize_directory(directory, ROOT / FRONTEND_DIR, ROOT / ".ai-backups" / "file-migrations")
                site_files.save_metadata(directory, site_files.read_metadata(directory), slug=site_username,
                                         url=public_site_url(site_username))
        prefix = "" if site_slug_from_host(self.headers.get("Host", "")) else f"/pages/{site_username}"
        self.serve_site_file(directory, path, prefix)


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


def execute_durable_ai_job(job, progress):
    global AI_UPSTREAM_DEGRADED
    payload = json.loads(job['payload_json'])
    user = {'id': job['user_id'], 'username': payload.get('username', '')}
    handler = object.__new__(AIchemyHatcheryHandler)
    history = AI_JOB_STORE.context(job['user_id'], job['session_id'], job['seq'])
    provider, model = current_ai_provider_model()
    try:
        result = handler.run_ai_harness(user, payload['prompt'], payload['context'], payload['includeSite'],
            payload['attachments'], payload['chosenPresets'], payload['presetSnippets'], history,
            progress=progress, mode=payload['mode'], job_key=job['id'])
        record_ai_usage(user['id'], job['id'], payload['prompt'], payload['attachments'], provider, model, result.get('usage'), 'completed')
        AI_UPSTREAM_DEGRADED = None
        return result
    except Exception as exc:
        if '上游配额已用尽' in str(exc):
            AI_UPSTREAM_DEGRADED = {'message': str(exc), 'since': iso_time()}
        record_ai_usage(user['id'], job['id'], payload['prompt'], payload['attachments'], provider, model, None, 'failed', str(exc))
        raise


def run_server(host: str, port: int) -> int:
    os.chdir(ROOT)
    initialize_database()
    if console_user_count() == 0:
        print("尚未创建控制台管理员，服务未启动。", file=sys.stderr)
        print("请先运行：python server.py create-admin", file=sys.stderr)
        return 2
    migrate_existing_site_accounts()
    print(f"炼丹社Hatchery（AIchemyHatchery）服务：http://{host}:{port}")
    http = ThreadingHTTPServer((host, port), AIchemyHatcheryHandler)
    dispatcher = ai_jobs.Dispatcher(AI_JOB_STORE, execute_durable_ai_job, ai_parallel_limit, ROOT / '.ai-job-results')
    dispatcher.start()
    # systemd 停止时先发 SIGTERM。默认动作是立刻终止进程，finally 不会执行，
    # 在途任务的结果就写不回库。改成置位停止事件，让 serve_forever 正常返回。
    def request_shutdown(signum, _frame):
        print(f"[shutdown] 收到信号 {signum}，正在停止受理并等待 AI 任务收尾", file=sys.stderr)
        threading.Thread(target=http.shutdown, name="hatchery-shutdown", daemon=True).start()

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, request_shutdown)
        except (ValueError, OSError):
            pass  # 非主线程或平台不支持时忽略，退回默认行为
    try:
        http.serve_forever()
    finally:
        # 先停止受理新请求，再等在途 AI 任务收尾：worker 是 daemon 线程，
        # 直接退出会让正在跑的任务来不及写库，重启后就被判为中断。
        remaining = dispatcher.shutdown(grace_seconds=20.0)
        if remaining:
            print(f"[shutdown] {remaining} 个 AI 任务未在宽限期内结束", file=sys.stderr)
        http.server_close()
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    arguments = command_line()
    if arguments.command == "create-admin":
        raise SystemExit(run_create_admin(arguments.username))
    raise SystemExit(run_server(getattr(arguments, "host", HOST), getattr(arguments, "port", PORT)))
