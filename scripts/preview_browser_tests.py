"""Exercise embedded previews with real local files/HTTP and controlled slow requests.

Only unrelated AI services are stubbed. No model or production service is called.
"""
import json
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect
from site_files_tests import FileFixture, PROJECT, PREVIEW


def main():
    output = PROJECT / 'output/preview-load'
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with FileFixture() as fixture, sync_playwright() as pw:
        (fixture.directory / 'index.html').write_text('''<!doctype html><html><head>
<meta charset="utf-8"><link rel="stylesheet" href="styles.css"><script defer src="script.js"></script>
</head><body><h1>预览已准备好</h1><button id="counter">0</button><img src="slow.svg" alt="测试图片"></body></html>''')
        (fixture.directory / 'styles.css').write_text('body{background:#ebeff4;color:#182438;padding:32px;font-family:sans-serif}button{padding:12px 24px}')
        script = fixture.directory / 'script.js'
        script.write_text("document.querySelector('#counter').onclick=e=>e.target.textContent=Number(e.target.textContent)+1;window.fixtureVersion=1;")
        (fixture.directory / 'slow.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="20" fill="#648080"/></svg>')
        browser = pw.chromium.launch(headless=True, **({'executable_path': os.environ['BROWSER_BIN']} if os.environ.get('BROWSER_BIN') else {}))
        try:
            page = browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/ai/conversation', lambda route: route.fulfill(json={'session':{'id':'fixture', 'jobs':[], 'messagesHtml':''}}))
            page.route('**/api/ai/status', lambda route: route.fulfill(json={'configured':False}))
            held = {'document':[], 'image':[]}
            requests = []
            mode = {'document':'hold', 'image':'hold'}

            def preview_route(route):
                path = urlparse(route.request.url).path
                if route.request.resource_type == 'document':
                    requests.append(route.request.url)
                    if mode['document'] == 'hold': held['document'].append(route); return
                    if mode['document'] == 'error': route.fulfill(status=503, content_type='text/html', body='Unavailable'); return
                if path.endswith('/slow.svg') and mode['image'] == 'hold': held['image'].append(route); return
                route.continue_()

            page.route(f'**/preview/{PREVIEW}/**', preview_route)
            page.goto(f'http://127.0.0.1:{fixture.port}/', wait_until='domcontentloaded')
            root = page.locator('#canvasContent')
            expect(root).to_have_attribute('data-preview-state', 'loading')
            page.locator('#previewExpandFab').click()
            expect(page.get_by_text('正在加载预览…', exact=True)).to_be_visible()
            expect(page.locator('#siteFilePreview')).to_be_hidden()
            assert root.evaluate('(el)=>getComputedStyle(el.parentElement).backgroundColor') == 'rgb(32, 33, 36)'
            page.screenshot(path=str(output / 'preview-loading.png'), animations='disabled')
            # A message from the parent, even with the right token, cannot mark the frame ready.
            page.evaluate("window.postMessage({type:'hatchery-preview-ready',token:HatcheryPreview.navigationId},location.origin)")
            expect(root).to_have_attribute('data-preview-state', 'loading')
            mode['document'] = 'pass'
            assert held['document']; held['document'].pop().continue_()
            expect(root).to_have_attribute('data-preview-state', 'ready')
            assert held['image'], 'The frame should be usable while its image is still pending'
            frame = page.frame_locator('#siteFilePreview')
            frame.locator('#counter').click(); expect(frame.locator('#counter')).to_have_text('1')
            assert page.frames[1].evaluate("(()=>{try{parent.document.body;return false}catch{return true}})()")
            checks.append('loading UI replaces white canvas; DOM ready works before slow image; sandbox and message source verified')
            mode['image'] = 'pass'
            for route in held['image']: route.continue_()
            held['image'].clear()
            page.evaluate('window.savedPreview=document.querySelector("#siteFilePreview");for(let i=0;i<3;i++)applySiteFiles(fileSite)')
            page.locator('button[data-preview-device="mobile"]').click()
            page.locator('button[data-preview-device="desktop"]').click()
            page.evaluate('window.dispatchEvent(new Event("focus"))')
            page.evaluate('Promise.all([reloadSiteFiles(),reloadSiteFiles(),reloadSiteFiles()])')
            expect(frame.locator('#counter')).to_have_text('1')
            assert page.evaluate('savedPreview===document.querySelector("#siteFilePreview")')
            assert len(requests) == 1, requests
            checks.append('unchanged metadata, focus, concurrent refresh and device switching retain iframe and interaction; 1 document request')
            script.write_text(script.read_text().replace('fixtureVersion=1', 'fixtureVersion=2'))
            page.evaluate('reloadSiteFiles()')
            expect(root).to_have_attribute('data-preview-state', 'ready')
            expect(frame.locator('#counter')).to_have_text('0')
            assert page.frames[1].evaluate('window.fixtureVersion') == 2
            assert len(requests) == 2, requests
            checks.append('same-size file edit changes revision and triggers exactly one navigation')
            page.screenshot(path=str(output / 'preview-ready.png'))

            mode['document'] = 'error'
            page.evaluate('HatcheryPreview.reload()')
            expect(root).to_have_attribute('data-preview-state', 'error')
            expect(page.get_by_role('button', name='重新加载', exact=True)).to_be_visible()
            page.screenshot(path=str(output / 'preview-error.png'))
            mode['document'] = 'pass'
            count = len(requests)
            page.get_by_role('button', name='重新加载', exact=True).click()
            expect(root).to_have_attribute('data-preview-state', 'ready')
            assert len(requests) == count + 1
            checks.append('HTTP error shows recovery actions; manual retry performs one successful navigation')

            # Check real deadlines while the document request remains pending.
            mode['document'] = 'hold'
            old_token = page.evaluate('HatcheryPreview.navigationId')
            page.evaluate('HatcheryPreview.reload()')
            expect(root).to_have_attribute('data-preview-state', 'slow', timeout=10000)
            page.evaluate("token=>window.dispatchEvent(new MessageEvent('message',{source:document.querySelector('#siteFilePreview').contentWindow,data:{type:'hatchery-preview-ready',token}}))", old_token)
            expect(root).to_have_attribute('data-preview-state', 'slow')
            expect(root).to_have_attribute('data-preview-state', 'error', timeout=25000)
            mode['document'] = 'pass'
            for route in held['document']: route.continue_()
            held['document'].clear()
            expect(root).to_have_attribute('data-preview-state', 'ready')
            checks.append('slow and timeout states, stale ready message ignored, late successful response recovers')

            page.route('**/api/site/files', lambda route: route.fulfill(status=503, json={'error':'测试文件列表暂时不可用'}))
            page.evaluate('enterConsole(currentConsoleUser)')
            expect(root).to_have_attribute('data-preview-state', 'error')
            expect(page.get_by_text('读取网站文件失败：测试文件列表暂时不可用', exact=True).first).to_be_visible()
            page.unroute('**/api/site/files')
            page.get_by_role('button', name='重新加载', exact=True).click()
            expect(root).to_have_attribute('data-preview-state', 'ready')
            checks.append('metadata failure and retry reinitialize the console preview')

            delayed_metadata = []
            page.route('**/api/site/files', lambda route: delayed_metadata.append(route))
            with page.expect_request('**/api/site/files'):
                page.evaluate('()=>{window.delayedEnter=enterConsole(currentConsoleUser)}')
            expect(root).to_have_attribute('data-preview-state', 'loading')
            page.unroute('**/api/site/files')
            page.evaluate('enterConsole(currentConsoleUser)')
            expect(root).to_have_attribute('data-preview-state', 'ready')
            count = len(requests)
            old_info = json.loads(fixture.request('/api/site/files')[2])
            old_info['metadata']['siteName'] = 'stale response must not apply'
            old_info['previewRevision'] = 'outdated'
            assert delayed_metadata
            delayed_metadata[0].fulfill(json=old_info)
            page.evaluate('window.delayedEnter')
            assert page.evaluate('fileSite.metadata.siteName') != old_info['metadata']['siteName']
            assert len(requests) == count
            checks.append('a late response from a superseded initialization cannot overwrite the current preview')
            page.emulate_media(reduced_motion='reduce')
            page.evaluate('HatcheryPreview.fail()')
            for width in (390, 320):
                page.set_viewport_size({'width':width, 'height':844})
                actions = page.locator('.preview-load-content')
                assert actions.evaluate('(el)=>el.scrollWidth <= el.clientWidth'), width
            page.set_viewport_size({'width':1440, 'height':1000})
            page.evaluate('applySiteFiles({...fileSite,pages:[],files:[]})')
            expect(root).to_have_attribute('data-preview-state', 'empty')
            expect(page.locator('#siteFilePreview')).to_have_count(0)
            expect(page.get_by_text('网站还没有页面', exact=True)).to_be_visible()
            page.evaluate('window.dispatchEvent(new Event("alchemyhatchery:logged-out"))')
            expect(page.locator('.preview-load-state')).to_have_count(0)
            checks.append('empty website, reduced motion, narrow recovery controls and logout cleanup')
            assert not errors, errors
            report = {'checks':checks, 'javascriptErrors':errors, 'screenshots':str(output)}
            (output / 'verified.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            browser.close()


if __name__ == '__main__':
    main()
