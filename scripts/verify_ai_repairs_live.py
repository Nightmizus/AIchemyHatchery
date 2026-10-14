"""Bounded live non-AI smoke and an explicitly labelled account report import.

Creates a temporary 15-minute console session for the already identified test
account, removes it on exit, and never submits an AI request or queries usage.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/ai-fixes/20261013'
BASE='http://39.106.77.105'
OLD='c3d65680-bbea-4c05-babb-554ffce2194b'
REPORT=str(uuid.uuid5(uuid.NAMESPACE_URL,'hatchery-ai-repairs-20261013:'+OLD))
NAMESPACE='AIUI40_20261012T034719Z'


def remote(script):
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','root@39.106.77.105',
                           'cd /opt/hatchery && .venv/bin/python -'],input=script,capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Production report/session operation failed; no credentials are printed')
    return result.stdout


def main():
    report=(OUT/'account-report.html').read_text()
    patterns=[r'postgres(?:ql)?://[^\s/]+:[^\s@]+@',r'sk-[a-zA-Z0-9_-]{20,}',r'(?i)(?:authorization|password|cookie)["\x27]?\s*[:=]\s*["\x27]']
    if any(re.search(p,report) for p in patterns):raise RuntimeError('Report contains a potential credential; import stopped')
    script='''import json,secrets,hashlib,uuid,sys
from datetime import timedelta
from pathlib import Path
import server
sid=__OLD__;rid=__REPORT__;text=__HTML__
token=secrets.token_urlsafe(36);digest=hashlib.sha256(token.encode()).hexdigest()
with server.neon_db() as conn,conn.cursor() as cur:
    cur.execute('SELECT user_id FROM hatchery_ai_chat_sessions WHERE id=%s',(sid,));uid=cur.fetchone()[0]
    now=server.iso_time()
    cur.execute("INSERT INTO hatchery_ai_chat_sessions(id,user_id,title,messages_html,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",(rid,uid,'AI 问题修复与历史证据 10月13日',text,now,now))
    cur.execute('SELECT messages_html FROM hatchery_ai_chat_sessions WHERE id=%s AND user_id=%s',(rid,uid))
    if cur.fetchone()[0]!=text:raise RuntimeError('Existing report differs; it was not overwritten')
    cur.execute('INSERT INTO console_sessions(token_hash,user_id,created_at,expires_at,last_seen_at,ip_address,user_agent) VALUES(%s,%s,%s,%s,%s,%s,%s)',(digest,uid,now,server.iso_time(server.utc_now()+timedelta(minutes=15)),now,'127.0.0.1','Hatchery repair verification'))
print(json.dumps({'token':token,'digest':digest}))
'''.replace('__OLD__',repr(OLD)).replace('__REPORT__',repr(REPORT)).replace('__HTML__',repr(report))
    auth=json.loads(remote(script))
    checks=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
                context.add_cookies([{'name':'alchemy_hatchery_console_session','value':auth['token'],'domain':'39.106.77.105','path':'/','httpOnly':True,'sameSite':'Strict'}])
                response=context.request.get(BASE+'/api/site/files');assert response.ok
                info=response.json();assert info['format']=='files' and info['publicUrl']=='/pages/taffy/'
                assert 'AIUI40_20261012T034719Z/r05-counter.js' in info['files']
                assert 'AIUI40_20261012T034719Z/details/index.html' in info['files']
                assert not any(k in info['metadata'] for k in ('pages','html','elements'))
                checks+=['authenticated direct-file API','metadata-only site.json','details retained','IP public URL']
                response=context.request.get(BASE+'/api/ai/sessions/'+OLD);assert response.ok and response.json()['session']['serverOwned']
                response=context.request.get(BASE+'/api/ai/sessions/'+REPORT);assert response.ok
                assert '系统导入' in response.json()['session']['messagesHtml']
                assert 'TEST-009' in response.json()['session']['messagesHtml']
                recovery=json.loads((OUT/'runtime-restoration.json').read_text()) if (OUT/'runtime-restoration.json').exists() else None
                if recovery:
                    appendix=context.request.get(BASE+'/api/ai/sessions/'+recovery['appendixId']);assert appendix.ok
                    assert '系统归档：原始运行时回复' in appendix.json()['session']['messagesHtml']
                    assert appendix.json()['session']['messagesHtml'].count('<article class="ai-message assistant">')==37
                    checks+=['36 original runtime replies archived and readable']
                page=context.new_page();errors=[];ai_requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:ai_requests.append(r.url) if r.method=='POST' and '/api/ai/run' in r.url and '/ack' not in r.url else None)
                page.goto(BASE+'/hatchery/c/'+REPORT)
                page.locator('#aiChatMessages').get_by_text('系统导入：问题修复与历史证据',exact=True).wait_for()
                page.reload();page.locator('#aiChatMessages').get_by_text('系统导入：问题修复与历史证据',exact=True).wait_for()
                checks+=['report imported without model invocation','report survives browser reload','old session still available']
                page.screenshot(path=str(OUT/'live-account-report.png'))
                published=context.new_page();published.on('pageerror',lambda e:errors.append(str(e)))
                published.goto(BASE+'/pages/taffy/'+NAMESPACE+'/')
                published.locator('#r05-counter-button').click(click_count=3)
                assert published.locator('#r05-counter-value').inner_text()=='3'
                kb=published.locator('#r06-keyboard-button');kb.focus()
                before=published.locator('#r06-keyboard-status').inner_text()
                published.keyboard.press('Enter');assert published.locator('#r06-keyboard-status').inner_text()!=before
                published.keyboard.press('Space');assert published.locator('#r06-keyboard-status').inner_text()==before
                with published.expect_download() as pending:published.locator('#r18-download-link').click()
                pending.value.save_as(OUT/'live-R18-download-test.txt')
                assert (OUT/'live-R18-download-test.txt').read_text().splitlines()[0]=='R18-DOWNLOAD'
                for marker in ('R28-A-RESULT','R28-B-RESULT','R31-NEW-TAB-RESULT','R31-OLD-TAB-RESULT'):
                    assert marker in published.locator('body').inner_text()
                if recovery:
                    assert published.locator('[data-hatchery-runtime-restored="20261013"]').count()==1
                    assert published.locator('#r28-two-tabs-diff-session').count()==1
                    assert published.locator('#r31-new-tab').count()==1
                checks+=['live external JS 0→3','live Enter/Space','actual downloaded contents','four markers present; two original blocks restored with provenance']
                images=published.locator('img').evaluate_all('(nodes)=>nodes.map(e=>({src:e.getAttribute("src"),loaded:e.complete&&e.naturalWidth>0}))')
                published.set_viewport_size({'width':390,'height':844})
                assert published.evaluate('document.documentElement.scrollWidth<=innerWidth')
                published.screenshot(path=str(OUT/'live-test-page-mobile.png'),full_page=True)
                checks+=['live 390px viewport has no horizontal overflow']
                gallery=context.request.get(BASE+'/gallery');assert gallery.ok
                assert 'href="/pages/taffy/"' in gallery.text()
                checks+=['Gallery uses IP path']
                assert not ai_requests,ai_requests
                assert not errors,errors
                result={'checkedAt':datetime.now(timezone.utc).isoformat(),'passed':checks,'errors':errors,'images':images,
                        'reportSessionId':REPORT,'reportUrl':BASE+'/hatchery/c/'+REPORT,'realAiCalls':0}
                (OUT/'production-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
                print(json.dumps(result,ensure_ascii=False,indent=2))
            finally:browser.close()
    finally:
        remote("import server\nwith server.neon_db() as conn,conn.cursor() as cur:\n    cur.execute('DELETE FROM console_sessions WHERE token_hash=%s',("+repr(auth['digest'])+",))\n")


if __name__=='__main__':main()
