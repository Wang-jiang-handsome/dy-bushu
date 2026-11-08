/* ==========================================================================
   views-user-ops.js —— 好友管理 + 执行记录（用户端）
   模板片段：/assets/views/friends.html、/assets/views/records.html
   接口见 docs/API_CONTRACT.md §5：
     GET/POST /friends、POST /friends/import、PUT/DELETE /friends/<friend_id>、
     POST /friends/sync、GET /douyin/stats/send-history、
     GET /douyin/task-records、GET /douyin/task-records/<run_id>
   截图必须走 api.screenshotBlob()：/files/screenshot 需要登录鉴权，<img src> 会 401。
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  S.defineView('view-friends', '/assets/views/friends.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        accounts: [],
        filters: { account_id: '', keyword: '' },
        // 新增 / 编辑
        showForm: false, editing: null, submitting: false, formError: '',
        form: { account_id: '', nickname: '', sec_uid: '', remark: '' },
        // 批量导入
        showImport: false, importing: false, importError: '', importText: '',
        importAccountId: '', importResult: null,
        // 发送台账
        showHistory: false, historyLoading: false, historyError: '', history: [],
        historySummary: null, historyPage: 1, historyPages: 0, historyTotal: 0,
        historyFilters: { account_id: '', start_date: '', end_date: '' },
        // 批量删除（当前页勾选）
        selectedIds: [],
        batchDeleting: false,
        acting: 0
      };
    },
    created: function () { this.loadAccounts(); this.load(); },
    methods: {
      loadAccounts: function () {
        var self = this;
        return this.api.page('/douyin/accounts', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) {
            self.accounts = res.items;
            if (!self.form.account_id && res.items.length) self.form.account_id = res.items[0].id;
            if (!self.importAccountId && res.items.length) self.importAccountId = res.items[0].id;
          })
          .catch(function () { self.accounts = []; });
      },
      accountName: function (id) {
        var hit = this.accounts.filter(function (a) { return String(a.id) === String(id); })[0];
        return hit ? (hit.nickname || hit.remark || ('账号 #' + id)) : (id ? ('账号 #' + id) : '—');
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/friends', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
          self.selectedIds = [];
        }).catch(function (err) {
          self.error = err.message || '好友列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () { this.filters = { account_id: '', keyword: '' }; this.search(); },
      // ---------- 批量删除 ----------
      // 只对「当前页」勾选，避免用户误以为勾了全部 1000 个；翻页不保留选择（语义简单、可预期）
      toggleSelectAll: function () {
        var self = this;
        if (this.selectedIds.length === this.items.length) {
          this.selectedIds = [];
        } else {
          this.selectedIds = this.items.map(function (f) { return Number(f.id); });
        }
        return self.selectedIds;
      },
      toggleSelect: function (row) {
        var id = Number(row.id);
        var at = this.selectedIds.indexOf(id);
        if (at >= 0) this.selectedIds.splice(at, 1);
        else this.selectedIds.push(id);
      },
      isSelected: function (row) { return this.selectedIds.indexOf(Number(row.id)) >= 0; },
      batchRemove: function () {
        var self = this;
        if (this.batchDeleting) return;
        var ids = this.selectedIds.slice();
        if (!ids.length) { this.ui.toast('请先勾选要删除的好友', 'warning'); return; }
        this.ui.confirm({
          title: '批量删除好友',
          message: '确定删除选中的 ' + ids.length + ' 个好友吗？\n删除后这些好友不会再收到火花消息，历史发送台账会保留。',
          confirmText: '全部删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.batchDeleting = true;
          return self.api.post('/friends/batch-delete', { ids: ids }).then(function (res) {
            var n = (res && res.deleted) || 0;
            self.succeed('已删除 ' + n + ' 个好友');
            self.selectedIds = [];
            self.load();
          }).catch(function () { /* api 层已提示 */ })
            .then(function () { self.batchDeleting = false; });
        });
      },
      openCreate: function () {
        this.editing = null;
        this.form = {
          account_id: this.filters.account_id || (this.accounts.length ? this.accounts[0].id : ''),
          nickname: '', sec_uid: '', remark: ''
        };
        this.formError = '';
        this.showForm = true;
      },
      openEdit: function (row) {
        this.editing = row;
        this.form = {
          account_id: row.account_id || '', nickname: row.nickname || '',
          sec_uid: row.sec_uid || '', remark: row.remark || ''
        };
        this.formError = '';
        this.showForm = true;
      },
      saveForm: function () {
        var self = this;
        if (this.submitting) return;
        if (!this.form.account_id) { this.formError = '请选择所属抖音账号'; return; }
        if (!this.form.nickname.trim()) { this.formError = '请填写好友昵称（要与抖音里显示的一致）'; return; }
        this.submitting = true;
        this.formError = '';
        var body = {
          account_id: parseInt(this.form.account_id, 10),
          nickname: this.form.nickname.trim(),
          sec_uid: this.form.sec_uid.trim(),
          remark: this.form.remark.trim()
        };
        var job = this.editing
          ? this.api.put('/friends/' + this.editing.id, body)
          : this.api.post('/friends', body);
        job.then(function () {
          self.showForm = false;
          self.succeed(self.editing ? '好友已更新' : '好友已添加');
          self.load();
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      },
      toggleFriend: function (row) {
        var self = this;
        this.acting = row.id;
        this.api.put('/friends/' + row.id, { enabled: !row.enabled }).then(function () {
          self.succeed(row.enabled ? '已停用该好友（不再收到消息）' : '已启用该好友');
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.acting = 0; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除好友',
          message: '确定删除好友「' + row.nickname + '」吗？\n删除后该好友不会再收到火花消息，历史发送台账会保留。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          return self.api.del('/friends/' + row.id);
        }).then(function (res) {
          if (res === null) return;
          self.succeed('好友已删除');
          self.load();
        }).catch(function () { /* api 层已提示 */ });
      },

      /* ---------------- 批量导入 ---------------- */
      openImport: function () {
        this.importText = '';
        this.importError = '';
        this.importResult = null;
        this.showImport = true;
      },
      doImport: function () {
        var self = this;
        if (this.importing) return;
        if (!this.importAccountId) { this.importError = '请选择要导入到哪个账号'; return; }
        var lines = this.fmt.lines(this.importText);
        if (!lines.length) { this.importError = '请至少输入一行好友昵称'; return; }
        this.importing = true;
        this.importError = '';
        // 实测定稿：后端读的是 text 字段（只发 content 会 400「没有解析到任何好友」）
        this.api.post('/friends/import', {
          account_id: parseInt(this.importAccountId, 10),
          text: this.importText
        }).then(function (data) {
          self.importResult = data || {};
          var added = self.importResult.imported || self.importResult.created ||
            self.importResult.added || self.importResult.success;
          if (added === undefined) self.succeed('导入完成，共 ' + lines.length + ' 行');
          else self.succeed('导入完成：成功 ' + added + ' 条');
          self.load();
        }).catch(function (err) {
          self.importError = err.message || '导入失败';
        }).then(function () { self.importing = false; });
      },

      /* ---------------- 从抖音同步 ---------------- */
      sync: function () {
        var self = this;
        var accountId = this.filters.account_id || (this.accounts.length ? this.accounts[0].id : '');
        if (!accountId) { this.ui.warn('请先在「抖音账号」里添加一个账号'); return; }
        this.ui.confirm({
          title: '从抖音同步好友',
          message: '将读取该账号在抖音里的最近会话好友并加入列表。\n需要账号登录态有效，过程中会短暂启动浏览器。',
          confirmText: '开始同步', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = -1;
          return self.api.post('/friends/sync', { account_id: parseInt(accountId, 10) });
        }).then(function (data) {
          if (data === null) { self.acting = 0; return; }
          var added = data && (data.added !== undefined ? data.added : data.synced);
          self.succeed(added === undefined ? '同步完成' : ('同步完成，新增 ' + added + ' 位好友'));
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },

      /* ---------------- 发送台账 ---------------- */
      openHistory: function () {
        this.showHistory = true;
        this.historyPage = 1;
        this.historyFilters.account_id = this.filters.account_id || '';
        this.loadHistory();
      },
      loadHistory: function () {
        var self = this;
        this.historyLoading = true;
        this.historyError = '';
        this.api.page('/douyin/stats/send-history', Object.assign({
          page: this.historyPage, page_size: 20
        }, this.historyFilters), { silent: true }).then(function (res) {
          self.history = res.items;
          self.historyPages = res.pages;
          self.historyTotal = res.total;
        }).catch(function (err) {
          self.historyError = err.message || '发送台账加载失败';
        }).then(function () { self.historyLoading = false; });
      },
      historyFriend: function (row) {
        return row.friend_name || row.friend_key || row.nickname || '—';
      }
    }
  });

  /* --------------------------- 执行记录 --------------------------- */
  S.defineView('view-records', '/assets/views/records.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        accounts: [],
        filters: { account_id: '', status: '', start_date: '', end_date: '' },
        showDetail: false, detailLoading: false, detailError: '', detail: null,
        shots: {}, showShot: false, shotUrl: '', acting: false
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.run_status); },
      detailItems: function () {
        var d = this.detail || {};
        if (Array.isArray(d.items)) return d.items;
        if (d.detail && Array.isArray(d.detail.items)) return d.detail.items;
        return [];
      },
      detailShots: function () {
        var d = this.detail || {};
        var list = d.screenshots || d.run_screenshots || [];
        if (typeof list === 'string') {
          try { list = JSON.parse(list); } catch (e) { list = []; }
        }
        return Array.isArray(list) ? list : [];
      }
    },
    created: function () {
      var self = this;
      this.loadAccounts().then(function () {
        self.load().then(function () {
          if (self.route.params.id) self.openDetail(self.route.params.id);
        });
      });
    },
    methods: {
      loadAccounts: function () {
        var self = this;
        return this.api.page('/douyin/accounts', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) { self.accounts = res.items; })
          .catch(function () { self.accounts = []; });
      },
      accountName: function (row) {
        if (row.account_nickname) return row.account_nickname;
        var id = row.account_id;
        var hit = this.accounts.filter(function (a) { return String(a.id) === String(id); })[0];
        return hit ? (hit.nickname || hit.remark || ('账号 #' + id)) : ('账号 #' + id);
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/douyin/task-records', Object.assign({
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
        this.filters = { account_id: '', status: '', start_date: '', end_date: '' };
        this.search();
      },
      openDetail: function (id) {
        var self = this;
        this.showDetail = true;
        this.detailLoading = true;
        this.detailError = '';
        this.detail = null;
        this.shots = {};
        this.api.get('/douyin/task-records/' + id, null, { silent: true })
          .then(function (data) {
            self.detail = data || {};
            self.loadItemShots(self.detailItems);
            self.loadRunShots();
          })
          .catch(function (err) { self.detailError = err.message || '详情加载失败'; })
          .then(function () { self.detailLoading = false; });
      },
      loadItemShots: function (items) {
        var self = this;
        var runId = (this.detail && (this.detail.id || this.detail.run_id)) || this.route.params.id;
        (items || []).forEach(function (item) {
          if (!item || !item.screenshot) return;
          var key = item.id || item.friend_name;
          self.api.screenshotBlob(runId, item.screenshot).then(function (url) {
            // Vue 3 的 reactive 是 Proxy，新增键也能触发更新
            self.shots[key] = url;
          }).catch(function () { /* 截图缺失不影响详情阅读 */ });
        });
      },
      // 任务级截图（task_runs.screenshots JSON 数组），key 用 run-<下标> 避免与好友截图冲突
      loadRunShots: function () {
        var self = this;
        var runId = (this.detail && (this.detail.id || this.detail.run_id)) || this.route.params.id;
        (this.detailShots || []).forEach(function (name, index) {
          if (!name) return;
          self.api.screenshotBlob(runId, name).then(function (url) {
            self.shots['run-' + index] = url;
          }).catch(function () { /* 忽略单张失败 */ });
        });
      },
      openShot: function (url) {
        if (!url) return;
        this.shotUrl = url;
        this.showShot = true;
      },
      runAgain: function () {
        var self = this;
        var taskId = this.detail && this.detail.task_id;
        if (!taskId) { this.ui.warn('该记录没有关联的任务，无法重跑'); return; }
        this.ui.confirm({
          title: '立即重跑该任务',
          message: '将立刻执行一次关联任务，会真的给好友发送消息。',
          confirmText: '立即执行', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          return self.api.post('/tasks/' + taskId + '/run', {});
        }).then(function (res) {
          if (res !== null) self.succeed('已提交执行');
          self.acting = false;
        }).catch(function () { self.acting = false; });
      }
    }
  });
})();
