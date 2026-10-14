"""Durable AI requests and server-owned conversation history.

Single-conversation model: every account has exactly one conversation, derived
deterministically from its user id. There is no list, rename, or delete — a
browser submits idempotent requests and reads its own history, nothing more.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import sys
import uuid
import os
from pathlib import Path
from html.parser import HTMLParser
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor


# 空闲超过这个时长，dsh 磁盘会话被回收；下一条消息重新开会话。
IDLE_SESSION_SECONDS = 10 * 60


def conversation_id(user_id) -> str:
    """该账号唯一的会话 id。

    确定性派生：同一用户永远得到同一个 id，因此"首次对话自动激活、
    之后持续复用"不需要任何建表或状态迁移。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hatchery-conversation:{user_id}"))


class Conflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


DDL = """
ALTER TABLE hatchery_ai_chat_sessions ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS hatchery_ai_jobs (
    id TEXT PRIMARY KEY,
    seq BIGSERIAL UNIQUE,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    events_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    acknowledged BOOLEAN NOT NULL DEFAULT false,
    UNIQUE(user_id, request_id)
);
CREATE INDEX IF NOT EXISTS hatchery_ai_jobs_session_idx ON hatchery_ai_jobs(user_id, session_id, seq);
CREATE INDEX IF NOT EXISTS hatchery_ai_jobs_queue_idx ON hatchery_ai_jobs(status, seq);
CREATE UNIQUE INDEX IF NOT EXISTS hatchery_ai_jobs_running_user_idx ON hatchery_ai_jobs(user_id) WHERE status = 'running';
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def public_job(row):
    payload = json.loads(row["payload_json"])
    return {"jobId": row["id"], "requestId": row["request_id"],
            "sessionId": row["session_id"], "prompt": payload["prompt"],
            "mode": payload.get("mode", "full"), "status": row["status"],
            "events": json.loads(row["events_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"], "createdAt": iso(row["created_at"]),
            "startedAt": iso(row["started_at"]), "finishedAt": iso(row["finished_at"]),
            "updatedAt": iso(row["updated_at"]), "sequence": row["seq"],
            "attachments": [{"name": a.get("name", "附件"), "kind": a.get("kind", "text")}
                            for a in payload.get("attachments", [])]}


def render_history(legacy, jobs):
    # Freeze the legacy transcript. In particular, old pagehide handlers must
    # never write this projection back as a replacement for authoritative data.
    legacy = re.sub(r'\s+id=["\'](?:aiRun[^"\']*|aiResult|aiPlanBox)["\']', '', legacy or '')
    legacy = re.sub(r'(<small\b[^>]*class=["\'][^"\']*ai-queue-tag[^"\']*["\'][^>]*>).*?(</small>)',
                    r'\1旧排队记录未保存执行任务，无法自动恢复\2', legacy, flags=re.S)
    legacy = re.sub(r'<div\b[^>]*class=["\'][^"\']*ai-working-status[^"\']*["\'][^>]*>.*?</div>',
                    '<p class="ai-legacy-status">旧任务未保留可靠终态，请核对网站；不会自动重发。</p>', legacy, flags=re.S)
    chunks = [legacy]
    esc = lambda s: html.escape(str(s or ''), quote=True)
    for index, job in enumerate(jobs):
        jid, status = job['jobId'], job['status']
        latest = index == len(jobs)-1
        result = job.get('result') or {}
        attributes = f'data-ai-job-id="{esc(jid)}" data-ai-status="{esc(status)}"'
        files = ''.join(f'<span>{esc(a["name"])}</span>' for a in job.get('attachments', []))
        chunks.append(f'<article class="ai-message user" data-ai-request-id="{esc(job["requestId"])}"><div><p>{esc(job["prompt"])}</p><div class="ai-message-files">{files}</div></div></article>')
        usage = result.get('contextUsage')
        if not usage:
            usage = next((e.get('contextUsage') for e in reversed(job.get('events', [])) if e.get('contextUsage')), None)
        usage_attr = f' data-context-usage="{esc(dumps(usage))}"' if usage else ''
        card_id = ' id="aiRunCard"' if latest else ''
        chunks.append(f'<section class="ai-run-output ai-run-archive"{card_id} {attributes}{usage_attr}>')
        if status in ('queued', 'running'):
            label = '已在服务器排队，关闭页面后仍会继续' if status == 'queued' else 'AI 正在处理请求'
            chunks.append(f'<p class="ai-working-status" role="status">{label}</p>')
        events = ''.join(f'<p class="ai-run-tool {"failed" if e.get("status")=="failed" else ""}"><b>{esc(e.get("label"))}</b> {esc(e.get("detail"))}</p>'
                         for e in job.get('events', []) if e.get('kind') != 'context')
        chunks.append(f'<details class="ai-run-process"><summary>查看过程</summary><div class="ai-run-stream">{events}</div></details>')
        final_id = ' id="aiRunFinal"' if latest else ''
        summary_id = ' id="aiRunSummary"' if latest else ''
        if status == 'completed':
            summary = esc(result.get('summary') or 'AI 请求已完成。')
        elif status not in ('queued', 'running'):
            summary = esc(job.get('error') or '任务未完成，已保存的文件仍保留。')
        else:
            summary = ''
        hidden = ' hidden' if status in ('queued', 'running') else ''
        chunks.append(f'<div class="ai-run-final"{final_id}{hidden}><p{summary_id}>{summary}</p>')
        plan = result.get('plan')
        if plan:
            chunks.append(f'<div class="ai-plan-box"><p>{esc(plan)}</p><button type="button" data-ai-plan-execute>按此计划执行</button></div>')
        for name in result.get('changedFiles', []):
            chunks.append(f'<div class="ai-change">已修改文件：{esc(name)}</div>')
        chunks.append('</div></section>')
    return ''.join(chunks)


class LegacyContext(HTMLParser):
    """Read saved user text and final replies, excluding tool logs and UI labels."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.role = None
        self.block_depth = None
        self.text_depth = None
        self.parts = []
        self.entries = []
        self.run_block = False
        self.final_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('br', 'img', 'input', 'hr', 'meta', 'link', 'source', 'wbr'):
            return
        self.depth += 1
        classes = attrs.get('class', '').split()
        if self.role is None:
            if 'ai-message' in classes and 'ai-greeting' not in classes:
                self.role = 'user' if 'user' in classes else 'assistant'
            elif 'ai-run-output' in classes or 'ai-run-archive' in classes:
                self.role = 'assistant'
                self.run_block = True
            if self.role:
                self.block_depth = self.depth
                self.parts = []
        if 'ai-run-final' in classes:
            self.final_depth = self.depth
        if self.role and tag == 'p' and self.text_depth is None and (not self.run_block or self.final_depth is not None):
            self.text_depth = self.depth

    def handle_endtag(self, tag):
        if tag in ('br', 'img', 'input', 'hr', 'meta', 'link', 'source', 'wbr'):
            return
        if self.text_depth == self.depth:
            self.parts.append('\n')
            self.text_depth = None
        if self.final_depth == self.depth:
            self.final_depth = None
        if self.block_depth == self.depth:
            text = ''.join(self.parts).strip()
            if text:
                self.entries.append({'role': self.role, 'text': text})
            self.role = self.block_depth = None
            self.run_block = False
        self.depth = max(0, self.depth-1)

    def handle_data(self, text):
        if self.role and self.text_depth is not None:
            self.parts.append(text)


class JobStore:
    def __init__(self, db, read_db=None):
        self.db = db
        # 只读查询走 autocommit 连接：省掉远端事务模式下的 BEGIN/COMMIT 两次往返。
        self.read_db = read_db or db

    def initialize(self):
        with self.db() as conn, conn.cursor() as cur:
            for statement in DDL.split(';'):
                if statement.strip():
                    cur.execute(statement)

    def enqueue(self, user, payload, request_id, limit=32):
        """排队一条请求。会话由账号派生，调用方不选会话。"""
        uid = str(user['id'])
        session_id = conversation_id(uid)
        job_id = 'job-' + uuid.uuid4().hex[:24]
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Serializes acceptance, claiming, and queue limits across threads/processes.
            cur.execute('LOCK TABLE hatchery_ai_jobs IN SHARE ROW EXCLUSIVE MODE')
            cur.execute('SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND request_id=%s', (uid, request_id))
            previous = cur.fetchone()
            if previous:
                if previous['session_id'] != session_id or json.loads(previous['payload_json']) != payload:
                    raise Conflict('同一请求编号不能用于不同内容')
                return public_job(previous)
            cur.execute("SELECT count(*) AS n FROM hatchery_ai_jobs WHERE user_id=%s AND status IN ('queued','running')", (uid,))
            if cur.fetchone()['n'] >= limit:
                raise QueueFull('等待中的任务已达上限，请等待现有任务结束后再发送')
            now = datetime.now(timezone.utc).isoformat()
            cur.execute('INSERT INTO hatchery_ai_chat_sessions(id,user_id,title,messages_html,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                        (session_id, uid, payload['prompt'][:60], '', now, now))
            cur.execute('INSERT INTO hatchery_ai_jobs(id,user_id,session_id,request_id,payload_json) VALUES(%s,%s,%s,%s,%s) RETURNING *',
                        (job_id, uid, session_id, request_id, dumps(payload)))
            row = cur.fetchone()
            cur.execute('UPDATE hatchery_ai_chat_sessions SET updated_at=%s, revision=revision+1 WHERE id=%s', (now, session_id))
            return public_job(row)

    def claim(self, max_parallel):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('LOCK TABLE hatchery_ai_jobs IN SHARE ROW EXCLUSIVE MODE')
            cur.execute("SELECT count(*) AS n FROM hatchery_ai_jobs WHERE status='running'")
            if cur.fetchone()['n'] >= max_parallel:
                return None
            cur.execute("""SELECT j.* FROM hatchery_ai_jobs j WHERE j.status='queued'
                AND NOT EXISTS (SELECT 1 FROM hatchery_ai_jobs r WHERE r.user_id=j.user_id AND r.status='running')
                ORDER BY j.seq LIMIT 1 FOR UPDATE""")
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("UPDATE hatchery_ai_jobs SET status='running',started_at=now(),updated_at=now(),lease_until=now()+interval '60 seconds' WHERE id=%s RETURNING *", (row['id'],))
            return dict(cur.fetchone())

    def renew(self, ids):
        if not ids:
            return
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET lease_until=now()+interval '60 seconds' WHERE id=ANY(%s) AND status='running'", (list(ids),))

    def recover_expired(self):
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE hatchery_ai_jobs SET status='interrupted',finished_at=now(),updated_at=now(),
                error='服务中断，任务未完成；为避免重复执行，未自动重发。已保存的文件仍保留。'
                WHERE status='running' AND lease_until < now()""")

    def progress(self, job_id, events):
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET events_json=%s,updated_at=now() WHERE id=%s AND status='running'", (dumps(events), job_id))

    def finish(self, job_id, result=None, error=None):
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE hatchery_ai_jobs SET status=%s,result_json=%s,error=%s,finished_at=now(),updated_at=now(),
                lease_until=NULL WHERE id=%s AND status IN ('running','interrupted') RETURNING session_id""",
                        ('failed' if error else 'completed', dumps(result) if result is not None else None, error, job_id))
            row = cur.fetchone()
            if row:
                cur.execute('UPDATE hatchery_ai_chat_sessions SET revision=revision+1,updated_at=%s WHERE id=%s',
                            (datetime.now(timezone.utc).isoformat(), row[0]))

    def get(self, user_id, job_id):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND id=%s', (str(user_id), job_id))
            row = cur.fetchone()
            return public_job(row) if row else None

    def active(self, user_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND (status IN ('queued','running') OR NOT acknowledged) ORDER BY seq", (str(user_id),))
            return [public_job(r) for r in cur.fetchall()]

    def ack(self, user_id, job_id):
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET acknowledged=true WHERE user_id=%s AND id=%s AND status NOT IN ('queued','running')", (str(user_id), job_id))

    def session(self, user_id, session_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # One statement gives history and jobs the same snapshot without
            # three sequential round trips to the remote database.
            cur.execute('''SELECT s.*, COALESCE((
                SELECT json_agg(j ORDER BY j.seq) FROM hatchery_ai_jobs j
                WHERE j.session_id=s.id AND j.user_id=s.user_id
            ), '[]'::json) AS jobs
                FROM hatchery_ai_chat_sessions s WHERE s.id=%s AND s.user_id=%s''',
                        (session_id, str(user_id)))
            row = cur.fetchone()
            if not row:
                return None
            jobs = [public_job(job) for job in row['jobs']]
        return {'id': row['id'], 'title': row['title'], 'messagesHtml': render_history(row['messages_html'], jobs),
                'revision': row['revision'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'jobs': jobs,
                'serverOwned': True}

    def context(self, user_id, session_id, before_seq):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT messages_html FROM hatchery_ai_chat_sessions WHERE user_id=%s AND id=%s', (str(user_id), session_id))
            session = cur.fetchone()
            cur.execute("SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND session_id=%s AND seq<%s ORDER BY seq DESC LIMIT 12",
                        (str(user_id), session_id, before_seq))
            rows = list(reversed(cur.fetchall()))
        parser = LegacyContext()
        parser.feed(session['messages_html'] if session else '')
        history = parser.entries[-12:]
        for row in rows:
            job = public_job(row)
            history.append({'role': 'user', 'text': job['prompt']})
            if job['result']:
                history.append({'role': 'assistant', 'text': job['result'].get('summary', '') + ('\n'+job['result']['plan'] if job['result'].get('plan') else '')})
        return history


class Dispatcher:
    """Claim FIFO work durably; a lost lease is never silently replayed."""
    def __init__(self, store, execute, max_parallel, spool=None):
        self.store, self.execute, self.max_parallel = store, execute, max_parallel
        self.spool = Path(spool) if spool else None
        if self.spool:
            self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.running = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='hatchery-ai-dispatch', daemon=True)
        self.thread.start()

    def loop(self):
        last_renew = 0
        while not self.stop.is_set():
            try:
                with self.lock:
                    running = set(self.running)
                if time.monotonic()-last_renew >= 10:
                    self.store.renew(running)
                    self.flush_results()
                    self.store.recover_expired()
                    last_renew = time.monotonic()
                while len(running) < self.max_parallel:
                    job = self.store.claim(self.max_parallel)
                    if not job:
                        break
                    with self.lock:
                        self.running.add(job['id'])
                    running.add(job['id'])
                    try:
                        threading.Thread(target=self.worker, args=(job,), name='hatchery-ai-'+job['id'][-6:], daemon=True).start()
                    except Exception:
                        with self.lock:
                            self.running.discard(job['id'])
                        self.store.finish(job['id'], error='任务执行线程未能启动，请重试')
                        raise
            except Exception as exc:
                # Never print request data, database credentials, or raw DB diagnostics.
                print(f'[ai-dispatch] 存储暂不可用：{type(exc).__name__}', file=sys.stderr)
            self.stop.wait(1)

    def flush_results(self):
        if not self.spool:
            return
        for path in self.spool.glob('job-*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                self.store.progress(path.stem, data['events'])
                self.store.finish(path.stem, data['result'], data['error'])
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # Another dispatcher may have completed this atomic record.

    def save_result(self, job_id, data):
        if not self.spool:
            return
        temporary = self.spool / (job_id + '.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            stream.write(dumps(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.spool / (job_id + '.json'))

    def worker(self, job):
        events = []
        last_save = 0

        def progress(event):
            nonlocal last_save
            event = dict(event)
            existing = next((e for e in events if e.get('id') == event.get('id')), None)
            if existing is not None:
                existing.update(event)
            else:
                event['sequence'] = len(events)+1
                events.append(event)
            if time.monotonic()-last_save >= 0.8 or event.get('status') in ('done','failed'):
                try:
                    self.store.progress(job['id'], events)
                    last_save = time.monotonic()
                except Exception:
                    pass  # A progress-save outage cannot cancel a model execution.

        result = error = None
        try:
            result = self.execute(job, progress)
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '任务执行失败，请稍后重试'
        try:
            try:
                self.save_result(job['id'], {'result': result, 'error': error, 'events': events})
            except OSError as exc:
                print(f'[ai-dispatch] 本地结果保存失败：{type(exc).__name__}', file=sys.stderr)
            # Retry only result persistence, never the model call or its file effects.
            while True:
                try:
                    self.store.progress(job['id'], events)
                    self.store.finish(job['id'], result, error)
                    if self.spool:
                        (self.spool / (job['id'] + '.json')).unlink(missing_ok=True)
                    break
                except Exception as exc:
                    print(f'[ai-dispatch] 保存结果稍后重试：{type(exc).__name__}', file=sys.stderr)
                    if self.stop.wait(2):
                        break
        finally:
            with self.lock:
                self.running.discard(job['id'])
