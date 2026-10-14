"""Reconcile the 21 reported findings with repair and validation evidence."""
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/ai-fixes/20261013'
ORIGINAL=ROOT/'output/ai-loadtest/20261013T000233Z-issue-review'
RESOLUTIONS={
 'AI-001':('已修复并恢复原始区块','服务器以真实文件为准；同账号 FIFO；跨进程文件锁；修改前备份。R28-A/R31-NEW 已从原始运行时编辑日志恢复内容与结构，并校正已知错误状态说明。','真实 PostgreSQL + 双标签页本地回归；原测试页实际交互'),
 'AI-002':('已修复；历史残缺单独归档','历史由服务器追加生成，拒绝旧页面整段覆盖；确认结果后仍可读取；不再裁剪 DOM。保留旧会话与原始证据；找回 36 条可对应原记录的运行时回复，另行归档，不伪造没有终态的任务结果。','重复请求幂等、确认后读取、刷新及 A/B 前进后退'),
 'AI-003':('已修复','队列和终态持久化；重启将失去租约的任务标为中断，不重放；旧虚假排队标签显示无法自动恢复。','队列重启、过期任务、结果落盘补偿测试'),
 'AI-004':('保护与诊断已修复；上游表现未重测','每次循环检查时间上限；无关通知不能续命；不吞协议错误；接受新会话实际进展；进程组收尾。仅无进展且文件未变化时允许一次重试。','6 项协议/看门狗回归；没有重跑真实 AI 压测，不能保证上游不再停滞'),
 'AI-005':('IP 路径入口已修复','用户选择暂不启用域名。发布、Gallery、账号站点地址使用 /pages/<slug>/，发布后检查实际可达性；域名和 HTTPS 保持暂停。','本地发布和下载检查；生产 IP 访问记录'),
 'AI-006':('已修复','保存和展示完整 summary/plan，移除 500/1000/8000 字符裁剪；输出触顶不记为成功。从运行时日志找回 21 条比旧采集版本更完整的回复；仅恢复有原始来源的文本。','超过 1000 字符回复与超过 8000 字符计划回归'),
 'AI-007':('已修复','打包 site_files.py、ai_jobs.py 与前端文件接口；旧站点一次性导出真实 HTML/CSS/JS；site.json 仅保留元信息。','文件迁移与接口、生产文件清单及模块校验'),
 'AI-008':('已修复','提供真实独立 JavaScript 文件操作；原 R05 测试页已接入 r05-counter.js，替换三次 CSS 状态模拟。','计数器实际 0→3→5，键盘和外链脚本加载'),
 'AI-009':('已修复','同账号请求进入服务器队列，真实按序执行；只在编号冲突或队列满时拒绝，不承诺未受理的请求会自动执行。','同账号跨会话 FIFO、跨账号全局并发上限、请求幂等'),
 'AI-010':('已修复','缺少用量写 NULL、显示未知；汇总标出未知次数。未查询模型用量、余额或账单，也不重算历史消耗。','缺少值与明确零值分开验证'),
 'AI-011':('已修复展示与已知错误文案','工具能力与验证要求写入提示；检查项标为 AI 提供的说明；文件变化由服务器计算。原 R27/R31 的错误状态声明已更正。','独立浏览器交互，不采用 AI 自述作为通过依据'),
 'AI-012':('已核实，无需恢复详情页','生产草稿、预览、发布快照均有三页，详情页未丢失；迁移保留三个内容页和 home 路由兼容页。','只读生产 recovery-audit.json + 文件迁移核对'),
 'TEST-001':('已修复','以当前 jobId 的结构化终态判定，普通中文“失败判据”不会被当作失败。','长回复含该词仍通过'),
 'TEST-002':('已修复','每条请求保存并交叉检查 jobId/requestId/sessionId；不借用前一任务摘要。','新回归支持库和浏览器执行'),
 'TEST-003':('已修复','未知状态、超时或失败立即中止顺序用例；并发只在显式并发用例发起。旧内存测试器已停用。','独立服务器队列测试；新脚本无批量真实调用'),
 'TEST-004':('已补齐本地验证','执行三次独立提交并等待各自终态，核对同一会话和前次修改保留。','浏览器三个真实 HTTP 提交，执行器为固定本地实现'),
 'TEST-005':('已补齐本地验证','直接在 A/B 之间导航、后退、前进，分别核对地址、选中项、全套消息和上下文环。','浏览器 A/B 回归'),
 'TEST-006':('已补齐本地验证','实际点击、Tab/Enter/Space、下载文件内容、CSS、相对链接往返、手机溢出、刷新。另修复隔离页面下载被浏览器忽略的问题。','15 项浏览器检查及原测试页面检查；原来的真实 AI 轮次不改判为通过'),
 'TEST-007':('已修复','比较全部请求/任务 ID；只读模式比较所有文件内容哈希；不以当前轮号出现代表完整恢复。','完整历史与 chat/plan 文件清单哈希回归'),
 'TEST-008':('已修复交付方式','保留原 46 条记录与纠错报告，修复情况另存；脚本留在仓库；账号内归档为系统导入报告，不伪造 AI 会话或新测试轮次。','本报告、结构化清单、完整回复归档与账号导入记录'),
 'TEST-009':('已修复证据分发边界','原会话导出保留但设为 0600，加入 Git 忽略；output、私有快照和结果缓冲不进入仓库/部署包。报告不复制口令或连接串。','显式发布清单；不把本地敏感导出当作可分发附件；未擅自轮换凭据'),
}


def main():
    issues=json.loads((ORIGINAL/'issues.json').read_text())
    deployment=json.loads((OUT/'deployment.json').read_text()) if (OUT/'deployment.json').exists() else {'state':'prepared'}
    records=[]
    for item in issues:
        state,change,validation=RESOLUTIONS[item['id']]
        records.append({'id':item['id'],'title':item['title'],'state':state,'change':change,'validation':validation})
    (OUT/'resolutions.json').write_text(json.dumps({'deployment':deployment,'realAiCalls':0,'findings':records},ensure_ascii=False,indent=2))
    lines=['# AIchemyHatchery 问题修复与验证记录','',
        '已处理原报告的 21 项发现。真实 AI 压测保持停止；本次没有新增模型调用，也没有查询用量、余额或账单。', '',
        '发布入口按用户选择保留为 IP 路径。DNS 和 HTTPS 暂不启用。原测试页详情页仍在；两个缺失区块已从原始运行时编辑日志恢复。36 条原始回复另行归档，其中 21 条比旧采集版本更完整；没有留存终态的任务不补写结果。','',
        f"部署状态：{deployment['state']}。",'',
        '| 编号 | 处理结果 | 修复与验证 |','|---|---|---|']
    for r in records:lines.append(f"| {r['id']} | {r['state']} | {r['change']} 验证：{r['validation']} |")
    lines += ['', '## 验证范围', '',
        '- 8 项真实 PostgreSQL 队列和历史测试、8 项文件/发布测试、6 项看门狗测试、7 项上下文与配置测试，以及 Node 文件工具测试通过。',
        '- 本地浏览器通过双标签、三次独立提交、长回复、A/B 导航、只读文件哈希、点击、键盘、下载、CSS、链接和手机视口检查。模型边界使用固定测试执行器，不能替代真实 AI 质量或容量结论。',
        '- 原测试网站副本实际验证：独立 JS 计数 0→3→5，Enter/Space 切换，下载内容首行为 R18-DOWNLOAD，四个并发标记同时存在，390px 视口无横向溢出。两个原始区块恢复后已再次检查。',
        '- 上游长期无响应仍可能发生；修复确保有明确终态、保留文件和可恢复的结果记录。', '',
        '## 文件与回滚', '',
        '- 原报告和证据保持原样：output/ai-loadtest/20261013T000233Z-issue-review/。',
        '- 本次验证与逐项结果：browser-validation.json、recorded-site-validation.json、recovery-audit.json、resolutions.json。',
        '- 发布文件严格来自 release-manifest.json。生产备份位置见 deployment.json；恢复旧代码和站点时应先停止服务并核对备份之后的新写入，不能覆盖后来产生的用户内容。',
        '- 未提交或推送 Git。', '']
    (OUT/'repair-report.md').write_text('\n'.join(lines))
    # This is an explicitly labelled evidence archive, not fabricated chat replay.
    fragments=['<article class="ai-message assistant"><div><h2>系统导入：问题修复与历史证据</h2><p>本记录由维护流程导入，没有调用模型。原会话保留；历史摘录可能包含已被纠正的错误陈述，不代表恢复了原始完整会话。</p>']
    for r in records:fragments.append('<p>'+html.escape(r['id']+' '+r['title']+'\n'+r['state']+'：'+r['change']+'\n验证：'+r['validation'])+'</p>')
    fragments.append('</div></article>')
    rows=[json.loads(l) for l in (ROOT/'output/ai-ui-40rounds/20261012T034719Z/rounds.jsonl').read_text().splitlines()]
    for index,row in enumerate(rows,1):
        if row.get('round') not in ('R27','R28','R29','R30','R31','R32','R33','R34','R35','R36','R37','R38','R39','R40'):continue
        evidence={k:v for k,v in row.items() if k in ('messages_tail','a_tail','b_tail','after_tail','terminal_summary','new_tab','old_tab','result_a','result_b')}
        if not evidence:continue
        fragments.append('<details class="ai-run-process"><summary>'+html.escape(f"原始记录 {row['round']} · rounds.jsonl 第 {index} 行")+'</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'+html.escape(json.dumps(evidence,ensure_ascii=False,indent=2))+'</pre></details>')
    (OUT/'account-report.html').write_text(''.join(fragments))
    print(json.dumps({'findings':len(records),'report':str(OUT/'repair-report.md'),'deployment':deployment['state']},ensure_ascii=False))


if __name__=='__main__':main()
