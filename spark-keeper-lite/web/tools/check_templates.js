/* ==========================================================================
   check_templates.js —— 前端静态自检（用 Vue 自己的编译器验证所有页面模板）

   为什么需要它
   ------------
   前端没有构建步骤，模板写在 .html 片段里、运行时才编译。写错一个标签、
   用错一个图标名、v-model 绑了不存在的字段，浏览器里才会炸（而且是白屏）。
   这个脚本在 Node 里加载本地 `vue.global.prod.js` 的编译器，把所有片段真正
   编译一遍，并交叉检查：
     1. 所有模板能否编译（未闭合标签、非法指令、重复 attribute）
     2. 组件名是否已注册（防止写了 <data-table2> 这种不存在的组件）
     3. <icon name="xxx"> 是否在 ICONS 里
     4. <status-tag kind="xxx"> / statusText('xxx') / OPTIONS.xxx 的字典键是否存在
     5. defineView(name, file) 引用的片段文件是否存在
     6. 模板里 v-model / @click 引用的根标识符是否在该页面组件的 JS 里出现过（尽力而为）

   用法（Node 必须用完整路径，它不在 PATH）：
     & "C:\Users\huawei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" ^
       web\tools\check_templates.js
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');

// --root <dir>：对另一份 web 目录（例如负向测试用的临时副本）执行同样的检查
const rootArgIndex = process.argv.indexOf('--root');
const WEB = rootArgIndex >= 0 && process.argv[rootArgIndex + 1]
  ? path.resolve(process.argv[rootArgIndex + 1])
  : path.resolve(__dirname, '..');
const STATIC = path.join(WEB, 'static');
const ASSETS = path.join(STATIC, 'assets');
const VIEWS = path.join(ASSETS, 'views');
const VENDOR = path.join(STATIC, 'vendor', 'vue.global.prod.js');

const problems = [];
const warnings = [];
const info = [];

function fail(where, message) { problems.push(`${where}: ${message}`); }
function warn(where, message) { warnings.push(`${where}: ${message}`); }

/* ---------------- 1. 加载 Vue 编译器 ---------------- */
// Vue 的浏览器构建在解析 HTML 实体时会用 document.createElement('div') + innerHTML
// （decodeHtmlBrowser）。Node 里没有 DOM，所以这里补一个「够用」的实体解码 shim，
// 否则任何含 &gt; / &#10; / &nbsp; 的模板都会在编译阶段抛 TypeError（假错误）。
const NAMED_ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
  copy: '\u00a9', reg: '\u00ae', trade: '\u2122', hellip: '\u2026',
  mdash: '\u2014', ndash: '\u2013', middot: '\u00b7', times: '\u00d7',
  laquo: '\u00ab', raquo: '\u00bb', ldquo: '\u201c', rdquo: '\u201d',
  lsquo: '\u2018', rsquo: '\u2019', yen: '\u00a5', deg: '\u00b0'
};

function decodeEntities(raw) {
  return String(raw).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);/g, function (whole, body) {
    if (body.charAt(0) === '#') {
      const isHex = body.charAt(1) === 'x' || body.charAt(1) === 'X';
      const code = parseInt(isHex ? body.slice(2) : body.slice(1), isHex ? 16 : 10);
      return isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, body) ? NAMED_ENTITIES[body] : whole;
  });
}

function fakeElement() {
  let html = '';
  const element = {
    style: {},
    setAttribute() {},
    appendChild() {},
    set innerHTML(value) {
      html = String(value);
      // Vue 的 decodeHtmlBrowser(asAttr=true) 会塞 `<div foo="...">` 再取属性值
      const attrMatch = html.match(/^<div foo="([\s\S]*)">$/);
      element._attrValue = attrMatch ? decodeEntities(attrMatch[1]) : null;
      element._text = attrMatch ? '' : decodeEntities(html);
    },
    get innerHTML() { return html; },
    get textContent() { return element._text || ''; },
    get children() {
      return [{ getAttribute: function () { return element._attrValue; } }];
    },
    get childNodes() {
      return [{ nodeValue: element._text || '' }];
    }
  };
  return element;
}

let Vue = null;
try {
  global.window = global.window || { navigator: { userAgent: 'node' }, addEventListener() {} };
  global.document = global.document || {
    createElement: fakeElement,
    createElementNS: fakeElement,
    createTextNode: () => ({ nodeValue: '' }),
    querySelector: () => null,
    addEventListener() {}
  };
  const source = fs.readFileSync(VENDOR, 'utf8');
  // 全局构建用 `var Vue = function(e){...}({})` 形式，直接在模块作用域里 eval
  const module = { exports: {} };
  const factory = new Function('module', 'exports', 'window', 'document',
    source + '\n;module.exports = (typeof Vue !== "undefined" ? Vue : undefined);');
  factory(module, module.exports, global.window, global.document);
  Vue = module.exports && module.exports.compile ? module.exports : (global.Vue || null);
} catch (err) {
  console.error('无法加载 Vue 编译器：' + err.message);
  process.exit(2);
}
if (!Vue || typeof Vue.compile !== 'function') {
  console.error('Vue 编译器不可用（compile 不是函数），跳过模板编译检查。');
  process.exit(2);
}
// 编译器生成的渲染函数里会引用全局 `Vue`（浏览器里由 <script> 提供），Node 里手动挂上
global.Vue = Vue;
info.push(`Vue 编译器已加载：${Vue.version || '未知版本'}`);

/* ---------------- 2. 读源码并抽取字典 ---------------- */
const appJs = fs.readFileSync(path.join(ASSETS, 'app.js'), 'utf8');

function extractIconNames(source) {
  const block = source.match(/var ICONS = \{([\s\S]*?)\n  \};/);
  if (!block) return new Set();
  const names = new Set();
  const re = /(?:^|\n)\s*'?([A-Za-z][A-Za-z0-9_-]*)'?:\s*'/g;
  let m;
  while ((m = re.exec(block[1]))) names.add(m[1]);
  return names;
}

function extractStatusKinds(source) {
  const block = source.match(/var STATUS = \{([\s\S]*?)\n  \};/);
  if (!block) return new Map();
  const kinds = new Map();
  // 逐个顶层键；值为 { key: {t,k} ... }
  const re = /(?:^|\n)\s{4}([A-Za-z][A-Za-z0-9_]*):\s*\{([\s\S]*?)\n    \}/g;
  let m;
  while ((m = re.exec(block[1]))) {
    const keys = new Set();
    const keyRe = /([A-Za-z_][A-Za-z0-9_]*|''|\"\")?\s*:\s*\{\s*t:/g;
    let k;
    while ((k = keyRe.exec(m[2]))) {
      let name = k[1];
      if (name === "''" || name === '""') name = '';
      if (name !== undefined) keys.add(name);
    }
    kinds.set(m[1], keys);
  }
  return kinds;
}

function extractOptionsKeys(source) {
  const block = source.match(/var OPTIONS = \{([\s\S]*?)\n  \};/);
  if (!block) return new Set();
  const keys = new Set();
  const re = /(?:^|\n)\s{4}([A-Za-z][A-Za-z0-9_]*):/g;
  let m;
  while ((m = re.exec(block[1]))) keys.add(m[1]);
  return keys;
}

const ICONS = extractIconNames(appJs);
const STATUS = extractStatusKinds(appJs);
const OPTIONS_KEYS = extractOptionsKeys(appJs);
info.push(`字典：ICONS ${ICONS.size} 个图标 · STATUS ${STATUS.size} 组 · OPTIONS ${OPTIONS_KEYS.size} 组`);

// 已注册的全局组件（app.js 里 app.component('x', ...) + 页面组件名）
const GLOBAL_COMPONENTS = new Set(['icon', 'status-tag', 'copy-button', 'data-table', 'pager',
  'modal', 'drawer', 'field', 'alert', 'empty-state', 'stat-card', 'page-head', 'time-text',
  'money', 'toast-host', 'confirm-dialog', 'loading', 'router-view']);
const PAGE_COMPONENTS = new Set(['view-login', 'view-register', 'view-forgot', 'view-dashboard',
  'view-accounts', 'view-account-detail', 'view-tasks', 'view-task-edit', 'view-friends',
  'view-records', 'view-packages', 'view-orders', 'view-card', 'view-logs', 'view-settings',
  'view-notifications', 'view-admin-dashboard', 'view-admin-users', 'view-admin-cards',
  'view-admin-packages', 'view-admin-orders', 'view-admin-accounts', 'view-admin-tasks',
  'view-admin-records', 'view-admin-logs', 'view-admin-announcements', 'view-admin-configs',
  'view-admin-proxies', 'view-admin-system']);

// HTML 原生标签（含 svg 里的）
const HTML_TAGS = new Set(('html,head,body,title,meta,link,script,style,noscript,div,span,p,a,img,br,hr,' +
  'ul,ol,li,dl,dt,dd,table,thead,tbody,tfoot,tr,th,td,caption,colgroup,col,' +
  'form,input,button,select,option,optgroup,textarea,label,fieldset,legend,output,' +
  'h1,h2,h3,h4,h5,h6,header,footer,main,aside,nav,section,article,figure,figcaption,' +
  'strong,em,b,i,u,s,small,sub,sup,mark,abbr,cite,code,pre,kbd,samp,var,blockquote,q,' +
  'video,audio,source,track,canvas,svg,path,circle,rect,line,polyline,polygon,ellipse,g,defs,' +
  'lineargradient,stop,text,tspan,use,symbol,clipPath,mask,pattern,filter,' +
  'template,slot,component,transition,transitiongroup,keepalive,teleport,suspense,' +
  'details,summary,dialog,progress,meter,time,address,iframe,object,embed,param,map,area,' +
  'picture,figure,ins,del,data,dfn,ruby,rt,rp,wbr,bdi,bdo,hr').split(',').map(function (s) {
  return s.trim().toLowerCase();
}));

/* ---------------- 3. 收集 defineView 注册信息 ---------------- */
const viewFiles = new Map();   // name → { file, jsFile }
const jsFiles = fs.readdirSync(ASSETS).filter(function (f) { return f.endsWith('.js'); });
jsFiles.forEach(function (file) {
  const source = fs.readFileSync(path.join(ASSETS, file), 'utf8');
  const re = /defineView\(\s*'([^']+)'\s*,\s*'([^']+)'/g;
  let m;
  while ((m = re.exec(source))) viewFiles.set(m[1], { file: m[2], jsFile: file });
});

/* ---------------- 4. 逐片段编译 + 交叉检查 ---------------- */
const fragments = fs.readdirSync(VIEWS).filter(function (f) { return f.endsWith('.html'); });
const usedComponents = new Set();

// 需要配对的标签（Vue 的编译器对「末尾漏闭合」很宽容，会自动补上，
// 但漏一个 </div> 往往意味着结构错了、样式会串位，所以这里自己配平检查）
const BALANCE_TAGS = new Set(('div,template,section,table,thead,tbody,tfoot,tr,td,th,form,fieldset,' +
  'aside,nav,header,main,footer,label,pre,code,span,button,a,p,ul,ol,li,h1,h2,h3,h4,h5,h6,' +
  'select,optgroup,textarea,figure,article,dl,dt,dd,strong,em,small,blockquote,details,summary').split(','));

function checkTagBalance(html, where) {
  const stripped = html.replace(/<!--[\s\S]*?-->/g, '');
  const tagRe = /<(\/?)([A-Za-z][A-Za-z0-9-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/g;
  const stack = [];
  let m;
  while ((m = tagRe.exec(stripped))) {
    const closing = m[1] === '/';
    const tag = m[2].toLowerCase();
    const selfClosed = m[4] === '/';
    if (!BALANCE_TAGS.has(tag) || selfClosed) continue;
    if (!closing) { stack.push(tag); continue; }
    const top = stack.pop();
    if (top !== tag) {
      fail(where, `标签不匹配：遇到 </${tag}>，但当前未闭合的是 <${top || '无'}>`);
      return;
    }
  }
  if (stack.length) {
    fail(where, `有 ${stack.length} 个标签未闭合：${stack.slice(-6).map(function (t) {
      return '<' + t + '>';
    }).join(' ')}`);
  }
}

fragments.forEach(function (fragment) {
  const full = path.join(VIEWS, fragment);
  const html = fs.readFileSync(full, 'utf8');
  const where = 'views/' + fragment;

  // 4.1 编译
  try {
    Vue.compile(html);
  } catch (err) {
    fail(where, '模板编译失败：' + err.message);
  }

  // 4.1b 标签配平（编译器对漏闭合过于宽容，这里单独查）
  checkTagBalance(html, where);

  // 4.2 组件名
  const tagRe = /<([A-Za-z][A-Za-z0-9-]*)/g;
  let t;
  while ((t = tagRe.exec(html))) {
    const tag = t[1];
    const lower = tag.toLowerCase();
    if (lower.indexOf('view-') === 0) usedComponents.add(lower);
    if (HTML_TAGS.has(lower)) continue;
    if (GLOBAL_COMPONENTS.has(lower) || PAGE_COMPONENTS.has(lower)) { usedComponents.add(lower); continue; }
    fail(where, `未知标签 <${tag}>（既不是 HTML 标签，也不在已注册组件里）`);
  }

  // 4.3 图标名
  const iconRe = /<icon[^>]*\bname="([A-Za-z0-9_-]+)"/g;
  let i;
  while ((i = iconRe.exec(html))) {
    if (!ICONS.has(i[1])) fail(where, `图标 "${i[1]}" 不在 ICONS 字典里`);
  }

  // 4.4 status-tag kind + statusText('kind') + OPTIONS.kind
  const kindRe = /<status-tag[^>]*\bkind="([A-Za-z0-9_]+)"/g;
  let k;
  while ((k = kindRe.exec(html))) {
    if (!STATUS.has(k[1])) fail(where, `status-tag kind="${k[1]}" 不在 STATUS 字典里`);
  }
  const statusTextRe = /statusText\(\s*'([A-Za-z0-9_]+)'/g;
  while ((k = statusTextRe.exec(html))) {
    if (!STATUS.has(k[1])) fail(where, `statusText('${k[1]}') 不在 STATUS 字典里`);
  }
  const optionRe = /OPTIONS\.([A-Za-z0-9_]+)/g;
  while ((k = optionRe.exec(html))) {
    if (!OPTIONS_KEYS.has(k[1])) fail(where, `OPTIONS.${k[1]} 不存在`);
  }
  // 同时检查 JS 里的引用（注意要用「片段文件名完全相同」来定位，避免 packages.html 命中 admin-packages.html）
  const owner = Array.from(viewFiles.entries()).filter(function (entry) {
    return entry[1].file.replace(/^\/assets\/views\//, '') === fragment;
  }).map(function (entry) { return entry[1]; })[0];
  if (owner) {
    const js = fs.readFileSync(path.join(ASSETS, owner.jsFile), 'utf8');
    const optionRe2 = /OPTIONS\.([A-Za-z0-9_]+)/g;
    while ((k = optionRe2.exec(js))) {
      if (!OPTIONS_KEYS.has(k[1])) fail(owner.jsFile, `OPTIONS.${k[1]} 不存在`);
    }
    const statusRe2 = /statusText\(\s*'([A-Za-z0-9_]+)'/g;
    while ((k = statusRe2.exec(js))) {
      if (!STATUS.has(k[1])) fail(owner.jsFile, `statusText('${k[1]}') 不在 STATUS 字典里`);
    }
  }

  // 4.5 v-model 根标识符是否在该片段所属 JS 里出现（尽力而为，只给警告）
  if (owner) {
    const js = fs.readFileSync(path.join(ASSETS, owner.jsFile), 'utf8');
    const modelRe = /v-model(?:\.\w+)?="([A-Za-z_$][A-Za-z0-9_$]*)/g;
    let mv;
    const missing = new Set();
    while ((mv = modelRe.exec(html))) {
      const root = mv[1];
      // 框架注入的全局（store/route/user/...）不算页面字段
      if (['store', 'route', 'user', 'quota', 'fmt', 'api', 'ui', 'nav'].indexOf(root) >= 0) continue;
      if (!new RegExp('\\b' + root + '\\b').test(js)) missing.add(root);
    }
    if (missing.size) {
      warn(where, `v-model 绑定的字段在 ${owner.jsFile} 里没有出现：${Array.from(missing).join(', ')}`);
    }
  }
});

/* ---------------- 5. defineView 引用的片段是否存在 ---------------- */
viewFiles.forEach(function (value, name) {
  const rel = value.file.replace(/^\/assets\//, '');
  const full = path.join(ASSETS, rel);
  if (!fs.existsSync(full)) fail(value.jsFile, `defineView('${name}') 引用的片段不存在：${value.file}`);
});

/* ---------------- 5.1 每个片段都必须被引用（孤儿片段 = 永远不显示） ----------------
   教训：views/layout.html（App 外壳）一开始没有任何代码去 fetch，静态检查全绿，
   但根组件没有 template → 整站白屏。所以这里强制「views/ 下每个 .html 都被引用」。 */
const shellMatch = appJs.match(/APP_SHELL_FILE\s*=\s*'([^']+)'/);
const referencedFragments = new Set();
viewFiles.forEach(function (value) {
  referencedFragments.add(value.file.replace(/^\/assets\/views\//, ''));
});
if (shellMatch) referencedFragments.add(shellMatch[1].replace(/^\/assets\/views\//, ''));
fragments.forEach(function (fragment) {
  if (!referencedFragments.has(fragment)) {
    fail('views/' + fragment, '孤儿片段：没有任何 defineView / APP_SHELL_FILE 引用它，永远不会被加载');
  }
});

/* ---------------- 5.2 根组件必须拿到外壳模板 ---------------- */
if (!shellMatch) {
  fail('app.js', '找不到 APP_SHELL_FILE 常量：App 外壳不会被加载（根组件无模板 = 白屏）');
} else {
  if (!/App\.template\s*=/.test(appJs)) {
    fail('app.js', 'app.js 里没有 App.template = ... 赋值：根组件无模板时会白屏');
  }
  const shellFile = shellMatch[1].replace(/^\/assets\//, '');
  if (!fs.existsSync(path.join(ASSETS, shellFile))) {
    fail('app.js', `APP_SHELL_FILE 指向的文件不存在：/assets/${shellFile}`);
  }
  // 外壳必须真的有 router-view，否则页面永远渲染不出来
  const shellHtml = fs.existsSync(path.join(ASSETS, shellFile))
    ? fs.readFileSync(path.join(ASSETS, shellFile), 'utf8') : '';
  if (shellHtml && !/<router-view/.test(shellHtml)) {
    fail(shellFile, '外壳里没有 <router-view>：页面组件没有挂载点');
  }
}

/* ---------------- 6. 路由里的 view 是否都注册过 ---------------- */
const routeRe = /\{\s*path:\s*'([^']+)'[^}]*view:\s*'([^']+)'/g;
let r;
while ((r = routeRe.exec(appJs))) {
  if (!viewFiles.has(r[2])) fail('app.js', `路由 ${r[1]} 指向的组件 ${r[2]} 没有注册`);
}

/* ---------------- 7. index.html 引用的脚本是否存在 ---------------- */
const indexHtml = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const srcRe = /src="(\/assets\/[^"]+|\/vendor\/[^"]+)"/g;
let s;
while ((s = srcRe.exec(indexHtml))) {
  const full = path.join(STATIC, s[1].replace(/^\//, ''));
  if (!fs.existsSync(full)) fail('index.html', `引用的脚本不存在：${s[1]}`);
}
const linkRe = /href="(\/assets\/[^"]+|\/static\/[^"]+|\/vendor\/[^"]+)"/g;
while ((s = linkRe.exec(indexHtml))) {
  const full = path.join(STATIC, s[1].replace(/^\//, ''));
  if (!fs.existsSync(full)) fail('index.html', `引用的资源不存在：${s[1]}`);
}

/* ---------------- 8. 没有被任何路由引用的页面组件（提示） ---------------- */
const routedViews = new Set();
while ((r = routeRe.exec(appJs))) routedViews.add(r[2]);
viewFiles.forEach(function (value, name) {
  if (!routedViews.has(name)) warn(value.jsFile, `组件 ${name} 没有出现在路由表里（可能是遗漏）`);
});

/* ---------------- 输出 ---------------- */
const width = 84;
console.log('='.repeat(width));
console.log('前端静态自检（模板编译 + 组件/图标/字典/文件引用）');
console.log('='.repeat(width));
info.forEach(function (line) { console.log('  · ' + line); });
console.log(`  · 页面片段 ${fragments.length} 个，注册组件 ${viewFiles.size} 个`);
console.log('-'.repeat(width));

if (problems.length) {
  console.log(`发现 ${problems.length} 个错误：`);
  problems.forEach(function (line) { console.log('  ✗ ' + line); });
} else {
  console.log('所有模板编译通过，组件/图标/字典/文件引用均正确 ✅');
}
if (warnings.length) {
  console.log(`\n${warnings.length} 条提示（不阻塞，但值得看一眼）：`);
  warnings.forEach(function (line) { console.log('  ! ' + line); });
}
console.log('-'.repeat(width));
process.exit(problems.length ? 1 : 0);
