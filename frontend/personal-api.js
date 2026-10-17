(() => {
  'use strict';
  const endpoint = '/api/account/api-config';
  let generation = 0;

  function renderSettings(config) {
    openModal('我的 API', `<div class="account-security">
      <section><h3>${config.needsReconfiguration ? '个人 API 配置需要恢复' : config.configured ? '正在使用自己的 API' : '当前使用 Hatchery 默认服务'}</h3>
      <p>为当前账号配置 OpenAI 兼容的 Chat Completions 接口。保存后，新建站请求及 HatcheryStudy 的默认平台使用此配置；正在运行的任务保持原配置。</p>
      <form class="account-settings-form" id="personalApiForm" autocomplete="off">
        <label>Base URL<input name="baseUrl" type="url" required maxlength="2048" placeholder="https://api.example.com/v1" value="${esc(config.baseUrl)}" aria-describedby="personalApiHelp"></label>
        <label>API Key${config.hasKey ? '（已保存，留空保留）' : ''}<input name="apiKey" type="password" autocomplete="new-password" spellcheck="false" maxlength="2048" ${config.hasKey ? '' : 'required'} placeholder="${config.hasKey ? '更换地址时需重新填写 Key' : '粘贴你的 API Key'}"></label>
        <label>模型 ID<input name="model" required maxlength="160" spellcheck="false" placeholder="填写服务商提供的准确模型 ID" value="${esc(config.model)}"></label>
        <p id="personalApiHelp">请填写公网 HTTPS 基础地址（通常以 /v1 结尾）。建站模型需支持流式输出和工具调用。当前适配器支持文本，暂不支持图片附件。Key 加密保存在服务器，界面不回显。</p>
        <p id="personalApiMessage" role="status" aria-live="polite"></p>
        <button type="submit">保存配置</button>
        <button type="button" data-personal-api="test" ${config.configured ? '' : 'disabled'}>测试已保存的连接</button>
        <button type="button" data-personal-api="clear" ${config.configured || config.needsReconfiguration ? '' : 'disabled'}>清除并恢复默认服务</button>
      </form></section></div>`);
    const form = document.getElementById('personalApiForm');
    const message = document.getElementById('personalApiMessage');
    if (config.needsReconfiguration) {
      message.setAttribute('role', 'alert');
      message.textContent = config.recoveryMessage;
    }
    async function run(action) {
      const buttons = [...form.querySelectorAll('button')];
      const disabled = buttons.map(button => button.disabled);
      buttons.forEach(button => { button.disabled = true; });
      message.textContent = action === 'test' ? '正在读取模型列表，请稍候…' : '正在保存…';
      message.setAttribute('role', 'status');
      try {
        const body = {action};
        if (action === 'save') {
          const data = new FormData(form);
          for (const name of ['baseUrl', 'apiKey', 'model']) body[name] = String(data.get(name) || '').trim();
        }
        const payload = await consoleRequest(endpoint, {method: 'POST', body});
        if (!form.isConnected) return;
        if (action === 'test') {
          message.textContent = payload.message;
        } else {
          renderSettings(payload);
          document.getElementById('personalApiMessage').textContent = action === 'clear' ? '已恢复默认服务。' : '已保存，将用于接下来的请求。';
          void loadAiStatus();
        }
      } catch (error) {
        if (form.isConnected) {
          message.setAttribute('role', 'alert');
          message.textContent = error.message;
        }
      } finally {
        buttons.forEach((button, index) => { button.disabled = disabled[index]; });
      }
    }
    form.addEventListener('submit', event => { event.preventDefault(); void run('save'); });
    form.addEventListener('click', event => {
      const action = event.target.closest('[data-personal-api]')?.dataset.personalApi;
      if (action) void run(action);
    });
    form.elements.baseUrl.focus();
  }

  document.getElementById('personalApiSettingsBtn').addEventListener('click', async () => {
    const requestId = ++generation;
    const userId = currentConsoleUser?.id;
    openModal('我的 API', '<p id="personalApiLoading" role="status">正在读取配置…</p>');
    try {
      const config = await consoleRequest(endpoint);
      if (requestId === generation && userId === currentConsoleUser?.id && document.getElementById('personalApiLoading')) renderSettings(config);
    } catch (error) {
      const loading = document.getElementById('personalApiLoading');
      if (loading) { loading.setAttribute('role', 'alert'); loading.textContent = error.message; }
    }
  });
})();
