# spark-keeper-lite · 抖音云端自动续火花系统（纯卡密版，不含支付）

> 单租户自托管版：托管抖音账号 → 每天定时给指定好友发消息续火花 → 卡密发卡与兑换。
> **不含在线支付**：`app/payment/` 目录不存在，`/api/v1/pay/*` 返回 404（这是设计如此，不是故障）。
> **零第三方依赖**：后端只用 Python 标准库 + SQLite，浏览器自动化是自研 CDP 客户端（不依赖 playwright）。
> 前端是 CDN 版 Vue 3 的预编译单页应用，不需要 npm、不需要构建。

| 想干什么 | 去哪里 |
| --- | --- |
| 5 分钟在本机跑起来 | 本文「快速开始」 |
| 部署到云服务器（Docker / HTTPS / 备份） | [`deploy/DEPLOY.md`](deploy/DEPLOY.md) |
| 日常运维（巡检、备份恢复、排障、迁移机器） | [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
| 接口清单 | [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) |

## lite 与 pro 的区别

| | lite（本包） | pro |
| --- | --- | --- |
| 卡密生成/兑换（时间卡、次数卡、余额卡） | ✅ | ✅ |
| 订单、余额 | ✅ | ✅ |
| 人工确认收款（后台补单） | ✅ | ✅ |
| 易支付/支付宝/微信在线支付与回调 | ❌ 无此模块 | ✅ |
| `/api/v1/pay/notify/<channel>`、`/pay/return` | ❌ 404（正常现象） | ✅ |
| 其余功能（账号托管、定时任务、好友、记录、管理端） | 完全一致 | 完全一致 |

> 站点配置里的 `pay.enabled` 默认就是 `0`，本包后台也没有在线支付入口，
> 因此用户端「支付方式」只会显示卡密（不会出现点不动的假按钮）。

---

## 快速开始（本机 5 分钟）

### 需要什么

* **Python 3.9+**（Windows / macOS / Linux 都行，只用标准库，**不用 pip install**）
* **Chromium 或 Chrome**（自动续火花要用；本机只想看界面可以先开演示模式 `SK_DEMO_MODE=1`）

### 方式 A：Windows 一键

```bat
start.bat
```

> **双击后窗口一闪就没了？** 说明它在启动前就报错了，而报错信息来不及看。
> 请**按住 Shift 在项目目录空白处右键 →「在此处打开终端」**，然后执行：
>
> ```bat
> start.bat --check
> ```
>
> 错误信息会停住让你看清（`--check` 只做环境体检 + 数据库迁移，不常驻服务）。
> 其它参数：`--port 9000`（换端口）、`--lan`（绑 0.0.0.0，允许局域网访问）、
> `--init-only`（只初始化，不起服务）、`--demo`（不开真浏览器）、`--open`（自动开浏览器）。

> **更新过项目文件 / 页面行为与代码不一致时（必看）**：
> **Ctrl+C 停掉服务 → 重新运行 `start.bat` → 浏览器按一次 Ctrl+F5（强制刷新）**。
> 后端改代码必须重启进程才生效；前端虽然已改成协商缓存（`ETag` + `no-cache`，
> 没变返回 304、变了立刻拿到新版），但浏览器里可能还留着上一版页面，强刷一次最稳。
> 顺序反了会出现「代码明明修了、页面还是旧的」这种最费时间的假象。
> 排查完再双击 `start.bat` 正常启动即可。

首次运行会自动建库、建管理员，并把初始密码写到 `data\INITIAL_ADMIN.txt`；
然后浏览器打开 <http://127.0.0.1:8000/> 登录即可。

### 方式 B：Linux / macOS 一键

```sh
sh start.sh            # 也可以用 SK_PORT=9000 sh start.sh 换端口
```

### 方式 C：Docker（推荐用于服务器）

```sh
cp .env.example .env
python3 -c "import secrets;print('SK_SECRET='+secrets.token_urlsafe(48))"   # 填进 .env
docker compose up -d --build
docker compose exec app cat /app/data/INITIAL_ADMIN.txt
```

### 方式 D：手动（想自己控制每一步）

```sh
python main.py init                 # 建库 + 建管理员（密码只打印一次，注意抄下来）
python main.py check                # 环境体检：Python / 磁盘 / 浏览器 / 时区
python main.py serve --port 8000    # 启动（数据默认落在当前目录的 data/）
```

> **`SK_PROJECT_DIR` 是干什么的**：`app/config.py` 用 `PROJECT_DIR/web/static` 找前端产物，
> **关于 `SK_PROJECT_DIR`**：`app/config.py` 用「`web/static` 所在的那一层」作为项目根来找前端产物，
> 它会**自动判断**（开发树与交付树都正确），所以一般不用手工设置。`start.sh` / `start.bat` /
> Dockerfile / docker-compose 为了确定性会显式设置它。只有目录结构不常规时才需要自己覆盖；
> 设错的症状是「接口都正常，但打开页面 404」。

### 默认账号

| 项 | 值 |
| --- | --- |
| 用户名 | `admin`（可用 `SK_ADMIN_USER` 改） |
| 密码 | `data/INITIAL_ADMIN.txt` 里那次生成的值（**登录后立即修改并删除该文件**） |
| 地址 | <http://127.0.0.1:8000/> |

---

## 目录说明

```
spark-keeper-lite/
├── main.py                  CLI 入口：init / serve / worker / migrate / admin / card / task / check / diagnose
├── app/                     后端（api / common / automation / worker）—— 注意：没有 payment/
├── db/migrations/*.sql      数据库结构，按版本号顺序执行（幂等）
├── web/static/              前端产物（index.html + assets + vendor/vue.global.prod.js）
├── deploy/                  Dockerfile、compose、nginx.conf、entrypoint.sh、DEPLOY.md、.env.example
├── docker-compose.yml       根目录的 compose（=`deploy/docker-compose.lite.yml`，便于直接 up）
├── tools/                   backup.py（热备份）、check_contract.py（契约自检）、selftest.py（一键自检）
├── tests/                   端到端测试（unittest，零依赖）
├── docs/                    API_CONTRACT.md / RUNBOOK.md / SHARED_CONTEXT.md
├── VERSION                  交付版本信息
└── data/                    运行产物（数据库、登录态、截图、备份）—— 首次运行自动创建
```

---

## 怎么确认它没坏（30 秒）

```sh
python tools/selftest.py                      # 全量（含契约检查，会按 lite 版放行 /pay/*）
python tools/check_contract.py --profile lite # 只跑契约检查（lite 版专用档位）
python main.py check                          # 环境体检
```

`selftest.py` 会依次跑：零第三方依赖扫描 → 契约一致性 → 端到端测试套件 → 自动化引擎单测 →
热备份 → 部署产物静态校验 → 环境体检，最后给汇总表；**全通过时退出码 0**。

> **前端冒烟会真登录一次**：`tools/selftest.py` 里的 `dom_smoke` 会在隔离库里自动建一个
> 已知口令的管理员，然后断言「登录返回令牌 → 令牌写入本地存储 → 后续请求带 Authorization →
> 退出后清空」。想手工指定账号排查时：
>
> ```bat
> rem Windows（PowerShell）
> $env:SK_SMOKE_PASSWORD="你的密码"; node web\tools\dom_smoke.mjs --base http://127.0.0.1:8000 --root web/static
> ```
> ```sh
> # Linux/macOS
> SK_SMOKE_PASSWORD='你的密码' node web/tools/dom_smoke.mjs --base http://127.0.0.1:8000 --root web/static
> ```
>
> 排查「前端没带令牌」还是「后端拒绝令牌」用 `web/tools/diagnose_login.mjs`——它会打印
> 每个请求的 URL、是否带 Authorization、状态码，一眼看出断在哪一环。

验证「本包确实不含支付」：

```sh
python main.py serve --port 8000 &
python -c "import urllib.request,urllib.error;
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/pay/notify/easypay', timeout=5)
except urllib.error.HTTPError as e:
    print('pay/notify 状态码 =', e.code)   # 期望 404
"
```

---

## 功能一览

**用户端**：注册/登录/改密/找回密码 · 仪表盘（7 天趋势）· 抖音账号托管（扫码登录、短信登录、
登录态检测、代理）· 定时任务（时间/重复模式/随机抖动/随机文案/发送间隔/每日上限/重试、
立即执行、下次执行预览）· 好友管理（增删改、批量导入、从抖音同步会话）· 执行记录（每个好友的
结果与截图）· 发送台账 · 套餐 · 卡密兑换 · 订单 · 运行日志 · 站内通知。

**管理端**：概览统计 · 用户管理（改角色/状态/密码、授权套餐、调余额）· 卡密批次（生成/停用/
导出 CSV）· 套餐管理 · 订单管理（人工确认收款）· 账号与任务总览（批量启停、代跑）·
执行记录 · 运行日志与审计日志 · 公告 · 站点配置 · 代理管理 · 系统环境（体检 + 指标 + worker 心跳）。

**发卡**：`python main.py card --package monthly --type time --days 30 --count 10`
（也支持 `--type count --use-limit N` 与 `--type balance --amount 1900`），或在后台「卡密管理」生成。

**调度**：每天每账号只跑一次（数据库唯一索引兜底）· 发送台账幂等（同一天同一好友只发一次）·
随机抖动 · 停机后不补跑 · worker 心跳 · 超时任务标记失败。

---

## 安全须知

* 生产环境必须设 `SK_SECRET`（≥32 字符）并保持稳定，否则每次重启都会让所有人掉线。
* `data/` 里有登录态文件与数据库，**不要**提交到版本库、不要放进公开备份。
* 公网部署请置于 HTTPS 反向代理之后（`deploy/nginx.conf` 已给好示例），8000 端口只绑本机。
* 首次登录后：改密码、删 `INITIAL_ADMIN.txt`、按 `deploy/DEPLOY.md` 第 14 节的清单过一遍。

---

## 未验证项（诚实声明）

本交付包在**开发机**上已验证：安装向导、健康检查、全部核心接口、契约一致性（lite 档位）、
端到端测试套件、热备份、冷启动（`init` → `serve` → 探针 200）、`/api/v1/pay/*` 返回 404。

**未**在开发机验证（环境所限，需在部署机首次运行时确认）：

1. `docker build` / `docker compose up`（开发机无 Docker），
2. 容器内 Chromium 启动（容器沙箱可能需按 `deploy/DEPLOY.md` 第 13 节调整），
3. `nginx -t` 配置语法（开发机无 nginx），
4. 真实抖音扫码登录与真实发送（开发机禁止启动浏览器；本机用 `SK_DEMO_MODE=1` 跑通了 UI 流程），
5. 真实 SMTP / 短信投递。

排障与处置见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md) 第 12 节与 [`docs/RUNBOOK.md`](docs/RUNBOOK.md)。
