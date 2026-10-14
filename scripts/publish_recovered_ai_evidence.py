"""Apply reviewed recovered blocks and import attributed original replies, without AI calls."""
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/ai-fixes/20261013'
REPORT='5d96887c-eb91-5ea8-a836-0a38ae67d8d5'
APPENDIX=str(uuid.uuid5(uuid.NAMESPACE_URL,'hatchery-runtime-replies-20261013:'+REPORT))


def main():
    replies=json.loads((OUT/'recovered-replies.json').read_text())
    blocks=['<article class="ai-message assistant"><div><h2>系统归档：原始运行时回复</h2><p>以下内容直接来自已有运行时日志，没有调用模型。原采集中的 R16–R19 有错配，因此采集标签仅供查找原证据；不把这些标签当作实际任务归属或通过结论。历史回复中的错误验证声明也不代表当前事实。</p></div></article>']
    for r in replies:
        labels=', '.join(f"{c['round']}（第 {c['line']} 行 {c['field']}）" for c in r['capturedAs'])
        blocks.append('<article class="ai-message assistant"><div><h3>'+html.escape(str(r['createdAt'])+' · '+r['session'])+'</h3><small>'+html.escape('来源文件 SHA256 '+r['sourceSha256']+'；原采集标签：'+labels+'；运行时结束原因：'+str(r['finishReason']))+'</small><p>'+html.escape(r['summary'])+'</p>' + ('<p>'+html.escape(r['plan'])+'</p>' if r['plan'] else '')+'</div></article>')
    appendix=''.join(blocks)
    report=(OUT/'account-report.html').read_text()
    link=f'<article class="ai-message assistant"><div><p>补充恢复：<a href="/hatchery/c/{APPENDIX}">查看 36 条原始运行时回复</a>。其中 21 条比此前截断的采集版本更完整，两个缺失区块也已从原始编辑日志恢复。</p></div></article>'
    if f'href="/hatchery/c/{APPENDIX}"' not in report:report+=link
    for content in (report,appendix):
        if re.search(r'postgres(?:ql)?://[^\s/]+:[^\s@]+@|sk-[A-Za-z0-9_-]{20,}',content):raise ValueError('Potential credential in recovered report; stopped')
    old=(OUT/'private/account-report-before-recovery.html').read_text()
    payload={'fragments':json.loads((OUT/'recovered-site-fragments.json').read_text()),'report':report,'appendix':appendix,
             'oldReportHash':hashlib.sha256(old.encode()).hexdigest(),'reportId':REPORT,'appendixId':APPENDIX}
    # A reviewable local payload; raw runtime event streams are never uploaded.
    package=OUT/'recovery-publish.json';package.write_text(json.dumps(payload,ensure_ascii=False));package.chmod(0o600)
    subprocess.run(['scp','-q',str(ROOT/'scripts/restore_runtime_fragments.py'),str(package),'root@39.106.77.105:/tmp/'],check=True,timeout=30)
    script=r'''
import hashlib,json,os,pwd,shutil,sys
from datetime import datetime,timezone
from pathlib import Path
app=Path('/opt/hatchery');sys.path.insert(0,str(app));sys.path.insert(0,'/tmp')
import server
from restore_runtime_fragments import restore
payload=json.loads(Path('/tmp/recovery-publish.json').read_text())
with server.neon_db() as conn,conn.cursor() as cur:
    cur.execute('SELECT user_id,messages_html FROM hatchery_ai_chat_sessions WHERE id=%s',(payload['reportId'],))
    uid,old=cur.fetchone()
    digest=hashlib.sha256(old.encode()).hexdigest()
    if digest not in (payload['oldReportHash'],hashlib.sha256(payload['report'].encode()).hexdigest()):raise RuntimeError('Report changed; no overwrite allowed')
with server.site_files.site_lock(uid):
    directory,slug=server.user_site_directory(uid)
    backup=app/'.ai-backups/runtime-restoration';backup.mkdir(parents=True,exist_ok=True,mode=0o700)
    shutil.copy2(directory/'AIUI40_20261012T034719Z/index.html',backup/('before-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.html'))
    result=restore(directory,payload['fragments'])
with server.neon_db() as conn,conn.cursor() as cur:
    cur.execute('SELECT messages_html FROM hatchery_ai_chat_sessions WHERE id=%s AND user_id=%s FOR UPDATE',(payload['reportId'],uid))
    old=cur.fetchone()[0]
    if hashlib.sha256(old.encode()).hexdigest() not in (payload['oldReportHash'],hashlib.sha256(payload['report'].encode()).hexdigest()):raise RuntimeError('Report changed; no overwrite allowed')
    cur.execute('UPDATE hatchery_ai_chat_sessions SET messages_html=%s,updated_at=%s,revision=revision+1 WHERE id=%s AND user_id=%s',(payload['report'],server.iso_time(),payload['reportId'],uid))
    cur.execute('INSERT INTO hatchery_ai_chat_sessions(id,user_id,title,messages_html,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',(payload['appendixId'],uid,'原始 AI 回复恢复归档 10月13日',payload['appendix'],server.iso_time(),server.iso_time()))
    cur.execute('SELECT messages_html FROM hatchery_ai_chat_sessions WHERE id=%s AND user_id=%s',(payload['appendixId'],uid))
    if cur.fetchone()[0]!=payload['appendix']:raise RuntimeError('Existing appendix differs; stopped')
print(json.dumps({'restoration':result,'reportId':payload['reportId'],'appendixId':payload['appendixId'],'replies':36,'realAiCalls':0}))
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','root@39.106.77.105','chown hatchery:hatchery /tmp/recovery-publish.json && cd /opt/hatchery && runuser -u hatchery -- .venv/bin/python -'],input=script,text=True,capture_output=True,timeout=45)
    if result.returncode:
        diagnostic=OUT/'private/recovery-publish-error.log';diagnostic.write_text(result.stderr);diagnostic.chmod(0o600)
        raise RuntimeError('Recovery publication failed; private diagnostics saved. Inspect operation state before retrying.')
    data=json.loads(result.stdout);(OUT/'runtime-restoration.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    (OUT/'account-report.html').write_text(report);(OUT/'full-replies.html').write_text(appendix)
    print(json.dumps(data,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
