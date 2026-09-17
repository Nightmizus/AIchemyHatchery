"""AI 多会话并发隔离测试服务器：假 DB + 假登录 + 可控的假 AI 任务引擎。

复现与验证「两个会话同时发消息」的并发行为。不连 Neon、不调用真实 LLM：
/api/ai/run 收到请求后注册任务，线程在 delayMs 后完成，返回一个确定性的
siteReplace（往 pages[0].code 追加带 prompt 的标记 section），便于断言
任务是否都生效、以及后一个任务的 context 是否包含前一个任务的结果。

用法：
    .venv/bin/python scripts/ai_concurrency_harness.py --port 4182

控制接口：
    GET  /__test/login?as=tester|other|admin     身份 cookie
    POST /__test/ai/config   {"delayMs":700,"fail":false,"serial":true}
                             serial=true 镜像修复后服务端的“同用户单任务”守卫
    GET  /__test/ai/jobs     每个任务的 prompt / 状态 / 启动时拿到的 pages[0].code
    GET  /__test/state       假库（会话、草稿）与任务摘要
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg2  # noqa: E402

import server as srv  # noqa: E402

TEST_USERS = {
    "admin": {"id": "u-admin", "name": "admin", "campusId": srv.SITE_OWNER_CAMPUS_ID, "isAdmin": True},
    "tester": {"id": "u1", "name": "tester", "campusId": "20260001", "isAdmin": False},
    "other": {"id": "u2", "name": "other", "campusId": "20260002", "isAdmin": False},
}


class FakeCursor:
    def __init__(self):
        self.rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        text = " ".join(str(sql).split())
        self.rows = []
        if text == "SELECT 1":
            self.rows = [{"one": 1}]
        # 其余读语句一律空结果，写语句视为成功（本 harness 不测这些表）

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def cursor(self, *args, **kwargs):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def fake_neon_db():
    yield FakeConn()


srv.neon_db = fake_neon_db

LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}      # id -> {id,userId,title,html,createdAt,updatedAt}
DRAFTS: dict[str, str] = {}         # userId -> 草稿 JSON 字符串
DRAFT_SAVES: list[dict] = []        # 每次草稿保存的 {userId, pages0}
AI_JOB_META: dict[str, dict] = {}   # jobId -> {prompt, context, contextPage0Code, createdAt, completedAt}
AI_CONFIG = {"delayMs": 700, "fail": False, "serial": True}


def now_iso():
    return srv.iso_time()


def fake_ai_result(meta):
    """确定性的 siteReplace：往 pages[0].code 末尾追加带 prompt 的标记。"""
    site = json.loads(json.dumps(meta["context"].get("site") or {}))
    pages = site.get("pages") or []
    if pages:
        marker = f'<section class="ai-task-marker" data-task="{meta["prompt"]}">任务 {meta["prompt"]} 已应用</section>'
        pages[0]["code"] = f"{pages[0].get('code') or ''}\n{marker}"
    return {
        "runId": meta["jobId"],
        "summary": f"已按「{meta['prompt']}」完成修改并验证。",
        "mode": "full",
        "siteReplace": site,
        "siteOperations": [],
        "changedFiles": [],
        "restartRequired": False,
        "undoAvailable": False,
    }


class TestHandler(srv.AIchemyHatcheryHandler):
    def log_message(self, fmt, *args):
        pass

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

    # ---------- 测试控制 ----------
    def _serve_test(self, parsed):
        if parsed.path == "/__test/login":
            who = parse_qs(parsed.query).get("as", ["none"])[0]
            self.send_response(302)
            self.send_header("Set-Cookie", f"test_user={who}; Path=/; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return True
        if parsed.path == "/__test/reset":
            with LOCK:
                SESSIONS.clear()
                DRAFTS.clear()
                DRAFT_SAVES.clear()
                AI_JOB_META.clear()
                with srv.AI_RUN_JOBS_LOCK:
                    srv.AI_RUN_JOBS.clear()
                AI_CONFIG.update({"delayMs": 700, "fail": False, "serial": True})
            self.send_json({"ok": True})
            return True
        if parsed.path == "/__test/ai/config":
            data = self.read_json()
            with LOCK:
                AI_CONFIG.update({key: value for key, value in data.items() if key in AI_CONFIG})
            self.send_json({"ok": True, "config": dict(AI_CONFIG)})
            return True
        if parsed.path == "/__test/ai/jobs":
            # 从 meta 读（任务被客户端 ack 后会从 AI_RUN_JOBS 移除，这里仍能看到全history）
            with LOCK:
                jobs = [{"jobId": meta["jobId"], "prompt": meta["prompt"], "status": meta.get("status", "running"),
                         "contextPage0Code": meta["contextPage0Code"], "createdAt": meta["createdAt"],
                         "completedAt": meta.get("completedAt")}
                        for meta in AI_JOB_META.values()]
            jobs.sort(key=lambda item: item["createdAt"])
            self.send_json({"jobs": jobs})
            return True
        if parsed.path == "/__test/state":
            with LOCK:
                payload = {
                    "sessions": [{"id": s["id"], "userId": s["userId"], "title": s["title"], "html": s["html"]} for s in SESSIONS.values()],
                    "drafts": DRAFTS,
                    "draftSaves": DRAFT_SAVES,
                    "config": dict(AI_CONFIG),
                    "jobs": [{"jobId": jid, "prompt": m["prompt"], "status": (srv.AI_RUN_JOBS.get(jid) or {}).get("status", "acked")}
                             for jid, m in AI_JOB_META.items()],
                }
            self.send_json(payload)
            return True
        return False

    # ---------- 假 AI 引擎与会话/草稿 API ----------
    def _serve_api(self, parsed, method):
        path = parsed.path
        if path == "/api/ai/run" and method == "POST" and (parse_qs(parsed.query).get("async") or [""])[0] == "1":
            user = self.require_console_user()
            if not user:
                return True
            body = self.read_json()
            prompt = str(body.get("prompt") or "")
            if not prompt:
                self.send_json({"error": "缺少任务描述"}, 400)
                return True
            # serial=true 镜像修复后服务端的“同账号单任务”守卫（user_running_ai_job）
            if AI_CONFIG["serial"]:
                busy = srv.user_running_ai_job(user["id"])
                if busy:
                    self.send_json({"error": "已有 AI 任务在进行中，稍后会自动继续", "jobId": busy, "busy": True}, 409)
                    return True
            job_id = f"job-{secrets.token_hex(12)}"
            site = body.get("context") or {}
            pages = (site.get("site") or {}).get("pages") or []
            meta = {"jobId": job_id, "prompt": prompt, "context": site,
                    "contextPage0Code": str((pages[0] or {}).get("code") or "") if pages else "",
                    "createdAt": time.time(), "completedAt": None}
            with LOCK:
                AI_JOB_META[job_id] = meta
                # 注册进真实的 AI_RUN_JOBS：/api/ai/run/status、/active、/ack 走服务端真实处理器
                with srv.AI_RUN_JOBS_LOCK:
                    srv.AI_RUN_JOBS[job_id] = {"userId": str(user["id"]), "sessionId": str(body.get("sessionId") or ""),
                                               "prompt": prompt[:120], "status": "running", "events": [], "createdAt": meta["createdAt"],
                                               "result": None, "error": None}
                delay_ms = AI_CONFIG["delayMs"]
                fail = AI_CONFIG["fail"]

            def worker():
                time.sleep(max(0.05, delay_ms / 1000))
                with LOCK:
                    if job_id not in AI_JOB_META:
                        return
                    if fail:
                        with srv.AI_RUN_JOBS_LOCK:
                            job = srv.AI_RUN_JOBS.get(job_id)
                            if job and job.get("status") == "running":
                                job.update({"status": "failed", "error": "模拟的上游失败"})
                        AI_JOB_META[job_id]["completedAt"] = time.time()
                        AI_JOB_META[job_id]["status"] = "failed"
                        return
                    result = fake_ai_result(AI_JOB_META[job_id])
                    AI_JOB_META[job_id]["completedAt"] = time.time()
                    AI_JOB_META[job_id]["status"] = "completed"
                    with srv.AI_RUN_JOBS_LOCK:
                        job = srv.AI_RUN_JOBS.get(job_id)
                        if job and job.get("status") == "running":
                            job.update({"status": "completed", "result": result})

            threading.Thread(target=worker, daemon=True).start()
            self.send_json({"jobId": job_id, "status": "running"}, 202)
            return True
        if path == "/api/ai/status" and method == "GET":
            self.send_json({"configured": True, "models": [{"id": "k3", "name": "k3"}], "efforts": [],
                            "model": "k3", "reasoningEffort": ""})
            return True
        if path == "/api/ai/undo" and method == "POST":
            if not self.require_console_user():
                return True
            self.send_json({"ok": True, "restoredFiles": [], "restartRequired": False})
            return True
        if path == "/api/server/restart" and method == "POST":
            if not self.require_console_user():
                return True
            self.send_json({"ok": True})
            return True
        if path == "/api/console/draft":
            user = self.require_console_user()
            if not user:
                return True
            if method == "GET":
                raw = DRAFTS.get(str(user["id"]))
                self.send_json({"draft": json.loads(raw) if raw else None})
                return True
            if method == "POST":
                data = self.read_json()
                if not isinstance(data.get("pages"), list):
                    self.send_json({"error": "草稿缺少页面数据"}, 400)
                    return True
                with LOCK:
                    DRAFTS[str(user["id"])] = json.dumps(data, ensure_ascii=False)
                    DRAFT_SAVES.append({"userId": str(user["id"]), "pages0": str((data.get("pages") or [{}])[0].get("code") or ""), "at": time.time()})
                self.send_json({"ok": True})
                return True
        if path == "/api/ai/sessions":
            user = self.require_console_user()
            if not user:
                return True
            if method == "GET":
                with LOCK:
                    rows = [dict(s) for s in SESSIONS.values() if s["userId"] == str(user["id"]) and s["html"]]
                rows.sort(key=lambda item: item["updatedAt"], reverse=True)
                self.send_json({"sessions": [{"id": r["id"], "title": r["title"], "createdAt": r["createdAt"], "updatedAt": r["updatedAt"]} for r in rows[:50]]})
                return True
            if method == "POST":
                payload = self.read_json()
                requested = str(payload.get("id") or "").lower()
                if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", requested):
                    self.send_json({"error": "会话编号格式无效"}, 400)
                    return True
                with LOCK:
                    existing = SESSIONS.get(requested)
                    if existing and existing["userId"] != str(user["id"]):
                        self.send_json({"error": "会话编号冲突，请刷新后重试"}, 409)
                        return True
                    SESSIONS[requested] = {"id": requested, "userId": str(user["id"]),
                                           "title": str(payload.get("title") or "新聊天")[:60],
                                           "html": str(payload.get("messagesHtml") or ""),
                                           "createdAt": existing["createdAt"] if existing else now_iso(), "updatedAt": now_iso()}
                self.send_json({"session": {"id": requested, "title": SESSIONS[requested]["title"], "createdAt": SESSIONS[requested]["createdAt"], "updatedAt": SESSIONS[requested]["updatedAt"]}})
                return True
        match = re.fullmatch(r"/api/ai/sessions/([0-9a-fA-F-]{32,36})(/delete)?", path)
        if match:
            user = self.require_console_user()
            if not user:
                return True
            session_id, is_delete = match.group(1).lower(), bool(match.group(2))
            with LOCK:
                session = SESSIONS.get(session_id)
                owned = bool(session) and session["userId"] == str(user["id"])
                if method == "GET" and not is_delete:
                    if not owned:
                        self.send_json({"error": "会话不存在或已删除"}, 404)
                        return True
                    self.send_json({"session": {"id": session["id"], "title": session["title"], "messagesHtml": session["html"],
                                                "createdAt": session["createdAt"], "updatedAt": session["updatedAt"]}})
                    return True
                if not owned:
                    self.send_json({"error": "会话不存在或已删除"}, 404)
                    return True
                if is_delete:
                    SESSIONS.pop(session_id)
                    self.send_json({"ok": True})
                else:
                    payload = self.read_json()
                    if "title" in payload and str(payload.get("title") or "").strip():
                        session["title"] = str(payload["title"])[:60]
                    if "messagesHtml" in payload:
                        session["html"] = str(payload["messagesHtml"] or "")
                    session["updatedAt"] = now_iso()
                    self.send_json({"ok": True})
            return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        if self._serve_test(parsed):
            return
        if self._serve_api(parsed, "GET"):
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if self._serve_test(parsed):
            return
        if self._serve_api(parsed, "POST"):
            return
        super().do_POST()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4182)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="hatchery-ai-concurrency-"))
    srv.PUBLISHED = tmp / "published"
    srv.PUBLISHED.mkdir()
    os.chdir(srv.ROOT)
    print(f"ai concurrency harness: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), TestHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
