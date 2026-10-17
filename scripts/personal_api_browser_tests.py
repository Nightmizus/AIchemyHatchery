"""Browser integration against an isolated account fixture; no production services."""
from contextlib import ExitStack
import functools
import os
from pathlib import Path
import socket
import threading
from unittest.mock import patch

from personal_api_tests import Fixture, PUBLIC_DNS, REAL_GETADDRINFO, DATA
import server
import personal_api
from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).resolve().parents[1]


def main():
    with ExitStack() as stack:
        fixture = stack.enter_context(Fixture())
        stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=lambda host, *a, **k:
                            REAL_GETADDRINFO(host, *a, **k) if host == '127.0.0.1' else PUBLIC_DNS))
        stack.enter_context(patch.object(personal_api, 'test_connection', return_value=None))
        class Handler(server.AIchemyHatcheryHandler):
            def console_user(self):
                uid = self.headers.get('X-Test-User', 'one')
                return {'id': uid, 'username': uid, 'role': 'user', 'previewId': 'a' * 32}
            def log_message(self, *args): pass
            def _do_GET(self):
                path = self.path.split('?')[0]
                if path == '/api/auth/me':
                    self.send_json({'authenticated': True, 'user': self.console_user()}); return
                if path == '/api/site/files':
                    self.send_json({'format': 'files', 'pages': ['index.html'], 'metadata': {'siteName': 'Test site'}, 'slug': '', 'previewUrl': '/preview/' + 'a' * 32 + '/'}); return
                if path == '/api/ai/conversation':
                    self.send_json({'session': {'id': server.ai_jobs.conversation_id(self.console_user()['id']), 'messagesHtml': '', 'jobs': [], 'revision': 0}}); return
                if path == '/api/ai/run/active':
                    self.send_json({'jobs': []}); return
                if path.startswith('/preview/'):
                    self.send_json({'preview': True}); return
                super()._do_GET()
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(PROJECT)))
        threading.Thread(target=http.serve_forever, daemon=True).start()
        stack.callback(http.server_close); stack.callback(http.shutdown)
        playwright = stack.enter_context(sync_playwright())
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        stack.callback(browser.close)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        base = f'http://127.0.0.1:{http.server_port}'
        page.goto(base)
        page.locator('#authGate').wait_for(state='hidden')
        page.locator('#consoleAccountBtn').click()
        page.get_by_role('button', name='我的 API', exact=True).click()
        page.locator('#personalApiForm').wait_for()
        page.get_by_label('Base URL', exact=True).fill('http://api.example.com/v1')
        page.get_by_label('API Key', exact=False).fill(DATA['apiKey'])
        page.get_by_label('模型 ID', exact=True).fill(DATA['model'])
        page.get_by_role('button', name='保存配置', exact=True).click()
        page.get_by_role('alert').filter(has_text='HTTPS').wait_for()
        page.get_by_label('Base URL', exact=True).fill(DATA['baseUrl'])
        page.get_by_role('button', name='保存配置', exact=True).click()
        page.get_by_text('已保存，将用于接下来的请求。', exact=True).wait_for()
        assert page.get_by_label('API Key', exact=False).input_value() == ''
        assert fixture.store.load('one')['api_key'] == DATA['apiKey']
        page.get_by_role('button', name='测试已保存的连接', exact=True).click()
        page.get_by_text('连接成功，模型已在列表中找到；尚未测试生成和工具调用', exact=True).wait_for()
        screenshot = Path(os.environ.get('PERSONAL_API_SCREENSHOTS', PROJECT / 'output' / 'playwright'))
        screenshot.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot / 'personal-api-desktop.png'), full_page=True)
        page.reload()
        page.locator('#authGate').wait_for(state='hidden')
        page.locator('#consoleAccountBtn').click()
        page.get_by_role('button', name='我的 API', exact=True).click()
        page.locator('#personalApiForm').wait_for()
        assert page.get_by_label('Base URL', exact=True).input_value() == DATA['baseUrl']
        page.get_by_label('模型 ID', exact=True).fill('next-model')
        page.get_by_role('button', name='保存配置', exact=True).click()
        page.get_by_text('已保存，将用于接下来的请求。', exact=True).wait_for()
        assert fixture.store.load('one')['api_key'] == DATA['apiKey']
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.locator('#personalApiForm').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(screenshot / 'personal-api-mobile.png'), full_page=True)
        page.get_by_role('button', name='清除并恢复默认服务', exact=True).click()
        page.get_by_text('已恢复默认服务。', exact=True).wait_for()
        assert fixture.store.load('one') is None
        # Reproduce actual key-file loss while ciphertext remains in the database.
        def reopen():
            page.locator('#closeModalBtn').click()
            page.locator('#previewModal').wait_for(state='hidden')
            page.locator('#consoleAccountBtn').click()
            page.get_by_role('button', name='我的 API', exact=True).click()
            page.locator('#personalApiForm').wait_for()

        fixture.store.save('one', DATA)
        (fixture.root / '.private-api-config' / 'key').unlink()
        reopen()
        page.get_by_role('alert').filter(has_text='无法解密').wait_for()
        assert page.get_by_role('button', name='清除并恢复默认服务', exact=True).is_enabled()
        assert page.get_by_label('API Key', exact=False).get_attribute('required') is not None
        page.get_by_label('Base URL', exact=True).fill(DATA['baseUrl'])
        page.get_by_label('API Key', exact=False).fill('recovered-key')
        page.get_by_label('模型 ID', exact=True).fill(DATA['model'])
        page.get_by_role('button', name='保存配置', exact=True).click()
        page.get_by_text('已保存，将用于接下来的请求。', exact=True).wait_for()
        assert fixture.store.load('one')['api_key'] == 'recovered-key'
        assert page.get_by_label('API Key', exact=False).input_value() == ''
        (fixture.root / '.private-api-config' / 'key').unlink()
        reopen()
        page.get_by_role('alert').filter(has_text='无法解密').wait_for()
        page.get_by_role('button', name='清除并恢复默认服务', exact=True).click()
        page.get_by_text('已恢复默认服务。', exact=True).wait_for()
        assert fixture.store.load('one') is None
        assert not errors, errors
        print('Browser PASS: settings, validation, key secrecy, reload, mobile, lost-key reconfiguration and clear; no JavaScript errors')


if __name__ == '__main__': main()
