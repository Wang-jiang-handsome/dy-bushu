/* ==========================================================================
   views-admin-money.js —— 卡密管理 + 套餐管理 + 订单管理（管理端）
   模板片段：/assets/views/admin-cards.html、admin-packages.html、admin-orders.html
   接口见 docs/API_CONTRACT.md §7（以下字段均已实测）：
     生成：POST /admin/card-keys/generate {package_code,card_type,days,count,use_limit,amount_cents,note,batch_no,prefix}
           → {batch_id,batch_no,count,codes[],package_code,package_name,card_type,days,use_limit,
              amount_cents,expire_at,benefit_text}
     列表：GET /admin/card-keys → items[{id,code,batch_no,card_type,days,use_limit,used_count,
           amount_cents,status,package_id,package_name,bound_username,used_by_username,used_at,
           expire_at,note,created_at,benefit_text}]
     统计：GET /admin/card-keys/stats → {total,used,unused,disabled,expired,balance_amount_cents,
           batches[{batch_no,card_type,days,use_limit,amount_cents,quantity,used_count,
           disabled_count,package_name,note,created_at}]}
     导出：POST /admin/card-keys/export {batch_no,format} → data{filename,content,content_type,count}
     启停：POST /admin/card-keys/<card_id>/status {status}
     删除：DELETE /admin/card-keys/<card_id>
     套餐：GET/POST /admin/packages、PUT/DELETE /admin/packages/<package_id>
     订单：GET /admin/orders（items + summary{total,revenue_cents}）、
           POST /admin/orders/<order_no>/confirm、POST /admin/orders/<order_no>/refund {remark}
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;

  S.defineView('view-admin-cards', '/assets/views/admin-cards.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { batch_no: '', status: '', package_id: '', keyword: '' },
        stats: null, statsError: '',
        packages: [],
        showGen: false, generating: false, genError: '', genResult: null,
        gen: {
          package_code: 'monthly', card_type: 'time', days: 30, count: 50,
          use_limit: 1, amount_yuan: '0', note: '', prefix: '', batch_no: ''
        },
        acting: 0, exporting: false, showBatch: false, batchDetail: null,
        showCodes: false, codeText: ''
      };
    },
    computed: {
      statusOptions: function () {
        return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.card_status);
      },
      activePackages: function () {
        return this.packages.filter(function (p) { return p.is_active !== false; });
      },
      genAmountCents: function () { return this.fmt.toCents(this.gen.amount_yuan); },
      genPreview: function () {
        var g = this.gen;
        var kind = this.statusText('cardType', g.card_type);
        var benefit = g.card_type === 'balance'
          ? ('余额 ' + this.money(this.genAmountCents))
          : (g.card_type === 'count'
            ? ('可用 ' + (g.use_limit || 1) + ' 次')
            : ((g.days || 30) + ' 天套餐'));
        return kind + ' · ' + benefit + ' · 共 ' + (g.count || 0) + ' 张';
      }
    },
    created: function () { this.loadStats(); this.loadPackages(); this.load(); },
    methods: {
      loadStats: function () {
        var self = this;
        this.statsError = '';
        return this.api.get('/admin/card-keys/stats', null, { silent: true })
          .then(function (data) { self.stats = data || {}; })
          .catch(function (err) { self.statsError = err.message || '统计加载失败'; });
      },
      loadPackages: function () {
        var self = this;
        return this.api.page('/admin/packages', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) {
            self.packages = res.items;
            if (res.items.length && !self.gen.package_code) self.gen.package_code = res.items[0].code;
          })
          .catch(function () { self.packages = []; });
      },
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/card-keys', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
        }).catch(function (err) {
          self.error = err.message || '卡密列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () {
        this.filters = { batch_no: '', status: '', package_id: '', keyword: '' };
        this.search();
      },
      filterByBatch: function (batchNo) {
        this.filters.batch_no = batchNo;
        this.search();
      },
      openGen: function () {
        this.genError = '';
        this.genResult = null;
        if (this.activePackages.length && !this.gen.package_code) {
          this.gen.package_code = this.activePackages[0].code;
        }
        this.showGen = true;
      },
      generate: function () {
        var self = this;
        if (this.generating) return;
        var g = this.gen;
        if (!g.count || g.count < 1 || g.count > 5000) { this.genError = '生成数量需在 1～5000 之间'; return; }
        if (g.card_type === 'time' && (!g.days || g.days < 1)) { this.genError = '时长卡需要填写有效天数'; return; }
        if (g.card_type === 'count' && (!g.use_limit || g.use_limit < 1)) { this.genError = '次数卡需要填写可用次数'; return; }
        if (g.card_type === 'balance' && this.genAmountCents <= 0) { this.genError = '余额卡需要填写大于 0 的面额'; return; }
        if (!g.package_code && g.card_type !== 'balance') { this.genError = '请选择关联套餐'; return; }
        this.generating = true;
        this.genError = '';
        this.api.post('/admin/card-keys/generate', {
          package_code: g.package_code,
          card_type: g.card_type,
          days: parseInt(g.days, 10) || 30,
          count: parseInt(g.count, 10),
          use_limit: parseInt(g.use_limit, 10) || 1,
          amount_cents: this.genAmountCents,
          note: g.note,
          batch_no: g.batch_no || '',
          prefix: g.prefix || ''
        }).then(function (data) {
          self.genResult = data || {};
          self.succeed('已生成 ' + ((data && data.count) || 0) + ' 张卡密（批次 ' +
            ((data && data.batch_no) || '') + '）');
          self.loadStats();
          self.load();
        }).catch(function (err) {
          self.genError = err.message || '生成失败';
        }).then(function () { self.generating = false; });
      },
      copyAllCodes: function () {
        var codes = (this.genResult && this.genResult.codes) || [];
        if (!codes.length) { this.ui.warn('没有可复制的卡密'); return; }
        this.codeText = codes.join('\n');
        this.showCodes = true;
      },
      toggle: function (row) {
        var self = this;
        var next = row.status === 'disabled' ? 'unused' : 'disabled';
        this.acting = row.id;
        this.api.post('/admin/card-keys/' + row.id + '/status', { status: next })
          .then(function () {
            self.succeed(next === 'disabled' ? '卡密已停用' : '卡密已启用');
            self.load();
            self.loadStats();
          })
          .catch(function () { /* api 层已提示 */ })
          .then(function () { self.acting = 0; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除卡密',
          message: '确定删除卡密 ' + row.code + ' 吗？\n只有未使用的卡密可以删除，删除后不可恢复。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.del('/admin/card-keys/' + row.id);
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('卡密已删除');
          self.load();
          self.loadStats();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },
      exportCards: function (batchNo) {
        var self = this;
        this.exporting = true;
        this.api.post('/admin/card-keys/export', {
          batch_no: batchNo || this.filters.batch_no || '',
          status: batchNo ? '' : this.filters.status,
          format: 'csv'
        }).then(function (data) {
          var d = data || {};
          var content = d.content || d.text || '';
          if (!content && Array.isArray(d.codes)) content = d.codes.join('\n');
          if (!content) { self.ui.warn('导出内容为空'); return; }
          var filename = d.filename || ('卡密导出_' + (batchNo || 'all') + '.csv');
          self.api.downloadText(filename, content, d.content_type || 'text/csv');
          self.succeed('已导出 ' + (d.count || '') + ' 张卡密：' + filename);
        }).catch(function () { /* api 层已提示 */ })
          .then(function () { self.exporting = false; });
      },
      openBatch: function (batch) {
        this.batchDetail = batch;
        this.showBatch = true;
      }
    }
  });

  /* --------------------------- 套餐管理 --------------------------- */
  S.defineView('view-admin-packages', '/assets/views/admin-packages.html', {
    data: function () {
      return {
        loading: false, error: '', items: [],
        showForm: false, editing: null, submitting: false, formError: '',
        form: {
          code: '', name: '', description: '', days: 30,
          price_yuan: '19.00', original_yuan: '', account_limit: 2, daily_task_limit: 3,
          features_text: '', sort_order: 0, is_active: true
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
        return this.api.page('/admin/packages', { page: 1, page_size: 100 }, { silent: true })
          .then(function (res) { self.items = res.items; })
          .catch(function (err) { self.error = err.message || '套餐列表加载失败'; })
          .then(function () { self.loading = false; });
      },
      openCreate: function () {
        this.editing = null;
        this.form = {
          code: '', name: '', description: '', days: 30, price_yuan: '19.00', original_yuan: '',
          account_limit: 2, daily_task_limit: 3, features_text: '', sort_order: 0, is_active: true
        };
        this.formError = '';
        this.showForm = true;
      },
      openEdit: function (row) {
        this.editing = row;
        var features = row.features || [];
        if (!Array.isArray(features)) {
          try { features = JSON.parse(features); } catch (e) { features = [String(features)]; }
        }
        this.form = {
          code: row.code || '', name: row.name || '', description: row.description || '',
          days: row.days || 30,
          price_yuan: this.fmt.yuan(row.price_cents),
          original_yuan: row.original_cents ? this.fmt.yuan(row.original_cents) : '',
          account_limit: row.account_limit || 1, daily_task_limit: row.daily_task_limit || 1,
          features_text: (features || []).join('\n'),
          sort_order: row.sort_order || 0,
          is_active: row.is_active === undefined ? true : !!row.is_active
        };
        this.formError = '';
        this.showForm = true;
      },
      useTemplate: function (kind) {
        var templates = {
          trial: { code: 'trial', name: '体验卡', days: 7, price_yuan: '0', account_limit: 1, daily_task_limit: 1, description: '7 天体验，1 个账号，每天 1 次', features_text: '1 个抖音账号\n每天 1 次自动续火花\n保留 7 天执行记录' },
          monthly: { code: 'monthly', name: '月卡', days: 30, price_yuan: '19.00', account_limit: 2, daily_task_limit: 3, description: '30 天，2 个账号，每天 3 次', features_text: '2 个抖音账号\n每天 3 次自动续火花\n完整执行记录与截图\n失败自动重试' },
          quarter: { code: 'quarter', name: '季卡', days: 90, price_yuan: '49.00', account_limit: 5, daily_task_limit: 6, description: '90 天，5 个账号，每天 6 次', features_text: '5 个抖音账号\n每天 6 次自动续火花\n完整执行记录与截图\n失败自动重试\n随机文案与发送间隔' },
          yearly: { code: 'yearly', name: '年卡', days: 365, price_yuan: '159.00', account_limit: 10, daily_task_limit: 12, description: '365 天，10 个账号，每天 12 次', features_text: '10 个抖音账号\n每天 12 次自动续火花\n完整执行记录与截图\n失败自动重试\n随机文案与发送间隔\n优先技术支持' }
        };
        var t = templates[kind];
        if (!t) return;
        Object.assign(this.form, t);
        this.ui.info('已套用「' + t.name + '」模板，可继续修改');
      },
      save: function () {
        var self = this;
        if (this.submitting) return;
        var f = this.form;
        if (!/^[a-z][a-z0-9_]{1,19}$/.test(f.code)) {
          this.formError = '套餐标识需为 2-20 位小写字母、数字或下划线，且以字母开头（如 monthly）';
          return;
        }
        if (!f.name.trim()) { this.formError = '请填写套餐名称'; return; }
        if (!f.days || f.days < 1) { this.formError = '有效天数至少为 1'; return; }
        this.submitting = true;
        this.formError = '';
        var body = {
          code: f.code.trim(), name: f.name.trim(), description: f.description.trim(),
          days: parseInt(f.days, 10),
          price_cents: this.fmt.toCents(f.price_yuan),
          original_cents: f.original_yuan ? this.fmt.toCents(f.original_yuan) : 0,
          account_limit: parseInt(f.account_limit, 10) || 1,
          daily_task_limit: parseInt(f.daily_task_limit, 10) || 1,
          features: this.fmt.lines(f.features_text),
          sort_order: parseInt(f.sort_order, 10) || 0,
          is_active: !!f.is_active
        };
        var job = this.editing
          ? this.api.put('/admin/packages/' + this.editing.id, body)
          : this.api.post('/admin/packages', body);
        job.then(function () {
          self.showForm = false;
          self.succeed(self.editing ? '套餐已保存' : '套餐已创建');
          self.load();
        }).catch(function (err) {
          self.formError = err.message || '保存失败';
        }).then(function () { self.submitting = false; });
      },
      remove: function (row) {
        var self = this;
        this.ui.confirm({
          title: '删除套餐',
          message: '确定删除套餐「' + row.name + '」吗？\n已经产生订单的套餐不会被真正删除，系统会改为「停用」（下架）。',
          confirmText: '确认删除', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = row.id;
          return self.api.del('/admin/packages/' + row.id);
        }).then(function (res) {
          if (res === null) { self.acting = 0; return; }
          self.succeed('已处理删除请求');
          self.load();
          self.acting = 0;
        }).catch(function () { self.acting = 0; });
      },
      toggleActive: function (row) {
        var self = this;
        this.acting = row.id;
        this.api.put('/admin/packages/' + row.id, { is_active: !row.is_active })
          .then(function () {
            self.succeed(row.is_active ? '套餐已下架' : '套餐已上架');
            self.load();
          })
          .catch(function () { /* api 层已提示 */ })
          .then(function () { self.acting = 0; });
      }
    }
  });

  /* --------------------------- 订单管理 --------------------------- */
  S.defineView('view-admin-orders', '/assets/views/admin-orders.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { status: '', channel: '', keyword: '', start_date: '', end_date: '' },
        summary: null,
        showDetail: false, detailLoading: false, detail: null, detailError: '',
        acting: false, revenueTotal: 0
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.order_status); },
      channelOptions: function () { return [{ value: '', label: '全部渠道' }].concat(this.OPTIONS.channels); },
      pageRevenue: function () {
        var sum = 0;
        this.items.forEach(function (row) {
          if (row.status === 'paid') sum += (row.payable_cents || row.amount_cents || 0);
        });
        return sum;
      }
    },
    created: function () { this.load(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/admin/orders', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
          self.summary = res.summary;
        }).catch(function (err) {
          self.error = err.message || '订单列表加载失败';
        }).then(function () { self.loading = false; });
      },
      search: function () { this.page = 1; this.load(); },
      reset: function () {
        this.filters = { status: '', channel: '', keyword: '', start_date: '', end_date: '' };
        this.search();
      },
      amountOf: function (row) {
        if (row.payable_cents !== undefined && row.payable_cents !== null) return row.payable_cents;
        return row.amount_cents;
      },
      openDetail: function (row) {
        var self = this;
        this.showDetail = true;
        this.detail = row;
        this.detailError = '';
        this.detailLoading = true;
        // 管理端没有单条订单详情接口，用列表行数据 + 用户信息兜底展示
        this.api.get('/admin/orders', Object.assign({ keyword: row.order_no, page: 1, page_size: 1 }, {}), { silent: true })
          .then(function (data) {
            var items = (data && data.items) || [];
            if (items.length) self.detail = items[0];
          })
          .catch(function (err) { self.detailError = err.message || ''; })
          .then(function () { self.detailLoading = false; });
      },
      confirmPaid: function (row) {
        var self = this;
        this.ui.confirm({
          title: '人工确认收款',
          message: '确认订单 ' + row.order_no + '（' + this.money(this.amountOf(row)) + '）已经收到款项？\n' +
            '确认后会写入资金流水并立即为用户开通/续期套餐，请先核对到账记录。',
          confirmText: '确认已收款', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          return self.api.post('/admin/orders/' + row.order_no + '/confirm', {});
        }).then(function (res) {
          if (res === null) { self.acting = false; return; }
          self.succeed('已确认收款，套餐已开通');
          self.showDetail = false;
          self.load();
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      refund: function (row) {
        var self = this;
        this.ui.prompt({
          title: '标记退款',
          message: '该操作只在系统内做退款记录，不会自动调用支付通道退款。\n请先在支付平台完成实际退款，再填写备注。',
          inputLabel: '退款备注（必填）',
          inputPlaceholder: '例如：用户申请退款，已在支付宝原路退回',
          inputRequired: true,
          confirmText: '标记为已退款'
        }).then(function (remark) {
          if (remark === null) return null;
          self.acting = true;
          return self.api.post('/admin/orders/' + row.order_no + '/refund', { remark: remark });
        }).then(function (res) {
          if (res === null) { self.acting = false; return; }
          self.succeed('已标记退款');
          self.showDetail = false;
          self.load();
          self.acting = false;
        }).catch(function () { self.acting = false; });
      }
    }
  });
})();
