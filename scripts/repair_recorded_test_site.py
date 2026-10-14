"""Repair the recorded AIUI40 test page only, after the files migration.

Reinstated markers are explicitly labelled reconstruction from saved requests;
they are not presented as recovered original HTML or new real-AI test passes.
"""
from pathlib import Path
import re
from urllib.parse import unquote

NAMESPACE = 'AIUI40_20261012T034719Z'
COUNTER = """const button=document.querySelector('#r05-counter-button');
const output=document.querySelector('#r05-counter-value');
let count=0;
if(button&&output){output.textContent='0';output.setAttribute('aria-live','polite');
button.addEventListener('click',()=>{output.textContent=String(++count)});}
"""


def repair(directory):
    root = Path(directory)
    page = root / NAMESPACE / 'index.html'
    original = text = page.read_text()
    if 'data-hatchery-repair="20261013"' in text or 'data-hatchery-runtime-restored="20261013"' in text:
        return []
    text, count = re.subn(r"<span class='r05-btn' id='r05-counter-button'>.*?</span>",
                          "<button class='r05-btn' id='r05-counter-button' type='button'>点击 +1</button>", text)
    if count != 1:
        raise ValueError('Recorded counter markup changed; stop for review')
    text = re.sub(r"<input class='r05-cb'[^>]*>", '', text)
    text = re.sub(r"<label class='r05-hit'[^>]*>.*?</label>", '', text)
    text = text.replace('脚本文件：<b>无</b>（未新增 r05-counter.js：本页不加载任何外部或内联脚本，计数由 CSS 状态推进）',
                        '脚本文件：<b>r05-counter.js</b>（独立 JavaScript，每次点击加一）')
    text = text.replace('计数由 CSS 状态推进', '计数由独立 JavaScript 更新')
    text = text.replace('标签页状态：双标签一致 · 同一份站点数据', '历史核验：旧版本曾出现标签页历史分叉；本次已修复持久化')
    text = text.replace('操作状态：旧标签追加写入 · 新标签内容未覆盖', '历史核验：旧版新标签标记曾缺失；本次已按保存的请求补建')
    match = re.search(r"id='r18-download-link' href='data:text/plain;charset=utf-8,([^']+)'", text)
    if not match:
        raise ValueError('Recorded download contents changed; stop for review')
    download = unquote(match.group(1))
    text = text.replace('data:text/plain;charset=utf-8,' + match.group(1), 'R18-download-test.txt')
    text = text.replace('不跳转页面、不发起网络请求', '由本站真实文件提供下载')
    note = '''<section data-hatchery-repair="20261013" style="padding:24px;margin:24px 0;border:1px solid currentColor;border-radius:12px">
<h2>历史测试内容修复记录</h2><p>以下两项标记在旧版本快照中缺失，现根据保存的用户请求补建。
原始区块未能找回；这不代表旧测试通过，也不是一次新的 AI 测试。</p>
<p id="r28-a-result-mark">R28-A-RESULT</p><p id="r31-new-tab-mark">R31-NEW-TAB-RESULT</p>
<p>本页其他轮次保留为历史记录；其中的自述不能替代实际验收。详情页保留。</p></section>
<style>#r05-counter-value::after{display:none!important}#r05-counter-button{cursor:pointer;font:inherit}
.r05-row,.r06-row{min-width:0}.r18-pre{max-width:100%;overflow:auto}
</style><script src="r05-counter.js" defer></script>'''
    text = text.replace('</main>', note + '</main>', 1)
    if text == original or 'data-hatchery-repair="20261013"' not in text:
        raise ValueError('Test page repair could not be placed safely')
    (page.parent / 'r05-counter.js').write_text(COUNTER)
    (page.parent / 'R18-download-test.txt').write_text(download)
    page.write_text(text)
    return [str(p.relative_to(root)) for p in (page, page.parent / 'r05-counter.js', page.parent / 'R18-download-test.txt')]


if __name__ == '__main__':
    import argparse,json
    parser=argparse.ArgumentParser();parser.add_argument('site_directory');args=parser.parse_args()
    print(json.dumps({'changed':repair(args.site_directory)}, ensure_ascii=False))
