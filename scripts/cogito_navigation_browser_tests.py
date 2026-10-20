"""Check cogito's responsive layout on a read-only copy of its website.

Usage: python scripts/cogito_navigation_browser_tests.py /copied/cogito
Requires Playwright and its Chromium browser. All requests stay on loopback;
the API is stubbed, with no production writes, credentials or model calls.
Companion repair: ops/repairs/cogito-mobile-navigation.patch, scoped only to
cogito/assets/css/style.css. Apply to a selected source copy, not the console.
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


PAGES = ('index', 'activities', 'reading', 'genealogy', 'toolbox', 'submissions', 'admin')
WIDTHS = (390, 320, 360, 375, 414, 560, 561, 768, 880, 881, 1024, 1440)
GEOMETRY = """() => {
  const rect = e => {
    const r = e.getBoundingClientRect();
    return {left:r.left, right:r.right, width:r.width, height:r.height,
            client:e.clientWidth, scroll:e.scrollWidth};
  };
  return {width:innerWidth, scroll:document.documentElement.scrollWidth,
    controls:[...document.querySelectorAll('.brand, .themetoggle, .navtoggle')]
      .filter(e => e.getBoundingClientRect().width)
      .map(e => ({name:e.className, ...rect(e)})),
    text:[...document.querySelectorAll('.brand__name, .brand__sub')].map(rect),
    nav:rect(document.querySelector('#sitenav'))};
}"""


def check_layout(page, label):
    state = page.evaluate(GEOMETRY)
    width = state['width']
    assert width == page.viewport_size['width'], (label, 'mobile viewport expanded', state)
    assert state['scroll'] <= width + 1, (label, 'page overflows', state)
    previous = None
    for control in state['controls']:
        assert control['left'] >= -1 and control['right'] <= width + 1, (label, control)
        if previous is not None:
            assert previous['right'] <= control['left'] + 1, (label, 'overlap', state)
        previous = control
        if width <= 880 and control['name'] != 'brand':
            assert control['width'] >= 44 and control['height'] >= 44, (label, 'tap target', control)
    for text in state['text']:
        assert text['scroll'] <= text['client'] + 1, (label, 'brand text clipped', text)
    if state['nav']['width']:
        assert state['nav']['left'] >= -1 and state['nav']['right'] <= width + 1, (label, state['nav'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    for name in PAGES:
        assert (source / (name + '.html')).is_file(), name

    class Static(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Static, directory=str(source)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    errors, blocked = [], []
    layouts = interactions = 0

    def local_only(route):
        request = route.request
        url = urlparse(request.url)
        if url.hostname != '127.0.0.1' or url.port != server.server_port or request.method != 'GET':
            blocked.append(request.method + ' ' + request.url)
            route.abort()
        elif url.path.startswith('/_api/'):
            route.fulfill(status=503, content_type='application/json', body='{"error":"offline fixture"}')
        else:
            route.continue_()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={'width': 390, 'height': 900}, service_workers='block')
                context.route('**/*', local_only)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                for name in PAGES:
                    page.goto(base + '/' + name + '.html', wait_until='networkidle')
                    if name == 'admin':
                        # Loopback is shorter than the public hostname. Reproduce
                        # the actual address shown by the live admin page too.
                        page.locator('#api-base').evaluate("e => e.textContent = 'https://cogito.aichemy.club/_api'")
                    for theme in ('cosmos', 'paper'):
                        page.evaluate('(theme) => COGITO_THEME.set(theme)', theme)
                        for width in WIDTHS:
                            page.set_viewport_size({'width': width, 'height': 900})
                            label = (name, theme, width)
                            check_layout(page, label)
                            layouts += 1
                            menu, nav = page.locator('.navtoggle'), page.locator('#sitenav')
                            if width <= 880:
                                expect(menu).to_be_visible()
                                expect(nav).to_be_hidden()
                                menu.click()
                                expect(menu).to_have_attribute('aria-expanded', 'true')
                                expect(nav).to_be_visible()
                                expect(nav.locator('a')).to_have_count(7)
                                for link in nav.locator('a').all():
                                    expect(link).to_be_visible()
                                check_layout(page, label + ('open',))
                                layouts += 1
                                menu.press('Enter')
                                expect(menu).to_have_attribute('aria-expanded', 'false')
                                expect(nav).to_be_hidden()
                                menu.press('Space')
                                expect(menu).to_have_attribute('aria-expanded', 'true')
                                menu.click()
                                expect(nav).to_be_hidden()
                                interactions += 1
                            else:
                                expect(menu).to_be_hidden()
                                expect(nav).to_be_visible()
                    if name == 'genealogy':
                        page.set_viewport_size({'width': 320, 'height': 900})
                        scroller = page.locator('.gx-figure__scroll')
                        result = scroller.evaluate('''e => {
                          e.scrollLeft = 100;
                          return {left:e.scrollLeft, client:e.clientWidth, scroll:e.scrollWidth};
                        }''')
                        assert result['scroll'] > result['client'] and result['left'] > 0, result
                        check_layout(page, 'graph scroll stays inside its container')
                    if name == 'admin':
                        page.set_viewport_size({'width': 320, 'height': 900})
                        page.locator('#api-base').evaluate("e => e.textContent = 'https://aichemy.club/preview/' + 'a'.repeat(32) + '/_api'")
                        check_layout(page, 'long preview backend address')
                        layouts += 1

                # A touch-enabled phone plus landscape rotation and actual theme clicks.
                phone = browser.new_context(viewport={'width': 390, 'height': 844},
                                            is_mobile=True, has_touch=True, device_scale_factor=3,
                                            service_workers='block')
                phone.route('**/*', local_only)
                mobile = phone.new_page()
                mobile.on('pageerror', lambda error: errors.append(str(error)))
                mobile.goto(base + '/index.html', wait_until='networkidle')
                for width, height in ((320, 740), (390, 844), (844, 390)):
                    mobile.set_viewport_size({'width': width, 'height': height})
                    for theme in ('cosmos', 'paper'):
                        mobile.evaluate('(theme) => COGITO_THEME.set(theme)', theme)
                        check_layout(mobile, ('touch', width, theme))
                        if args.screenshots and width <= 390:
                            args.screenshots.mkdir(parents=True, exist_ok=True)
                            mobile.screenshot(path=str(args.screenshots / f'home-{width}-{theme}.png'))
                        mobile.locator('.navtoggle').tap()
                        expect(mobile.locator('#sitenav')).to_be_visible()
                        mobile.locator('.themetoggle').tap()
                        expect(mobile.locator('html')).to_have_attribute('data-theme', 'paper' if theme == 'cosmos' else 'cosmos')
                        check_layout(mobile, ('touch open + theme switch', width, theme))
                        mobile.locator('.navtoggle').tap()
                        expect(mobile.locator('#sitenav')).to_be_hidden()
                        layouts += 2
                        interactions += 1
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    print(json.dumps({'passedLayouts': layouts, 'passedMenuInteractions': interactions,
                      'pages': len(PAGES), 'themes': 2, 'widths': WIDTHS,
                      'javascriptErrors': errors, 'blockedRequests': len(blocked),
                      'productionWrites': 0, 'realModelCalls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
