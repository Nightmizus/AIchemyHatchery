"""Hatchery Gallery 隔离测试服务器：用内存假数据库 + 假登录 + 临时 published 目录跑真实 handler。

不连 Neon、不写共享库、不碰仓库里的 published/，专门用来端到端验证 /gallery、
发布收录开关、管理员站点总览这条链路。

用法：
    .venv/bin/python scripts/gallery_test_harness.py --port 4181

测试控制接口（仅本进程）：
    GET  /__test/login?as=admin|tester|other|none   写入身份 cookie 后跳回控制台
    POST /__test/seed     {"reset":true,"sites":[...],"files":[...]}   写入发布记录 / site.json
    POST /__test/reset    清空假库与临时目录
    GET  /__test/state    查看假库内容与最近执行的 SQL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg2  # noqa: E402  服务本身依赖它，这里只借用异常类型

import server as srv  # noqa: E402

TEST_USERS = {
    "admin": {"id": "u-admin", "name": "admin", "campusId": srv.SITE_OWNER_CAMPUS_ID, "isAdmin": True, "email": "admin@test.local"},
    "tester": {"id": "u1", "name": "tester", "campusId": "20260001", "isAdmin": False, "email": "tester@test.local"},
    "other": {"id": "u2", "name": "other", "campusId": "20260002", "isAdmin": False, "email": "other@test.local"},
}


class Row(dict):
    """既能按列名取（RealDictCursor 风格），也能按下标取（普通 cursor 风格）。"""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class FakeDB:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sites: dict[str, dict] = {}
        self.statements: list[tuple[str, tuple]] = []

    def user(self, user_id: str) -> dict:
        return next((u for u in TEST_USERS.values() if u["id"] == user_id), {})


DB = FakeDB()


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(str(sql).split())
        self.db.statements.append((text, tuple(params or ())))
        self.rows = []
        sites = self.db.sites
        if text == "SELECT 1":
            self.rows = [Row(one=1)]
        elif text.startswith("SELECT column_name FROM information_schema.columns"):
            self.rows = [Row(column_name=name) for name in ("id", "name", "campusId", "email", "realName", "isAdmin", "createdAt", "bannedUntil")]
        elif text == "SELECT user_id FROM hatchery_published_sites WHERE slug = %s":
            site = sites.get(params[0])
            self.rows = [Row(user_id=site["user_id"])] if site else []
        elif text == "SELECT slug FROM hatchery_published_sites WHERE user_id = %s":
            self.rows = [Row(slug=s["slug"]) for s in sites.values() if s["user_id"] == params[0]]
        elif text == "SELECT slug, listed_in_gallery FROM hatchery_published_sites WHERE user_id = %s":
            self.rows = [Row(slug=s["slug"], listed_in_gallery=s["listed_in_gallery"]) for s in sites.values() if s["user_id"] == params[0]]
        elif text.startswith("INSERT INTO hatchery_published_sites("):
            slug, user_id, created_at, updated_at, listed = params
            if slug in sites:
                raise psycopg2.IntegrityError("duplicate key value violates unique constraint")
            sites[slug] = {"slug": slug, "user_id": user_id, "created_at": created_at, "updated_at": updated_at, "listed_in_gallery": bool(listed)}
        elif text.startswith("UPDATE hatchery_published_sites SET updated_at = %s, listed_in_gallery = %s WHERE slug = %s"):
            updated_at, listed, slug = params
            if slug in sites:
                sites[slug].update(updated_at=updated_at, listed_in_gallery=bool(listed))
        elif text == "DELETE FROM hatchery_published_sites WHERE slug = %s":
            sites.pop(params[0], None)
        elif text.startswith("SELECT p.slug") and "FROM hatchery_published_sites p" in text:
            listed_only = "WHERE p.listed_in_gallery" in text
            joined = []
            for site in sites.values():
                if listed_only and not site["listed_in_gallery"]:
                    continue
                owner = self.db.user(site["user_id"])
                joined.append(Row(slug=site["slug"], created_at=site["created_at"], updated_at=site["updated_at"],
                                  listed_in_gallery=site["listed_in_gallery"], owner_name=owner.get("name"), owner_campus_id=owner.get("campusId")))
            joined.sort(key=lambda r: str(r["updated_at"]), reverse=True)
            self.rows = joined
        # 其余读语句返回空，写语句当作成功；全部留在 statements 里供断言

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def cursor(self, *args, **kwargs):
        return FakeCursor(DB)

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def fake_neon_db():
    yield FakeConn()


srv.neon_db = fake_neon_db


class TestHandler(srv.AIchemyHatcheryHandler):
    def log_message(self, fmt, *args):  # 安静一点
        pass

    # 身份：cookie test_user 或请求头 X-Test-User
    def console_user(self):
        name = self.headers.get("X-Test-User") or ""
        if not name:
            cookie = self.headers.get("Cookie", "")
            match = re.search(r"(?:^|;\s*)test_user=([a-z]+)", cookie)
            name = match.group(1) if match else ""
        user = TEST_USERS.get(name)
        if not user:
            return None
        row = dict(user)
        row["username"] = row["name"]
        row["role"] = "admin" if row.get("isAdmin") else "user"
        row["status"] = "active"
        return row

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/__test/login":
            who = parse_qs(parsed.query).get("as", ["none"])[0]
            self.send_response(302)
            self.send_header("Set-Cookie", f"test_user={who}; Path=/; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if parsed.path == "/__test/state":
            self.send_json({"sites": list(DB.sites.values()), "published": sorted(p.name for p in srv.PUBLISHED.iterdir()),
                            "statements": DB.statements[-40:]})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/__test/reset":
            reset_all()
            self.send_json({"ok": True})
            return
        if parsed.path == "/__test/seed":
            data = self.read_json()
            if data.get("reset"):
                reset_all()
            for site in data.get("sites", []):
                DB.sites[site["slug"]] = {
                    "slug": site["slug"], "user_id": site.get("user_id", "u1"),
                    "created_at": site.get("created_at", "2026-09-01T00:00:00+00:00"),
                    "updated_at": site.get("updated_at", "2026-09-01T00:00:00+00:00"),
                    "listed_in_gallery": bool(site.get("listed", True)),
                }
            for item in data.get("files", []):
                site_dir = srv.PUBLISHED / item["slug"]
                site_dir.mkdir(parents=True, exist_ok=True)
                if "raw" in item:
                    (site_dir / "site.json").write_text(item["raw"], encoding="utf-8")
                else:
                    payload = {"siteName": item.get("siteName", ""), "description": item.get("description", ""),
                               "pages": item.get("pages", [{"id": "page-1", "name": "首页", "path": "home", "html": "<h1>hi</h1>"}])}
                    if item.get("padding"):
                        payload["padding"] = "x" * int(item["padding"])
                    (site_dir / "site.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                if item.get("mtime"):
                    os.utime(site_dir / "site.json", (item["mtime"], item["mtime"]))
            DB.statements.clear()
            self.send_json({"ok": True, "sites": len(DB.sites), "published": len(list(srv.PUBLISHED.iterdir()))})
            return
        super().do_POST()


def reset_all() -> None:
    DB.reset()
    for entry in srv.PUBLISHED.iterdir():
        shutil.rmtree(entry, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4181)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="hatchery-gallery-test-"))
    srv.PUBLISHED = tmp / "published"
    srv.PUBLISHED.mkdir()
    os.chdir(srv.ROOT)
    print(f"gallery test harness: http://127.0.0.1:{args.port}  published={srv.PUBLISHED}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), TestHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
