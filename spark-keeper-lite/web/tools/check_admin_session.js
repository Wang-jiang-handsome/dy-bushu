/**
 * 管理员会话「刷新页面」后是否还在（行为检查，不需要真实浏览器）。
 *
 * 真实事故：管理员登录后台，刷新一下页面就变成普通用户界面（侧边栏变用户菜单、
 * 访问 /admin 被踢回 /dashboard）。
 *
 * 关键点：刷新时的会话恢复走 `GET /user/info`，该接口返回的是**扁平用户对象**
 * （`{code:0,data:{...user...}}`，`data` 本身就是 user）。如果前端用
 * `pick(null, data, ['user','profile'])` 之类的方式取，拿不到键就会退回默认值，
 * 把 `state.user` 写成非用户对象 → `state.user.role` 丢失 → `isAdmin()` 变 false
 * → 管理员被当成普通用户（或直接被守卫踢出 /admin）。
 *
 * 本脚本用与 check_boot.js 相同的最小 DOM + fetch 桩，真跑 app.js 的 boot()，
 * 断言「刷新后 role/isAdmin 仍然正确」。这样即使以后有人改回错误写法，
 * 也会在自检里立刻暴露。
 *
 * 用法：
 *   node web/tools/check_admin_session.js [--root web/static]
 * 退出码 0 = 通过。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const args = process.argv.slice(2);
const rootIdx = args.indexOf('--root');
const WEB = rootIdx >= 0 && args[rootIdx + 1]
  ? path.resolve(args[rootIdx + 1])
  : path.resolve(__dirname, '..');
const STATIC = path.join(WEB, 'static');

const PASSED = [];
const FAILED = [];
const check = (name, ok, detail = '') => {
  (ok ? PASSED : FAILED).push(name);
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
};

function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => map.set(String(k), String(v)),
    removeItem: (k) => map.delete(String(k)),
    clear: () => map.clear(),
    key: (i) => Array.from(map.keys())[i] || null,
    get length() { return map.size; }
  };
}

const ADMIN = {
  id: 1, username: 'admin', nickname: 'admin', role: 'admin', status: 'active',
  email: '', phone: '', avatar: '', balance_cents: 0, quota: null
};
const DEMO = { ...ADMIN, id: 2, username: 'demo', nickname: 'demo', role: 'user' };

function buildEnv({ user, hash, apiUser }) {
  const fetched = [];
  const storageLocal = makeStorage();
  if (user) {
    storageLocal.setItem('spark_token', 'fake-access-token');
    storageLocal.setItem('spark_refresh', 'fake-refresh-token');
    storageLocal.setItem('spark_user', JSON.stringify(user));
  }

  function fetchStub(url) {
    const clean = String(url).split('?')[0];
    fetched.push(clean);
    const json = (data, status = 200) => Promise.resolve({
      ok: status < 400, status,
      headers: { get: () => 'application/json' },
      text: () => Promise.resolve(JSON.stringify(data)),
      json: () => Promise.resolve(data)
    });
    const file = path.join(STATIC, clean.replace(/^\//, ''));
    if ((clean.startsWith('/assets/') || clean.startsWith('/vendor/')) &&
        fs.existsSync(file) && fs.statSync(file).isFile()) {
      const body = fs.readFileSync(file, 'utf8');
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(body) });
    }
    // 关键桩：/user/info 返回扁平用户对象，和真实后端一致
    if (/\/user\/info$/.test(clean)) return json({ code: 0, message: 'ok', data: apiUser || user });
    if (/\/site\/(config|public)/.test(clean)) {
      return json({ code: 0, message: 'ok', data: { 'site.name': '自检' } });
    }
    if (/\/health$/.test(clean)) return json({ code: 0, message: 'ok', data: { ok: true } });
    if (/limit-status$/.test(clean)) return json({ code: 0, message: 'ok', data: {} });
    if (/unread/.test(clean)) return json({ code: 0, message: 'ok', data: { count: 0 } });
    return json({ code: 0, message: 'ok', data: {} });
  }

  const elementStub = {
    style: {}, dataset: {}, classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null, appendChild() {}, removeChild() {},
    insertBefore() {}, addEventListener() {}, removeEventListener() {}, click() {},
    querySelector: () => null, querySelectorAll: () => [],
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = v; }, get textContent() { return this._text || ''; }
  };

  const sandbox = {
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    Promise, Intl, JSON, Math, Date, URL, Number, String, Boolean, Array, Object,
    Error, RegExp, parseInt, parseFloat, isNaN,
    TextEncoder, TextDecoder, Blob: class {}, Event: class {}, CustomEvent: class {},
    fetch: fetchStub,
    localStorage: storageLocal,
    sessionStorage: makeStorage(),
    addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
    location: {
      hash, href: 'http://127.0.0.1/' + hash, origin: 'http://127.0.0.1',
      protocol: 'http:', host: '127.0.0.1', pathname: '/', search: '',
      assign() {}, replace() {}, reload() {}
    },
    history: { pushState() {}, replaceState() {}, back() {} },
    isSecureContext: false,
    scrollTo() {},
    navigator: { userAgent: 'check_admin_session' },
    __SPARK__: { apiBase: '/api/v1', siteName: '自检', version: '0.0.0' },
    document: {
      documentElement: elementStub, head: elementStub, body: elementStub,
      createElement: () => Object.assign({}, elementStub),
      createElementNS: () => Object.assign({}, elementStub),
      createTextNode: () => ({}),
      getElementById: () => null,      // 不真正 mount，避免需要完整 DOM
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener() {}
    }
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;

  const context = vm.createContext(sandbox);
  const indexHtml = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const vueSrc = fs.readFileSync(path.join(STATIC, 'vendor', 'vue.global.prod.js'), 'utf8');
  vm.runInContext(vueSrc + '\n;window.Vue = Vue;', context, { filename: 'vue.global.prod.js' });
  const scriptRe = /<script\s+src="([^"]+)"/g;
  let m;
  while ((m = scriptRe.exec(indexHtml))) {
    const src = m[1];
    const file = path.join(STATIC, src.replace(/^\//, ''));
    if (!fs.existsSync(file)) continue;
    vm.runInContext(fs.readFileSync(file, 'utf8'), context, { filename: src });
  }
  const Spark = sandbox.window.Spark;
  if (!Spark) throw new Error('app.js 没有导出 window.Spark');
  return { Spark, fetched };
}

(async () => {
  console.log('== 管理员刷新后是否仍是管理员 ==');

  // 场景 1：管理员已登录，刷新页面（hash 停在 #/admin）
  let env1;
  try {
    env1 = buildEnv({ user: ADMIN, hash: '#/admin', apiUser: ADMIN });
    await Promise.resolve(env1.Spark.boot());
  } catch (err) {
    console.log('  FAIL  应用启动抛异常：' + err.message);
    process.exit(1);
  }
  const s1 = env1.Spark.state;
  const u1 = s1.user;
  check('刷新后 state.user 仍是用户对象（不是数组/字符串）',
        !!u1 && typeof u1 === 'object' && !Array.isArray(u1),
        Array.isArray(u1) ? '实际是数组：' + JSON.stringify(u1) : '实际类型：' + typeof u1);
  check('刷新后 role 仍是 admin', !!u1 && u1.role === 'admin',
        '实际 role=' + (u1 && u1.role));
  check('刷新后 isAdmin() 仍为 true', typeof s1.isAdmin === 'function' && s1.isAdmin() === true);
  check('刷新时确实重新拉了 /user/info',
        env1.fetched.some((u) => /\/user\/info$/.test(u)),
        env1.fetched.filter((u) => /user\//.test(u)).join(','));

  // 场景 2：普通用户刷新，不能被误判为管理员
  let env2;
  try {
    env2 = buildEnv({ user: DEMO, hash: '#/dashboard', apiUser: DEMO });
    await Promise.resolve(env2.Spark.boot());
  } catch (err) {
    console.log('  FAIL  普通用户场景启动抛异常：' + err.message);
    process.exit(1);
  }
  const s2 = env2.Spark.state;
  check('普通用户刷新后 isAdmin() 为 false', s2.isAdmin() === false,
        'role=' + (s2.user && s2.user.role));
  check('普通用户刷新后 role 保持 user', !!s2.user && s2.user.role === 'user',
        '实际 role=' + (s2.user && s2.user.role));

  console.log('');
  console.log(`通过 ${PASSED.length} 项，失败 ${FAILED.length} 项`);
  if (FAILED.length) {
    console.log('失败项：' + FAILED.join(' / '));
    process.exit(1);
  }
})();
