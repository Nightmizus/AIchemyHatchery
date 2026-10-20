"""Aggregate private, account-scoped exports into a credential-free audit.

Never copy prompts, final answers, raw tool output or account IDs to the report.
Unrecognized errors fail closed and require classification instead of silently
omitting errors. This script neither connects to production nor modifies history.
"""
import argparse
from collections import Counter
import json
from pathlib import Path


# tool, known error signature, category, disposition
RULES = [
    ('write_file', 'absolute paths', '写入越过本站目录', '保留安全拦截；不授予宿主机或其他网站权限'),
    ('browser_open', 'beginning with /', '浏览器拒绝站内相对路径', '已修复：站内相对路径规范化，仍验证归属和符号链接'),
    ('search_files', 'EISDIR', '搜索目录被当成文件', '已修复：目录递归搜索'),
    ('search_files', 'absolute paths', '搜索本站根目录被拒绝', '已修复：允许 . 和 ./，不允许 ..'),
    ('web_search', 'no web_search_tool_result', '搜索网关未返回原生搜索结果', '外部阻塞：需要兼容的搜索网关；未静默切换供应商或伪造结果'),
    ('web_fetch', 'fetch failed', '外部网页抓取网络失败', '外部错误：日志不足以区分 DNS、出口、TLS 或对方拒绝；不能宣称已根治'),
    ('web_fetch', 'cross-origin redirect', '抓取跨域重定向被拦截', '保留安全保护；模型可重新显式请求公开目标 URL'),
    ('web_fetch', 'unsupported content type', '网页响应格式不支持', '保留格式校验；非 HTML 响应不能假装已读取网页'),
    ('replace_file', 'arguments must be an object', '替换调用 JSON 参数损坏', '合法 JSON 字符串现可解析；损坏参数仍拒绝，提示转义和拆分，不猜测写入'),
    ('browser_open', 'stay inside this website', '浏览器把 /index.html 当控制台根路径', '已修复：映射到本站预览，其他预览仍不可访问'),
    ('web_fetch', 'non-public IP', '抓取域名解析到非公网地址', '保留 SSRF 保护；不为连通性解除私网限制'),
    ('site_service', 'Unknown website service action', '模型使用不存在的服务操作', '错误信息列出有效操作；仍拒绝未知操作'),
    ('read_file', 'ENOENT', '模型把外部网址当成本地文件', '保留文件边界；外部网页应使用 web_fetch，不扩展本地读权限'),
    ('write_file', 'arguments must be an object', '写文件调用 JSON 参数损坏', '合法 JSON 字符串现可解析；损坏输入不写文件并给出明确纠正提示'),
    ('site_service', 'Command must be', '服务命令超过 8000 字符', '已把上限加入工具 schema，并在执行前明确提示拆分；不提高命令上限'),
    ('replace_file', 'replace must be', '缺少 replace 被误报为长度超限', '已修复：分别报告参数缺失和长度超限'),
    ('replace_file', 'search text must occur', '替换目标与当前文件不匹配', '保留精确匹配保护；提示先读最新文件，绝不强制覆盖'),
    ('replace_file', 'search must be', '缺少 search 被误报为长度超限', '已修复：分别报告必填、长度和唯一匹配约束'),
    ('site_service', 'Backend did not become healthy', '生成的网站后端健康检查失败', '历史后续任务已修复 Unix socket 日志地址错误；本轮用复制源码回归'),
    ('browser_open', 'timed out', '浏览器等待页面加载超时', '新增 15 秒 DOM 捕获、独立无痕上下文；真实浏览器验证慢资源，非全部资源成功承诺'),
    ('site_service', '[object Object]', '服务错误对象显示成无效文字', '已修复：非字符串错误给出可读提示，不输出原始敏感对象'),
]

FAILURES = [
    ([31], '旧版整轮执行时间上限', '现有代码已取消总时限，本轮验证持续有进展的长任务不被旧环境变量终止'),
    ([54, 55, 57, 60, 62], '上游 HTTP 400 / INVALID_REQUEST', '已修复状态码丢失与错误分类；具体拒绝原因未保留，仍需上游网关日志，不能保证请求恢复'),
    ([53, 58], '恢复后仍只有推理、没有最终答复', '已修复误取旧回复/嵌套 JSON 为成功；缺失最终答复会明确失败并有限恢复，不展示推理'),
    ([61], '原生流停滞，恢复次数耗尽', '已修复 v3 嵌套进度识别和错误原因丢失；真正无输出仍有限恢复，保留文件，不无限重放写入'),
    ([56, 59], '数据库连接池耗尽，模型尚未开始', '已修复：连接槽有界等待、异常释放、24 并发真实 PostgreSQL 回归'),
]


def build(snapshot, runtime):
    counts = Counter()
    seqs = {}
    for session in runtime:
        for failure in session['failedTools']:
            text = json.dumps(failure['result'], ensure_ascii=False)
            matches = [i for i, rule in enumerate(RULES) if rule[0] == failure['tool'] and rule[1] in text]
            if len(matches) != 1:
                raise ValueError('unclassified tool failure; inspect the private export without printing it')
            i = matches[0]; counts[i] += 1
            seqs.setdefault(i, set()).add(session['jobSeq'])
    failed = {job['seq'] for job in snapshot['jobs'] if job['status'] == 'failed'}
    if failed != {seq for group, _, _ in FAILURES for seq in group}:
        raise ValueError('task failure set changed; audit the new private evidence first')
    categories = [{'tool': tool, 'category': category, 'count': counts[i], 'jobSeqs': sorted(seqs.get(i, [])), 'disposition': disposition}
                  for i, (tool, _, category, disposition) in enumerate(RULES)]
    return {
        'auditDate': '2026-10-19', 'accountName': '我', 'site': 'cogito',
        'scope': '该账号数据库中全部保留任务及匹配的原生日志；不等同于已删除或未保留的历史',
        'totals': {'jobs': len(snapshot['jobs']), 'jobStates': dict(Counter(j['status'] for j in snapshot['jobs'])),
                   'retainedNativeSessions': len(runtime), 'toolFailures': sum(counts.values()), 'toolFailureCategories': len(counts)},
        'countingNote': '11 个失败任务与 128 次工具失败是不同层级，可能同轮发生，不相加当作独立事故；20 项网站巡检发现也单独列出。',
        'jobFailures': [{'jobSeqs': group, 'count': len(group), 'cause': cause, 'disposition': disposition} for group, cause, disposition in FAILURES],
        'toolFailures': categories,
        'additionalConfirmedDefects': [
            {'defect': 'replace_file 对 $ 字符串二次解释，$$ 变成 $', 'affectedHistoricalCalls': 7,
             'fix': '使用字面量替换回调；覆盖 $$、$&、$`、$\'；旧网站当前选择器已由历史续作修复'},
            {'defect': 'v3 工具结果嵌套层级解析错误，失败被一直显示为运行中',
             'fix': '按 toolCallId 匹配真实结果；未确认操作在任务结束时标记 interrupted，不虚报成功'},
            {'defect': '预览动态代理只接受十六进制 ID，实际 ID 是 URL-safe 32 字符',
             'fix': '修正路由匹配；真实 HTTP 测试涵盖正确归属、未知 ID、凭据隔离'},
            {'defect': '最终报告解析误把 summary 内嵌 JSON 当完整回复', 'affectedJobSeqs': [49],
             'recoveredSummaryCharacters': 3186, 'fix': '仅解析完整报告或严格限定恢复；不修改线上历史'},
            {'defect': '原生进程退出后 stdout/stderr 管道未关闭',
             'fix': '回收进程组、读取线程及管道；真实离线原生协议回归'},
        ],
        'historicalSiteReview': [
            {'items': [1, 2, 3, 4, 7, 8], 'topic': '地点、活动时间、招新、投稿与栏目文案', 'status': '历史第 52 轮已修改；本轮复制源码核对，不覆盖后来用户内容'},
            {'items': [5, 9, 10, 11, 13, 16], 'topic': '动态统计、二维码、导航、图谱定位、上传文件 CSP', 'status': '历史已修复；本轮真实浏览器/本地后端复验'},
            {'items': [6, 20], 'topic': '预览快照与正式数据分叉', 'status': '本轮修复平台预览动态接口 404；离线时仍明确标示快照，未自动覆盖数据库或快照'},
            {'items': [12], 'topic': '上传媒体路径在预览中失效', 'status': '当前网站已使用相对媒体路径；本轮验证预览二维码 URL 与下载一致'},
            {'items': [14], 'topic': '无资源期数锚点', 'status': '当前源码保留无资源卡片；本轮在临时数据库解除全部绑定后验证锚点'},
            {'items': [15], 'topic': '空列表项', 'status': '本轮复现：4 行输入含 3 个空值仍渲染 4 个列表项。已修复本站副本并提供 ops/repairs/cogito-blank-list-items.patch；待授权部署'},
            {'items': [17], 'topic': '可能残留初始管理员凭据文件', 'status': '已只读确认本站 data 目录存在，initial_admin_password.txt 不存在；未读取凭据'},
            {'items': [18], 'topic': '重复页头页脚工程建议', 'status': '非已复现错误；不为消除建议而重写全部页面'},
            {'items': [19], 'topic': '未引用旧封面', 'status': '历史第 52 轮已删除；本轮不再删除文件'},
        ],
        'undeliveredRequests': [
            {'jobSeqs': [53, 58], 'request': 'AI 自动审核投稿', 'status': '当前网站仍为人工审核；失败任务没有完成此功能，不能谎称已实现。站点服务仍无平台密钥和任意出网权限。'},
            {'jobSeqs': [61], 'request': '投稿页面与审核管理面板', 'status': '该轮仅部分写入；当前后台仍叫投稿审核，未完成，不能把任务恢复机制修复等同功能交付。'},
        ],
        'verification': {'productionWrites': 0, 'realModelCalls': 0, 'deployment': 'not deployed',
                         'guarantee': '回归验证覆盖已复现的平台代码路径，不保证模型永不产生错误参数或外部网关永不故障。'},
        'recordedRegressionResults': {
            'date': '2026-10-19', 'note': '本次审查实际执行结果，不代表在每次生成统计报告时自动运行测试',
            'pythonTests': {'passed': 119, 'failed': 0},
            'browserChecks': {'passed': 46, 'failed': 0, 'suites': {
                'security_session_browser_tests.py': 6, 'ai_queue_browser_tests.py': 9,
                'ai_markdown_browser_tests.py': 9, 'preview_browser_tests.py': 8,
                'site_tool_browser_tests.mjs': 4, 'history_site_browser_tests.py': 10}},
            'nodeToolSuites': ['site_file_tools_tests.mjs: PASS', 'site_runtime_tool_tests.mjs: PASS'],
            'additionalChecks': ['git diff --check: PASS', '本站补丁反向应用检查: PASS', 'Python 编译检查: PASS'],
            'limitations': '网站回归使用私有源码副本与临时数据，不登录或写入生产网站；原生运行时使用离线适配器，不消耗真实模型额度。',
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private_directory', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    report = build(json.loads((args.private_directory / 'snapshot.json').read_text()),
                   json.loads((args.private_directory / 'runtime.json').read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['totals'], ensure_ascii=False))


if __name__ == '__main__': main()
