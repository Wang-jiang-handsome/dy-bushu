/* ==========================================================================
   check_boot.js —— 真跑一遍「启动时把模板注入组件」这一步，断言根组件有外壳

   为什么必须有这个脚本
   --------------------
   曾经真实发生过：`views/layout.html`（App 外壳：侧边栏 + 顶栏 + router-view）
   没有任何代码去 fetch，根组件 `App` 没有 template。结果是：
     * 所有静态检查全绿（模板能编译、片段文件存在、组件都注册了）
     * 静态资源全 200（layout.html 也能被 HTTP 取到）
     * 但整站白屏 —— Vue 对没有 template/render 的根组件只渲染一个注释节点。
   静态检查抓不到「谁没被加载」，所以这里改成**行为验证**：
     1. 用 fetch 桩把片段从磁盘喂给 app.js（不联网）；
     2. 调 Spark.testHooks.loadFragments()（就是 boot() 里那一步）；
     3. 断言 App.template 真的被填成了外壳，且外壳里有 .layout / <router-view> / toast-host；
     4. 断言 29 个视图组件的 template 都不是占位串。

   用法：
     & "C:\Users\huawei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" ^
       web\tools\check_boot.js
     # 可选：--root <dir> 检查另一份副本（负向测试用）

   分工（别把两边当成重复实现而删掉其中一个）
   ----------------------------------------
   * 本脚本：用 **fetch 桩 + vm** 覆盖 `boot()` 的时序与「模板注入」这一步，
     不连真实后端，因此可以在任何环境跑、结果稳定。
   * `web/tools/dom_smoke.mjs`（Lead 维护）：连**真实后端**，覆盖真实 HTTP、
     接口白名单、跨进程行为与「外壳片段真能取到」。
   两者互补：一个管时序确定性，一个管真实链路。`selftest_guards.py` 会同时验证两者都真的会报错。
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');

const rootIndex = process.argv.indexOf('--root');
const WEB = rootIndex >= 0 && process.argv[rootIndex + 1]
  ? path.resolve(process.argv[rootIndex + 1])
  : path.resolve(__dirname, '..');
const STATIC = path.join(WEB, 'static');
const VENDOR = path.join(STATIC, 'vendor', 'vue.global.prod.js');
const APP_JS = path.join(STATIC, 'assets', 'app.js');

const problems = [];
const info = [];

/* ---------------- 最小浏览器环境 ---------------- */
function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(String(k), String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear()
  };
}

// fetch 桩：把 /assets/... 映射到磁盘；其余（接口）返回一个空的成功响应
const fetched = [];
function fetchStub(url) {
  const clean = String(url).split('?')[0];
  fetched.push(clean);
  let filePath = null;
  if (clean.indexOf('/assets/') === 0) filePath = path.join(STATIC, clean.replace(/^\//, ''));
  else if (clean.indexOf('/vendor/') === 0) filePath = path.join(STATIC, clean.replace(/^\//, ''));
  if (filePath) {
    if (!fs.existsSync(filePath)) {
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('') });
    }
    const body = fs.readFileSync(filePath, 'utf8');
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(body) });
  }
  // 接口：一律返回 code:0 的空数据，boot 期间的接口调用不影响本检查
  return Promise.resolve({
    ok: true, status: 200,
    text: () => Promise.resolve(JSON.stringify({ code: 0, message: 'ok', data: {} })),
    headers: { get: () => 'application/json' }
  });
}

const sandbox = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Intl, JSON, Math, Date, URL,
  TextEncoder, TextDecoder,
  fetch: fetchStub,
  localStorage: makeStorage(),
  sessionStorage: makeStorage(),
  addEventListener() {},
  location: { hash: '', href: 'http://127.0.0.1:8011/', replace() {}, assign() {} },
  isSecureContext: false,
  scrollTo() {},
  navigator: { userAgent: 'check_boot' },
  __SPARK__: { apiBase: '/api/v1', siteName: '自检', version: '0.0.0' }
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
const elementStub = {
  style: {}, dataset: {}, classList: { add() {}, remove() {}, contains: () => false },
  setAttribute() {}, getAttribute: () => null, appendChild() {}, removeChild() {},
  addEventListener() {}, removeEventListener() {}, click() {},
  querySelector: () => null, querySelectorAll: () => [],
  set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; },
  set textContent(v) { this._text = v; }, get textContent() { return this._text || ''; }
};
sandbox.document = {
  documentElement: elementStub,
  head: elementStub,
  body: elementStub,
  createElement: () => Object.assign({}, elementStub),
  createElementNS: () => Object.assign({}, elementStub),
  createTextNode: () => ({}),
  getElementById: () => null,          // 关键：不让它 mount（本脚本只验证模板注入）
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}
};

/* ---------------- 按 index.html 的真实顺序加载脚本 ---------------- */
const vm = require('vm');
const context = vm.createContext(sandbox);

function runScript(source, filename) {
  try {
    vm.runInContext(source, context, { filename });
    return true;
  } catch (err) {
    problems.push(`${filename} 执行失败：${err.message}`);
    return false;
  }
}

const indexHtml = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
// 1) 先加载 Vue（index.html 里是 <script src="/vendor/vue.global.prod.js">）
const vueSource = fs.readFileSync(VENDOR, 'utf8');
runScript(vueSource + '\n;window.Vue = (typeof Vue !== "undefined" ? Vue : window.Vue);',
  'vendor/vue.global.prod.js');
if (!sandbox.window.Vue || !sandbox.window.Vue.version) {
  problems.push('Vue 全局构建没有加载出 window.Vue');
} else {
  info.push(`Vue ${sandbox.window.Vue.version} 已加载`);
}

// 2) 再按 index.html 里的顺序加载 app.js 与各页面模块（模拟浏览器）
const scriptSrcs = [];
const scriptRe = /<script\s+src="([^"]+)"/g;
let sm;
while ((sm = scriptRe.exec(indexHtml))) scriptSrcs.push(sm[1]);
info.push(`index.html 引用脚本 ${scriptSrcs.length} 个：${scriptSrcs.map(function (s) {
  return s.split('/').pop();
}).join(', ')}`);

scriptSrcs.forEach(function (src) {
  const file = path.join(STATIC, src.replace(/^\//, ''));
  if (!fs.existsSync(file)) {
    problems.push(`index.html 引用的脚本不存在：${src}`);
    return;
  }
  runScript(fs.readFileSync(file, 'utf8'), src);
});

const Spark = sandbox.window.Spark;
if (!Spark) {
  problems.push('app.js 没有挂上 window.Spark');
} else {
  info.push(`window.Spark 已挂载，键 ${Object.keys(Spark).length} 个`);
}

/* ---------------- 真跑 loadFragments() ---------------- */
(async function main() {
  let failed = problems.length > 0;

  if (Spark && Spark.testHooks && typeof Spark.testHooks.loadFragments === 'function') {
    try {
      await Spark.testHooks.loadFragments();
    } catch (err) {
      problems.push('loadFragments() 抛异常：' + err.message);
      failed = true;
    }
  } else if (Spark) {
    problems.push('Spark.testHooks.loadFragments 不存在（app.js 被改坏或没导出测试钩子）');
    failed = true;
  }

  const shellFile = Spark && Spark.testHooks && Spark.testHooks.shellFile
    ? Spark.testHooks.shellFile() : '/assets/views/layout.html';
  const AppTemplate = Spark && Spark.testHooks ? (Spark.testHooks.App || {}).template : '';

  // 1) 根组件必须有模板
  if (!AppTemplate || AppTemplate.replace(/\s/g, '').length < 40) {
    problems.push(`根组件 App 没有拿到模板（期望来自 ${shellFile}）→ 浏览器里会整站白屏`);
    failed = true;
  } else {
    info.push(`App 模板已注入（${AppTemplate.length} 字符）`);
    // 2) 外壳必须含关键结构
    const mustHave = [
      ['class="layout"', '侧边栏布局容器'],
      ['<router-view', '页面挂载点'],
      ['<toast-host', 'toast 容器'],
      ['<confirm-dialog', '二次确认弹窗'],
      ['class="sidebar"', '侧边栏']
    ];
    mustHave.forEach(function (pair) {
      if (AppTemplate.indexOf(pair[0]) < 0) {
        problems.push(`App 外壳里缺少 ${pair[0]}（${pair[1]}）`);
        failed = true;
      }
    });
  }

  // 3) 所有视图组件的模板都要被真实内容替换掉（不能还是「页面模板未加载」占位）
  const views = (Spark && Spark.views) || {};
  const names = Object.keys(views);
  if (names.length !== 29) {
    problems.push(`注册视图数量异常：${names.length}（期望 29）`);
    failed = true;
  }
  let placeholderCount = 0;
  names.forEach(function (name) {
    const tpl = views[name].template || '';
    if (tpl.indexOf('页面模板未加载') >= 0 || tpl.replace(/\s/g, '').length < 40) {
      problems.push(`视图 ${name} 的模板没有被注入（仍是占位内容）`);
      placeholderCount += 1;
      failed = true;
    }
  });
  info.push(`视图组件 ${names.length} 个，模板占位未注入 ${placeholderCount} 个`);

  // 4) 片段请求数应与「视图数 + 外壳」一致
  const fragmentRequests = fetched.filter(function (u) { return u.indexOf('/assets/views/') === 0; });
  info.push(`启动时拉取的模板片段 ${fragmentRequests.length} 个（视图 ${names.length} + 外壳 1）`);
  if (fragmentRequests.length !== names.length + 1) {
    problems.push(`片段请求数 ${fragmentRequests.length} ≠ 视图数 + 1（${names.length + 1}），可能有片段没被加载`);
    failed = true;
  }
  if (fragmentRequests.indexOf(shellFile) < 0) {
    problems.push(`启动时没有请求外壳 ${shellFile}`);
    failed = true;
  }

  const width = 84;
  console.log('='.repeat(width));
  console.log('启动流程行为自检（真跑 loadFragments → 断言 App 外壳模板已注入）');
  console.log('='.repeat(width));
  info.forEach(function (line) { console.log('  · ' + line); });
  console.log('-'.repeat(width));
  if (problems.length) {
    console.log(`发现 ${problems.length} 个错误：`);
    problems.forEach(function (line) { console.log('  ✗ ' + line); });
  } else {
    console.log('App 外壳模板注入正常、29 个视图模板全部就位、片段请求数一致 ✅');
  }
  console.log('-'.repeat(width));
  process.exit(failed ? 1 : 0);
})();
