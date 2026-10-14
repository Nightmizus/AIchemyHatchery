"""ID-bound browser checks reusable in separately authorized real-AI runs.

No keywords classify task status. A timeout/unknown/failed task raises and stops
the sequence. Every submission has its own requestId and jobId in saved evidence.
"""
from pathlib import Path
import time


def submit(page, prompt):
    page.locator('#aiPrompt').fill(prompt)
    with page.expect_response(lambda r: '/api/ai/run?async=1' in r.url and r.request.method == 'POST') as response:
        page.locator('#aiAdjustBtn').click()
    reply = response.value
    assert reply.ok, f'Admission failed: HTTP {reply.status}'
    job = reply.json()
    assert job.get('jobId') and job.get('requestId') and job.get('sessionId')
    assert job['prompt'] == prompt
    return job


def wait_terminal(request, base, accepted, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = request.get(base + '/api/ai/run/status?id=' + accepted['jobId'])
        assert response.ok, f'Unknown task status: HTTP {response.status}; sequence stopped'
        job = response.json()
        for key in ('jobId', 'requestId', 'sessionId'): assert job[key] == accepted[key], f'Mismatched {key}'
        if job['status'] == 'completed': return job
        if job['status'] not in ('queued', 'running'):
            raise AssertionError(f'Task {job["jobId"]} ended {job["status"]}; sequence stopped')
        time.sleep(.1)
    raise TimeoutError(f'No terminal status for {accepted["jobId"]}; sequence stopped')


def assert_history(page, jobs):
    assert jobs
    sid = jobs[0]['sessionId']
    page.wait_for_function('(id)=>aiChatSessionId===id', arg=sid)
    page.wait_for_function('(ids)=>ids.every(id=>document.querySelector(`[data-ai-job-id="${id}"][data-ai-status="completed"]`))', arg=[j['jobId'] for j in jobs])
    assert page.url.endswith('/c/' + sid)
    assert page.locator('.ai-session-item.active').get_attribute('data-ai-session') == sid
    for job in jobs:
        user = page.locator(f'[data-ai-request-id="{job["requestId"]}"]')
        assert user.count() == 1 and user.locator('p').inner_text() == job['prompt']
        assert page.locator(f'[data-ai-job-id="{job["jobId"]}"]').count() == 1


def assert_interactions(page, downloads):
    downloads = Path(downloads); downloads.mkdir(exist_ok=True)
    counter = page.locator('#counter'); button = page.locator('#increment')
    assert counter.inner_text() == '0'
    button.click(click_count=3); assert counter.inner_text() == '3'
    button.focus(); page.keyboard.press('Enter'); page.keyboard.press('Space'); assert counter.inner_text() == '5'
    page.keyboard.press('Tab'); assert page.locator('#download').evaluate('(el)=>el===document.activeElement')
    with page.expect_download() as pending: page.locator('#download').click()
    pending.value.save_as(downloads / pending.value.suggested_filename)
    assert (downloads / pending.value.suggested_filename).read_text() == 'actual download contents\n'
    assert page.locator('body').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(240, 244, 255)'
    page.locator('#detail').click(); assert page.locator('h1').inner_text() == 'Details kept'
    page.get_by_text('Home', exact=True).click(); page.locator('#counter').wait_for()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.reload(); assert page.locator('#counter').inner_text() == '0'
    return ['external JS repeated clicks', 'Tab/Enter/Space', 'downloaded file contents', 'computed CSS', 'relative links round trip', 'mobile overflow', 'reload persisted files', 'publish fallback reachable']
