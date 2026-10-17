"""Actual console/assets and server history; only admission is stubbed, no AI calls."""
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from site_files_tests import FileFixture
import ai_jobs

SUMMARY = '''## 完成情况
第一行
第二行

**已完成**首页修改，文件是 `index.html`。

- 导航已更新
- [验证预览](https://example.com/)
  - 子检查已通过

1. 保存文件
2. 核对预览

> 这里只报告实际检查结果。

```html
<div class="example">代码保留，不作为页面执行</div>
'''+ 'long_line_' * 35 + '''
```

| 检查 | 结果 |
| --- | --- |
| 页面 | 通过 |

<script>window.__markdownXss = true</script>
[危险链接](javascript:alert(1))
![远程图片](https://markdown-pixel.invalid/pixel.png)
'''
PLAN = '### 后续计划\n\n1. **读取** `index.html`\n2. 保留现有内容，再修改\n\n```js\nconsole.log("plan source");\n```'


def main():
    checks = []; calls = []; remote_images = []
    jobs = [dict(jobId='job-' + f'{index:024x}', requestId=str(index), status='completed', mode='plan',
                 prompt='# 用户文字 **保持原样**\n下一行', createdAt='2026-10-16T00:00:00Z',
                 events=[{'id': 'tool', 'kind': 'tool', 'label': '**工具日志不做 Markdown 渲染**', 'status': 'done'}],
                 result={'summary': SUMMARY, 'plan': PLAN}) for index in (1, 2)]
    with FileFixture() as fixture, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('https://markdown-pixel.invalid/**', lambda route: (remote_images.append(route.request.url), route.abort()))
            def route_ai(route):
                path = urlparse(route.request.url).path
                if path == '/api/ai/conversation':
                    payload = {'session': {'id': ai_jobs.conversation_id('file-test-user'), 'jobs': jobs,
                                           'messagesHtml': ai_jobs.render_history('', jobs)}}
                elif path == '/api/ai/run/active': payload = {'jobs': jobs}
                elif path == '/api/ai/run/ack': payload = {'ok': True}
                elif path == '/api/ai/run' and route.request.method == 'POST':
                    request = route.request.post_data_json; calls.append(request)
                    payload = {**request, 'jobId': 'job-' + '3' * 24, 'status': 'queued', 'createdAt': '2026-10-16T00:00:00Z'}
                    route.fulfill(status=202, json=payload); return
                else: route.continue_(); return
                route.fulfill(json=payload)
            page.route('**/api/ai/**', route_ai)
            page.goto(f'http://127.0.0.1:{fixture.port}/')
            page.wait_for_function('currentConsoleUser !== null && !fileSiteLoading && document.querySelectorAll(".ai-run-summary.ai-markdown").length === 2')
            def assert_history():
                for summary in page.locator('.ai-run-summary').all():
                    for selector in ('h2', 'strong', 'ul', 'ol', 'blockquote', 'pre code', 'table', 'br'):
                        assert summary.locator(selector).count(), selector
                    assert summary.locator('script, img, svg, a[href^="javascript:"]').count() == 0
                    link = summary.get_by_role('link', name='验证预览')
                    assert link.get_attribute('rel') == 'noopener noreferrer'
                    assert summary.locator('pre code').inner_text().startswith('<div')
                assert page.locator('.ai-message.user strong, .ai-run-tool strong').count() == 0
                assert page.evaluate('window.__markdownXss !== true')
                assert not remote_images
            assert_history()
            checks += ['all archived/latest replies render Markdown', 'single newlines and paragraphs preserved', 'HTML/unsafe links inert; no remote image requests', 'user messages and tool logs unchanged']
            page.evaluate('resumeAiRuns()'); assert_history()
            page.reload()
            page.wait_for_function('document.querySelectorAll(".ai-run-summary.ai-markdown").length === 2')
            assert_history(); checks.append('polling and reload preserve formatting without double rendering')
            screenshot = Path(__file__).resolve().parents[1] / 'output/playwright'
            screenshot.mkdir(parents=True, exist_ok=True)
            page.locator('#aiConversation').evaluate('(node) => node.scrollTop = 0')
            page.screenshot(path=str(screenshot / 'ai-markdown-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.ai-run-summary pre').first.evaluate('(node) => node.scrollWidth > node.clientWidth')
            page.screenshot(path=str(screenshot / 'ai-markdown-mobile.png'))
            checks.append('mobile keeps long code scroll inside reply')
            page.locator('.ai-plan-box [data-ai-plan-execute]').last.click()
            page.wait_for_function('!Array.from(aiRuns.values()).some(run => run.status === "submitting")')
            assert len(calls) == 1 and PLAN in calls[0]['prompt']
            checks.append('execute plan submits original Markdown with line breaks')
            rendered = page.evaluate('''(source) => {
                const root = document.createElement('div');
                root.innerHTML = AI_CHAT_INITIAL_HTML;
                renderAiRunResult({mode: 'plan'}, {summary: source, mode: 'plan', plan: '1. **计划**'}, root);
                return root.innerHTML;
            }''', '**正在完成**\n第二行')
            assert '<strong>正在完成</strong>' in rendered and '<br>' in rendered
            checks.append('direct result renderer matches server history')
            source = '**完整内容**\n保留第二行'
            preserved = page.evaluate('''(source) => {
                const root = document.createElement('div');
                root.innerHTML = '<div class="ai-run-final"><p id="aiRunSummary"></p></div>';
                typeAiSummary(source, root);
                finishAiSummaryTyping(root);
                return window.AIchemyHatcheryMarkdown.source(root.querySelector('#aiRunSummary'));
            }''', source)
            assert preserved == source
            checks.append('interrupted typing archives the full original Markdown')
            assert not errors, errors
            print(json.dumps({'passed': checks, 'errors': errors, 'realModelCalls': 0}, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__': main()
