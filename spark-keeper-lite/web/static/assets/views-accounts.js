/* ==========================================================================
   views-accounts.js —— 抖音账号列表（含扫码登录）+ 账号详情
   模板片段：/assets/views/accounts.html、/assets/views/account-detail.html
   接口见 docs/API_CONTRACT.md §4：
     GET/POST /douyin/accounts、GET/PATCH/DELETE /douyin/accounts/<id>、
     POST /douyin/login/browser、/douyin/login/sms/send、/douyin/login/sms/submit、
     GET /douyin/login/status、POST /douyin/login/cancel、/douyin/login/refresh、
     POST /douyin/accounts/<id>/check
   扫码登录的关键时序（队友 A/D 明确）：
     1) POST /douyin/accounts 建占位记录 → 拿到 account_id；
     2) POST /douyin/login/browser {account_id} → 拿到 session_id（首包 qrcode_image 可能为空）；
     3) 每 1 秒轮询 /douyin/login/status，直到「拿到二维码图片」且进入终态
        （success/failed/canceled/expired）才停止。
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  var TERMINAL = ['success', 'failed', 'canceled', 'expired'];

  S.defineView('view-accounts', '/assets/views/accounts.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { status: '', keyword: '' },
        accounts: [],
        // 新增/编辑
        showForm: false, editing: null, submitting: false, formError: '',
        form: { nickname: '', remark: '', proxy_id: '', login_type: 'qrcode' },
        // 扫码登录
        showLogin: false, loginAccount: null, session: null, sessionId: '',
        // 「可见窗口」模式：后端开一个真实 Chrome 窗口，用户直接在窗口里登录
        visibleMode: false,
        //: 请求了「窗口里登录」但服务端没有图形界面 → 已降级为网页扫码
        visibleFallback: false,
        //: 宿主机能力（有没有图形界面 / 找到浏览器没有），决定按钮可用性
        caps: null,
        qrImage: '', qrData: '', loginStatus: 'pending', loginStep: '', loginMessage: '',
        loginExpireAt: '', pollTimer: null, pollCount: 0, starting: false,
        // 短信登录
        smsMode: false, smsPhone: '', smsCode: '', smsSending: false, smsError: '',
        // 手动导入 Cookie（扫码失败时的兜底）
        showCookie: false, cookieAccount: null, cookieText: '', cookieVerify: true,
        cookieSubmitting: false,
        checking: false
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.account_status); },
      terminal: function () { return TERMINAL.indexOf(this.loginStatus) >= 0; },
      qrWaiting: function () {
        return !this.qrImage && ['pending', 'scanned', 'confirmed'].indexOf(this.loginStatus) >= 0;
      }
    },
    created: function () { this.load(); this.loadCaps(); },
    unmounted: function () { this.stopPoll(); },
    methods: {
      // 宿主机能力：云主机/容器里没有图形界面，就不能「在窗口里登录」
      loadCaps: function () {
        var self = this;
        return this.api.get('/douyin/capabilities', null, { silent: true })
          .then(function (data) { self.caps = data || null; })
          .catch(function () { self.caps = null; });
      },
      visibleBlockedReason: function () {
        var caps = this.caps;
        if (!caps) return '';                       // 还没探测到，别误拦
        if (!caps.display) return caps.hint || '服务器没有图形界面，无法弹出浏览器窗口';
        return '';
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/douyin/accounts', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '账号列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () { this.filters = { status: '', keyword: '' }; this.search(); },

      /* ---------------- 新增 / 编辑 ---------------- */
      openCreate: function () {
        this.editing = null;
        this.form = { nickname: '', remark: '', proxy_id: '', login_type: 'qrcode' };
        this.formError = '';
        this.showForm = true;
      },
      openEdit: function (row) {
        this.editing = row;
        this.form = {
          nickname: row.nickname || '',
          remark: row.remark || '',
          proxy_id: row.proxy_id || '',
          login_type: row.login_type || 'qrcode'
        };
        this.formError = '';
        this.showForm = true;
      },
      saveForm: function () {
        var self = this;
        if (this.submitting) return;
        if (!this.editing && !this.form.nickname.trim()) {
          this.formError = '请给这个账号起一个备注名，方便区分（例如「小号 A」）';
          return;
        }
        this.submitting = true;
        this.formError = '';
        var job;
        if (this.editing) {
          // 契约 §4：PATCH 只支持改「备注 / 代理 / 启用状态」
          var patch = { remark: this.form.remark.trim(), nickname: this.form.nickname.trim() };
          patch.proxy_id = this.form.proxy_id ? parseInt(this.form.proxy_id, 10) : null;
          job = this.api.patch('/douyin/accounts/' + this.editing.id, patch);
        } else {
          var payload = {
            nickname: this.form.nickname.trim(),
            remark: this.form.remark.trim(),
            login_type: this.form.login_type
          };
          if (this.form.proxy_id) payload.proxy_id = parseInt(this.form.proxy_id, 10);
          job = this.api.post('/douyin/accounts', payload);
        }
        job.then(function (data) {
          self.showForm = false;
          self.succeed(self.editing ? '已保存' : '账号已创建，正在生成二维码…');
          self.load();
          if (!self.editing) {
            var accountId = (data && (data.account_id || (data.account && data.account.id))) || null;
            if (accountId) {
              self.startLogin({ id: accountId, nickname: self.form.nickname, status: 'offline' });
            } else {
              self.ui.warn('账号已创建，请在列表中点击「扫码登录」完成绑定');
            }
          }
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除账号',
          message: '确定删除「' + (row.remark || row.nickname || ('账号 #' + row.id)) +
            '」吗？\n该账号下的定时任务、好友与登录态文件都会一并删除，且不可恢复。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          return self.api.del('/douyin/accounts/' + row.id);
        }).then(function (res) {
          if (res === null) return;
          self.succeed('账号已删除');
          self.load();
        }).catch(function () { /* api 层已提示 */ });
      },
      check: function (row) {
        var self = this;
        this.checking = true;
        this.api.post('/douyin/accounts/' + row.id + '/check', {}).then(function (data) {
          var okFlag = !data || data.valid === undefined ? true : !!data.valid;
          var msg = (data && (data.message || data.status)) || (okFlag ? '登录态有效' : '登录态已失效');
          if (okFlag) self.succeed(msg); else self.ui.warn(msg);
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.checking = false; });
      },

      /* ---------------- 扫码 / 短信登录 ---------------- */
      startLogin: function (row, opts) {
        var self = this;
        opts = opts || {};
        this.loginAccount = row;
        this.showLogin = true;
        this.smsMode = (row.login_type === 'sms');
        this.smsPhone = '';
        this.smsCode = '';
        this.smsError = '';
        this.qrImage = '';
        this.qrData = '';
        this.loginStatus = 'pending';
        this.loginStep = '';
        this.loginMessage = '';
        this.sessionId = '';
        this.pollCount = 0;
        this.starting = true;
        this.stopPoll();
        if (this.smsMode) { this.starting = false; return; }
        var visible = !!opts.visible;
        this.visibleMode = visible;
        this.api.post('/douyin/login/browser', { account_id: row.id, method: 'qrcode', visible: visible }).then(function (data) {
          self.session = data || {};
          self.sessionId = (data && data.session_id) || '';
          self.qrImage = (data && data.qrcode_image) || '';
          self.qrData = (data && data.qrcode_data) || '';
          self.loginStatus = (data && data.status) || 'pending';
          self.loginExpireAt = (data && data.expire_at) || '';
          // 服务端可能把「可见窗口」降级成无头（服务器没有图形界面），
          // 以返回的 visible 为准，否则界面会一直提示「去窗口里看」而其实没有窗口。
          self.visibleMode = !!(data && data.visible);
          self.visibleFallback = !!visible && !self.visibleMode;
          self.loginMessage = self.visibleMode
            ? ((data && data.message) || '已在本机打开浏览器窗口，请在弹出的窗口里登录抖音…')
            : ((self.visibleFallback && data && data.message) || '正在等待扫码…');
          if (!self.sessionId) {
            self.loginStatus = 'failed';
            self.loginMessage = '后端未返回 session_id，无法轮询登录状态';
            return;
          }
          // 首包可能没有二维码：必须持续轮询，直到拿到图片或进入终态
          self.pollTick();
        }).catch(function (err) {
          self.loginStatus = 'failed';
          self.loginMessage = err.message || '启动扫码登录失败';
        }).then(function () { self.starting = false; });
      },
      pollTick: function () {
        var self = this;
        this.stopPoll();
        this.pollTimer = setInterval(function () { self.fetchStatus(); }, 1000);
      },
      fetchStatus: function () {
        var self = this;
        if (!this.sessionId) return;
        this.pollCount += 1;
        if (this.pollCount > 300) {          // 5 分钟兜底，避免无限轮询
          this.loginStatus = 'expired';
          this.loginMessage = '等待超时，请点击「刷新二维码」重试';
          this.stopPoll();
          return;
        }
        this.api.get('/douyin/login/status', { session_id: this.sessionId }, { silent: true })
          .then(function (data) {
            if (!data) return;
            self.loginStatus = data.status || self.loginStatus;
            self.loginStep = data.step || '';
            self.loginMessage = data.message || '';
            if (data.qrcode_image) self.qrImage = data.qrcode_image;
            if (data.qrcode_data) self.qrData = data.qrcode_data;
            if (data.expire_at) self.loginExpireAt = data.expire_at;
            if (TERMINAL.indexOf(self.loginStatus) >= 0) {
              self.stopPoll();
              self.onTerminal(data);
            }
          })
          .catch(function (err) {
            // 单次轮询失败不终止：网络抖动后继续（连续失败由 pollCount 兜底）
            self.loginMessage = err.message || '状态查询失败，正在重试…';
          });
      },
      onTerminal: function (data) {
        if (this.loginStatus === 'success') {
          this.succeed('登录成功，账号登录态已保存');
          this.load();
          var self = this;
          setTimeout(function () { self.closeLogin(); }, 900);
        } else if (this.loginStatus === 'failed') {
          this.ui.err((data && data.message) || '登录失败，请重试');
        } else if (this.loginStatus === 'expired') {
          this.ui.warn('二维码已过期，请点击「刷新二维码」重新获取');
        } else if (this.loginStatus === 'canceled') {
          this.ui.info('已取消登录');
        }
      },
      refreshQr: function () {
        var self = this;
        if (!this.sessionId) { this.startLogin(this.loginAccount); return; }
        this.starting = true;
        this.api.post('/douyin/login/refresh', { session_id: this.sessionId }).then(function (data) {
          self.qrImage = (data && data.qrcode_image) || '';
          self.qrData = (data && data.qrcode_data) || '';
          self.loginStatus = (data && data.status) || 'pending';
          self.loginMessage = '二维码已刷新，请重新扫码';
          self.pollCount = 0;
          self.pollTick();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.starting = false; });
      },

      /* ---------------- 手动导入 Cookie ----------------
         扫码依赖抖音网页的登录弹层与二维码渲染，实测会遇到「弹层打开了但二维码
         图片一直不出现」（风控/网络/版本差异）。此时用户完全可以自己在浏览器里
         登录好，把 Cookie 粘进来——这条路不依赖任何页面结构，最稳。 */
      openCookie: function (row) {
        this.cookieAccount = row;
        this.cookieText = '';
        this.cookieVerify = true;
        this.cookieSubmitting = false;
        this.showCookie = true;
      },
      submitCookie: function () {
        var self = this;
        if (this.cookieSubmitting) return;
        var text = (this.cookieText || '').trim();
        if (!text) { this.ui.warn('请先粘贴 Cookie 内容'); return; }
        if (!this.cookieAccount) return;
        this.cookieSubmitting = true;
        this.stopPoll();
        this.showLogin = false;
        this.api.post('/douyin/accounts/' + this.cookieAccount.id + '/cookies', {
          cookies: text,
          verify: !!this.cookieVerify
        }).then(function (data) {
          var verified = data ? data.verified : null;
          var count = (data && data.cookie_count) || 0;
          if (verified === false) {
            // 已保存但抖音不认：明确告诉用户，别让他以为可以跑任务了
            self.ui.warn('已导入 ' + count + ' 条 Cookie，但抖音未接受这个登录态：'
              + '可能已过期或复制不完整。请重新复制一次。');
          } else if (verified === true) {
            self.succeed('导入成功并校验通过（' + count + ' 条 Cookie），账号已上线');
          } else {
            self.succeed('已导入 ' + count + ' 条 Cookie（未校验），可点「检测」确认');
          }
          self.showCookie = false;
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () {
          self.cookieSubmitting = false;
        });
      },
      sendSms: function () {
        var self = this;
        if (!this.smsPhone.trim()) { this.smsError = '请输入手机号'; return; }
        this.smsSending = true;
        this.smsError = '';
        this.api.post('/douyin/login/sms/send', {
          account_id: this.loginAccount.id, phone: this.smsPhone.trim()
        }).then(function (data) {
          self.sessionId = (data && data.session_id) || self.sessionId;
          self.succeed('验证码已发送，请查看手机短信');
        }).catch(function (err) {
          self.smsError = err.message || '验证码发送失败';
        }).then(function () { self.smsSending = false; });
      },
      submitSms: function () {
        var self = this;
        if (!this.smsCode.trim()) { this.smsError = '请输入短信验证码'; return; }
        this.smsSending = true;
        this.smsError = '';
        this.api.post('/douyin/login/sms/submit', {
          account_id: this.loginAccount.id, session_id: this.sessionId, code: this.smsCode.trim()
        }).then(function (data) {
          var status = (data && data.status) || 'success';
          if (status === 'success' || status === 'confirmed') {
            self.succeed('登录成功，账号登录态已保存');
            self.load();
            self.closeLogin();
          } else {
            self.smsError = (data && data.message) || '验证码校验未通过，请重试';
          }
        }).catch(function (err) {
          self.smsError = err.message || '登录失败';
        }).then(function () { this.smsSending = false; });
      },
      closeLogin: function () {
        var self = this;
        this.stopPoll();
        if (this.sessionId && !this.terminal) {
          // 用户主动关闭：通知后端取消会话，避免后台线程空转
          this.api.post('/douyin/login/cancel', { session_id: this.sessionId }, { silent: true })
            .catch(function () { /* 取消失败不影响关闭弹窗 */ });
        }
        this.showLogin = false;
        this.sessionId = '';
        this.session = null;
        this.qrImage = '';
        this.load();
      },
      stopPoll: function () {
        if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      }
    }
  });

  /* --------------------------- 账号详情 --------------------------- */
  S.defineView('view-account-detail', '/assets/views/account-detail.html', {
    data: function () {
      return {
        id: null,
        loading: false, error: '', account: null,
        tasks: [], tasksLoading: false,
        friends: [], friendsLoading: false, friendsError: '', friendTotal: 0,
        runs: [], runsLoading: false, runsError: '',
        checking: false
      };
    },
    created: function () {
      this.id = this.route.params.id;
      this.load();
    },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/douyin/accounts/' + this.id, null, { silent: true }).then(function (data) {
          self.account = (data && (data.account || data)) || {};
        }).catch(function (err) {
          self.error = err.message || '账号信息加载失败';
        }).then(function () {
          self.loading = false;
          self.loadRelated();
        });
      },
      loadRelated: function () {
        var self = this;
        this.tasksLoading = true;
        this.friendsLoading = true;
        this.runsLoading = true;
        this.api.page('/tasks', { account_id: this.id, page: 1, page_size: 50 }, { silent: true })
          .then(function (res) { self.tasks = res.items; })
          .catch(function () { self.tasks = []; })
          .then(function () { self.tasksLoading = false; });
        this.api.page('/friends', { account_id: this.id, page: 1, page_size: 10 }, { silent: true })
          .then(function (res) { self.friends = res.items; self.friendTotal = res.total; })
          .catch(function (err) { self.friendsError = err.message || ''; })
          .then(function () { self.friendsLoading = false; });
        this.api.page('/douyin/task-records', { account_id: this.id, page: 1, page_size: 10 }, { silent: true })
          .then(function (res) { self.runs = res.items; })
          .catch(function (err) { self.runsError = err.message || ''; })
          .then(function () { self.runsLoading = false; });
      },
      check: function () {
        var self = this;
        this.checking = true;
        this.api.post('/douyin/accounts/' + this.id + '/check', {}).then(function (data) {
          var okFlag = !data || data.valid === undefined ? true : !!data.valid;
          if (okFlag) self.succeed((data && data.message) || '登录态有效');
          else self.ui.warn((data && data.message) || '登录态已失效，请重新扫码登录');
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.checking = false; });
      },
      toggleTask: function (row) {
        var self = this;
        this.api.post('/tasks/' + row.id + '/toggle', {}).then(function () {
          self.succeed(row.enabled ? '任务已停用' : '任务已启用');
          self.loadRelated();
        }).catch(function () { /* api 层已提示 */ });
      },
      remove: function () {
        var self = this;
        this.ui.confirm({
          title: '删除账号',
          message: '确定删除该抖音账号吗？其定时任务、好友与登录态都会被删除，且不可恢复。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          return self.api.del('/douyin/accounts/' + self.id);
        }).then(function (res) {
          if (res === null) return;
          self.succeed('账号已删除');
          self.nav.go('/accounts');
        }).catch(function () { /* api 层已提示 */ });
      }
    }
  });
})();
