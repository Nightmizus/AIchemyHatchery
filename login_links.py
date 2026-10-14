"""Operator-issued, expiring, single-use console login links.

Issuance is available only to trusted server-side callers. Public routes redeem
an existing bearer token; they cannot select a user or issue another link.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets


ORIGIN = "https://aichemy.club"
PAGE_PATH = "/auth/login-link"
API_PATH = "/api/auth/login-link"
SESSION_SECONDS = 2 * 60 * 60
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
SCHEMA = '''
CREATE TABLE IF NOT EXISTS hatchery_login_links (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    session_hash TEXT,
    reason TEXT NOT NULL
)
'''


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _audit(cur, user_id, event, detail):
    cur.execute(
        "INSERT INTO hatchery_audit_events(user_id,event,detail_json,created_at) VALUES(%s,%s,%s,%s)",
        (user_id, event, json.dumps(detail, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )


def issue(conn, user_id, reason, ttl_seconds=900):
    """Return a raw token once; the caller must commit before delivering it."""
    if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 900:
        raise ValueError("Login links must expire within 1–15 minutes")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("An authorization reason is required")
    with conn.cursor() as cur:
        cur.execute('SELECT id FROM "User" WHERE id=%s AND "bannedUntil" IS NULL FOR SHARE', (user_id,))
        if not cur.fetchone():
            raise ValueError("Account is unavailable")
        token = secrets.token_urlsafe(32)
        digest = token_hash(token)
        cur.execute(
            "INSERT INTO hatchery_login_links(token_hash,user_id,expires_at,reason) "
            "VALUES(%s,%s,now()+%s*interval '1 second',%s) RETURNING expires_at",
            (digest, user_id, ttl_seconds, reason.strip()),
        )
        expires_at = cur.fetchone()[0]
        _audit(cur, user_id, "auth.login_link.issued", {"link_hash": digest, "reason": reason.strip()})
    return {"token": token, "expires_at": expires_at, "url": f"{ORIGIN}{PAGE_PATH}#{token}"}


def redeem(conn, token, ip_address="", user_agent=""):
    """Consume the link and create a session in the same transaction."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        return None
    digest = token_hash(token)
    with conn.cursor() as cur:
        # Row locks make concurrent redemption single-use and prevent a ban from
        # racing with session creation. Failed session/audit writes roll back all.
        cur.execute(
            '''SELECT l.user_id FROM hatchery_login_links l JOIN "User" u ON u.id=l.user_id
               WHERE l.token_hash=%s AND l.consumed_at IS NULL AND l.expires_at>clock_timestamp()
                 AND u."bannedUntil" IS NULL
               FOR UPDATE OF l FOR SHARE OF u''',
            (digest,),
        )
        row = cur.fetchone()
        if not row:
            return None
        user_id = row[0]
        session_token = secrets.token_urlsafe(32)
        session_hash = token_hash(session_token)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=SESSION_SECONDS)
        cur.execute(
            "UPDATE hatchery_login_links SET consumed_at=clock_timestamp(),session_hash=%s "
            "WHERE token_hash=%s AND consumed_at IS NULL AND expires_at>clock_timestamp()",
            (session_hash, digest),
        )
        if cur.rowcount != 1:
            return None
        cur.execute(
            "INSERT INTO console_sessions(token_hash,user_id,created_at,expires_at,last_seen_at,ip_address,user_agent) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (session_hash, user_id, now.isoformat(), expires.isoformat(), now.isoformat(),
             str(ip_address)[:100], str(user_agent)[:300]),
        )
        _audit(cur, user_id, "auth.login_link.redeemed", {"link_hash": digest, "session_hash": session_hash})
    return {"user_id": user_id, "cookie":
            f"alchemy_hatchery_console_session={session_token}; Path=/; Max-Age={SESSION_SECONDS}; "
            "HttpOnly; Secure; SameSite=Strict"}


def _trusted_https(handler):
    return (handler.request_host() == "aichemy.club"
            and handler.headers.get("X-Forwarded-Proto", "").strip().lower() == "https")


def handle_redeem(handler, database):
    if (not _trusted_https(handler)
            or handler.headers.get("Origin", "") != ORIGIN
            or handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"):
        handler.send_json({"error": "请从 HTTPS 授权链接页面登录"}, 403)
        return
    payload = handler.read_json(1024)
    if not isinstance(payload, dict):
        handler.send_json({"error": "链接无效"}, 400)
        return
    if handler.login_is_limited():
        handler.send_json({"error": "尝试次数过多，请稍后重试"}, 429)
        return
    with database() as conn:
        result = redeem(conn, payload.get("token"),
                        handler.headers.get("X-Real-IP", handler.client_address[0]),
                        handler.headers.get("User-Agent", ""))
    if result is None:
        handler.record_login_failure()
        handler.send_json({"error": "链接已过期、已使用或不可用，请申请新的授权链接"}, 410)
        return
    handler.send_json({"ok": True, "redirect": "/hatchery"}, headers={"Set-Cookie": result["cookie"]})


def serve_page(handler):
    if not _trusted_https(handler):
        handler.send_json({"error": "请使用原始 HTTPS 授权链接"}, 403)
        return
    nonce = secrets.token_urlsafe(18)
    body = PAGE.replace("__NONCE__", nonce).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Security-Policy",
                        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    handler.end_headers()
    handler.wfile.write(body)


PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>授权登录 · Hatchery</title>
<style>body{margin:0;min-height:100dvh;display:grid;place-items:center;background:#101714;color:#e8ece5;
font:16px/1.7 system-ui,sans-serif}main{box-sizing:border-box;width:min(420px,90vw);padding:32px;
border:1px solid #39463a;border-radius:14px;background:#19211b}h1{font-size:25px;margin:0 0 10px}
p{color:#b5c1b3}button{width:100%;border:0;border-radius:8px;padding:13px;background:#d9eaa9;
color:#1a2316;font:inherit;font-weight:600;cursor:pointer}button:disabled{opacity:.5;cursor:default}
</style></head><body><main><h1>授权登录</h1><p id="message" role="status">点击继续，进入 Hatchery。</p>
<button id="continue" type="button" disabled>继续登录</button></main>
<script nonce="__NONCE__">
(() => {
  let token = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  const message = document.querySelector('#message');
  const button = document.querySelector('#continue');
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    message.textContent = '链接无效，请打开完整的授权链接。'; return;
  }
  button.disabled = false;
  button.addEventListener('click', async () => {
    button.disabled = true;
    message.textContent = '正在登录…';
    try {
      const response = await fetch('/api/auth/login-link', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', referrerPolicy: 'no-referrer',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '登录失败，请重试。');
      token = '';
      location.replace('/hatchery');
    } catch (error) {
      message.textContent = error.message || '连接失败，请重新打开授权链接。';
      button.disabled = false;
    }
  });
})();
</script></body></html>'''
