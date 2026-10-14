"""Opt-in real gateway integration: two real DSH calls in temporary website files.

.venv/bin/python scripts/site_files_real_smoke.py
Requires the configured gateway key. Does not write production sites or call a fake model.
"""
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_files_tests import FileFixture, USER, PROJECT
import server
import site_files

with FileFixture() as f:
    old_port = server.PORT; server.PORT = f.port
    (f.directory / 'keep.txt').write_text('Keep this unrelated file unchanged.\n')
    def progress(event):
        if event.get('kind') == 'tool':
            print(json.dumps({k: event.get(k) for k in ('tool', 'status', 'label')}, ensure_ascii=False), flush=True)
    result = f.handler.run_ai_harness(USER, '''请实际编辑当前网站文件，做一个最小可验证的页面，不要改 keep.txt。创建或改写 index.html：完整 HTML，链接 styles.css 与 script.js；有初始文本为 0 的 #counter、有 #increment 按钮（文本“加一”），点击按钮数字加一。styles.css 把 body 背景设置为 rgb(240,244,255)。JavaScript 单独写入 script.js。创建 about/index.html，包含文本 About direct files；首页用相对链接 about/ 指向它。保留 site.json 为基本信息；不要嵌入页面代码。使用当前预览地址检查加载结果。''',
        {'site': {'pages': [{'code': 'THIS STALE CLIENT DATA MUST NEVER REPLACE DISK'}]}}, True, [], [], progress=progress)
    assert (f.directory / 'keep.txt').read_text() == 'Keep this unrelated file unchanged.\n'
    assert {'index.html', 'styles.css', 'script.js', 'about/index.html'} <= set(result['siteFiles']['files']), result
    assert 'pages' not in site_files.read_metadata(f.directory)
    before = site_files.file_manifest(f.directory)
    second = f.handler.run_ai_harness(USER, '只修改 index.html 中 #increment 按钮的显示文案，从“加一”改成“再加一”。请实际修改文件，不要改其他文件。无需再做浏览器检查。', {'site': {'pages': []}}, True, [], [], progress=progress)
    after = site_files.file_manifest(f.directory)
    assert '再加一' in (f.directory / 'index.html').read_text()
    assert [n for n in before.keys() | after.keys() if before.get(n) != after.get(n)] == ['index.html']
    artifacts = Path(tempfile.mkdtemp(prefix='hatchery-real-files-artifacts-'))
    shutil.copytree(f.directory, artifacts / 'site')
    (artifacts / 'result.json').write_text(json.dumps({'first': result, 'second': second}, ensure_ascii=False))
    print(f'Real model artifacts: {artifacts}', flush=True)
    subprocess.run([str(PROJECT / '.shots-venv/bin/python'), str(PROJECT / 'scripts/site_files_browser_tests.py'), str(f.port)], check=True)
    print(json.dumps({'provider': 'real configured DeepSeek gateway', 'run1ChangedFiles': result['changedFiles'], 'run2ChangedFiles': second['changedFiles'], 'usage': [result['usage'], second['usage']], 'checks': result['checks']}, ensure_ascii=False), flush=True)
    server.PORT = old_port
