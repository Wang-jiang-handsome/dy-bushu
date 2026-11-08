/* ==========================================================================
   views-admin-ops.js —— 账号总览 + 任务总览 + 执行记录（管理端）
   模板片段：/assets/views/admin-accounts.html、admin-tasks.html、admin-records.html
   接口见 docs/API_CONTRACT.md §7：
     GET /admin/douyin-accounts、POST /admin/douyin-accounts/<account_id>/status {status}
     GET /admin/scheduled-tasks、POST /admin/scheduled-tasks/batch {ids, action}
     POST /admin/tasks/<task_id>/run、GET /admin/tasks（= /admin/task-records 别名）
     详情复用 GET /douyin/task-records/<run_id>（需归属校验，越权时会返回 403）
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  S.defineView('view-admin-accounts', '/assets/views/admin-accounts.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { keyword: '', status: '', user_id: '' },
        acting: 0
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.account_status); },
      onlineCount: function () {
        return this.items.filter(function (a) { return a.status === 'online'; }).length;
      }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/douyin-accounts', Object.assign({
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
      reset: function () { this.filters = { keyword: '', status: '', user_id: '' }; this.search(); },
      changeStatus: function (row, status) {
        var self = this;
        if (!status || status === row.status) return;
        this.ui.confirm({
          title: '修改账号状态',
          message: '把「' + (row.nickname || ('账号 #' + row.id)) + '」的状态改为「' +
            this.statusText('account', status) + '」？\n' +
            (status === 'offline' || status === 'expired'
              ? '该账号的定时任务将无法执行，直到用户重新扫码登录。' : ''),
          confirmText: '确认修改', danger: status === 'banned'
        }).then(function (ok) {
          if (!ok) { self.load(); return null; }
          self.acting = row.id;
          return self.api.post('/admin/douyin-accounts/' + row.id + '/status', { status: status });
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('账号状态已更新');
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; self.load(); });
      },
      ownerText: function (row) {
        return row.username || row.nickname_owner || (row.user_id ? ('用户 #' + row.user_id) : '—');
      }
    }
  });

  /* --------------------------- 任务总览 --------------------------- */
  S.defineView('view-admin-tasks', '/assets/views/admin-tasks.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { keyword: '', enabled: '', user_id: '' },
        selected: [], acting: 0, batchRunning: false
      };
    },
    computed: {
      enabledOptions: function () {
        return [
          { value: '', label: '全部任务' },
          { value: '1', label: '仅已启用' },
          { value: '0', label: '仅已停用' }
        ];
      },
      allSelected: function () {
        return this.items.length > 0 && this.selected.length === this.items.length;
      }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/scheduled-tasks', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
          self.selected = [];
        }).catch(function (err) {
          self.error = err.message || '任务列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () { this.filters = { keyword: '', enabled: '', user_id: '' }; this.search(); },
      toggleAll: function () {
        if (this.allSelected) this.selected = [];
        else this.selected = this.items.map(function (t) { return t.id; });
      },
      has: function (id) { return this.selected.indexOf(id) >= 0; },
      toggleOne: function (id) {
        var index = this.selected.indexOf(id);
        if (index >= 0) this.selected.splice(index, 1);
        else this.selected.push(id);
      },
      batch: function (action) {
        var self = this;
        if (!this.selected.length) { this.ui.warn('请先勾选要操作的任务'); return; }
        var labels = { enable: '批量启用', disable: '批量停用', delete: '批量删除' };
        this.ui.confirm({
          title: labels[action] || '批量操作',
          message: '将对选中的 ' + this.selected.length + ' 个任务执行「' + (labels[action] || action) + '」。' +
            (action === 'delete' ? '\n删除后任务配置不可恢复（历史执行记录会保留）。' : ''),
          confirmText: labels[action] || '确认',
          danger: action === 'delete'
        }).then(function (ok) {
          if (!ok) return null;
          self.batchRunning = true;
          return self.api.post('/admin/scheduled-tasks/batch', { ids: self.selected, action: action });
        }).then(function (res) {
          if (res === null) { self.batchRunning = false; return; }
          var affected = res && (res.affected !== undefined ? res.affected : res.count);
          self.succeed((labels[action] || '操作') + '完成' + (affected !== undefined ? '（' + affected + ' 个）' : ''));
          self.load();
          self.batchRunning = false;
        }).catch(function () { self.batchRunning = false; });
      },
      toggle: function (row) {
        var self = this;
        this.acting = row.id;
        this.api.post('/admin/scheduled-tasks/batch', {
          ids: [row.id], action: row.enabled ? 'disable' : 'enable'
        }).then(function () {
          self.succeed(row.enabled ? '任务已停用' : '任务已启用');
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.acting = 0; });
      },
      runNow: function (row) {
        var self = this;
        this.ui.confirm({
          title: '管理员代跑一次',
          message: '将立即执行任务「' + (row.name || ('#' + row.id)) + '」。\n' +
            '这会占用该账号当天的执行次数，并且真的会给好友发送消息。',
          confirmText: '立即执行', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.post('/admin/tasks/' + row.id + '/run', {});
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('已提交执行，可在执行记录里查看结果');
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },
      repeatText: function (row) {
        if (row.repeat_mode === 'interval') {
          var mins = row.interval_minutes || 1440;
          if (mins % 1440 === 0) return '每 ' + (mins / 1440) + ' 天';
          if (mins % 60 === 0) return '每 ' + (mins / 60) + ' 小时';
          return '每 ' + mins + ' 分钟';
        }
        return this.statusText('repeat', row.repeat_mode);
      }
    }
  });

  /* --------------------------- 执行记录（全站） --------------------------- */
  S.defineView('view-admin-records', '/assets/views/admin-records.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { account_id: '', status: '', user_id: '', start_date: '', end_date: '' },
        showDetail: false, detailLoading: false, detail: null, detailError: '',
        shots: {}, showShot: false, shotUrl: ''
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.run_status); },
      detailItems: function () {
        var d = this.detail || {};
        if (Array.isArray(d.items)) return d.items;
        if (d.detail && Array.isArray(d.detail.items)) return d.detail.items;
        return [];
      }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/tasks', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '执行记录加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () {
        this.filters = { account_id: '', status: '', user_id: '', start_date: '', end_date: '' };
        this.search();
      },
      ownerText: function (row) {
        return row.username || (row.user_id ? ('用户 #' + row.user_id) : '—');
      },
      openDetail: function (row) {
        var self = this;
        this.showDetail = true;
        this.detailLoading = true;
        this.detailError = '';
        this.detail = row;
        this.shots = {};
        this.api.get('/douyin/task-records/' + row.id, null, { silent: true }).then(function (data) {
          self.detail = data || row;
          self.loadItemShots();
        }).catch(function (err) {
          // 该接口按「数据归属」校验，管理员查看别人的记录会被拒绝：如实提示，不伪装成功
          self.detailError = (err && err.status === 403)
            ? '该记录属于其他用户，明细接口按归属校验，无法查看每个好友的结果与截图。'
            : (err.message || '详情加载失败');
        }).then(function () { self.detailLoading = false; });
      },
      loadItemShots: function () {
        var self = this;
        var runId = (this.detail && this.detail.id) || null;
        (this.detailItems || []).forEach(function (item) {
          if (!item || !item.screenshot) return;
          var key = item.id || item.friend_name;
          self.api.screenshotBlob(runId, item.screenshot).then(function (url) {
            self.shots[key] = url;
          }).catch(function () { /* 截图缺失不影响阅读 */ });
        });
      },
      openShot: function (url) {
        if (!url) return;
        this.shotUrl = url;
        this.showShot = true;
      }
    }
  });
})();
