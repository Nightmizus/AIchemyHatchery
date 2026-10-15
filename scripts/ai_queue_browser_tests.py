"""Real console/browser delivery regressions; HTTP admission is a fixture, no model calls.

Run with the Python environment containing Playwright and the app dependencies.
The actual dispatcher/concurrency is separately checked in ai_jobs_tests.py.
"""
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from site_files_tests import FileFixture
import ai_jobs

SESSION = '11111111-1111-4111-8111-111111111111'


def main():
    checks = []
    jobs = {}; calls = []; failures = {'lose_ack': False, 'offline': False}
    capacity = {'running': 2, 'queued': 1, 'maxParallel': 2, 'ownQueued': 1, 'ahead': 0, 'reason': 'capacity'}
    with FileFixture() as fixture, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route_api(route):
                request = route.request; parsed = urlparse(request.url)
                if parsed.path == '/api/ai/conversation':
                    data = {'session': {'id': SESSION, 'jobs': list(jobs.values()),
                                        'messagesHtml': ai_jobs.render_history('', list(jobs.values()))}}
                elif parsed.path == '/api/ai/run/active':
                    data = {'jobs': list(jobs.values()), 'queue': capacity}
                elif parsed.path == '/api/ai/run/ack':
                    data = {'ok': True}
                elif parsed.path == '/api/ai/run/status':
                    job = jobs.get((parse_qs(parsed.query).get('id') or [''])[0])
                    route.fulfill(status=200 if job else 404, json=job or {'error': 'No such task'})
                    return
                elif parsed.path == '/api/ai/run' and request.method == 'POST':
                    payload = request.post_data_json; calls.append(payload)
                    if failures['offline']:
                        route.abort('timedout'); return
                    job = next((job for job in jobs.values() if job['requestId'] == payload['requestId']), None)
                    if job is None:
                        job = {'jobId': 'job-' + f'{len(jobs)+1:024x}', 'requestId': payload['requestId'],
                               'sessionId': SESSION, 'status': 'queued', 'events': [], 'mode': payload['mode'],
                               'prompt': payload['prompt'], 'createdAt': '2026-10-15T00:00:00Z', 'result': None}
                        jobs[job['jobId']] = job
                    if failures['lose_ack']:
                        failures['lose_ack'] = False
                        route.fulfill(status=503, body='temporary gateway error'); return
                    route.fulfill(status=202, json=job); return
                else:
                    route.continue_(); return
                route.fulfill(json=data)
            page.route('**/api/ai/**', route_api)
            page.goto(f'http://127.0.0.1:{fixture.port}/')
            page.wait_for_function('currentConsoleUser !== null && fileSite?.format === "files" && !fileSiteLoading')
            def send(text):
                page.locator('#aiPrompt').fill(text)
                with page.expect_response(lambda response: '/api/ai/run?async=1' in response.url and response.status == 202):
                    page.locator('#aiAdjustBtn').click()
                page.wait_for_function('!Array.from(aiRuns.values()).some(run=>run.status==="submitting")')
                page.evaluate('resumeAiRuns()')
            send('first request')
            send('second request while another task is queued')
            assert len(jobs) == 2 and len({call['requestId'] for call in calls}) == 2
            page.wait_for_function('document.querySelector(".ai-working-status")?.textContent.includes("并发 2/2")')
            assert page.evaluate('typeof enqueueAiRun') == 'undefined'
            checks += ['every click admitted despite existing queued work', 'server capacity reason visible', 'no browser-only execution queue']
            for job in jobs.values():
                job.update(status='completed', result={'summary': 'fixture completed', 'changedFiles': []})
            page.evaluate('resumeAiRuns()')
            page.wait_for_function('!anyAiRunActive()')
            page.evaluate("rememberServerJob({jobId:'job-ffffffffffffffffffffffff',status:'queued',mode:'full',createdAt:new Date().toISOString()})")
            page.evaluate('resumeAiRuns()')
            page.wait_for_function('!anyAiRunActive()')
            assert page.evaluate("!aiRuns.has('job-ffffffffffffffffffffffff')")
            checks += ['terminal state releases waiting', 'authoritative 404 clears phantom queued task']
            failures['lose_ack'] = True
            prior = len(calls)
            send('acknowledgement lost after database acceptance')
            attempts = calls[prior:]
            assert len(attempts) == 2 and attempts[0]['requestId'] == attempts[1]['requestId']
            assert len(jobs) == 3
            checks += ['lost acknowledgement retries same requestId exactly once in server fixture']
            for job in jobs.values():
                job.update(status='completed', result={'summary': 'fixture completed'})
            page.evaluate('resumeAiRuns()')
            page.wait_for_function('!anyAiRunActive()')
            failures['offline'] = True
            page.locator('#aiPrompt').fill('network failure must not become queued')
            prior = len(calls)
            page.locator('#aiAdjustBtn').click()
            page.wait_for_function('!anyAiRunActive() && document.querySelector("#aiPrompt").value.includes("network failure")', timeout=18000)
            failed = calls[prior:]
            assert len(failed) == 4 and len({call['requestId'] for call in failed}) == 1
            assert len(jobs) == 3
            failures['offline'] = False
            send('network failure must not become queued')
            assert calls[-1]['requestId'] == failed[0]['requestId']
            checks += ['offline admission is not server queue', 'failed message preserved and retry is idempotent']
            for job in jobs.values():
                job.update(status='completed', result={'summary': 'fixture completed'})
            page.reload()
            page.wait_for_function('!anyAiRunActive() && currentConsoleUser !== null')
            page.wait_for_function('(n)=>document.querySelectorAll("[data-ai-job-id]").length===n', arg=len(jobs))
            assert not errors, errors
            checks += ['reload recovers authoritative history with no duplicate execution']
            print(json.dumps({'passed': checks, 'errors': errors, 'realModelCalls': 0}, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
