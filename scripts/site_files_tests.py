"""File/HTTP/publish regressions. Uses a temporary SQLite index; no simulated AI."""
from contextlib import contextmanager, ExitStack
import functools
import http.client
import json
import os
import hashlib
import re
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import personal_api
import gallery
import server
import site_files

PROJECT = Path(__file__).resolve().parents[1]
USER = {'id': 'file-test-user', 'username': 'tester', 'role': 'user'}
PREVIEW = 'a' * 32


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, params=()):
        self.cursor.execute(sql.replace('%s', '?'), params)
    def fetchone(self): return self.cursor.fetchone()


class Connection:
    def __init__(self, connection): self.connection = connection
    def cursor(self): return Cursor(self.connection)


class FileFixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='hatchery-files-')))
        self.published = self.root / 'published'
        self.db = sqlite3.connect(self.root / 'index.db', check_same_thread=False)
        self.db.executescript('''
        CREATE TABLE hatchery_published_sites(slug TEXT PRIMARY KEY, user_id TEXT UNIQUE, created_at TEXT, updated_at TEXT, listed_in_gallery BOOLEAN);
        CREATE TABLE hatchery_user_extras(user_id TEXT PRIMARY KEY, preview_id TEXT);
        CREATE TABLE hatchery_site_previews(user_id TEXT PRIMARY KEY, data_json TEXT);
        CREATE TABLE "User"(id TEXT PRIMARY KEY);
        ''')
        self.db.executescript(personal_api.SCHEMA)
        self.db.executescript(gallery.SCHEMA)
        for table in ('users', 'sessions', 'settings', 'invitations', 'audit_log', 'deployments'):
            self.db.execute(f'CREATE TABLE hatchery_site_{table}(site_username TEXT, value TEXT)')
        self.db.execute('INSERT INTO hatchery_user_extras VALUES (?,?)', (USER['id'], PREVIEW))
        self.db.execute('INSERT INTO "User" VALUES (?)', (USER["id"],))
        self.db.commit()
        self.db_lock = threading.RLock()
        @contextmanager
        def database(autocommit=False):
            with self.db_lock:
                try:
                    yield Connection(self.db)
                    if not autocommit:
                        self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
        self.stack.enter_context(patch.object(server, 'user_column_name', lambda name: None))
        self.stack.enter_context(patch.object(server, 'PUBLISHED', self.published))
        self.stack.enter_context(patch.object(server, 'neon_db', database))
        self.stack.enter_context(patch.object(server, 'user_preview_id', lambda uid: PREVIEW))
        self.stack.enter_context(patch.object(server, 'audit_event', lambda *a, **k: None))
        self.stack.enter_context(patch.object(server, 'rate_limit_hit', lambda *a, **k: False))
        fixture = self
        class Handler(server.AIchemyHatcheryHandler):
            def console_user(self): return dict(USER)
            def log_message(self, *args): pass
            def attach_preview_id(self, payload, user_id):
                payload.update(previewId=PREVIEW)
                return payload
            def _do_GET(self):
                # These UI services are unrelated to filesystem tests. No AI response is intercepted.
                path = self.path.split('?')[0]
                if path == '/api/auth/me':
                    self.send_json({'authenticated': True, 'user': {**USER, 'previewId': PREVIEW}}); return
                if path in ('/api/ai/sessions', '/api/ai/run/active'):
                    self.send_json({'sessions': [], 'jobs': []}); return
                if path == '/api/console/draft':
                    self.send_json({'draft': None}); return
                super()._do_GET()
            def do_POST(self):
                if self.path.startswith('/api/ai/sessions'):
                    self.read_json(); self.send_json({'ok': True}); return
                super().do_POST()
        self.Handler = Handler
        self.handler = object.__new__(Handler)
        self.handler.require_console_user = lambda: dict(USER)
        self.handler.command = 'POST'
        self.responses = []
        self.handler.send_json = lambda data, status=200: self.responses.append((status, data))
        self.directory, _ = self.handler.ensure_site_files(USER['id'])
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(PROJECT)))
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        return self
    def request(self, path, method='GET', host=None, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        extra = {'Host': host} if host else {}
        if body is not None: extra['Content-Type'] = 'application/json'
        extra.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, extra)
        res = conn.getresponse(); result = (res.status, dict(res.getheaders()), res.read()); conn.close()
        return result
    def publish(self, slug, **values):
        self.handler.read_json = lambda: {'slug': slug, **values}
        self.handler.handle_publish()
        return self.responses[-1]
    def __exit__(self, *args):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2)
        self.db.close(); self.stack.close()


class FilesTests(unittest.TestCase):
    def test_ai_status_explicitly_reports_no_total_execution_limit(self):
        with FileFixture() as fixture:
            status, _, body = fixture.request('/api/ai/status')
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertIn('executionTimeoutSeconds', payload)
            self.assertIsNone(payload['executionTimeoutSeconds'])

    def test_console_shell_and_script_versions_cannot_remain_stale(self):
        with FileFixture() as f:
            version = hashlib.sha256((PROJECT / 'frontend/script.js').read_bytes()).hexdigest()[:20]
            for path in ('/', '/index.html', '/c/11111111-1111-4111-8111-111111111111'):
                status, headers, body = f.request(path)
                self.assertEqual(status, 200)
                self.assertIn('no-store', headers['Cache-Control'])
                self.assertIn('/script.js?v=' + version, body.decode())
                self.assertEqual(f.request(path, 'HEAD')[2], b'')
            for asset in ('markdown-it.min.js', 'ai-markdown.js'):
                self.assertIn('/' + asset + '?v=', body.decode())
                self.assertEqual(f.request('/' + asset)[0], 200)
            for path in ('/script.js', '/script.js?v=old-browser-version'):
                self.assertIn('no-store', f.request(path)[1]['Cache-Control'])
            status, headers, body = f.request('/script.js?v=' + version)
            self.assertEqual(status, 200)
            self.assertIn('immutable', headers['Cache-Control'])
            self.assertEqual(body, (PROJECT / 'frontend/script.js').read_bytes())

    def test_asset_version_is_derived_from_content_without_manual_version_bumps(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'ROOT', Path(folder)):
            target = Path(folder) / 'frontend/script.js'; target.parent.mkdir()
            target.write_text('first build')
            first = server.console_asset_version('/script.js')
            target.write_text('second build')
            self.assertNotEqual(server.console_asset_version('/script.js'), first)

    def test_metadata_only_and_files_survive_reinitialize(self):
        with FileFixture() as f:
            index = f.directory / 'index.html'; index.write_text('<h1>direct edit</h1>')
            f.handler.ensure_site_files(USER['id'], {'pages': [{'html': 'stale'}]})
            self.assertEqual(index.read_text(), '<h1>direct edit</h1>')
            site_files.save_metadata(f.directory, {'siteName': '名字', 'pages': ['ignored']})
            self.assertEqual(set(site_files.read_metadata(f.directory)), site_files.META_FIELDS)
            status, info = f.publish('alpha', pages=[{'html': 'stale'}])
            self.assertEqual(status, 200)
            self.assertEqual((f.published / 'alpha/index.html').read_text(), '<h1>direct edit</h1>')

    def test_publish_rename_preserves_files_and_member_rows_and_collision(self):
        with FileFixture() as f:
            (f.directory / 'nested').mkdir(); (f.directory / 'nested/data.bin').write_bytes(b'\x00\xff')
            self.assertEqual(f.publish('alpha')[0], 200)
            f.db.execute("INSERT INTO hatchery_site_users VALUES ('alpha','existing-member')"); f.db.commit()
            self.assertEqual(f.publish('bravo')[0], 200)
            self.assertFalse((f.published / 'alpha').exists())
            self.assertEqual((f.published / 'bravo/nested/data.bin').read_bytes(), b'\x00\xff')
            self.assertEqual(f.db.execute('SELECT site_username,value FROM hatchery_site_users').fetchone(), ('bravo', 'existing-member'))
            f.db.execute("INSERT INTO hatchery_published_sites VALUES ('taken','other','','',1)"); f.db.commit()
            self.assertEqual(f.publish('taken')[0], 409)
            (f.published / 'orphan').mkdir()
            self.assertEqual(f.publish('orphan')[0], 409)
            self.assertTrue((f.published / 'bravo/index.html').exists())
            self.assertNotEqual(site_files.draft_directory(f.published, 'a/b'), site_files.draft_directory(f.published, 'ab'))

    def test_publish_failure_rolls_back_directory_and_metadata(self):
        with FileFixture() as f:
            original = (f.directory / 'site.json').read_bytes()
            with patch.object(server, 'audit_event', side_effect=RuntimeError('transaction failed')):
                with self.assertRaises(RuntimeError): f.publish('alpha')
            self.assertFalse((f.published / 'alpha').exists())
            self.assertEqual((f.directory / 'site.json').read_bytes(), original)
            self.assertEqual(f.db.execute('SELECT count(*) FROM hatchery_published_sites').fetchone()[0], 0)

    def test_static_http_assets_head_paths_and_isolation(self):
        with FileFixture() as f:
            (f.directory / 'script.js').write_text('window.siteScript=true')
            (f.directory / 'about').mkdir(); (f.directory / 'about/index.html').write_text('<h1>About</h1>')
            (f.root / 'secret.txt').write_text('private')
            (f.directory / 'escape').symlink_to(f.root)
            f.publish('alpha')
            slug_host = f'alpha.{server.SITE_BASE_DOMAIN}'
            status, headers, body = f.request('/script.js', host=slug_host)
            self.assertEqual((status, body), (200, b'window.siteScript=true'))
            self.assertIn('javascript', headers['Content-Type'])
            status, headers, body = f.request('/pages/alpha/script.js', 'HEAD')
            self.assertEqual((status, body), (200, b''))
            self.assertIn('sandbox', headers['Content-Security-Policy'])
            self.assertEqual(f.request('/pages/alpha')[0], 308)
            self.assertEqual(f.request('/pages/alpha/about')[1]['Location'], '/pages/alpha/about/')
            self.assertTrue(f.request(f'/preview/{PREVIEW}/about/')[2].startswith(b'<h1>About</h1>'))
            self.assertEqual((f.published / 'alpha/about/index.html').read_bytes(), b'<h1>About</h1>')
            status, headers, body = f.request('/pages/alpha/about/?__hatchery_download=about.html')
            self.assertEqual((status, body), (200, b'<h1>About</h1>'))
            self.assertIn("filename*=UTF-8''about.html", headers['Content-Disposition'])
            for path in ('../secret.txt', '%2e%2e/secret.txt', 'escape/secret.txt', 'site.json', 'runtime.json', '.env', 'missing.html'):
                self.assertEqual(f.request('/pages/alpha/' + path)[0], 404, path)

    def test_published_assets_cache_and_previews_always_revalidate(self):
        with FileFixture() as f:
            (f.directory / 'script.js').write_text('window.siteScript=true')
            (f.directory / 'about').mkdir(); (f.directory / 'about/index.html').write_text('<h1>About</h1>')
            f.publish('alpha')
            slug_host = f'alpha.{server.SITE_BASE_DOMAIN}'
            status, headers, body = f.request('/script.js', host=slug_host)
            self.assertEqual((status, body), (200, b'window.siteScript=true'))
            self.assertEqual(headers['Cache-Control'], 'public, max-age=300')
            self.assertIn('ETag', headers); self.assertIn('Last-Modified', headers)
            status, headers, body = f.request('/script.js', host=slug_host, headers={'If-None-Match': headers['ETag']})
            self.assertEqual((status, body), (304, b''))
            # nginx 压缩后会把 ETag 变弱，浏览器回传的是 W/"..."
            status, headers, body = f.request('/script.js', host=slug_host, headers={'If-None-Match': 'W/' + headers['ETag']})
            self.assertEqual((status, body), (304, b''))
            # HTML stays no-store; preview assets revalidate, including sub-second edits.
            self.assertEqual(f.request('/pages/alpha/about/')[1]['Cache-Control'], 'no-store')
            path = f'/preview/{PREVIEW}/script.js'
            status, headers, body = f.request(path)
            self.assertEqual(headers['Cache-Control'], 'private, no-cache, must-revalidate')
            etag = headers['ETag']
            status, headers, body = f.request(path, headers={'If-None-Match':etag})
            self.assertEqual((status, body), (304, b''))
            self.assertEqual(headers['Access-Control-Allow-Origin'], '*')
            self.assertIn('sandbox', headers['Content-Security-Policy'])
            script = f.published / 'alpha/script.js'; before = script.stat()
            script.write_text('window.siteScript=null')
            os.utime(script, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
            status, headers, body = f.request(path, headers={'If-None-Match':etag})
            self.assertEqual((status, body), (200, b'window.siteScript=null'))
            self.assertNotEqual(headers['ETag'], etag)

    def test_preview_assets_use_one_authorized_lookup_and_skip_unchanged_file_reads(self):
        with FileFixture() as f:
            (f.directory / 'styles.css').write_text('body{color:navy}')
            database = server.neon_db; calls = []
            @contextmanager
            def counted(*args, **kwargs):
                calls.append(1)
                with database(*args, **kwargs) as conn: yield conn
            with patch.object(server, 'neon_db', counted):
                status, headers, _ = f.request(f'/preview/{PREVIEW}/styles.css')
            self.assertEqual(status, 200)
            self.assertEqual(len(calls), 1)
            original_read = Path.read_bytes
            def forbid_asset_read(path):
                if path.name == 'styles.css': raise AssertionError('unchanged resource was read')
                return original_read(path)
            with patch.object(Path, 'read_bytes', forbid_asset_read):
                self.assertEqual(f.request(f'/preview/{PREVIEW}/styles.css', headers={'If-None-Match':headers['ETag']})[0], 304)

    def test_preview_revision_detects_same_size_changes_and_ready_bridge_stays_sandboxed(self):
        with FileFixture() as f:
            page = f.directory / 'index.html'; page.write_text('<h1>One</h1>')
            old = f.handler.site_files_info(USER['id'])['previewRevision']
            self.assertEqual(old, f.handler.site_files_info(USER['id'])['previewRevision'])
            stat = page.stat(); page.write_text('<h1>Two</h1>')
            os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            self.assertNotEqual(old, f.handler.site_files_info(USER['id'])['previewRevision'])
            status, headers, body = f.request(f'/preview/{PREVIEW}/?__hatchery_preview=abcdef12-3456-7890')
            self.assertEqual(status, 200)
            self.assertIn(b'hatchery-preview-ready', body)
            self.assertIn(b'abcdef12-3456-7890', body)
            self.assertNotIn('allow-same-origin', headers['Content-Security-Policy'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(page.read_text(), '<h1>Two</h1>')
            self.assertNotIn(b'hatchery-preview-ready', f.request(f'/preview/{PREVIEW}/')[2])

    def test_legacy_migration_is_once_content_lives_in_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'site'; root.mkdir()
            legacy = {'siteName': '旧网站', 'username': 'old', 'theme': 'minimal', 'pages': [
                {'id': 'home', 'name': '首页', 'path': 'home', 'html': '<h1>原来的标题</h1>'},
                {'id': 'about', 'name': '介绍', 'path': 'about', 'html': '<p>介绍文字</p>'}], 'forumPosts': [{'id': 'topic'}]}
            (root / 'site.json').write_text(json.dumps(legacy))
            site_files.initialize_directory(root, PROJECT / 'frontend', Path(tmp) / 'backups')
            self.assertIn('原来的标题', (root / 'index.html').read_text())
            self.assertIn('介绍文字', (root / 'about/index.html').read_text())
            self.assertNotIn('pages', site_files.read_metadata(root))
            self.assertEqual(json.loads((root / 'runtime.json').read_text())['forumPosts'], [{'id': 'topic'}])
            self.assertTrue(list((Path(tmp) / 'backups').glob('*/legacy-source.json')))
            (root / 'index.html').write_text('<h1>edited file</h1>')
            before = site_files.file_manifest(root)
            site_files.initialize_directory(root, PROJECT / 'frontend', Path(tmp) / 'backups', legacy)
            self.assertEqual(before, site_files.file_manifest(root))

    def test_migration_copies_available_legacy_attachments_into_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            frontend = project / 'frontend'; frontend.mkdir()
            for name in ('viewer.html', 'viewer.js', 'styles.css', 'motion.css'):
                shutil.copy2(PROJECT / 'frontend' / name, frontend / name)
            original = project / 'tmp/ai-attachments/user/1234567890abcdef/image.png'
            original.parent.mkdir(parents=True); original.write_bytes(b'original-image')
            legacy = {'pages': [{'path': 'home', 'html': '<img src="/attachments/ai/user/1234567890abcdef/image.png">'}]}
            site = project / 'site'
            site_files.initialize_directory(site, frontend, project / 'backups', legacy)
            self.assertIn('assets/imported/1234567890abcdef/image.png', (site / 'index.html').read_text())
            original.unlink()
            self.assertEqual((site / 'assets/imported/1234567890abcdef/image.png').read_bytes(), b'original-image')

    def test_legacy_conflict_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'site'; root.mkdir()
            legacy = {'pages': [{'path': 'home', 'html': 'old'}]}
            (root / 'site.json').write_text(json.dumps(legacy)); (root / 'index.html').write_text('existing')
            with self.assertRaises(ValueError): site_files.initialize_directory(root, PROJECT / 'frontend', Path(tmp) / 'backups')
            self.assertEqual((root / 'index.html').read_text(), 'existing')
            self.assertEqual(site_files.read_metadata(root), legacy)

    def test_persistent_upload_and_lock(self):
        with FileFixture() as f:
            manifest = server.save_ai_attachments(USER['id'], [{'kind': 'text', 'name': 'note.txt', 'content': 'persistent'}], f.directory)
            self.assertEqual((f.directory / manifest[0]['url']).read_text(), 'persistent')
            self.assertTrue(manifest[0]['url'].startswith('assets/uploads/'))
            entered, release = threading.Event(), threading.Event()
            def locked():
                with site_files.site_lock(USER['id']): entered.set(); release.wait(5)
            thread = threading.Thread(target=locked); thread.start(); entered.wait(2)
            try:
                with self.assertRaises(ValueError): f.publish('alpha')
            finally: release.set(); thread.join()


if __name__ == '__main__': unittest.main()
