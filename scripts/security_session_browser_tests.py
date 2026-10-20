"""Real PostgreSQL + HTTP + browser isolation; only the model execution is omitted.

The website-building frontend is unchanged. Exercise its real polling/history
against security jobs, including queued/running/completed/failed audits.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from site_files_tests import FileFixture, USER
from local_postgres import LocalPostgres
import ai_jobs
import server
import site_files


def main():
    checks = []
    with LocalPostgres() as pg, FileFixture() as fixture, sync_playwright() as pw:
        store = ai_jobs.JobStore(pg.db)
        with pg.db() as conn, conn.cursor() as cur:
            cur.execute('CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,messages_html TEXT,created_at TEXT,updated_at TEXT)')
        store.initialize()
        building = store.enqueue(USER, {'prompt': '继续编辑我的网站', 'mode': 'full'}, str(uuid.uuid4()))
        store.claim(2)
        store.finish(building['jobId'], {'summary': '## 建站记录\n\n这条建站回复必须保持不变。'})
        store.ack(USER['id'], building['jobId'])
        saved_session = store.session(USER['id'], building['sessionId'])
        saved_files = site_files.file_manifest(fixture.directory)
        original_get = fixture.Handler._do_GET

        def get(handler):
            # FileFixture normally stubs the queue empty; use the real handler
            # here so the test would expose the original audit/chat mixing bug.
            if handler.path.split('?')[0] == '/api/ai/run/active':
                return server.AIchemyHatcheryHandler._do_GET(handler)
            return original_get(handler)

        with patch.object(server, 'AI_JOB_STORE', store), patch.object(fixture.Handler, '_do_GET', get):
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 960})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{fixture.port}/')
                page.wait_for_function('currentConsoleUser && !fileSiteLoading && aiChatSessionId')
                expect(page.locator('#aiChatMessages')).to_contain_text('这条建站回复必须保持不变')
                page.locator('#aiPrompt').fill('尚未发送的建站草稿')
                page.locator('#aiFileInput').set_input_files({'name': 'draft.txt', 'mimeType': 'text/plain', 'buffer': b'preserve attachment'})
                page.wait_for_function('aiAttachments.length === 1')
                page.evaluate('setAiPermissionMode("plan")')
                snapshot = '''() => ({id:aiChatSessionId, html:aiSessionHtml, prompt:document.querySelector('#aiPrompt').value,
                    attachments:aiAttachments, mode:aiPermissionMode, usage:document.querySelector('#aiContextRingWrap').getAttribute('aria-valuenow')})'''
                before = page.evaluate(snapshot)

                def untouched():
                    page.evaluate('resumeAiRuns()')
                    assert page.evaluate(snapshot) == before
                    assert page.evaluate('!anyAiRunActive() && !document.body.classList.contains("ai-session-busy")')
                    assert store.session(USER['id'], building['sessionId']) == saved_session
                    assert store.active(USER['id']) == []

                page.locator('[data-activity="security"]').click()
                expect(page.locator('#securityRunBtn')).to_be_enabled()
                page.locator('#securityRunBtn').click()
                expect(page.locator('#securityStatus')).to_contain_text('已在服务器排队')
                audit = store.latest_security(USER['id'])
                assert audit['sessionId'] != building['sessionId']
                assert store.session(USER['id'], audit['sessionId']) is not None
                untouched()
                assert fixture.request('/api/ai/run/status?id=' + audit['jobId'])[0] == 404
                checks.append('queued audit uses a separate session, hidden from building status/history/active APIs')

                assert store.claim(2)['id'] == audit['jobId']
                target = fixture.handler.security_target(USER['id'])
                evidence = {'target': target, 'model': 'fixture', 'scope': '本站只读审查', 'checks': []}
                store.progress(audit['jobId'], [
                    {'id': 'security-baseline', 'kind': 'tool', 'label': '独立测试', 'security': evidence},
                    {'id': 'dsh-context', 'kind': 'context', 'contextUsage': {'inputTokens': 980000, 'contextWindow': 1000000}},
                ])
                page.evaluate('AIchemyHatcherySecurity.refresh()')
                expect(page.locator('#securityStatus')).to_contain_text('正在只读审查')
                untouched()
                # Navigation must not clear drafts/attachments or append audit messages.
                page.evaluate('switchActivity("ai")')
                expect(page.locator('#aiPrompt')).to_have_value('尚未发送的建站草稿')
                expect(page.locator('#aiChatMessages')).not_to_contain_text('独立测试')
                page.locator('[data-activity="security"]').click()
                untouched()
                checks.append('running audit cannot change building draft, attachment, mode, history, busy state or context meter')

                store.finish(audit['jobId'], {'summary': '## 独立安全报告\n\n仅在漏洞测试模块显示。', 'security': evidence})
                page.evaluate('AIchemyHatcherySecurity.refresh()')
                expect(page.locator('#securityReportBody h2')).to_have_text('独立安全报告')
                assert store.session(USER['id'], audit['sessionId']) is None
                untouched()
                expect(page.locator('#aiChatMessages')).not_to_contain_text('独立安全报告')
                checks.append('completed temporary session deleted; report remains only in security module')

                page.locator('#securityRunBtn').click()
                expect(page.locator('#securityStatus')).to_contain_text('已在服务器排队')
                second = store.latest_security(USER['id'])
                assert second['sessionId'] not in (audit['sessionId'], building['sessionId'])
                store.claim(2); store.finish(second['jobId'], error='测试模拟的模型故障')
                page.evaluate('AIchemyHatcherySecurity.refresh()')
                expect(page.locator('#securityStatus')).to_contain_text('测试模拟的模型故障')
                assert store.session(USER['id'], second['sessionId']) is None
                untouched()
                checks.append('next audit gets a fresh session and failure cleans it without touching building session')

                # Sending an actual building request still reuses the original
                # conversation; do not merely prove that audits disappeared.
                page.evaluate('switchActivity("ai")')
                with page.expect_response(lambda response: '/api/ai/run?async=1' in response.url and response.status == 202):
                    page.locator('#aiAdjustBtn').click()
                new_chat = store.active(USER['id'])
                assert len(new_chat) == 1 and new_chat[0]['sessionId'] == building['sessionId']
                assert new_chat[0]['mode'] == 'plan' and new_chat[0]['attachments'][0]['name'] == 'draft.txt'
                history = store.context(USER['id'], building['sessionId'], new_chat[0]['sequence'])
                assert '建站记录' in json.dumps(history, ensure_ascii=False)
                assert '独立安全报告' not in json.dumps(history, ensure_ascii=False)
                store.claim(2); store.finish(new_chat[0]['jobId'], {'summary': '继续建站的回复'})
                page.evaluate('resumeAiRuns()')
                expect(page.locator('#aiChatMessages')).to_contain_text('继续建站的回复')
                checks.append('subsequent building request reuses its original conversation and context, preserving selected mode and attachment')

                page.reload()
                page.wait_for_function('currentConsoleUser && !fileSiteLoading && aiChatSessionId')
                expect(page.locator('#aiChatMessages')).to_contain_text('这条建站回复必须保持不变')
                expect(page.locator('#aiChatMessages')).not_to_contain_text('测试模拟的模型故障')
                assert page.evaluate('!anyAiRunActive()')
                page.locator('[data-activity="security"]').click()
                expect(page.locator('#securityStatus')).to_contain_text('测试模拟的模型故障')
                assert site_files.file_manifest(fixture.directory) == saved_files
                assert not errors, errors
                checks.append('reload preserves isolated results, website files unchanged, no JavaScript errors')
                print(json.dumps({'passed': len(checks), 'checks': checks, 'realModelCalls': 0}, ensure_ascii=False))
            finally:
                browser.close()


if __name__ == '__main__': main()
