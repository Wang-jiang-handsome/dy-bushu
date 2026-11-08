/* ==========================================================================
   views-tasks.js —— 定时任务列表 + 新建/编辑表单
   模板片段：/assets/views/tasks.html、/assets/views/task-edit.html
   接口见 docs/API_CONTRACT.md §5：
     GET/POST /tasks、GET/PUT/DELETE /tasks/<task_id>、POST /tasks/<task_id>/toggle、
     POST /tasks/<task_id>/run、POST /tasks/<task_id>/preview、GET /tasks/<task_id>/runs、
     GET /douyin/accounts、GET /friends
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;
  var DEFAULT_MESSAGES = ['火花不能断 🔥', '打卡 ✨', '来续个火花～'];

  S.defineView('view-tasks', '/assets/views/tasks.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { account_id: '', enabled: '' },
        accounts: [],
        acting: 0,                       // 正在操作的任务 id，防重复点击
        showPreview: false, previewLoading: false, previewTask: null, previewData: null,
        showRuns: false, runsLoading: false, runsTask: null, runs: [], runsError: '',
        runsPage: 1, runsPages: 0, runsTotal: 0
      };
    },
    computed: {
      enabledOptions: function () {
        return [
          { value: '', label: '全部任务' },
          { value: '1', label: '仅已启用' },
          { value: '0', label: '仅已停用' }
        ];
      }
    },
    created: function () { this.loadAccounts(); this.load(); },
    methods: {
      loadAccounts: function () {
        var self = this;
        this.api.page('/douyin/accounts', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) { self.accounts = res.items; })
          .catch(function () { self.accounts = []; });
      },
      accountName: function (id) {
        var hit = this.accounts.filter(function (a) { return String(a.id) === String(id); })[0];
        return hit ? (hit.nickname || hit.remark || ('账号 #' + id)) : ('账号 #' + id);
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/tasks', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '任务列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () { this.filters = { account_id: '', enabled: '' }; this.search(); },
      toggle: function (row) {
        var self = this;
        this.acting = row.id;
        this.api.post('/tasks/' + row.id + '/toggle', {}).then(function (data) {
          self.succeed(row.enabled ? '任务已停用' : '任务已启用');
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.acting = 0; });
      },
      runNow: function (row) {
        var self = this;
        this.ui.confirm({
          title: '立即执行一次',
          message: '将立刻执行任务「' + (row.name || ('#' + row.id)) + '」一次。\n' +
            '这会占用该账号今天的执行次数，并且真的会给好友发送消息。',
          confirmText: '立即执行', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.post('/tasks/' + row.id + '/run', {});
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('已提交执行，稍后可在执行记录中查看结果');
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },
      preview: function (row) {
        var self = this;
        this.previewTask = row;
        this.previewData = null;
        this.showPreview = true;
        this.previewLoading = true;
        this.api.post('/tasks/' + row.id + '/preview', {}).then(function (data) {
          self.previewData = data || {};
        }).catch(function (err) {
          self.previewData = { error: err.message || '预览失败' };
        }).then(function () { self.previewLoading = false; });
      },
      // 实测响应：{next_runs:[UTC...], next_runs_local:[本地时间...], messages:[...], message_warnings:[]}
      previewTimes: function () {
        var d = this.previewData || {};
        var list = d.times || d.next_runs || d.run_times || d.items || [];
        var local = d.next_runs_local || d.times_local || [];
        var messages = Array.isArray(d.messages) ? d.messages : [];
        if (!Array.isArray(list)) return [];
        return list.map(function (it, index) {
          if (typeof it === 'string') {
            return {
              time: it,
              local: local[index] || '',
              message: messages[index] || ''
            };
          }
          return {
            time: it.time || it.run_at || it.next_run_at || '',
            local: it.local || it.run_at_local || local[index] || '',
            message: it.message || it.text || messages[index] || ''
          };
        });
      },
      previewMessages: function () {
        var d = this.previewData || {};
        var list = d.messages || d.sample_messages || [];
        if (!Array.isArray(list)) return [];
        // 去重后展示，避免同一条文案重复出现多次
        var seen = {};
        return list.filter(function (m) {
          var key = String(m);
          if (seen[key]) return false;
          seen[key] = true;
          return true;
        });
      },
      previewWarnings: function () {
        var list = (this.previewData && this.previewData.message_warnings) || [];
        return Array.isArray(list) ? list : [];
      },
      openRuns: function (row) {
        this.runsTask = row;
        this.runsPage = 1;
        this.showRuns = true;
        this.loadRuns();
      },
      loadRuns: function () {
        var self = this;
        if (!this.runsTask) return;
        this.runsLoading = true;
        this.runsError = '';
        this.api.page('/tasks/' + this.runsTask.id + '/runs', {
          page: this.runsPage, page_size: 10
        }, { silent: true }).then(function (res) {
          self.runs = res.items;
          self.runsPages = res.pages;
          self.runsTotal = res.total;
        }).catch(function (err) {
          self.runsError = err.message || '执行记录加载失败';
        }).then(function () { self.runsLoading = false; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除定时任务',
          message: '确定删除任务「' + (row.name || ('#' + row.id)) + '」吗？\n' +
            '删除后不再自动执行，历史执行记录会保留。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          return self.api.del('/tasks/' + row.id);
        }).then(function (res) {
          if (res === null) return;
          self.succeed('任务已删除');
          self.load();
        }).catch(function () { /* api 层已提示 */ });
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

  /* --------------------------- 新建 / 编辑 --------------------------- */
  S.defineView('view-task-edit', '/assets/views/task-edit.html', {
    data: function () {
      return {
        id: null,
        loading: false, error: '', accounts: [], friends: [], friendsLoading: false, friendsError: '',
        submitting: false, formError: '',
        form: {
          account_id: '', name: '自动续火花', task_type: 'spark', enabled: true,
          time_of_day: '08:30', repeat_mode: 'daily', weekdays: [], interval_minutes: 1440,
          jitter_seconds: 300, timezone: '', friend_mode: 'all', friend_ids: [],
          messages_text: DEFAULT_MESSAGES.join('\n'), gap_min: 3, gap_max: 9,
          daily_limit: 1, max_retry: 2, cron_expr: ''
        },
        showAdvanced: false
      };
    },
    computed: {
      isEdit: function () { return !!this.id; },
      repeatOptions: function () { return this.OPTIONS.repeat_mode; },
      friendModeOptions: function () { return this.OPTIONS.friend_mode; },
      timezoneOptions: function () {
        var list = this.OPTIONS.timezones.slice();
        var tz = this.tz || Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (tz && list.indexOf(tz) < 0) list.unshift(tz);
        return list;
      },
      messageList: function () { return this.fmt.lines(this.form.messages_text); },
      allFriendsSelected: function () {
        return this.friends.length > 0 && this.form.friend_ids.length === this.friends.length;
      }
    },
    created: function () {
      var self = this;
      this.id = this.route.params.id || null;
      this.form.timezone = this.tz || '';
      this.loadAccounts().then(function () {
        if (self.id) self.loadTask();
        else if (self.route.query.account_id) self.form.account_id = self.route.query.account_id;
      });
    },
    methods: {
      loadAccounts: function () {
        var self = this;
        return this.api.page('/douyin/accounts', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) {
            self.accounts = res.items;
            if (!self.form.account_id && res.items.length === 1) self.form.account_id = res.items[0].id;
          })
          .catch(function (err) { self.error = err.message || '账号列表加载失败'; });
      },
      loadTask: function () {
        var self = this;
        this.loading = true;
        return this.api.get('/tasks/' + this.id, null, { silent: true }).then(function (data) {
          var t = (data && (data.task || data)) || {};
          var gap = t.send_gap_seconds || {};
          if (typeof gap === 'string') { try { gap = JSON.parse(gap); } catch (e) { gap = {}; } }
          self.form = {
            account_id: t.account_id || '',
            name: t.name || '自动续火花',
            task_type: t.task_type || 'spark',
            enabled: t.enabled === undefined ? true : !!t.enabled,
            time_of_day: t.time_of_day || '08:30',
            repeat_mode: t.repeat_mode || 'daily',
            weekdays: (t.weekdays || []).map(String),
            interval_minutes: t.interval_minutes || 1440,
            jitter_seconds: t.jitter_seconds === undefined ? 300 : t.jitter_seconds,
            timezone: t.timezone || self.tz || '',
            friend_mode: t.friend_mode || 'all',
            friend_ids: (t.friend_ids || []).map(Number),
            messages_text: (t.messages || []).join('\n') || DEFAULT_MESSAGES.join('\n'),
            gap_min: gap.min === undefined ? 3 : gap.min,
            gap_max: gap.max === undefined ? 9 : gap.max,
            daily_limit: t.daily_limit === undefined ? 1 : t.daily_limit,
            max_retry: t.max_retry === undefined ? 2 : t.max_retry,
            cron_expr: t.cron_expr || ''
          };
          self.showAdvanced = !!t.cron_expr;
          if (self.form.account_id) self.loadFriends();
        }).catch(function (err) {
          self.error = err.message || '任务详情加载失败';
        }).then(function () { self.loading = false; });
      },
      loadFriends: function () {
        var self = this;
        if (!this.form.account_id) { this.friends = []; return; }
        this.friendsLoading = true;
        this.friends = [];
        // ⚠️ 接口 page_size 上限是 200，超过直接 400。旧实现要 500 → 后端拒绝 →
        // catch 把 friends 清空 → 界面显示「该账号还没有好友」，其实好友是存在的。
        // 现在按 200 一页循环，把全量拉回来。
        var PAGE_SIZE = 200;
        var collected = [];
        var fetchPage = function (page) {
          return self.api.page('/friends', {
            account_id: self.form.account_id, page: page, page_size: PAGE_SIZE
          }, { silent: true }).then(function (res) {
            var items = (res && res.items) || [];
            collected = collected.concat(items);
            var pages = Number((res && res.pages) || 1);
            if (items.length && page < pages) return fetchPage(page + 1);
            return collected;
          });
        };
        fetchPage(1)
          .then(function (items) {
            self.friends = items;
            self.friendsError = '';
            // 已删除的好友 id 要清理，避免提交不存在的 id
            var valid = items.map(function (f) { return Number(f.id); });
            self.form.friend_ids = (self.form.friend_ids || []).filter(function (id) {
              return valid.indexOf(id) >= 0;
            });
          })
          .catch(function (err) {
            // 不要把失败当「没有好友」：以前 catch 里静默清空，界面显示
            // 「该账号还没有好友」，其实是接口报错（例如 page_size 超限 400）。
            self.friends = [];
            self.friendsError = (err && err.message) || '好友列表加载失败，请刷新重试';
          })
          .then(function () { self.friendsLoading = false; });
      },
      toggleAllFriends: function () {
        if (this.allFriendsSelected) this.form.friend_ids = [];
        else this.form.friend_ids = this.friends.map(function (f) { return Number(f.id); });
      },
      resetMessages: function () {
        this.form.messages_text = DEFAULT_MESSAGES.join('\n');
        this.ui.info('已恢复默认文案');
      },
      validate: function () {
        var f = this.form;
        if (!f.account_id) return '请选择要托管的抖音账号';
        if (!f.name.trim()) return '请填写任务名称';
        if (f.repeat_mode !== 'interval' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(f.time_of_day || '')) {
          return '执行时间格式应为 HH:MM，例如 08:30';
        }
        if (f.repeat_mode === 'interval' && (!f.interval_minutes || f.interval_minutes < 5)) {
          return '固定间隔至少 5 分钟';
        }
        if (f.repeat_mode === 'custom' && !f.weekdays.length) return '自定义重复至少要选择一天';
        if (f.friend_mode === 'selected' && !f.friend_ids.length) return '已选择「指定好友」，请至少勾选一个好友';
        if (!this.messageList.length) return '请至少填写一条候选文案';
        if (Number(f.gap_min) > Number(f.gap_max)) return '发送间隔的最小值不能大于最大值';
        if (Number(f.gap_min) < 1) return '发送间隔至少 1 秒';
        if (!f.daily_limit || f.daily_limit < 1) return '每天上限至少为 1';
        return '';
      },
      payload: function () {
        var f = this.form;
        return {
          account_id: parseInt(f.account_id, 10),
          name: f.name.trim(),
          task_type: f.task_type,
          enabled: !!f.enabled,
          time_of_day: f.time_of_day,
          repeat_mode: f.repeat_mode,
          weekdays: f.repeat_mode === 'custom' ? f.weekdays.map(Number) : [],
          interval_minutes: parseInt(f.interval_minutes, 10) || 1440,
          jitter_seconds: parseInt(f.jitter_seconds, 10) || 0,
          timezone: f.timezone || this.tz || 'Asia/Shanghai',
          friend_mode: f.friend_mode,
          friend_ids: f.friend_mode === 'selected' ? f.friend_ids.map(Number) : [],
          messages: this.messageList,
          send_gap_seconds: { min: parseInt(f.gap_min, 10) || 3, max: parseInt(f.gap_max, 10) || 9 },
          daily_limit: parseInt(f.daily_limit, 10) || 1,
          max_retry: parseInt(f.max_retry, 10) || 0,
          cron_expr: this.showAdvanced ? (f.cron_expr || '') : ''
        };
      },
      submit: function () {
        var self = this;
        if (this.submitting) return;
        var problem = this.validate();
        if (problem) { this.formError = problem; return; }
        this.formError = '';
        this.submitting = true;
        var body = this.payload();
        var job = this.isEdit
          ? this.api.put('/tasks/' + this.id, body)
          : this.api.post('/tasks', body);
        job.then(function () {
          self.succeed(self.isEdit ? '任务已保存' : '任务已创建');
          self.nav.go('/tasks');
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      }
    }
  });
})();
