"""Browser validation of the repaired recorded website, using local copied files."""
import json
from pathlib import Path
import shutil
from site_files_tests import FileFixture
from repair_recorded_test_site import NAMESPACE
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/ai-fixes/20261013'

with FileFixture() as f, sync_playwright() as pw:
    shutil.copytree(OUT / 'rehearsal/published/taffy', f.directory, dirs_exist_ok=True)
    assert f.publish('rehearsal')[0] == 200
    browser = pw.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width':1440,'height':1000}, accept_downloads=True)
        page.goto(f'http://127.0.0.1:{f.port}/pages/rehearsal/{NAMESPACE}/')
        page.locator('#r05-counter-button').click(click_count=3)
        assert page.locator('#r05-counter-value').inner_text() == '3'
        page.locator('#r05-counter-button').focus();page.keyboard.press('Enter');page.keyboard.press('Space')
        assert page.locator('#r05-counter-value').inner_text() == '5'
        kb=page.locator('#r06-keyboard-button');kb.focus()
        before=page.locator('#r06-keyboard-status').inner_text()
        page.keyboard.press('Enter');after=page.locator('#r06-keyboard-status').inner_text()
        assert before != after
        page.keyboard.press('Space');assert page.locator('#r06-keyboard-status').inner_text() == before
        with page.expect_download() as pending:page.locator('#r18-download-link').click()
        destination=OUT/'R18-download-test.txt';pending.value.save_as(destination)
        assert destination.read_text().splitlines()[0]=='R18-DOWNLOAD'
        for marker in ('R28-A-RESULT','R28-B-RESULT','R31-NEW-TAB-RESULT','R31-OLD-TAB-RESULT'):
            assert marker in page.locator('body').inner_text()
        page.set_viewport_size({'width':390,'height':844})
        overflow=page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,offenders:[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right}))})')
        assert overflow['scrollWidth']<=overflow['width'],overflow
        page.screenshot(path=str(OUT/'repaired-test-page-mobile.png'),full_page=True)
        report={'kind':'recorded site copied locally; no model calls','counter':'0→3→5','keyboard':'Enter toggles, Space restores','download':'R18 file contents verified','markers':'all four present; two restored from original runtime edits','mobileOverflow':False}
        (OUT/'recorded-site-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))
    finally:browser.close()
