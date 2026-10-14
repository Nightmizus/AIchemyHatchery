"""Local browser + PostgreSQL regressions with a deterministic executor.

Actual application HTTP, queue, rendering, files and browser interactions are used.
The model boundary is a fixture: this is never evidence of real AI quality/load.
"""
from contextlib import ExitStack
import functools
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_postgres import LocalPostgres
from ai_regression_support import submit, wait_terminal, assert_history, assert_interactions
import ai_jobs
import server
import site_files
from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).resolve().parents[1]
USER = {'id': 'browser-regression', 'username': 'tester', 'role': 'user'}
PREVIEW = 'b' * 32


def main():
    checks = []
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='hatchery-browser-')))
        pg = stack.enter_context(LocalPostgres())
        stack.enter_context(patch.object(server, 'neon_db', pg.db))
        stack.enter_context(patch.object(server, 'NEON_DATABASE_URL', 'local-fixture-only'))
        stack.enter_context(patch.object(server, 'PUBLISHED', root / 'published'))
        stack.enter_context(patch.object(site_files, 'LOCK_ROOT', root / 'locks'))
        stack.enter_context(patch.object(server, 'user_preview_id', lambda uid: PREVIEW))
        stack.enter_context(patch.object(server, 'user_column_name', lambda name: None))
        stack.enter_context(patch.object(server, 'harness_status', lambda root: {'configured': True, 'contextWindow': 1000000}))
        server.initialize_database()
        with pg.db() as conn, conn.cursor() as cur:
            cur.execute('CREATE TABLE "User"(id TEXT PRIMARY KEY, name TEXT, "campusId" TEXT)')
            cur.execute('INSERT INTO "User" VALUES(%s,%s,%s)', (USER['id'], USER['username'], 'test-only'))
            cur.execute('INSERT INTO hatchery_user_extras VALUES(%s,%s,%s)', (USER['id'], PREVIEW, server.iso_time()))
        handler = object.__new__(server.AIchemyHatcheryHandler)
        directory, _ = handler.ensure_site_files(USER['id'])
        (directory / 'index.html').write_text('''<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="style.css"><h1>Fixture site</h1><p id="counter">0</p>
            <button id="increment">Increment</button><a id="download" href="answer.txt" download>Download</a>
            <a id="detail" href="details/">Details</a><script src="script.js"></script>''')
        (directory / 'style.css').write_text('body{background:rgb(240,244,255);max-width:100%;overflow-wrap:anywhere}')
        (directory / 'script.js').write_text("document.querySelector('#increment').onclick=()=>document.querySelector('#counter').textContent=Number(document.querySelector('#counter').textContent)+1")
        (directory / 'answer.txt').write_text('actual download contents\n')
        (directory / 'details').mkdir(); (directory / 'details/index.html').write_text('<h1>Details kept</h1><a href="../">Home</a>')
        def executor(**kwargs):
            time.sleep(.35)
            mode = kwargs['mode']
            prompt = kwargs['prompt'].split('当前请求：')[-1].split('\n当前网站文件')[0]
            kwargs['progress']({'id': 'fixture', 'kind': 'tool', 'label': 'Local regression executor', 'status': 'done'})
            if mode == 'full':
                with site_files.confined_path(kwargs['workspace'], 'index.html').open('a') as out:
                    out.write('<p>' + __import__('html').escape(prompt) + '</p>')
            return {'summary': ('失败判据只是文字；' * 200 if 'long' in prompt else 'fixture reply: ') + prompt,
                    'plan': 'local read-only plan' if mode == 'plan' else '',
                    'contextUsage': {'usedTokens': 42000 if 'session-b' in prompt else 17000, 'contextWindow': 1000000},
                    'checks': [], 'trace': [], 'finishReason': 'completed'}
        stack.enter_context(patch.object(server, 'run_deepseek_harness', executor))
        class Handler(server.AIchemyHatcheryHandler):
            def console_user(self): return dict(USER)
            def public_user(self, user): return dict(USER)
            def log_message(self, *args): pass
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(PROJECT)))
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        dispatcher = ai_jobs.Dispatcher(server.AI_JOB_STORE, server.execute_durable_ai_job, 2, root / 'results')
        dispatcher.start()
        def cleanup():
            dispatcher.stop.set(); dispatcher.thread.join(3)
            http.shutdown(); http.server_close(); thread.join(3)
        stack.callback(cleanup)
        base = f'http://127.0.0.1:{http.server_port}'
        with sync_playwright() as pw, ExitStack() as browser_stack:
            browser = pw.chromium.launch(headless=True)
            browser_stack.callback(browser.close)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page = context.new_page(); errors = []; sent = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: sent.append(request.post_data_json) if request.method == 'POST' and '/api/ai/sessions/' in request.url else None)
            page.goto(base); page.wait_for_function('fileSite?.format === "files" && currentConsoleUser !== null')
            jobs = []
            for prompt in ('step-1', 'step-2', 'long step-3'):
                job = submit(page, prompt); wait_terminal(context.request, base, job); jobs.append(job)
            session_a = jobs[0]['sessionId']
            assert all(j['sessionId'] == session_a for j in jobs)
            page.reload(); assert_history(page, jobs)
            assert len(page.locator('[data-ai-job-id]').last.locator('.ai-run-final p').first.inner_text()) > 1000
            checks += ['three distinct sequential submissions', 'full long reply', 'history retained after reload']
            tab = context.new_page(); tab.goto(page.url); assert_history(tab, jobs)
            first = submit(page, 'NEW-TAB-RESULT'); second = submit(tab, 'OLD-TAB-RESULT')
            for job in (first, second): wait_terminal(context.request, base, job); jobs.append(job)
            page.evaluate('resumeAiRuns()'); tab.evaluate('resumeAiRuns()')
            assert_history(page, jobs); assert_history(tab, jobs)
            body = (directory / 'index.html').read_text()
            assert 'NEW-TAB-RESULT' in body and 'OLD-TAB-RESULT' in body
            assert (directory / 'details/index.html').is_file()
            checks += ['two tabs preserve both edits and all request/reply IDs', 'unmodified details page retained']
            page.locator('#aiNewChatBtn').click()
            other = submit(page, 'session-b'); wait_terminal(context.request, base, other); assert_history(page, [other])
            session_b = other['sessionId']
            page.locator(f'[data-ai-session="{session_a}"]').click(); assert_history(page, jobs)
            page.locator(f'[data-ai-session="{session_b}"]').click(); assert_history(page, [other])
            page.go_back(); assert_history(page, jobs)
            assert page.evaluate('aiContextUsage().usedTokens') == 17000
            page.go_forward(); assert_history(page, [other])
            assert page.evaluate('aiContextUsage().usedTokens') == 42000
            checks += ['A/B back-forward URL, selected session, exact history and context ring']
            for mode in ('chat', 'plan'):
                before = site_files.file_manifest(directory)
                page.evaluate('(mode)=>{aiPermissionMode=mode;renderAiPermissionMode()}', mode)
                job = submit(page, f'{mode} readonly'); wait_terminal(context.request, base, job)
                assert site_files.file_manifest(directory) == before
            checks += ['chat/plan full file hashes unchanged']
            # Use an actual top-level published document for downloads/keyboard/mobile checks.
            page.evaluate('submitPublish("fixture-published",true)')
            assert '发布成功' in page.locator('#previewModal').inner_text()
            assert f'{base}/pages/fixture-published/' in page.locator('#previewModal').inner_text()
            gallery = context.request.get(base + '/gallery').text()
            assert 'href="/pages/fixture-published/"' in gallery
            preview = context.new_page(); preview.goto(base + '/pages/fixture-published/')
            checks += assert_interactions(preview, root / 'downloads')
            assert not any('messagesHtml' in item for item in sent)
            assert not errors, errors
            output = PROJECT / 'output/ai-fixes/20261013'; output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'browser-regressions.png'))
            (output / 'browser-validation.json').write_text(json.dumps({'kind': 'local deterministic executor; no real AI calls', 'passed': checks, 'jobIds': [j['jobId'] for j in jobs], 'errors': errors}, ensure_ascii=False, indent=2))
            print(json.dumps({'passed': checks, 'errors': errors}, ensure_ascii=False))


if __name__ == '__main__': main()
