"""发布流程冒烟测试：登录 → 发布 → 占用冲突 → 改名删旧 → 账号系统 → 清理。

用法（需要本地服务与 PGlite 已启动，且存在 pubtest1/pubtest2 两个控制台账号）：
    python scripts/publish-test.py
"""
import json, os, subprocess, sys, tempfile

BASE = "http://127.0.0.1:4180"
JAR_DIR = tempfile.mkdtemp(prefix="publish-test-")


def cleanup():
    """删除测试用户占用的发布路径与目录（本地环境直接清库）。"""
    if "127.0.0.1" not in BASE and "localhost" not in BASE:
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import server as srv
        with srv.neon_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slug FROM hatchery_published_sites ps JOIN \"User\" u ON u.id::text = ps.user_id WHERE u.name LIKE 'pubtest%'"
                )
                for (slug,) in cur.fetchall():
                    srv.delete_site_records(cur, slug)
                    print(f"已清理发布记录：{slug}")
        for name in ("alpha-one", "beta-two", "a--b"):
            site_dir = srv.PUBLISHED / name
            if site_dir.exists():
                import shutil
                shutil.rmtree(site_dir)
                print(f"已删除目录：published/{name}")
    except Exception as exc:
        print(f"清理失败（可手工删除）：{exc}", file=sys.stderr)


cleanup()  # 先清掉上次运行可能留下的占用

for jar, name in ((os.path.join(JAR_DIR, "u1.jar"), "pubtest1"), (os.path.join(JAR_DIR, "u2.jar"), "pubtest2")):
    login = subprocess.run(
        ["curl", "-s", "-c", jar, "-X", "POST", BASE + "/api/auth/login",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"username": name, "password": "testpass123"})],
        capture_output=True).stdout.decode("utf-8", errors="replace")
    assert '"ok": true' in login, f"{name} 登录失败：{login[:200]}"

def site_payload(slug, account=False):
    html = '<section class="hero"><h1>Hello</h1></section>'
    if account:
        html += '<section class="account-block"><div class="account-panel" data-login-label="登录账号"></div></section>'
    return {
        "slug": slug,
        "username": "pubtest1",
        "siteName": "Test Site",
        "description": "publish flow test",
        "theme": {},
        "background": "#fff",
        "contentWidth": 960,
        "pages": [{"id": "p1", "name": "首页", "path": "", "parentId": None, "kind": "page", "html": html}],
        "forumPosts": [],
    }

def call(jar, method, path, body=None):
    cmd = ["curl", "-s", "-b", jar, "-X", method, BASE + path]
    tmp = None
    if body is not None:
        fd, tmp = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        cmd += ["-H", "Content-Type: application/json", "-d", "@" + tmp]
    try:
        out = subprocess.run(cmd, capture_output=True).stdout.decode("utf-8", errors="replace")
    finally:
        if tmp:
            os.unlink(tmp)
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:300]}

def http_code(jar, path):
    out = subprocess.run(["curl", "-s", "-b", jar, "-o", "/dev/null", "-w", "%{http_code}", BASE + path], capture_output=True).stdout.decode()
    return out

passed, failed = [], []
def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail and not cond else ""))

# 1. user1 publishes alpha-one
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload("alpha-one"))
check("发布 alpha-one", r.get("ok") and r.get("url") == "/pages/alpha-one" and r.get("publicUrl") == "https://alpha-one.hatchery.mizusumi.com", str(r)[:200])

# 2. /pages/alpha-one serves the site
check("GET /pages/alpha-one → 200", http_code(os.path.join(JAR_DIR, "u1.jar"), "/pages/alpha-one") == "200")
check("GET /pages/alpha-one/ → 200", http_code(os.path.join(JAR_DIR, "u1.jar"), "/pages/alpha-one/") == "200")

# 3. runtime state endpoint
r = call(os.path.join(JAR_DIR, "u1.jar"), "GET", "/api/runtime/alpha-one/state")
check("runtime state", "forumPosts" in r, str(r)[:200])

# 4. /api/site/alpha-one returns site.json
r = call(os.path.join(JAR_DIR, "u1.jar"), "GET", "/api/site/alpha-one")
check("/api/site 返回站点数据（basePath 正确）", r.get("basePath") == "/pages/alpha-one" and r.get("username") == "alpha-one", str(r)[:200])

# 5. re-publish same slug (update in place)
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload("alpha-one"))
check("同 slug 重新发布", r.get("ok") and r.get("replacedSlug") is None, str(r)[:200])

# 6. user2 cannot take alpha-one (409)
r = call(os.path.join(JAR_DIR, "u2.jar"), "POST", "/api/publish", site_payload("alpha-one"))
check("他人占用返回 409", "已被占用" in str(r.get("error", "")), str(r)[:200])

# 7. invalid slugs
for bad, why in [("ab", "太短"), ("-abc-", "短横线开头结尾"), ("a_b", "下划线"), ("www", "保留字"), ("a--b", "连续短横线")]:
    r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload(bad))
    check(f"非法 slug {bad}（{why}）被拒", bool(r.get("error")), f"{bad} 未被拒: {str(r)[:120]}")

# 8. user1 renames to beta-two → old page deleted
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload("beta-two"))
check("改名发布 beta-two，返回 replacedSlug", r.get("ok") and r.get("replacedSlug") == "alpha-one", str(r)[:200])
check("旧页面 /pages/alpha-one → 404", http_code(os.path.join(JAR_DIR, "u1.jar"), "/pages/alpha-one") == "404")
check("新页面 /pages/beta-two → 200", http_code(os.path.join(JAR_DIR, "u1.jar"), "/pages/beta-two") == "200")

# 9. old slug freed: user2 can now take alpha-one
r = call(os.path.join(JAR_DIR, "u2.jar"), "POST", "/api/publish", site_payload("alpha-one"))
check("改名后旧 slug 可被他人取用", r.get("ok"), str(r)[:200])

# 10. account-enabled site: first publish returns siteAdmin creds, second keeps them
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload("beta-two", account=True))
check("带账号系统的站点首发返回站长凭据", r.get("ok") and r.get("accountEnabled") and (r.get("siteAdmin") or {}).get("username") == "pubtest1" and (r.get("siteAdmin") or {}).get("password"), str(r)[:200])
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/publish", site_payload("beta-two", account=True))
check("重新发布保留账号数据（不再返回凭据）", r.get("ok") and r.get("siteAdmin") is None, str(r)[:200])

# 11. reset-owner returns new password
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/site-account/reset-owner", {})
check("reset-owner 重置站长密码", r.get("ok") and (r.get("siteAdmin") or {}).get("password"), str(r)[:200])

# 12. site member login with owner creds works on runtime endpoint
r = call(os.path.join(JAR_DIR, "u1.jar"), "POST", "/api/runtime/beta-two/login", {"username": "pubtest1", "password": "testpass123"})
check("站点账号登录（错误密码被拒）", bool(r.get("error")), str(r)[:200])

# 13. /api/auth/me carries publishSlug
r = call(os.path.join(JAR_DIR, "u1.jar"), "GET", "/api/auth/me")
check("/api/auth/me 返回 publishSlug=beta-two", r.get("user", {}).get("publishSlug") == "beta-two", str(r)[:200])

print(f"\n{len(passed)} passed, {len(failed)} failed")
cleanup()
sys.exit(1 if failed else 0)
