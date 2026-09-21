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
import smtplib
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

from deepseek_harness_adapter import HarnessRuntimeError, harness_status, resolve_llm_provider, run_deepseek_harness

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

_DB_POOL: pg_pool.ThreadedConnectionPool | None = None
_DB_POOL_LOCK = threading.Lock()


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
def neon_db():
    """Yield a pooled Neon PostgreSQL connection with RealDictCursor.
    取出时先 SELECT 1 探活，空闲被 Neon 断开的旧连接丢弃换新。"""
    conn = db_pool().getconn()
    try:
        try:
            with conn.cursor() as probe:
                probe.execute("SELECT 1")
        except psycopg2.Error:
            db_pool().putconn(conn, close=True)
            conn = db_pool().getconn()
        # Neon 连接池可能归还残留事务的会话；先回滚再设 autocommit，
        # 否则 set_session 会抛 "cannot be used inside a transaction"。
        conn.rollback()
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        db_pool().putconn(conn)
RUNTIME_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
# 通用限流桶（按 "用途:标识" 计数），供验证码发送/校验等敏感接口使用
RATE_BUCKETS: dict[str, list[float]] = {}
RATE_BUCKETS_LOCK = threading.Lock()
# 管理员在面板里改 AI Key 时，.env 写入与 os.environ 更新必须串行
AI_CONFIG_LOCK = threading.Lock()
KIMI_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{6,199}$")
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
# AI 任务并发上限：工作区已按任务隔离（sites/<user>/.runs/<job>），不再需要全局串行，
# 槽位只做资源背压（每个任务都是一条 harness 子进程 + 一路模型调用）。
AI_RUN_MAX_PARALLEL = max(1, int(os.environ.get("HATCHERY_AI_MAX_PARALLEL", "4")))
AI_RUN_SLOTS_LOCK = threading.Lock()
AI_RUN_SLOT_OWNERS: set[str] = set()
# 看门狗阈值必须大于 harness 自身超时，否则合法的长跑会被强标失败并强放槽位，
# 而子进程还在继续跑。跟随 DEEPSEEK_HARNESS_TIMEOUT_SECONDS 推导。
_HARNESS_TIMEOUT_FOR_STALE = max(60.0, min(1800.0, float(os.environ.get("DEEPSEEK_HARNESS_TIMEOUT_SECONDS", "1200"))))
AI_JOB_STALE_SECONDS = float(os.environ.get("HATCHERY_AI_JOB_STALE_SECONDS", _HARNESS_TIMEOUT_FOR_STALE * 1.1 + 60))


def ai_run_slot_acquire(owner: str) -> bool:
    with AI_RUN_SLOTS_LOCK:
        if len(AI_RUN_SLOT_OWNERS) < AI_RUN_MAX_PARALLEL:
            AI_RUN_SLOT_OWNERS.add(owner)
            return True
    ai_run_jobs_sweep_stale(time.time())
    with AI_RUN_SLOTS_LOCK:
        if len(AI_RUN_SLOT_OWNERS) < AI_RUN_MAX_PARALLEL:
            AI_RUN_SLOT_OWNERS.add(owner)
            return True
    return False


def ai_run_slot_release(owner: str) -> None:
    with AI_RUN_SLOTS_LOCK:
        AI_RUN_SLOT_OWNERS.discard(owner)


def user_running_ai_job(user_id) -> str | None:
    """同一账号同时只允许一个 AI 任务：网站只有一份，两个并行任务各自基于同一份快照改动，
    后完成者的整站结果会把先完成者的修改覆盖掉。返回进行中的 jobId（超过 1 小时的僵尸任务不算）。"""
    now = time.time()
    with AI_RUN_JOBS_LOCK:
        for job_id, job in AI_RUN_JOBS.items():
            if (str(job.get("userId", "")) == str(user_id) and job.get("status") == "running"
                    and now - float(job.get("createdAt", now)) < 3600):
                return job_id
    return None


def ai_run_jobs_sweep_stale(now: float) -> int:
    """清理超时仍挂起的 AI 任务：标记失败并强放其并发槽位。返回清理的任务数。
    槽位可能被 harness 子进程挂起长期占用；超过 harness 自身超时后仍持有即判定为泄漏。
    工作区已按任务隔离，被强放槽位的旧 worker 即便还活着也只写自己的目录，不会污染新任务。"""
    with AI_RUN_JOBS_LOCK:
        stale_ids = [
            key for key, value in AI_RUN_JOBS.items()
            if value.get("status") == "running" and now - float(value.get("createdAt", now)) > AI_JOB_STALE_SECONDS
        ]
        for key in stale_ids:
            AI_RUN_JOBS[key].update({"status": "failed", "error": "任务超时被系统终止"})
    if stale_ids:
        with AI_RUN_SLOTS_LOCK:
            for key in stale_ids:
                AI_RUN_SLOT_OWNERS.discard(key)
    return len(stale_ids)
AI_RUN_JOBS_LOCK = threading.Lock()
AI_BACKUPS: dict[str, dict[str, str]] = {}
AI_RUN_JOBS: dict[str, dict] = {}
# 上游 AI 当前是否处于"额度耗尽"等降级状态：用户一打开编辑器就该知道，
# 而不是写完需求点发送、白等几秒才收到失败。任务成功即自动清除。
AI_UPSTREAM_DEGRADED: dict | None = None
# 前端静态文件统一放在 frontend/；对外 URL 仍是根路径（/styles.css…），由 handler 翻译
FRONTEND_DIR = "frontend"
AI_SOURCE_FILES = ("server.py", "frontend/index.html", "frontend/styles.css", "frontend/mica.css", "frontend/ai-chat.css", "frontend/motion.css", "frontend/auth.js", "frontend/script.js", "frontend/viewer.html", "frontend/viewer.js")
PUBLIC_STATIC_PATHS = frozenset(("/index.html", "/styles.css", "/mica.css", "/ai-chat.css", "/motion.css", "/auth.js", "/script.js", "/viewer.js"))
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


def ai_site_workspace(user_id) -> Path:
    """The one folder the AI may touch: the current website's own directory."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))[:64] or "unknown"
    return ROOT / "sites" / safe


# 每次 AI 任务拿独立的临时工作区（用户目录下的 .runs/<任务号>），
# 让同一用户的多个会话可以并行执行而互不覆盖 site.json。
def ai_job_workspace(user_id, job_key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "", str(job_key))[:48] or "unknown"
    return ai_site_workspace(user_id) / ".runs" / safe_key


# 进行中任务的 job_key -> (user_id, workspace)：供 /ai-preview/<id>/run/<job_key> 实时渲染该任务的工作区
AI_RUN_WORKSPACES: dict[str, tuple[str, Path]] = {}


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


def save_ai_attachments(user_id: str, attachments: list[dict]) -> list[dict]:
    """把本次任务的附件写进临时目录，返回带公开 URL 的清单（AI 用它嵌入网页）。"""
    manifest: list[dict] = []
    if not attachments:
        return manifest
    sweep_stale_ai_attachments()
    safe_user = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))[:64] or "unknown"
    for item in attachments:
        name = str(item.get("name", "附件"))
        kind = str(item.get("kind", "text"))
        if kind == "text":
            mime_type, payload = str(item.get("type", "text/plain")), str(item.get("content", "")).encode("utf-8")
        else:
            try:
                mime_type, payload = decode_data_url(str(item.get("content", "")))
            except ValueError:
                continue  # 单个附件损坏不拖垮整个任务
        attach_id = secrets.token_hex(8)
        attach_dir = AI_ATTACHMENT_ROOT / safe_user / attach_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        (attach_dir / name).write_bytes(payload)
        manifest.append({"name": name, "kind": kind, "type": mime_type, "size": len(payload),
                         "url": f"/attachments/ai/{safe_user}/{attach_id}/{quote(name)}"})
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
    with neon_db() as conn:
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
    site.json 带着整站页面（含 base64 图片，动辄数 MB），Gallery/总览每次都全量解析会越来越慢，
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


def public_site_url(slug: str) -> str:
    """站点公开地址。DNS 安全的发布路径走子域名；历史遗留的老用户名目录（大写、下划线）
    不能当子域名，只能走 /pages/<slug> 路径。"""
    if SITE_SLUG_PATTERN.fullmatch(slug) and slug not in RESERVED_SITE_SLUGS:
        return f"https://{slug}.{SITE_BASE_DOMAIN}"
    return f"/pages/{slug}"


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
            if not entry.is_dir():
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
    ALTER TABLE hatchery_published_sites ADD COLUMN IF NOT EXISTS listed_in_gallery BOOLEAN NOT NULL DEFAULT TRUE;
    -- 失败原因落库：任务失败时排障不必依赖转瞬即逝的内存与容器日志
    ALTER TABLE hatchery_ai_usage ADD COLUMN IF NOT EXISTS error TEXT;
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


def record_ai_usage(user_id: str, job_id: str | None, prompt: str, attachments: list[dict], provider: str, model: str, usage: dict | None, status: str, error: str = "") -> None:
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
                        input_tokens + output_tokens,
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
    return {
        "provider": provider_name,
        "providerLabel": provider["label"],
        "model": os.environ.get(provider["model_env"], "").strip() or provider["default_model"],
        "explicitProvider": os.environ.get("MIAODA_LLM", "").strip().lower(),
        "kimiKeySet": bool(kimi_key),
        "kimiKeyMask": f"···{kimi_key[-4:]}" if kimi_key else "",
        "glmKeySet": bool(glm_key),
        "glmKeyMask": f"···{glm_key[-4:]}" if glm_key else "",
        "deepseekKeySet": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
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
        static_path = urlparse(self.path).path
        if static_path == "/" or static_path in PUBLIC_STATIC_PATHS:
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
        if parsed.path == "/":
            self.path = f"/{FRONTEND_DIR}/index.html"
            super().do_HEAD()
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self.path = f"/{FRONTEND_DIR}{parsed.path}"
            super().do_HEAD()
            return
        if parsed.path in ("/favicon.svg", "/favicon.ico"):
            self.send_favicon(head_only=True)
            return
        self.send_error(404, "Not found")

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

    def attach_preview_id(self, payload: dict, user_id: str) -> dict:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT preview_id FROM hatchery_user_extras WHERE user_id = %s", (str(user_id),))
                row = cur.fetchone()
                cur.execute("SELECT slug, listed_in_gallery FROM hatchery_published_sites WHERE user_id = %s", (str(user_id),))
                site = cur.fetchone()
        payload["previewId"] = row[0] if row else None
        payload["publishSlug"] = site[0] if site else None
        payload["publishListed"] = bool(site[1]) if site else None
        payload["publishDomain"] = SITE_BASE_DOMAIN
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

    def handle_admin_ai_config_update(self) -> None:
        # 改 AI Key 影响全站 AI 服务，只有管理员可用；Key 本身永远不进日志与审计
        admin = self.require_console_user(admin=True)
        if not admin:
            return
        data = self.read_json()
        if "glmApiKey" in data:
            # 智谱 GLM Key 形如 <32 位 id>.<16 位 secret>，与 Kimi 共用同一字符白名单；Kimi 分支保持原样
            key = str(data.get("glmApiKey", "")).strip()
            if key and not KIMI_KEY_PATTERN.fullmatch(key):
                raise ValueError("Key 格式无效：应为 7 位以上的字母数字串（可含 -、_、.），智谱 GLM Key 一般形如 xxxx.xxxx")
            with AI_CONFIG_LOCK:
                persist_env_value("GLM_API_KEY", key)
                os.environ["GLM_API_KEY"] = key
                with neon_db() as conn:
                    with conn.cursor() as cur:
                        audit_event(cur, str(admin["id"]), "admin.ai_config", {"glmKeySet": bool(key)})
            self.send_json({"ok": True, **ai_config_snapshot()})
            return
        key = str(data.get("kimiApiKey", "")).strip()
        if key and not KIMI_KEY_PATTERN.fullmatch(key):
            raise ValueError("Key 格式无效：应为 7 位以上的字母数字串（可含 -、_、.），Kimi Key 一般以 sk- 开头")
        with AI_CONFIG_LOCK:
            persist_env_value("KIMI_API_KEY", key)
            os.environ["KIMI_API_KEY"] = key
            with neon_db() as conn:
                with conn.cursor() as cur:
                    audit_event(cur, str(admin["id"]), "admin.ai_config", {"kimiKeySet": bool(key)})
        self.send_json({"ok": True, **ai_config_snapshot()})

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
            # CSRF：所有写操作都要求同源发起（原先仅依赖 SameSite=Strict，同站子域可绕过）
            if not self.same_origin_request():
                self.send_json({"error": "请求来源不被允许"}, 403)
                return
            if path == "/api/auth/login":
                self.handle_auth_login()
            elif path == "/api/auth/send-otp":
                self.handle_send_otp()
            elif path == "/api/auth/logout":
                self.handle_auth_logout()
            elif path == "/api/auth/change-password":
                self.handle_change_password()
            elif path == "/api/admin/users/status":
                self.handle_admin_user_status()
            elif path == "/api/admin/ai-config":
                self.handle_admin_ai_config_update()
            elif path == "/api/auth/sessions/revoke-others":
                self.handle_revoke_other_sessions()
            elif path == "/api/console/draft":
                self.handle_save_draft()
            elif path == "/api/site-account/reset-owner":
                self.handle_site_owner_reset()
            elif path == "/api/ai/sessions":
                user = self.require_console_user()
                if user:
                    self.handle_ai_session_create(user)
            elif path.startswith("/api/ai/sessions/"):
                user = self.require_console_user()
                if not user:
                    return
                session_match = re.fullmatch(r"/api/ai/sessions/([0-9a-fA-F-]{32,36})(/delete)?", path)
                if not session_match:
                    self.send_json({"error": "接口不存在"}, 404)
                elif session_match.group(2) == "/delete":
                    self.handle_ai_session_delete(user, session_match.group(1))
                else:
                    self.handle_ai_session_update(user, session_match.group(1))
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
            self.send_json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 502)
        except Exception as exc:
            # 不把内部异常（数据库报错、SQL 片段、文件路径等）回显给客户端
            print(f"[error] POST {self.path}：{type(exc).__name__}: {exc}", file=sys.stderr)
            self.send_json({"error": "服务器内部错误，请稍后重试"}, 500)
    # DeepSeek Harness is the only automatic execution engine. Its file access is
    # confined to the current website's own folder (sites/<user_id>/); the AI may
    # not read or modify anything else on the host.
    @staticmethod
    def sweep_stale_job_workspaces(user_id) -> None:
        """清理一天前的任务级工作区残留（异常中断的任务目录靠这里回收）。"""
        runs_root = ai_site_workspace(user_id) / ".runs"
        try:
            entries = list(runs_root.iterdir())
        except OSError:
            return
        cutoff = time.time() - 86400
        for entry in entries:
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass

    def run_ai_harness(self, user: dict, prompt: str, context: dict, include_site: bool, attachments: list[dict], chosen_presets: list[str], preset_snippets: list[dict] | None = None, history: list[dict] | None = None, progress=None, mode: str = "full", model_override: str = "", effort_override: str = "", job_key: str = "") -> dict:
        run_id = f"run-{secrets.token_hex(10)}"
        if mode not in ("chat", "plan", "full"):
            mode = "full"
        if history:
            lines = ["对话历史（帮助理解上下文，按时间先后；当前请求见末尾）："]
            for item in history:
                lines.append(f"{'用户' if item['role'] == 'user' else 'AI'}：{item['text']}")
            prompt = "\n".join(lines) + "\n\n当前请求：" + prompt
        site_state = context.get("site") if isinstance(context, dict) else None
        if not isinstance(site_state, dict) or not isinstance(site_state.get("pages"), list):
            raise ValueError("缺少当前网站数据，无法启动 AI 任务")
        if chosen_presets:
            if mode == "full":
                prompt = prompt + "\n\n用户已确认选用这些预设模块：" + "、".join(chosen_presets) + "。请直接开始制作网站，把它们融入设计，不要再询问。"
            elif mode == "plan":
                prompt = prompt + "\n\n用户想选用这些预设模块：" + "、".join(chosen_presets) + "。请把它们纳入实施计划。"
        if preset_snippets and mode != "chat":
            blocks = "\n\n".join(f"【{item['name']} / {item['type']}】\n{item['html']}" for item in preset_snippets)
            if mode == "full":
                prompt = prompt + "\n\n用户手动选用了以下模块。请读取它们的代码并嵌入到整页代码中（可自由改写文案与样式以融入整体设计，但保留模块的结构意图）：\n\n" + blocks
            else:
                prompt = prompt + "\n\n用户手动选用了以下模块，请把它们的结构意图纳入实施计划：\n\n" + blocks
        total_elements = sum(len(page.get("elements", [])) for page in site_state.get("pages", []) if isinstance(page, dict))
        if mode == "full" and not chosen_presets and total_elements == 0:
            style_seed = secrets.choice(SITE_STYLE_SEEDS)
            prompt = prompt + f"\n\n本次是从零建站。如果用户没有透露社团、项目或爱好等风格线索，参考这个随机风格方向：{style_seed}；如果用户给了线索，以用户线索为准。"
        run_token = re.sub(r"[^A-Za-z0-9_-]", "", str(job_key))[:48]
        if run_token:
            # 每个任务独立工作区：同一会话/同一用户的多个任务并行时互不覆盖 site.json
            self.sweep_stale_job_workspaces(user["id"])
            workspace = ai_job_workspace(user["id"], run_token)
        else:
            workspace = ai_site_workspace(user["id"])
        workspace.mkdir(parents=True, exist_ok=True)
        preview_url = f"/ai-preview/{user_preview_id(user['id'])}"
        if run_token:
            preview_url = f"{preview_url}/run/{run_token}"
            with AI_RUN_JOBS_LOCK:
                AI_RUN_WORKSPACES[run_token] = (str(user["id"]), workspace)
        if mode == "full":
            prompt = prompt + f"\n\n本次改动的工作区实时预览地址：{preview_url}（它实时渲染你改完后的 site.json。用浏览器工具打开它验证改动效果，不要打开控制台首页——那是登录页，看不到任何东西）。"
        elif mode == "plan":
            prompt = prompt + f"\n\n当前网站的实时预览地址：{preview_url}（实时渲染当前 site.json。可用浏览器工具打开查看现状，不要打开控制台首页——那是登录页，看不到任何东西）。"
        site_json_path = workspace / "site.json"
        original_site_json = json.dumps(site_state, ensure_ascii=False, indent=2)
        write_text_exact(site_json_path, original_site_json)
        # 附件先落盘换稳定 URL，清单写进提示词：AI 拿 URL 嵌入网页；图片是否额外喂给模型由适配器能力决定
        attachment_manifest = save_ai_attachments(str(user["id"]), attachments)
        prompt = prompt + build_attachment_prompt(attachment_manifest)

        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    result = run_deepseek_harness(
                        root=ROOT,
                        prompt=prompt,
                        context=context,
                        attachments=attachments,
                        workspace=workspace,
                        preview_port=PORT,
                        progress=progress,
                        mode=mode,
                        model=model_override or None,
                        reasoning=effort_override or None,
                    )
                    break
                except HarnessRuntimeError as error:
                    # 上游偶发"接单后一言不发"（0 进展直接判卡死）：手动重试基本都能成功，
                    # 这里自动整机重试一次——site.json 还原成任务前状态，避免半截改动带偏第二次
                    if attempt > 1 or not getattr(error, "stall_no_progress", False):
                        raise
                    print(f"[ai-run] {run_id} 上游零进展卡死，自动重试一次", file=sys.stderr, flush=True)
                    write_text_exact(site_json_path, original_site_json)
                    if progress:
                        progress({"id": "ai-auto-retry", "kind": "tool", "label": "自动重试", "detail": "上游长时间没有响应，本次任务已自动重新开始", "status": "running", "tool": ""})
        finally:
            if run_token:
                # 任务结束即摘掉任务级预览入口；目录在读完 site.json 后删除
                with AI_RUN_JOBS_LOCK:
                    AI_RUN_WORKSPACES.pop(run_token, None)

        site_replace = None
        changed_files: list[str] = []
        if not site_json_path.is_file():
            raise ValueError("AI 删除了网站数据文件 site.json，本次任务已中止")
        after_text = read_text_exact(site_json_path)
        if run_token:
            shutil.rmtree(workspace, ignore_errors=True)
        if after_text != original_site_json:
            if len(after_text.encode("utf-8")) > 4_000_000:
                raise ValueError("AI 修改后的网站数据超过大小限制，本次任务已中止")
            try:
                site_replace = json.loads(after_text)
            except json.JSONDecodeError as exc:
                write_text_exact(site_json_path, original_site_json)
                raise ValueError(f"AI 把 site.json 改成了无法解析的 JSON，已还原：{exc}") from exc
            self.validate_site_replace(site_replace)
            if site_replace == site_state:
                site_replace = None
            else:
                changed_files = ["site.json"]

        proposal = self.validate_ai_proposal(
            {
                "summary": result.get("summary", "网站修改已完成"),
                "risk": result.get("risk", "medium"),
                "assumptions": [],
                "siteOperations": result.get("siteOperations", []),
                "checks": result.get("checks", []),
            },
            context,
        )
        proposal = self.constrain_instance_intent(proposal, prompt, context)
        if mode != "full":
            # 只读模式硬兜底：即便模型越权改了 site.json 或返回了站点操作，也一律丢弃并还原
            if site_replace is not None or proposal["siteOperations"]:
                proposal["checks"].append(f"{'仅聊天' if mode == 'chat' else 'plan模式'}为只读模式，已丢弃模型的越权改动")
            if site_replace is not None:
                write_text_exact(site_json_path, original_site_json)
            site_replace = None
            proposal["siteOperations"] = []
            changed_files = []
        if site_replace is not None and proposal["siteOperations"]:
            # site.json 已包含全部改动。模型常把同样的改动又在 siteOperations 里重复描述，
            # 再应用一次会重复元素，或指向替换后已不存在的页面/ID（整批回滚的直接原因）。
            dropped = len(proposal["siteOperations"])
            proposal["siteOperations"] = []
            proposal["checks"].append(f"网站数据已整体更新，忽略了模型重复返回的 {dropped} 条站点操作")
        if not include_site:
            proposal["siteOperations"] = []
            site_replace = None
            changed_files = []

        # 模型应自行决定模块，不再向用户提问；askPresets 一律不下发，
        # 但如果模型还是问了而不是直接做，把它做的任何改动丢弃，避免半成品落站。
        model_asked = bool(result.get("askPresets"))
        if model_asked and site_replace is None and not proposal["siteOperations"]:
            proposal["checks"].append("模型试图先让你选模块而不是直接制作，本次未应用改动；可直接重试，或在左侧“浏览模板”手动挑选模块")
            changed_files = []

        return {
            "runId": run_id,
            "mode": mode,
            "plan": str(result.get("plan") or "")[:8000] if mode == "plan" else "",
            "summary": proposal["summary"],
            "risk": proposal["risk"],
            "siteOperations": proposal["siteOperations"],
            "siteReplace": site_replace,
            "askPresets": [],
            "question": "",
            "checks": proposal["checks"],
            "trace": result.get("trace", []),
            "changedFiles": changed_files,
            "restartRequired": False,
            "undoAvailable": False,
            "engine": "deepseek-harness",
            "finishReason": result.get("finishReason"),
            "usage": result.get("usage"),
        }

    def validate_site_replace(self, data) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or not data["pages"]:
            raise ValueError("AI 修改后的网站数据结构无效，本次任务已中止")
        for page in data["pages"]:
            if not isinstance(page, dict) or not isinstance(page.get("elements", []), list):
                raise ValueError("AI 修改后的页面数据结构无效，本次任务已中止")

    def validate_ai_proposal(self, proposal: dict, context: dict | None = None) -> dict:
        if not isinstance(proposal, dict):
            raise ValueError("DeepSeek Harness 返回的方案不是对象")
        proposal["summary"] = str(proposal.get("summary", "AI 已生成修改方案"))[:500]
        if proposal.get("risk") not in ("low", "medium", "high"):
            proposal["risk"] = "medium"
        operations = proposal.get("siteOperations", [])
        if not isinstance(operations, list) or len(operations) > 40:
            raise ValueError("站点修改操作格式错误或数量过多")
        allowed_ops = {"set_site", "set_page", "add_page", "remove_page", "add_element", "update_element", "remove_element", "move_element", "set_items"}
        valid_element_types = None
        if isinstance(context, dict) and isinstance(context.get("elementTypes"), list):
            valid_element_types = {str(item) for item in context["elementTypes"]}
        kept_operations = []
        dropped_types: list[str] = []
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") not in allowed_ops:
                raise ValueError("DeepSeek Harness 返回了不支持的站点操作")
            # 模型偶尔编造编辑器不存在的模块类型；整条失败会回滚全部改动，改为丢弃这一条并记录
            if valid_element_types is not None and operation.get("op") == "add_element" and str(operation.get("type", "")) not in valid_element_types:
                dropped_types.append(str(operation.get("type", ""))[:24])
                continue
            kept_operations.append(operation)
        operations = kept_operations
        proposal["siteOperations"] = operations
        proposal["assumptions"] = [str(item)[:300] for item in proposal.get("assumptions", []) if str(item).strip()][:10]
        proposal["checks"] = [str(item)[:300] for item in proposal.get("checks", []) if str(item).strip()][:10]
        if dropped_types:
            proposal["checks"].append(f"已忽略 {len(dropped_types)} 个编辑器不支持的模块类型：{'、'.join(dropped_types)}")
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
        proposal["risk"] = "low"
        proposal["summary"] = f"仅修改当前{('首页大字' if target_type == 'hero' else '公告栏')}实例；已阻止全局样式和源码改动。"
        proposal["checks"] = ["检查当前模块实例立即变化", "检查同类模块与后续新增模块保持默认样式"]
        return proposal

    def prepare_ai_run(self) -> tuple[str, dict, bool, list[dict], list[str], list[dict], str]:
        payload = self.read_json()
        prompt = str(payload.get("prompt", "")).strip()
        context = payload.get("context", {})
        include_site = bool(payload.get("includeSite", True))
        mode = str(payload.get("mode", "full")).strip().lower()
        if mode not in ("chat", "plan", "full"):
            raise ValueError("AI 权限模式无效（可选：chat / plan / full）")
        chosen_model = str(payload.get("model", "")).strip()[:64]
        if chosen_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", chosen_model):
            raise ValueError("模型选择无效")
        chosen_effort = str(payload.get("reasoningEffort", "")).strip().lower()
        if chosen_effort and chosen_effort not in ("off", "low", "high"):
            raise ValueError("推理档位无效（可选：off / low / high）")
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
        # 会话锚点：任务挂到某个聊天会话上，关掉网页再打开能按用户找回进度
        session_id = str(payload.get("sessionId") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f-]{32,36}", session_id):
            session_id = ""
        return prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, mode, chosen_model, chosen_effort, session_id

    def handle_ai_session_create(self, user: dict) -> None:
        payload = self.read_json()
        title = str(payload.get("title") or "新聊天").strip()[:60] or "新聊天"
        messages_html = str(payload.get("messagesHtml") or "")
        if len(messages_html) > 600_000:
            raise ValueError("会话内容过大，无法保存")
        # 前端在发第一条消息时就本地生成会话 id 并绑给 AI 任务，创建必须是幂等 upsert
        requested_id = str(payload.get("id") or "")
        if requested_id:
            if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", requested_id):
                raise ValueError("会话编号格式无效")
            session_id = requested_id.lower()
        else:
            session_id = str(uuid.uuid4())
        now = iso_time()
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hatchery_ai_chat_sessions(id, user_id, title, messages_html, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, messages_html=EXCLUDED.messages_html, updated_at=EXCLUDED.updated_at"
                    " WHERE hatchery_ai_chat_sessions.user_id = EXCLUDED.user_id",
                    (session_id, str(user["id"]), title, messages_html, now, now),
                )
                if cur.rowcount == 0:
                    self.send_json({"error": "会话编号冲突，请刷新后重试"}, 409)
                    return
        self.send_json({"session": {"id": session_id, "title": title, "createdAt": now, "updatedAt": now}})

    def handle_ai_session_update(self, user: dict, session_id: str) -> None:
        payload = self.read_json()
        fields, values = [], []
        if "title" in payload:
            title = str(payload.get("title") or "").strip()[:60]
            if title:
                fields.append("title = %s")
                values.append(title)
        if "messagesHtml" in payload:
            messages_html = str(payload.get("messagesHtml") or "")
            if len(messages_html) > 600_000:
                raise ValueError("会话内容过大，无法保存")
            fields.append("messages_html = %s")
            values.append(messages_html)
        if not fields:
            self.send_json({"ok": True})
            return
        fields.append("updated_at = %s")
        values.append(iso_time())
        values.extend([session_id, str(user["id"])])
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE hatchery_ai_chat_sessions SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
                    values,
                )
                if cur.rowcount == 0:
                    self.send_json({"error": "会话不存在或已删除"}, 404)
                    return
        self.send_json({"ok": True})

    def handle_ai_session_delete(self, user: dict, session_id: str) -> None:
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM hatchery_ai_chat_sessions WHERE id = %s AND user_id = %s",
                    (session_id, str(user["id"])),
                )
                if cur.rowcount == 0:
                    self.send_json({"error": "会话不存在或已删除"}, 404)
                    return
        self.send_json({"ok": True})

    def handle_ai_run(self, user: dict) -> None:
        global AI_UPSTREAM_DEGRADED
        prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, mode, chosen_model, chosen_effort, _session_id = self.prepare_ai_run()
        if rate_limit_hit("ai-run-user", str(user["id"]), 20, 600):
            raise ValueError("AI 任务请求过于频繁，请稍后再试")
        busy_job = user_running_ai_job(user["id"])
        if busy_job:
            # 同账号已有任务在跑（可能是另一个页面/设备发起的）：让前端排队等待，避免整站结果互相覆盖
            self.send_json({"error": "已有 AI 任务在进行中，稍后会自动继续", "jobId": busy_job, "busy": True}, 409)
            return
        lock_owner = f"sync-{secrets.token_hex(6)}"
        if not ai_run_slot_acquire(lock_owner):
            raise ValueError("当前同时运行的 AI 任务较多，请稍候再试")
        provider_name, model = current_ai_provider_model()
        model = chosen_model or model
        try:
            result = self.run_ai_harness(user, prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, mode=mode, model_override=chosen_model, effort_override=chosen_effort, job_key=lock_owner)
            record_ai_usage(user["id"], None, prompt, attachments, provider_name, model, result.get("usage"), "completed")
            AI_UPSTREAM_DEGRADED = None
        except Exception:
            run_error = str(sys.exc_info()[1])
            print(f"[ai-run] 同步任务失败：{run_error}", file=sys.stderr, flush=True)
            record_ai_usage(user["id"], None, prompt, attachments, provider_name, model, None, "failed", run_error)
            if "配额已用尽" in run_error:
                AI_UPSTREAM_DEGRADED = {"reason": "quota", "message": run_error[:300], "since": iso_time()}
            raise
        finally:
            ai_run_slot_release(lock_owner)
        self.send_json(result)

    def handle_ai_run_start(self, user: dict) -> None:
        prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, mode, chosen_model, chosen_effort, session_id = self.prepare_ai_run()
        if rate_limit_hit("ai-run-user", str(user["id"]), 20, 600):
            raise ValueError("AI 任务请求过于频繁，请稍后再试")
        busy_job = user_running_ai_job(user["id"])
        if busy_job:
            # 同账号已有任务在跑（可能是另一个页面/设备发起的）：让前端排队等待，避免整站结果互相覆盖
            self.send_json({"error": "已有 AI 任务在进行中，稍后会自动继续", "jobId": busy_job, "busy": True}, 409)
            return
        job_id = f"job-{secrets.token_hex(12)}"
        if not ai_run_slot_acquire(job_id):
            raise ValueError("当前同时运行的 AI 任务较多，请稍候再试")
        now = time.time()
        with AI_RUN_JOBS_LOCK:
            for stale_id in [key for key, value in AI_RUN_JOBS.items() if now - float(value.get("createdAt", now)) > 3600]:
                AI_RUN_JOBS.pop(stale_id, None)
            # sessionId 是"关网页再打开还能找回进度"的关键：任务属于哪个会话；结果在客户端确认（ack）前一直保留
            AI_RUN_JOBS[job_id] = {"userId": str(user["id"]), "sessionId": session_id, "prompt": prompt[:120], "status": "running", "events": [], "createdAt": now, "result": None, "error": None}

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
            global AI_UPSTREAM_DEGRADED
            provider_name, model = current_ai_provider_model()
            model = chosen_model or model
            try:
                result = self.run_ai_harness(user, prompt, context, include_site, attachments, chosen_presets, preset_snippets, history, progress=progress, mode=mode, model_override=chosen_model, effort_override=chosen_effort, job_key=job_id)
                record_ai_usage(user["id"], job_id, prompt, attachments, provider_name, model, result.get("usage"), "completed")
                AI_UPSTREAM_DEGRADED = None
                with AI_RUN_JOBS_LOCK:
                    job = AI_RUN_JOBS.get(job_id)
                    if job and job.get("status") == "running":
                        job.update({"status": "completed", "result": result})
            except Exception as error:
                print(f"[ai-run] {job_id} 失败：{error}", file=sys.stderr, flush=True)
                record_ai_usage(user["id"], job_id, prompt, attachments, provider_name, model, None, "failed", str(error))
                if "配额已用尽" in str(error):
                    AI_UPSTREAM_DEGRADED = {"reason": "quota", "message": str(error)[:300], "since": iso_time()}
                with AI_RUN_JOBS_LOCK:
                    job = AI_RUN_JOBS.get(job_id)
                    if job and job.get("status") == "running":
                        job.update({"status": "failed", "error": str(error)[:1000]})
            finally:
                ai_run_slot_release(job_id)

        try:
            threading.Thread(target=worker, name=f"hatchery-ai-{job_id[-6:]}", daemon=True).start()
        except Exception:
            with AI_RUN_JOBS_LOCK:
                AI_RUN_JOBS.pop(job_id, None)
            ai_run_slot_release(job_id)
            raise
        self.send_json({"jobId": job_id, "status": "running"}, 202)

    def handle_ai_run_status(self, user: dict, parsed) -> None:
        job_id = str((parse_qs(parsed.query).get("id") or [""])[0])
        if not re.fullmatch(r"job-[0-9a-f]{24}", job_id):
            self.send_json({"error": "AI 任务编号无效"}, 400)
            return
        payload = None
        with AI_RUN_JOBS_LOCK:
            job = AI_RUN_JOBS.get(job_id)
            if job and str(job.get("userId", "")) == str(user["id"]):
                payload = {
                    "jobId": job_id,
                    "status": job["status"],
                    "events": [dict(item) for item in job["events"]],
                    "result": job.get("result"),
                    "error": job.get("error"),
                }
        if payload:
            self.send_json(payload)
            return
        # 内存表查无≠失败：已完成任务被任一客户端确认（ack）后即从内存摘除，服务重启也会清空内存表。
        # 先查任务落库的终态，有结论按终态返回；结果 payload 不落库，completed 时 result 为 null，
        # 前端据此静默收尾，不再把"已成功的任务"弹成"未能恢复"。
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, error FROM hatchery_ai_usage WHERE job_id = %s AND user_id = %s",
                    (job_id, str(user["id"])),
                )
                settled = cur.fetchone()
        if settled and settled[0] == "completed":
            self.send_json({"jobId": job_id, "status": "completed", "events": [], "result": None})
            return
        if settled and settled[0] == "failed":
            self.send_json({"jobId": job_id, "status": "failed", "events": [], "error": str(settled[1] or "AI 任务失败")})
            return
        self.send_json({"error": "AI 任务不存在或已过期（可能刚重启过服务）", "status": "failed"}, 404)

    def handle_ai_run_active(self, user: dict) -> None:
        """当前用户的可重挂任务：进行中的 + 已结束但结果还没被任何客户端取走的。
        浏览器关掉再打开、换设备登录，都靠这个接口把进度/结果找回来。"""
        with AI_RUN_JOBS_LOCK:
            jobs = []
            for job_id, job in AI_RUN_JOBS.items():
                if str(job.get("userId", "")) != str(user["id"]):
                    continue
                jobs.append({
                    "jobId": job_id,
                    "sessionId": job.get("sessionId") or "",
                    "prompt": job.get("prompt") or "",
                    "status": job["status"],
                    "events": [dict(item) for item in job["events"]],
                    "result": job.get("result") if job["status"] != "running" else None,
                    "error": job.get("error"),
                    "createdAt": job.get("createdAt"),
                })
            jobs.sort(key=lambda item: float(item.get("createdAt") or 0))
        self.send_json({"jobs": jobs})

    def handle_ai_run_ack(self, user: dict) -> None:
        """客户端确认已取走结果：已结束的任务直接移除释放内存；仍在运行的忽略（结果还没产出，不能标记取走）。"""
        job_id = str(self.read_json().get("jobId", ""))
        if not re.fullmatch(r"job-[0-9a-f]{24}", job_id):
            self.send_json({"error": "AI 任务编号无效"}, 400)
            return
        with AI_RUN_JOBS_LOCK:
            job = AI_RUN_JOBS.get(job_id)
            if not job or str(job.get("userId", "")) != str(user["id"]):
                self.send_json({"ok": True})
                return
            if job["status"] != "running":
                AI_RUN_JOBS.pop(job_id, None)
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
        user = self.require_console_user()
        if not user:
            return
        data = self.read_json()
        slug = str(data.pop("slug", "") or "").strip().lower()
        listed_in_gallery = bool(data.pop("listed", True))
        if not SITE_SLUG_PATTERN.fullmatch(slug):
            raise ValueError("发布路径需为 3–32 位小写字母、数字或短横线，且不能以短横线开头或结尾")
        if slug in RESERVED_SITE_SLUGS:
            raise ValueError("这个发布路径是系统保留字，请换一个")
        data, has_account = self.prepare_site_payload(data)
        user_id = str(user["id"])
        username = str(user["username"])
        old_slug: str | None = None
        try:
            with neon_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT user_id FROM hatchery_published_sites WHERE slug = %s", (slug,))
                    taken = cur.fetchone()
                    if taken and str(taken[0]) != user_id:
                        self.send_json({"error": f"发布路径 “{slug}” 已被占用，请换一个"}, 409)
                        return
                    cur.execute("SELECT slug FROM hatchery_published_sites WHERE user_id = %s", (user_id,))
                    mine = cur.fetchone()
                    old_slug = str(mine[0]) if mine else None
                    if old_slug and old_slug != slug:
                        delete_site_records(cur, old_slug)
                    now = iso_time()
                    if old_slug == slug:
                        cur.execute("UPDATE hatchery_published_sites SET updated_at = %s, listed_in_gallery = %s WHERE slug = %s", (now, listed_in_gallery, slug))
                    else:
                        cur.execute(
                            "INSERT INTO hatchery_published_sites(slug,user_id,created_at,updated_at,listed_in_gallery) VALUES(%s,%s,%s,%s,%s)",
                            (slug, user_id, now, now, listed_in_gallery),
                        )
                    audit_event(cur, user_id, "site.published", {"slug": slug, "replaced": old_slug if old_slug != slug else None})
        except psycopg2.IntegrityError:
            self.send_json({"error": f"发布路径 “{slug}” 已被占用，请换一个"}, 409)
            return
        # 改名换路径：旧页面（目录、论坛等运行数据）整体删除
        if old_slug and old_slug != slug:
            shutil.rmtree(PUBLISHED / old_slug, ignore_errors=True)
        data["username"] = slug
        data["basePath"] = f"/pages/{slug}"
        data["previewMode"] = False
        site_dir = PUBLISHED / slug
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "site.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        site_admin = initialize_site_account(slug, username) if has_account else None
        if has_account:
            record_site_deployment(slug, data, username)
            record_site_audit(slug, username, "site.publish", f"/pages/{slug}", f"{len(data['pages'])} pages")
        with RUNTIME_LOCK:
            self.load_runtime(slug)
        self.send_json({
            "ok": True,
            "url": f"/pages/{slug}",
            "slug": slug,
            "publicUrl": f"https://{slug}.{SITE_BASE_DOMAIN}",
            "accountEnabled": has_account,
            "siteAdmin": site_admin,
            "replacedSlug": old_slug if old_slug and old_slug != slug else None,
        })

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

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
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
        site_host_slug = site_slug_from_host(self.headers.get("Host", ""))
        if site_host_slug and parsed.path not in PUBLIC_STATIC_PATHS and not parsed.path.startswith("/api/"):
            sub_path = "" if parsed.path in ("", "/") else parsed.path
            self.serve_published(f"/pages/{site_host_slug}{sub_path}", site_host_slug)
            return
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
        if parsed.path == "/api/ai/run/active":
            user = self.require_console_user()
            if user:
                self.handle_ai_run_active(user)
            return
        if parsed.path == "/api/ai/status":
            status = harness_status(ROOT)
            status.update({"filePolicy": "site-workspace-only"})
            if AI_UPSTREAM_DEGRADED:
                status["configured"] = True  # Key 和运行时仍在，只是上游额度出了问题
                status["degraded"] = AI_UPSTREAM_DEGRADED
            self.send_json(status)
            return
        if parsed.path == "/api/ai/sessions":
            user = self.require_console_user()
            if not user:
                return
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT id, title, created_at, updated_at
                        FROM hatchery_ai_chat_sessions
                        WHERE user_id = %s AND messages_html <> ''
                        ORDER BY updated_at DESC
                        LIMIT 50
                        """,
                        (str(user["id"]),),
                    )
                    rows = cur.fetchall()
            self.send_json({"sessions": [
                {"id": row["id"], "title": row["title"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
                for row in rows
            ]})
            return
        ai_session_match = re.fullmatch(r"/api/ai/sessions/([0-9a-fA-F-]{32,36})", parsed.path)
        if ai_session_match:
            user = self.require_console_user()
            if not user:
                return
            with neon_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, title, messages_html, created_at, updated_at FROM hatchery_ai_chat_sessions WHERE id = %s AND user_id = %s",
                        (ai_session_match.group(1), str(user["id"])),
                    )
                    row = cur.fetchone()
            if not row:
                self.send_json({"error": "会话不存在或已删除"}, 404)
                return
            self.send_json({"session": {
                "id": row["id"],
                "title": row["title"],
                "messagesHtml": row["messages_html"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }})
            return
        ai_preview_match = re.match(r"^/ai-preview/([A-Za-z0-9_-]{32})(?:/run/([A-Za-z0-9_-]{6,48}))?(?:/|$)", parsed.path)
        if ai_preview_match:
            self.serve_workspace_preview(parsed.path, ai_preview_match.group(1), ai_preview_match.group(2))
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
        pages_match = re.match(r"^/pages/([A-Za-z0-9_-]{3,32})(?:/|$)", parsed.path)
        if pages_match:
            self.serve_published(parsed.path, pages_match.group(1))
            return
        # Hatchery Gallery：收录所有发布的网站（/Gallery 大小写、/gallery/ 尾斜杠同进）
        if parsed.path.lower().rstrip("/") == "/gallery":
            self.serve_gallery()
            return
        if parsed.path == "/":
            self.path = f"/{FRONTEND_DIR}/index.html"
            super().do_GET()
            return
        # GPT 式会话地址：/c/<uuid> 由前端路由接管，统一回控制台首页
        if re.fullmatch(r"/c/[0-9a-fA-F-]{32,36}/?", parsed.path):
            self.path = f"/{FRONTEND_DIR}/index.html"
            super().do_GET()
            return
        if parsed.path in PUBLIC_STATIC_PATHS:
            self.path = f"/{FRONTEND_DIR}{parsed.path}"
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
<header class="top"><h1>Hatchery Gallery</h1><a class="back" href="/">← 回到控制台</a></header>
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
        """AI 工作区实时预览：按随机预览 id 读取对应用户工作区的 site.json，把 pages[].code 直接渲染出来。
        带 run_token 时渲染该任务独立工作区（并行任务各自预览各自的 site.json）。"""
        with neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM hatchery_user_extras WHERE preview_id = %s", (preview_id,))
                row = cur.fetchone()
        if not row:
            self.send_error(404, "Not found")
            return
        if run_token:
            with AI_RUN_JOBS_LOCK:
                entry = AI_RUN_WORKSPACES.get(run_token)
            if not entry or str(entry[0]) != str(row[0]):
                body = "<meta charset='utf-8'><p style='font:14px sans-serif;padding:40px'>该 AI 任务已结束，任务级预览随之关闭。</p>".encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            site_file = entry[1] / "site.json"
        else:
            site_file = ai_site_workspace(row[0]) / "site.json"
        if not site_file.is_file():
            body = "<meta charset='utf-8'><p style='font:14px sans-serif;padding:40px'>工作区还没有内容，先让 AI 做点什么。</p>".encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        state = json.loads(site_file.read_text(encoding="utf-8"))
        pages = []
        for page in state.get("pages", []):
            if not isinstance(page, dict):
                continue
            pages.append({
                "id": page.get("id"),
                "name": page.get("name", "页面"),
                "path": page.get("path", "page"),
                "parentId": page.get("parentId"),
                "kind": page.get("kind", "page"),
                "html": str(page.get("code", "")),
            })
        site = {
            "username": "preview",
            "siteName": state.get("siteName", "未命名网站"),
            "description": state.get("description", ""),
            "theme": state.get("theme", "minimal"),
            "background": state.get("background", "#ffffff"),
            "contentWidth": state.get("contentWidth", 100),
            "pages": pages,
            "forumPosts": [],
            "previewMode": True,
            "previewId": preview_id,
            "basePath": f"/ai-preview/{preview_id}" + (f"/run/{run_token}" if run_token else ""),
        }
        template = (ROOT / FRONTEND_DIR / "viewer.html").read_text(encoding="utf-8")
        site_data = json.dumps(site, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        prefix = f"/ai-preview/{preview_id}" + (f"/run/{run_token}" if run_token else "")
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

    def serve_preview(self, path: str, preview_id: str, data_json: str) -> None:
        template = (ROOT / FRONTEND_DIR / "viewer.html").read_text(encoding="utf-8")
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
            body = f"<meta charset='utf-8'><title>尚未发布</title><p style='font:16px sans-serif;padding:40px'>{site_username}.{SITE_BASE_DOMAIN} 尚未发布网站。</p>".encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        template = (ROOT / FRONTEND_DIR / "viewer.html").read_text(encoding="utf-8")
        site = json.loads(site_file.read_text(encoding="utf-8"))
        site["username"] = site_username
        site["previewMode"] = False
        site["basePath"] = f"/pages/{site_username}"
        site_data = json.dumps(site, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        prefix = f"/pages/{site_username}"
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
