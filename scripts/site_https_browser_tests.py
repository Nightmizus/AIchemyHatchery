"""Verify publication stays clickable during signing and promotes after TLS readiness."""
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from site_files_tests import FileFixture, USER
import server


def main():
    with FileFixture() as fixture, patch.object(server, 'SITE_SUBDOMAINS_ENABLED', True), \
            patch.object(server, 'SITE_BASE_DOMAIN', 'aichemy.test'), \
            patch.object(server, 'CERT_SYNC_TRIGGER', fixture.root / 'var/request'), \
            patch.object(server, 'site_https_ready', return_value=False) as ready, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/ai/conversation', lambda route: route.fulfill(json={'jobs': [], 'messagesHtml': ''}))
            page.goto(f'http://127.0.0.1:{fixture.port}/')
            page.wait_for_function('fileSite?.format === "files" && currentConsoleUser !== null')
            page.evaluate('submitPublish("new-site",true)')
            link = page.locator('[data-publish-open]')
            assert link.get_attribute('href') == f'http://127.0.0.1:{fixture.port}/pages/new-site/'
            assert '正在准备' in page.locator('[data-publish-https]').inner_text()
            response = page.request.get(link.get_attribute('href'))
            assert response.status == 200
            ready.return_value = True
            page.wait_for_function('document.querySelector("[data-publish-open]")?.href === "https://new-site.aichemy.test/"')
            assert '已就绪' in page.locator('[data-publish-https]').inner_text()
            assert page.evaluate('currentConsoleUser.publishUrl') == 'https://new-site.aichemy.test/'
            assert not errors, errors
            print('PASS: publish → working interim URL → verified HTTPS URL, no browser errors; zero CA/model calls')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
