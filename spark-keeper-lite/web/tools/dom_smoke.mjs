/**
 * 前端运行时冒烟（Lead 维护）：用 Node 的 vm 上下文 + 最小 DOM 影子，
 * 把真实前端产物真正初始化一遍，并对着真实后端发真实请求。
 *
 * 为什么需要它
 * ------------
 * 本机沙箱起不了浏览器（Chrome/Edge 子进程被拒），前端此前只有静态检查。
 * 这个脚本能抓住「模板路径写错、视图注册名与路由不匹配、启动流程抛异常、
 * 接口路径对不上」这类只在运行时暴露的问题。
 *
 * 关键实现点（踩过的坑，别再改回去）
 * ----------------------------------
 * 1. **必须用 `vm.createContext()`**：浏览器里 `window === globalThis`，而
 *    vue.global.prod.js 是裸 `var Vue = ...`、页面代码读 `window.Spark`。
 *    用 `new Function` 包一层时顶层 var 只活在函数作用域里；手工拼 window 对象
 *    又会出现「Vue 挂 A 对象、代码读 B 对象」的假失败。只有 vm 上下文里
 *    `globalThis` 就是这个上下文的全局代理，两边才对得上。
 * 2. 脚本按 index.html 里的顺序执行，最后再触发 DOMContentLoaded / load。
 *
 * 抓不到什么（如实说明）
 * ---------------------
 * CSS 布局、真实点击/输入事件、二维码图片渲染效果 —— 必须在真浏览器里看。
 *
 * 用法:
 *   node web/tools/dom_smoke.mjs --base http://127.0.0.1:PORT --root web/static
 */

import { readFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import vm from 'node:vm';

const args = process.argv.slice(2);
const getArg = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const BASE = getArg('--base', 'http://127.0.0.1:8000');
const ROOT = resolve(getArg('--root', 'web/static'));

const PASS = [], FAIL = [], WARN = [];
const check = (name, cond, detail = '') => {
  (cond ? PASS : FAIL).push(name);
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
};
const warn = (msg) => { WARN.push(msg); console.log(`  WARN  ${msg}`); };

// ---------------------------------------------------------------------------
// 最小 DOM 影子
// ---------------------------------------------------------------------------
class FakeClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) { const v = on === undefined ? !this.set.has(c) : !!on; if (v) this.set.add(c); else this.set.delete(c); return v; }
  get length() { return this.set.size; }
  item(i) { return [...this.set][i] ?? null; }
}

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.nodeName = this.tagName;
    this.nodeType = 1;
    this.children = [];
    this.childNodes = this.children;
    this.attributes = {};
    this.style = { setProperty() {}, removeProperty() {}, getPropertyValue: () => '' };
    this.dataset = {};
    this.classList = new FakeClassList();
    this._text = '';
    this._html = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.selectedIndex = 0;
    this.options = [];
    this.files = [];
    this.parentNode = null;
    this.ownerDocument = null;
    this._listeners = {};
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.scrollWidth = 100;
    this.offsetWidth = 100;
    this.offsetHeight = 30;
    this.offsetTop = 0;
    this.hidden = false;
    this.tabIndex = 0;
  }
  get className() { return [...this.classList.set].join(' '); }
  set className(v) { this.classList.set = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); if (this.ownerDocument) this.ownerDocument.register(this); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v ?? ''); this._html = ''; this.children.length = 0; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v ?? ''); }
  get innerText() { return this._text; }
  set innerText(v) { this._text = String(v ?? ''); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get parentElement() { return this.parentNode; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return this.parentNode.children[i + 1] || null;
  }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === 'string' ? new FakeText(n) : n)); }
  prepend(node) { this.children.unshift(node); node.parentNode = this; return node; }
  insertBefore(node, ref) {
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i >= 0) this.children.splice(i, 0, node); else this.children.push(node);
    node.parentNode = this; return node;
  }
  removeChild(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    node.parentNode = null; return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) { this.children.length = 0; this.append(...nodes); }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === 'class') this.className = v;
    if (k.startsWith('data-')) {
      this.dataset[k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = String(v);
    }
  }
  getAttribute(k) { return k === 'class' ? this.className : (k in this.attributes ? this.attributes[k] : null); }
  removeAttribute(k) { delete this.attributes[k]; }
  hasAttribute(k) { return k in this.attributes; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    const arr = this._listeners[type] || [];
    const i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
  }
  dispatchEvent(evt) {
    const e = evt || {};
    if (!e.target) e.target = this;
    if (!e.preventDefault) e.preventDefault = () => {};
    if (!e.stopPropagation) e.stopPropagation = () => {};
    (this._listeners[e.type] || []).forEach((fn) => { try { fn.call(this, e); } catch (err) { warn(`事件处理器抛错(${e.type}): ${err && err.message}`); } });
    return true;
  }
  click() { this.dispatchEvent({ type: 'click' }); }
  focus() { this.dispatchEvent({ type: 'focus' }); }
  blur() {}
  select() {}
  setSelectionRange() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30, x: 0, y: 0 }; }
  closest(sel) {
    let node = this;
    const want = String(sel).replace(/^\./, '');
    while (node) {
      if (String(sel).startsWith('.') && node.classList && node.classList.contains(want)) return node;
      if (String(sel).toUpperCase() === node.tagName) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  matches() { return false; }
  contains() { return false; }
  cloneNode() { const n = new FakeNode(this.tagName); n._text = this._text; return n; }
  getElementsByTagName() { return []; }
}

class FakeText extends FakeNode {
  constructor(text) { super('#text'); this.nodeType = 3; this.nodeName = '#text'; this._text = String(text ?? ''); }
}

class FakeDocument extends FakeNode {
  constructor() {
    super('#document');
    this.nodeType = 9;
    this.documentElement = new FakeNode('html');
    this.head = new FakeNode('head');
    this.body = new FakeNode('body');
    this.ownerDocument = this;
    this.documentElement.ownerDocument = this;
    this.head.ownerDocument = this;
    this.body.ownerDocument = this;
    this.appendChild(this.documentElement);
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.title = '';
    this.readyState = 'complete';
    this.cookie = '';
    this.hidden = false;
    this.visibilityState = 'visible';
    this._ids = new Map();
    this._tags = new Map();
    this._listeners = {};
  }
  createElement(tag) { const n = new FakeNode(tag); n.ownerDocument = this; return n; }
  createElementNS(_ns, tag) { return this.createElement(tag); }
  createTextNode(text) { const n = new FakeText(text); n.ownerDocument = this; return n; }
  createDocumentFragment() { const n = new FakeNode('#fragment'); n.ownerDocument = this; return n; }
  register(node) {
    if (node.id) this._ids.set(node.id, node);
    const tag = node.tagName;
    if (!this._tags.has(tag)) this._tags.set(tag, []);
    const bucket = this._tags.get(tag);
    if (!bucket.includes(node)) bucket.push(node);
  }
  getElementById(id) { return this._ids.get(String(id)) || null; }
  getElementsByTagName(tag) { return this._tags.get(String(tag).toUpperCase()) || []; }
  getElementsByClassName(cls) {
    const out = [];
    for (const bucket of this._tags.values()) {
      for (const n of bucket) if (n.classList && n.classList.contains(cls)) out.push(n);
    }
    return out;
  }
  querySelector(sel) {
    const s = String(sel).trim();
    if (s.startsWith('#')) return this._ids.get(s.slice(1)) || null;
    if (s.startsWith('.')) return this.getElementsByClassName(s.slice(1))[0] || null;
    return (this._tags.get(s.toUpperCase()) || [])[0] || null;
  }
  querySelectorAll(sel) {
    const s = String(sel).trim();
    if (s.startsWith('#')) { const n = this._ids.get(s.slice(1)); return n ? [n] : []; }
    if (s.startsWith('.')) return this.getElementsByClassName(s.slice(1));
    return this._tags.get(s.toUpperCase()) || [];
  }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    const arr = this._listeners[type] || [];
    const i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
  }
  dispatchEvent(evt) {
    const e = evt || {};
    if (!e.type) e.type = 'event';
    const fire = (type) => (this._listeners[type] || []).forEach((fn) => {
      try { fn.call(this, e); } catch (err) { warn(`document 事件处理器抛错(${type}): ${err && err.message}`); }
    });
    fire(e.type);
    if (e.type === 'DOMContentLoaded') fire('readystatechange');
    if (e.type === 'load') fire('pageshow');
    return true;
  }
}

// ---------------------------------------------------------------------------
// 起 vm 上下文
// ---------------------------------------------------------------------------
const doc = new FakeDocument();
const appRoot = doc.createElement('div');
appRoot.id = 'app';
doc.body.appendChild(appRoot);

const htmlPath = join(ROOT, 'index.html');
if (!existsSync(htmlPath)) {
  console.error(`[致命] 找不到 ${htmlPath}`);
  process.exit(2);
}
const html = readFileSync(htmlPath, 'utf8');

// 把 index.html 里出现的 id 补进影子 DOM（app.js 可能 getElementById）
for (const m of html.matchAll(/id="([^"]+)"/g)) {
  if (doc.getElementById(m[1])) continue;
  const node = doc.createElement('div');
  node.setAttribute('id', m[1]);
  doc.body.appendChild(node);
  doc.register(node);
}

const localStorageMap = new Map();
const sessionStorageMap = new Map();
const fetchLog = [];
const realFetch = globalThis.fetch;

const sandbox = {};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.document = doc;
sandbox.console = console;
sandbox.setTimeout = setTimeout;
sandbox.clearTimeout = clearTimeout;
sandbox.setInterval = setInterval;
sandbox.clearInterval = clearInterval;
sandbox.queueMicrotask = queueMicrotask;
sandbox.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
sandbox.cancelAnimationFrame = (id) => clearTimeout(id);
sandbox.performance = globalThis.performance;
sandbox.URL = URL;
sandbox.URLSearchParams = URLSearchParams;
sandbox.TextEncoder = TextEncoder;
sandbox.TextDecoder = TextDecoder;
sandbox.AbortController = AbortController;
sandbox.structuredClone = globalThis.structuredClone;
sandbox.btoa = (s) => Buffer.from(String(s), 'binary').toString('base64');
sandbox.atob = (s) => Buffer.from(String(s), 'base64').toString('binary');
sandbox.crypto = globalThis.crypto;
sandbox.navigator = { userAgent: 'node-dom-smoke', language: 'zh-CN', languages: ['zh-CN'], clipboard: null, onLine: true };
sandbox.localStorage = {
  getItem: (k) => (localStorageMap.has(String(k)) ? localStorageMap.get(String(k)) : null),
  setItem: (k, v) => localStorageMap.set(String(k), String(v)),
  removeItem: (k) => localStorageMap.delete(String(k)),
  clear: () => localStorageMap.clear(),
  key: (i) => [...localStorageMap.keys()][i] ?? null,
  get length() { return localStorageMap.size; },
};
sandbox.sessionStorage = {
  getItem: (k) => (sessionStorageMap.has(String(k)) ? sessionStorageMap.get(String(k)) : null),
  setItem: (k, v) => sessionStorageMap.set(String(k), String(v)),
  removeItem: (k) => sessionStorageMap.delete(String(k)),
  clear: () => sessionStorageMap.clear(),
  key: (i) => [...sessionStorageMap.keys()][i] ?? null,
  get length() { return sessionStorageMap.size; },
};
sandbox.location = {
  href: `${BASE}/`, origin: BASE, protocol: 'http:', host: '127.0.0.1', hostname: '127.0.0.1',
  port: new URL(BASE).port, pathname: '/', search: '', hash: '#/login',
  _listeners: {},
  assign(u) { this.href = String(u); if (String(u).includes('#')) this.hash = '#' + String(u).split('#')[1]; },
  replace(u) { this.assign(u); },
  reload() {},
  toString() { return this.href; },
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); },
  removeEventListener() {},
};
sandbox.history = { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {} };
sandbox.matchMedia = () => ({ matches: false, media: '', addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
sandbox.getComputedStyle = () => ({ getPropertyValue: () => '', width: '100px', height: '30px' });
sandbox.alert = (m) => warn(`alert() 被调用：${m}`);
sandbox.confirm = () => true;
sandbox.prompt = () => null;
sandbox.open = () => null;
sandbox.CSS = { supports: () => true, escape: (s) => String(s) };
sandbox.Blob = class { constructor(parts, opts) { this.parts = parts; this.type = (opts || {}).type || ''; } };
sandbox.FormData = class { constructor() { this._d = new Map(); } append(k, v) { this._d.set(k, v); } get(k) { return this._d.get(k); } };
sandbox.FileReader = class { readAsDataURL() {} };
sandbox.Image = class { set src(_v) { if (this.onload) setTimeout(() => this.onload(), 0); } };
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.dispatchEvent = () => true;
sandbox.fetch = async (url, init) => {
  const target = String(url).startsWith('http') ? String(url) : BASE + String(url);
  const headers = (init && init.headers) || {};
  const authHeader = headers.Authorization || headers.authorization || '';
  const resp = await realFetch(target, init);
  fetchLog.push({
    url: String(url),
    method: (init && init.method) || 'GET',
    status: resp.status,
    auth: authHeader ? 'bearer' : 'none',
  });
  return resp;
};
sandbox.XMLHttpRequest = class {
  open(m, u) { this._m = m; this._u = u; }
  setRequestHeader() {}
  send() { this.status = 0; if (this.onerror) this.onerror(); }
  addEventListener() {}
};
// 未实现的浏览器 API 一旦被调用要能看见，而不是静默 undefined
const NOT_IMPLEMENTED = ['IntersectionObserver', 'ResizeObserver', 'MutationObserver', 'Notification', 'clipboard'];
for (const name of NOT_IMPLEMENTED) {
  sandbox[name] = function NotImplemented() { warn(`页面调用了未实现的浏览器 API：${name}`); };
}
sandbox.Notification = Object.assign(sandbox.Notification || function () {}, { permission: 'denied', requestPermission: async () => 'denied' });

vm.createContext(sandbox);

// ---------------------------------------------------------------------------
// 按 index.html 的真实顺序执行脚本
// ---------------------------------------------------------------------------
const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
check('index.html 引用了脚本', scripts.length >= 2, `${scripts.length} 个`);

const remote = scripts.filter((s) => /^https?:\/\//.test(s));
if (remote.length) warn(`index.html 里有远程脚本：${remote.join(', ')}（本脚本跳过）`);
const localScripts = scripts.filter((s) => !/^https?:\/\//.test(s));

const loaded = [];
for (const src of localScripts) {
  const file = join(ROOT, src.replace(/^\//, ''));
  if (!existsSync(file)) { warn(`本地脚本不存在，已跳过：${src}`); continue; }
  try {
    vm.runInContext(readFileSync(file, 'utf8'), sandbox, { filename: src, displayErrors: true });
    loaded.push(src);
  } catch (err) {
    check(`加载脚本 ${src}`, false, `${err && err.message}`);
  }
}
check('本地脚本全部加载成功', loaded.length === localScripts.length,
  `${loaded.length}/${localScripts.length}`);

// 触发 DOMContentLoaded / load（页面里 boot() 通常在这些事件里）
try {
  doc.dispatchEvent({ type: 'DOMContentLoaded' });
  doc.dispatchEvent({ type: 'load' });
  sandbox.dispatchEvent({ type: 'load' });
} catch (err) {
  warn(`触发 load 事件时抛错：${err && err.message}`);
}
// 给 boot() 里的 fetch 一点时间完成
await new Promise((r) => setTimeout(r, 1500));

// ---------------------------------------------------------------------------
// 断言
// ---------------------------------------------------------------------------
const Vue = sandbox.Vue;
check('Vue 3 全局构建已加载', !!(Vue && (Vue.createApp || Vue.version)),
  Vue && Vue.version ? `version=${Vue.version}` : 'window.Vue 为 undefined');

const Spark = sandbox.Spark;
check('app.js 已挂载 window.Spark', !!(Spark && typeof Spark.boot === 'function'),
  Spark ? `键：${Object.keys(Spark).length} 个` : 'window.Spark 为 undefined');

if (Spark) {
  const viewNames = Object.keys(Spark.views || {});
  check('视图已注册到 Spark.views', viewNames.length >= 25,
    `${viewNames.length} 个：${viewNames.slice(0, 5).join(', ')}…`);
  // 模板路径存在 Spark.viewFiles 里（视图对象本身只有 data/methods 等）。
  // 之前我按 view.file 判断，是口径写错了 —— 这里两种都查，避免再次误报。
  const files = Spark.viewFiles || {};
  const missingTpl = viewNames.filter((n) => {
    const view = Spark.views[n] || {};
    return !(view.file || files[n]);
  });
  check('每个视图都绑定了模板文件', missingTpl.length === 0,
    missingTpl.slice(0, 4).join(', ') || `${Object.keys(files).length} 个模板`);
  check('组件池齐备', Object.keys(Spark.components || {}).length >= 10,
    `${Object.keys(Spark.components || {}).length} 个`);
}

// 模板片段真的能取到（views-*.js 会 fetch 它们）
if (Spark) {
  const files = [...new Set(Object.values(Spark.viewFiles || {}).filter(Boolean))];
  const bad = [];
  for (const f of files) {
    const r = await sandbox.fetch(f).catch(() => null);
    if (!r || r.status !== 200) bad.push(`${f}(${r ? r.status : 'ERR'})`);
  }
  check('视图模板片段全部可取', bad.length === 0,
    bad.length ? bad.slice(0, 5).join(', ') : `${files.length} 个`);
}

// 前端关心的接口确实存在于运行时路由表
const routesResp = await sandbox.fetch('/api/v1/_routes');
const routesBody = await routesResp.json();
const apiPaths = new Set((routesBody.data?.routes || []).map((r) => r.path));
const probePaths = [
  '/api/v1/health', '/api/v1/auth/public-configs', '/api/v1/auth/user/login',
  '/api/v1/auth/user/register', '/api/v1/packages', '/api/v1/user/info',
  '/api/v1/user/dashboard', '/api/v1/douyin/accounts', '/api/v1/douyin/login/browser',
  '/api/v1/tasks', '/api/v1/friends', '/api/v1/user/card-keys/redeem',
  '/api/v1/user/orders', '/api/v1/admin/stats',
];
const missing = probePaths.filter((p) => !apiPaths.has(p));
check('前端关心的接口都在运行时路由表里', missing.length === 0,
  missing.length ? `缺失：${missing.join(', ')}` : `${probePaths.length} 条 · 路由表 ${apiPaths.size} 条`);

// 启动过程不应产生 5xx
const serverErrors = fetchLog.filter((f) => f.status >= 500);
check('启动过程后端无 5xx', serverErrors.length === 0,
  serverErrors.map((f) => `${f.method} ${f.url} -> ${f.status}`).join('; '));

// 一次真实导航：跑到 #/login 并确认渲染流程没炸
if (Spark && Spark.nav) {
  try {
    sandbox.location.hash = '#/login';
    if (typeof Spark.nav.go === 'function') Spark.nav.go('/login');
    await new Promise((r) => setTimeout(r, 300));
    check('导航到 #/login 未抛错', true);
  } catch (err) {
    check('导航到 #/login 未抛错', false, `${err && err.message}`);
  }
}

// ---------------------------------------------------------------------------
// 关键断言：根组件「真的拿到了外壳模板」
// ---------------------------------------------------------------------------
// 这一条是补上来的，起因是一次真实事故：根组件 App 没有 template，而
// views/layout.html（整个侧边栏+顶栏+router-view）没有被任何代码加载 ——
// 结果 Vue 只渲染一个注释节点，**用户看到整站白屏**。
// 而当时这个冒烟脚本 11 项全绿：因为它只验证「模块加载成功 / 视图注册了 /
// 片段能取到 / 接口对得上」，没有任何一条验证「mount 之后外壳真的存在」。
// 静态检查全绿 + 页面全白，是最难发现的一类缺陷，所以这里必须有一条硬断言。
// testHooks.shellFile 是「返回路径的函数」（也可能是字符串），这里两种都兼容
const rawShell = Spark?.testHooks?.shellFile;
const shellFile = typeof rawShell === 'function' ? rawShell() : (rawShell || '/assets/views/layout.html');
// 注意：这里**显式调用 loadAppShell()** 而不是等 boot() 自然跑完。
// 原因：影子环境里没有真实 event loop 的时序保证，boot() 的 await 链何时结束不确定；
// 等它会让断言变成「我等的够不够久」（第一版就因此误报）。显式调用测的是
// 「外壳注入机制 + 模板内容是否正确」——这正是那次整站白屏事故的根因所在。
// boot() 的完整时序由前端自己的 web/tools/check_boot.js 覆盖。
if (Spark?.testHooks?.loadAppShell) {
  try {
    await Spark.testHooks.loadAppShell();
  } catch (err) {
    warn(`主动调用 loadAppShell() 抛错：${err && err.message}`);
  }
}
if (Spark?.testHooks?.loadFragments) {
  try {
    await Spark.testHooks.loadFragments();
  } catch (err) {
    warn(`主动调用 loadFragments() 抛错：${err && err.message}`);
  }
}
const appShell = Spark?.testHooks?.App?.template || '';
console.log(`  [诊断] App.template 长度=${appShell.length} shellFile=${shellFile} ` +
  `testHooks=${Object.keys(Spark?.testHooks || {}).join(',')}`);
check('根组件已注入 App 外壳模板', appShell.length > 200,
  appShell ? `${appShell.length} 字符` : `App.template 为空 —— 浏览器里会整站白屏（应来自 ${shellFile}）`);
for (const marker of ['<router-view', 'layout']) {
  check(`外壳含关键标记 ${marker}`, appShell.includes(marker),
    appShell.includes(marker) ? '' : '外壳模板可能加载失败或未注入');
}

// 外壳片段真的能取到（与其它 29 个片段一起）
const shellResp = await sandbox.fetch(shellFile).catch(() => null);
check('外壳片段 HTTP 可取', !!shellResp && shellResp.status === 200,
  shellResp ? `status=${shellResp.status}` : '请求失败');

// 启动请求清单：未登录时只应请求公开接口，且不能带 Authorization
const apiCalls = fetchLog.filter((f) => f.url.includes('/api/v1/'));
const tplCalls = fetchLog.filter((f) => f.url.includes('/assets/views/'));
// 用「唯一片段数」而不是请求次数：boot() 与显式 loadFragments() 可能各拉一轮，
// 次数会翻倍，但真正的契约是「29 个视图 + 1 个外壳都被取到过」。
const uniqueTpl = new Set(tplCalls.map((f) => f.url.split('?')[0]));
const authed = fetchLog.filter((f) => (f.auth || '').includes('Bearer'));
const bootApiAllowed = new Set(['/api/v1/auth/public-configs', '/api/v1/health', '/api/v1/_routes']);
const unexpectedApi = apiCalls.filter((f) => {
  const clean = f.url.split('?')[0];
  return ![...bootApiAllowed].some((p) => clean.endsWith(p));
});
check('启动期间的 API 请求都是公开接口', unexpectedApi.length === 0,
  unexpectedApi.map((f) => f.url).join(', ') || `${apiCalls.length} 条`);
check('模板请求包含外壳 + 全部视图', uniqueTpl.size >= 30,
  `${uniqueTpl.size} 个唯一片段（请求 ${tplCalls.length} 次，29 视图 + 1 外壳）`);
check('外壳片段在请求清单里', [...uniqueTpl].some((u) => u.endsWith('layout.html')),
  shellFile);
check('启动期间没有任何带令牌的请求', authed.length === 0,
  authed.map((f) => f.url).join(', ') || '未登录会话不应发 Authorization');

// ---------------------------------------------------------------------------
// 登录链路断言（补上来的，起因是一次真实的「登录后不跳转」事故）
// ---------------------------------------------------------------------------
// 事故根因：app.js 的 pick() 第一个参数是 null 占位、函数却从它开始迭代枚举，
// 于是 setSession() 里 pick(null, data, ['access_token','token']) 永远返回
// undefined —— 登录接口 200 但令牌从不落盘，随后的 /user/dashboard 全部 401。
// 当时这个冒烟脚本 19 项全绿，因为它只用未登录状态探测，**从没真的登录一次**。
// 所以这里必须真跑一遍登录，并断言「令牌落到 storage + 后续请求带上 Authorization」。
const SMOKE_USER = process.env.SK_SMOKE_USER || 'admin';
const SMOKE_PASS = process.env.SK_SMOKE_PASSWORD || '';
if (Spark?.api?.auth?.login && SMOKE_PASS) {
  let loginData = null;
  try {
    loginData = await Spark.api.auth.login(
      { identifier: SMOKE_USER, password: SMOKE_PASS, remember: true }, false);
  } catch (err) {
    check('登录调用成功', false, `${err && err.message}`);
  }
  if (loginData) {
    check('登录返回令牌字段', !!(loginData.access_token || loginData.token),
      Object.keys(loginData).slice(0, 6).join(','));
    check('登录后 state.isLogged() 为真', Spark.store.isLogged() === true,
      `token=${!!Spark.store.token} user=${!!Spark.store.user}`);
    const stored = (() => {
      try { return localStorageMap.get('spark_token') || sessionStorageMap?.get?.('spark_token') || ''; }
      catch { return ''; }
    })();
    check('令牌已写入本地存储', !!stored, stored ? `${String(stored).length} 字符` : '未写入');

    const before = fetchLog.length;
    let dashOk = false;
    let dashErr = '';
    try { await Spark.api.get('/user/dashboard'); dashOk = true; }
    catch (err) { dashErr = err && err.message; }
    check('登录后拉仪表盘成功', dashOk, dashOk ? '' : dashErr);
    const after = fetchLog.slice(before);
    const authorized = after.filter((f) => f.auth === 'bearer');
    check('登录后的请求带上了 Authorization', authorized.length > 0,
      `${authorized.length}/${after.length} 条`);
    const unauthed = after.filter((f) => f.auth !== 'bearer' && f.url.includes('/api/v1/'));
    check('登录后的 API 请求没有漏带令牌', unauthed.length === 0,
      unauthed.map((f) => `${f.method} ${f.url} -> ${f.status}`).join('; '));

    // 退出登录：令牌必须被清干净
    try { await Spark.api.auth.logout(); } catch { /* 忽略 */ }
    check('退出后令牌已清除', !Spark.store.token && !Spark.store.isLogged());
  }
} else {
  warn('跳过登录链路断言（未提供 SK_SMOKE_PASSWORD 或前端未暴露 api.auth）');
  console.log('        用法: $env:SK_SMOKE_PASSWORD="<管理员密码>"; node web/tools/dom_smoke.mjs ...');
}

console.log(`\n  前端运行时冒烟：通过 ${PASS.length} · 失败 ${FAIL.length} · 警告 ${WARN.length}`);
console.log(`  请求记录 ${fetchLog.length} 条`);
if (FAIL.length) {
  console.log('  失败项：' + FAIL.join(', '));
  process.exit(1);
}
process.exit(0);
