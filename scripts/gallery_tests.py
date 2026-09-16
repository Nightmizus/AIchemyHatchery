"""Hatchery Gallery 回归测试（配合 scripts/gallery_test_harness.py 使用）。

    .venv/bin/python scripts/gallery_test_harness.py --port 4181 &
    .venv/bin/python scripts/gallery_tests.py [--base http://127.0.0.1:4181]

每一轮独立播种假数据，逐项断言并打印 PASS/FAIL，最后以失败数作为退出码。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4181"
FAILURES: list[str] = []
PASSES = 0


def request(method: str, path: str, body: dict | None = None, user: str | None = None, headers: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("X-Test-User", user)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), dict(error.headers)


def seed(**payload):
    status, text, _ = request("POST", "/__test/seed", {"reset": True, **payload})
    assert status == 200, text


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}" + (f"  —— {detail}" if detail else ""))


def gallery() -> str:
    status, text, _ = request("GET", "/gallery")
    assert status == 200, f"/gallery -> {status}"
    return text


def card_count(page: str) -> int:
    match = re.search(r"共收录 (\d+) 个网站", page)
    return int(match.group(1)) if match else -1


def card_order(page: str) -> list[str]:
    return re.findall(r'<span class="host">([^<.]+)\.', page)


def page_payload(site_name: str, description: str, html: str = "<h1>hello</h1>") -> dict:
    return {"siteName": site_name, "description": description, "theme": "minimal", "background": "#ffffff", "contentWidth": 100,
            "pages": [{"id": "page-1", "name": "首页", "path": "home", "parentId": None, "kind": "page", "html": html}], "forumPosts": []}


def round_2_filtering() -> None:
    print("\nR2 过滤规则：未收录 / 磁盘缺失 / 损坏 site.json / XSS 转义 / 排序")
    seed(
        sites=[
            {"slug": "alpha", "user_id": "u1", "listed": True, "updated_at": "2026-09-10T00:00:00+00:00"},
            {"slug": "bravo", "user_id": "u2", "listed": False, "updated_at": "2026-09-11T00:00:00+00:00"},
            {"slug": "charlie", "user_id": "u1", "listed": True, "updated_at": "2026-09-12T00:00:00+00:00"},
            {"slug": "delta", "user_id": "u2", "listed": True, "updated_at": "2026-09-13T00:00:00+00:00"},
            {"slug": "echo", "user_id": "u1", "listed": True, "updated_at": "2026-09-14T00:00:00+00:00"},
        ],
        files=[
            {"slug": "alpha", "siteName": "Alpha 社团站", "description": "第一个站点"},
            {"slug": "bravo", "siteName": "Bravo 未收录", "description": "不该出现"},
            # charlie：数据库有行但磁盘没有 site.json（已删除 / 改名残留）
            {"slug": "delta", "raw": "{ this is not json"},
            {"slug": "echo", "siteName": "<script>alert(1)</script>Echo", "description": "简介里有 <b>标签</b> & 符号"},
        ],
    )
    page = gallery()
    check("收录站点显示", "Alpha 社团站" in page and "第一个站点" in page)
    check("未收录站点不显示", "Bravo" not in page)
    check("磁盘已删除的残留行不显示", "charlie" not in page)
    check("损坏 site.json 回退为 slug + 默认简介", ">delta<" in page and "这个网站还没有写介绍" in page)
    check("站点名中的脚本被转义", "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;Echo" in page)
    check("简介中的标签与 & 被转义", "<b>标签</b>" not in page and "&amp; 符号" in page)
    check("计数为 3", card_count(page) == 3, f"实际 {card_count(page)}")
    check("按更新时间倒序", card_order(page) == ["echo", "delta", "alpha"], f"实际 {card_order(page)}")


def round_3_legacy_consistency() -> None:
    print("\nR3 历史遗留站点：仅磁盘无 DB 行 —— 管理员总览与 Gallery 应一致")
    seed(
        sites=[{"slug": "alpha", "user_id": "u1", "listed": True, "updated_at": "2026-09-10T00:00:00+00:00"}],
        files=[
            {"slug": "alpha", "siteName": "Alpha 社团站", "description": "有数据库行"},
            {"slug": "legacy-site", "siteName": "老站点", "description": "迁移前发布，数据库没有行", "mtime": 1757000000},
            {"slug": "Legacy_User", "siteName": "老用户名站点", "description": "下划线与大写，不能当子域名", "mtime": 1757100000},
        ],
    )
    status, text, _ = request("GET", "/api/admin/sites", user="admin")
    check("管理员总览接口可用", status == 200, f"{status} {text[:80]}")
    sites = {item["slug"]: item for item in json.loads(text).get("sites", [])} if status == 200 else {}
    check("管理员总览包含遗留站点并标记为已收录", sites.get("legacy-site", {}).get("listed") is True and sites.get("Legacy_User", {}).get("listed") is True)
    page = gallery()
    check("Gallery 包含遗留站点（与总览一致）", "老站点" in page, "Gallery 只查了数据库，遗留站点丢失")
    check("Gallery 包含老用户名站点", "老用户名站点" in page)
    check("老用户名站点链接走 /pages/ 路径（不能当子域名）", 'href="/pages/Legacy_User"' in page, "子域名 Legacy_User.xxx 非法")
    check("管理员总览中老用户名站点的 publicUrl 同样走路径", sites.get("Legacy_User", {}).get("publicUrl", "").startswith("/pages/") or sites.get("Legacy_User", {}).get("publicUrl", "").endswith("/pages/Legacy_User"), f"实际 {sites.get('Legacy_User', {}).get('publicUrl')}")
    check("遗留站点总数一致", card_count(page) == len(sites) == 3, f"gallery={card_count(page)} admin={len(sites)}")


def round_4_publish_flow() -> None:
    print("\nR4 发布流程：收录开关落库并反映到 Gallery；重发；改名删旧；占用冲突")
    seed()
    status, text, _ = request("POST", "/api/publish", {**page_payload("发布测试站", "第一次发布，不收录"), "slug": "pub-one", "listed": False}, user="tester")
    check("首次发布成功", status == 200, f"{status} {text[:120]}")
    state = json.loads(request("GET", "/__test/state")[1])
    row = next((s for s in state["sites"] if s["slug"] == "pub-one"), None)
    check("listed=false 落库", row is not None and row["listed_in_gallery"] is False, f"row={row}")
    check("未收录时 Gallery 不显示", "发布测试站" not in gallery())
    me = json.loads(request("GET", "/api/auth/me", user="tester")[1])
    check("/api/auth/me 返回 publishListed=false", me.get("user", {}).get("publishListed") is False, f"{me.get('user', {}).get('publishListed')}")

    status, text, _ = request("POST", "/api/publish", {**page_payload("发布测试站·改", "第二次发布，收录"), "slug": "pub-one", "listed": True}, user="tester")
    check("同路径重发成功", status == 200, f"{status} {text[:120]}")
    state = json.loads(request("GET", "/__test/state")[1])
    row = next((s for s in state["sites"] if s["slug"] == "pub-one"), None)
    check("重发切换为收录", row is not None and row["listed_in_gallery"] is True, f"row={row}")
    page = gallery()
    check("Gallery 显示最新站名与简介", "发布测试站·改" in page and "第二次发布，收录" in page)
    check("Gallery 卡片显示作者", "tester" in page)

    status, text, _ = request("POST", "/api/publish", {**page_payload("发布测试站·三", "省略 listed 字段"), "slug": "pub-one"}, user="tester")
    state = json.loads(request("GET", "/__test/state")[1])
    row = next((s for s in state["sites"] if s["slug"] == "pub-one"), None)
    check("省略 listed 字段默认收录（兼容旧客户端）", status == 200 and row and row["listed_in_gallery"] is True)

    status, text, _ = request("POST", "/api/publish", {**page_payload("改名后的站", "换路径"), "slug": "pub-two", "listed": False}, user="tester")
    payload = json.loads(text) if status == 200 else {}
    state = json.loads(request("GET", "/__test/state")[1])
    slugs = {s["slug"]: s for s in state["sites"]}
    check("改名发布成功并返回 replacedSlug", status == 200 and payload.get("replacedSlug") == "pub-one", f"{status} {text[:120]}")
    check("旧路径记录删除、旧目录删除", "pub-one" not in slugs and "pub-one" not in state["published"], f"sites={list(slugs)} dirs={state['published']}")
    check("新路径 listed=false", slugs.get("pub-two", {}).get("listed_in_gallery") is False)
    check("改名后的未收录站点不在 Gallery", card_count(gallery()) == 0, f"count={card_count(gallery())}")

    status, text, _ = request("POST", "/api/publish", {**page_payload("抢占", "别人的路径"), "slug": "pub-two", "listed": True}, user="other")
    check("他人占用路径返回 409", status == 409, f"{status} {text[:100]}")
    state = json.loads(request("GET", "/__test/state")[1])
    check("冲突未改变原记录的收录状态", next(s for s in state["sites"] if s["slug"] == "pub-two")["listed_in_gallery"] is False)


def round_6_routing() -> None:
    print("\nR6 路由与保留字：大小写 / 尾斜杠 / 查询串 / 子域名 / gallery 保留字")
    seed(sites=[{"slug": "alpha", "user_id": "u1", "listed": True}], files=[{"slug": "alpha", "siteName": "Alpha", "description": "x"}])
    status, text, _ = request("GET", "/Gallery")
    check("/Gallery 大小写不敏感", status == 200 and "Hatchery Gallery" in text, f"{status}")
    status, text, _ = request("GET", "/gallery/")
    check("/gallery/ 尾斜杠可访问", status == 200 and "Hatchery Gallery" in text, f"{status}")
    status, text, _ = request("GET", "/gallery?from=sidebar")
    check("/gallery 带查询串可访问", status == 200 and "Hatchery Gallery" in text, f"{status}")
    status, text, headers = request("GET", "/gallery")
    check("响应头 no-store 与 noindex", headers.get("Cache-Control") == "no-store" and "noindex" in headers.get("X-Robots-Tag", ""))
    status, text, _ = request("GET", "/gallery", headers={"Host": "alpha.hatchery.mizusumi.com"})
    check("站点子域名下 /gallery 不返回收录页", "Hatchery Gallery" not in text, f"{status}")
    status, text, _ = request("POST", "/api/publish", {**page_payload("冒充收录页", "抢 gallery 子域名"), "slug": "gallery", "listed": True}, user="tester")
    check("gallery 为保留字，不能作为发布路径", status == 400 and "保留字" in text, f"{status} {text[:100]}")
    state = json.loads(request("GET", "/__test/state")[1])
    check("保留字发布未落库", all(s["slug"] != "gallery" for s in state["sites"]))


def round_7_performance() -> None:
    print("\nR7 性能：40 个约 1MB 的 site.json")
    files = [{"slug": f"big-{i:02d}", "siteName": f"大站点 {i}", "description": "带大量图片数据的站点", "padding": 1_000_000} for i in range(40)]
    sites = [{"slug": f"big-{i:02d}", "user_id": "u1", "listed": True, "updated_at": f"2026-09-{(i % 28) + 1:02d}T00:00:00+00:00"} for i in range(40)]
    seed(sites=sites, files=files)
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        page = gallery()
        timings.append((time.perf_counter() - started) * 1000)
    check("40 个大站点全部渲染", card_count(page) == 40, f"count={card_count(page)}")
    best = min(timings)
    check("重复访问 /gallery 单次耗时 < 150ms（首访可慢，后续应命中缓存）", best < 150, f"耗时 {', '.join(f'{t:.0f}ms' for t in timings)}")
    print(f"        耗时：{', '.join(f'{t:.0f}ms' for t in timings)}")
    # 改动 site.json 后应立刻反映（缓存不能过期失效）
    seed(sites=sites[:1], files=[{**files[0], "siteName": "大站点 0 已改名"}])
    check("site.json 变更后 Gallery 立即更新", "大站点 0 已改名" in gallery())


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--rounds", default="2,3,4,6,7")
    args = parser.parse_args()
    BASE = args.base.rstrip("/")
    rounds = {"2": round_2_filtering, "3": round_3_legacy_consistency, "4": round_4_publish_flow, "6": round_6_routing, "7": round_7_performance}
    for key in args.rounds.split(","):
        rounds[key.strip()]()
    print(f"\n通过 {PASSES} 项，失败 {len(FAILURES)} 项")
    for name in FAILURES:
        print(f"  ✗ {name}")
    return len(FAILURES)


if __name__ == "__main__":
    raise SystemExit(main())
