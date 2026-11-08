/* ==========================================================================
   spark-keeper 前端核心（Vue 3 global build，无构建、无 import/export）
   --------------------------------------------------------------------------
   本文件只做「框架层」：
     api      —— fetch 封装（自动带 token、401 自动 refresh 一次、统一错误 toast）
     state    —— 全局 reactive 状态（user / site / quota / theme / unread）
     fmt      —— 时间（按站点时区）与金额（分→¥0.00）格式化
     ui       —— toast / confirm / prompt（危险操作二次确认）
     nav      —— hash 路由 + 登录守卫 + admin 守卫
     components —— 表格 / 分页器 / 状态标签 / 弹窗 / 抽屉 / 图标 / 复制按钮 等
     boot()   —— 拉取页面片段模板 → 恢复会话 → mount

   页面实现放在 views-*.js（用 Spark.defineView 注册，模板是 views/*.html 片段），
   写法约定见 web/CONVENTIONS.md。**不要在本文件里写业务页面逻辑。**
   ========================================================================== */
(function () {
  'use strict';

  var V = window.Vue;
  var bootHost = document.getElementById('app');
  if (!V) {
    if (bootHost) {
      bootHost.innerHTML = '<div style="padding:40px;text-align:center;font-family:sans-serif">' +
        '<h2>前端资源未加载</h2><p>请确认 <code>/static/vendor/vue.global.prod.js</code> 可访问。</p></div>';
    }
    return;
  }

  var CFG = Object.assign({
    version: '1.0.0',
    apiBase: '/api/v1',
    siteName: '抖音续火花系统',
    allowRegister: true,
    onlinePayEnabled: false
  }, window.__SPARK__ || {});

  var LS = {
    token: 'spark_token',
    refresh: 'spark_refresh',
    user: 'spark_user',
    theme: 'spark_theme'
  };

  /* ======================================================================
     1. 小工具
     ====================================================================== */
  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function toNum(v, d) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : (d === undefined ? 0 : d);
  }

  function pick(data, keys, dflt) {
    /* 从 data 里按 keys 的顺序取第一个「有值」的字段，用于兼容后端字段命名差异。
     *
     * 签名是 pick(data, keys, dflt)，调用形式全是 pick(null, data, ['a','b'])——
     * 第一个 null 是历史遗留的占位参数，真正的数据在第二个参数里。
     * （老实现是 `for (var i = 1; i < arguments.length; i++)`，从第二个参数开始
     *   当作「数据源」去枚举它自己的键，于是把 ['access_token','token'] 这个
     *   数组本身当成了数据源 → 永远返回 undefined。后果是登录令牌存不进
     *   localStorage、列表分页字段全空、配额显示为 0。已加诊断脚本
     *   web/tools/diagnose_login.mjs 与 dom_smoke 的登录断言防止回归。）
     */
    var lookup = data;
    var legacyCall = false;
    if ((lookup === null || lookup === undefined) && keys && !Array.isArray(keys) && typeof keys === 'object') {
      legacyCall = true;
      // 容错：万一有人误传成 (null, data, keys) 这种老式三参形式，也能取到
      lookup = keys;
      keys = arguments.length > 2 ? arguments[2] : undefined;
      if (keys && !Array.isArray(keys) && typeof keys === 'object') lookup = keys;
    }
    if (lookup === null || lookup === undefined) {
      // 老写法 pick(null, data, ['k']) 的兼容：第二个参数才是数据、第三个是键数组
      if (keys && !Array.isArray(keys) && typeof keys === 'object') {
        lookup = keys;
        keys = arguments.length > 2 ? arguments[2] : undefined;
      }
    }
    if (!lookup || typeof lookup !== 'object') return dflt;
    var list = Array.isArray(keys) ? keys : (keys === undefined || keys === null ? [] : [keys]);
    for (var i = 0; i < list.length; i++) {
      var k = list[i];
      if (k === undefined || k === null) continue;
      var v = lookup[k];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    // 关键：老式调用 pick(null, data, ['a','b']) 里，第三个参数是**键数组**而不是默认值。
    // 如果这里原样返回 dflt，调用方就拿到 ["a","b"] 这个数组（真值），`|| data` 也就不生效——
    // 真实事故：刷新页面后 state.user 被写成 ["user","profile"]，管理员降级成普通用户。
    // 所以：命中老式签名且没取到值时，返回 undefined（让调用方的 || 生效）。
    if (legacyCall) return undefined;
    return dflt;
  }

  function boolOf(v, dflt) {
    if (v === undefined || v === null || v === '') return !!dflt;
    if (typeof v === 'boolean') return v;
    if (typeof v === 'number') return v !== 0;
    var s = String(v).toLowerCase();
    if (s === '1' || s === 'true' || s === 'yes' || s === 'on') return true;
    if (s === '0' || s === 'false' || s === 'no' || s === 'off') return false;
    return !!dflt;
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait || 300);
    };
  }

  function emptyQuery(q) {
    var out = {};
    Object.keys(q || {}).forEach(function (k) {
      var v = q[k];
      if (v !== undefined && v !== null && v !== '') out[k] = v;
    });
    return out;
  }

  function stripHtml(s) {
    return String(s === undefined || s === null ? '' : s).replace(/<[^>]*>/g, '');
  }

  /* ======================================================================
     2. 格式化：时间按站点时区、金额按分
     ====================================================================== */
  var fmt = {};

  // 契约：后端时间一律是 UTC 字符串 'YYYY-MM-DD HH:MM:SS'
  fmt.parse = function (value) {
    if (!value) return null;
    if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
    var s = String(value).trim();
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
    if (m) {
      return new Date(Date.UTC(
        parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10),
        parseInt(m[4] || '0', 10), parseInt(m[5] || '0', 10), parseInt(m[6] || '0', 10)
      ));
    }
    var d = new Date(s.replace(' ', 'T') + (s.indexOf('Z') < 0 && s.indexOf('+') < 0 ? 'Z' : ''));
    return isNaN(d.getTime()) ? null : d;
  };

  // 用 Intl 取「站点时区」的日期分量，避免 toLocaleString 在不同浏览器的格式差异
  function partsIn(date, tz) {
    var opts = {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    };
    if (tz) opts.timeZone = tz;
    var parts;
    try {
      parts = new Intl.DateTimeFormat('en-GB', opts).formatToParts(date);
    } catch (e) {
      delete opts.timeZone;                       // 时区名非法时退回浏览器本机时区
      parts = new Intl.DateTimeFormat('en-GB', opts).formatToParts(date);
    }
    var out = {};
    parts.forEach(function (p) { if (p.type !== 'literal') out[p.type] = p.value; });
    if (out.hour === '24') out.hour = '00';
    return out;
  }

  fmt.time = function (value, tz) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var p = partsIn(d, tz);
    return p.year + '-' + p.month + '-' + p.day + ' ' + p.hour + ':' + p.minute + ':' + p.second;
  };

  fmt.minute = function (value, tz) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var p = partsIn(d, tz);
    return p.year + '-' + p.month + '-' + p.day + ' ' + p.hour + ':' + p.minute;
  };

  fmt.date = function (value, tz) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var p = partsIn(d, tz);
    return p.year + '-' + p.month + '-' + p.day;
  };

  fmt.clock = function (value, tz) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var p = partsIn(d, tz);
    return p.hour + ':' + p.minute + ':' + p.second;
  };

  fmt.ago = function (value, tz) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 0) {
      var ahead = -diff;
      if (ahead < 60) return '即将执行';
      if (ahead < 3600) return Math.round(ahead / 60) + ' 分钟后';
      if (ahead < 86400) return Math.round(ahead / 3600) + ' 小时后';
      return Math.round(ahead / 86400) + ' 天后';
    }
    if (diff < 45) return '刚刚';
    if (diff < 3600) return Math.round(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.round(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.round(diff / 86400) + ' 天前';
    return fmt.date(value, tz);
  };

  // 剩余时间：用于套餐到期（'还剩 12 天 3 小时'）
  fmt.remaining = function (value) {
    var d = fmt.parse(value);
    if (!d) return '—';
    var diff = d.getTime() - Date.now();
    if (diff <= 0) return '已到期';
    var mins = Math.floor(diff / 60000);
    var days = Math.floor(mins / 1440);
    var hours = Math.floor((mins % 1440) / 60);
    if (days > 0) return days + ' 天' + (hours ? ' ' + hours + ' 小时' : '');
    if (hours > 0) return hours + ' 小时' + (mins % 60 ? ' ' + (mins % 60) + ' 分钟' : '');
    return Math.max(1, mins) + ' 分钟';
  };

  fmt.money = function (cents) {
    var n = toNum(cents, 0);
    var sign = n < 0 ? '-' : '';
    var v = Math.abs(n) / 100;
    var s = v.toFixed(2);
    var parts = s.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return sign + '¥' + parts.join('.');
  };

  fmt.yuan = function (cents) { return (toNum(cents, 0) / 100).toFixed(2); };

  fmt.toCents = function (yuan) {
    var n = String(yuan === undefined || yuan === null ? '' : yuan).replace(/[^\d.\-]/g, '');
    if (!n) return 0;
    return Math.round(toNum(n, 0) * 100);
  };

  fmt.number = function (n) {
    return toNum(n, 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };

  fmt.duration = function (ms) {
    var n = toNum(ms, 0);
    if (!n) return '—';
    if (n < 1000) return n + ' 毫秒';
    var s = n / 1000;
    if (s < 60) return (s >= 10 ? Math.round(s) : s.toFixed(1)) + ' 秒';
    var m = Math.floor(s / 60);
    if (m < 60) return m + ' 分 ' + Math.round(s % 60) + ' 秒';
    return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
  };

  fmt.bytes = function (n) {
    var v = toNum(n, 0);
    if (v < 1024) return v + ' B';
    if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
    if (v < 1024 * 1024 * 1024) return (v / 1048576).toFixed(1) + ' MB';
    return (v / 1073741824).toFixed(2) + ' GB';
  };

  fmt.uptime = function (seconds) {
    var s = toNum(seconds, 0);
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + ' 天 ' + h + ' 小时';
    if (h) return h + ' 小时 ' + m + ' 分';
    return m + ' 分 ' + Math.floor(s % 60) + ' 秒';
  };

  fmt.lines = function (text) {
    // 多行文本框 → 字符串数组（去掉空行与首尾空白）
    return String(text || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
  };

  fmt.weekdayText = function (weekdays) {
    var names = { '1': '周一', '2': '周二', '3': '周三', '4': '周四', '5': '周五', '6': '周六', '7': '周日', '0': '周日' };
    var list = Array.isArray(weekdays) ? weekdays : [];
    if (!list.length) return '未选择';
    return list.map(function (d) { return names[String(d)] || ('周' + d); }).join('、');
  };

  /* ======================================================================
     3. 状态字典（中文文案 + 标签色调，全站唯一来源）
     ====================================================================== */
  var STATUS = {
    account: {
      online: { t: '登录正常', k: 'success' },
      offline: { t: '未登录', k: 'muted' },
      expired: { t: '登录态失效', k: 'warn' },
      banned: { t: '账号异常', k: 'danger' },
      need_verify: { t: '需验证', k: 'warn' },
      paused: { t: '已暂停', k: 'muted' }
    },
    run: {
      running: { t: '执行中', k: 'info' },
      success: { t: '成功', k: 'success' },
      partial: { t: '部分成功', k: 'warn' },
      failed: { t: '失败', k: 'danger' },
      skipped: { t: '已跳过', k: 'muted' },
      canceled: { t: '已取消', k: 'muted' },
      empty: { t: '未执行', k: 'muted' }
    },
    item: {
      pending: { t: '待发送', k: 'muted' },
      success: { t: '成功', k: 'success' },
      failed: { t: '失败', k: 'danger' },
      skipped: { t: '跳过', k: 'muted' }
    },
    order: {
      pending: { t: '待支付', k: 'warn' },
      paid: { t: '已支付', k: 'success' },
      failed: { t: '支付失败', k: 'danger' },
      canceled: { t: '已取消', k: 'muted' },
      refunded: { t: '已退款', k: 'info' },
      expired: { t: '已过期', k: 'muted' }
    },
    card: {
      unused: { t: '未使用', k: 'success' },
      used: { t: '已使用', k: 'muted' },
      disabled: { t: '已停用', k: 'warn' },
      expired: { t: '已过期', k: 'muted' }
    },
    login: {
      pending: { t: '等待扫码', k: 'info' },
      scanned: { t: '已扫码，请在手机上确认', k: 'warn' },
      confirmed: { t: '已确认，正在保存登录态', k: 'info' },
      success: { t: '登录成功', k: 'success' },
      failed: { t: '登录失败', k: 'danger' },
      canceled: { t: '已取消', k: 'muted' },
      expired: { t: '二维码已过期', k: 'warn' }
    },
    user: {
      active: { t: '正常', k: 'success' },
      disabled: { t: '已禁用', k: 'danger' }
    },
    role: {
      user: { t: '普通用户', k: 'muted' },
      agent: { t: '代理', k: 'info' },
      admin: { t: '管理员', k: 'primary' }
    },
    level: {
      debug: { t: '调试', k: 'muted' },
      info: { t: '信息', k: 'info' },
      warning: { t: '警告', k: 'warn' },
      error: { t: '错误', k: 'danger' }
    },
    trigger: {
      schedule: { t: '定时', k: 'info' },
      manual: { t: '手动', k: 'primary' },
      retry: { t: '重试', k: 'warn' }
    },
    channel: {
      card: { t: '卡密兑换', k: 'info' },
      easypay: { t: '易支付', k: 'primary' },
      alipay: { t: '支付宝', k: 'primary' },
      wechat: { t: '微信支付', k: 'success' },
      balance: { t: '余额支付', k: 'warn' },
      manual: { t: '人工确认', k: 'muted' }
    },
    repeat: {
      daily: { t: '每天', k: 'info' },
      weekdays: { t: '工作日', k: 'info' },
      weekends: { t: '周末', k: 'info' },
      custom: { t: '自定义', k: 'primary' },
      interval: { t: '固定间隔', k: 'primary' }
    },
    friendMode: {
      all: { t: '全部好友', k: 'info' },
      selected: { t: '指定好友', k: 'primary' }
    },
    taskType: {
      spark: { t: '续火花', k: 'primary' },
      message: { t: '发消息', k: 'info' },
      check: { t: '仅检测', k: 'muted' }
    },
    taskState: {
      on: { t: '已启用', k: 'success' },
      off: { t: '已停用', k: 'muted' }
    },
    announcement: {
      info: { t: '通知', k: 'info' },
      warning: { t: '提醒', k: 'warn' },
      important: { t: '重要', k: 'danger' }
    },
    cardType: {
      time: { t: '时长卡', k: 'primary' },
      count: { t: '次数卡', k: 'info' },
      balance: { t: '余额卡', k: 'warn' }
    },
    redemption: {
      activate: { t: '激活', k: 'success' },
      renew: { t: '续期', k: 'primary' },
      topup: { t: '充值', k: 'info' },
      consume: { t: '扣次', k: 'muted' }
    },
    proxy: {
      active: { t: '可用', k: 'success' },
      disabled: { t: '已停用', k: 'muted' },
      dead: { t: '不可用', k: 'danger' }
    },
    scope: {
      user: { t: '用户', k: 'muted' },
      admin: { t: '管理端', k: 'primary' },
      system: { t: '系统', k: 'info' },
      worker: { t: '调度器', k: 'warn' }
    }
  };

  var OPTIONS = {
    repeat_mode: [
      { value: 'daily', label: '每天' },
      { value: 'weekdays', label: '工作日（周一至周五）' },
      { value: 'weekends', label: '周末（周六日）' },
      { value: 'custom', label: '自定义星期' },
      { value: 'interval', label: '固定间隔' }
    ],
    friend_mode: [
      { value: 'all', label: '全部好友' },
      { value: 'selected', label: '指定好友' }
    ],
    task_type: [
      { value: 'spark', label: '续火花（发消息）' },
      { value: 'message', label: '仅发消息' },
      { value: 'check', label: '仅检测登录态' }
    ],
    account_status: [
      { value: 'online', label: '登录正常' },
      { value: 'offline', label: '未登录' },
      { value: 'expired', label: '登录态失效' },
      { value: 'banned', label: '账号异常' },
      { value: 'need_verify', label: '需验证' }
    ],
    run_status: [
      { value: 'running', label: '执行中' },
      { value: 'success', label: '成功' },
      { value: 'partial', label: '部分成功' },
      { value: 'failed', label: '失败' },
      { value: 'skipped', label: '已跳过' },
      { value: 'canceled', label: '已取消' }
    ],
    order_status: [
      { value: 'pending', label: '待支付' },
      { value: 'paid', label: '已支付' },
      { value: 'failed', label: '支付失败' },
      { value: 'canceled', label: '已取消' },
      { value: 'refunded', label: '已退款' },
      { value: 'expired', label: '已过期' }
    ],
    channels: [
      { value: 'card', label: '卡密兑换' },
      { value: 'easypay', label: '易支付' },
      { value: 'alipay', label: '支付宝' },
      { value: 'wechat', label: '微信支付' },
      { value: 'balance', label: '余额支付' },
      { value: 'manual', label: '人工确认' }
    ],
    card_status: [
      { value: 'unused', label: '未使用' },
      { value: 'used', label: '已使用' },
      { value: 'disabled', label: '已停用' },
      { value: 'expired', label: '已过期' }
    ],
    card_type: [
      { value: 'time', label: '时长卡（延长套餐到期时间）' },
      { value: 'count', label: '次数卡（增加每天的额外执行次数）' },
      { value: 'balance', label: '余额卡（充值账户余额）' }
    ],
    levels: [
      { value: '', label: '全部级别' },
      { value: 'debug', label: '调试' },
      { value: 'info', label: '信息' },
      { value: 'warning', label: '警告' },
      { value: 'error', label: '错误' }
    ],
    scopes: [
      { value: '', label: '全部来源' },
      { value: 'user', label: '用户' },
      { value: 'admin', label: '管理端' },
      { value: 'system', label: '系统' },
      { value: 'worker', label: '调度器' }
    ],
    roles: [
      { value: '', label: '全部角色' },
      { value: 'user', label: '普通用户' },
      { value: 'agent', label: '代理' },
      { value: 'admin', label: '管理员' }
    ],
    user_status: [
      { value: '', label: '全部状态' },
      { value: 'active', label: '正常' },
      { value: 'disabled', label: '已禁用' }
    ],
    announcement_level: [
      { value: 'info', label: '通知' },
      { value: 'warning', label: '提醒' },
      { value: 'important', label: '重要' }
    ],
    weekdays: [
      { value: '1', label: '周一' }, { value: '2', label: '周二' }, { value: '3', label: '周三' },
      { value: '4', label: '周四' }, { value: '5', label: '周五' }, { value: '6', label: '周六' },
      { value: '7', label: '周日' }
    ],
    timezones: [
      'Asia/Shanghai', 'Asia/Urumqi', 'Asia/Hong_Kong', 'Asia/Taipei', 'Asia/Tokyo',
      'Asia/Singapore', 'UTC'
    ]
  };

  /* ======================================================================
     4. api：fetch 封装
     ====================================================================== */
  function ApiError(message, code, status, data) {
    this.name = 'ApiError';
    this.message = message || '请求失败';
    this.code = code || 'error';
    this.status = status || 0;
    this.data = data;
  }
  ApiError.prototype = Object.create(Error.prototype);

  var refreshing = null;

  function getToken() { return storageGet(LS.token); }

  function absUrl(path) {
    if (/^https?:\/\//i.test(path)) return path;
    if (path.charAt(0) !== '/') path = '/' + path;
    return CFG.apiBase + path;
  }

  function qs(query) {
    var q = emptyQuery(query);
    var keys = Object.keys(q);
    if (!keys.length) return '';
    return '?' + keys.map(function (k) {
      var v = q[k];
      if (Array.isArray(v)) v = v.join(',');
      return encodeURIComponent(k) + '=' + encodeURIComponent(v === null || v === undefined ? '' : v);
    }).join('&');
  }

  function tryRefresh() {
    if (refreshing) return refreshing;
    var rt = storageGet(LS.refresh);
    if (!rt) return Promise.resolve(false);
    refreshing = fetch(absUrl('/auth/refresh'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
      credentials: 'same-origin'
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        if (!res.ok || !payload || payload.code !== 0 || !payload.data) return false;
        state.setSession(payload.data, true);
        return true;
      });
    }).catch(function () { return false; }).then(function (okFlag) {
      refreshing = null;
      return okFlag;
    });
    return refreshing;
  }

  function request(method, path, opts) {
    opts = opts || {};
    var url = absUrl(path) + qs(opts.query);
    var headers = Object.assign({ 'Accept': 'application/json' }, opts.headers || {});
    var token = getToken();
    if (token && !opts.noAuth) headers['Authorization'] = 'Bearer ' + token;
    var body;
    if (opts.body !== undefined && opts.body !== null && method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
    return fetch(url, {
      method: method, headers: headers, body: body, credentials: 'same-origin'
    }).then(function (res) {
      if (res.status === 401 && !opts.retried && !opts.noAuth && getToken()) {
        return tryRefresh().then(function (refreshed) {
          if (refreshed) {
            var retryOpts = Object.assign({}, opts, { retried: true });
            return request(method, path, retryOpts);
          }
          state.clearSession(false);
          ui.err('登录状态已失效，请重新登录');
          go('/login?redirect=' + encodeURIComponent(currentTarget()));
          throw new ApiError('登录状态已失效，请重新登录', 'unauthorized', 401);
        });
      }
      return res.text().then(function (text) {
        var payload = null;
        if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }
        if (payload && typeof payload === 'object' && 'code' in payload) {
          if (payload.code !== 0 && payload.code !== '0') {
            var err = new ApiError(payload.message || '请求失败', payload.code, res.status, payload.data);
            if (!opts.silent) ui.err(err.message);
            throw err;
          }
          return payload.data;
        }
        if (!res.ok) {
          var e2 = new ApiError('请求失败（HTTP ' + res.status + '）', 'http_' + res.status, res.status, payload);
          if (!opts.silent) ui.err(e2.message);
          throw e2;
        }
        if (!text) return null;
        if (payload === null) {
          // 非 JSON 响应（例如导出接口直接给文本）
          var e3 = new ApiError('服务端返回了无法解析的内容', 'bad_response', res.status, text);
          if (!opts.silent) ui.err(e3.message);
          throw e3;
        }
        return payload;
      });
    }, function (netErr) {
      var e4 = new ApiError('网络异常，无法连接服务器', 'network_error', 0, netErr);
      if (!opts.silent) ui.err(e4.message);
      throw e4;
    });
  }

  function normalizePage(data) {
    var out = { items: [], total: 0, page: 1, page_size: 20, pages: 0, unread: -1, summary: null, raw: data };
    if (Array.isArray(data)) {
      out.items = data;
      out.total = data.length;
      out.pages = data.length ? 1 : 0;
      return out;
    }
    if (!data || typeof data !== 'object') return out;
    var items = pick(null, data, ['items', 'list', 'rows', 'records', 'data']);
    out.items = Array.isArray(items) ? items : [];
    out.total = toNum(pick(null, data, ['total', 'count', 'total_count']), out.items.length);
    out.page = toNum(pick(null, data, ['page', 'current_page', 'page_no']), 1) || 1;
    out.page_size = toNum(pick(null, data, ['page_size', 'per_page', 'limit']), 20) || 20;
    out.pages = toNum(pick(null, data, ['pages', 'total_pages', 'last_page']), 0);
    if (!out.pages) out.pages = out.total ? Math.ceil(out.total / out.page_size) : 0;
    // 通知列表会额外带 unread（-1 表示后端没给，页面自己按 is_read 统计）
    var unread = pick(null, data, ['unread', 'unread_count']);
    out.unread = unread === undefined ? -1 : toNum(unread, -1);
    out.summary = data.summary || null;
    return out;
  }

  var api = {
    CFG: CFG,
    ApiError: ApiError,
    request: request,
    absUrl: absUrl,
    token: getToken,
    get: function (path, query, opts) {
      return request('GET', path, Object.assign({}, opts || {}, { query: query }));
    },
    // 分页列表：自动把响应规整成 {items,total,page,page_size,pages}
    page: function (path, query, opts) {
      return request('GET', path, Object.assign({}, opts || {}, { query: query })).then(normalizePage);
    },
    post: function (path, body, opts) {
      return request('POST', path, Object.assign({}, opts || {}, { body: body === undefined ? {} : body }));
    },
    put: function (path, body, opts) {
      return request('PUT', path, Object.assign({}, opts || {}, { body: body === undefined ? {} : body }));
    },
    patch: function (path, body, opts) {
      return request('PATCH', path, Object.assign({}, opts || {}, { body: body === undefined ? {} : body }));
    },
    del: function (path, body, opts) {
      return request('DELETE', path, Object.assign({}, opts || {}, { body: body }));
    },
    // 需要鉴权的文件（截图/导出）：用 fetch 拿 blob 再转 objectURL
    blobUrl: function (path, query) {
      var headers = {};
      var token = getToken();
      if (token) headers['Authorization'] = 'Bearer ' + token;
      return fetch(absUrl(path) + qs(query), { headers: headers, credentials: 'same-origin' })
        .then(function (res) {
          if (!res.ok) throw new ApiError('图片加载失败（HTTP ' + res.status + '）', 'http_' + res.status, res.status);
          return res.blob();
        })
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          objectUrls.push(url);
          return url;
        });
    },
    // 任务截图（契约 §8）：/api/v1/files/screenshot/<run_id>/<name> 需要登录鉴权，
    // 所以不能用 <img src="...">——必须走 fetch 带 token，再转 objectURL。
    screenshotPath: function (runId, name) {
      var s = String(name === undefined || name === null ? '' : name).trim();
      if (!s) return '';
      s = s.replace(/^https?:\/\/[^/]+/i, '');            // 去掉后端可能返回的绝对地址前缀
      if (s.indexOf(CFG.apiBase) === 0) s = s.slice(CFG.apiBase.length);
      if (s.charAt(0) === '/') return s;                   // 已经是 /files/... 形式
      if (s.indexOf('files/') === 0) return '/' + s;
      return '/files/screenshot/' + encodeURIComponent(String(runId)) + '/' + s.split('/').map(encodeURIComponent).join('/');
    },
    screenshotBlob: function (runId, name) {
      var path = api.screenshotPath(runId, name);
      if (!path) return Promise.reject(new ApiError('该记录没有截图', 'not_found', 404));
      return api.blobUrl(path);
    },
    downloadText: function (filename, text, mime) {
      var blob = new Blob(['\ufeff' + text], { type: (mime || 'text/plain') + ';charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    },
    auth: {
      login: function (payload, admin) {
        return request('POST', admin ? '/auth/admin/login' : '/auth/user/login', { body: payload })
          .then(function (data) { state.setSession(data); return data; });
      },
      register: function (payload) {
        return request('POST', '/auth/user/register', { body: payload })
          .then(function (data) { state.setSession(data); return data; });
      },
      sendCode: function (payload) { return request('POST', '/auth/send-verify-code', { body: payload }); },
      verifyCode: function (payload) { return request('POST', '/auth/verify-code', { body: payload }); },
      forgot: function (payload) { return request('POST', '/auth/forgot-password', { body: payload }); },
      reset: function (payload) { return request('POST', '/auth/reset-password', { body: payload }); },
      logout: function () {
        var done = function () { state.clearSession(); go('/login'); };
        return request('POST', '/auth/logout', { body: {}, silent: true }).then(done, done);
      }
    }
  };

  /* ---- 令牌存储：勾选「记住我」→ localStorage，否则 sessionStorage（关页即失效） ---- */
  function storageGet(key) {
    try {
      var v = localStorage.getItem(key);
      if (v !== null && v !== '') return v;
    } catch (e) { /* 隐私模式忽略 */ }
    try { return sessionStorage.getItem(key) || ''; } catch (e) { return ''; }
  }

  function storageSet(key, value, remember) {
    var target = remember ? localStorage : sessionStorage;
    var other = remember ? sessionStorage : localStorage;
    try { other.removeItem(key); } catch (e) { /* 忽略 */ }
    try { target.setItem(key, value); } catch (e) { /* 忽略 */ }
  }

  function storageDel(key) {
    try { localStorage.removeItem(key); } catch (e) { /* 忽略 */ }
    try { sessionStorage.removeItem(key); } catch (e) { /* 忽略 */ }
  }

  var objectUrls = [];
  function releaseObjectUrls() {
    objectUrls.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) { /* 已释放则忽略 */ } });
    objectUrls = [];
  }

  /* ======================================================================
     5. 全局状态
     ====================================================================== */
  function readJSON(key) {
    var raw = storageGet(key);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  var state = V.reactive({
    ready: false,
    bootError: '',
    user: readJSON(LS.user) || null,
    token: getToken(),
    site: {},                 // site_configs 平铺：{'site.name': '...', 'auth.allow_register': '1'}
    quota: null,
    limit: null,
    unread: 0,
    theme: 'light',
    sidebarOpen: false,
    navOpen: false,
    loadingCount: 0,
    version: CFG.version,
    demoMode: false
  });

  state.isLogged = function () { return !!state.token && !!state.user; };
  state.isAdmin = function () { return !!(state.user && state.user.role === 'admin'); };

  state.setSession = function (data, silent) {
    if (!data) return;
    var token = pick(null, data, ['access_token', 'token']);
    var refresh = pick(null, data, ['refresh_token']);
    var user = pick(null, data, ['user', 'profile', 'user_info']);
    // 后端回显 remember：决定令牌放 localStorage（持久）还是 sessionStorage（关页失效）
    var remember = data.remember === undefined ? true : boolOf(data.remember, true);
    if (token) {
      state.token = token;
      storageSet(LS.token, token, remember);
    }
    if (refresh) storageSet(LS.refresh, refresh, remember);
    if (user) {
      state.user = user;
      state.quota = user.quota ? normalizeQuota(user.quota) : state.quota;
      storageSet(LS.user, JSON.stringify(user), remember);
    }
    if (!silent && state.isLogged()) { state.loadUnread(); state.loadLimit(); }
  };

  state.patchUser = function (patch) {
    state.user = Object.assign({}, state.user || {}, patch);
    storageSet(LS.user, JSON.stringify(state.user), true);
  };

  state.clearSession = function (notify) {
    state.token = '';
    state.user = null;
    state.quota = null;
    state.limit = null;
    state.unread = 0;
    storageDel(LS.token);
    storageDel(LS.refresh);
    storageDel(LS.user);
    if (notify === true) ui.info('已退出登录');
  };

  state.siteValue = function (key, dflt) {
    var v = state.site ? state.site[key] : undefined;
    return (v === undefined || v === null || v === '') ? dflt : v;
  };

  state.applyTheme = function (theme) {
    state.theme = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', state.theme);
    try { localStorage.setItem(LS.theme, state.theme); } catch (e) { /* 忽略 */ }
  };

  state.toggleTheme = function () {
    state.applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  };

  // 后端 /auth/public-configs 给的是扁平 snake_case（site_name/allow_register/online_pay_enabled），
  // 而 site_configs 表里是点号键（site.name）。这里统一：读的时候两种写法都能命中。
  var SITE_ALIASES = {
    'site.name': ['site.name', 'site_name', 'name'],
    'site.logo': ['site.logo', 'site_logo', 'logo'],
    'site.icp': ['site.icp', 'site_icp'],
    'site.customer_service': ['site.customer_service', 'site_customer_service'],
    'site.announcement': ['site.announcement', 'site_notice', 'notice'],
    'site.timezone': ['site.timezone', 'default_timezone', 'timezone'],
    'auth.allow_register': ['auth.allow_register', 'allow_register'],
    'auth.require_email_verify': ['auth.require_email_verify', 'require_email_verify'],
    'auth.require_invite': ['auth.require_invite', 'require_invite'],
    'pay.enabled': ['pay.enabled', 'online_pay_enabled'],
    'pay.channel': ['pay.channel', 'pay_channel'],
    'task.default_time': ['task.default_time', 'default_time'],
    'task.default_timezone': ['task.default_timezone', 'default_timezone'],
    'site.version': ['site.version', 'version'],
    'site.api_base': ['site.api_base', 'api_base']
  };

  function normalizeSite(raw) {
    var site = Object.assign({}, raw || {});
    Object.keys(SITE_ALIASES).forEach(function (canonical) {
      if (site[canonical] !== undefined && site[canonical] !== '') return;
      var keys = SITE_ALIASES[canonical];
      for (var i = 1; i < keys.length; i++) {
        var v = site[keys[i]];
        if (v !== undefined && v !== null && v !== '') { site[canonical] = v; return; }
      }
    });
    return site;
  }

  state.loadSite = function () {
    return request('GET', '/auth/public-configs', { silent: true, noAuth: true }).then(function (data) {
      var raw = {};
      if (data && typeof data === 'object') {
        if (data.items && typeof data.items === 'object' && !Array.isArray(data.items)) {
          raw = Object.assign({}, data.items);
        } else if (Array.isArray(data.items)) {
          data.items.forEach(function (row) {
            if (row && row.key !== undefined) raw[row.key] = row.value;
          });
        } else {
          raw = Object.assign({}, data);
        }
        if (data.site && typeof data.site === 'object') raw = Object.assign(raw, data.site);
      }
      var site = normalizeSite(raw);
      state.site = site;
      CFG.onlinePayEnabled = boolOf(site['pay.enabled'], CFG.onlinePayEnabled);
      CFG.allowRegister = boolOf(site['auth.allow_register'], CFG.allowRegister);
      if (site['site.name']) CFG.siteName = site['site.name'];
      if (site['site.version']) state.version = site['site.version'];
      if (site['site.api_base']) CFG.apiBase = site['site.api_base'];
      applySiteChrome();
      return site;
    });
  };

  state.loadUnread = function () {
    if (!state.isLogged()) { state.unread = 0; return Promise.resolve(0); }
    return request('GET', '/user/notifications', { query: { page: 1, page_size: 1 }, silent: true })
      .then(function (data) {
        var n = 0;
        if (data && typeof data === 'object') {
          n = toNum(pick(null, data, ['unread', 'unread_count', 'unread_total']), NaN);
          if (!isFinite(n)) {
            var items = data.items || [];
            n = items.filter(function (it) { return it && !it.read_at; }).length;
          }
        }
        state.unread = n;
        return n;
      })
      .catch(function () { return 0; });
  };

  // 存活探针顺带拿版本与演示模式开关（public 接口，失败不影响启动）
  state.loadHealth = function () {
    return request('GET', '/health', { silent: true, noAuth: true }).then(function (data) {
      var d = data || {};
      if (d.version) state.version = d.version;
      state.demoMode = boolOf(pick(null, d, ['demo_mode', 'demoMode']), false);
      return d;
    }).catch(function () { return null; });
  };

  state.loadLimit = function () {
    if (!state.isLogged()) return Promise.resolve(null);
    return request('GET', '/user/packages/limit-status', { silent: true }).then(function (data) {
      var limit = normalizeQuota(data);
      if (limit) {
        state.limit = limit;
        state.quota = limit;
        if (state.user) state.user.quota = limit;
      }
      return limit;
    }).catch(function () { return null; });
  };

  state.refreshUser = function () {
    if (!state.token) return Promise.resolve(null);
    return request('GET', '/user/info', { silent: true }).then(function (data) {
      // /user/info 返回的是**扁平用户对象**（data 本身就是 user），
      // 也可能被某些版本包成 {user: ...} / {profile: ...}，三种都要吃下。
      // 注意不要写成 pick(null, data, ['user','profile'])：缺键时老式签名会把
      // 键数组当默认值返回，导致 state.user 变成 ["user","profile"]（真实事故）。
      var user = (data && (data.user || data.profile)) || data;
      if (user && typeof user === 'object') {
        state.user = user;
        if (user.quota) state.quota = normalizeQuota(user.quota);
        try { localStorage.setItem(LS.user, JSON.stringify(user)); } catch (e) { /* 忽略 */ }
      }
      return user;
    });
  };

  function normalizeQuota(q) {
    if (!q || typeof q !== 'object') return null;
    // 保留后端新增字段（bonus_task_count / bonus_expire_at / account_remaining 等），
    // 否则页面拿不到「赠送次数」这类加成信息
    return Object.assign({}, q, {
      active: boolOf(q.active, true),
      package_id: pick(null, q, ['package_id']),
      package_code: pick(null, q, ['package_code']) || '',
      package_name: pick(null, q, ['package_name']) || '',
      expire_at: pick(null, q, ['expire_at', 'package_expire_at']) || '',
      expire_text: pick(null, q, ['expire_text']) || '',
      account_limit: toNum(pick(null, q, ['account_limit']), 0),
      account_used: toNum(pick(null, q, ['account_used', 'accounts_used']), 0),
      daily_task_limit: toNum(pick(null, q, ['daily_task_limit']), 0),
      // 不限次模式：后端给 daily_task_unlimited=true 时，界面显示 x/∞ 而不是 x/0
      daily_task_unlimited: !!pick(null, q, ['daily_task_unlimited']),
      daily_task_used: toNum(pick(null, q, ['daily_task_used', 'today_used']), 0),
      bonus_task_count: toNum(pick(null, q, ['bonus_task_count']), 0),
      bonus_expire_at: pick(null, q, ['bonus_expire_at']) || '',
      role: pick(null, q, ['role']) || ''
    });
  }

  function applySiteChrome() {
    var name = state.siteValue('site.name', CFG.siteName);
    document.title = name;
    var link = document.querySelector('link[rel="icon"]');
    var logo = state.siteValue('site.logo', '');
    if (link && logo) link.setAttribute('href', logo);
  }

  /* ======================================================================
     6. ui：toast / confirm / prompt
     ====================================================================== */
  // userMenuOpen 放在这里（而不是 App 组件的 data 里）：因为「点空白关闭菜单」的
  // 全局监听器在组件外，需要读写同一份状态。放在组件 data 里会导致两边各有一份、
  // 全局监听器改不动组件那份（那正是「点退出没反应」这类问题的温床）。
  var uiState = V.reactive({ toasts: [], dialog: null, userMenuOpen: false });

  function toast(message, type, timeout) {
    var text = stripHtml(message);
    if (!text) return;
    var item = { id: ++toastSeq, message: text, type: type || 'info' };
    uiState.toasts.push(item);
    var ms = timeout === undefined ? (item.type === 'error' ? 5200 : 2800) : timeout;
    if (ms > 0) setTimeout(function () { closeToast(item.id); }, ms);
  }
  var toastSeq = 0;
  function closeToast(id) {
    for (var i = 0; i < uiState.toasts.length; i++) {
      if (uiState.toasts[i].id === id) { uiState.toasts.splice(i, 1); return; }
    }
  }

  var dialogSeq = 0;
  function openDialog(opts) {
    return new Promise(function (resolve) {
      uiState.dialog = Object.assign({
        id: ++dialogSeq,
        title: '请确认',
        message: '',
        html: '',
        confirmText: '确认',
        cancelText: '取消',
        danger: false,
        // prompt 模式
        input: false,
        inputLabel: '',
        inputPlaceholder: '',
        inputValue: '',
        inputRequired: false,
        inputHint: '',
        inputType: 'text',
        wide: false,
        resolve: resolve
      }, opts || {});
    });
  }

  var ui = {
    state: uiState,
    toast: toast,
    ok: function (m) { toast(m, 'success'); },
    err: function (m) { toast(m, 'error'); },
    warn: function (m) { toast(m, 'warn'); },
    info: function (m) { toast(m, 'info'); },
    closeToast: closeToast,
    confirm: function (opts) {
      if (typeof opts === 'string') opts = { message: opts };
      return openDialog(Object.assign({ danger: true, confirmText: '确认' }, opts));
    },
    prompt: function (opts) {
      if (typeof opts === 'string') opts = { message: opts };
      return openDialog(Object.assign({ input: true, danger: false, confirmText: '确定' }, opts));
    },
    // 供 confirm 弹窗组件调用
    resolveDialog: function (value) {
      var d = uiState.dialog;
      uiState.dialog = null;
      if (d && d.resolve) d.resolve(value);
    }
  };

  /* ======================================================================
     7. 路由（hash 路由 + 守卫）
     ====================================================================== */
  var ROUTES = [
    { path: '/login', view: 'view-login', title: '登录', meta: { guest: true } },
    { path: '/register', view: 'view-register', title: '注册', meta: { guest: true } },
    { path: '/forgot', view: 'view-forgot', title: '找回密码', meta: { guest: true } },
    { path: '/dashboard', view: 'view-dashboard', title: '仪表盘', meta: { auth: true } },
    { path: '/accounts', view: 'view-accounts', title: '抖音账号', meta: { auth: true } },
    { path: '/accounts/:id', view: 'view-account-detail', title: '账号详情', meta: { auth: true } },
    { path: '/tasks', view: 'view-tasks', title: '定时任务', meta: { auth: true } },
    { path: '/tasks/new', view: 'view-task-edit', title: '新建定时任务', meta: { auth: true } },
    { path: '/tasks/:id', view: 'view-task-edit', title: '编辑定时任务', meta: { auth: true } },
    { path: '/friends', view: 'view-friends', title: '好友管理', meta: { auth: true } },
    { path: '/records', view: 'view-records', title: '执行记录', meta: { auth: true } },
    { path: '/records/:id', view: 'view-records', title: '执行详情', meta: { auth: true } },
    { path: '/packages', view: 'view-packages', title: '套餐购买', meta: { auth: true } },
    { path: '/orders', view: 'view-orders', title: '我的订单', meta: { auth: true } },
    { path: '/card', view: 'view-card', title: '卡密兑换', meta: { auth: true } },
    { path: '/logs', view: 'view-logs', title: '运行日志', meta: { auth: true } },
    { path: '/settings', view: 'view-settings', title: '个人设置', meta: { auth: true } },
    { path: '/notifications', view: 'view-notifications', title: '站内通知', meta: { auth: true } },
    { path: '/admin', view: 'view-admin-dashboard', title: '管理概览', meta: { auth: true, admin: true } },
    { path: '/admin/users', view: 'view-admin-users', title: '用户管理', meta: { auth: true, admin: true } },
    { path: '/admin/cards', view: 'view-admin-cards', title: '卡密管理', meta: { auth: true, admin: true } },
    { path: '/admin/packages', view: 'view-admin-packages', title: '套餐管理', meta: { auth: true, admin: true } },
    { path: '/admin/orders', view: 'view-admin-orders', title: '订单管理', meta: { auth: true, admin: true } },
    { path: '/admin/accounts', view: 'view-admin-accounts', title: '账号总览', meta: { auth: true, admin: true } },
    { path: '/admin/tasks', view: 'view-admin-tasks', title: '任务总览', meta: { auth: true, admin: true } },
    { path: '/admin/records', view: 'view-admin-records', title: '执行记录', meta: { auth: true, admin: true } },
    { path: '/admin/logs', view: 'view-admin-logs', title: '运行与审计日志', meta: { auth: true, admin: true } },
    { path: '/admin/announcements', view: 'view-admin-announcements', title: '公告管理', meta: { auth: true, admin: true } },
    { path: '/admin/configs', view: 'view-admin-configs', title: '站点配置', meta: { auth: true, admin: true } },
    { path: '/admin/proxies', view: 'view-admin-proxies', title: '代理管理', meta: { auth: true, admin: true } },
    { path: '/admin/system', view: 'view-admin-system', title: '系统环境', meta: { auth: true, admin: true } }
  ];

  // FAQ：为什么不用 vue-router —— 无构建环境下少一个依赖，hash 路由 40 行就够。
  function compile(pattern) {
    var keys = [];
    var regex = pattern.replace(/\/:([A-Za-z0-9_]+)/g, function (_, key) {
      keys.push(key);
      return '/([^/]+)';
    });
    return { regex: new RegExp('^' + regex + '$'), keys: keys };
  }
  ROUTES.forEach(function (r) { var c = compile(r.path); r.regex = c.regex; r.keys = c.keys; });

  function parseTarget(raw) {
    var s = String(raw || '').replace(/^#/, '');
    if (!s) s = '/';
    var path = s, query = {};
    var qi = s.indexOf('?');
    if (qi >= 0) {
      path = s.slice(0, qi);
      s.slice(qi + 1).split('&').forEach(function (pair) {
        if (!pair) return;
        var eq = pair.indexOf('=');
        var k = eq < 0 ? pair : pair.slice(0, eq);
        var v = eq < 0 ? '' : pair.slice(eq + 1);
        try { query[decodeURIComponent(k)] = decodeURIComponent(v.replace(/\+/g, ' ')); }
        catch (e) { query[k] = v; }
      });
    }
    if (path.length > 1 && path.charAt(path.length - 1) === '/') path = path.slice(0, -1);
    return { path: path || '/', query: query };
  }

  function matchRoute(path) {
    for (var i = 0; i < ROUTES.length; i++) {
      var m = path.match(ROUTES[i].regex);
      if (m) {
        var params = {};
        ROUTES[i].keys.forEach(function (k, idx) { params[k] = decodeURIComponent(m[idx + 1]); });
        return { def: ROUTES[i], params: params };
      }
    }
    return null;
  }

  function currentTarget() {
    return location.hash ? location.hash.replace(/^#/, '') : '/dashboard';
  }

  function go(target) {
    var next = String(target || '');
    if (next.charAt(0) === '#') next = next.slice(1);
    if (!next) next = '/';
    if (currentTarget() === next) { resolveRoute(true); return; }
    location.hash = next;
  }

  var route = V.reactive({
    path: '/', query: {}, params: {}, view: 'view-dashboard', def: ROUTES[3], notFound: false, key: ''
  });

  var router = {
    routes: ROUTES,
    current: route,
    go: go,
    replace: function (target) { go(target); },
    href: function (target) { return '#' + String(target || '/').replace(/^#/, ''); },
    isActive: function (path, exact) {
      var base = path.split('?')[0];
      if (exact) return route.path === base;
      if (base === '/admin') return route.path === '/admin';
      return route.path === base || route.path.indexOf(base + '/') === 0;
    },
    resolve: resolveRoute
  };

  function resolveRoute(silent) {
    var parsed = parseTarget(currentTarget());
    var hit = matchRoute(parsed.path);
    if (!hit) {
      hit = { def: ROUTES[3], params: {} };      // 未知路径 → 仪表盘
      route.notFound = true;
    } else {
      route.notFound = false;
    }
    var def = hit.def;
    var meta = def.meta || {};

    if (meta.auth && !state.isLogged()) {
      // 未登录访问受限页：跳登录并带上回跳地址
      go('/login?redirect=' + encodeURIComponent(parsed.path + (parsed.query.redirect ? '' : '')));
      return;
    }
    if (meta.admin && !state.isAdmin()) {
      ui.err('需要管理员权限');
      go('/dashboard');
      return;
    }
    if (meta.guest && state.isLogged()) {
      var back = parsed.query.redirect ? decodeURIComponent(parsed.query.redirect) : '/dashboard';
      go(back);
      return;
    }

    releaseObjectUrls();
    route.path = parsed.path;
    route.query = parsed.query;
    route.params = hit.params;
    route.def = def;
    route.view = def.view;
    route.key = parsed.path + '|' + JSON.stringify(hit.params) + '|' + JSON.stringify(parsed.query);
    state.sidebarOpen = false;
    state.navOpen = false;

    var siteName = state.siteValue('site.name', CFG.siteName);
    document.title = def.title + ' · ' + siteName;
    if (!silent && typeof window.scrollTo === 'function') window.scrollTo({ top: 0, behavior: 'auto' });
  }

  window.addEventListener('hashchange', function () { resolveRoute(); });

  /* ======================================================================
     8. 全局组件
     ====================================================================== */
  var ICONS = {
    dashboard: '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    key: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    cart: '<circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    sun: '<circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    menu: '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
    x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    search: '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
    play: '<polygon points="5 3 19 12 5 21 5 3"/>',
    eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    check: '<polyline points="20 6 9 17 4 12"/>',
    'check-circle': '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    'x-circle': '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    chart: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
    trend: '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    globe: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
    cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
    database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
    activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    card: '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    chevronLeft: '<polyline points="15 18 9 12 15 6"/>',
    chevronRight: '<polyline points="9 18 15 12 9 6"/>',
    chevronDown: '<polyline points="6 9 12 15 18 9"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    smartphone: '<rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>',
    mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22 6 12 13 2 6"/>',
    lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
    clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    megaphone: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    robot: '<rect x="4" y="8" width="16" height="12" rx="3"/><line x1="12" y1="4" x2="12" y2="8"/><circle cx="12" cy="3" r="1"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/>',
    flame: '<path d="M12 2c1.5 3.5 5 5.2 5 9a5 5 0 0 1-10 0c0-1.6.6-2.8 1.4-3.9.6 1.2 1.3 1.9 2.3 2.2-.9-2.7-.7-5.4 1.3-7.3z"/>',
    send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    message: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    power: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
    wifi: '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
    tag: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
    list: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    sliders: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    tool: '<path d="M14.7 6.3a4 4 0 0 0 5.6 5.6l-8.1 8.1-5.6-5.6z"/><path d="M9.5 14.5L3 21"/>'
  };

  var Icon = {
    name: 'Icon',
    props: {
      name: { type: String, required: true },
      size: { type: [Number, String], default: 18 },
      stroke: { type: [Number, String], default: 1.7 }
    },
    computed: {
      body: function () { return ICONS[this.name] || ICONS.info; }
    },
    template: '<svg class="icon" :width="size" :height="size" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" :stroke-width="stroke" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true" v-html="body"></svg>'
  };

  var StatusTag = {
    name: 'StatusTag',
    props: {
      kind: { type: String, default: '' },
      value: { type: [String, Number, Boolean], default: '' },
      text: { type: String, default: '' },
      tone: { type: String, default: '' },
      dot: { type: Boolean, default: true }
    },
    computed: {
      entry: function () {
        var dict = STATUS[this.kind];
        var key = String(this.value === null || this.value === undefined ? '' : this.value);
        if (dict && dict[key]) return dict[key];
        if (dict && dict[''] && !key) return dict[''];
        return { t: this.text || key || '未知', k: 'muted' };
      },
      label: function () { return this.text || this.entry.t; },
      klass: function () { return 'tag tag--' + (this.tone || this.entry.k || 'muted') + (this.dot ? ' tag--dot' : ''); }
    },
    template: '<span :class="klass">{{ label }}</span>'
  };

  var CopyButton = {
    name: 'CopyButton',
    props: {
      text: { type: [String, Number], default: '' },
      label: { type: String, default: '' },
      size: { type: String, default: 'sm' },
      iconOnly: { type: Boolean, default: false }
    },
    methods: {
      copy: function () {
        var value = String(this.text === undefined || this.text === null ? '' : this.text);
        if (!value) { ui.warn('没有可复制的内容'); return; }
        var done = function () { ui.ok('已复制到剪贴板'); };
        var fail = function () {
          // 非 HTTPS 下 navigator.clipboard 不可用，退回 textarea + execCommand
          try {
            var ta = document.createElement('textarea');
            ta.value = value;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            done();
          } catch (e) { ui.err('复制失败，请手动选择文本'); }
        };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(value).then(done, fail);
        } else { fail(); }
      }
    },
    template: '<button type="button" class="btn btn--ghost btn--sm" @click="copy" :title="label || \'复制\'">' +
      '<icon name="copy" :size="13" v-if="iconOnly"></icon>' +
      '<span v-if="!iconOnly"><icon name="copy" :size="13" style="vertical-align:-2px"></icon> {{ label || \'复制\' }}</span>' +
      '</button>'
  };

  var DataTable = {
    name: 'DataTable',
    props: {
      loading: { type: Boolean, default: false },
      error: { type: String, default: '' },
      empty: { type: Boolean, default: false },
      colspan: { type: [Number, String], default: 1 },
      emptyTitle: { type: String, default: '暂无数据' },
      emptyHint: { type: String, default: '' },
      skeletonRows: { type: Number, default: 5 }
    },
    template:
      '<div class="table-wrap">' +
      '  <table class="table"><slot name="head"></slot>' +
      '    <tbody v-if="loading">' +
      '      <tr v-for="n in skeletonRows" :key="\'sk\'+n"><td :colspan="colspan"><div class="skeleton"></div></td></tr>' +
      '    </tbody>' +
      '    <tbody v-else-if="error">' +
      '      <tr><td :colspan="colspan">' +
      '        <div class="empty"><div class="empty__icon"><icon name="alert" :size="20"></icon></div>' +
      '          <div class="empty__title">加载失败</div>' +
      '          <div class="empty__hint">{{ error }}</div>' +
      '          <div class="empty__action"><button class="btn btn--sm" @click="$emit(\'retry\')">' +
      '            <icon name="refresh" :size="13"></icon> 重新加载</button></div>' +
      '        </div></td></tr>' +
      '    </tbody>' +
      '    <tbody v-else-if="empty">' +
      '      <tr><td :colspan="colspan">' +
      '        <div class="empty"><div class="empty__icon"><icon name="inbox" :size="20"></icon></div>' +
      '          <div class="empty__title">{{ emptyTitle }}</div>' +
      '          <div class="empty__hint" v-if="emptyHint">{{ emptyHint }}</div>' +
      '          <div class="empty__action"><slot name="empty-action"></slot></div>' +
      '        </div></td></tr>' +
      '    </tbody>' +
      '    <tbody v-else><slot></slot></tbody>' +
      '  </table></div>'
  };

  var Pager = {
    name: 'Pager',
    props: {
      page: { type: Number, default: 1 },
      pages: { type: Number, default: 0 },
      total: { type: Number, default: 0 },
      pageSize: { type: Number, default: 20 },
      sizes: { type: Array, default: function () { return [10, 20, 50, 100]; } },
      showSize: { type: Boolean, default: true }
    },
    emits: ['change', 'size'],
    computed: {
      numbers: function () {
        var out = [];
        var pages = this.pages || 0;
        var cur = this.page || 1;
        if (pages <= 7) {
          for (var i = 1; i <= pages; i++) out.push(i);
          return out;
        }
        out.push(1);
        var start = Math.max(2, cur - 1);
        var end = Math.min(pages - 1, cur + 1);
        if (start > 2) out.push('...');
        for (var j = start; j <= end; j++) out.push(j);
        if (end < pages - 1) out.push('...');
        out.push(pages);
        return out;
      },
      from: function () { return this.total ? (this.page - 1) * this.pageSize + 1 : 0; },
      to: function () { return Math.min(this.total, this.page * this.pageSize); }
    },
    methods: {
      goto: function (p) {
        if (p === '...' || p === this.page || p < 1 || (this.pages && p > this.pages)) return;
        this.$emit('change', p);
      },
      changeSize: function (e) { this.$emit('size', parseInt(e.target.value, 10)); }
    },
    template:
      '<div class="pager" v-if="total > 0 || pages > 1">' +
      '  <div class="pager__info">共 {{ total }} 条{{ total ? \'，当前 \' + from + \'-\' + to : \'\' }}</div>' +
      '  <div class="pager__pages">' +
      '    <button class="pager__btn" :disabled="page<=1" @click="goto(page-1)" title="上一页">' +
      '      <icon name="chevronLeft" :size="14"></icon></button>' +
      '    <button v-for="(p,i) in numbers" :key="i" class="pager__btn" :class="{\'is-active\': p===page}" ' +
      '      :disabled="p===\'...\'" @click="goto(p)">{{ p }}</button>' +
      '    <button class="pager__btn" :disabled="!pages || page>=pages" @click="goto(page+1)" title="下一页">' +
      '      <icon name="chevronRight" :size="14"></icon></button>' +
      '  </div>' +
      '  <label class="pager__size" v-if="showSize">每页' +
      '    <span class="select-wrap"><select class="select" :value="pageSize" @change="changeSize">' +
      '      <option v-for="s in sizes" :key="s" :value="s">{{ s }}</option>' +
      '    </select></span> 条</label>' +
      '</div>'
  };

  var Modal = {
    name: 'Modal',
    props: {
      modelValue: { type: Boolean, default: false },
      title: { type: String, default: '' },
      sub: { type: String, default: '' },
      size: { type: String, default: '' },
      closeOnBackdrop: { type: Boolean, default: true }
    },
    emits: ['update:modelValue', 'close'],
    methods: {
      close: function () { this.$emit('update:modelValue', false); this.$emit('close'); },
      backdrop: function (e) { if (this.closeOnBackdrop && e.target === e.currentTarget) this.close(); }
    },
    template:
      '<div class="overlay" v-if="modelValue" @click="backdrop">' +
      '  <div class="modal" :class="size ? \'modal--\'+size : \'\'" role="dialog" aria-modal="true">' +
      '    <div class="modal__head">' +
      '      <div class="modal__title">{{ title }}<div class="modal__sub" v-if="sub">{{ sub }}</div></div>' +
      '      <button class="icon-btn" @click="close" title="关闭"><icon name="x" :size="16"></icon></button>' +
      '    </div>' +
      '    <div class="modal__body"><slot></slot></div>' +
      '    <div class="modal__foot" v-if="$slots.foot"><slot name="foot"></slot></div>' +
      '  </div>' +
      '</div>'
  };

  var Drawer = {
    name: 'Drawer',
    props: {
      modelValue: { type: Boolean, default: false },
      title: { type: String, default: '' },
      sub: { type: String, default: '' },
      size: { type: String, default: '' }
    },
    emits: ['update:modelValue', 'close'],
    methods: {
      close: function () { this.$emit('update:modelValue', false); this.$emit('close'); },
      backdrop: function (e) { if (e.target === e.currentTarget) this.close(); }
    },
    template:
      '<div class="overlay overlay--drawer" v-if="modelValue" @click="backdrop">' +
      '  <div class="drawer" :class="size ? \'drawer--\'+size : \'\'" role="dialog" aria-modal="true">' +
      '    <div class="drawer__head">' +
      '      <div class="modal__title">{{ title }}<div class="modal__sub" v-if="sub">{{ sub }}</div></div>' +
      '      <button class="icon-btn" @click="close" title="关闭"><icon name="x" :size="16"></icon></button>' +
      '    </div>' +
      '    <div class="drawer__body"><slot></slot></div>' +
      '    <div class="drawer__foot" v-if="$slots.foot"><slot name="foot"></slot></div>' +
      '  </div>' +
      '</div>'
  };

  var Field = {
    name: 'Field',
    props: {
      label: { type: String, default: '' },
      hint: { type: String, default: '' },
      error: { type: String, default: '' },
      required: { type: Boolean, default: false },
      span: { type: [Number, String], default: 0 }
    },
    computed: {
      style: function () { return this.span ? { gridColumn: 'span ' + this.span } : {}; }
    },
    template:
      '<div class="field" :style="style">' +
      '  <label class="field__label" v-if="label">{{ label }}<span class="req" v-if="required">*</span></label>' +
      '  <slot></slot>' +
      '  <div class="field__hint" v-if="hint && !error">{{ hint }}</div>' +
      '  <div class="field__error" v-if="error">{{ error }}</div>' +
      '</div>'
  };

  var Alert = {
    name: 'Alert',
    props: {
      type: { type: String, default: 'info' },
      message: { type: String, default: '' },
      closable: { type: Boolean, default: false }
    },
    data: function () { return { closed: false }; },
    computed: {
      iconName: function () {
        if (this.type === 'error') return 'x-circle';
        if (this.type === 'success') return 'check-circle';
        if (this.type === 'warn') return 'alert';
        return 'info';
      }
    },
    template:
      '<div class="alert" :class="\'alert--\'+type" v-if="!closed && (message || $slots.default)">' +
      '  <icon :name="iconName" :size="16" style="margin-top:2px"></icon>' +
      '  <div class="alert__body"><slot>{{ message }}</slot></div>' +
      '  <button class="alert__close" v-if="closable" @click="closed=true" title="关闭">×</button>' +
      '</div>'
  };

  var EmptyState = {
    name: 'EmptyState',
    props: {
      icon: { type: String, default: 'inbox' },
      title: { type: String, default: '暂无数据' },
      hint: { type: String, default: '' }
    },
    template:
      '<div class="empty"><div class="empty__icon"><icon :name="icon" :size="20"></icon></div>' +
      '  <div class="empty__title">{{ title }}</div>' +
      '  <div class="empty__hint" v-if="hint">{{ hint }}</div>' +
      '  <div class="empty__action" v-if="$slots.default"><slot></slot></div>' +
      '</div>'
  };

  var StatCard = {
    name: 'StatCard',
    props: {
      label: { type: String, default: '' },
      value: { type: [String, Number], default: '—' },
      hint: { type: String, default: '' },
      icon: { type: String, default: 'chart' },
      tone: { type: String, default: '' }
    },
    template:
      '<div class="stat">' +
      '  <div class="stat__icon" :class="tone ? \'stat__icon--\'+tone : \'\'"><icon :name="icon" :size="18"></icon></div>' +
      '  <div class="stat__body">' +
      '    <div class="stat__label">{{ label }}</div>' +
      '    <div class="stat__value"><slot>{{ value }}</slot></div>' +
      '    <div class="stat__hint" v-if="hint">{{ hint }}</div>' +
      '  </div>' +
      '</div>'
  };

  var PageHead = {
    name: 'PageHead',
    props: {
      title: { type: String, default: '' },
      sub: { type: String, default: '' }
    },
    template:
      '<div class="page-head"><div class="page-head__text">' +
      '  <div class="page-head__title">{{ title }}</div>' +
      '  <div class="page-head__sub" v-if="sub">{{ sub }}</div>' +
      '</div><div class="page-head__actions"><slot></slot></div></div>'
  };

  var TimeText = {
    name: 'TimeText',
    props: {
      value: { type: [String, Number], default: '' },
      mode: { type: String, default: 'time' },
      tz: { type: String, default: '' }
    },
    computed: {
      text: function () {
        var zone = this.tz || timezone();
        if (this.mode === 'date') return fmt.date(this.value, zone);
        if (this.mode === 'minute') return fmt.minute(this.value, zone);
        if (this.mode === 'ago') return fmt.ago(this.value, zone);
        if (this.mode === 'remaining') return fmt.remaining(this.value);
        if (this.mode === 'full') {
          var d = fmt.parse(this.value);
          if (!d) return '—';
          return fmt.time(this.value, zone) + '（' + fmt.ago(this.value, zone) + '）';
        }
        return fmt.time(this.value, zone);
      }
    },
    template: '<span :title="text">{{ text }}</span>'
  };

  var Money = {
    name: 'Money',
    props: {
      cents: { type: [Number, String], default: 0 },
      strong: { type: Boolean, default: false }
    },
    computed: { text: function () { return fmt.money(this.cents); } },
    template: '<span :class="{\'cell-strong\': strong}" style="font-variant-numeric:tabular-nums">{{ text }}</span>'
  };

  var ToastHost = {
    name: 'ToastHost',
    computed: {
      toasts: function () { return uiState.toasts; }
    },
    methods: {
      iconOf: function (type) {
        if (type === 'success') return 'check-circle';
        if (type === 'error') return 'x-circle';
        if (type === 'warn') return 'alert';
        return 'info';
      },
      close: function (id) { ui.closeToast(id); }
    },
    template:
      '<div id="toast-host" aria-live="polite">' +
      '  <div v-for="t in toasts" :key="t.id" class="toast" :class="\'toast--\'+t.type" @click="close(t.id)">' +
      '    <span class="toast__icon"><icon :name="iconOf(t.type)" :size="16"></icon></span>' +
      '    <span class="toast__msg">{{ t.message }}</span>' +
      '  </div>' +
      '</div>'
  };

  var ConfirmDialog = {
    name: 'ConfirmDialog',
    data: function () { return { inputValue: '', inputError: '' }; },
    computed: {
      dialog: function () { return uiState.dialog; }
    },
    watch: {
      dialog: function (d) {
        this.inputValue = d ? (d.inputValue || '') : '';
        this.inputError = '';
      }
    },
    methods: {
      cancel: function () { ui.resolveDialog(null); },
      accept: function () {
        if (!this.dialog) return;
        if (this.dialog.input) {
          if (this.dialog.inputRequired && !String(this.inputValue || '').trim()) {
            this.inputError = '该项为必填';
            return;
          }
          ui.resolveDialog(String(this.inputValue || '').trim());
        } else {
          ui.resolveDialog(true);
        }
      }
    },
    template:
      '<div class="overlay" v-if="dialog" @click.self="cancel">' +
      '  <div class="modal" :class="dialog.wide ? \'modal--lg\' : \'modal--sm\'" role="alertdialog">' +
      '    <div class="modal__head"><div class="modal__title">{{ dialog.title }}</div>' +
      '      <button class="icon-btn" @click="cancel" title="关闭"><icon name="x" :size="16"></icon></button></div>' +
      '    <div class="modal__body">' +
      '      <p v-if="dialog.message" style="white-space:pre-wrap;margin:0">{{ dialog.message }}</p>' +
      '      <div v-html="dialog.html" v-if="dialog.html"></div>' +
      '      <div class="field mt-12" v-if="dialog.input">' +
      '        <label class="field__label" v-if="dialog.inputLabel">{{ dialog.inputLabel }}</label>' +
      '        <input class="input" :class="{\'is-invalid\': inputError}" :type="dialog.inputType" ' +
      '          :placeholder="dialog.inputPlaceholder" v-model="inputValue" @keyup.enter="accept" />' +
      '        <div class="field__hint" v-if="dialog.inputHint && !inputError">{{ dialog.inputHint }}</div>' +
      '        <div class="field__error" v-if="inputError">{{ inputError }}</div>' +
      '      </div>' +
      '    </div>' +
      '    <div class="modal__foot">' +
      '      <button class="btn" @click="cancel">{{ dialog.cancelText }}</button>' +
      '      <button class="btn" :class="dialog.danger ? \'btn--danger\' : \'btn--primary\'" @click="accept">' +
      '        {{ dialog.confirmText }}</button>' +
      '    </div>' +
      '  </div>' +
      '</div>'
  };

  var Loading = {
    name: 'Loading',
    props: { text: { type: String, default: '加载中…' } },
    template: '<div class="loading-inline"><icon name="refresh" :size="15" class="spin"></icon> {{ text }}</div>'
  };

  /* ======================================================================
     9. 视图注册表 + 启动
     ====================================================================== */
  var views = {};      // name → 组件选项（template 在 boot 时填）
  var viewFiles = {};  // name → 片段地址

  // App 外壳（侧边栏 + 顶栏 + router-view）。它不是「页面」，所以不走 defineView，
  // 但**必须**在 mount 之前填进 App.template —— 否则根组件没有模板，
  // Vue 只会渲染一个空注释节点，用户看到的是白屏（这是最容易漏的一个坑）。
  var APP_SHELL_FILE = '/assets/views/layout.html';

  function defineView(name, file, options) {
    var opts = Object.assign({ name: name, template: '<div class="card"><div class="card__body">' +
      '<loading text="页面模板未加载"></loading></div></div>' }, options || {});
    views[name] = opts;
    viewFiles[name] = file;
    return opts;
  }

  var viewsReady = null;

  function loadFragment(name, file) {
    return fetch(file, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.text();
    }).then(function (html) {
      if (views[name]) views[name].template = html;
    }).catch(function (err) {
      if (views[name]) {
        views[name].template = '<div class="page"><page-head title="页面加载失败"></page-head>' +
          '<alert type="error" message="模板片段 ' + file + ' 加载失败（' + err.message +
          '）。请刷新重试；若持续失败请检查静态资源是否被正确托管。"></alert></div>';
      }
    });
  }

  function loadAppShell() {
    return fetch(APP_SHELL_FILE, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.text();
    }).then(function (html) {
      if (html && html.replace(/\s/g, '').length > 40) App.template = html;
    }).catch(function (err) {
      // 外壳都拿不到时，至少给一个能显示原因的极简外壳，而不是白屏
      App.template = '<div class="page"><div class="card"><div class="card__body">' +
        '<h2>页面外壳加载失败</h2>' +
        '<p class="text-danger">' + APP_SHELL_FILE + ' 加载失败：' + (err && err.message) + '</p>' +
        '<p class="text-muted fs-13">请确认后端静态目录指向 web/static，并刷新页面重试。</p>' +
        '</div></div></div>';
    });
  }

  function loadFragments() {
    var jobs = Object.keys(viewFiles).map(function (name) { return loadFragment(name, viewFiles[name]); });
    jobs.push(loadAppShell());
    return Promise.all(jobs);
  }

  function timezone() {
    return state.siteValue('site.timezone', '');
  }

  var NAV_USER = [
    { group: '总览', items: [{ path: '/dashboard', label: '仪表盘', icon: 'dashboard' }] },
    {
      group: '续火花',
      items: [
        { path: '/accounts', label: '抖音账号', icon: 'smartphone' },
        { path: '/tasks', label: '定时任务', icon: 'clock' },
        { path: '/friends', label: '好友管理', icon: 'users' },
        { path: '/records', label: '执行记录', icon: 'file' }
      ]
    },
    {
      group: '套餐与订单',
      items: [
        { path: '/packages', label: '套餐购买', icon: 'box' },
        { path: '/orders', label: '我的订单', icon: 'cart' },
        { path: '/card', label: '卡密兑换', icon: 'key' }
      ]
    },
    {
      group: '账户',
      items: [
        { path: '/notifications', label: '站内通知', icon: 'bell' },
        { path: '/logs', label: '运行日志', icon: 'list' },
        { path: '/settings', label: '个人设置', icon: 'settings' }
      ]
    }
  ];

  var NAV_ADMIN = [
    { group: '管理', items: [{ path: '/admin', label: '管理概览', icon: 'chart', exact: true }] },
    {
      group: '业务',
      items: [
        { path: '/admin/users', label: '用户管理', icon: 'users' },
        { path: '/admin/accounts', label: '账号总览', icon: 'smartphone' },
        { path: '/admin/tasks', label: '任务总览', icon: 'clock' },
        { path: '/admin/records', label: '执行记录', icon: 'file' }
      ]
    },
    {
      group: '财务',
      items: [
        { path: '/admin/orders', label: '订单管理', icon: 'cart' },
        { path: '/admin/cards', label: '卡密管理', icon: 'key' },
        { path: '/admin/packages', label: '套餐管理', icon: 'box' }
      ]
    },
    {
      group: '运营与系统',
      items: [
        { path: '/admin/announcements', label: '公告管理', icon: 'megaphone' },
        { path: '/admin/configs', label: '站点配置', icon: 'sliders' },
        { path: '/admin/proxies', label: '代理管理', icon: 'globe' },
        { path: '/admin/logs', label: '运行与审计日志', icon: 'list' },
        { path: '/admin/system', label: '系统环境', icon: 'server' }
      ]
    }
  ];

  var RouterView = {
    name: 'RouterView',
    computed: {
      view: function () { return route.view; },
      // key 里带上参数/查询串：从 /tasks/1 切到 /tasks/2 时强制重建组件，避免残留旧数据
      routeKey: function () { return route.key; }
    },
    template: '<component :is="view" :key="routeKey"></component>'
  };

  var App = {
    name: 'App',
    // 注意：userMenuOpen 不放在 data 里，而是绑定共享的 uiState.userMenuOpen（见上方 computed）。
    // 因为「点空白关闭菜单」的全局监听器在组件外，两边必须读写同一份状态。
    data: function () { return { navQuery: '', logoFailed: false }; },
    computed: {
      userMenuOpen: {
        get: function () { return uiState.userMenuOpen; },
        set: function (v) { uiState.userMenuOpen = !!v; }
      },
      layoutMode: function () {
        var meta = (route.def && route.def.meta) || {};
        return (meta.guest && !state.isLogged()) ? 'auth' : 'app';
      },
      quotaPct: function () {
        var q = state.limit || state.quota || (state.user && state.user.quota) || {};
        var limit = toNum(q.account_limit, 0);
        if (!limit) return 0;
        return Math.min(100, Math.round(toNum(q.account_used, 0) * 100 / limit));
      },
      navGroups: function () { return NAV_USER; },
      adminGroups: function () { return NAV_ADMIN; }
    },
    methods: {
      isActive: function (item) { return router.isActive(item.path, item.exact); },
      toggleSidebar: function () { state.sidebarOpen = !state.sidebarOpen; },
      closeSidebar: function () { state.sidebarOpen = false; },
      toggleTheme: function () { state.toggleTheme(); },
      logout: function () {
        var self = this;
        ui.confirm({
          title: '退出登录', message: '确定要退出当前账号吗？', confirmText: '退出', danger: false
        }).then(function (okFlag) {
          if (okFlag) { self.userMenuOpen = false; api.auth.logout(); }
        });
      },
      siteFooter: function () {
        var icp = state.siteValue('site.icp', '');
        var cs = state.siteValue('site.customer_service', '');
        return { icp: icp, service: cs };
      }
    }
  };

  /* 用户菜单：点页面任意其它地方关闭。
     不用透明遮罩（那会被 sticky 顶栏的层叠上下文困住/盖住，实测导致
     「点了退出登录没反应」）。改为 document 级监听 + closest 判断，
     没有任何层级问题，也不影响其它点击。 */
  document.addEventListener('click', function (ev) {
    if (!uiState.userMenuOpen) return;
    var target = ev.target;
    if (target && target.closest && target.closest('.user-menu-wrap')) return;
    uiState.userMenuOpen = false;
  }, true);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && uiState.userMenuOpen) uiState.userMenuOpen = false;
  });

  function mountApp() {
    // 双保险：根组件没有模板就等于白屏，所以这里再判一次（boot 里已 await loadAppShell）
    if (!App.template) {
      App.template = '<div class="page"><div class="card"><div class="card__body">' +
        '<h2>页面外壳未加载</h2><p class="text-danger">App 模板为空，' + APP_SHELL_FILE +
        ' 没有被正确注入。</p></div></div></div>';
    }
    var app = V.createApp(App);
    app.component('icon', Icon);
    app.component('status-tag', StatusTag);
    app.component('copy-button', CopyButton);
    app.component('data-table', DataTable);
    app.component('pager', Pager);
    app.component('modal', Modal);
    app.component('drawer', Drawer);
    app.component('field', Field);
    app.component('alert', Alert);
    app.component('empty-state', EmptyState);
    app.component('stat-card', StatCard);
    app.component('page-head', PageHead);
    app.component('time-text', TimeText);
    app.component('money', Money);
    app.component('toast-host', ToastHost);
    app.component('confirm-dialog', ConfirmDialog);
    app.component('loading', Loading);
    app.component('router-view', RouterView);
    Object.keys(views).forEach(function (name) {
      app.component(name, views[name]);
    });

    // 全站共享的只读能力 + 状态快捷方式（页面里直接用 isAdmin / money() / api 等）
    app.config.globalProperties.api = api;
    app.config.globalProperties.ui = ui;
    app.config.globalProperties.nav = router;
    app.config.globalProperties.fmt = fmt;
    app.config.globalProperties.STATUS = STATUS;
    app.config.globalProperties.OPTIONS = OPTIONS;

    app.mixin({
      computed: {
        store: function () { return state; },
        user: function () { return state.user || {}; },
        quota: function () {
          var q = state.limit || state.quota || (state.user && state.user.quota) || {};
          return Object.assign({
            active: false, package_name: '', expire_text: '', account_limit: 0, account_used: 0,
            daily_task_limit: 0, daily_task_used: 0
          }, q);
        },
        isLogged: function () { return state.isLogged(); },
        isAdmin: function () { return state.isAdmin(); },
        siteName: function () { return state.siteValue('site.name', CFG.siteName); },
        allowRegister: function () {
          return boolOf(state.site['auth.allow_register'], CFG.allowRegister);
        },
        onlinePayEnabled: function () {
          return boolOf(state.site['pay.enabled'], CFG.onlinePayEnabled);
        },
        tz: function () { return timezone(); },
        route: function () { return route; },
        routeQuery: function () { return route.query || {}; },
        unread: function () { return state.unread; },
        appVersion: function () { return state.version || CFG.version; }
      },
      methods: {
        money: function (cents) { return fmt.money(cents); },
        timeText: function (v) { return fmt.time(v, timezone()); },
        minuteText: function (v) { return fmt.minute(v, timezone()); },
        dateText: function (v) { return fmt.date(v, timezone()); },
        agoText: function (v) { return fmt.ago(v, timezone()); },
        // 抖动窗口：任务带 ±N 分钟抖动时，实际触发时间在 [next_run_at, next_run_at+N] 之间。
        // 任务列表要把它显示出来，否则用户以为「到点了却没执行」。
        jitterEnd: function (row) {
          var base = row && row.next_run_at;
          if (!base) return '';
          var secs = Number(row.jitter_seconds) || 0;
          if (!secs) return '';
          return this.minuteText(this.jitterEndIso(row));
        },
        jitterEndIso: function (row) {
          var base = row && row.next_run_at;
          if (!base) return '';
          var secs = Number(row.jitter_seconds) || 0;
          // 后端给的是 "YYYY-MM-DD HH:mm:ss"（UTC），转成毫秒再回写成同样的格式
          var ms = Date.parse(String(base).replace(' ', 'T') + 'Z');
          if (!ms) return base;
          var d = new Date(ms + secs * 1000);
          var pad = function (n) { return (n < 10 ? '0' : '') + n; };
          return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) +
            ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
        },
        jitterMinutes: function (row) { return Math.round((Number(row && row.jitter_seconds) || 0) / 60); },
        on: function (v, d) { return boolOf(v, d); },
        statusText: function (kind, value) {
          var dict = STATUS[kind] || {};
          var hit = dict[String(value === undefined || value === null ? '' : value)];
          return hit ? hit.t : (value || '未知');
        },
        optionLabel: function (list, value) {
          var hit = (list || []).filter(function (o) { return String(o.value) === String(value); })[0];
          return hit ? hit.label : (value === undefined || value === null || value === '' ? '—' : String(value));
        },
        warn: function (m) { ui.warn(m); },
        fail: function (m) { ui.err(m); },
        succeed: function (m) { ui.ok(m); }
      }
    });

    app.mount('#app');
    state.ready = true;
  }

  function boot() {
    state.applyTheme(localStorage.getItem(LS.theme) || 'light');
    var jobs = [];
    jobs.push(state.loadSite().catch(function (err) {
      state.bootError = err && err.message ? err.message : '站点配置加载失败';
    }));
    jobs.push(loadFragments());
    jobs.push(state.loadHealth());
    if (state.token) {
      jobs.push(state.refreshUser().catch(function () {
        // token 失效：refresh 已在 api 层尝试过，这里只清理本地状态
        state.clearSession();
      }));
    }
    return Promise.all(jobs).then(function () {
      resolveRoute();
      mountApp();
      if (state.isLogged()) {
        state.loadUnread();
        state.loadLimit();
        setInterval(function () {
          if (!state.isLogged() || document.hidden) return;
          state.loadLimit();
          state.loadUnread();
        }, 60000);
      }
    });
  }

  window.Spark = {
    version: CFG.version,
    config: CFG,
    Vue: V,
    api: api,
    state: state,
    store: state,
    fmt: fmt,
    ui: ui,
    router: router,
    nav: router,
    STATUS: STATUS,
    OPTIONS: OPTIONS,
    icons: ICONS,
    views: views,
    viewFiles: viewFiles,
    defineView: defineView,
    components: {
      Icon: Icon, StatusTag: StatusTag, CopyButton: CopyButton, DataTable: DataTable, Pager: Pager,
      Modal: Modal, Drawer: Drawer, Field: Field, Alert: Alert, EmptyState: EmptyState,
      StatCard: StatCard, PageHead: PageHead, TimeText: TimeText, Money: Money
    },
    helpers: { pick: pick, toNum: toNum, boolOf: boolOf, debounce: debounce, emptyQuery: emptyQuery },
    // 只给自检脚本（web/tools/check_boot.js）用：让它能在 Node 里真跑一遍
    // 「拉取片段 → 注入 App 外壳模板」这一步，断言根组件确实拿到了模板。
    // 运行时不会被业务代码使用。
    testHooks: {
      App: App,
      loadFragments: loadFragments,
      loadAppShell: loadAppShell,
      shellFile: function () { return APP_SHELL_FILE; }
    },
    boot: boot
  };
})();
