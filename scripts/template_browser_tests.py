"""Template flow through the actual console, files and admission handler.

Only durable job storage/AI execution is a fixture; no paid model or production
data is touched. Run with Playwright and the application dependencies installed.
"""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from site_files_tests import FileFixture, USER, PROJECT
import ai_jobs
import server


def main():
    checks, submissions, jobs = [], [], {}
    behavior = {'reject': False, 'hold': False, 'lose_ack': 0, 'offline': False}
    held = []

    def enqueue(user, payload, request_id):
        submissions.append((user, payload, request_id))
        if behavior['reject']:
            raise ai_jobs.QueueFull('测试队列暂满，请重试')
        if request_id not in jobs:
            jobs[request_id] = {'jobId': 'job-' + f'{len(jobs)+1:024x}', 'requestId': request_id,
                               'status': 'queued', 'mode': payload['mode'], 'events': [],
                               'prompt': payload['prompt'], 'result': None, 'createdAt': '2026-10-19T12:00:00Z'}
        return jobs[request_id]

    with FileFixture() as f, patch.object(server.AI_JOB_STORE, 'enqueue', side_effect=enqueue), sync_playwright() as pw:
        (f.directory / 'about.html').write_text('<h1>关于我们</h1>')
        fingerprint = lambda: {str(p.relative_to(f.directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in f.directory.rglob('*') if p.is_file()}
        original_files = fingerprint()
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route_api(route):
                path = urlparse(route.request.url).path
                if path == '/api/ai/conversation':
                    route.fulfill(json={'session': {'id': ai_jobs.conversation_id(USER['id']),
                        'messagesHtml': ai_jobs.render_history('', list(jobs.values())), 'jobs': list(jobs.values())}})
                elif path == '/api/ai/run/active':
                    route.fulfill(json={'jobs': list(jobs.values()), 'queue': {'running': 1, 'queued': len(jobs), 'maxParallel': 2}})
                elif path == '/api/ai/status':
                    route.fulfill(json={'configured': True})
                elif path == '/api/ai/run/status':
                    job_id = parse_qs(urlparse(route.request.url).query)['id'][0]
                    job = next((j for j in jobs.values() if j['jobId'] == job_id), None)
                    route.fulfill(status=200 if job else 404, json=job or {'error': 'missing'})
                elif path == '/api/ai/run/ack':
                    route.fulfill(json={'ok': True})
                elif path == '/api/ai/run':
                    if behavior['hold']:
                        held.append(route)
                    elif behavior['offline']:
                        route.abort('timedout')
                    elif behavior['lose_ack']:
                        behavior['lose_ack'] -= 1
                        assert route.fetch().status == 202
                        route.fulfill(status=503, json={'error': 'lost acknowledgement'})
                    else:
                        route.continue_()
                else:
                    route.continue_()

            page.route('**/api/ai/**', route_api)
            page.goto(f'http://127.0.0.1:{f.port}/')
            page.wait_for_function('currentConsoleUser && !fileSiteLoading && fileSite?.pages.includes("about.html")')
            opener = page.locator('[data-template-open]')
            picker = page.locator('#templatePicker')
            opener.click()
            expect(page.locator('[data-template-type]')).to_have_count(24)
            expect(page.locator('[data-template-confirm]')).to_be_disabled()
            expect(page.locator('[data-template-search]')).to_be_focused()
            page.locator('[data-template-type="hero"]').click()
            page.locator('[data-template-search]').fill('投票')
            expect(page.locator('[data-template-card]:visible')).to_have_count(1)
            page.locator('[data-template-type="poll"]').click()
            page.locator('[data-template-category]').select_option('0')
            expect(page.locator('[data-template-empty]')).to_be_visible()
            page.locator('[data-template-reset-filter]').click()
            expect(page.locator('[data-template-card]:visible')).to_have_count(24)
            expect(page.locator('[data-template-count]')).to_have_text('已选 2 / 8')
            page.locator('[data-template-search]').fill('<img onerror=alert(1)>')
            expect(page.locator('[data-template-empty]')).to_be_visible()
            page.locator('[data-template-reset-filter]').click()
            checks.append('24 modules, search/category intersection, empty reset and cross-filter selection')

            # Every snippet is deterministic and does not mutate the visual editor.
            before = page.evaluate('JSON.stringify([state,nextElementId,nextPageId,nextItemId,previewDB.accountLoggedIn])')
            snippets = page.evaluate('TEMPLATE_TYPES.map(templateSnippet)')
            assert snippets == page.evaluate('TEMPLATE_TYPES.map(templateSnippet)')
            assert before == page.evaluate('JSON.stringify([state,nextElementId,nextPageId,nextItemId,previewDB.accountLoggedIn])')
            for snippet in snippets:
                assert snippet['html'] and 'data-preview-action' not in snippet['html']
                page.locator(f'[data-template-preview="{snippet["type"]}"]').click()
                frame = page.frame_locator('[data-template-preview-panel] iframe')
                expect(frame.locator('main')).not_to_be_empty()
                assert page.locator('[data-template-preview-panel] iframe').get_attribute('sandbox') == ''
                assert "default-src 'none'" in page.locator('[data-template-preview-panel] iframe').get_attribute('srcdoc')
                page.locator('[data-template-preview-close]').click()
            assert before == page.evaluate('JSON.stringify([state,nextElementId,nextPageId,nextItemId,previewDB.accountLoggedIn])')
            assert '固定为' not in next(s['html'] for s in snippets if s['type'] == 'account')
            checks.append('all 24 read-only previews and stable snippets, no phantom pages or fake admin login')

            # No delayed closing element can consume a rapid reopen.
            page.evaluate('closeTemplatePicker(); openTemplatePicker(); closeTemplatePicker(); openTemplatePicker()')
            expect(picker).to_have_count(1)
            expect(page.locator('[data-template-count]')).to_have_text('已选 2 / 8')
            page.locator('[data-template-notes]').fill('保留已有内容，放在介绍后')
            page.locator('[data-template-target]').select_option('about.html')
            page.reload()
            page.wait_for_function('currentConsoleUser && !fileSiteLoading')
            opener.click()
            expect(page.locator('[data-template-count]')).to_have_text('已选 2 / 8')
            expect(page.locator('[data-template-notes]')).to_have_value('保留已有内容，放在介绍后')
            expect(page.locator('[data-template-target]')).to_have_value('about.html')
            page.keyboard.press('Escape')
            expect(picker).to_have_count(0)
            expect(opener).to_be_focused()
            assert not page.evaluate('document.querySelector("#aiComposer").inert')
            opener.click()
            checks.append('rapid close/reopen, per-account reload draft, Escape and focus restoration')

            page.locator('[data-template-clear]').click()
            types = page.evaluate('TEMPLATE_TYPES.slice(0,9)')
            for kind in types:
                page.locator(f'[data-template-type="{kind}"]').click()
            expect(page.locator('[data-template-count]')).to_have_text('已选 8 / 8')
            expect(page.locator('[data-template-status]')).to_contain_text('最多添加 8 个')
            expect(page.locator(f'[data-template-type="{types[-1]}"]')).to_have_attribute('aria-pressed', 'false')
            page.locator('[data-template-clear]').click()
            page.locator('[data-template-type="projects"]').click()
            page.locator('[data-template-type="account"]').click()
            checks.append('eight-module limit and clear-selection controls')

            # Local prerequisites and explicit server rejection must not discard work.
            page.evaluate('fileSiteLoading=true')
            page.locator('[data-template-confirm]').click()
            expect(page.locator('[data-template-status]')).to_contain_text('尚未加载')
            assert not submissions
            page.evaluate('fileSiteLoading=false; aiServiceConfigured=false')
            page.locator('[data-template-confirm]').click()
            expect(page.locator('[data-template-status]')).to_contain_text('AI 尚未配置')
            page.evaluate('aiServiceConfigured=true; aiAttachments=[{name:"notes.txt",type:"text/plain",kind:"text",content:"参考要求",size:12}]; renderAiAttachments(); setAiPermissionMode("chat")')
            behavior['reject'] = True
            page.locator('[data-template-confirm]').click()
            expect(page.locator('[data-template-status]')).to_contain_text('测试队列暂满')
            expect(page.locator('[data-template-count]')).to_have_text('已选 2 / 8')
            assert page.evaluate('aiAttachments.length') == 1
            assert len(submissions) == 1 and not jobs
            checks.append('loading/configuration/HTTP 429 retain selection, requirements and attachments')

            behavior.update(reject=False, hold=True)
            before_calls = len(submissions)
            page.evaluate('document.querySelector("[data-template-confirm]").click(); document.querySelector("[data-template-confirm]").click()')
            expect(page.locator('[data-template-confirm]')).to_be_disabled()
            page.locator('[data-template-close]').click()
            opener.click()
            expect(page.locator('[data-template-confirm]')).to_be_disabled()
            page.wait_for_function('templateDraft.submitting')
            assert len(held) == 1
            behavior['hold'] = False
            held.pop().continue_()
            expect(picker).to_have_count(0)
            assert len(submissions) == before_calls + 1 and len(jobs) == 1
            user, payload, _rid = submissions[-1]
            assert user['id'] == USER['id'] and payload['mode'] == 'full'
            assert payload['context']['activeFile'] == 'about.html'
            assert payload['chosenPresets'] == ['projects', 'account']
            assert len(payload['presetSnippets']) == 2 and len(payload['attachments']) == 1
            assert '其他用户' in payload['prompt'] and '保留已有内容' in payload['prompt']
            assert page.evaluate('aiPermissionMode') == 'chat'
            assert page.evaluate('aiAttachments.length') == 0
            checks.append('double click and reopen while pending enqueue once, full action uses selected page without changing chat mode')

            opener.click()
            expect(page.locator('[data-template-count]')).to_have_text('已选 0 / 8')
            expect(page.locator('[data-template-status]')).to_contain_text('服务器排队')
            page.locator('[data-template-type="forum"]').click()
            behavior['lose_ack'] = 1
            before_calls = len(submissions)
            page.locator('[data-template-confirm]').click()
            expect(picker).to_have_count(0, timeout=12000)
            assert len(submissions) == before_calls + 2 and len(jobs) == 2
            assert submissions[-1][2] == submissions[-2][2]
            checks.append('browse/select while AI busy; lost acknowledgement retries exactly one server job')

            opener.click()
            page.locator('[data-template-type="hero"]').click()
            behavior['offline'] = True
            page.locator('[data-template-confirm]').click()
            expect(page.locator('[data-template-status]')).to_contain_text('未确认服务器是否受理', timeout=16000)
            retry_id = page.evaluate('JSON.parse(sessionStorage.getItem(templateStorageKey().replace("templates:v1","pending-template-request"))).id')
            behavior['offline'] = False
            page.reload()
            page.wait_for_function('currentConsoleUser && !fileSiteLoading')
            page.evaluate('state.siteName="其他已完成任务更新过网站名称"; state.description="已更新的简介"')
            opener.click()
            expect(page.locator('[data-template-count]')).to_have_text('已选 1 / 8')
            page.locator('[data-template-confirm]').click()
            expect(picker).to_have_count(0)
            assert submissions[-1][2] == retry_id and len(jobs) == 3
            checks.append('offline failure and reload retry preserve original request even if website metadata changes')

            opener.click()
            page.locator('[data-template-type="hero"]').click()
            page.locator('[data-template-preview="hero"]').click()
            expect(page.frame_locator('[data-template-preview-panel] iframe').locator('h2')).to_be_visible()
            output = PROJECT / 'output/playwright'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'templates-desktop.png'), full_page=True, animations='disabled')
            for width in (390, 320):
                page.set_viewport_size({'width': width, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                box = page.locator('[data-template-confirm]').bounding_box()
                assert box and box['x'] >= 0 and box['x'] + box['width'] <= width and box['y'] + box['height'] <= 844
                assert page.locator('.template-picker-body').bounding_box()['height'] > 300
                assert page.locator('[data-template-close]').evaluate('(el)=>{const r=el.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el}')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(output / 'templates-mobile.png'), full_page=True, animations='disabled')
            page.keyboard.press('Escape')
            page.emulate_media(reduced_motion='reduce')
            opener.click()
            page.keyboard.press('Escape')
            expect(picker).to_have_count(0)
            checks.append('desktop and 390/320px mobile layouts, reachable action, reduced-motion keyboard navigation')

            opener.click()
            page.evaluate('document.querySelector("#aiPrompt").value="第一账号私有草稿"; showAuthGate(); currentConsoleUser={id:"second-account",username:"other"}; openTemplatePicker()')
            expect(page.locator('[data-template-count]')).to_have_text('已选 0 / 8')
            expect(page.locator('[data-template-notes]')).to_have_value('')
            assert fingerprint() == original_files
            assert not errors, errors
            checks.append('account isolation, browsing/admission never changes website files, no JavaScript errors')
            print(json.dumps({'passed': len(checks), 'checks': checks, 'realModelCalls': 0}, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
