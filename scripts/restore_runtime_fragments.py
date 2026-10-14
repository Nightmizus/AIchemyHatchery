"""Restore the two lost blocks from saved runtime edit arguments, with provenance."""
import hashlib
import json
from pathlib import Path
import re
import os

NAMESPACE='AIUI40_20261012T034719Z'


def fragments(records):
    result=[]
    for record in records:
        for edit in record['edits']:
            for identity,state in [('r28-two-tabs-diff-session','r28-a-result-state'),('r31-new-tab','r31-new-tab-state')]:
                match=re.search(r"<section\b[^>]*id='"+identity+r"'[^>]*>.*?</section>",edit['content'],re.S)
                if match:
                    result.append({'id':identity,'stateId':state,'html':match.group(0),'session':record['session'],
                        'sequence':edit['sequence'],'sourceSha256':record['sourceSha256']})
    if len(result)!=2:raise ValueError('Expected exactly the two saved original blocks')
    return result


def restore(directory, saved):
    page=Path(directory)/NAMESPACE/'index.html'
    before=text=page.read_text()
    if 'data-hatchery-runtime-restored="20261013"' in text:return {'alreadyRestored':True}
    note=re.compile(r'<section data-hatchery-repair="20261013".*?</section>',re.S)
    if len(note.findall(text))!=1:raise ValueError('Expected the reviewed repair note; stop before modifying a changed page')
    text=note.sub('',text)
    existing=set(re.findall(r'\bid=["\x27]([^"\x27]+)',text))
    restored=[]
    for source in saved:
        block=source['html']
        # Preserve original source in the evidence file; correct its known false status on the live page.
        block=re.sub(r"(id='"+source['stateId']+r"'[^>]*>).*?(</span>)",r'\1从原始运行时记录恢复；旧版覆盖问题已确认\2',block,count=1,flags=re.S)
        for identity in re.findall(r"\bid='([^']+)'",block):
            if identity in existing:
                block=block.replace("id='"+identity+"'","id='"+identity+"-restored'")
                block=block.replace("aria-labelledby='"+identity+"'","aria-labelledby='"+identity+"-restored'")
        restored.append(block)
    note='''<section data-hatchery-runtime-restored="20261013" style="padding:24px;margin:24px 0;border:1px solid currentColor;border-radius:12px">
<h2>原始区块恢复记录</h2><p>R28-A 与 R31 新标签区块来自原始运行时编辑日志，已恢复其内容与结构；
旧状态文字已按实际证据更正。原测试中的覆盖和历史回退仍判定为失败，不计作新的 AI 测试通过。</p>
<p>本页其他轮次保留为历史记录，其中自述不能替代实际验收。详情页保留。完整运行时回复已另行归档。</p></section>'''
    text=text.replace('</main>',''.join(restored)+note+'</main>',1)
    text=text.replace('旧版新标签标记曾缺失；本次已按保存的请求补建','旧版新标签标记曾缺失；现已从原始运行时记录恢复')
    temporary=page.with_suffix('.restore.tmp');temporary.write_text(text);os.replace(temporary,page)
    return {'beforeSha256':hashlib.sha256(before.encode()).hexdigest(),'afterSha256':hashlib.sha256(text.encode()).hexdigest(),
            'restored':[{'id':s['id'],'session':s['session'],'sequence':s['sequence'],'sourceSha256':s['sourceSha256']} for s in saved]}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('site_directory');parser.add_argument('fragments_json');a=parser.parse_args()
    print(json.dumps(restore(a.site_directory,json.loads(Path(a.fragments_json).read_text())),ensure_ascii=False))
