# 共享上下文（所有队友必读）

## 这个项目是什么

`dy.yxaoi.com` 是一个「抖音云端自动续火花 + 卡密发卡 + 支付」的 SaaS。
用户要求做一个相似的自托管系统，**单租户、自己用或授权少数账号使用**，
最终要能部署到云服务器。交付两份：`spark-keeper-pro/`（含在线支付）与
`spark-keeper-lite/`（不含支付，纯卡密），共享同一份引擎代码。

## 环境实测结论（不要再试第二遍）

| 事实 | 影响 |
| --- | --- |
| 内置 Python 3.15（`python` 命令可用） | 后端语言 |
| **pip 装不了任何包**（沙箱禁止写系统临时目录，实测 fastapi/pydantic 全部失败） | **后端只准用标准库**，零第三方依赖 |
| **不能启动任何子进程**（playwright 驱动、chrome 全部被沙箱拒绝） | 自动化层只能在部署环境真跑，本机靠单元测试 + 假后端验证 |
| Node 24 在 `C:\Users\huawei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe`（不在 PATH，`npm/pnpm/node` 命令都找不到） | 前端**不做构建**，用 CDN 版 Vue 3 全局构建 + 预编译模板 |
| 工作区外不可写，`TEMP` 不可写 | 临时文件写 `backend/data/tmp/` |
| 出口网络可用（PyPI/npm registry 200，web_fetch 可用） | 可以下载前端库、查文档 |

**违反「零依赖」的代码会在部署时炸**，这是硬约束。

## 技术栈（已定，不要改）

* 后端：Python 标准库 + SQLite（WAL）。HTTP 用 `http.server.ThreadingHTTPServer`，
  路由用 `app/router.py` 的 `@route()` 装饰器，鉴权用 HS256 JWT + `sessions` 表白名单。
* 前端：单页应用，`web/static/index.html` + `assets/app.js` + `assets/style.css`，
  用 Vue 3 **global build**（`vue.global.prod.js`，走 CDN，`web/static/vendor/` 放本地兜底副本）。
  **不要用 `.vue` 单文件组件、不要用 `import`、不要用构建工具**——模板写在
  `<script type="text/x-template" id="...">` 里，或者用渲染函数/字符串模板。
  图标用内联 SVG（不引图标库）。
* 浏览器自动化：自研 CDP 客户端（`app/automation/cdp.py`，标准库 socket 实现
  RFC6455 WebSocket），启动 `chrome --headless=new --remote-debugging-port=0`，
  通过 `/json/version` 拿 webSocketDebuggerUrl，用 `Runtime.evaluate` / `Page.navigate` /
  `Input.*` 驱动。**不依赖 playwright**。
* 部署：Dockerfile（python:3.11-slim + chromium）+ docker-compose + Nginx 反代示例。

## 目录与写入范围（严格遵守，避免互相覆盖）

```
spark-shared/
├── backend/
│   ├── main.py                    ✅ 已完成（Lead）
│   ├── app/
│   │   ├── config.py  db.py  router.py  http_server.py  bootstrap.py  version.py
│   │   │                          ✅ 已完成（Lead，read-only，需要改就找 Lead）
│   │   ├── common/                ✅ 已完成（Lead，read-only）
│   │   │   security.py timeutil.py quota.py textutil.py mailer.py logging.py auth.py validate.py
│   │   ├── api/
│   │   │   ├── __init__.py        ✅
│   │   │   ├── auth.py user.py accounts.py tasks.py packages.py orders.py    ← 队友 A
│   │   │   ├── admin.py cardkeys.py install.py public.py files.py            ← 队友 B
│   │   ├── automation/            ← 队友 D
│   │   │   cdp.py wsdump.py douyin.py selectors.py login.py task_runner.py
│   │   │   session_store.py diagnostics.py chrome.py(已完成) humanize.py
│   │   └── worker/                ← 队友 D
│   │       __init__.py runner.py scheduler.py pool.py
│   ├── db/migrations/0001_init.sql ✅ 已完成（Lead，read-only）
│   └── data/                      运行产物，已 gitignore
├── web/                           ← 队友 C
│   ├── static/index.html  static/assets/app.js  static/assets/style.css
│   ├── static/assets/views/*.html   （可选：把大页面拆成片段再 fetch 注入）
│   └── static/vendor/vue.global.prod.js
├── pro/app/payment/               ← 队友 B（pro 版专属支付模块）
│   gateway.py  easypay.py  routes.py  reconcile.py
├── tests/                         ← 队友 E
├── deploy/                        ← 队友 E
│   Dockerfile  docker-compose.yml  nginx.conf  entrypoint.sh  DEPLOY.md
└── docs/API_CONTRACT.md           ✅ 已完成（Lead，冻结契约，改动需先找 Lead）
```

**只写自己范围内的文件。** 需要别人范围的改动就在任务里留言/找 Lead，不要动手。

## 已完成的地基（直接用，别重写）

* 迁移已跑通：40 张表（users / sessions / verify_codes / packages / card_batches /
  card_keys / card_redemptions / orders / payment_notifications / balance_logs /
  agent_levels / proxies / douyin_accounts / login_sessions / scheduled_tasks / friends /
  task_runs / task_run_items / send_history / announcements / site_configs /
  notifications / audit_logs / run_logs / worker_heartbeats / message_outbox / schema_migrations）。
  字段定义见 `backend/db/migrations/0001_init.sql`（**先读它再写代码**）。
* `app/router.py`：`@route(method, path, auth="public|user|admin", summary="", tags=())`，
  处理器签名 `def handler(req: Request) -> Response`，路径参数用 `<name>` 会自动传成关键字参数。
  返回 `ok(data)` / `fail(message, status, code)`，或直接返回 dict/list。
  抛 `HttpError(status, message, code=...)`。
* `Request` 提供 `json() / field() / str_field() / int_field() / bool_field() / page_args() /
  require_user() / require_admin() / user / user_id / client_ip / headers / query`。
* `app/db.py`：`query_one / query_all / query_scalar / insert / update / delete /
  transaction() / paginate(sql, params, page, page_size) / migrate()`。
* `app/common/timeutil.py`：`utc_now_str()` / `to_db()` / `parse_db()` / `is_expired()` /
  `local_date_str(tz)` / `next_run_time(...)` / `humanize_delta()` / `add_days_str()`。
* `app/common/quota.py`：`Quota.for_user(id)` → `.ensure()` / `.ensure_can_add_account()` /
  `.ensure_can_run_today()` / `.summary()`；`limit_status(id)`。
* `app/common/textutil.py`：`as_list / as_str_list / as_int_list / as_dict / as_min_max /
  pick_message(messages, vars) / message_warnings / normalize_name / redact`。
* `app/common/auth.py`：`issue_tokens / refresh_access_token / revoke_session /
  authenticate(identifier, password, ip, user_agent) / register(...) / create_verify_code(...) /
  check_verify_code(...) / change_password(...) / create_reset_token / consume_reset_token /
  public_user(user)`。
* `app/common/security.py`：`hash_password / verify_password / password_problem / sign_jwt /
  verify_jwt / encrypt / decrypt / new_card_code / normalize_card_code / card_code_hash /
  new_order_no / mask_phone / mask_email / const_time_eq / sha1|sha256_hex / safe_compare_dict`。
* `app/common/logging.py`：`log / info / warning / error / audit / notify / prune`，
  带 `log_context(user_id=..., source=...)` 上下文管理器。
* `app/common/mailer.py`：`enqueue / send_email / send_sms / flush / send_verify_code_email /
  send_verify_code_sms / health`。
* `app/bootstrap.py`：`get_config / set_config / public_configs / ensure_admin / init_app /
  initialization_status / check_cloud_environment`。
* CLI：`python main.py init|serve|worker|migrate|admin|card|task|check|diagnose`。

## 代码风格要求（会被检查）

* 中文注释解释「为什么」，不是「做了什么」；每个模块顶部写清职责与坑。
* 类型标注齐全，`from __future__ import annotations`。
* 不吞异常：要么处理，要么带上下文抛出；`except Exception` 必须写清楚为什么安全。
* 不用 f-string 拼 SQL；SQL 一律参数化。
* 所有多租户数据查询必须带 `user_id` 条件（越权是这类系统最大的漏洞）。
* 写完自己跑一遍：`python main.py check`、`python -c "import app.bootstrap as b; b.init_app()"`、
  以及自己模块的最小自测。

## 如何本地验证（不需要联网、不需要浏览器）

```powershell
cd "D:\DeppSeek WorkRegion\spark-shared\backend"
$env:PYTHONUTF8=1
$env:SK_DATA_DIR="D:\DeppSeek WorkRegion\spark-shared\backend\data"
python main.py check
python -m tests.smoke          # 队友 E 提供；没写之前可跳过
```

* 起服务：`python main.py serve --port 8011`（后台跑，别占用 8000）。
* 发请求：`curl.exe` 在这台机器上 SSL 有问题，**用 python 的 urllib** 发 HTTP 请求（本地 http 不受影响）。
* 沙箱禁止起子进程，所以**不要**在测试里真的拉起 uvicorn/chrome。

## 交付标准

能被别人复现：文档写清「怎么跑起来、怎么验证、哪里没验证」。
**没验证的部分必须诚实标注**（例如「浏览器自动化逻辑已单测，但本机无法启动真实
Chrome，需在部署机首次运行时确认」）。禁止声称验证过实际没跑的东西。
