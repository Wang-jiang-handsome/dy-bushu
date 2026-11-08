/**
 * 诊断：前端登录后立刻拉 /user/dashboard 为什么会 401。
 *
 * 在 Node 的 vm 上下文里真跑前端产物（与 dom_smoke.mjs 同一套影子 DOM），
 * 打印每个请求的 URL + 是否带 Authorization + 状态码，把「前端没带令牌」与
 * 「后端拒绝令牌」两种情况区分开。
 *
 * 用法：
 *   node web/tools/diagnose_login.mjs --base http://127.0.0.1:8099 --root web/static \
 *        --user admin --pass ProbeAdmin123
 */

import { readFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import vm from 'node:vm';

const args = process.argv.slice(2);
const getArg = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const BASE = getArg('--base', 'http://127.0.0.1:8000');
const ROOT = resolve(getArg('--root', 'web/static'));
const USER = getArg('--user', 'admin');
const PASS = getArg('--pass', '');

// ---- 极简 DOM 影子（够 app.js 启动用；与 dom_smoke.mjs 保持同一思路）----
class N {
  constructor(t) {
    this.tagName = String(t || 'div').toUpperCase(); this.nodeType = 1; this.children = [];
    this.childNodes = this.children; this.attributes = {}; this.style = { setProperty() {}, removeProperty() {}, getPropertyValue: () => '' };
    this.dataset = {}; this._text = ''; this._html = ''; this.value = ''; this.checked = false;
    this.selectedIndex = 0; this.options = []; this.files = []; this.parentNode = null; this._listeners = {};
    this.offsetWidth = 100; this.offsetHeight = 30; this.scrollTop = 0; this.hidden = false;
    this.classList = { set: new Set(), add(...c) { c.forEach((x) => x && this.set.add(x)); }, remove(...c) { c.forEach((x) => this.set.delete(x)); }, contains: (c) => this.set.has(c), toggle() { return false; } };
  }
  get className() { return [...this.classList.set].join(' '); }
  set className(v) { this.classList.set = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); }
  get textContent() { return this._text; } set textContent(v) { this._text = String(v ?? ''); }
  get innerHTML() { return this._html; } set innerHTML(v) { this._html = String(v ?? ''); }
  get innerText() { return this._text; } set innerText(v) { this._text = String(v ?? ''); }
  get firstChild() { return this.children[0] || null; }
  get parentElement() { return this.parentNode; }
  appendChild(n) { this.children.push(n); n.parentNode = this; return n; }
  append(...n) { n.forEach((x) => this.appendChild(typeof x === 'string' ? new N('#text') : x)); }
  prepend(n) { this.children.unshift(n); return n; }
  insertBefore(n) { this.children.push(n); return n; }
  removeChild(n) { const i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); return n; }
  remove() {} replaceChildren() {} setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'class') this.className = v; }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; } removeAttribute(k) { delete this.attributes[k]; }
  hasAttribute(k) { return k in this.attributes; }
  addEventListener(t, f) { (this._listeners[t] ||= []).push(f); } removeEventListener() {}
  dispatchEvent(e) { (this._listeners[e.type] || []).forEach((f) => f.call(this, e)); return true; }
  click() { this.dispatchEvent({ type: 'click' }); } focus() {} blur() {} select() {} setSelectionRange() {}
  scrollIntoView() {} getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30 }; }
  closest() { return null; } querySelector() { return null; } querySelectorAll() { return []; }
  matches() { return false; } contains() { return false; } cloneNode() { return new N(this.tagName); }
}
class D extends N {
  constructor() {
    super('#document'); this.nodeType = 9; this.documentElement = new N('html'); this.head = new N('head'); this.body = new N('body');
    this.appendChild(this.documentElement); this.documentElement.appendChild(this.head); this.documentElement.appendChild(this.body);
    this.title = ''; this.readyState = 'complete'; this.cookie = ''; this.hidden = false; this.visibilityState = 'visible';
    this._ids = new Map(); this._tags = new Map(); this._l = {};
  }
  createElement(t) { return new N(t); } createElementNS(_n, t) { return new N(t); } createTextNode() { return new N('#text'); }
  createDocumentFragment() { return new N('#fragment'); }
  getElementById(id) { return this._ids.get(String(id)) || null; }
  getElementsByTagName(t) { return this._tags.get(String(t).toUpperCase()) || []; }
  getElementsByClassName() { return []; }
  querySelector(s) { return String(s).startsWith('#') ? (this._ids.get(String(s).slice(1)) || null) : null; }
  querySelectorAll() { return []; }
  addEventListener(t, f) { (this._l[t] ||= []).push(f); } removeEventListener() {}
  dispatchEvent(e) { (this._l[e.type] || []).forEach((f) => f.call(this, e)); return true; }
}

const doc = new D();
const appRoot = doc.createElement('div'); appRoot.id = 'app'; doc.body.appendChild(appRoot); doc._ids.set('app', appRoot);
const html = readFileSync(join(ROOT, 'index.html'), 'utf8');
for (const m of html.matchAll(/id="([^"]+)"/g)) { if (!doc._ids.has(m[1])) { const n = doc.createElement('div'); n.id = m[1]; doc.body.appendChild(n); doc._ids.set(m[1], n); } }

// ---- 请求日志：这是本诊断的核心 ----
const LOG = [];
const mem = new Map();
const realFetch = globalThis.fetch;
const sandbox = {};
sandbox.globalThis = sandbox; sandbox.window = sandbox; sandbox.self = sandbox;
sandbox.document = doc; sandbox.console = console;
sandbox.setTimeout = setTimeout; sandbox.clearTimeout = clearTimeout;
sandbox.setInterval = setInterval; sandbox.clearInterval = clearInterval;
sandbox.queueMicrotask = queueMicrotask;
sandbox.requestAnimationFrame = (f) => setTimeout(() => f(Date.now()), 0);
sandbox.cancelAnimationFrame = (i) => clearTimeout(i);
sandbox.performance = globalThis.performance; sandbox.URL = URL; sandbox.URLSearchParams = URLSearchParams;
sandbox.TextEncoder = TextEncoder; sandbox.TextDecoder = TextDecoder; sandbox.AbortController = AbortController;
sandbox.btoa = (s) => Buffer.from(String(s), 'binary').toString('base64');
sandbox.atob = (s) => Buffer.from(String(s), 'base64').toString('binary');
sandbox.crypto = globalThis.crypto;
sandbox.navigator = { userAgent: 'node-login-diag', language: 'zh-CN', languages: ['zh-CN'], onLine: true };
sandbox.localStorage = {
  getItem: (k) => (mem.has('L:' + k) ? mem.get('L:' + k) : null),
  setItem: (k, v) => mem.set('L:' + k, String(v)),
  removeItem: (k) => mem.delete('L:' + k), clear: () => mem.clear(),
  key: (i) => [...mem.keys()].filter((x) => x.startsWith('L:'))[i] ?? null,
  get length() { return [...mem.keys()].filter((x) => x.startsWith('L:')).length; },
};
sandbox.sessionStorage = {
  getItem: (k) => (mem.has('S:' + k) ? mem.get('S:' + k) : null),
  setItem: (k, v) => mem.set('S:' + k, String(v)),
  removeItem: (k) => mem.delete('S:' + k), clear: () => {},
  key: () => null, get length() { return 0; },
};
sandbox.location = {
  href: `${BASE}/`, origin: BASE, protocol: 'http:', host: '127.0.0.1', hostname: '127.0.0.1',
  port: new URL(BASE).port, pathname: '/', search: '', hash: '#/login',
  _l: {}, assign(u) { this.href = String(u); }, replace(u) { this.assign(u); }, reload() {},
  toString() { return this.href; },
  addEventListener(t, f) { (this._l[t] ||= []).push(f); }, removeEventListener() {},
};
sandbox.history = { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {} };
sandbox.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
sandbox.getComputedStyle = () => ({ getPropertyValue: () => '' });
sandbox.alert = (m) => console.log('  [alert]', m);
sandbox.confirm = () => true; sandbox.prompt = () => null; sandbox.open = () => null;
sandbox.CSS = { supports: () => true, escape: (s) => String(s) };
sandbox.Blob = class { constructor(p, o) { this.parts = p; this.type = (o || {}).type || ''; } };
sandbox.FormData = class { append() {} get() { return null; } };
sandbox.FileReader = class {}; sandbox.Image = class { set src(_v) {} };
sandbox.addEventListener = () => {}; sandbox.removeEventListener = () => {}; sandbox.dispatchEvent = () => true;
sandbox.fetch = async (url, init) => {
  const target = String(url).startsWith('http') ? String(url) : BASE + String(url);
  const hdrs = (init && init.headers) || {};
  const auth = hdrs.Authorization || hdrs.authorization || '';
  const res = await realFetch(target, init);
  LOG.push({ url: String(url), method: (init && init.method) || 'GET', status: res.status, auth: auth ? auth.slice(0, 24) + '…' : '(none)' });
  return res;
};
vm.createContext(sandbox);

// ---- 按 index.html 顺序加载脚本 ----
for (const m of html.matchAll(/<script[^>]+src="([^"]+)"/g)) {
  const src = m[1];
  if (/^https?:\/\//.test(src)) continue;
  const file = join(ROOT, src.replace(/^\//, ''));
  if (!existsSync(file)) continue;
  try { vm.runInContext(readFileSync(file, 'utf8'), sandbox, { filename: src }); }
  catch (e) { console.log(`  [加载失败] ${src}: ${e.message}`); }
}
doc.dispatchEvent({ type: 'DOMContentLoaded' });
doc.dispatchEvent({ type: 'load' });
await new Promise((r) => setTimeout(r, 1200));

const Spark = sandbox.Spark;
if (!Spark) { console.log('✗ window.Spark 未挂载'); process.exit(2); }
console.log('  Spark 已加载，testHooks:', Object.keys(Spark.testHooks || {}).join(','));
console.log('  state.isLogged() =', Spark.store.isLogged(), '| token 存储 =', mem.has('L:spark_token') ? 'localStorage' : (mem.has('S:spark_token') ? 'sessionStorage' : '无'));

console.log('\n  === 登录前请求 ===');
LOG.forEach((l) => console.log(`    ${l.method.padEnd(4)} ${l.status}  auth=${l.auth}  ${l.url}`));
LOG.length = 0;

console.log(`\n  === 调用 api.auth.login({identifier:"${USER}"}) ===`);
let loginData = null, loginErr = null;
try {
  loginData = await Spark.api.auth.login({ identifier: USER, password: PASS, remember: true }, false);
} catch (e) { loginErr = e; }
console.log('  登录结果:', loginErr ? `✗ ${loginErr.message}` : '✓ 成功');
if (loginData) console.log('  返回字段:', Object.keys(loginData).join(','));
console.log('  state.token 已设置 =', !!Spark.store.token, '| state.user =', !!Spark.store.user, '| isLogged =', Spark.store.isLogged());
console.log('  localStorage.spark_token =', mem.get('L:spark_token') ? '有' : '无');
LOG.forEach((l) => console.log(`    ${l.method.padEnd(4)} ${l.status}  auth=${l.auth}  ${l.url}`));
LOG.length = 0;

console.log('\n  === 登录后立刻拉 /user/dashboard（这正是你看到的 401）===');
let dashErr = null, dashData = null;
try { dashData = await Spark.api.get('/user/dashboard'); } catch (e) { dashErr = e; }
LOG.forEach((l) => console.log(`    ${l.method.padEnd(4)} ${l.status}  auth=${l.auth}  ${l.url}`));
console.log('  结果:', dashErr ? `✗ ${dashErr.message} (code=${dashErr.code})` : '✓ 200');

console.log('\n  === 结论 ===');
const dashReq = LOG[0];
if (!dashReq) console.log('  ⚠ 没有发出 dashboard 请求');
else if (dashReq.auth === '(none)') console.log('  ✗ 前端没有把 Authorization 带上 → 前端令牌存储/读取问题');
else if (dashReq.status === 401) console.log('  ✗ 带了令牌但被后端拒绝 → 令牌/会话/密钥问题');
else console.log('  ✓ 请求正常');
process.exit(0);
