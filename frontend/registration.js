/* SDSZ registration stays in Hatchery; the shared service owns accounts/OTPs. */
(() => {
  'use strict';
  const root = document.querySelector('#registerForm');
  const campusForm = document.querySelector('#registrationCampus');
  const info = document.querySelector('#registrationInfo');
  if (!root || !campusForm || !info) return;
  const email = info.elements.email;
  const send = document.querySelector('#registrationSendOtp');
  const finish = info.querySelector('[type="submit"]');
  const back = document.querySelector('#registrationBack');
  const hint = document.querySelector('#registrationOtpHint');
  const success = document.querySelector('#registrationSuccess');
  const state = { campus: '', revision: 0, sending: false, submitting: false, checking: false,
    sentEmail: '', deadline: 0, timer: null };
  for (let i = 1; i <= 24; i++) info.elements.classGroup.add(new Option(`${i}班`, `${i}班`));

  function error(id, text = '') {
    const node = document.querySelector('#' + id);
    node.textContent = text;
    node.hidden = !text;
  }
  async function request(operation, body) {
    // One attempt: a lost response can still have sent an email/created an account.
    return window.AIchemyHatcheryAuth.request('/api/auth/' + operation, { method: 'POST', body });
  }
  function refresh() {
    const remaining = Math.max(0, Math.ceil((state.deadline - Date.now()) / 1000));
    send.disabled = state.sending || state.submitting || remaining > 0 || !email.value.trim() || !state.campus;
    send.textContent = state.sending ? '发送中…' : remaining ? `${remaining}s` : state.sentEmail && state.sentEmail === email.value.trim() ? '重发' : '发送验证码';
    finish.disabled = state.submitting || state.sending || !state.sentEmail || state.sentEmail !== email.value.trim();
    back.disabled = state.submitting;
    hint.hidden = !state.sentEmail || state.sentEmail !== email.value.trim();
    if (!remaining && state.timer) { clearInterval(state.timer); state.timer = null; }
  }
  function cooldown(seconds) {
    state.deadline = Date.now() + seconds * 1000;
    clearInterval(state.timer);
    state.timer = setInterval(refresh, 250);
    refresh();
  }
  function step(name) {
    campusForm.hidden = name !== 'campus';
    info.hidden = name !== 'info';
    root.querySelectorAll('[data-registration-step]').forEach(node => {
      if (node.dataset.registrationStep === name) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
      node.classList.toggle('done', name === 'info' && node.dataset.registrationStep === 'campus');
    });
    document.querySelector('#registrationVerifiedCampus').textContent = state.campus;
    (name === 'campus' ? campusForm.elements.campusId : info.elements.name).focus();
  }
  function clearCode() {
    state.revision++;
    state.sentEmail = '';
    info.elements.code.value = '';
    error('registrationOtpError');
    refresh();
  }
  function passwords() {
    const password = info.elements.password.value;
    const mismatch = Boolean(info.elements.confirmPassword.value && info.elements.confirmPassword.value !== password);
    document.querySelector('#registrationMismatch').hidden = !mismatch;
    info.elements.confirmPassword.setCustomValidity(mismatch ? '两次密码不一致' : '');
    const strength = document.querySelector('#registrationStrength');
    const level = !password ? 'empty' : password.length < 8 ? 'weak' : password.length < 12 ? 'medium' : 'strong';
    strength.className = 'password-strength ' + level;
    strength.querySelector('span').textContent = { empty: '至少 8 位', weak: '弱', medium: '中', strong: '强' }[level];
  }

  campusForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (state.checking || !campusForm.reportValidity()) return;
    const campus = campusForm.elements.campusId.value.trim();
    if (!campus) return;
    const revision = ++state.revision;
    state.checking = true;
    const button = campusForm.querySelector('[type="submit"]');
    button.disabled = true; button.querySelector('b').textContent = '验证中…';
    campusForm.elements.campusId.readOnly = true;
    error('registrationCampusError'); success.hidden = true;
    try {
      await request('campus', { campusId: campus });
      if (revision !== state.revision) return;
      state.campus = campus;
      clearCode();
      step('info');
    } catch (failure) {
      if (revision === state.revision) error('registrationCampusError', failure.message);
    } finally {
      state.checking = false;
      campusForm.elements.campusId.readOnly = false;
      button.disabled = false; button.querySelector('b').textContent = '验证校园号';
      refresh();
    }
  });
  send.addEventListener('click', async () => {
    if (send.disabled || !email.reportValidity()) return;
    const address = email.value.trim(), campus = state.campus, revision = state.revision;
    state.sending = true; error('registrationOtpError'); refresh();
    try {
      await request('send-otp', { campusId: campus, email: address });
      // Cooldown still applies if the user changed the email during this request.
      cooldown(60);
      if (revision === state.revision && campus === state.campus && address === email.value.trim()) {
        state.sentEmail = address;
        info.elements.code.focus();
      }
    } catch (failure) {
      if (failure.status === 429) cooldown(Math.min(3600, Math.max(1, Number(failure.payload?.retryAfter) || 60)));
      if (revision === state.revision) error('registrationOtpError', failure.message);
    } finally { state.sending = false; refresh(); }
  });
  email.addEventListener('input', clearCode);
  info.elements.code.addEventListener('input', () => {
    info.elements.code.value = info.elements.code.value.replace(/[^0-9]/g, '').slice(0, 6);
  });
  info.elements.password.addEventListener('input', passwords);
  info.elements.confirmPassword.addEventListener('input', passwords);
  document.querySelector('#registrationTogglePassword').addEventListener('click', event => {
    const visible = info.elements.password.type === 'password';
    info.elements.password.type = info.elements.confirmPassword.type = visible ? 'text' : 'password';
    event.currentTarget.textContent = visible ? '隐藏' : '显示';
    event.currentTarget.setAttribute('aria-pressed', String(visible));
    event.currentTarget.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  });
  back.addEventListener('click', () => {
    if (state.submitting) return;
    state.campus = '';
    clearCode();
    error('registrationError');
    step('campus');
  });
  info.addEventListener('submit', async event => {
    event.preventDefault();
    passwords();
    if (finish.disabled || state.submitting || !state.campus || !info.reportValidity()) return;
    const data = { campusId: state.campus };
    for (const name of ['name', 'realName', 'email', 'grade', 'classGroup', 'password', 'code']) {
      data[name] = name === 'password' ? info.elements[name].value : info.elements[name].value.trim();
    }
    state.submitting = true; error('registrationError'); refresh();
    finish.querySelector('b').textContent = '注册中…';
    const fields = [...info.querySelectorAll('input,select')];
    fields.forEach(node => { node.disabled = true; });
    try {
      await request('register', data);
      info.reset();
      info.elements.password.type = info.elements.confirmPassword.type = 'password';
      const toggle = document.querySelector('#registrationTogglePassword');
      toggle.textContent = '显示'; toggle.setAttribute('aria-pressed', 'false'); toggle.setAttribute('aria-label', '显示密码');
      state.campus = '';
      clearCode(); passwords(); step('campus');
      document.querySelector('.auth-tabs [data-auth-tab="login"]').click();
      document.querySelector('#loginUsername').value = data.campusId;
      document.querySelector('#loginPassword').focus();
      success.textContent = '注册成功！请使用刚设置的密码登录。该账号也可用于 Groovin。';
      success.hidden = false;
    } catch (failure) {
      error('registrationError', failure.name === 'AbortError' || failure instanceof TypeError
        ? '注册结果暂未确认，请先尝试登录；不要连续重复提交。' : failure.message);
    } finally {
      delete data.password;
      state.submitting = false;
      fields.forEach(node => { node.disabled = false; });
      finish.querySelector('b').textContent = '完成注册';
      refresh();
    }
  });
  refresh();
})();
