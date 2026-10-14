"""Package the reviewed AI repairs; --deploy installs them with private backups.

No Git operations, DNS/TLS changes, provider queries or model calls are performed.
The existing native installation and .env are preserved.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/ai-fixes/20261013'
HOST = 'root@39.106.77.105'

REMOTE = r'''
import hashlib,json,os,pwd,shutil,subprocess,sys,tarfile,time
from pathlib import Path
release=__RELEASE__
app=Path('/opt/hatchery'); stage=Path('/opt/hatchery-deployments')/release
backup=Path('/var/backups/hatchery')/release
stage.mkdir(parents=True,exist_ok=False);backup.mkdir(parents=True,exist_ok=False,mode=0o700)
with tarfile.open('/tmp/'+release+'.tar.gz') as archive:archive.extractall(stage,filter='data')
manifest=json.loads((stage/'release-manifest.json').read_text())
for name,digest in manifest['sha256'].items():
    if hashlib.sha256((stage/name).read_bytes()).hexdigest()!=digest:raise SystemExit('package digest mismatch')
python=str(app/'.venv/bin/python')
subprocess.run([python,'-m','py_compile',*[str(stage/n) for n in manifest['sha256'] if n.endswith('.py')]],check=True)
env_hash=hashlib.sha256((app/'.env').read_bytes()).hexdigest()
# All persistent website data and the old application files are captured privately.
subprocess.run(['systemctl','stop','hatchery'],check=True,timeout=40)
started=False
try:
    names=[n for n in manifest['sha256'] if (app/n).is_file()]
    with tarfile.open(backup/'before.tar.gz','w:gz') as archive:
        for name in names:archive.add(app/name,arcname=name,recursive=False)
        for name in ('.env','published','sites','tmp/ai-attachments'):
            if (app/name).exists():archive.add(app/name,arcname=name)
    (backup/'before.tar.gz').chmod(0o600)
    # Snapshot only Hatchery history/site tables. Never query usage or shared users.
    snapshot_script=r"""
import json,sys
from pathlib import Path
sys.path.insert(0,'/opt/hatchery')
import server
rows={}
with server.neon_db() as conn,conn.cursor() as cur:
    cur.execute('SET TRANSACTION READ ONLY')
    for table in ['hatchery_ai_chat_sessions','hatchery_site_drafts','hatchery_site_previews','hatchery_published_sites']:
        cur.execute('SELECT * FROM '+table)
        names=[c[0] for c in cur.description]
        rows[table]=[dict(zip(names,r)) for r in cur.fetchall()]
Path(sys.argv[1]).write_text(json.dumps(rows,ensure_ascii=False,default=str))
"""
    subprocess.run([python,'-c',snapshot_script,str(backup/'history-and-sites.json')],check=True,cwd=app,timeout=45)
    (backup/'history-and-sites.json').chmod(0o600)
    identity=pwd.getpwnam('hatchery')
    for name in manifest['sha256']:
        if name.startswith('scripts/'):continue
        target=app/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(stage/name,target);os.chown(target,identity.pw_uid,identity.pw_gid)
    # Initialize durable storage and migrate/repair only the recorded test user's site.
    repair_script=r"""
import json,sys
sys.path.insert(0,'/opt/hatchery')
sys.path.insert(0,sys.argv[1]+'/scripts')
import server
from repair_recorded_test_site import repair
server.initialize_database()
with server.neon_db() as conn,conn.cursor() as cur:
    cur.execute('SELECT user_id FROM hatchery_ai_chat_sessions WHERE id=%s',('c3d65680-bbea-4c05-babb-554ffce2194b',))
    uid=cur.fetchone()[0]
handler=object.__new__(server.AIchemyHatcheryHandler)
with server.site_files.site_lock(uid):
    directory,slug=handler.ensure_site_files(uid)
    changed=repair(directory)
print(json.dumps({'slug':slug,'changed':changed,'pages':server.site_files.page_files(directory)}))
"""
    result=subprocess.run(['runuser','-u','hatchery','--',python,'-c',repair_script,str(stage)],cwd=app,capture_output=True,text=True,timeout=60)
    if result.returncode:raise RuntimeError('migration/repair failed: '+result.stderr[-1500:])
    if hashlib.sha256((app/'.env').read_bytes()).hexdigest()!=env_hash:raise RuntimeError('configuration changed unexpectedly')
    subprocess.run(['systemctl','start','hatchery'],check=True,timeout=30)
    started=True
    for _ in range(30):
        check=subprocess.run(['curl','-fsS','--max-time','2','http://127.0.0.1:4173/api/ai/status'],capture_output=True,text=True)
        if check.returncode==0:break
        time.sleep(.5)
    if check.returncode:raise RuntimeError('application health check failed')
    status=json.loads(check.stdout)
    print(json.dumps({'release':release,'backup':str(backup),'repair':json.loads(result.stdout),
                      'configured':status.get('configured'),'model':status.get('model'),'envUnchanged':True,
                      'domainChanges':False,'realAiCalls':0,'state':'deployed'}))
except Exception:
    if not started:
        with tarfile.open(backup/'before.tar.gz') as archive:archive.extractall(app,filter='fully_trusted')
        subprocess.run(['chown','-R','hatchery:hatchery',str(app/'published'),str(app/'sites')],check=False)
        subprocess.run(['systemctl','start','hatchery'],check=False)
    raise
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--deploy',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    release='hatchery-ai-repair-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    names=['server.py','site_files.py','ai_jobs.py','deepseek_harness_adapter.py','scripts/repair_recorded_test_site.py']
    names += [str(p.relative_to(ROOT)) for d in ('frontend','deepseek_harness') for p in (ROOT/d).rglob('*') if p.is_file() and not p.is_symlink()]
    manifest={'release':release,'sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sorted(names)},
              'scope':'AI persistence, files, IP publication, recorded test-page corrections; no DNS/TLS/model calls'}
    mf=OUT/'release-manifest.json';mf.write_text(json.dumps(manifest,indent=2))
    package=OUT/(release+'.tar.gz')
    with tarfile.open(package,'w:gz') as archive:
        for name in names:archive.add(ROOT/name,arcname=name,recursive=False)
        archive.add(mf,arcname='release-manifest.json')
    print(json.dumps({'package':str(package),'files':len(names),'release':release}),flush=True)
    if not args.deploy:return
    subprocess.run(['scp','-q',str(package),HOST+':/tmp/'+package.name],check=True,timeout=60)
    script=REMOTE.replace('__RELEASE__',repr(release))
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',HOST,'python3 -'],input=script,capture_output=True,text=True,timeout=240)
    if result.returncode:
        (OUT/'deployment-error.log').write_text(result.stderr)
        raise SystemExit('Deployment failed; private diagnostics saved locally. Check backup and service before retrying.')
    data=json.loads(result.stdout.strip().splitlines()[-1]);(OUT/'deployment.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    print(json.dumps(data,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
