(() => {
  'use strict';

  const authState = { user: null, ready: false, mode: 'login' };

  function safeReturnTarget() {
    const value = new URL(location.href).searchParams.get('returnTo');
    if (!value) return '';
    try {
      const target = new URL(value, location.origin);
      if (target.origin !== location.origin) return '';
      return `${target.pathname}${target.search}${target.hash}`;
    } catch {
      return '';
    }
  }

  async function request(path, options = {}) {
    const requestOptions = {
      method: options.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    };
    if (options.body !== undefined) {
      requestOptions.headers['Content-Type'] = 'application/json';
      requestOptions.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, requestOptions);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      // 非 JSON 响应（多半是代理/网关的 HTML 错误页）：带上状态码，方便定位是哪一层出的问题
      payload = { error: `服务返回了无法解析的内容（HTTP ${response.status}，可能是服务重启中或网关故障）` };
    }
    if (!response.ok) {
      const error = new Error(payload.error || `请求失败（${response.status}）`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function authError(message = '') {
    const node = document.querySelector('#authError');
    if (!node) return;
    node.textContent = message;
    node.hidden = !message;
  }

  function setMode(mode) {
    authState.mode = mode === 'register' ? 'register' : 'login';
    document.querySelectorAll('[data-auth-tab]').forEach(button => {
      const active = button.dataset.authTab === authState.mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    const login = document.querySelector('#loginForm');
    const register = document.querySelector('#registerForm');
    if (login) login.hidden = authState.mode !== 'login';
    if (register) register.hidden = authState.mode !== 'register';
    const title = document.querySelector('#authTitle');
    const description = document.querySelector('#authDescription');
    if (title) title.textContent = authState.mode === 'login' ? '欢迎回来' : '创建你的账号';
    if (description) description.textContent = authState.mode === 'login'
      ? '使用账号继续编辑、同步并发布网站。'
      : '用户名、密码和数字校园号缺一不可；校园号注册成功后即被占用。';
    authError();
    document.querySelector(`#${authState.mode}Form input`)?.focus();
  }

  function setBusy(form, busy, label) {
    const button = form.querySelector('.auth-submit');
    if (!button) return;
    if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.querySelector('b')?.textContent || '';
    button.disabled = busy;
    button.classList.toggle('loading', busy);
    const text = button.querySelector('b');
    if (text) text.textContent = busy ? label : button.dataset.defaultLabel;
  }

  function fieldError(input, message = '') {
    const field = input?.closest('.auth-field');
    if (!field) return;
    field.classList.toggle('invalid', Boolean(message));
    const node = field.querySelector('.field-error');
    if (node) {
      node.textContent = message;
      node.hidden = !message;
    }
  }

  function validateRegistration(form) {
    const username = form.elements.username;
    const password = form.elements.password;
    const confirm = form.elements.confirmPassword;
    const campusId = form.elements.campusId;
    const email = form.elements.email;
    const code = form.elements.code;
    [username, password, confirm, campusId, email, code].forEach(input => fieldError(input));
    let valid = true;
    if (!/^[A-Za-z0-9_-]{3,32}$/.test(username.value.trim())) {
      fieldError(username, '请输入 3–32 位字母、数字、下划线或短横线');
      valid = false;
    }
    if (password.value.length < 8 || password.value.length > 128) {
      fieldError(password, '密码长度需为 8–128 位');
      valid = false;
    }
    if (confirm.value !== password.value) {
      fieldError(confirm, '两次输入的密码不一致');
      valid = false;
    }
    if (!campusId.value.trim()) {
      fieldError(campusId, '请输入数字校园号');
      valid = false;
    }
    if (!email.value.trim() || !email.value.includes('@')) {
      fieldError(email, '请输入有效邮箱');
      valid = false;
    }
    if (!/^\d{6}$/.test(code.value.trim())) {
      fieldError(code, '请输入 6 位数字验证码');
      valid = false;
    }
    return valid;
  }

  function validateLogin(form) {
    const username = form.elements.username;
    const password = form.elements.password;
    [username, password].forEach(input => fieldError(input));
    let valid = true;
    if (!username.value.trim()) {
      fieldError(username, '请输入用户名、邮箱或校园号');
      valid = false;
    }
    if (password.value.length < 8 || password.value.length > 128) {
      fieldError(password, '请输入 8–128 位密码');
      valid = false;
    }
    return valid;
  }

  function updatePasswordStrength(input) {
    const meter = input.closest('.auth-field')?.querySelector('.password-strength');
    if (!meter) return;
    const value = input.value;
    let score = 0;
    if (value.length >= 8) score += 1;
    if (value.length >= 12) score += 1;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1;
    if (/\d/.test(value)) score += 1;
    if (/[^A-Za-z0-9]/.test(value)) score += 1;
    const level = score >= 4 ? 'strong' : score >= 2 ? 'medium' : value ? 'weak' : 'empty';
    meter.className = `password-strength ${level}`;
    meter.querySelector('span').textContent = { strong: '强', medium: '一般', weak: '弱', empty: '至少 8 位' }[level];
  }

  function updateAccountUI(user) {
    if (!user) return;
    const username = String(user.username);
    const initial = username.slice(0, 1).toUpperCase();
    const values = {
      '#consoleAvatar': initial,
      '#menuAvatar': initial,
      '#menuUsername': username,
      '#accountUsername': username,
      '#accountPreviewPath': user.previewId ? `/preview/${user.previewId}` : '首次预览后生成',
      '#accountPublishPath': user.publishSlug ? `${user.publishSlug}.hatchery.mizusumi.com` : '未发布',
    };
    Object.entries(values).forEach(([selector, value]) => {
      const node = document.querySelector(selector);
      if (node) node.textContent = value;
    });
    const manager = document.querySelector('#inviteManagerBtn');
    if (manager) manager.hidden = user.role !== 'admin';
  }

  function removeSensitiveQuery() {
    const url = new URL(location.href);
    const sensitiveKeys = ['password', 'confirmPassword', 'code'];
    const hadSensitive = sensitiveKeys.some(key => url.searchParams.has(key));
    if (!hadSensitive && !url.searchParams.has('username')) return;
    sensitiveKeys.forEach(key => url.searchParams.delete(key));
    url.searchParams.delete('username');
    history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    if (hadSensitive) authError('已从地址栏移除不安全的账号参数，请在下方重新登录。');
  }

  function showGate(message = '') {
    authState.user = null;
    const gate = document.querySelector('#authGate');
    if (gate) {
      gate.dataset.state = 'form';
      gate.hidden = false;
    }
    if (message) authError(message);
    window.dispatchEvent(new CustomEvent('alchemyhatchery:logged-out', { detail: { reason: message } }));
  }

  function authenticated(user) {
    authState.user = user;
    updateAccountUI(user);
    const gate = document.querySelector('#authGate');
    if (gate) gate.hidden = true;
    authError();
    const returnTarget = safeReturnTarget();
    if (returnTarget) {
      location.replace(returnTarget);
      return;
    }
    window.dispatchEvent(new CustomEvent('alchemyhatchery:authenticated', { detail: { user } }));
  }

  async function submit(form) {
    const mode = form.dataset.authForm;
    authError();
    form.querySelectorAll('.auth-field.invalid').forEach(field => field.classList.remove('invalid'));
    if (mode === 'register' ? !validateRegistration(form) : !validateLogin(form)) return;
    setBusy(form, true, mode === 'register' ? '正在创建账号…' : '正在验证账号…');
    const data = new FormData(form);
    try {
      const payload = await request(`/api/auth/${mode}`, {
        method: 'POST',
        body: {
          username: String(data.get('username') || '').trim(),
          password: String(data.get('password') || ''),
          identifier: String(data.get('username') || '').trim(),
          campusId: String(data.get('campusId') || '').trim(),
          email: String(data.get('email') || '').trim(),
          code: String(data.get('code') || '').trim(),
          realName: String(data.get('realName') || '').trim(),
          grade: String(data.get('grade') || '').trim(),
          classGroup: String(data.get('classGroup') || '').trim(),
          remember: Boolean(data.get('remember')),
        },
      });
      form.reset();
      authenticated(payload.user);
      window.dispatchEvent(new CustomEvent('alchemyhatchery:toast', { detail: { message: mode === 'register' ? '账号创建成功' : '登录成功' } }));
    } catch (error) {
      authError(error.message);
      form.querySelector('input[type="password"]')?.focus();
    } finally {
      setBusy(form, false, '');
    }
  }

  async function logout() {
    try {
      await request('/api/auth/logout', { method: 'POST', body: {} });
    } finally {
      showGate();
      setMode('login');
    }
  }

  async function bootstrap() {
    if (authState.ready) return;
    authState.ready = true;
    removeSensitiveQuery();
    try {
      const payload = await request('/api/auth/me');
      authenticated(payload.user);
    } catch {
      showGate();
    }
  }

  document.addEventListener('submit', event => {
    const form = event.target.closest?.('[data-auth-form]');
    if (!form) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    void submit(form);
  }, true);

  document.addEventListener('click', event => {
    const tab = event.target.closest?.('[data-auth-tab]');
    if (tab) {
      event.preventDefault();
      setMode(tab.dataset.authTab);
      return;
    }
    const otpBtn = event.target.closest?.('#sendOtpBtn');
    if (otpBtn) {
      event.preventDefault();
      const emailInput = document.querySelector('#registerEmail');
      const email = emailInput?.value?.trim();
      if (!email || !email.includes('@')) {
        fieldError(emailInput, '请先输入有效邮箱');
        return;
      }
      fieldError(emailInput);
      otpBtn.disabled = true;
      otpBtn.textContent = '发送中…';
      request('/api/auth/send-otp', { method: 'POST', body: { email } })
        .then(() => {
          otpBtn.textContent = '已发送';
          let countdown = 60;
          const timer = setInterval(() => {
            countdown -= 1;
            otpBtn.textContent = `${countdown}s`;
            if (countdown <= 0) { clearInterval(timer); otpBtn.textContent = '发送验证码'; otpBtn.disabled = false; }
          }, 1000);
        })
        .catch(err => {
          authError(err.message);
          otpBtn.textContent = '发送验证码';
          otpBtn.disabled = false;
        });
      return;
    }
    const toggle = event.target.closest?.('[data-password-toggle]');
    if (toggle) {
      const input = document.querySelector(`#${toggle.dataset.passwordToggle}`);
      if (!input) return;
      const visible = input.type === 'text';
      input.type = visible ? 'password' : 'text';
      toggle.textContent = visible ? '显示' : '隐藏';
      toggle.setAttribute('aria-pressed', String(!visible));
      input.focus();
      return;
    }
    const help = event.target.closest?.('[data-auth-help]');
    if (help) {
      authError('忘记密码请联系管理员核验身份后处理；不要通过聊天发送原密码。');
    }
  });

  document.addEventListener('input', event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    fieldError(input);
    if (input.dataset.passwordStrength !== undefined) updatePasswordStrength(input);
  });

  window.AIchemyHatcheryAuth = {
    request,
    showGate,
    logout,
    bootstrap,
    get user() { return authState.user; },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  else void bootstrap();
})();
