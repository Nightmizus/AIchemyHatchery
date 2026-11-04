"""Actual browser + WebSocket incremental updates; no model or production DB."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from site_files_tests import FileFixture, USER
from job_events import EventHub, LiveServer
import ai_jobs


def main():
    jobs, requests, errors = {}, [], []
    hub = EventHub()
    now = lambda: datetime.now(timezone.utc).isoformat()
    with FileFixture() as fixture, sync_playwright() as pw:
        live = LiveServer(hub, lambda _:USER, fixture.port+1, heartbeat=0.5)
        live.start()
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on('pageerror', lambda error:errors.append(str(error)))
            def api(route):
                path = urlparse(route.request.url).path
                requests.append(path)
                if path == '/api/ai/conversation':
                    route.fulfill(json={'session':{'id':ai_jobs.conversation_id(USER['id']),
                        'jobs':list(jobs.values()),'messagesHtml':ai_jobs.render_history('',list(jobs.values()))}})
                elif path == '/api/ai/run/active': route.fulfill(json={'jobs':list(jobs.values()),'queue':None})
                elif path == '/api/ai/run/ack': route.fulfill(json={'ok':True})
                else: route.continue_()
            page.route('**/api/ai/**', api)
            page.goto(f'http://127.0.0.1:{fixture.port}/')
            page.wait_for_function('currentConsoleUser && fileSite?.format === "files" && aiLiveConnection?.connected')
            page.locator('#aiPrompt').fill('unsent draft stays here')
            job = {'jobId':'job-'+'1'*24,'requestId':'11111111-1111-4111-8111-111111111111',
                   'sessionId':ai_jobs.conversation_id(USER['id']),'sequence':1,'mode':'chat','kind':'chat',
                   'status':'queued','events':[],'result':None,'error':None,'prompt':'<script>window.injected=1</script>',
                   'createdAt':now(),'updatedAt':now()}
            jobs[job['jobId']] = job
            def state(): hub.publish(USER['id'],'job',{'job':dict(job),'html':ai_jobs.render_history('',[job])})
            state()
            page.wait_for_function('document.querySelector("section[data-ai-status=queued]") !== null')
            job.update(status='running',updatedAt=now());state()
            page.wait_for_function('document.querySelector("section[data-ai-status=running]") !== null')
            # A worker's event doesn't wait for a history refresh or DB round trip.
            before = len(requests)
            def progress(text):
                event = {'id':'stream-one','kind':'message','label':'AI 正在回复','detail':text,'status':'running'}
                job.update(events=[event],updatedAt=now())
                hub.publish(USER['id'],'progress',{'jobId':job['jobId'],'event':event,'updatedAt':job['updatedAt']})
            started=time.monotonic();progress('first live text')
            expect(page.locator('[data-ai-event-id=stream-one]')).to_contain_text('first live text')
            first_ms=round((time.monotonic()-started)*1000)
            for i in range(20): progress('delta-'+str(i))
            expect(page.locator('[data-ai-event-id=stream-one]')).to_contain_text('delta-19')
            assert len(requests)==before, requests[before:]
            expect(page.locator('#aiPrompt')).to_have_value('unsent draft stays here')
            assert page.evaluate('window.injected') is None
            assert page.locator('section[data-ai-job-id]').count()==1
            page.evaluate('aiLiveConnection.socket.close()')
            page.wait_for_function('!aiLiveConnection.connected')
            progress('delivered after reconnect')
            expect(page.locator('[data-ai-event-id=stream-one]')).to_contain_text('delivered after reconnect',timeout=7000)
            # Overflow deliberately forces an authoritative baseline, never a silent gap.
            page.evaluate('aiLiveConnection.socket.close()')
            page.wait_for_function('!aiLiveConnection.connected')
            hub.max_events=2
            for i in range(6): progress('gap-repair-'+str(i))
            expect(page.locator('[data-ai-event-id=stream-one]')).to_contain_text('gap-repair-5',timeout=7000)
            hub.publish('other-account','progress',{'jobId':job['jobId'],'event':{'id':'leak','detail':'private-other'}})
            page.wait_for_timeout(100)
            expect(page.locator('#aiChatMessages')).not_to_contain_text('private-other')
            job.update(status='completed',result={'summary':'## Finished once','changedFiles':[]},updatedAt=now());state()
            page.wait_for_function('!anyAiRunActive()')
            expect(page.locator('.ai-run-final')).to_contain_text('Finished once')
            assert page.locator('section[data-ai-job-id]').count()==1
            page.evaluate('showAuthGate()')
            assert page.evaluate('aiLiveConnection') is None
            assert not errors, errors
            print(json.dumps({'live_dom_ms':first_ms,'progress_requests':0,'reconnect_replay':True,
                'gap_reset':True,'draft_preserved':True,'other_account_hidden':True,'duplicate_jobs':False,'page_errors':errors}))
        finally:
            browser.close();live.close()


if __name__ == '__main__': main()
