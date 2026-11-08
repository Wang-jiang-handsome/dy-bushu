/* ==========================================================================
   views-auth.js —— 登录 / 注册 / 找回密码（未登录区）
   模板片段：/assets/views/login.html、register.html、forgot.html
   接口见 docs/API_CONTRACT.md §2：
     POST /auth/user/login、/auth/admin/login、/auth/refresh、/auth/logout、
     /auth/send-verify-code、/auth/verify-code、/auth/user/register、
     /auth/forgot-password、/auth/reset-password、GET /auth/public-configs
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  /* ------------------------------- 登录 ------------------------------- */
  S.defineView('view-login', '/assets/views/login.html', {
    data: function () {
      return {
        form: { identifier: '', password: '', remember: true },
        adminMode: false,
        submitting: false,
        error: '',
        showPassword: false
      };
    },
    computed: {
      redirect: function () { return this.route.query.redirect || '/dashboard'; }
    },
    methods: {
      submit: function () {
        var self = this;
        if (this.submitting) return;
        this.error = '';
        if (!this.form.identifier.trim()) { this.error = '请输入用户名 / 邮箱 / 手机号'; return; }
        if (!this.form.password) { this.error = '请输入密码'; return; }
        this.submitting = true;
        this.api.auth.login({
          identifier: this.form.identifier.trim(),
          password: this.form.password,
          remember: !!this.form.remember
        }, this.adminMode).then(function () {
          self.succeed('登录成功，欢迎回来');
          var target = self.redirect;
          if (self.adminMode && target === '/dashboard') target = '/admin';
          self.nav.go(target);
        }).catch(function (err) {
          self.error = err.message || '登录失败，请稍后重试';
        }).then(function () { self.submitting = false; });
      },
      useDemo: function () {
        this.form.identifier = 'admin';
        this.form.password = '';
        this.error = '';
        this.ui.info('请输入管理员密码（安装时生成或 SK_ADMIN_PASSWORD 指定）');
      }
    }
  });

  /* ------------------------------- 注册 ------------------------------- */
  S.defineView('view-register', '/assets/views/register.html', {
    data: function () {
      return {
        form: {
          username: '', password: '', confirm_password: '', nickname: '',
          email: '', phone: '', verify_code: '', invite_code: ''
        },
        sending: false,
        countdown: 0,
        timer: null,
        devCode: '',
        submitting: false,
        error: ''
      };
    },
    computed: {
      // 站点配置要求邮箱验证时才强制填验证码
      needVerify: function () {
        return this.on(this.store.site['auth.require_email_verify'], false);
      },
      needInvite: function () {
        return this.on(this.store.site['auth.require_invite'], false);
      },
      codeChannel: function () { return this.form.phone && !this.form.email ? 'sms' : 'email'; },
      codeTarget: function () { return this.codeChannel === 'sms' ? this.form.phone : this.form.email; }
    },
    unmounted: function () { if (this.timer) clearInterval(this.timer); },
    methods: {
      sendCode: function () {
        var self = this;
        if (this.sending || this.countdown > 0) return;
        if (!this.codeTarget) { this.error = '请先填写邮箱或手机号'; return; }
        this.error = '';
        this.sending = true;
        this.api.auth.sendCode({
          scene: 'register', channel: this.codeChannel, target: this.codeTarget.trim()
        }).then(function (data) {
          self.devCode = (data && data.dev_code) || '';
          if (data && data.sent) {
            self.succeed('验证码已发送到 ' + (data.target || self.codeTarget));
          } else if (self.devCode) {
            self.ui.warn('未配置邮件/短信通道，本地开发验证码：' + self.devCode);
          } else {
            self.ui.warn('验证码已生成，请查看服务端日志');
          }
          self.startCountdown(60);
        }).catch(function (err) {
          self.error = err.message || '验证码发送失败';
        }).then(function () { self.sending = false; });
      },
      startCountdown: function (seconds) {
        var self = this;
        this.countdown = seconds;
        if (this.timer) clearInterval(this.timer);
        this.timer = setInterval(function () {
          self.countdown -= 1;
          if (self.countdown <= 0) { clearInterval(self.timer); self.timer = null; self.countdown = 0; }
        }, 1000);
      },
      submit: function () {
        var self = this;
        if (this.submitting) return;
        this.error = '';
        var f = this.form;
        if (!/^[A-Za-z0-9_]{3,20}$/.test(f.username)) { this.error = '用户名需为 3-20 位字母、数字或下划线'; return; }
        if (!f.password || f.password.length < 8) { this.error = '密码至少 8 位'; return; }
        if (f.password !== f.confirm_password) { this.error = '两次输入的密码不一致'; return; }
        if (this.needVerify && !f.verify_code) { this.error = '请输入邮箱验证码'; return; }
        if (!f.email && !f.phone && this.needVerify) { this.error = '本站要求邮箱验证，请填写邮箱'; return; }
        this.submitting = true;
        this.api.auth.register({
          username: f.username.trim(), password: f.password, confirm_password: f.confirm_password,
          nickname: f.nickname.trim(), email: f.email.trim(), phone: f.phone.trim(),
          verify_code: f.verify_code.trim(), invite_code: f.invite_code.trim()
        }).then(function () {
          self.succeed('注册成功，已自动登录');
          self.nav.go('/dashboard');
        }).catch(function (err) {
          self.error = err.message || '注册失败';
        }).then(function () { self.submitting = false; });
      }
    }
  });

  /* ----------------------------- 找回密码 ----------------------------- */
  S.defineView('view-forgot', '/assets/views/forgot.html', {
    data: function () {
      return {
        step: 1,
        identifier: '',
        code: '',
        password: '',
        confirm: '',
        resetToken: '',
        devCode: '',
        maskedTarget: '',
        sending: false,
        countdown: 0,
        timer: null,
        submitting: false,
        error: '',
        done: false
      };
    },
    unmounted: function () { if (this.timer) clearInterval(this.timer); },
    methods: {
      requestCode: function () {
        var self = this;
        if (this.sending || this.countdown > 0) return;
        if (!this.identifier.trim()) { this.error = '请输入用户名 / 邮箱 / 手机号'; return; }
        this.error = '';
        this.sending = true;
        this.api.auth.forgot({ identifier: this.identifier.trim() }).then(function (data) {
          self.resetToken = (data && data.reset_token) || '';
          self.devCode = (data && data.dev_code) || '';
          self.maskedTarget = (data && data.target) || '';
          self.step = 2;
          if (data && data.reset_token) {
            self.ui.info('验证码已发送' + (self.maskedTarget ? '至 ' + self.maskedTarget : ''));
          } else {
            self.ui.warn((data && data.message) || '若账号存在，验证码已发送');
          }
          self.startCountdown(60);
        }).catch(function (err) {
          self.error = err.message || '请求失败';
        }).then(function () { self.sending = false; });
      },
      startCountdown: function (seconds) {
        var self = this;
        this.countdown = seconds;
        if (this.timer) clearInterval(this.timer);
        this.timer = setInterval(function () {
          self.countdown -= 1;
          if (self.countdown <= 0) { clearInterval(self.timer); self.timer = null; self.countdown = 0; }
        }, 1000);
      },
      submit: function () {
        var self = this;
        if (this.submitting) return;
        this.error = '';
        if (!this.resetToken) { this.error = '请先获取验证码'; return; }
        if (!this.code.trim()) { this.error = '请输入验证码'; return; }
        if (!this.password || this.password.length < 8) { this.error = '新密码至少 8 位'; return; }
        if (this.password !== this.confirm) { this.error = '两次输入的密码不一致'; return; }
        this.submitting = true;
        this.api.auth.reset({
          reset_token: this.resetToken, verify_code: this.code.trim(),
          password: this.password, confirm_password: this.confirm
        }).then(function (data) {
          self.done = true;
          self.succeed('密码已重置' + (data && data.username ? '（' + data.username + '）' : '') + '，请用新密码登录');
          setTimeout(function () { self.nav.go('/login'); }, 1200);
        }).catch(function (err) {
          self.error = err.message || '重置失败';
        }).then(function () { self.submitting = false; });
      }
    }
  });
})();
