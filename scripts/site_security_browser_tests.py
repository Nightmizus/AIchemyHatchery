"""Actual console/assets/API handlers; queue completion is controlled, no model calls."""
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from site_files_tests import FileFixture, USER
import ai_jobs
import server

REPORT = '''## 待复核风险

**DOM XSS**：`index.html:12` 将查询参数写入 `innerHTML`。
这是源码分析发现，未执行攻击。

- 建议使用 `textContent`。
- 未验证登录态和后台数据变更。

<script>window.securityXss = true</script>
![不应加载](https://security-pixel.invalid/pixel.png)
'''


def main():
    jobs = {}; submissions = []; checks = []
    latest = lambda _uid: list(jobs.values())[-1] if jobs else None
    def enqueue(user, payload, rid):
        submissions.append(rid)
        if rid not in jobs:
            jobs[rid] = {'jobId': 'job-' + f'{len(jobs)+1:024x}', 'requestId': rid, 'kind': 'security',
                         'mode': 'security', 'status': 'queued', 'events': [], 'result': None, 'prompt': payload['prompt']}
        return jobs[rid]
    with FileFixture() as f, patch.object(server.AI_JOB_STORE, 'enqueue', side_effect=enqueue), \
            patch.object(server.AI_JOB_STORE, 'latest_security', side_effect=latest), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            errors, requested = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requested.append(urlparse(request.url).path))
            def conversation(route):
                route.fulfill(json={'session': {'id': ai_jobs.conversation_id(USER['id']), 'messagesHtml': '', 'jobs': []}})
            page.route('**/api/ai/conversation', conversation)
            page.route('https://security-pixel.invalid/**', lambda route: route.abort())
            page.goto(f'http://127.0.0.1:{f.port}/')
            page.wait_for_function('currentConsoleUser !== null && !fileSiteLoading')
            page.locator('[data-activity="security"]').click()
            page.wait_for_function('!document.querySelector("#securityRunBtn").disabled')
            assert '/preview/' in page.locator('#securityTarget').inner_text()
            assert '控制台服务' not in page.locator('.security-panel').inner_text()
            lost = [True]
            def lose_ack(route):
                if lost[0]:
                    lost[0] = False
                    response = route.fetch()
                    assert response.status == 202
                    route.fulfill(status=503, json={'error': 'fixture lost acknowledgement'})
                else: route.continue_()
            page.route('**/api/security/run', lose_ack)
            requested.clear()
            page.locator('#securityRunBtn').click()
            page.wait_for_function('document.querySelector("#securityStatus").textContent.includes("已在服务器排队")')
            assert len(submissions) == 2 and len(set(submissions)) == 1 and len(jobs) == 1
            assert page.locator('#securityRunBtn').is_disabled()
            assert not any(path in ('/', '/.env', '/.git/config', '/server.py') for path in requested)
            checks += ['only the owned website is displayed', 'lost admission acknowledgement reuses one request ID', 'no console scanning requests']

            job = latest(USER['id']); target = f.handler.security_target(USER['id'])
            evidence = {'target': target, 'checks': [{'name': '首页', 'status': 'pass', 'detail': 'HTTP 200'},
                {'name': '敏感文件', 'status': 'warn', 'detail': '未验证：请求失败'}], 'scope': '只读源码审查；未执行写入', 'model': 'fixture-model'}
            job.update(status='running', events=[{'id': 'security-baseline', 'label': '本站基础检查完成', 'detail': '分析源码', 'security': evidence}])
            page.evaluate('AIchemyHatcherySecurity.refresh()')
            page.wait_for_function('document.querySelector("#securitySummary").textContent.includes("1 项待复核")')
            job.update(status='completed', finishedAt='2026-10-19T12:00:00Z', result={'security': evidence, 'summary': REPORT})
            page.evaluate('AIchemyHatcherySecurity.refresh()')
            page.locator('#securityReport').wait_for(state='visible')
            assert page.locator('#securityReportBody h2').inner_text() == '待复核风险'
            assert page.locator('#securityReportBody strong').inner_text() == 'DOM XSS'
            assert page.locator('#securityReportBody script, #securityReportBody img').count() == 0
            assert page.evaluate('window.securityXss !== true')
            checks += ['live progress and partial evidence', 'Markdown report is rendered safely']

            page.reload()
            page.wait_for_function('currentConsoleUser !== null && !fileSiteLoading')
            page.locator('[data-activity="security"]').click()
            page.locator('#securityReport').wait_for(state='visible')
            assert page.locator('#securityReportBody h2').count() == 1
            assert len(jobs) == 1
            # Resetting/re-entering with the same report must not leave a blank body.
            page.evaluate('AIchemyHatcherySecurity.reset(); AIchemyHatcherySecurity.refresh()')
            page.wait_for_function('document.querySelector("#securityReportBody h2") !== null')
            checks += ['reload resumes the saved report without resubmission', 'reset cannot retain stale report cache']

            output = Path(__file__).resolve().parents[1] / 'output/playwright'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'site-security-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'site-security-mobile.png'), full_page=True)
            checks += ['mobile has no horizontal overflow']

            job.update(status='failed', result=None, error='fixture model unavailable')
            page.evaluate('AIchemyHatcherySecurity.refresh()')
            page.wait_for_function('document.querySelector("#securityStatus").textContent.includes("fixture model unavailable")')
            assert page.locator('#securityReport').is_hidden()
            assert not page.locator('#securitySummary').is_hidden()
            assert page.locator('#securityRunBtn').is_enabled()
            page.evaluate('showAuthGate()')
            assert page.locator('#securityReportBody').inner_text() == ''
            assert page.locator('#securityResults').inner_text() == ''
            checks += ['model failure keeps basic evidence without claiming completion', 'logout clears private results']
            assert not errors, errors
            print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__': main()
