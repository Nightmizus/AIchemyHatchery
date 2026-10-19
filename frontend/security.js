/* Website-only security jobs use the same durable queue and model as AI chat. */
(() => {
  'use strict';
  let options, generation = 0, loading = false, submitting = false, target = null, job = null, uncertain = false;
  const $ = selector => document.querySelector(selector);
  const active = value => ['queued', 'running'].includes(value?.status);
  const pendingKey = user => `alchemyhatchery:${user.id}:pending-security-request`;
  function pending(user) { try { return sessionStorage.getItem(pendingKey(user)); } catch { return null; } }
  function clearPending(user, id) { try { if (pending(user) === id) sessionStorage.removeItem(pendingKey(user)); } catch {} }
  function setStatus(text) { $('#securityStatus').textContent = text; }
  function updateButton() {
    const button = $('#securityRunBtn');
    button.disabled = submitting || active(job) || !target;
    button.textContent = submitting ? '正在提交…' : active(job) ? (job.status === 'queued' ? '已排队' : '正在测试…') : uncertain ? '确认提交 / 重试' : job ? '重新测试本站' : '开始测试本站';
  }
  function reset() {
    generation++; loading = submitting = uncertain = false; target = job = null;
    $('#securityTarget').textContent = '正在确认你的网站…';
    setStatus('读取本站信息后即可测试');
    $('#securityResults').replaceChildren();
    $('#securityChecks').hidden = true;
    $('#securitySummary').hidden = true;
    $('#securityReport').hidden = true;
    $('#securityReportBody').replaceChildren();
    delete $('#securityReportBody').dataset.aiMarkdownSource;
    $('#securityProcess').hidden = true;
    $('#securityEvents').replaceChildren();
    updateButton();
  }
  function renderChecks(security) {
    const checks = security?.checks || [], list = $('#securityResults');
    list.replaceChildren();
    const counts = {pass: 0, warn: 0, fail: 0, skip: 0};
    for (const item of checks) {
      const status = Object.hasOwn(counts, item.status) ? item.status : 'warn'; counts[status]++;
      const row = document.createElement('article'); row.className = `security-check ${status}`;
      const icon = document.createElement('i'); icon.textContent = {pass: '✓', warn: '!', fail: '×', skip: '–'}[status]; icon.setAttribute('aria-hidden', 'true');
      const content = document.createElement('div'), title = document.createElement('b'), detail = document.createElement('small');
      title.textContent = item.name; detail.textContent = item.detail; content.append(title, detail); row.append(icon, content); list.append(row);
    }
    const summary = $('#securitySummary'); summary.hidden = !checks.length;
    $('#securityChecks').hidden = !checks.length;
    summary.textContent = `基础检查：${counts.fail} 项确认风险，${counts.warn} 项待复核，${counts.pass} 项通过，${counts.skip} 项未验证。AI 源码分析见下方报告。`;
  }
  function render() {
    const security = job?.result?.security || [...(job?.events || [])].reverse().find(event => event.security)?.security;
    const tested = security?.target || target;
    $('#securityTarget').textContent = tested ? `${tested.name} · ${tested.published ? '已发布网站' : '草稿网站'}\n${tested.url}${tested.url !== tested.path ? `\nHTTP 检查路径：${tested.path}` : ''}` : '尚未取得本站信息';
    if (uncertain) setStatus('提交结果尚未确认。重试会沿用同一请求编号，避免重复测试。');
    else if (!job) setStatus('检查本站响应和源码；使用当前账号的模型配置。');
    else if (job.status === 'queued') setStatus('已在服务器排队，轮到本站后自动开始；关闭页面仍会继续。');
    else if (job.status === 'running') setStatus(security ? '基础检查已取得结果，AI 正在只读审查本站源码…' : '正在检查本站 HTTP 响应…');
    else if (job.status === 'completed') setStatus(`测试完成 · ${job.finishedAt ? new Date(job.finishedAt).toLocaleString() : ''}；报告对应测试时的网站版本。`);
    else setStatus(`测试未完成：${job.error || '任务中断'}。下方保留已完成的基础检查，可重新测试。`);
    renderChecks(security);
    const events = (job?.events || []).filter(event => event.kind !== 'context');
    $('#securityProcess').hidden = !events.length;
    $('#securityEvents').replaceChildren(...events.map(event => {
      const line = document.createElement('p'); line.textContent = `${event.label || ''} ${event.detail || ''}`; return line;
    }));
    const report = $('#securityReport'), body = $('#securityReportBody'), text = job?.status === 'completed' ? job.result?.summary : '';
    report.hidden = !text;
    if (text && body.dataset.aiMarkdownSource !== text) window.AIchemyHatcheryMarkdown.renderInto(body, text);
    $('#securityReportMeta').textContent = security ? [security.model ? `模型：${security.model}` : '', security.scope].filter(Boolean).join('\n') : '';
    updateButton();
  }
  async function refresh() {
    const user = options?.getUser();
    if (!user || loading || submitting) return;
    const version = generation; loading = true;
    try {
      const payload = await options.request('/api/security/latest');
      if (version !== generation || user !== options.getUser()) return;
      target = payload.target; job = payload.job;
      if (job && pending(user) === job.requestId) { clearPending(user, job.requestId); uncertain = false; }
      else uncertain = Boolean(pending(user));
      render();
    } catch (error) {
      if (version !== generation || user !== options.getUser()) return;
      setStatus(`暂时无法读取测试状态：${error.message}。已有任务不会因此终止。`);
    } finally { if (version === generation) { loading = false; updateButton(); } }
  }
  async function run() {
    const user = options?.getUser();
    if (!user || submitting || active(job) || !target) return;
    const version = ++generation; loading = false;
    let requestId;
    try { requestId = pending(user) || options.createRequestId(); }
    catch { setStatus('无法生成请求编号，测试尚未提交，请刷新后重试。'); return; }
    try { sessionStorage.setItem(pendingKey(user), requestId); } catch {}
    submitting = true; uncertain = false; updateButton(); setStatus('正在提交，尚未确认进入服务器队列…');
    try {
      let accepted;
      for (let attempt = 0; attempt < 3; attempt++) {
        if (version !== generation || user !== options.getUser()) return;
        try {
          accepted = await options.request('/api/security/run', {method: 'POST', body: {requestId}});
          if (!accepted.jobId || accepted.requestId !== requestId || accepted.kind !== 'security' ||
              !['queued', 'running', 'completed', 'failed', 'interrupted'].includes(accepted.status)) throw new Error('服务器未确认测试任务');
          break;
        } catch (error) { if ((error.status && error.status < 500) || attempt === 2) throw error; }
      }
      if (version !== generation || user !== options.getUser()) return;
      job = accepted; clearPending(user, requestId); render();
    } catch (error) {
      if (version !== generation || user !== options.getUser()) return;
      uncertain = !error.status || error.status >= 500;
      if (!uncertain) clearPending(user, requestId);
      setStatus(uncertain ? '提交结果尚未确认。请重试，将沿用同一请求编号以避免重复测试。' : `提交失败：${error.message}`);
    } finally { if (version === generation) { submitting = false; updateButton(); } }
  }
  function init(config) {
    options = config; reset();
    $('#securityRunBtn').addEventListener('click', () => void run());
    setInterval(() => { if (!document.hidden && !$('.security-panel').hidden) void refresh(); }, 2500);
    window.addEventListener('online', () => { if (!$('.security-panel').hidden) void refresh(); });
  }
  window.AIchemyHatcherySecurity = Object.freeze({init, reset, refresh});
})();
