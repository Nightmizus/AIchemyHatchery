"""Actual console + HTTP cancellation + PostgreSQL + worker; no model calls."""
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlparse
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright,expect
import ai_jobs
import server
from local_postgres import LocalPostgres
from site_files_tests import FileFixture,USER


def main():
    with LocalPostgres() as pg:
        with pg.db() as conn,conn.cursor() as cur:
            cur.execute('''CREATE TABLE hatchery_ai_chat_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                title TEXT NOT NULL,messages_html TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        store=ai_jobs.JobStore(pg.db);store.initialize()
        entered,closing,release=(threading.Event() for _ in range(3))
        executions=[]
        def execute(job,progress):
            executions.append(job['id']);entered.set()
            try:
                while True:progress.check_cancelled();time.sleep(.01)
            finally:closing.set();release.wait(10)
        dispatcher=ai_jobs.Dispatcher(store,execute,1);dispatcher.start()
        try:
            with FileFixture() as fixture,patch.object(server,'AI_JOB_STORE',store),sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                try:
                    page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    def api(route):
                        path=urlparse(route.request.url).path
                        if path=='/api/ai/conversation':
                            route.fulfill(json={'session':store.session(USER['id'],ai_jobs.conversation_id(USER['id']))})
                        elif path=='/api/ai/run/active':
                            route.fulfill(json={'jobs':store.active(USER['id']),'queue':store.queue_status(USER['id'],1)})
                        else:route.continue_()
                    page.route('**/api/ai/**',api)
                    page.goto(f'http://127.0.0.1:{fixture.port}/')
                    page.wait_for_function('currentConsoleUser && fileSite?.format==="files" && !fileSiteLoading')
                    page.evaluate('aiServiceConfigured=true')
                    def send(text):
                        page.locator('#aiPrompt').fill(text)
                        with page.expect_response(lambda r:'/api/ai/run?async=1' in r.url and r.status==202) as response:
                            page.locator('#aiAdjustBtn').click()
                        page.wait_for_function('!Array.from(aiRuns.values()).some(r=>r.status==="submitting")')
                        page.evaluate('resumeAiRuns()')
                        return response.value.json()
                    first=send('build fixture');assert entered.wait(2)
                    second=send('queued fixture')
                    queued=page.locator(f'[data-ai-cancel="{second["jobId"]}"]')
                    expect(queued).to_have_text('取消排队');queued.click()
                    page.wait_for_function('(id)=>aiRuns.get(id)?.status==="cancelled"',arg=second['jobId'])
                    page.reload();page.wait_for_function('currentConsoleUser && !fileSiteLoading')
                    expect(page.locator(f'section[data-ai-job-id="{second["jobId"]}"]')).to_have_attribute('data-ai-status','cancelled')
                    button=page.locator(f'[data-ai-cancel="{first["jobId"]}"]')
                    expect(button).to_have_text('停止任务');button.click();assert closing.wait(2)
                    expect(page.locator(f'[data-ai-cancel="{first["jobId"]}"]')).to_be_disabled()
                    expect(page.locator(f'section[data-ai-job-id="{first["jobId"]}"]')).to_have_attribute('data-ai-status','running')
                    release.set()
                    deadline=time.monotonic()+4
                    while store.get(USER['id'],first['jobId'])['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
                    page.evaluate('resumeAiRuns()')
                    page.wait_for_function('!anyAiRunActive()')
                    assert executions==[first['jobId']],executions
                    assert not errors,errors
                    # A successful POST remains accepted if the following history
                    # request fails; it must not offer a duplicate model submission.
                    page.evaluate('aiServiceConfigured=true')
                    page.evaluate('()=>{window.fixtureSync=syncAiChatSessionFromServer;syncAiChatSessionFromServer=()=>Promise.reject(new Error("offline history"))}')
                    control=send('停停停')
                    assert control['status']=='completed'
                    assert not page.locator('.ai-admission-state').filter(has_text='提交未确认').count()
                    page.evaluate('()=>{syncAiChatSessionFromServer=window.fixtureSync}')
                    print(json.dumps({'checks':['queued cancellation never executes','running cancellation waits for worker exit','refresh preserves cancelled state','HTTP JSON control contract','accepted control survives history failure','no browser errors'],'realModelCalls':0},ensure_ascii=False))
                finally:browser.close()
        finally:
            release.set();dispatcher.shutdown(5);dispatcher.thread.join(3)


if __name__=='__main__':main()
