"""Gallery/category regressions using disposable PostgreSQL and real HTTP handlers.

No production database, shared account changes, external CA or model calls.
"""
from contextlib import ExitStack
from http.client import HTTPConnection
from pathlib import Path
import functools
import json
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gallery
import server
import site_files
from local_postgres import LocalPostgres
from site_files_tests import FileFixture, USER

PROJECT = Path(__file__).resolve().parents[1]
ADMIN = {"id": "admin", "username": "admin", "role": "admin"}
EXAMPLES = (
    ("cogito", "Cogito 哲学社", "一起聊聊康德那些事。从圆桌讨论到哲学 Workshop，给每一个问题留出思考的空间。"),
    ("racingbusiness", "赛车商业与体育传媒社", "从赛道到商业，从赛事到传播，一起探索体育背后的广阔世界。"),
    ("sdszrootca", "SDSZ 网络安全社", "一起研究网络安全，分享技术，在 CTF 与实践中发现新的可能。"),
    ("sdszemergingmarketssociety", "经济研究与评论社", "读懂经济，观察世界。学生学刊《实验经济评论》、年度论坛与社团活动。"),
    ("sleepsdsz", "sleep@sdsz", "同学们一起维护的校园休息地点清单。分享安静的角落，留下一条实用的评价。"),
    ("m0nesy", "m0NESY · Falcons 档案站", "CS2 天才狙击手 m0NESY 的生涯档案：猎鹰时代、冠军荣誉与打法解析。"),
)


class GalleryFixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.pg = self.stack.enter_context(LocalPostgres())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="hatchery-gallery-")))
        self.published = self.root / 'published'
        self.published.mkdir()
        for name, value in (("neon_db", self.pg.db), ("PUBLISHED", self.published),
                            ("SITE_SUBDOMAINS_ENABLED", False), ("rate_limit_hit", lambda *a: False)):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(site_files, 'LOCK_ROOT', self.root / 'locks'))
        with self.pg.db() as conn, conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE "User"(id TEXT PRIMARY KEY, name TEXT, "campusId" TEXT);
                CREATE TABLE hatchery_published_sites(slug TEXT PRIMARY KEY, user_id TEXT UNIQUE,
                    created_at TEXT, updated_at TEXT, listed_in_gallery BOOLEAN NOT NULL DEFAULT TRUE);
                CREATE TABLE hatchery_audit_events(id BIGSERIAL PRIMARY KEY, user_id TEXT,
                    event TEXT, detail_json TEXT, created_at TEXT);
            ''')
        for slug, name, description in EXAMPLES:
            self.add_site(slug, name, description)
        self.initialize()

        class Handler(server.AIchemyHatcheryHandler):
            def console_user(self):
                role = self.headers.get('X-Test-Role', 'none')
                if role == 'admin': return dict(ADMIN)
                if role == 'user': return dict(USER)
                return None
            def log_message(self, *args): pass
        self.Handler = Handler
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(PROJECT)))
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        return self

    def add_site(self, slug, name=None, description="同学们的新作品", listed=True, disk=True, registered=True):
        now = '2026-10-19T00:00:00+00:00'
        if disk:
            directory = self.published / slug
            directory.mkdir(exist_ok=True)
            (directory / 'site.json').write_text(json.dumps({"format": "files", "siteName": name or slug,
                "description": description, "slug": slug}, ensure_ascii=False))
            (directory / 'index.html').write_text('<h1>Test website</h1>')
        if registered:
            with self.pg.db() as conn, conn.cursor() as cur:
                cur.execute('INSERT INTO "User" VALUES(%s,%s,%s)', (slug, '校园创作者', '20260001'))
                cur.execute('INSERT INTO hatchery_published_sites VALUES(%s,%s,%s,%s,%s)',
                            (slug, slug, now, now, listed))

    def initialize(self):
        with self.pg.db() as conn:
            gallery.initialize(conn, server.published_site_slugs_on_disk())

    def request(self, path='/gallery', method='GET', data=None, role='none', headers=None):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=10)
        extra = {'X-Test-Role': role, **(headers or {})}
        if data is not None: extra['Content-Type'] = 'application/json'
        conn.request(method, path, json.dumps(data) if data is not None else None, extra)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def set_category(self, slug, is_partner, **kwargs):
        return self.request('/api/admin/sites/category', 'POST', {'slug': slug, 'isPartner': is_partner},
                            **{'role': 'admin', **kwargs})

    def index(self):
        status, _, body = self.request('/api/admin/sites', role='admin')
        assert status == 200, body
        return {site['slug']: site for site in json.loads(body)['sites']}

    def __exit__(self, *args):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2)
        self.stack.close()


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.f = GalleryFixture().__enter__()
        self.addCleanup(self.f.__exit__)

    def test_initial_four_and_future_sites_default_to_other(self):
        self.assertEqual({slug for slug, site in self.f.index().items() if site['isPartner']}, set(gallery.INITIAL_PARTNERS))
        self.f.add_site('future-club', '未来社团', '合作社团')
        self.f.add_site('disk-only', registered=False)
        self.f.initialize()
        self.assertFalse(self.f.index()['future-club']['isPartner'])
        self.assertFalse(self.f.index()['disk-only']['isPartner'])

    def test_categories_survive_restart_including_manual_demotion(self):
        self.assertEqual(self.f.set_category('cogito', False)[0], 200)
        self.assertEqual(self.f.set_category('sleepsdsz', True)[0], 200)
        self.f.initialize()
        self.assertFalse(self.f.index()['cogito']['isPartner'])
        self.assertTrue(self.f.index()['sleepsdsz']['isPartner'])
        with self.f.pg.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT detail_json FROM hatchery_audit_events WHERE event='admin.site_category'")
            self.assertEqual(len(cur.fetchall()), 2)

    def test_missing_initial_club_is_not_promoted_on_later_restart(self):
        with self.f.pg.db() as conn, conn.cursor() as cur:
            cur.execute('TRUNCATE hatchery_gallery_categories, hatchery_gallery_migrations')
            cur.execute('DELETE FROM hatchery_published_sites')
            gallery.initialize(conn, set())
        self.f.initialize()
        self.assertFalse(any(site['isPartner'] for site in self.f.index().values()))

    def test_initial_migration_uses_registered_sites_without_local_file_copies(self):
        with self.f.pg.db() as conn, conn.cursor() as cur:
            cur.execute('TRUNCATE hatchery_gallery_categories, hatchery_gallery_migrations')
            gallery.initialize(conn, set())
        self.assertEqual({slug for slug, site in self.f.index().items() if site['isPartner']}, set(gallery.INITIAL_PARTNERS))

    def test_admin_only_and_same_origin_required(self):
        for role, code in (('none', 401), ('user', 403)):
            self.assertEqual(self.f.set_category('sleepsdsz', True, role=role)[0], code)
            self.assertEqual(self.f.request('/api/admin/sites', role=role)[0], code)
        self.assertEqual(self.f.set_category('sleepsdsz', True, headers={'Origin': 'https://untrusted.example'})[0], 403)
        self.assertFalse(self.f.index()['sleepsdsz']['isPartner'])

    def test_invalid_requests_do_not_create_categories(self):
        for value in ('false', 1, None, [], {}):
            self.assertEqual(self.f.set_category('sleepsdsz', value)[0], 400)
        for slug in ('../cogito', '/cogito', '', '.drafts', 'one\\two'):
            self.assertEqual(self.f.set_category(slug, True)[0], 400)
        self.assertEqual(self.f.set_category('missing-site', True)[0], 404)
        self.assertFalse(self.f.index()['sleepsdsz']['isPartner'])

    def test_all_sites_including_legacy_hidden_and_missing_are_manageable(self):
        self.f.add_site('legacy-site', registered=False)
        self.f.add_site('hidden-site', listed=False)
        self.f.add_site('missing-files', disk=False)
        for slug in ('legacy-site', 'hidden-site', 'missing-files'):
            self.assertEqual(self.f.set_category(slug, True)[0], 200)
            self.assertTrue(self.f.index()[slug]['isPartner'])
        page = self.f.request()[2].decode()
        self.assertIn('legacy-site', page)
        self.assertNotIn('hidden-site', page)
        self.assertNotIn('missing-files', page)

    def test_gallery_grouping_routes_assets_and_escaped_metadata(self):
        self.f.add_site('unsafe-text', '<script>alert(1)</script>', '"><img src=x onerror=alert(2)>')
        for path in ('/gallery', '/Gallery', '/gallery/', '/gallery?from=console'):
            code, headers, body = self.f.request(path)
            text = body.decode()
            self.assertEqual(code, 200)
            self.assertIn('no-store', headers['Cache-Control'])
            self.assertLess(text.index('id="partner-clubs"'), text.index('id="other-sites"'))
            partners, others = text.split('<section id="partner-clubs"', 1)[1].split('<section id="other-sites"', 1)
            self.assertEqual(partners.count('data-gallery-card'), 4)
            self.assertEqual(others.count('data-gallery-card'), 3)
            self.assertIn('&lt;script&gt;', text)
            self.assertNotIn('<script>alert', text)
            self.assertNotIn('<img src=x', text)
        for asset in ('/gallery.css', '/gallery.js'):
            code, headers, _ = self.f.request(asset + '?v=' + server.console_asset_version(asset))
            self.assertEqual(code, 200)
            self.assertIn('immutable', headers['Cache-Control'])

    def test_index_has_no_silent_500_site_cutoff(self):
        with self.f.pg.db() as conn, conn.cursor() as cur:
            cur.executemany('INSERT INTO hatchery_published_sites VALUES(%s,%s,%s,%s,TRUE)',
                            [(f'bulk-{n}', f'bulk-{n}', '2026-01-01', '2026-01-01') for n in range(501)])
        self.assertEqual(len(self.f.index()), 507)

    def test_featured_pick_is_unique_and_does_not_change_admin_categories(self):
        self.f.add_site('poki', 'Poki 大赛')
        self.f.add_site('new-site', '新网站')
        text = self.f.request()[2].decode()
        featured, remainder = text.split('<section id="featured-sites"', 1)[1].split('<section id="partner-clubs"', 1)
        self.assertEqual(featured.count('data-gallery-card'), 1)
        self.assertIn('Poki 大赛', featured)
        self.assertNotIn('Poki 大赛', remainder)
        self.assertFalse(self.f.index()['poki']['isPartner'])
        self.assertIn('新网站', remainder.split('<section id="other-sites"', 1)[1])
        # Being featured must not prevent an administrator from adding any site
        # to the partner section as well.
        self.assertEqual(self.f.set_category('poki', True)[0], 200)
        partners = self.f.request()[2].decode().split('<section id="partner-clubs"', 1)[1].split('<section id="other-sites"', 1)[0]
        self.assertIn('Poki 大赛', partners)
        for path in gallery.PREVIEW_ASSETS.values():
            code, headers, body = self.f.request(path + '?v=' + server.console_asset_version(path))
            headers = {key.lower(): value for key, value in headers.items()}
            self.assertEqual(code, 200)
            self.assertEqual(headers['content-type'], 'image/jpeg')
            self.assertIn('immutable', headers['cache-control'])
            self.assertTrue(body.startswith(b'\xff\xd8'))

    def test_unlisted_or_missing_featured_site_is_not_exposed(self):
        self.f.add_site('poki', 'Private Poki', listed=False)
        self.assertNotIn('Private Poki', self.f.request()[2].decode())
        with self.f.pg.db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hatchery_published_sites SET listed_in_gallery=TRUE WHERE slug='poki'")
        shutil.rmtree(self.f.published / 'poki')
        self.assertNotIn('Private Poki', self.f.request()[2].decode())


class PublishCategoryTests(unittest.TestCase):
    def test_publish_cannot_self_promote_and_rename_preserves_admin_choice(self):
        with FileFixture() as f:
            (f.directory / 'index.html').write_text('<h1>New website</h1>')
            self.assertEqual(f.publish('first-site', isPartner=True, category='partner')[0], 200)
            self.assertIsNone(f.db.execute('SELECT is_partner FROM hatchery_gallery_categories WHERE slug=?', ('first-site',)).fetchone())
            with patch.object(f.Handler, 'console_user', return_value=ADMIN):
                self.assertEqual(f.request('/api/admin/sites/category', 'POST', body={'slug':'first-site', 'isPartner': True})[0], 200)
            self.assertEqual(f.publish('first-site', isPartner=False)[0], 200)
            self.assertTrue(f.db.execute('SELECT is_partner FROM hatchery_gallery_categories WHERE slug=?', ('first-site',)).fetchone()[0])
            self.assertEqual(f.publish('renamed-site')[0], 200)
            self.assertTrue(f.db.execute('SELECT is_partner FROM hatchery_gallery_categories WHERE slug=?', ('renamed-site',)).fetchone()[0])
            self.assertIsNone(f.db.execute('SELECT is_partner FROM hatchery_gallery_categories WHERE slug=?', ('first-site',)).fetchone())

    def test_new_owner_never_inherits_old_slug_category(self):
        with FileFixture() as f:
            (f.directory / 'index.html').write_text('<h1>New website</h1>')
            f.db.execute('INSERT INTO hatchery_gallery_categories VALUES(?,?,?,?)', ('old-site', True, 'admin', '2026-10-19'))
            f.db.commit()
            self.assertEqual(f.publish('old-site')[0], 200)
            self.assertIsNone(f.db.execute('SELECT is_partner FROM hatchery_gallery_categories WHERE slug=?', ('old-site',)).fetchone())


if __name__ == '__main__':
    unittest.main()
