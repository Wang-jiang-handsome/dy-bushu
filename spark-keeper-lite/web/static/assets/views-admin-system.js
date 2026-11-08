/* ==========================================================================
   views-admin-system.js —— 日志 / 公告 / 站点配置 / 代理 / 系统环境（管理端）
   模板片段：/assets/views/admin-logs.html、admin-announcements.html、
             admin-configs.html、admin-proxies.html、admin-system.html
   接口见 docs/API_CONTRACT.md §7（字段均已实测）：
     GET /admin/logs → items[{id,scope,level,user_id,source,message,context,created_at,username}]
     GET /admin/logs/admin → items[{id,user_id,username,scope,action,target,detail,ip,user_agent,created_at}]
     DELETE /admin/logs（按天数清理）
     公告：GET/POST /admin/announcements、PUT/DELETE /admin/announcements/<id>
     配置：GET /admin/configs → {items[{key,value,masked,is_public,group,description,updated_at}],groups,values}
           PUT /admin/configs **必须用 {values:{key:value}}**（传 items 会 400「没有要保存的配置项」）
     代理：GET/POST /admin/proxies、PUT/DELETE /admin/proxies/<id>、
           POST /admin/proxies/<id>/check → {id,ok,latency_ms,message,host}
     系统：GET /admin/system-environment、GET /admin/browser-processes、
           POST /admin/system/browser-processes/cleanup、GET/POST /admin/test-email、
           GET/POST /admin/test-ai、GET /admin/update、GET /admin/license1
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  var GROUP_LABELS = {
    general: '站点信息',
    auth: '注册与登录',
    payment: '支付设置',
    task: '任务默认值',
    smtp: '邮件（SMTP）',
    mail: '邮件（SMTP）',
    sms: '短信通道',
    ai: 'AI 文案',
    system: '系统',
    security: '安全',
    other: '其它'
  };

  function groupLabel(name) {
    return GROUP_LABELS[name] || (name || '其它');
  }

  S.defineView('view-admin-logs', '/assets/views/admin-logs.html', {
    data: function () {
      return {
        tab: 'run',
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { scope: '', level: '', keyword: '', source: '', start_date: '', end_date: '' },
        audit: { loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0, keyword: '' },
        expanded: {}, pruning: false
      };
    },
    computed: {
      levelOptions: function () { return this.OPTIONS.levels; },
      scopeOptions: function () { return this.OPTIONS.scopes; }
    },
    created: function () { this.load(); },
    methods: {
      switchTab: function (tab) {
        this.tab = tab;
        if (tab === 'audit' && !this.audit.items.length) this.loadAudit();
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/logs', Object.assign({
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
        this.filters = { scope: '', level: '', keyword: '', source: '', start_date: '', end_date: '' };
        this.search();
      },
      loadAudit: function () {
        var self = this;
        this.audit.loading = true;
        this.audit.error = '';
        return this.api.page('/admin/logs/admin', {
          page: this.audit.page, page_size: this.audit.pageSize, keyword: this.audit.keyword
        }, { silent: true }).then(function (res) {
          self.audit.items = res.items;
          self.audit.total = res.total;
          self.audit.pages = res.pages;
        }).catch(function (err) {
          self.audit.error = err.message || '审计日志加载失败';
        }).then(function () { self.audit.loading = false; });
      },
      toggle: function (row) { this.expanded[row.id] = !this.expanded[row.id]; },
      contextText: function (row) {
        var raw = row.context;
        if (!raw) return '';
        if (typeof raw === 'object') return JSON.stringify(raw, null, 2);
        try { return JSON.stringify(JSON.parse(String(raw)), null, 2); } catch (e) { return String(raw); }
      },
      prune: function () {
        var self = this;
        this.ui.prompt({
          title: '清理历史日志',
          message: '删除指定天数之前的运行日志与审计日志，用于控制数据库体积。',
          inputLabel: '保留最近多少天',
          inputPlaceholder: '例如 30',
          inputValue: '30',
          inputRequired: true,
          confirmText: '下一步'
        }).then(function (value) {
          if (value === null) return null;
          var days = parseInt(value, 10);
          if (!days || days < 1) { self.ui.warn('请输入大于 0 的天数'); return null; }
          return self.ui.confirm({
            title: '确认清理',
            message: '将永久删除 ' + days + ' 天之前的日志，操作不可撤销。',
            confirmText: '确认清理', danger: true
          }).then(function (ok) {
            if (!ok) return null;
            self.pruning = true;
            return self.api.del('/admin/logs', { days: days });
          });
        }).then(function (res) {
          if (res === null) { self.pruning = false; return; }
          var removed = res && (res.deleted !== undefined ? res.deleted : res.count);
          self.succeed('日志清理完成' + (removed !== undefined ? '（删除 ' + removed + ' 条）' : ''));
          self.load();
          self.audit.items = [];
          self.pruning = false;
        }).catch(function () { self.pruning = false; });
      }
    }
  });

  /* --------------------------- 公告管理 --------------------------- */
  S.defineView('view-admin-announcements', '/assets/views/admin-announcements.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        showForm: false, editing: null, submitting: false, formError: '',
        form: {
          title: '', content: '', level: 'info', is_pinned: false, is_active: true,
          publish_at: '', expire_at: ''
        },
        acting: 0
      };
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/announcements', {
          page: this.page, page_size: this.pageSize
        }, { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
        }).catch(function (err) {
          self.error = err.message || '公告列表加载失败';
        }).then(function () { self.loading = false; });
      },
      openCreate: function () {
        this.editing = null;
        this.form = {
          title: '', content: '', level: 'info', is_pinned: false, is_active: true,
          publish_at: '', expire_at: ''
        };
        this.formError = '';
        this.showForm = true;
      },
      openEdit: function (row) {
        this.editing = row;
        this.form = {
          title: row.title || '', content: row.content || '', level: row.level || 'info',
          is_pinned: !!row.is_pinned, is_active: row.is_active === undefined ? true : !!row.is_active,
          publish_at: row.publish_at || '', expire_at: row.expire_at || ''
        };
        this.formError = '';
        this.showForm = true;
      },
      save: function () {
        var self = this;
        if (this.submitting) return;
        if (!this.form.title.trim()) { this.formError = '请填写公告标题'; return; }
        if (!this.form.content.trim()) { this.formError = '请填写公告内容'; return; }
        this.submitting = true;
        this.formError = '';
        var body = {
          title: this.form.title.trim(), content: this.form.content.trim(),
          level: this.form.level, is_pinned: !!this.form.is_pinned,
          is_active: !!this.form.is_active,
          publish_at: this.form.publish_at || null, expire_at: this.form.expire_at || null
        };
        var job = this.editing
          ? this.api.put('/admin/announcements/' + this.editing.id, body)
          : this.api.post('/admin/announcements', body);
        job.then(function () {
          self.showForm = false;
          self.succeed(self.editing ? '公告已更新' : '公告已发布');
          self.load();
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除公告',
          message: '确定删除公告「' + row.title + '」吗？删除后用户端将不再显示。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.del('/admin/announcements/' + row.id);
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('公告已删除');
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },
      toggleActive: function (row) {
        var self = this;
        this.acting = row.id;
        this.api.put('/admin/announcements/' + row.id, {
          title: row.title, content: row.content, level: row.level,
          is_pinned: !!row.is_pinned, is_active: !row.is_active,
          publish_at: row.publish_at || null, expire_at: row.expire_at || null
        }).then(function () {
          self.succeed(row.is_active ? '公告已隐藏' : '公告已显示');
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.acting = 0; });
      }
    }
  });

  /* --------------------------- 站点配置 --------------------------- */
  S.defineView('view-admin-configs', '/assets/views/admin-configs.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], values: {}, original: {},
        groups: [], saving: false, saveError: '', dirtyKeys: []
      };
    },
    computed: {
      grouped: function () {
        var map = {};
        this.items.forEach(function (item) {
          var g = item.group || 'other';
          if (!map[g]) map[g] = [];
          map[g].push(item);
        });
        var order = this.groups.length ? this.groups : Object.keys(map);
        var self = this;
        return order.filter(function (g) { return map[g] && map[g].length; })
          .map(function (g) { return { name: g, label: groupLabel(g), items: map[g] }; });
      },
      dirtyCount: function () { return this.dirtyKeys.length; }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/admin/configs', null, { silent: true }).then(function (data) {
          var d = data || {};
          var items = d.items || [];
          self.items = items.map(function (item) {
            return Object.assign({}, item, {
              // 分组接口给的是 group，兼容 group_name
              group: item.group || item.group_name || 'other'
            });
          });
          self.groups = d.groups || [];
          var values = {};
          self.items.forEach(function (item) { values[item.key] = item.value; });
          self.values = values;
          self.original = Object.assign({}, values);
          self.dirtyKeys = [];
        }).catch(function (err) {
          self.error = err.message || '配置加载失败';
        }).then(function () { self.loading = false; });
      },
      isBool: function (item) {
        var v = String(this.values[item.key]);
        return (v === '0' || v === '1') && !/password|secret|key$/i.test(item.key);
      },
      isLongText: function (item) {
        return /announcement|notice|description|content|template/i.test(item.key);
      },
      isNumber: function (item) {
        return /(_limit|_minutes|_seconds|_count|_port|_days|_ttl|_size)$/i.test(item.key);
      },
      changed: function (item) {
        return String(this.values[item.key] === undefined ? '' : this.values[item.key]) !==
          String(this.original[item.key] === undefined ? '' : this.original[item.key]);
      },
      onInput: function (item) {
        var key = item.key;
        var list = this.dirtyKeys.slice();
        if (this.changed(item)) {
          if (list.indexOf(key) < 0) list.push(key);
        } else {
          list = list.filter(function (k) { return k !== key; });
        }
        this.dirtyKeys = list;
      },
      toggleBool: function (item) {
        this.values[item.key] = String(this.values[item.key]) === '1' ? '0' : '1';
        this.onInput(item);
      },
      revert: function () {
        this.values = Object.assign({}, this.original);
        this.dirtyKeys = [];
        this.ui.info('已还原为服务器上的当前配置');
      },
      save: function () {
        var self = this;
        if (this.saving) return;
        if (!this.dirtyKeys.length) { this.ui.info('没有需要保存的修改'); return; }
        // 掩码项（密码/密钥）为空表示「不修改」，不要提交空值覆盖
        var values = {};
        this.dirtyKeys.forEach(function (key) {
          var item = self.items.filter(function (i) { return i.key === key; })[0] || {};
          var value = self.values[key];
          if (item.masked && !String(value || '').trim()) return;
          values[key] = value === undefined || value === null ? '' : String(value);
        });
        if (!Object.keys(values).length) { this.ui.warn('没有可提交的有效修改'); return; }
        this.saving = true;
        this.saveError = '';
        this.ui.confirm({
          title: '保存站点配置',
          message: '将更新 ' + Object.keys(values).length + ' 项配置：\n' +
            Object.keys(values).map(function (k) {
              var item = self.items.filter(function (i) { return i.key === k; })[0] || {};
              return '· ' + (item.description || k);
            }).join('\n'),
          confirmText: '确认保存', danger: false
        }).then(function (ok) {
          if (!ok) { self.saving = false; return null; }
          // 实测：后端只认 {values:{key:value}}
          return self.api.put('/admin/configs', { values: values });
        }).then(function (res) {
          if (res === null) { self.saving = false; return; }
          var saved = res && res.saved;
          self.succeed('配置已保存' + (saved !== undefined ? '（' + saved + ' 项）' : ''));
          self.load();
          self.store.loadSite();
          self.saving = false;
        }).catch(function (err) {
          self.saveError = err.message || '保存失败';
          self.saving = false;
        });
      }
    }
  });

  /* --------------------------- 代理管理 --------------------------- */
  S.defineView('view-admin-proxies', '/assets/views/admin-proxies.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        showForm: false, editing: null, submitting: false, formError: '',
        form: {
          name: '', scheme: 'http', host: '', port: 8080, username: '', password: '',
          region: '', status: 'active', remark: ''
        },
        checking: {}, acting: 0
      };
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/proxies', {
          page: this.page, page_size: this.pageSize
        }, { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
        }).catch(function (err) {
          self.error = err.message || '代理列表加载失败';
        }).then(function () { self.loading = false; });
      },
      openCreate: function () {
        this.editing = null;
        this.form = {
          name: '', scheme: 'http', host: '', port: 8080, username: '', password: '',
          region: '', status: 'active', remark: ''
        };
        this.formError = '';
        this.showForm = true;
      },
      openEdit: function (row) {
        this.editing = row;
        this.form = {
          name: row.name || '', scheme: row.scheme || 'http', host: row.host || '',
          port: row.port || 8080, username: row.username || '', password: '',
          region: row.region || '', status: row.status || 'active', remark: row.remark || ''
        };
        this.formError = '';
        this.showForm = true;
      },
      save: function () {
        var self = this;
        if (this.submitting) return;
        if (!this.form.name.trim()) { this.formError = '请填写代理名称'; return; }
        if (!this.form.host.trim()) { this.formError = '请填写代理主机（IP 或域名）'; return; }
        var port = parseInt(this.form.port, 10);
        if (!port || port < 1 || port > 65535) { this.formError = '端口需在 1～65535 之间'; return; }
        this.submitting = true;
        this.formError = '';
        var body = {
          name: this.form.name.trim(), scheme: this.form.scheme, host: this.form.host.trim(),
          port: port, username: this.form.username.trim(), region: this.form.region.trim(),
          status: this.form.status, remark: this.form.remark.trim()
        };
        if (this.form.password) body.password = this.form.password;
        var job = this.editing
          ? this.api.put('/admin/proxies/' + this.editing.id, body)
          : this.api.post('/admin/proxies', body);
        job.then(function () {
          self.showForm = false;
          self.succeed(self.editing ? '代理已保存' : '代理已添加');
          self.load();
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      },
      check: function (row) {
        var self = this;
        this.checking[row.id] = true;
        this.api.post('/admin/proxies/' + row.id + '/check', {}).then(function (data) {
          var okFlag = data && data.ok;
          var latency = data && data.latency_ms;
          if (okFlag) self.succeed('代理可用' + (latency ? '（' + latency + ' ms）' : ''));
          else self.ui.warn('代理不可用：' + ((data && data.message) || '连接失败'));
          self.load();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.checking[row.id] = false; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除代理',
          message: '确定删除代理「' + row.name + '」吗？' +
            (row.account_count ? '\n当前有 ' + row.account_count + ' 个账号正在使用该代理，删除后它们会改为直连。' : ''),
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.del('/admin/proxies/' + row.id);
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('代理已删除');
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      }
    }
  });

  /* --------------------------- 系统环境 --------------------------- */
  S.defineView('view-admin-system', '/assets/views/admin-system.html', {
    data: function () {
      return {
        loading: false, error: '',
        env: null, procs: null, procsError: '',
        mail: null, mailError: '', mailTo: '', sendingMail: false,
        ai: null, aiError: '', testingAi: false,
        update: null, license: null,
        cleaning: false, tab: 'env'
      };
    },
    computed: {
      environment: function () { return (this.env && this.env.environment) || {}; },
      database: function () { return (this.env && this.env.database) || {}; },
      metrics: function () { return (this.env && this.env.metrics) || {}; },
      heartbeats: function () { return (this.env && this.env.heartbeats) || []; },
      counts: function () { return (this.env && this.env.counts) || {}; },
      settings: function () { return (this.env && this.env.settings) || {}; },
      procUsed: function () {
        if (!this.procs) return 0;
        return (this.procs.in_use !== undefined ? this.procs.in_use : (this.procs.active || 0));
      },
      procPct: function () {
        var limit = (this.procs && (this.procs.limit || this.procs.max)) || 0;
        if (!limit) return 0;
        return Math.min(100, Math.round(this.procUsed * 100 / limit));
      }
    },
    created: function () { this.load(); this.loadMail(); this.loadAi(); this.loadMisc(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/admin/system-environment', null, { silent: true }).then(function (data) {
          self.env = data || {};
        }).catch(function (err) {
          self.error = err.message || '环境体检加载失败';
        }).then(function () {
          self.loading = false;
          self.loadProcs();
        });
      },
      loadProcs: function () {
        var self = this;
        this.procsError = '';
        return this.api.get('/admin/browser-processes', null, { silent: true })
          .then(function (data) { self.procs = data || {}; })
          .catch(function (err) { self.procsError = err.message || '进程信息加载失败'; });
      },
      loadMail: function () {
        var self = this;
        this.mailError = '';
        return this.api.get('/admin/test-email', null, { silent: true })
          .then(function (data) { self.mail = data || {}; })
          .catch(function (err) { self.mailError = err.message || ''; });
      },
      loadAi: function () {
        var self = this;
        this.aiError = '';
        return this.api.get('/admin/test-ai', null, { silent: true })
          .then(function (data) { self.ai = data || {}; })
          .catch(function (err) { self.aiError = err.message || ''; });
      },
      loadMisc: function () {
        var self = this;
        this.api.get('/admin/update', null, { silent: true })
          .then(function (data) { self.update = data || {}; })
          .catch(function () { self.update = null; });
        this.api.get('/admin/license1', null, { silent: true })
          .then(function (data) { self.license = data || {}; })
          .catch(function () { self.license = null; });
      },
      cleanup: function () {
        var self = this;
        this.ui.confirm({
          title: '清理残留浏览器进程',
          message: '将回收没有被任务占用的浏览器实例与临时文件，用于解决「浏览器进程泄漏」导致的并发不足。\n' +
            '正在执行任务的浏览器不会被清理。',
          confirmText: '立即清理', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.cleaning = true;
          return self.api.post('/admin/system/browser-processes/cleanup', {});
        }).then(function (res) {
          if (res === null) { self.cleaning = false; return; }
          var killed = res && (res.cleaned !== undefined ? res.cleaned : res.killed);
          self.succeed('清理完成' + (killed !== undefined ? '（清理 ' + killed + ' 个）' : ''));
          self.loadProcs();
          self.cleaning = false;
        }).catch(function () { self.cleaning = false; });
      },
      sendTestMail: function () {
        var self = this;
        if (!this.mailTo.trim()) { this.ui.warn('请填写接收测试邮件的地址'); return; }
        this.sendingMail = true;
        this.api.post('/admin/test-email', { to: this.mailTo.trim() }).then(function (data) {
          self.succeed((data && data.message) || '测试邮件已发送，请查收');
          self.loadMail();
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.sendingMail = false; });
      },
      testAi: function () {
        var self = this;
        this.testingAi = true;
        this.api.post('/admin/test-ai', { prompt: '写一句续火花的招呼语' }).then(function (data) {
          self.ui.ok('AI 文案生成成功');
          self.ai = Object.assign({}, self.ai || {}, { last_result: (data && (data.text || data.content || data.result)) || '', message: '测试成功' });
        }).catch(function () { /* api 层已提示，未配置时会返回 400 与说明 */ })
          .then(function () { self.testingAi = false; });
      }
    }
  });
})();
