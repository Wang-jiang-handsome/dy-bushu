/* ==========================================================================
   views-admin-core.js —— 管理概览 + 用户管理（管理端，仅 admin 可见）
   模板片段：/assets/views/admin-dashboard.html、/assets/views/admin-users.html
   接口见 docs/API_CONTRACT.md §7（已实测字段）：
     GET /admin/stats → {users{total,today_new,active_7d,disabled},
       accounts{total,online,expired}, tasks{total,enabled,today_runs,today_success},
       orders{total,paid,pending,refunded,revenue_cents,today_revenue_cents},
       cards{total,used,unused,disabled}, trend[{date,runs,success,orders,revenue_cents}]}
     GET /admin/users、POST /admin/users、GET/PUT /admin/users/<id>、
     POST /admin/users/<id>/reset-password | grant-package | balance、DELETE /admin/users/<id>
   实测：POST /balance 用 change_cents（0 会被拒：「调整金额不能为 0」）；
        grant-package 用 {package_code, days}。
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  S.defineView('view-admin-dashboard', '/assets/views/admin-dashboard.html', {
    data: function () {
      return {
        loading: false, error: '', stats: null,
        autoRefresh: false, timer: null, lastLoadedAt: '',
        logs: [], logsLoading: false, logsError: ''
      };
    },
    computed: {
      users: function () { return (this.stats && this.stats.users) || {}; },
      accounts: function () { return (this.stats && this.stats.accounts) || {}; },
      tasks: function () { return (this.stats && this.stats.tasks) || {}; },
      orders: function () { return (this.stats && this.stats.orders) || {}; },
      cards: function () { return (this.stats && this.stats.cards) || {}; },
      trend: function () { return (this.stats && this.stats.trend) || []; },
      trendMax: function () {
        var max = 1;
        this.trend.forEach(function (d) {
          if ((d.runs || 0) > max) max = d.runs || 0;
        });
        return max;
      },
      revenueTrendMax: function () {
        var max = 1;
        this.trend.forEach(function (d) {
          if ((d.revenue_cents || 0) > max) max = d.revenue_cents || 0;
        });
        return max;
      }
    },
    created: function () { this.load(); this.loadLogs(); },
    unmounted: function () { this.stopTimer(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/admin/stats', null, { silent: true }).then(function (data) {
          self.stats = data || {};
          self.lastLoadedAt = self.timeText(new Date().toISOString());
        }).catch(function (err) {
          self.error = err.message || '统计加载失败';
        }).then(function () { self.loading = false; });
      },
      loadLogs: function () {
        var self = this;
        this.logsLoading = true;
        this.logsError = '';
        return this.api.page('/admin/logs', { page: 1, page_size: 10, level: 'error' }, { silent: true })
          .then(function (res) { self.logs = res.items; })
          .catch(function (err) { self.logsError = err.message || ''; })
          .then(function () { self.logsLoading = false; });
      },
      barHeight: function (row, key) {
        var v = row[key] || 0;
        if (!v) return '0%';
        return Math.max(4, Math.round(v * 100 / this.trendMax)) + '%';
      },
      revenueHeight: function (row) {
        var v = row.revenue_cents || 0;
        if (!v) return '0%';
        return Math.max(4, Math.round(v * 100 / this.revenueTrendMax)) + '%';
      },
      toggleAuto: function () {
        var self = this;
        this.autoRefresh = !this.autoRefresh;
        this.stopTimer();
        if (this.autoRefresh) {
          this.timer = setInterval(function () { self.load(); self.loadLogs(); }, 30000);
          this.ui.info('已开启自动刷新（每 30 秒）');
        }
      },
      stopTimer: function () { if (this.timer) { clearInterval(this.timer); this.timer = null; } }
    }
  });

  /* --------------------------- 用户管理 --------------------------- */
  S.defineView('view-admin-users', '/assets/views/admin-users.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { keyword: '', role: '', status: '', package_id: '' },
        packages: [],
        // 新建
        showForm: false, submitting: false, formError: '',
        form: {
          username: '', password: '', nickname: '', email: '', phone: '',
          role: 'user', status: 'active'
        },
        // 详情
        showDetail: false, detailLoading: false, detailError: '', detail: null,
        related: { accounts: [], tasks: [], orders: [] }, relatedLoading: false,
        acting: false
      };
    },
    computed: {
      roleOptions: function () { return this.OPTIONS.roles; },
      statusOptions: function () { return this.OPTIONS.user_status; }
    },
    created: function () { this.loadPackages(); this.load(); },
    unmounted: function () { /* 无定时器 */ },
    methods: {
      loadPackages: function () {
        var self = this;
        return this.api.page('/admin/packages', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) { self.packages = res.items; })
          .catch(function () { self.packages = []; });
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/users', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '用户列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () {
        this.filters = { keyword: '', role: '', status: '', package_id: '' };
        this.search();
      },
      openCreate: function () {
        this.form = {
          username: '', password: '', nickname: '', email: '', phone: '',
          role: 'user', status: 'active'
        };
        this.formError = '';
        this.showForm = true;
      },
      saveForm: function () {
        var self = this;
        if (this.submitting) return;
        if (!/^[A-Za-z0-9_]{3,20}$/.test(this.form.username)) {
          this.formError = '用户名需为 3-20 位字母、数字或下划线';
          return;
        }
        if (!this.form.password || this.form.password.length < 8) {
          this.formError = '初始密码至少 8 位';
          return;
        }
        this.submitting = true;
        this.formError = '';
        this.api.post('/admin/users', {
          username: this.form.username.trim(), password: this.form.password,
          nickname: this.form.nickname.trim(), email: this.form.email.trim(),
          phone: this.form.phone.trim(), role: this.form.role, status: this.form.status
        }).then(function () {
          self.showForm = false;
          self.succeed('用户已创建');
          self.load();
        }).catch(function (err) {
          self.formError = err.message || '创建失败';
        }).then(function () { self.submitting = false; });
      },
      openDetail: function (row) {
        var self = this;
        this.showDetail = true;
        this.detailLoading = true;
        this.detailError = '';
        this.detail = row;
        this.related = { accounts: [], tasks: [], orders: [] };
        this.api.get('/admin/users/' + row.id, null, { silent: true }).then(function (data) {
          self.detail = Object.assign({}, row, data || {});
          self.loadRelated(self.detail);
        }).catch(function (err) {
          self.detailError = err.message || '用户详情加载失败';
        }).then(function () { self.detailLoading = false; });
      },
      loadRelated: function (user) {
        var self = this;
        if (!user || !user.id) return;
        this.relatedLoading = true;
        var jobs = [
          this.api.page('/admin/douyin-accounts', { user_id: user.id, page: 1, page_size: 5 }, { silent: true })
            .then(function (res) { self.related.accounts = res.items; }).catch(function () { /* 忽略 */ }),
          this.api.page('/admin/scheduled-tasks', { user_id: user.id, page: 1, page_size: 5 }, { silent: true })
            .then(function (res) { self.related.tasks = res.items; }).catch(function () { /* 忽略 */ }),
          this.api.page('/admin/orders', { keyword: user.username, page: 1, page_size: 5 }, { silent: true })
            .then(function (res) { self.related.orders = res.items; }).catch(function () { /* 忽略 */ })
        ];
        Promise.all(jobs).then(function () { self.relatedLoading = false; });
      },
      toggleStatus: function (row) {
        var self = this;
        var next = row.status === 'active' ? 'disabled' : 'active';
        this.ui.confirm({
          title: next === 'disabled' ? '禁用用户' : '启用用户',
          message: next === 'disabled'
            ? '禁用后该用户无法登录，其定时任务也会停止执行。确定禁用「' + row.username + '」吗？'
            : '确定恢复「' + row.username + '」的登录权限吗？',
          confirmText: next === 'disabled' ? '确认禁用' : '确认启用',
          danger: next === 'disabled'
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          return self.api.put('/admin/users/' + row.id, { status: next });
        }).then(function (res) {
          if (res === null) { self.acting = false; return; }
          self.succeed('已更新用户状态');
          self.load();
          if (self.detail && self.detail.id === row.id) self.detail.status = next;
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      changeRole: function (row, role) {
        var self = this;
        if (!role || role === row.role) return;
        this.ui.confirm({
          title: '修改角色',
          message: '把「' + row.username + '」的角色改为「' + this.statusText('role', role) + '」？\n' +
            (role === 'admin' ? '管理员可以查看和修改全站数据，请谨慎授予。' : ''),
          confirmText: '确认修改', danger: role === 'admin'
        }).then(function (ok) {
          if (!ok) { row.role = self.detail && self.detail.id === row.id ? self.detail.role : row.role; return null; }
          self.acting = true;
          return self.api.put('/admin/users/' + row.id, { role: role });
        }).then(function (res) {
          if (res === null) { self.acting = false; self.load(); return; }
          self.succeed('角色已修改');
          self.load();
          self.acting = false;
        }).catch(function () { self.acting = false; self.load(); });
      },
      resetPassword: function (row) {
        var self = this;
        this.ui.prompt({
          title: '重置密码',
          message: '为用户「' + row.username + '」设置新密码。留空则由系统随机生成并显示一次。',
          inputLabel: '新密码（至少 8 位）',
          inputPlaceholder: '留空自动生成',
          inputRequired: false,
          confirmText: '重置密码'
        }).then(function (value) {
          if (value === null) return null;
          self.acting = true;
          return self.api.post('/admin/users/' + row.id + '/reset-password',
            value ? { password: value, new_password: value } : {});
        }).then(function (data) {
          if (data === null || data === undefined) { self.acting = false; return; }
          var pwd = (data && (data.password || data.new_password)) || '';
          if (pwd) {
            self.ui.confirm({
              title: '新密码已生成',
              message: '请立即复制并转告用户，本弹窗关闭后无法再查看：\n\n' + pwd,
              confirmText: '我已复制', cancelText: '关闭', danger: false
            });
          } else {
            self.succeed('密码已重置');
          }
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      grantPackage: function (row) {
        var self = this;
        var options = this.packages.filter(function (p) { return p.is_active !== false; });
        if (!options.length) { this.ui.warn('没有可授权的套餐，请先在「套餐管理」里创建'); return; }
        this.ui.confirm({
          title: '授权套餐',
          message: '为用户「' + row.username + '」授权套餐：' +
            options.map(function (p) { return p.name + '（' + p.days + ' 天）'; }).join(' / '),
          confirmText: '授权体验卡（7 天）',
          cancelText: '取消',
          danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          var trial = options.filter(function (p) { return p.code === 'trial'; })[0] || options[0];
          return self.api.post('/admin/users/' + row.id + '/grant-package', {
            package_code: trial.code, package_id: trial.id, days: trial.days || 7
          });
        }).then(function (data) {
          if (data === null || data === undefined) { self.acting = false; return; }
          var text = data && (data.package_name ? ('套餐：' + data.package_name) : '');
          self.succeed('已授权' + (text ? ' ' + text : ''));
          self.load();
          if (self.detail && self.detail.id === row.id) self.openDetail(row);
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      adjustBalance: function (row) {
        var self = this;
        this.ui.prompt({
          title: '调整余额',
          message: '为正数则增加余额，负数则扣减。当前余额 ' + this.money(row.balance_cents) + '。',
          inputLabel: '调整金额（元，可正可负）',
          inputPlaceholder: '例如 10 或 -5.5',
          inputRequired: true,
          inputType: 'text',
          confirmText: '下一步'
        }).then(function (value) {
          if (value === null) return null;
          var cents = self.fmt.toCents(value);
          if (!cents) { self.ui.warn('调整金额不能为 0'); return null; }
          return self.ui.prompt({
            title: '填写备注（必填）',
            message: '将' + (cents > 0 ? '增加' : '扣减') + ' ' + self.money(Math.abs(cents)) +
              '，请填写备注，会写入审计日志。',
            inputLabel: '备注',
            inputPlaceholder: '例如：手工补偿 / 活动赠送',
            inputRequired: true,
            confirmText: '确认调整'
          }).then(function (remark) {
            if (remark === null) return null;
            self.acting = true;
            // 实测确认：只认 change_cents（正数加、负数减），remark 必填；不要再发 amount_cents 制造歧义
            return self.api.post('/admin/users/' + row.id + '/balance', {
              change_cents: cents, remark: remark
            });
          });
        }).then(function (data) {
          if (data === null || data === undefined) { self.acting = false; return; }
          self.succeed('余额已调整');
          self.load();
          if (self.detail && self.detail.id === row.id) self.openDetail(row);
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      removeUser: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除用户',
          message: '确定删除用户「' + row.username + '」吗？\n' +
            '该用户的所有抖音账号、定时任务、好友、订单、卡密兑换记录都会被一并删除，且不可恢复。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          return self.api.del('/admin/users/' + row.id);
        }).then(function (res) {
          if (res === null) { self.acting = false; return; }
          self.succeed('用户已删除');
          self.showDetail = false;
          self.load();
          self.acting = false;
        }).catch(function () { self.acting = false; });
      }
    }
  });
})();
