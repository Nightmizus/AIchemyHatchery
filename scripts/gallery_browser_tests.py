"""Desktop/mobile gallery and admin categorization against real local PostgreSQL.

Unrelated console services are stubbed; gallery and category API are real.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect
from gallery_category_tests import GalleryFixture, ADMIN, PROJECT


def main():
    output = PROJECT / 'output/playwright'
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with GalleryFixture() as fixture, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, **({'executable_path': os.environ['BROWSER_BIN']} if os.environ.get('BROWSER_BIN') else {}))
        try:
            context = browser.new_context(viewport={'width':1440, 'height':1100}, extra_http_headers={'X-Test-Role':'admin'})
            page = context.new_page()
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{fixture.port}'
            page.goto(base + '/gallery')
            expect(page.locator('#partner-clubs [data-gallery-card]')).to_have_count(4)
            expect(page.locator('#other-sites [data-gallery-card]')).to_have_count(2)
            page.screenshot(path=str(output / 'gallery-desktop.png'), full_page=True)
            search = page.get_by_role('searchbox')
            search.fill('哲学')
            expect(page.locator('[data-gallery-card]:visible')).to_have_count(1)
            expect(page.locator('#other-sites [data-empty-search]')).to_be_visible()
            search.fill('no-such-site-1234')
            expect(page.locator('[data-gallery-card]:visible')).to_have_count(0)
            expect(page.locator('#search-status')).to_have_text('找到 0 个匹配的网站')
            page.get_by_role('button', name='清空搜索').click()
            expect(page.locator('[data-gallery-card]:visible')).to_have_count(6)
            search.fill('SLEEP'); expect(page.locator('[data-gallery-card]:visible')).to_have_count(1)
            search.press('Escape'); expect(page.locator('[data-gallery-card]:visible')).to_have_count(6)
            checks.append('search, no results, clear and keyboard Escape')
            for width in (1440, 1024, 768, 390, 320):
                page.set_viewport_size({'width':width, 'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                for link in page.locator('[data-gallery-card]').all():
                    assert link.get_attribute('rel') == 'noopener noreferrer'
                if width == 390: page.screenshot(path=str(output / 'gallery-mobile.png'), full_page=True)
            checks.append('gallery responsive at 1440/1024/768/390/320; safe external links')
            nojs = browser.new_context(java_script_enabled=False)
            plain = nojs.new_page(); plain.goto(base + '/gallery')
            expect(plain.locator('[data-gallery-card]')).to_have_count(6)
            expect(plain.locator('.gallery-search')).to_be_hidden()
            nojs.close(); checks.append('all sites and categories work without JavaScript')

            def console_services(route):
                path = urlparse(route.request.url).path
                if path in ('/api/admin/sites', '/api/admin/sites/category'):
                    route.continue_(); return
                payload = {
                    '/api/auth/me': {'authenticated':True, 'user': {**ADMIN, 'previewId':'a'*32}},
                    '/api/site/files': {'format':'files', 'metadata':{}, 'files':['index.html'], 'pages':['index.html'], 'slug':'sleepsdsz', 'previewUrl':'/pages/sleepsdsz/'},
                    '/api/console/draft': {'draft':None},
                    '/api/ai/conversation': {'session': {'id':'test-conversation', 'jobs':[], 'messagesHtml':''}},
                    '/api/ai/status': {'configured':False},
                    '/api/ai/run/active': {'jobs':[]},
                    '/api/admin/users': {'users':[], 'total':0},
                    '/api/admin/ai-usage': {'usage':[], 'totals':{}},
                    '/api/admin/ai-config': {'provider':'deepseek', 'providerLabel':'DeepSeek', 'model':'Test'},
                }.get(path, {})
                route.fulfill(json=payload)
            page.route('**/api/**', console_services)
            page.set_viewport_size({'width':1440, 'height':1100})
            page.goto(base + '/')
            page.wait_for_function('currentConsoleUser !== null && !fileSiteLoading')
            page.locator('#consoleAccountBtn').click()
            page.locator('#inviteManagerBtn').click()
            expect(page.locator('[data-admin-site]')).to_have_count(6)
            row = page.locator('[data-admin-site="sleepsdsz"]')
            row.get_by_role('button', name='sleep@sdsz：设为合作社团').click()
            expect(row.locator('[data-site-category]')).to_have_text('合作社团')
            assert fixture.index()['sleepsdsz']['isPartner']
            visitor = context.new_page(); visitor.goto(base + '/gallery')
            expect(visitor.locator('#partner-clubs [data-gallery-card]')).to_have_count(5)
            expect(visitor.locator('#other-sites [data-gallery-card]')).to_have_count(1)
            row.get_by_role('button', name='sleep@sdsz：移至其他网站').click()
            expect(row.locator('[data-site-category]')).to_have_text('其他网站')
            visitor.reload(); expect(visitor.locator('#partner-clubs [data-gallery-card]')).to_have_count(4)
            expect(visitor.locator('#other-sites [data-gallery-card]')).to_have_count(2)
            visitor.close()
            checks.append('real admin button promotes/demotes; PostgreSQL and public Gallery agree')
            page.locator('.admin-sites-manager').scroll_into_view_if_needed()
            page.screenshot(path=str(output / 'gallery-admin-desktop.png'), full_page=True)
            # Failed requests must preserve the old label and leave the button usable.
            page.route('**/api/admin/sites/category', lambda route: route.fulfill(status=503, json={'error':'暂时无法保存，请重试'}))
            row.get_by_role('button', name='sleep@sdsz：设为合作社团').click()
            expect(row.get_by_role('button')).to_be_enabled()
            expect(row.locator('[data-site-category]')).to_have_text('其他网站')
            assert not fixture.index()['sleepsdsz']['isPartner']
            checks.append('failed category request preserves label and re-enables button')
            page.unroute('**/api/admin/sites/category')
            page.reload(); page.wait_for_function('currentConsoleUser !== null && !fileSiteLoading')
            page.locator('#consoleAccountBtn').click(); page.locator('#inviteManagerBtn').click()
            expect(page.locator('[data-admin-site="sleepsdsz"] [data-site-category]')).to_have_text('其他网站')
            for width in (390, 320):
                page.set_viewport_size({'width':width, 'height':844})
                page.locator('.admin-sites-manager').scroll_into_view_if_needed()
                assert page.locator('.admin-site-list').evaluate('(el)=>el.scrollWidth <= el.clientWidth'), width
                if width == 390: page.screenshot(path=str(output / 'gallery-admin-mobile.png'), full_page=True)
            checks.append('admin reload and mobile controls at 390/320')
            assert not errors, errors
            print(json.dumps({'checks':checks, 'javascriptErrors':errors, 'screenshots':str(output)}, ensure_ascii=False, indent=2))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
