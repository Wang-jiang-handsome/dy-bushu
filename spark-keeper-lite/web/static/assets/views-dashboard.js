/* ==========================================================================
   views-dashboard.js —— 用户仪表盘
   模板片段：/assets/views/dashboard.html
   接口：GET /user/dashboard（契约 §3）、GET /user/packages/limit-status（轻量配额）
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  S.defineView('view-dashboard', '/assets/views/dashboard.html', {
    data: function () {
      return {
        loading: false,
        error: '',
        data: null,
        limit: null,
        running: false
      };
    },
    computed: {
      accounts: function () { return (this.data && this.data.accounts) || {}; },
      tasks: function () { return (this.data && this.data.tasks) || {}; },
      today: function () { return (this.data && this.data.today) || {}; },
      trend: function () { return (this.data && this.data.trend) || []; },
      recent: function () { return (this.data && this.data.recent_runs) || []; },
      quotaInfo: function () {
        var q = (this.data && this.data.quota) || this.limit || this.quota || {};
        return Object.assign({
          active: false, package_name: '', expire_text: '', account_limit: 0, account_used: 0,
          daily_task_limit: 0, daily_task_used: 0
        }, q);
      },
      trendMax: function () {
        var max = 1;
        this.trend.forEach(function (d) {
          var v = (d.success || 0) + (d.failed || 0);
          if (v > max) max = v;
        });
        return max;
      },
      successRate: function () {
        var s = this.today.success || 0;
        var f = this.today.failed || 0;
        if (!s && !f) return 0;
        return Math.round(s * 100 / (s + f));
      },
      accountPct: function () {
        var q = this.quotaInfo;
        if (!q.account_limit) return 0;
        return Math.min(100, Math.round((q.account_used || 0) * 100 / q.account_limit));
      },
      taskPct: function () {
        var q = this.quotaInfo;
        if (!q.daily_task_limit) return 0;
        return Math.min(100, Math.round((q.daily_task_used || 0) * 100 / q.daily_task_limit));
      }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/user/dashboard', null, { silent: true }).then(function (data) {
          self.data = data || {};
          self.limit = (data && data.quota) || null;
        }).catch(function (err) {
          self.error = err.message || '仪表盘加载失败';
        }).then(function () { self.loading = false; });
      },
      barHeight: function (row, key) {
        var v = row[key] || 0;
        if (!v) return '0%';
        return Math.max(4, Math.round(v * 100 / this.trendMax)) + '%';
      },
      runAll: function () {
        this.nav.go('/tasks');
      }
    }
  });
})();
