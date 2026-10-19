"""Persistent website directories. Page source is never stored in site.json.

The legacy exporter is a one-time compatibility boundary, not the authoring path.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_held = threading.local()
LOCK_ROOT = Path(__file__).resolve().parent / '.ai-backups' / 'locks'
META_FIELDS = {"format", "siteName", "description", "slug", "url", "createdAt", "updatedAt"}


@contextmanager
def site_lock(user_id, *, blocking=True):
    with _guard:
        lock = _locks.setdefault(str(user_id), threading.RLock())
    if not lock.acquire(blocking=blocking):
        raise ValueError("网站文件正在修改中，请稍后再试")
    descriptor = None
    held = getattr(_held, 'keys', set())
    _held.keys = held
    key = str(user_id)
    try:
        # Also serialize separate service processes during reload/restart.
        if os.name == 'posix' and key not in held:
            import fcntl
            LOCK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = (LOCK_ROOT / (hashlib.sha256(key.encode()).hexdigest() + '.lock')).open('a')
            try:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise ValueError('网站文件正在修改中，请稍后再试') from None
            held.add(key)
        yield
    finally:
        if descriptor:
            descriptor.close()
            held.discard(key)
        lock.release()


def draft_directory(published: Path, user_id) -> Path:
    return published / ".drafts" / hashlib.sha256(str(user_id).encode()).hexdigest()


def confined_path(root: Path, name: str, *, public=False) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError("无效的网站文件路径")
    parts = name.split("/")
    if name.startswith("/") or any(p in ("..", ".", "") for p in parts) or ":" in parts[0]:
        raise ValueError("路径必须位于当前网站目录内")
    # Do not follow symlinks, including an existing workspace root.
    if root.is_symlink():
        raise ValueError("网站目录不能是符号链接")
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("网站文件不能经过符号链接")
    if target.exists() and target.is_file() and target.stat().st_nlink != 1:
        raise ValueError("网站文件不能是硬链接")
    if public and (parts[0] == 'backend' or any(p.startswith(".") and p not in (".hatchery", ".well-known") for p in parts)
                   or name in ("runtime.json", "site.json")):
        raise ValueError("该文件不对外提供")
    return target


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hatchery-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_metadata(root: Path) -> dict:
    path = confined_path(root, "site.json")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("site.json 必须是网站基本信息对象")
    return data


def save_metadata(root: Path, values: dict, *, slug="", url="") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    old = read_metadata(root)
    data = {"format": "files", "siteName": str(values.get("siteName", old.get("siteName", "未命名网站")))[:200],
            "description": str(values.get("description", old.get("description", "")))[:2000],
            "slug": slug, "url": url, "createdAt": old.get("createdAt", now), "updatedAt": now}
    atomic_write(confined_path(root, "site.json"), json.dumps(data, ensure_ascii=False, indent=2).encode())
    return data


def list_files(root: Path) -> list[str]:
    result = []
    if not root.exists():
        return result
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            if name.startswith(".hatchery-write-"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                path = confined_path(root, relative)
                if path.is_file():
                    result.append(relative)
            except (ValueError, FileNotFoundError):
                continue
    return result


def preview_revision(root: Path, names: list[str]) -> str:
    """Cheap change token for the preview; never read large assets to poll changes."""
    digest = hashlib.sha256()
    for name in names:
        try:
            info = confined_path(root, name).stat()
            digest.update(json.dumps([name, info.st_mtime_ns, info.st_size, info.st_ino]).encode())
        except (ValueError, FileNotFoundError):
            continue
    return digest.hexdigest()[:24]


def file_manifest(root: Path) -> dict[str, str]:
    result = {}
    for name in list_files(root):
        try:
            with confined_path(root, name).open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError:
            continue
    return result


def page_files(root: Path) -> list[str]:
    return sorted((name for name in list_files(root) if name.lower().endswith((".html", ".htm"))),
                  key=lambda name: (name != "index.html", name))


def account_enabled(root: Path) -> bool:
    return any(re.search(r'class=[\"\'][^\"\']*\baccount-block\b', confined_path(root, name).read_text(encoding="utf-8", errors="replace"))
               for name in page_files(root))


def resolve_public_file(root: Path, relative: str) -> Path:
    name = relative.strip("/") or "index.html"
    path = confined_path(root, name, public=True)
    if path.is_dir():
        path = confined_path(root, name + "/index.html", public=True)
    elif not path.exists() and not Path(name).suffix:
        candidate = confined_path(root, name + ".html", public=True)
        if candidate.is_file():
            path = candidate
        elif name.startswith("forum/") and (root / ".hatchery/legacy.js").is_file():
            path = confined_path(root, "forum/index.html", public=True)
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def initialize_directory(root: Path, frontend: Path, backups: Path, legacy: dict | None = None):
    """Export existing JSON once, preserving a private backup before touching it."""
    with site_lock("directory:" + str(root.absolute())):
        _initialize_directory(root, frontend, backups, legacy)


def _initialize_directory(root: Path, frontend: Path, backups: Path, legacy: dict | None):
    root.mkdir(parents=True, exist_ok=True)
    data = read_metadata(root)
    if data.get("format") == "files":
        return
    legacy = data if isinstance(data.get("pages"), list) else (legacy or {})
    pages = [p for p in legacy.get("pages", []) if isinstance(p, dict)]
    if not pages:
        if not (root / "index.html").exists():
            atomic_write(confined_path(root, "index.html"),
                         '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>我的网站</title><body><main><h1>我的网站</h1><p>在左侧告诉 AI 你想制作什么。</p></main></body></html>'.encode())
        save_metadata(root, legacy or data, slug=str(data.get("slug", "")), url=str(data.get("url", "")))
        return
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / (hashlib.sha256(str(root).encode()).hexdigest()[:16] + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f"))
    shutil.copytree(root, backup, symlinks=True)
    backup.chmod(0o700)
    atomic_write(backup / "legacy-source.json", json.dumps(legacy, ensure_ascii=False).encode())
    routes = []
    for i, page in enumerate(pages):
        route = str(page.get("path", "page")).strip("/")
        if i == 0:
            route = ""
        filename = f"{route}/index.html" if route else "index.html"
        confined_path(root, filename)
        routes.append({"id": page.get("id", str(i)), "name": page.get("name", "页面"), "path": route, "filename": filename})
    if len({r["filename"] for r in routes}) != len(routes):
        raise ValueError("旧网站有重复页面路径，迁移已暂停，原始数据已备份")
    has_account = any('account-block' in str(p.get("html", p.get("code", ""))) for p in pages)
    meta = {k: legacy.get(k) for k in ("siteName", "description", "theme", "background", "contentWidth")}
    meta.update(pages=[{k: v for k, v in r.items() if k != "filename"} for r in routes], features={"account": has_account})
    template = (frontend / "viewer.html").read_text()
    script = (frontend / "viewer.js").read_text()
    script = script.replace("site.basePath||", "site.basePath??")
    script = script.replace('return page===site.pages[0]?siteBase:', 'return page===site.pages[0]?(siteBase+"/"):')
    script = script.replace('location.href=siteBase;', 'location.href=siteBase||"/";')
    script = script.replace("root.innerHTML=isAdminRoute?", "if(isAdminRoute||isForumRoute)root.innerHTML=isAdminRoute?")
    # Preview documents have an opaque sandbox origin and cannot access cookies/storage.
    script = script.replace("if(navigation?.type==='reload')sessionStorage.removeItem(previewSessionKey);", "try{if(navigation?.type==='reload')sessionStorage.removeItem(previewSessionKey)}catch{}")
    script = script.replace("function savePreviewRuntime(){if(isPreviewMode&&previewRuntime)sessionStorage.setItem(previewSessionKey,JSON.stringify(previewRuntime))}", "function savePreviewRuntime(){try{if(isPreviewMode&&previewRuntime)sessionStorage.setItem(previewSessionKey,JSON.stringify(previewRuntime))}catch{}}")
    outputs = {".hatchery/legacy.js": script.encode()}
    for name in ("styles.css", "motion.css"):
        outputs[f".hatchery/{name}"] = (frontend / name).read_bytes()
    extra = []
    if has_account:
        extra = [{"path": route, "filename": f"{route}/index.html"} for route in ("forum", "admin") if route not in {r["path"] for r in routes}]
    for i, route in enumerate(routes + extra):
        content = str(pages[i].get("html", pages[i].get("code", ""))) if i < len(pages) else ""
        depth = len(PurePosixPath(route["filename"]).parts) - 1
        assets = "../" * depth + ".hatchery/"
        def import_attachment(match):
            user, attachment_id, filename = match.groups()
            filename = unquote(filename)
            try:
                original = confined_path(frontend.parent / "tmp/ai-attachments", f"{user}/{attachment_id}/{filename}")
                if not original.is_file():
                    return match.group(0)
                destination = f"assets/imported/{attachment_id}/{filename}"
                confined_path(root, destination)
                outputs[destination] = original.read_bytes()
                return "../" * depth + quote(destination, safe="/")
            except (ValueError, OSError):
                return match.group(0)
        content = re.sub(r"/attachments/ai/([A-Za-z0-9_-]+)/([0-9a-f]{16})/([^\"'<>\s)]+)", import_attachment, content)
        # Only metadata is embedded; the actual page markup lives in the HTML body.
        bootstrap = "const m=location.pathname.match(/^\\/(pages|preview|ai-preview)\\/([^/]+)/);const s=window.__SITE_DATA__;s.previewMode=!!m&&m[1]!=='pages';s.previewId=s.previewMode?m[2]:'';s.username=m&&m[1]==='pages'?m[2]:location.hostname.split('.')[0];s.basePath=m?m[0]:'';window.__PAGE_PATH__=decodeURI(location.pathname.slice((s.basePath||'').length)).replace(/^\\/+|\\/+$/g,'').replace(/(?:^|\\/)index\\.html$/,'');"
        doc = template.replace('<main class="published-site" id="publishedSite"></main>', '<main class="published-site" id="publishedSite">' + content + '</main>')
        doc = doc.replace("__SITE_DATA_JSON__", json.dumps(meta, ensure_ascii=False).replace("</", "<\\/"))
        doc = doc.replace("__PAGE_PATH_JSON__", json.dumps(route["path"]))
        doc = doc.replace('<script src="/viewer.js', '<script>' + bootstrap + '</script><script src="' + assets + 'legacy.js')
        for name in ("styles.css", "motion.css"):
            doc = doc.replace('href="/' + name, 'href="' + assets + name)
        doc = doc.replace("临时预览", "网站预览").replace("此页面使用随机预览地址；交互数据仅保留在本次浏览会话中，刷新即清空。", "正在预览网站文件。成员账号和社区功能请在发布后的独立站点使用。")
        outputs[route["filename"]] = doc.encode()
        if i == 0:
            alias = str(pages[0].get("path", "")).strip("/")
            if alias and alias not in {r["path"] for r in routes + extra}:
                alias_file = alias + "/index.html"
                confined_path(root, alias_file)
                home = "../" * (len(PurePosixPath(alias_file).parts) - 1)
                outputs[alias_file] = f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={home}"><a href="{home}">首页</a>'.encode()
    for name, payload in outputs.items():
        target = confined_path(root, name)
        if target.exists() and target.read_bytes() != payload:
            raise ValueError(f"迁移会覆盖已有文件 {name}，已停止；旧数据已备份")
    for name, payload in outputs.items():
        atomic_write(confined_path(root, name), payload)
    if not (root / "runtime.json").exists():
        atomic_write(confined_path(root, "runtime.json"), json.dumps({"forumPosts": legacy.get("forumPosts", []), "registrations": {}, "joinedUsers": []}, ensure_ascii=False).encode())
    save_metadata(root, legacy, slug=str(data.get("username", "")), url=str(data.get("url", "")))
