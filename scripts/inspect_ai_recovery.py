"""Read only production snapshots for the reported conversation; never reads usage/billing."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/ai-fixes/20261013'
PRIVATE = OUT / 'private'

REMOTE = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,'/opt/hatchery')
import server
sid='c3d65680-bbea-4c05-babb-554ffce2194b'
out={'sessionId':sid}
with server.neon_db() as conn, conn.cursor() as cur:
    cur.execute('SET TRANSACTION READ ONLY')
    cur.execute('SELECT user_id FROM hatchery_ai_chat_sessions WHERE id=%s',(sid,))
    row=cur.fetchone()
    if not row: raise SystemExit('reported session missing')
    uid=out['userId']=row[0]
    cur.execute('SELECT id,title,messages_html,created_at,updated_at FROM hatchery_ai_chat_sessions WHERE user_id=%s ORDER BY created_at',(uid,))
    out['sessions']=[dict(zip(['id','title','messagesHtml','createdAt','updatedAt'],r)) for r in cur.fetchall()]
    for table,name in [('hatchery_site_drafts','draft'),('hatchery_site_previews','preview')]:
        cur.execute('SELECT data_json,updated_at FROM '+table+' WHERE user_id=%s',(uid,))
        row=cur.fetchone()
        out[name]={'data':json.loads(row[0]),'updatedAt':row[1]} if row else None
    cur.execute('SELECT slug FROM hatchery_published_sites WHERE user_id=%s',(uid,))
    row=cur.fetchone();out['slug']=row[0] if row else None
out['files']={}
paths=[Path('/opt/hatchery/sites')/str(uid)/'site.json']
if out['slug']:paths.append(Path('/opt/hatchery/published')/out['slug']/'site.json')
for p in paths:
    if p.is_file(): out['files'][str(p.relative_to('/opt/hatchery'))]=json.loads(p.read_text())
print(json.dumps(out,ensure_ascii=False))
'''


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'root@39.106.77.105',
                             'cd /opt/hatchery && .venv/bin/python -'], input=REMOTE, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise SystemExit('Read-only production snapshot failed; no database or site changes were made.')
    data = json.loads(result.stdout)
    raw = PRIVATE / 'production-before.json'
    raw.write_text(json.dumps(data, ensure_ascii=False, indent=2)); raw.chmod(0o600)
    sources = {k: v['data'] for k, v in data.items() if k in ('draft','preview') and v}
    sources.update(data['files'])
    markers = ['R28-A-RESULT','R28-B-RESULT','R31-NEW-TAB-RESULT','R31-OLD-TAB-RESULT']
    audit = {'sessionId':data['sessionId'],'slug':data['slug'],'sessions':len(data['sessions']), 'sources':{}}
    for name, source in sources.items():
        text = json.dumps(source, ensure_ascii=False)
        audit['sources'][name]={'pages':[{'id':p.get('id'),'name':p.get('name'),'path':p.get('path')} for p in source.get('pages',[])],
                                'markers':{m:m in text for m in markers}, 'sha256':hashlib.sha256(text.encode()).hexdigest()}
    (OUT / 'recovery-audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
