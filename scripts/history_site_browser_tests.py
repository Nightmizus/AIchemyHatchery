"""Replay the recorded website's regressions on a read-only source copy.

Usage: python scripts/history_site_browser_tests.py /private/copied/cogito
Only disposable SQLite, files, credentials and loopback HTTP are used. No model
calls, production credentials, public requests or production writes.
"""
import base64
import http.client
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import quote, urlparse

from playwright.sync_api import sync_playwright, expect


def main(source):
    source = Path(source).resolve()
    checks = []
    spec = importlib.util.spec_from_file_location('recorded_website', source / 'backend/app.py')
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    with tempfile.TemporaryDirectory(prefix='hatchery-recorded-site-') as folder:
        root = Path(folder)
        site.DATABASE_PATH = str(root / 'site.sqlite3')
        site.UPLOAD_DIR = str(root / 'uploads')
        site.SUBMIT_DIR = str(root / 'submissions')
        site.SOCKET_PATH = str(root / 'service.sock')
        site.SEED_PATH = str(source / 'assets/data/seed.js')
        site.ADMIN_USERNAME = 'fixture-admin'
        site.ADMIN_PASSWORD = 'fixture-only-not-a-real-password'
        for directory in (site.UPLOAD_DIR, site.SUBMIT_DIR):
            Path(directory).mkdir()
        site.init_db()
        backend = site.ThreadedUnixServer(site.SOCKET_PATH, site.Handler)
        threading.Thread(target=backend.serve_forever, daemon=True).start()

        class UnixHTTP(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect(site.SOCKET_PATH)

        prefix = '/preview/' + 'Az09_-' * 5 + 'QZ/'

        class Static(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(source), **kwargs)

            def log_message(self, *args): pass

            def do_GET(self):
                if self.path.startswith(prefix):
                    self.path = '/' + self.path[len(prefix):]
                if self.path.startswith('/_api/'):
                    return self.proxy()
                super().do_GET()

            def do_POST(self):
                if self.path.startswith(prefix):
                    self.path = '/' + self.path[len(prefix):]
                assert self.path.startswith('/_api/')
                self.proxy()

            def proxy(self):
                conn = UnixHTTP('localhost')
                try:
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    conn.request(self.command, self.path[len('/_api'):], body=body,
                                 headers={k: v for k, v in self.headers.items() if k.lower() not in ('host', 'connection')})
                    response = conn.getresponse(); data = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ('server', 'date', 'connection'):
                            self.send_header(key, value)
                    self.end_headers(); self.wfile.write(data)
                finally: conn.close()

        static = ThreadingHTTPServer(('127.0.0.1', 0), Static)
        threading.Thread(target=static.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{static.server_port}'
        try:
            # Preserve the actual Unix peer address and HTTP keep-alive behavior.
            conn = UnixHTTP('localhost')
            conn.request('POST', '/admin/settings', body='{}', headers={'Content-Type': 'application/json'})
            response = conn.getresponse(); assert response.status == 401; response.read()
            conn.request('GET', '/healthz')
            response = conn.getresponse(); assert response.status == 200; response.read(); conn.close()
            checks.append('Unix-socket logging and unauthorized-body draining keep the next request healthy')

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/*', lambda route: route.continue_() if urlparse(route.request.url).hostname == '127.0.0.1' else route.abort())
                    for name in ('index', 'activities', 'reading', 'genealogy', 'toolbox', 'submissions'):
                        page.goto(base + prefix + name + '.html')
                        expect(page.locator('#datasource')).to_contain_text('实时同步')
                        assert not errors, (name, errors)
                    checks.append('all six public pages render live local data without JavaScript errors')

                    # Historical QR default/download and duplicated nav current.
                    site.save_settings({'qr_image': ''})
                    page.goto(base + prefix + 'index.html')
                    expect(page.locator('#datasource')).to_contain_text('实时同步')
                    src = page.locator('#join-qr').get_attribute('src')
                    assert src and page.locator('#join-btn-1').get_attribute('href') == src
                    assert page.locator('nav [aria-current="page"]').count() == 1
                    site.save_settings({'qr_image': '/uploads/fixture.svg'})
                    Path(site.UPLOAD_DIR, 'fixture.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>')
                    page.reload(); expect(page.locator('#datasource')).to_contain_text('实时同步')
                    assert prefix + '_api/uploads/fixture.svg' in page.locator('#join-qr').get_attribute('src')
                    assert page.locator('#join-btn-1').get_attribute('href') == page.locator('#join-qr').get_attribute('src')
                    checks.append('QR fallback, uploaded QR download and a single current navigation item')

                    site.save_settings({'home_cards': ['', '  ', None, 'fixture visible item'],
                                        'sub_rules': ['', '  ', None, 'fixture rule']})
                    page.reload(); expect(page.locator('#datasource')).to_contain_text('实时同步')
                    expect(page.locator('#home-cards li')).to_have_count(1)
                    page.goto(base + prefix + 'submissions.html')
                    expect(page.locator('#datasource')).to_contain_text('实时同步')
                    expect(page.locator('#sub-rules li')).to_have_count(1)
                    checks.append('blank/null historical list rows never render empty list items')

                    # Dynamic counters must reflect modified content, not literals.
                    graph = {'nodes': [{'id': 'test-a', 'works': [{'links': ['a', 'b']}]}, {'id': 'test-b', 'works': []}], 'edges': [{'from': 'test-a', 'to': 'test-b'}]}
                    site.save_settings({'genealogy': graph})
                    page.goto(base + prefix + 'index.html'); expect(page.locator('#home-gx-people')).to_have_text('2')
                    expect(page.locator('#home-gx-edges')).to_have_text('1')
                    checks.append('home graph counters follow edited data')
                    site.save_settings({'genealogy': ''})
                    page.goto(base + prefix + 'genealogy.html#p-descartes')
                    expect(page.locator('#p-descartes')).to_be_in_viewport()
                    checks.append('rendered person anchor scrolls into view')

                    connection = site.db()
                    try:
                        connection.execute('UPDATE resources SET reading_id=NULL')
                        connection.commit()
                    finally: connection.close()
                    page.goto(base + prefix + 'toolbox.html')
                    expect(page.locator('#datasource')).to_contain_text('实时同步')
                    terms = site.read_content()['readings']
                    assert terms
                    for term in terms:
                        expect(page.locator('#term-' + str(term['id']))).to_contain_text('补充资源待补')
                    checks.append('terms without resources keep their link targets')

                    page.goto(base + prefix + 'admin.html')
                    page.locator('#username').fill(site.ADMIN_USERNAME)
                    page.locator('#password').fill(site.ADMIN_PASSWORD)
                    page.locator('#login-form button[type="submit"]').click()
                    expect(page.locator('#dash-view')).to_be_visible()
                    for tab in ('home', 'activities', 'readings', 'genealogy', 'resources', 'gifts', 'review', 'submissions', 'settings', 'account'):
                        page.locator(f'[data-tab="{tab}"]').click()
                        expect(page.locator(f'[data-tab="{tab}"]')).to_have_attribute('aria-selected', 'true')
                        assert page.locator('#tab-panel').inner_text().strip()
                    assert not errors, errors
                    checks.append('all ten admin tabs render; historical selector forEach errors absent')

                    # Real request flow with a new test-only account/session.
                    request = page.request
                    login = request.post(base + '/_api/login', data={'username': site.ADMIN_USERNAME, 'password': site.ADMIN_PASSWORD}).json()
                    headers = {'Authorization': 'Bearer ' + login['token']}
                    data = {'title': 'fixture submission', 'body': 'private pending text', 'contact': 'private fixture contact',
                            'files': [{'name': '中文稿件.pdf', 'data_base64': base64.b64encode(b'%PDF-1.4 fixture').decode()}]}
                    submitted = request.post(base + '/_api/submit', data=data).json()
                    assert submitted['ok'], submitted
                    sid = submitted['submission']['id']
                    public = request.get(base + '/_api/content').json()
                    assert all(row['id'] != sid for row in public['submissions'])
                    pending = next(row for row in request.get(base + '/_api/admin/all', headers=headers).json()['submissions'] if row['id'] == sid)
                    file_url = base + '/_api/files/' + quote(pending['attachments'][0]['key'])
                    assert request.get(file_url).status == 404
                    authorized = request.get(file_url, headers=headers)
                    assert authorized.status == 200
                    assert "filename*=UTF-8''" in authorized.headers['content-disposition']
                    assert request.post(base + '/_api/admin/review', headers=headers, data={'id': sid, 'action': 'approve'}).json()['ok']
                    public = request.get(base + '/_api/content').json()
                    published = next(row for row in public['submissions'] if row['id'] == sid)
                    assert 'contact' not in published and 'ip' not in published
                    assert request.get(file_url).status == 200
                    assert request.post(base + '/_api/admin/review', headers=headers, data={'id': sid, 'action': 'reject'}).json()['ok']
                    assert request.get(file_url).status == 404
                    checks.append('pending privacy, human approve/reject, Chinese filename and attachment deletion')
                    upload = request.get(base + '/_api/uploads/fixture.svg')
                    assert "sandbox; script-src 'none'" in upload.headers['content-security-policy']
                    checks.append('active uploaded content is served with sandbox CSP')
                    assert not errors, errors
                finally: browser.close()
        finally:
            static.shutdown(); static.server_close()
            backend.shutdown(); backend.server_close()
    print(json.dumps({'passed': len(checks), 'checks': checks, 'productionWrites': 0, 'realModelCalls': 0}, ensure_ascii=False))


if __name__ == '__main__': main(sys.argv[1])
