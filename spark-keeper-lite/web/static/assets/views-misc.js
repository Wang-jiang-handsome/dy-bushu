/* ==========================================================================
   views-misc.js —— 运行日志 + 个人设置 + 站内通知（用户端）
   模板片段：/assets/views/logs.html、settings.html、notifications.html
   接口见 docs/API_CONTRACT.md §2/§3：
     GET /user/logs、GET /user/info、PUT /user/profile、POST /user/change-password、
     GET /user/notifications、POST /user/notifications/read、GET /user/announcements
   实测：/user/notifications 的 data 额外带 unread；item 有 is_read 布尔字段。
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  function parseContext(raw) {
    if (!raw) return '';
    if (typeof raw === 'object') return JSON.stringify(raw, null, 2);
    var text = String(raw);
    try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text; }
  }

  S.defineView('view-logs', '/assets/views/logs.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { level: '', source: '', keyword: '', start_date: '', end_date: '' },
        expanded: {}
      };
    },
    computed: {
      levelOptions: function () { return this.OPTIONS.levels; }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/user/logs', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '日志加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () {
        this.filters = { level: '', source: '', keyword: '', start_date: '', end_date: '' };
        this.search();
      },
      toggle: function (row) {
        var key = row.id;
        this.expanded[key] = !this.expanded[key];
      },
      contextText: function (row) { return parseContext(row.context); },
      hasContext: function (row) {
        var text = this.contextText(row);
        return !!text && text !== '{}' && text !== '[]' && text !== '""';
      }
    }
  });

  /* --------------------------- 个人设置 --------------------------- */
  S.defineView('view-settings', '/assets/views/settings.html', {
    data: function () {
      return {
        loading: false, error: '',
        profile: { nickname: '', email: '', phone: '', avatar: '' },
        savingProfile: false, profileError: '',
        pwd: { old_password: '', new_password: '', confirm_password: '' },
        savingPwd: false, pwdError: ''
      };
    },
    computed: {
      avatarPreview: function () {
        return this.profile.avatar || this.user.avatar || '';
      },
      expireText: function () {
        if (!this.user.package_expire_at) return '—';
        return this.timeText(this.user.package_expire_at) + '（' + this.agoText(this.user.package_expire_at) + '）';
      },
      isDark: function () { return this.store.theme === 'dark'; }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/user/info', null, { silent: true }).then(function (data) {
          var u = (data && (data.user || data)) || {};
          self.profile = {
            nickname: u.nickname || '', email: u.email || '',
            phone: u.phone || '', avatar: u.avatar || ''
          };
        }).catch(function (err) {
          self.error = err.message || '账号信息加载失败';
        }).then(function () { self.loading = false; });
      },
      saveProfile: function () {
        var self = this;
        if (this.savingProfile) return;
        if (this.profile.email && this.profile.email.indexOf('@') < 0) {
          this.profileError = '邮箱格式不正确';
          return;
        }
        this.savingProfile = true;
        this.profileError = '';
        this.api.put('/user/profile', {
          nickname: this.profile.nickname.trim(),
          email: this.profile.email.trim(),
          phone: this.profile.phone.trim(),
          avatar: this.profile.avatar.trim()
        }).then(function () {
          self.succeed('资料已保存');
          return self.store.refreshUser();
        }).catch(function (err) {
          self.profileError = err.message || '保存失败';
        }).then(function () { self.savingProfile = false; });
      },
      changePassword: function () {
        var self = this;
        if (this.savingPwd) return;
        var p = this.pwd;
        if (!p.old_password) { this.pwdError = '请输入当前密码'; return; }
        if (!p.new_password || p.new_password.length < 8) { this.pwdError = '新密码至少 8 位'; return; }
        if (p.new_password !== p.confirm_password) { this.pwdError = '两次输入的新密码不一致'; return; }
        if (p.new_password === p.old_password) { this.pwdError = '新密码不能与当前密码相同'; return; }
        this.savingPwd = true;
        this.pwdError = '';
        this.api.post('/user/change-password', {
          old_password: p.old_password, new_password: p.new_password,
          confirm_password: p.confirm_password
        }).then(function () {
          self.succeed('密码已修改，请使用新密码重新登录');
          self.store.clearSession();
          self.nav.go('/login');
        }).catch(function (err) {
          self.pwdError = err.message || '修改失败';
        }).then(function () { self.savingPwd = false; });
      },
      switchTheme: function () { this.store.toggleTheme(); },
      logout: function () {
        this.ui.confirm({
          title: '退出登录', message: '确定要退出当前账号吗？', confirmText: '退出', danger: false
        }).then(function (ok) { if (ok) S.api.auth.logout(); });
      },
      clearAvatar: function () { this.profile.avatar = ''; }
    }
  });

  /* --------------------------- 站内通知 --------------------------- */
  S.defineView('view-notifications', '/assets/views/notifications.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        unread: 0, marking: false,
        announcementsLoading: false, announcements: [], announcementsError: ''
      };
    },
    created: function () { this.load(); this.loadAnnouncements(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/user/notifications', {
          page: this.page, page_size: this.pageSize
        }, { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
          // 后端实测会在 data 里直接给 unread；没有就按 is_read 统计
          self.unread = res.unread >= 0 ? res.unread : self.computeUnread();
        }).catch(function (err) {
          self.error = err.message || '通知加载失败';
        }).then(function () { self.loading = false; });
      },
      computeUnread: function () {
        var count = 0;
        this.items.forEach(function (row) {
          if (!row) return;
          if (row.is_read !== undefined) { if (row.is_read === false) count += 1; }
          else if (!row.read_at) count += 1;
        });
        return count;
      },
      loadAnnouncements: function () {
        var self = this;
        this.announcementsLoading = true;
        this.announcementsError = '';
        return this.api.get('/user/announcements', { page: 1, page_size: 5 }, { silent: true })
          .then(function (data) {
            var d = data || {};
            var list = d.items || (Array.isArray(d) ? d : []);
            self.announcements = Array.isArray(list) ? list : [];
          })
          .catch(function (err) {
            self.announcementsError = err.message || '公告加载失败';
          })
          .then(function () { self.announcementsLoading = false; });
      },
      isUnread: function (row) {
        if (row.is_read !== undefined) return row.is_read === false;
        return !row.read_at;
      },
      markOne: function (row) {
        var self = this;
        if (!this.isUnread(row)) { this.ui.info('该通知已是已读状态'); return; }
        this.marking = true;
        this.api.post('/user/notifications/read', { ids: [row.id] }).then(function () {
          row.is_read = true;
          row.read_at = new Date().toISOString().slice(0, 19).replace('T', ' ');
          self.unread = self.computeUnread();
          self.store.loadUnread();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.marking = false; });
      },
      markAll: function () {
        var self = this;
        if (!this.unread) { this.ui.info('没有未读通知'); return; }
        this.ui.confirm({
          title: '全部标为已读',
          message: '确定把当前所有未读通知都标为已读吗？',
          confirmText: '全部已读', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.marking = true;
          return self.api.post('/user/notifications/read', { all: true });
        }).then(function (res) {
          if (res === null) { self.marking = false; return; }
          self.succeed('已全部标为已读');
          self.store.loadUnread();
          self.load();
          self.marking = false;
        }).catch(function () { self.marking = false; });
      }
    }
  });
})();
