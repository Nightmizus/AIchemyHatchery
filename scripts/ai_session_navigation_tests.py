"""Browser navigation regressions with held HTTP responses and fixture conversations.

No model requests or production data. Uses FileFixture for the local application.
"""
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from site_files_tests import FileFixture
import ai_jobs

ROOT = Path(__file__).resolve().parents[1]
A, B, C = ('a' * 32, 'b' * 32, 'c' * 32)


def session(sid, text, tokens=17000):
    job = {'jobId': 'fixture-' + sid, 'requestId': sid, 'sessionId': sid, 'status': 'completed',
           'prompt': 'prompt-' + sid[0], 'events': [], 'mode': 'chat',
           'result': {'summary': text, 'checks': ['REMOVED-CHECK'],
                      'contextUsage': {'usedTokens': tokens, 'contextWindow': 1000000}}}
    return {'id': sid, 'title': '对话 ' + sid[0], 'messagesHtml': ai_jobs.render_history('', [job]),
            'jobs': [job], 'revision': 1, 'serverOwned': True}


def main():
    reports = []
    with FileFixture() as fixture, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            errors, requests, pending = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))

            def hold(route):
                pending.append((route.request.url.rsplit('/', 1)[-1], route))

            page.route(re.compile(r'/api/ai/sessions/[0-9a-f-]+$'), hold)
            page.goto(f'http://127.0.0.1:{fixture.port}/')
            page.wait_for_function('fileSite?.format === "files" && currentConsoleUser !== null')
            page.wait_for_load_state('networkidle')
            # Exercise polling explicitly so unrelated timer ticks don't choose
            # the ordering of the held responses below.
            page.evaluate('resumeAiRuns=async()=>{}')
            page.evaluate('(ids)=>{aiSessionList=ids.map(id=>({id,title:id[0]}));renderAiSessionList()}', [A, B])

            def take(sid):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    for i, (target, route) in enumerate(pending):
                        if target == sid:
                            pending.pop(i)
                            return route
                    page.wait_for_timeout(20)
                raise AssertionError('request not issued: ' + sid)

            def finish(route, data=None, status=200):
                route.fulfill(status=status, json={'session': data} if data else {'error': 'fixture failure'})
                page.wait_for_timeout(30)

            def visible(text):
                page.wait_for_function('(text)=>document.querySelector("#aiChatMessages").textContent.includes(text)', arg=text)

            def click(sid):
                return page.evaluate('''id=>{
                    const start=performance.now();document.querySelector(`[data-ai-session="${id}"]`).click();
                    return {elapsedMs:performance.now()-start,text:document.querySelector('#aiChatMessages').textContent,
                            active:document.querySelector('.ai-session-item.active')?.dataset.aiSession,
                            tokens:aiContextUsage().usedTokens};
                }''', sid)

            page.evaluate('void prefetchAiSessionHtml()')
            first_a = take(A)
            assert '正在加载对话' in click(A)['text']
            page.evaluate('(id)=>void syncAiChatSessionFromServer(id)', A)
            page.wait_for_timeout(50)
            assert sum(url.endswith('/sessions/' + A) for url in requests) == 1
            finish(first_a, session(A, 'answer-A'))
            visible('answer-A')
            finish(take(B), session(B, 'answer-B', 42000))
            reports.append('prefetch, selection and polling share one request')

            fast = click(B)
            assert 'answer-B' in fast['text'] and fast['tokens'] == 42000 and fast['active'] == B, fast
            assert fast['elapsedMs'] < 100, fast
            held_b = take(B)
            page.wait_for_timeout(1200)
            assert 'answer-B' in page.locator('#aiChatMessages').inner_text()
            fast_a = click(A)
            assert 'answer-A' in fast_a['text'] and fast_a['tokens'] == 17000
            held_a = take(A)
            finish(held_b, session(B, 'answer-B-updated', 43000))
            assert 'answer-A' in page.locator('#aiChatMessages').inner_text()
            finish(held_a, session(A, 'answer-A'))
            updated = click(B)
            assert 'answer-B-updated' in updated['text'] and updated['tokens'] == 43000
            finish(take(B), session(B, 'answer-B-updated', 43000))
            reports.append({'cachedSwitchMs': round(fast['elapsedMs'], 2), 'heldResponseMs': 1200})
            reports.append('late background response updates its cache without replacing another conversation')

            page.go_back()
            visible('answer-A')
            back_a = take(A)
            page.go_forward()
            visible('answer-B-updated')
            forward_b = take(B)
            finish(back_a, status=404)
            assert page.url.endswith('/c/' + B)
            visible('answer-B-updated')
            finish(forward_b, session(B, 'answer-B-updated', 43000))
            reports.append('back/forward uses cache; stale 404 cannot change the selected conversation')

            # A fresh read after submission must supersede an older in-flight snapshot.
            click(A)
            stale_a = take(A)
            page.evaluate('(id)=>void syncAiChatSessionFromServer(id,{force:true})', A)
            fresh_a = take(A)
            finish(fresh_a, session(A, 'answer-A-fresh'))
            visible('answer-A-fresh')
            finish(stale_a, session(A, 'answer-A-obsolete'))
            assert 'answer-A-fresh' in page.locator('#aiChatMessages').inner_text()
            assert page.evaluate('(id)=>aiSessionHtmlCache.get(id).includes("answer-A-fresh")', A)
            reports.append('submission refresh supersedes older responses in both DOM and cache')

            page.evaluate('(id)=>{aiSessionList.push({id,title:"cold conversation"});renderAiSessionList()}', C)
            cold = click(C)
            assert '正在加载对话' in cold['text'] and 'answer-A' not in cold['text']
            finish(take(C), status=503)
            page.locator('[data-ai-session-retry]').click()
            finish(take(C), session(C, 'answer-C'))
            visible('answer-C')
            assert 'AI 提供的检查说明' not in page.locator('#aiChatMessages').inner_text()
            assert 'REMOVED-CHECK' not in page.locator('#aiChatMessages').inner_text()
            reports.append('uncached selection clears the old conversation and supports retry; check section removed')

            click(B)
            deleted_b = take(B)
            page.locator(f'[data-ai-session-delete="{B}"]').click()
            page.wait_for_function('(id)=>aiSessionDeleted.has(id)', arg=B)
            finish(deleted_b, session(B, 'deleted-answer'))
            assert not page.evaluate('(id)=>aiSessionHtmlCache.has(id)', B)
            reports.append('deleted conversation cannot return through an in-flight cache fill')

            click(A)
            account_a = take(A)
            page.evaluate('showAuthGate()')
            finish(account_a, session(A, 'prior-account-answer'))
            assert page.evaluate('aiSessionHtmlCache.size===0 && aiSessionRequests.size===0')
            assert not errors, errors
            assert not any('/api/ai/run?async=' in url for url in requests)
            reports.append('logout discards pending responses and clears conversation cache')
            out = ROOT/'output/conversation-switch-fix'
            out.mkdir(parents=True, exist_ok=True)
            (out/'browser-checks.json').write_text(json.dumps({'passed': reports, 'errors': errors, 'realModelCalls': 0}, ensure_ascii=False, indent=2))
            print(json.dumps({'passed': reports, 'errors': errors}, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
