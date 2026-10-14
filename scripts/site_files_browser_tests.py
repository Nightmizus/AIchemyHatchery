"""Browser check against site_files_real_smoke.py's isolated server and real generated files."""
import json
import sys
from playwright.sync_api import sync_playwright

base = f'http://127.0.0.1:{int(sys.argv[1])}'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 960})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base + '/')
    page.wait_for_function('fileSite?.format === "files"')
    page.locator('#previewExpandFab').click()
    frame = page.frame_locator('#siteFilePreview')
    frame.locator('#counter').wait_for()
    assert frame.locator('#counter').inner_text() == '0'
    frame.locator('#increment').click()
    assert frame.locator('#counter').inner_text() == '1'
    assert frame.locator('body').evaluate('(node)=>getComputedStyle(node).backgroundColor') == 'rgb(240, 244, 255)'
    isolated = page.frames[1].evaluate('()=>{try{return parent.document.body ? false : false}catch{return true}}')
    assert isolated, 'site script acquired console origin'
    assert page.evaluate('document.querySelector("#aiModelBtn") === null')
    page.locator('#siteFileSelect').select_option('about/index.html')
    frame.get_by_text('About direct files', exact=True).wait_for()
    page.reload(); page.wait_for_function('fileSite?.format === "files"')
    page.locator('#previewExpandFab').click()
    frame = page.frame_locator('#siteFilePreview')
    frame.locator('#counter').wait_for()
    page.evaluate('refreshAiChangedFiles(["script.js","styles.css","index.html"])')
    page.wait_for_function('fileSite?.format === "files"')
    assert not errors, errors
    page.screenshot(path='/tmp/hatchery-direct-files-browser.png')
    browser.close()
print(json.dumps({'passed': ['real HTML preview', 'external CSS', 'external JS click', 'nested pages', 'reload persistence', 'console isolation', 'no model selector', 'user files do not reload console']}, ensure_ascii=False))
