"""Recover existing final replies and relevant edit arguments; no provider queries."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/ai-fixes/20261013'
REMOTE=r'''
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,'/opt/hatchery')
import deepseek_harness_adapter as dsh
root=Path('/opt/hatchery/.deepseek-harness/home/sessions')
out=[]
for path in root.rglob('*.jsonl'):
    text=path.read_text()
    if 'AIUI40_20261012T034719Z' not in text:continue
    events=[json.loads(line) for line in text.splitlines()]
    final=dsh._extract_json_object(dsh._final_response(events))
    edits=[]
    for event in events:
        if event.get('type')!='tool/call':continue
        data=event.get('data') or {};args=data.get('arguments')
        if isinstance(args,str):
            try:args=json.loads(args)
            except ValueError:continue
        if not isinstance(args,dict):continue
        # Only the exact missing-marker writes, not read output or usage records.
        content=args.get('replace') if data.get('name')=='replace_file' else args.get('content') if data.get('name')=='write_file' else None
        if isinstance(content,str) and any(m in content for m in ('R28-A-RESULT','R31-NEW-TAB-RESULT')):
            edits.append({'sequence':event['seq'],'tool':data['name'],'path':args.get('path'),'content':content})
    out.append({'session':path.parent.name,'createdAt':events[0].get('createdAt'),'sourceSha256':hashlib.sha256(text.encode()).hexdigest(),
        'summary':str(final.get('summary') or '') if final else '', 'plan':str(final.get('plan') or '') if final else '',
        'finishReason':dsh._finish_reason(events),'edits':edits})
print(json.dumps(out,ensure_ascii=False))
'''

def main():
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','root@39.106.77.105','/opt/hatchery/.venv/bin/python -'],input=REMOTE,text=True,capture_output=True,timeout=45)
    if r.returncode:raise SystemExit('Read-only runtime evidence recovery failed')
    records=json.loads(r.stdout)
    raw=OUT/'private/runtime-evidence.json';raw.write_text(json.dumps(records,ensure_ascii=False,indent=2));raw.chmod(0o600)
    captured=[]
    original=ROOT/'output/ai-ui-40rounds/20261012T034719Z/rounds.jsonl'
    for i,line in enumerate(original.read_text().splitlines(),1):
        row=json.loads(line)
        for key in ('terminal_summary','result_a','result_b','new_tab','old_tab'):
            value=row.get(key);summary=value.get('summary') if isinstance(value,dict) else value
            if isinstance(summary,str) and summary:
                captured.append({'round':row['round'],'field':key,'line':i,'text':summary})
    matched=[]
    for record in records:
        matches=[c for c in captured if record['summary'].startswith(c['text'])]
        if matches:
            matched.append({'session':record['session'],'createdAt':record['createdAt'],'sourceSha256':record['sourceSha256'],
                            'summary':record['summary'],'plan':record['plan'],'finishReason':record['finishReason'],
                            'capturedAs':[{k:v for k,v in c.items() if k!='text'} for c in matches],
                            'truncatedCaptureRecovered':any(len(record['summary'])>len(c['text']) for c in matches)})
    (OUT/'recovered-replies.json').write_text(json.dumps(matched,ensure_ascii=False,indent=2))
    audit={'matchingRuntimeReplies':len(matched),'longerThanCapturedReplies':sum(r['truncatedCaptureRecovered'] for r in matched),
           'missingMarkerEditCandidates':sum(len(r['edits']) for r in records),'runtimeSessionsWithNoFinalReply':sum(not r['summary'] for r in records),
           'note':'These are retained artifacts, not new runs or revised pass-rate statistics. R16–R19 capture misassociation remains documented.'}
    (OUT/'runtime-recovery-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2));print(json.dumps(audit))

if __name__=='__main__':main()
