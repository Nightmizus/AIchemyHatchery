// HatcheryStudy — 学习视图（复刻 studypanel：Markdown + KaTeX 课程渲染、流式输出）
(function () {
  'use strict';

  let busy = false;
  let open = false;
  let md = null;          // markdown-it 实例
  let libsReady = false;
  let sessionId = 'default';   // 当前会话
  let sessions = [];           // 会话列表
  let providerId = 'default';  // 当前平台
  let modelName = '';          // 当前模型（空=平台默认）
  let providers = [];          // 平台列表

  function $(id) { return document.getElementById(id); }
  function historyEl() { return $('studyHistory'); }
  function messagesEl() { return $('studyMessages'); }
  function promptEl() { return $('studyPrompt'); }
  function sendBtn() { return $('studySendBtn'); }
  function outlinesBar() { return $('studyOutlinesBar'); }
  function studyBtn() { return $('hatcheryStudyBtn'); }
  function sessionPop() { return $('studySessionPop'); }
  function configPop() { return $('studyConfigPop'); }

  // 当前平台/模型的短标签，显示在输入框上方
  function currentLabel() {
    const p = providers.find(x => x.id === providerId);
    const base = p ? (p.builtin ? '默认' : p.label) : providerId;
    return modelName ? `${base} · ${modelName}` : base;
  }

  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function scrollToEnd() {
    const el = historyEl();
    if (el) el.scrollTop = el.scrollHeight;
  }

  // ── Markdown + KaTeX：复刻 studypanel 的 studyMarkdown.ts ──
  function loadScript(src, globalName) {
    return new Promise((resolve, reject) => {
      if (window[globalName]) { resolve(); return; }
      const s = document.createElement('script');
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('资源加载失败：' + src));
      document.head.appendChild(s);
    });
  }
  function loadStyle(href) {
    return new Promise(resolve => {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = href;
      l.onload = l.onerror = () => resolve();
      document.head.appendChild(l);
    });
  }

  async function ensureLibs() {
    if (libsReady) return;
    await Promise.all([
      loadStyle('https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css'),
      loadScript('https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js', 'markdownit'),
      loadScript('https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js', 'katex'),
    ]);
    md = window.markdownit({
      html: false,
      linkify: true,
      breaks: false,
    });
    libsReady = true;
  }

  const MATH_OPTIONS = { throwOnError: false, strict: 'ignore', maxExpand: 1000, output: 'htmlAndMathml' };

  // 先提取 $...$ / $$...$$ / \(..\) / \[..\] 数学段，避免 Markdown 破坏 LaTeX
  function renderMath(html) {
    if (!window.katex) return html;
    const patterns = [
      { re: /\$\$([\s\S]+?)\$\$/g, display: true },
      { re: /\\\[([\s\S]+?)\\\]/g, display: true },
      { re: /\\\(([\s\S]+?)\\\)/g, display: false },
      { re: /\$([^\n$]+?)\$/g, display: false },
    ];
    for (const { re, display } of patterns) {
      html = html.replace(re, (match, body) => {
        try {
          return window.katex.renderToString(body.trim(), { ...MATH_OPTIONS, displayMode: display });
        } catch { return match; }
      });
    }
    return html;
  }

  function renderMarkdown(source) {
    if (!source) return '';
    // 模型偶尔把整个回答包在 ```markdown 里
    const envelope = /^\s*(`{3,}|~{3,})(?:markdown|md)\s*\n([\s\S]*?)\n\1\s*$/i.exec(source);
    const text = envelope ? envelope[2] : source;
    const html = md ? md.render(text) : `<p>${esc(text)}</p>`;
    return renderMath(html);
  }

  // ── 消息渲染 ──
  function appendUser(text) {
    const box = messagesEl();
    if (!box) return null;
    const art = document.createElement('article');
    art.className = 'study-turn user';
    art.innerHTML = `<p class="study-role">你</p><p>${esc(text)}</p>`;
    box.appendChild(art);
    scrollToEnd();
    return art;
  }

  function appendAssistant(title, markdown) {
    const box = messagesEl();
    if (!box) return null;
    const art = document.createElement('article');
    art.className = 'study-turn assistant';
    const heading = title ? `<p class="study-role">${esc(title)}</p>` : '<p class="study-role">教练</p>';
    art.innerHTML = `${heading}<div class="study-lesson">${renderMarkdown(markdown)}</div>`;
    box.appendChild(art);
    scrollToEnd();
    return art;
  }

  function appendError(message) {
    const box = messagesEl();
    if (!box) return;
    const div = document.createElement('div');
    div.className = 'study-error';
    div.textContent = message;
    box.appendChild(div);
    scrollToEnd();
  }

  // ── 大纲 ──
  async function loadOutlines() {
    const bar = outlinesBar();
    if (!bar) return;
    try {
      const payload = await window.consoleRequest('/api/study/outlines');
      const list = payload.outlines || [];
      const chips = list.map(f => `<span class="study-chip" title="${esc(f.name)}">${esc(f.name)}</span>`).join('');
      bar.innerHTML = (chips || '<span class="study-empty">未上传大纲</span>')
        + '<button class="study-add" id="studyAddOutlineBtn">＋ 上传</button>'
        + '<input type="file" id="studyOutlineInput" accept="*/*" multiple hidden>';
      bindOutlineUpload();
    } catch (e) {
      bar.innerHTML = `<span class="study-empty" style="color:#ff7b72">${esc(e.message)}</span>`;
    }
  }

  function bindOutlineUpload() {
    const btn = $('studyAddOutlineBtn');
    const input = $('studyOutlineInput');
    btn?.addEventListener('click', () => input?.click());
    input?.addEventListener('change', async e => {
      const files = Array.from(e.target.files || []);
      e.target.value = '';
      if (!files.length) return;
      btn.disabled = true;
      let ok = 0;
      for (const file of files) {
        if (file.size > 500 * 1024) { window.showToast?.(`${file.name} 超过 500 KB，已跳过`); continue; }
        let content;
        try { content = await file.text(); } catch { window.showToast?.(`${file.name} 读取失败`); continue; }
        try {
          await window.consoleRequest('/api/study/outlines', { method: 'POST', body: { name: file.name, content } });
          ok++;
        } catch (err) {
          window.showToast?.(`上传 ${file.name} 失败：${err.message}`);
        }
      }
      btn.disabled = false;
      if (ok) { window.showToast?.(`已上传 ${ok} 份大纲`); loadOutlines(); }
    });
  }

  // ── 历史 ──
  async function loadHistory() {
    const box = messagesEl();
    if (!box) return;
    try {
      const payload = await window.consoleRequest(
        '/api/study/history?session=' + encodeURIComponent(sessionId));
      const messages = payload.messages || [];
      if (payload.sessionId) sessionId = payload.sessionId;
      sessions = payload.sessions || [];
      const dot = $('studyProgressDot');
      if (dot) dot.hidden = !payload.hasProgress;
      if (!messages.length) {
        box.innerHTML = welcomeMarkup();
        bindStarters();
        return;
      }
      box.innerHTML = messages.map(m => {
        if (m.role === 'user') {
          return `<article class="study-turn user"><p class="study-role">你</p><p>${esc(m.raw_text || '')}</p></article>`;
        }
        const heading = m.title ? `<p class="study-role">${esc(m.title)}</p>` : '<p class="study-role">教练</p>';
        return `<article class="study-turn assistant">${heading}<div class="study-lesson">${renderMarkdown(m.markdown || m.raw_text || '')}</div></article>`;
      }).join('');
      scrollToEnd();
    } catch (e) {
      box.innerHTML = `<div class="study-error">${esc(e.message)}</div>`;
    }
  }

  // ── 会话切换（简陋版）──
  function togglePop(pop) {
    const other = pop === sessionPop() ? configPop() : sessionPop();
    if (other) other.hidden = true;
    if (!pop) return;
    pop.hidden = !pop.hidden;
  }

  function renderSessions() {
    const pop = sessionPop();
    if (!pop) return;
    const rows = sessions.map(s => {
      const label = (s.last_title || s.first_prompt || '(空会话)').slice(0, 40);
      const when = String(s.updated_at || '').replace('T', ' ').slice(5, 16);
      const active = s.session_id === sessionId;
      return `<div class="study-sess${active ? ' active' : ''}" data-sess="${esc(s.session_id)}">
        <div><b>${esc(label)}</b><small>${s.turns || 0} 轮 · ${esc(when)}</small></div>
        <button data-sess-del="${esc(s.session_id)}" title="删除">×</button>
      </div>`;
    }).join('') || '<p class="study-pop-note">还没有会话</p>';
    pop.innerHTML = `<h4>会话<small>${sessions.length} 个</small></h4>${rows}
      <p class="study-pop-note">点会话切换，× 删除该会话全部消息与进度。</p>`;
  }

  async function newSession() {
    sessionId = 's' + Date.now().toString(36);
    if (sessionPop()) sessionPop().hidden = true;
    if (configPop()) configPop().hidden = true;
    await loadHistory();
    promptEl()?.focus();
  }

  async function switchSession(id) {
    sessionId = id;
    if (sessionPop()) sessionPop().hidden = true;
    await loadHistory();
  }

  async function deleteSession(id) {
    if (!confirm('删除该会话的全部消息和进度？')) return;
    try {
      await window.consoleRequest('/api/study/session/delete', {
        method: 'POST', body: { sessionId: id },
      });
      if (id === sessionId) sessionId = 'default';
      await loadHistory();
      renderSessions();
      window.showToast?.('已删除会话');
    } catch (e) {
      window.showToast?.('删除失败：' + e.message);
    }
  }

  // ── 平台/模型配置（简陋版）──
  async function loadProviders() {
    try {
      const payload = await window.consoleRequest('/api/study/providers');
      providers = payload.providers || [];
    } catch {
      providers = [];
    }
  }

  function renderConfig() {
    const pop = configPop();
    if (!pop) return;
    const opts = providers.map(p =>
      `<option value="${esc(p.id)}"${p.id === providerId ? ' selected' : ''}>${esc(p.label)}${p.hasKey ? '' : '（无 Key）'}</option>`
    ).join('');
    const list = providers.filter(p => !p.builtin).map(p =>
      `<li><b>${esc(p.label)}</b><em>${esc(p.model || '默认')}</em>
        <button data-prov-del="${esc(p.id)}">删</button>
        <button data-prov-edit="${esc(p.id)}">改</button></li>`
    ).join('');
    pop.innerHTML = `
      <h4>平台与模型<small>仅自己可见</small></h4>
      <div class="study-pop-field"><label>平台</label><select id="provSelect">${opts}</select></div>
      <div class="study-pop-field"><label>模型</label>
        <input id="provModel" placeholder="留空用默认" value="${esc(modelName)}">
        <button class="study-hbtn" id="provFetch" title="拉取模型列表">拉取</button>
      </div>
      <div class="study-pop-row">
        <button id="provApply" class="primary">应用</button>
      </div>
      <h4 style="margin-top:12px">添加 / 修改平台</h4>
      <div class="study-pop-field"><label>ID</label><input id="npId" placeholder="myplatform"></div>
      <div class="study-pop-field"><label>名称</label><input id="npLabel" placeholder="显示名，可留空"></div>
      <div class="study-pop-field"><label>Base URL</label><input id="npBase" placeholder="https://api.example.com/v1"></div>
      <div class="study-pop-field"><label>API Key</label><input id="npKey" type="password" placeholder="留空=不修改已存的"></div>
      <div class="study-pop-field"><label>默认模型</label><input id="npModel" placeholder="可留空"></div>
      <div class="study-pop-row">
        <button id="npSave" class="primary">保存</button>
        <button id="npClear">清空</button>
      </div>
      <ul class="study-pop-list">${list}</ul>
      <p class="study-pop-note">Base URL 需兼容 OpenAI <code>/chat/completions</code>。Key 明文存在服务器 <code>study_providers.json</code>（权限 600）。</p>
      <div id="provMsg" class="study-pop-note" style="color:#58a6ff"></div>`;
    $('provSelect')?.addEventListener('change', e => {
      providerId = e.target.value;
    });
    $('provApply')?.addEventListener('click', () => {
      modelName = ($('provModel')?.value || '').trim();
      renderConfig();
      updateCurrentLabel();
      window.showToast?.(`已切换：${currentLabel()}`);
    });
    $('provFetch')?.addEventListener('click', async () => {
      const msg = $('provMsg');
      try {
        if (msg) msg.textContent = '正在拉取模型列表…';
        const payload = await window.consoleRequest('/api/study/providers', {
          method: 'POST', body: { action: 'models', id: providerId },
        });
        const models = payload.models || [];
        if (msg) msg.textContent = `共 ${models.length} 个模型：` + models.slice(0, 30).join(', ') + (models.length > 30 ? ' …' : '');
      } catch (e) {
        if (msg) msg.textContent = '拉取失败：' + e.message;
      }
    });
    $('npSave')?.addEventListener('click', async () => {
      const msg = $('provMsg');
      const body = {
        action: 'save',
        id: ($('npId')?.value || '').trim(),
        label: ($('npLabel')?.value || '').trim(),
        baseUrl: ($('npBase')?.value || '').trim(),
        apiKey: ($('npKey')?.value || '').trim(),
        model: ($('npModel')?.value || '').trim(),
      };
      try {
        await window.consoleRequest('/api/study/providers', { method: 'POST', body });
        await loadProviders();
        renderConfig();
        if (msg) msg.textContent = '已保存';
      } catch (e) {
        if (msg) msg.textContent = '保存失败：' + e.message;
      }
    });
    $('npClear')?.addEventListener('click', () => {
      ['npId','npLabel','npBase','npKey','npModel'].forEach(id => { const el = $(id); if (el) el.value = ''; });
    });
    pop.querySelectorAll('[data-prov-del]').forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm('删除该平台配置？')) return;
      try {
        await window.consoleRequest('/api/study/providers', {
          method: 'POST', body: { action: 'delete', id: btn.dataset.provDel },
        });
        if (providerId === btn.dataset.provDel) providerId = 'default';
        await loadProviders();
        renderConfig();
        updateCurrentLabel();
      } catch (e) {
        window.showToast?.('删除失败：' + e.message);
      }
    }));
    pop.querySelectorAll('[data-prov-edit]').forEach(btn => btn.addEventListener('click', () => {
      const p = providers.find(x => x.id === btn.dataset.provEdit);
      if (!p) return;
      if ($('npId')) $('npId').value = p.id;
      if ($('npLabel')) $('npLabel').value = p.label;
      if ($('npBase')) $('npBase').value = p.base_url || '';
      if ($('npModel')) $('npModel').value = p.model || '';
      if ($('npKey')) $('npKey').value = '';
    }));
  }

  function updateCurrentLabel() {
    const el = $('studyCurrentLabel');
    if (el) el.textContent = currentLabel();
  }

  function welcomeMarkup() {
    return `<div class="study-welcome">
      <h1>今天想学什么？</h1>
      <div class="study-starters">
        <button type="button">讲解一个新知识点</button>
        <button type="button">分析我的错题</button>
        <button type="button">帮我制定复习计划</button>
      </div>
    </div>`;
  }

  function bindStarters() {
    messagesEl()?.querySelectorAll('.study-starters button').forEach(btn => {
      btn.addEventListener('click', () => { send(btn.textContent); });
    });
  }

  // ── 面板开关 ──
  function openStudyPanel() {
    const p = $('studyPanel');
    if (!p) return;
    document.querySelector('.ai-chat-panel')?.setAttribute('hidden', '');
    document.querySelector('.security-panel')?.setAttribute('hidden', '');
    p.removeAttribute('hidden');
    document.querySelectorAll('.activity-item[data-activity]').forEach(item => {
      item.classList.remove('active');
      item.setAttribute('aria-selected', 'false');
    });
    $('aiNewChatBtn')?.classList.remove('active');
    studyBtn()?.classList.add('active');
    open = true;
    ensureLibs()
      .then(() => Promise.all([loadOutlines(), loadProviders()]))
      .then(() => { renderConfig(); updateCurrentLabel(); return loadHistory(); })
      .catch(err => appendError('渲染库加载失败：' + err.message + '（数学公式将显示为纯文本）'));
  }

  function closeStudyPanel() {
    const p = $('studyPanel');
    if (!p) return;
    p.setAttribute('hidden', '');
    studyBtn()?.classList.remove('active');
    open = false;
    document.querySelector('.ai-chat-panel')?.removeAttribute('hidden');
    $('aiNewChatBtn')?.classList.add('active');
  }

  // ── 发送（流式） ──
  function setBusy(value) {
    busy = value;
    const btn = sendBtn();
    if (btn) { btn.classList.toggle('is-busy', value); btn.disabled = value; }
  }

  async function send(text) {
    if (busy) return;
    const input = promptEl();
    const message = (text ?? input?.value ?? '').trim();
    if (!message) return;
    if (input) { input.value = ''; input.style.height = 'auto'; }

    // 清空欢迎页
    const box = messagesEl();
    if (box?.querySelector('.study-welcome')) box.innerHTML = '';

    appendUser(message);
    setBusy(true);

    // 流式占位
    const art = document.createElement('article');
    art.className = 'study-turn assistant';
    art.innerHTML = '<p class="study-role">教练</p><div class="study-working"><i></i><b>正在准备课程…</b></div>';
    messagesEl()?.appendChild(art);
    scrollToEnd();

    let buffer = '';
    let lastPaint = 0;

    try {
      const response = await fetch('/api/study/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: message,
          sessionId,
          provider: providerId,
          model: modelName,
        }),
      });
      if (!response.ok) {
        let detail = '请求失败';
        try { detail = (await response.json()).error || detail; } catch {}
        throw new Error(detail);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        const lines = pending.split('\n');
        pending = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          let event;
          try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (event.type === 'delta') {
            buffer += event.text;
            const now = Date.now();
            if (now - lastPaint > 80) { lastPaint = now; paint(art, buffer); }
          } else if (event.type === 'done') {
            paint(art, event.markdown ?? buffer, event.title);
            if (event.progressUpdated) {
              const note = document.createElement('p');
              note.className = 'study-progress-note';
              note.textContent = '学习进度已更新';
              art.appendChild(note);
              const dot = $('studyProgressDot');
              if (dot) dot.hidden = false;
            }
          } else if (event.type === 'error') {
            throw new Error(event.message);
          }
        }
      }
      if (buffer && !art.querySelector('.study-lesson')) paint(art, buffer);
    } catch (e) {
      art.remove();
      appendError(e.message || '发送失败');
    } finally {
      setBusy(false);
    }
  }

  function paint(art, markdown, title) {
    const heading = title ? `<p class="study-role">${esc(title)}</p>` : '<p class="study-role">教练</p>';
    art.innerHTML = `${heading}<div class="study-lesson">${renderMarkdown(markdown)}</div>`;
    scrollToEnd();
  }

  // ── 初始化 ──
  function init() {
    studyBtn()?.addEventListener('click', openStudyPanel);
    sendBtn()?.addEventListener('click', () => send());
    // 会话 / 配置浮层
    $('studySessionBtn')?.addEventListener('click', () => { renderSessions(); togglePop(sessionPop()); });
    $('studyConfigBtn')?.addEventListener('click', () => { renderConfig(); togglePop(configPop()); });
    $('studyNewSessionBtn')?.addEventListener('click', () => { void newSession(); });
    sessionPop()?.addEventListener('click', e => {
      const del = e.target.closest('[data-sess-del]');
      if (del) { e.stopPropagation(); void deleteSession(del.dataset.sessDel); return; }
      const row = e.target.closest('[data-sess]');
      if (row) void switchSession(row.dataset.sess);
    });
    // 点击浮层外面关掉
    document.addEventListener('click', e => {
      if (!open) return;
      const sp = sessionPop(), cp = configPop();
      if (sp && !sp.hidden && !sp.contains(e.target) && e.target.id !== 'studySessionBtn') sp.hidden = true;
      if (cp && !cp.hidden && !cp.contains(e.target) && e.target.id !== 'studyConfigBtn') cp.hidden = true;
    });
    promptEl()?.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    promptEl()?.addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 130) + 'px';
    });
    document.querySelectorAll('.activity-item[data-activity], #aiNewChatBtn').forEach(btn => {
      btn.addEventListener('click', () => { if (open) closeStudyPanel(); }, true);
    });
    // 预加载渲染库，打开面板时更快
    void ensureLibs().catch(() => {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
