"""Durable AI requests and server-owned conversation history.

Each account has one persistent website-building conversation. Security audits
use task-specific temporary sessions, deleted on completion; their report/job
records remain independently available for status, retries and report display.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import sys
import traceback
import uuid
import os
import copy
from pathlib import Path
from html.parser import HTMLParser
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor
from job_events import EventHub


def conversation_id(user_id) -> str:
    """该账号唯一的会话 id。

    确定性派生：同一用户永远得到同一个 id，因此"首次对话自动激活、
    之后持续复用"不需要任何建表或状态迁移。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hatchery-conversation:{user_id}"))


def is_security(payload) -> bool:
    return payload.get('kind') == 'security' or payload.get('mode') == 'security'


def security_session_id(user_id, request_id) -> str:
    return 'security-' + uuid.uuid5(uuid.NAMESPACE_URL, f'hatchery-security:{user_id}:{request_id}').hex


def _chat_jobs(alias=''):
    # Constants only, never a client-supplied SQL fragment. Both markers cover
    # older audit jobs that were attached to the website-building conversation.
    return (f"{alias}payload_json::jsonb->>'kind' IS DISTINCT FROM 'security' AND "
            f"{alias}payload_json::jsonb->>'mode' IS DISTINCT FROM 'security'")


class Conflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


STOP_MESSAGE = '已停止本次任务；已保存的文件保留。已提交的后台操作可能仍在收尾，请核对后再继续。'


class JobCancelled(RuntimeError):
    def __init__(self):
        super().__init__(STOP_MESSAGE)


def is_stop_request(payload):
    # Only standalone controls. “停止网站服务” and quoted instructions are tasks.
    text = re.sub(r'[\s，。！？,.!?]+', '', payload.get('prompt', '')).lower()
    return (not is_security(payload) and not payload.get('attachments') and text in {
        '停', '停停', '停停停', '停止', '停止生成', '停止任务', '暂停', '暂停生成',
        '先停一下', '停一下', '等一下', '等一下我要添加文件', '先等等我要上传文件',
        'stop', 'cancel', '/stop', '/cancel'})


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
ALTER TABLE hatchery_ai_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false;
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
            "kind": payload.get("kind", "chat"),
            "cancelRequested": bool(row.get('cancel_requested')),
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
        chunks.append(f'<article class="ai-message user" data-ai-request-id="{esc(job["requestId"])}" data-ai-job-message="{esc(jid)}"><div><p>{esc(job["prompt"])}</p><div class="ai-message-files">{files}</div></div></article>')
        usage = result.get('contextUsage')
        if not usage:
            usage = next((e.get('contextUsage') for e in reversed(job.get('events', [])) if e.get('contextUsage')), None)
        usage_attr = f' data-context-usage="{esc(dumps(usage))}"' if usage else ''
        card_id = ' id="aiRunCard"' if latest else ''
        chunks.append(f'<section class="ai-run-output ai-run-archive"{card_id} {attributes}{usage_attr}>')
        if status in ('queued', 'running'):
            label = '已在服务器排队，关闭页面后仍会继续' if status == 'queued' else 'AI 正在处理请求'
            if job.get('cancelRequested'):
                label = '正在停止，等待执行进程退出；已保存的文件会保留'
            chunks.append(f'<p class="ai-working-status" role="status">{label}</p>')
            disabled = ' disabled' if job.get('cancelRequested') else ''
            control = '正在停止…' if disabled else ('取消排队' if status == 'queued' else '停止任务')
            chunks.append(f'<button type="button" data-ai-cancel="{esc(jid)}"{disabled}>{control}</button>')
        events = ''.join(f'<p data-ai-event-id="{esc(e.get("id"))}" class="ai-run-tool {"failed" if e.get("status")=="failed" else ""}"><b>{esc(e.get("label"))}</b> {esc(e.get("detail"))}</p>'
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
        if plan and status == 'completed':
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
        self.events = EventHub()
        self.wake = threading.Event()
        self._live_lock = threading.Lock()
        self._live_progress = {}
        self._cancel_signals = {}

    def signal_cancellation(self, rows):
        with self._live_lock:
            for row in rows:
                signal = self._cancel_signals.get(row['id'])
                if signal is not None:
                    signal.set()
        self.wake.set()
        for row in rows:
            self.emit_state(row)

    def poll_cancellations(self, ids):
        if not ids:
            return
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT id FROM hatchery_ai_jobs WHERE id=ANY(%s) AND cancel_requested', (list(ids),))
            rows = cur.fetchall()
        # Polling covers another process accepting a cancellation; do not emit
        # duplicate state each second. Local acceptance signals immediately.
        with self._live_lock:
            for row in rows:
                signal = self._cancel_signals.get(row['id'])
                if signal is not None:
                    signal.set()

    @staticmethod
    def _cancel_rows(cur, uid, job_id=None):
        cur.execute(f'''UPDATE hatchery_ai_jobs SET cancel_requested=true,
            status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
            finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
            error=CASE WHEN status='queued' THEN %s ELSE error END,updated_at=now()
            WHERE user_id=%s AND status IN ('queued','running') AND NOT cancel_requested
                AND ({_chat_jobs()}) {'AND id=%s' if job_id else ''} RETURNING *''',
            ('已取消排队，本任务未开始执行。', str(uid), job_id) if job_id else ('已取消排队，本任务未开始执行。', str(uid)))
        return cur.fetchall()

    def cancel(self, user_id, job_id):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('LOCK TABLE hatchery_ai_jobs IN SHARE ROW EXCLUSIVE MODE')
            rows = self._cancel_rows(cur, user_id, job_id)
            cur.execute(f'SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND id=%s AND ({_chat_jobs()})',
                        (str(user_id), job_id))
            row = cur.fetchone()
        self.signal_cancellation(rows)
        return self._public_job(row) if row else None

    def _public_job(self, row):
        job = public_job(row)
        with self._live_lock:
            live = self._live_progress.get(row['id'])
            if live and live['user_id'] == str(row['user_id']) and row['status'] == 'running':
                job['events'] = copy.deepcopy(live['events'])
                if datetime.fromisoformat(live['updated_at']) > datetime.fromisoformat(job['updatedAt']):
                    job['updatedAt'] = live['updated_at']
        return job

    def emit_state(self, row):
        try: self._emit_state(row)
        except Exception as exc:
            # A best-effort downlink must never strand an already committed job.
            print(f'[ai-live] 状态等待 HTTP 校准：{type(exc).__name__}', file=sys.stderr)

    def _emit_state(self, row):
        job = self._public_job(row)
        if is_security(job): return
        # The live projection doesn't duplicate file trees / attachment payloads.
        if job['result']:
            job['result'] = {k:v for k,v in job['result'].items() if k in ('summary','plan','contextUsage','changedFiles','mode')}
        self.events.publish(row['user_id'], 'job', {'job':job,'html':render_history('', [job])})

    def publish_progress(self, row, event, events):
        now = datetime.now(timezone.utc).isoformat()
        with self._live_lock:
            self._live_progress[row['id']] = {'user_id':str(row['user_id']), 'events':copy.deepcopy(events), 'updated_at':now}
        if not is_security(json.loads(row['payload_json'])):
            try: self.events.publish(row['user_id'], 'progress', {'jobId':row['id'],'event':event,'updatedAt':now})
            except Exception as exc:
                print(f'[ai-live] 进度等待 HTTP 校准：{type(exc).__name__}', file=sys.stderr)

    def initialize(self):
        with self.db() as conn, conn.cursor() as cur:
            for statement in DDL.split(';'):
                if statement.strip():
                    cur.execute(statement)

    def enqueue(self, user, payload, request_id, limit=32):
        """Server binds chat to the account and audits to one temporary session."""
        uid = str(user['id'])
        audit = is_security(payload)
        session_id = security_session_id(uid, request_id) if audit else conversation_id(uid)
        job_id = 'job-' + uuid.uuid4().hex[:24]
        stopping = is_stop_request(payload)
        cancelled_rows = []
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Serializes acceptance, claiming, and queue limits across threads/processes.
            cur.execute('LOCK TABLE hatchery_ai_jobs IN SHARE ROW EXCLUSIVE MODE')
            cur.execute('SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND request_id=%s', (uid, request_id))
            previous = cur.fetchone()
            if previous:
                valid_sessions = {session_id, conversation_id(uid)} if audit else {session_id}
                if previous['session_id'] not in valid_sessions or json.loads(previous['payload_json']) != payload:
                    raise Conflict('同一请求编号不能用于不同内容')
                return public_job(previous)
            cur.execute("SELECT count(*) AS n FROM hatchery_ai_jobs WHERE user_id=%s AND status IN ('queued','running')", (uid,))
            if cur.fetchone()['n'] >= limit and not stopping:
                raise QueueFull('等待中的任务已达上限，请等待现有任务结束后再发送')
            now = datetime.now(timezone.utc).isoformat()
            cur.execute('INSERT INTO hatchery_ai_chat_sessions(id,user_id,title,messages_html,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                        (session_id, uid, payload['prompt'][:60], '', now, now))
            cur.execute('INSERT INTO hatchery_ai_jobs(id,user_id,session_id,request_id,payload_json) VALUES(%s,%s,%s,%s,%s) RETURNING *',
                        (job_id, uid, session_id, request_id, dumps(payload)))
            row = cur.fetchone()
            if stopping:
                # Bypass FIFO and cancel only work preceding this control.
                # Never cancel tasks submitted after it or another account's audit.
                cur.execute("UPDATE hatchery_ai_jobs SET status='completed',started_at=now(),finished_at=now() WHERE id=%s", (job_id,))
                cancelled_rows = self._cancel_rows(cur, uid)
                message = ('已请求停止此前的任务，并取消此前的排队请求；等待运行进程退出，已保存的文件保留。'
                           if cancelled_rows else '停止请求已处理；如有任务正在停止，请等待其收尾。')
                cur.execute('UPDATE hatchery_ai_jobs SET result_json=%s WHERE id=%s RETURNING *',
                            (dumps({'summary': message, 'changedFiles': []}), job_id))
                row = cur.fetchone()
            cur.execute('UPDATE hatchery_ai_chat_sessions SET updated_at=%s, revision=revision+1 WHERE id=%s', (now, session_id))
        self.signal_cancellation(cancelled_rows)
        self.wake.set()
        self.emit_state(row)
        return public_job(row)

    @staticmethod
    def _delete_security_session(cur, user_id, session_id):
        # Never delete the building conversation, including when finishing a
        # legacy audit that was incorrectly stored in that conversation.
        if not re.fullmatch(r'security-[0-9a-f]{32}', session_id):
            return
        cur.execute(f'''DELETE FROM hatchery_ai_chat_sessions s WHERE s.id=%s AND s.user_id=%s
            AND NOT EXISTS (SELECT 1 FROM hatchery_ai_jobs j WHERE j.session_id=s.id
                AND j.user_id=s.user_id AND (j.status IN ('queued','running') OR ({_chat_jobs('j.')})))''',
                    (session_id, str(user_id)))

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
            claimed = dict(cur.fetchone())
        self.emit_state(claimed)
        return claimed

    def renew(self, ids):
        """续租。返回未能续上的 id，调用方据此判断租约是否真的丢了。

        旧实现只看 UPDATE 的 WHERE 是否匹配，一旦数据库往返失败或连接池
        取连接超时，异常被上层吞掉、租约静默停在旧值，下一轮 recover_expired
        立刻把仍在正常运行的任务判为中断。
        """
        if not ids:
            return set()
        try:
            with self.db() as conn, conn.cursor() as cur:
                cur.execute("UPDATE hatchery_ai_jobs SET lease_until=now()+interval '60 seconds' "
                            "WHERE id=ANY(%s) AND status='running' RETURNING id", (list(ids),))
                renewed = {row[0] for row in cur.fetchall()}
            return set(ids) - renewed
        except Exception:
            # 续租失败时保守返回全部 id：宁可多给一轮宽限，也不要误判中断。
            return set(ids)

    def recover_expired(self):
        """把确实失去租约的任务标记为中断。

        宽限期必须显著大于续租周期（10 秒），否则单次续租失败 + 一轮调度
        抖动就会把健康任务判死。这里要求过期超过 5 分钟才认定真的丢了。
        """
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE hatchery_ai_jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'interrupted' END,finished_at=now(),updated_at=now(),
                error=CASE WHEN cancel_requested THEN '停止请求已保留，执行进程失联；请核对已保存文件与后台操作。' ELSE '服务中断，任务未完成；为避免重复执行，未自动重发。已保存的文件仍保留。' END
                WHERE status='running' AND lease_until < now() - interval '5 minutes'
                RETURNING id,user_id,session_id,payload_json""")
            rows = cur.fetchall()
            for row in rows:
                if is_security(json.loads(row[3])):
                    self._delete_security_session(cur, row[1], row[2])
            return [row[0] for row in rows]

    def progress(self, job_id, events):
        with self.db() as conn, conn.cursor() as cur:
            # Bound locks/queries so a progress writer cannot hold finalization
            # indefinitely. SET LOCAL doesn't leak into pooled connections.
            cur.execute("SET LOCAL statement_timeout='10s'; SET LOCAL lock_timeout='5s'")
            cur.execute("UPDATE hatchery_ai_jobs SET events_json=%s,updated_at=now() WHERE id=%s AND status='running'", (dumps(events), job_id))

    def finish(self, job_id, result=None, error=None):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""UPDATE hatchery_ai_jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE %s END,
                result_json=%s,error=CASE WHEN cancel_requested THEN %s ELSE %s END,finished_at=now(),updated_at=now(),
                lease_until=NULL WHERE id=%s AND status IN ('running','interrupted') RETURNING *""",
                        ('failed' if error else 'completed', dumps(result) if result is not None else None, STOP_MESSAGE, error, job_id))
            row = cur.fetchone()
            if row and is_security(json.loads(row['payload_json'])):
                self._delete_security_session(cur, row['user_id'], row['session_id'])
            elif row:
                cur.execute('UPDATE hatchery_ai_chat_sessions SET revision=revision+1,updated_at=%s WHERE id=%s',
                            (datetime.now(timezone.utc).isoformat(), row['session_id']))
        with self._live_lock: self._live_progress.pop(job_id, None)
        if row: self.emit_state(row)
        self.wake.set()

    def get(self, user_id, job_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND id=%s', (str(user_id), job_id))
            row = cur.fetchone()
            return self._public_job(row) if row else None

    def active(self, user_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND ({_chat_jobs()}) AND (status IN ('queued','running') OR NOT acknowledged) ORDER BY seq", (str(user_id),))
            return [self._public_job(r) for r in cur.fetchall()]

    def latest_security(self, user_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND payload_json::jsonb->>'kind'='security' ORDER BY seq DESC LIMIT 1", (str(user_id),))
            row = cur.fetchone()
            return self._public_job(row) if row else None

    def queue_status(self, user_id, max_parallel):
        """Explain waiting without exposing another account's IDs or messages."""
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f'''WITH first_waiting AS (
                SELECT min(seq) AS seq FROM hatchery_ai_jobs WHERE user_id=%s AND status='queued' AND ({_chat_jobs()})
            ) SELECT count(*) FILTER (WHERE status='running') AS running,
                count(*) FILTER (WHERE status='queued') AS queued,
                count(*) FILTER (WHERE status='running' AND user_id=%s) AS own_running,
                count(*) FILTER (WHERE status='queued' AND user_id=%s AND ({_chat_jobs()})) AS own_queued,
                count(*) FILTER (WHERE status='queued' AND seq < (SELECT seq FROM first_waiting)) AS ahead
                FROM hatchery_ai_jobs''', (str(user_id), str(user_id), str(user_id)))
            row = cur.fetchone()
        limit = max(1, int(max_parallel))
        reason = ('same_account' if row['own_running'] else
                  'capacity' if row['running'] >= limit else 'dispatching') if row['own_queued'] else ''
        return {'running': row['running'], 'queued': row['queued'], 'maxParallel': limit,
                'ownQueued': row['own_queued'], 'ahead': row['ahead'], 'reason': reason}

    def ack(self, user_id, job_id):
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_ai_jobs SET acknowledged=true WHERE user_id=%s AND id=%s AND status NOT IN ('queued','running')", (str(user_id), job_id))

    def session(self, user_id, session_id):
        with self.read_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # One statement gives history and jobs the same snapshot without
            # three sequential round trips to the remote database.
            cur.execute(f'''SELECT s.*, COALESCE((
                SELECT json_agg(j ORDER BY j.seq) FROM hatchery_ai_jobs j
                WHERE j.session_id=s.id AND j.user_id=s.user_id AND ({_chat_jobs('j.')})
            ), '[]'::json) AS jobs
                FROM hatchery_ai_chat_sessions s WHERE s.id=%s AND s.user_id=%s''',
                        (session_id, str(user_id)))
            row = cur.fetchone()
            if not row:
                return None
            jobs = [self._public_job(job) for job in row['jobs']]
        return {'id': row['id'], 'title': row['title'], 'messagesHtml': render_history(row['messages_html'], jobs),
                'revision': row['revision'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'jobs': jobs,
                'serverOwned': True}

    def context(self, user_id, session_id, before_seq):
        with self.db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT messages_html FROM hatchery_ai_chat_sessions WHERE user_id=%s AND id=%s', (str(user_id), session_id))
            session = cur.fetchone()
            cur.execute(f"SELECT * FROM hatchery_ai_jobs WHERE user_id=%s AND session_id=%s AND seq<%s AND ({_chat_jobs()}) ORDER BY seq DESC LIMIT 12",
                        (str(user_id), session_id, before_seq))
            rows = list(reversed(cur.fetchall()))
        parser = LegacyContext()
        parser.feed(session['messages_html'] if session else '')
        history = parser.entries[-12:]
        for row in rows:
            job = public_job(row)
            history.append({'role': 'user', 'text': job['prompt']})
            if job['status'] == 'cancelled':
                history.append({'role': 'assistant', 'text': job.get('error') or STOP_MESSAGE})
            elif job['result']:
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

    def parallel_limit(self):
        return max(1, int(self.max_parallel() if callable(self.max_parallel) else self.max_parallel))

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='hatchery-ai-dispatch', daemon=True)
        self.thread.start()

    def shutdown(self, grace_seconds: float = 20.0) -> int:
        """停止调度并等待在途任务收尾。

        worker 是 daemon 线程，进程退出时会被直接杀死，结果来不及写库；
        新进程随后按过期租约把任务判为中断。重启前留出收尾时间可以避免
        这类"部署即中断"。返回仍在运行、未能及时结束的任务数。
        """
        self.stop.set()
        self.store.wake.set()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            with self.lock:
                if not self.running:
                    return 0
            time.sleep(0.25)
        with self.lock:
            return len(self.running)

    def loop(self):
        last_renew = 0
        while not self.stop.is_set():
            self.store.wake.clear()
            try:
                with self.lock:
                    running = set(self.running)
                try:
                    self.store.poll_cancellations(running)
                except Exception as exc:
                    print(f'[ai-dispatch] 停止状态暂未同步：{type(exc).__name__}', file=sys.stderr)
                if time.monotonic()-last_renew >= 10:
                    lost = self.store.renew(running)
                    if lost:
                        # 续租没能确认时不推进 last_renew，下一轮立刻重试。
                        print(f'[ai-dispatch] 租约续期未确认：{len(lost)} 个任务', file=sys.stderr)
                    else:
                        last_renew = time.monotonic()
                    self.flush_results()
                    self.store.recover_expired()
                limit = self.parallel_limit()
                while len(running) < limit:
                    job = self.store.claim(limit)
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
            self.store.wake.wait(1 if running else 5)

    def flush_results(self):
        if not self.spool:
            return
        for path in self.spool.glob('job-*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('events'), list) or not {'result', 'error'} <= data.keys():
                    raise ValueError('Invalid result record')
                self.store.progress(path.stem, data['events'])
                self.store.finish(path.stem, data['result'], data['error'])
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # Another dispatcher may have completed this atomic record.
            except (ValueError, KeyError, TypeError):
                # Preserve the corrupt record for inspection, but never let one
                # unreadable spool file stop every account from being scheduled.
                path.replace(path.with_name(path.name + '.' + uuid.uuid4().hex + '.invalid'))
                print('[ai-dispatch] 已隔离损坏的结果记录', file=sys.stderr)
            except Exception as exc:
                print(f'[ai-dispatch] 结果待补写：{type(exc).__name__}', file=sys.stderr)

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
        cancelled = threading.Event()
        with self.store._live_lock:
            self.store._cancel_signals[job['id']] = cancelled

        def check_cancelled():
            if cancelled.is_set():
                raise JobCancelled()
        event_lock = threading.Lock()
        dirty, finished = threading.Event(), threading.Event()

        def save_progress():
            while not finished.is_set():
                dirty.wait(0.8)
                if finished.is_set(): break
                if not dirty.is_set(): continue
                # Coalesce bursts; a remote DB round trip never blocks the RPC reader.
                if finished.wait(0.25): break
                with event_lock:
                    snapshot = copy.deepcopy(events)
                    dirty.clear()
                try: self.store.progress(job['id'], snapshot)
                except Exception: dirty.set()
                finished.wait(0.55)

        writer = threading.Thread(target=save_progress, name='hatchery-progress-'+job['id'][-6:], daemon=True)
        writer.start()

        def progress(event):
            check_cancelled()
            event = dict(event)
            with event_lock:
                existing = next((e for e in events if e.get('id') == event.get('id')), None)
                if existing is not None: existing.update(event)
                else:
                    event['sequence'] = len(events)+1
                    events.append(event)
                self.store.publish_progress(job, event, events)
                dirty.set()

        progress.check_cancelled = check_cancelled
        result = error = None
        try:
            # Covers acceptance racing with worker registration or a restart.
            self.store.poll_cancellations({job['id']})
            check_cancelled()
            result = self.execute(job, progress)
            check_cancelled()
        except JobCancelled as exc:
            error = str(exc)
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '任务执行失败，请稍后重试'
            # Correlatable diagnostics without request bodies, SQL, credentials
            # or exception messages from an external dependency.
            frames = ' > '.join(f'{Path(frame.filename).name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__)[-5:])
            print(f'[ai-job-error] job={job["id"]} type={type(exc).__name__} frames={frames}', file=sys.stderr)
        finally:
            finished.set()
            dirty.set()
            writer.join()
        for event in events:
            if event.get('status') == 'running':
                if event.get('tool'):
                    event.update(status='interrupted', detail=str(event.get('detail') or '') + ' · 任务已结束，此步骤结果未确认')
                else:
                    event['status'] = 'failed' if error else 'done'
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
            with self.store._live_lock:
                self.store._cancel_signals.pop(job['id'], None)
            with self.lock:
                self.running.discard(job['id'])
