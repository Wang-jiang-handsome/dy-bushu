/* ==========================================================================
   views-billing.js —— 套餐购买 + 我的订单 + 卡密兑换（用户端）
   模板片段：/assets/views/packages.html、orders.html、card.html
   接口见 docs/API_CONTRACT.md §6：
     GET /packages、GET /user/packages、/user/packages/limit-info、
     /user/packages/limit-status、POST /user/packages/purchase、/user/packages/recharge、
     GET /user/packages/pay-methods、POST /user/card-keys/redeem、GET /user/card-keys、
     GET /user/orders、GET /user/orders/<order_no>、POST /user/orders/<order_no>/cancel、
     POST /user/orders/<order_no>/query
   规则：在线支付未启用（pay.enabled=0）时只给卡密渠道，不渲染任何假按钮。
   ========================================================================== */
(function () {
  'use strict';
  var S = window.Spark;
  var POLL_MS = 3000;          // 订单状态轮询间隔
  var POLL_MAX = 100;          // 最多轮询 100 次 ≈ 5 分钟

  function isTerminal(status) {
    return ['paid', 'failed', 'canceled', 'refunded', 'expired'].indexOf(String(status)) >= 0;
  }

  function isQrImage(value) {
    return typeof value === 'string' && value.indexOf('data:image') === 0;
  }

  S.defineView('view-packages', '/assets/views/packages.html', {
    data: function () {
      return {
        loading: false, error: '',
        packages: [], current: null, quota: null,
        payMethods: [], payMethodsError: '',
        methodChannel: 'card',
        // 下单
        buying: null, showPay: false, order: null, pollTimer: null, pollCount: 0,
        paying: false,
        // 充值
        showRecharge: false, rechargeYuan: '10', rechargeChannel: 'balance', recharging: false,
        rechargeOrder: null
      };
    },
    computed: {
      // 站点配置与支付方式接口都可能表达「在线支付是否开启」，以任一为真为准
      payEnabled: function () {
        if (this.onlinePayEnabled) return true;
        var m = this.payMethods || {};
        return !!m.online_pay_enabled || !!m.enabled;
      },
      enabledMethods: function () {
        var list = this.payMethods || [];
        if (!Array.isArray(list)) list = [];
        return list.filter(function (m) { return m && m.enabled !== false; });
      },
      currentQuota: function () {
        return this.quota || this.current || {};
      },
      accountPct: function () {
        var q = this.currentQuota;
        if (!q.account_limit) return 0;
        return Math.min(100, Math.round((q.account_used || 0) * 100 / q.account_limit));
      },
      taskPct: function () {
        var q = this.currentQuota;
        if (!q.daily_task_limit) return 0;
        return Math.min(100, Math.round((q.daily_task_used || 0) * 100 / q.daily_task_limit));
      },
      orderPaid: function () { return this.order && this.order.status === 'paid'; },
      orderQr: function () { return this.order && isQrImage(this.order.qr_code) ? this.order.qr_code : ''; },
      orderQrText: function () {
        if (!this.order || !this.order.qr_code || isQrImage(this.order.qr_code)) return '';
        return this.order.qr_code;
      }
    },
    created: function () { this.load(); },
    unmounted: function () { this.stopPoll(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.get('/user/packages', null, { silent: true }).then(function (data) {
          var d = data || {};
          var items = d.items || d.packages || d.list || [];
          self.packages = Array.isArray(items) ? items : [];
          self.current = d.current || d.my_package || d.current_package || d.package || null;
          self.quota = d.quota || d.limit || d.limits || null;
        }).catch(function (err) {
          // /user/packages 还没实现时退回公开的 /packages，至少让用户看到价格
          return self.api.get('/packages', null, { silent: true }).then(function (data) {
            var d = data || {};
            self.packages = d.items || (Array.isArray(d) ? d : []);
          }).catch(function () {
            self.error = err.message || '套餐加载失败';
          });
        }).then(function () {
          self.loadPayMethods();
          return self.api.get('/user/packages/limit-info', null, { silent: true }).then(function (data) {
            if (data) self.quota = data.quota || data;
          }).catch(function () { /* 配额详情拿不到不影响主流程 */ });
        }).then(function () { self.loading = false; });
      },
      loadPayMethods: function () {
        var self = this;
        return this.api.get('/user/packages/pay-methods', null, { silent: true }).then(function (data) {
          var d = data || {};
          self.payMethods = d.items || (Array.isArray(d) ? d : []);
          if (!self.payMethods.length) self.payMethods = [];
        }).catch(function (err) {
          self.payMethodsError = err.message || '';
          self.payMethods = [];
        }).then(function () {
          // 默认选中第一个可用渠道；没启用在线支付时强制卡密
          var usable = self.enabledMethods.filter(function (m) {
            return m.channel === 'card' || self.payEnabled;
          });
          if (!self.payEnabled) self.methodChannel = 'card';
          else if (usable.length && !usable.some(function (m) { return m.channel === self.methodChannel; })) {
            self.methodChannel = usable[0].channel;
          }
        });
      },
      price: function (pkg) {
        var price = pkg.price_cents || 0;
        return price;
      },
      isCurrent: function (pkg) {
        var cur = this.current || this.currentQuota || {};
        if (cur.package_id && pkg.id) return String(cur.package_id) === String(pkg.id);
        if (cur.package_code && pkg.code) return String(cur.package_code) === String(pkg.code);
        return false;
      },
      canBuy: function (pkg) {
        var price = pkg.price_cents || 0;
        if (price <= 0) return true;
        return this.payEnabled || this.hasCardChannel();
      },
      hasCardChannel: function () {
        var list = this.enabledMethods;
        if (!list.length) return !this.payEnabled;      // 支付方式接口不可用时，只允许卡密流程
        return list.some(function (m) { return m.channel === 'card'; });
      },
      canRecharge: function () {
        return this.payEnabled && this.enabledMethods.some(function (m) { return m.channel !== 'card'; });
      },

      /* ---------------- 下单 ---------------- */
      buy: function (pkg) {
        var self = this;
        var free = (pkg.price_cents || 0) <= 0;
        var channel = free ? 'card' : this.methodChannel;
        // 实测：0 元套餐后端会拒单（400「该套餐价格为 0，无需下单」），所以前端先说清楚，不发无意义的请求
        if (free) {
          this.ui.confirm({
            title: '0 元套餐无需下单',
            message: '「' + (pkg.name || pkg.code) + '」价格为 0，无法在线下单。\n' +
              '请使用卡密兑换开通，或联系管理员直接授权。',
            confirmText: '去卡密兑换', cancelText: '知道了', danger: false
          }).then(function (ok) { if (ok) self.nav.go('/card'); });
          return;
        }
        var doBuy = function () {
          self.buying = pkg.id;
          self.api.post('/user/packages/purchase', { package_id: pkg.id, channel: channel })
            .then(function (data) {
              self.order = data || {};
              self.showPay = true;
              self.pollCount = 0;
              if (self.order.status === 'paid') {
                self.succeed('开通成功');
                self.refreshAfterPaid();
              } else if (self.order.channel === 'card' || self.order.status === 'pending') {
                self.startPoll(self.order.order_no);
              }
            })
            .catch(function (err) { self.error = err.message || '下单失败'; })
            .then(function () { self.buying = null; });
        };
        this.ui.confirm({
          title: '确认下单',
          message: '套餐：' + (pkg.name || pkg.code) + '\n金额：' + this.money(pkg.price_cents || 0) +
            '\n支付方式：' + this.statusText('channel', channel || 'card'),
          confirmText: '创建订单', danger: false
        }).then(function (ok) { if (ok) doBuy(); });
      },
      startPoll: function (orderNo) {
        var self = this;
        if (!orderNo) return;
        this.stopPoll();
        this.pollTimer = setInterval(function () {
          self.pollCount += 1;
          if (self.pollCount > POLL_MAX) { self.stopPoll(); return; }
          self.api.get('/user/orders/' + orderNo, null, { silent: true }).then(function (data) {
            if (!data) return;
            self.order = Object.assign({}, self.order || {}, data);
            if (isTerminal(self.order.status)) {
              self.stopPoll();
              if (self.order.status === 'paid') {
                self.succeed('支付成功，套餐已生效');
                self.refreshAfterPaid();
              } else if (self.order.status === 'expired') {
                self.ui.warn('订单已过期，请重新下单');
              }
            }
          }).catch(function () { /* 轮询期间网络抖动忽略，下一轮继续 */ });
        }, POLL_MS);
      },
      stopPoll: function () {
        if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      },
      queryOrder: function () {
        var self = this;
        if (!this.order || !this.order.order_no) return;
        this.paying = true;
        this.api.post('/user/orders/' + this.order.order_no + '/query', {}).then(function (data) {
          if (data) self.order = Object.assign({}, self.order, data);
          if (self.order.status === 'paid') {
            self.succeed('已确认支付成功');
            self.stopPoll();
            self.refreshAfterPaid();
          } else {
            self.ui.info('支付通道暂未确认到账，请稍后再试');
          }
        }).catch(function () { /* api 层已提示 */ }).then(function () { self.paying = false; });
      },
      refreshAfterPaid: function () {
        this.store.refreshUser();
        this.store.loadLimit();
        this.load();
      },
      closePay: function () {
        this.stopPoll();
        this.showPay = false;
      },

      /* ---------------- 余额充值 ---------------- */
      openRecharge: function () {
        this.rechargeYuan = '10';
        this.rechargeOrder = null;
        this.showRecharge = true;
        var list = this.enabledMethods.filter(function (m) { return m.channel !== 'card'; });
        this.rechargeChannel = list.length ? list[0].channel : 'balance';
      },
      recharge: function () {
        var self = this;
        var cents = this.fmt.toCents(this.rechargeYuan);
        if (!cents || cents < 100) { this.ui.warn('充值金额至少 ¥1.00'); return; }
        this.recharging = true;
        this.api.post('/user/packages/recharge', { amount_cents: cents, channel: this.rechargeChannel })
          .then(function (data) {
            self.rechargeOrder = data || {};
            if (self.rechargeOrder.status === 'paid') {
              self.succeed('充值成功，余额已到账');
              self.store.refreshUser();
              self.showRecharge = false;
            } else {
              self.ui.info('充值订单已创建，请完成支付');
            }
          })
          .catch(function () { /* api 层已提示 */ })
          .then(function () { self.recharging = false; });
      }
    }
  });

  /* --------------------------- 我的订单 --------------------------- */
  S.defineView('view-orders', '/assets/views/orders.html', {
    data: function () {
      return {
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0,
        filters: { status: '', channel: '', keyword: '', start_date: '', end_date: '' },
        showDetail: false, detailLoading: false, detail: null, detailError: '',
        acting: false, pollTimer: null, pollCount: 0
      };
    },
    computed: {
      statusOptions: function () { return [{ value: '', label: '全部状态' }].concat(this.OPTIONS.order_status); },
      channelOptions: function () { return [{ value: '', label: '全部渠道' }].concat(this.OPTIONS.channels); },
      detailQr: function () {
        return this.detail && isQrImage(this.detail.qr_code) ? this.detail.qr_code : '';
      },
      detailQrText: function () {
        if (!this.detail || !this.detail.qr_code || isQrImage(this.detail.qr_code)) return '';
        return this.detail.qr_code;
      }
    },
    created: function () { this.load(); },
    unmounted: function () { this.stopPoll(); },
    methods: {
      load: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/user/orders', Object.assign({
          page: this.page, page_size: this.pageSize
        }, this.filters), { silent: true }).then(function (res) {
          self.items = res.items;
          self.total = res.total;
          self.pages = res.pages;
          self.pageSize = res.page_size || self.pageSize;
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
        this.detailLoading = true;
        this.detailError = '';
        this.detail = row || null;
        this.api.get('/user/orders/' + row.order_no, null, { silent: true })
          .then(function (data) {
            self.detail = data || row;
            if (self.detail && self.detail.status === 'pending') self.startPoll(self.detail.order_no);
          })
          .catch(function (err) { self.detailError = err.message || '订单详情加载失败'; })
          .then(function () { self.detailLoading = false; });
      },
      startPoll: function (orderNo) {
        var self = this;
        this.stopPoll();
        this.pollCount = 0;
        this.pollTimer = setInterval(function () {
          self.pollCount += 1;
          if (self.pollCount > POLL_MAX || !self.showDetail) { self.stopPoll(); return; }
          self.api.get('/user/orders/' + orderNo, null, { silent: true }).then(function (data) {
            if (!data) return;
            self.detail = Object.assign({}, self.detail || {}, data);
            if (isTerminal(self.detail.status)) {
              self.stopPoll();
              if (self.detail.status === 'paid') {
                self.succeed('支付成功，订单已完成');
                self.store.refreshUser();
                self.load();
              }
            }
          }).catch(function () { /* 忽略单次失败 */ });
        }, POLL_MS);
      },
      stopPoll: function () {
        if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      },
      query: function () {
        var self = this;
        if (!this.detail || !this.detail.order_no) return;
        this.acting = true;
        this.api.post('/user/orders/' + this.detail.order_no + '/query', {}).then(function (data) {
          if (data) self.detail = Object.assign({}, self.detail, data);
          if (self.detail.status === 'paid') { self.succeed('已确认支付成功'); self.stopPoll(); self.load(); }
          else self.ui.info('支付通道暂未确认到账');
        }).catch(function () { /* api 层已提示 */ }).then(function () { this.acting = false; });
      },
      cancel: function (row) {
        var self = this;
        this.ui.confirm({
          title: '取消订单',
          message: '确定取消订单 ' + row.order_no + ' 吗？取消后无法恢复，需要重新下单。',
          confirmText: '确认取消', danger: true
        }).then(function (ok) {
          if (!ok) return null;
          self.acting = true;
          return self.api.post('/user/orders/' + row.order_no + '/cancel', {});
        }).then(function (res) {
          if (res === null) { self.acting = false; return; }
          self.succeed('订单已取消');
          self.showDetail = false;
          self.load();
          self.acting = false;
        }).catch(function () { self.acting = false; });
      },
      closeDetail: function () { this.stopPoll(); this.showDetail = false; }
    }
  });

  /* --------------------------- 卡密兑换 --------------------------- */
  S.defineView('view-card', '/assets/views/card.html', {
    data: function () {
      return {
        code: '', redeeming: false, redeemError: '', result: null,
        loading: false, error: '', items: [], page: 1, pageSize: 20, total: 0, pages: 0
      };
    },
    created: function () { this.loadHistory(); },
    methods: {
      loadHistory: function () {
        var self = this;
        this.loading = true;
        this.error = '';
        return this.api.page('/user/card-keys', { page: this.page, page_size: this.pageSize }, { silent: true })
          .then(function (res) {
            self.items = res.items;
            self.total = res.total;
            self.pages = res.pages;
          })
          .catch(function (err) { self.error = err.message || '兑换记录加载失败'; })
          .then(function () { self.loading = false; });
      },
      onCodeInput: function () {
        this.code = String(this.code || '').toUpperCase().replace(/\s+/g, '');
        this.redeemError = '';
      },
      redeem: function () {
        var self = this;
        if (this.redeeming) return;
        var code = String(this.code || '').trim();
        if (!code) { this.redeemError = '请输入卡密'; return; }
        this.ui.confirm({
          title: '确认兑换',
          message: '确定使用卡密 ' + code + ' 吗？\n兑换后卡密立即作废，不能撤销。',
          confirmText: '确认兑换', danger: false
        }).then(function (ok) {
          if (!ok) return null;
          self.redeeming = true;
          self.redeemError = '';
          return self.api.post('/user/card-keys/redeem', { code: code });
        }).then(function (data) {
          if (data === null) { self.redeeming = false; return; }
          self.result = data || {};
          self.succeed((self.result && self.result.message) || '兑换成功');
          self.code = '';
          self.store.refreshUser();
          self.store.loadLimit();
          self.page = 1;
          self.loadHistory();
          self.redeeming = false;
        }).catch(function (err) {
          self.redeemError = err.message || '兑换失败';
          self.redeeming = false;
        });
      },
      maskCode: function (value) {
        var s = String(value || '');
        // 后端返回的 code 已经是打码形式（JEBF-****-****-K7GK），不要二次打码
        if (s.indexOf('*') >= 0) return s || '—';
        if (s.length <= 10) return s || '—';
        return s.slice(0, 4) + '****' + s.slice(-4);
      },
      rowExpire: function (row) {
        return row.expire_after || row.expire_at || '';
      }
    }
  });
})();
