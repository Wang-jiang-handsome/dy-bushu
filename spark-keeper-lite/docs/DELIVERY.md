# 交付说明 · 抖音续火花系统（自托管版）

> 请先读 [`README.md`](README.md) 了解怎么跑起来；本文说明**交付了什么、验证到什么程度、哪些还没验证**。

---

## 一、交付物清单

| 目录 | 说明 |
| --- | --- |
| [`spark-keeper-pro/`](../spark-keeper-pro/) | **交付物一（含在线支付）**：用户端 + 管理端 + 卡密 + 易支付在线支付，可独立部署。**139 个接口**（含 3 条 `/pay/*`） |
| [`spark-keeper-lite/`](../spark-keeper-lite/) | **交付物二（不含支付）**：其余功能完全相同，靠卡密激活。**136 个接口**，`/api/v1/pay/*` 返回 404 |
| `spark-shared/` | **开发主仓库（唯一真源）**：两份交付物由它组装而来，`backend/` 是引擎源码 |

两份交付目录各自独立、自包含（各含后端、前端、部署文件、文档、自检测试），
**差别只有一个**：pro 版多一个 `app/payment/` 目录（在线支付渠道 + 回调对账）。

**Lead 独立验证（在两份交付目录里实跑，不是开发树）**：

```
spark-keeper-pro : 143 个文件 / 1.91 MB · 根目录 20 条目 · data 仅 3 个空目录 · 无 pycache/db/secret
                   main.py init → 路由 139（payment loaded: True）· schema 0003
                   tools/acceptance_e2e.py    → 通过 36 · 失败 0 · 跳过 0
                   tools/integration_probe.py → 通过 21 · 失败 0 · 跳过 2
                   tools/foundation_selftest.py → 17/17 · tools/quota_selftest.py → 13/13
                   tools/check_encoding.py    → 424 文件、可疑 0
spark-keeper-lite: 138 个文件 / 1.88 MB · 路由 136（app.payment 未安装，属正常）
                   GET /api/v1/health 200 · /api/v1/packages 200
                   GET /api/v1/pay/notify/easypay 404 · /api/v1/pay/return 404 · /admin/stats 无令牌 401
```

> `quota_selftest` 在交付树是 13/13、开发树 14/14，差的那一项是「管理员 daily_limit=999」——
> 交付库还没有 admin，该条条件断言被跳过，属预期而非缺陷。

代码规模（`spark-shared/`）：后端 30 个 Python 文件约 1.6 万行，前端 12 个 JS + 30 个模板约 0.5 万行，
测试 400+ 个用例。**后端零第三方依赖**（纯标准库 + SQLite），**前端免构建**（Vue 3 全局构建 + hash 路由）。

### 功能对照（目标站 dy.yxaoi.com 有的，这里都有对应实现）

| 目标站功能 | 本项目实现 |
| --- | --- |
| 用户注册 / 登录 / 找回密码 | `/auth/user/register` · `/auth/user/login` · `/auth/forgot-password` · `/auth/reset-password` · 邮箱/短信验证码 |
| 管理端登录 | `/auth/admin/login`（非管理员一律拒绝） |
| 卡密系统（生成/兑换/停用/统计/导出） | `/admin/card-keys/*` + `/user/card-keys/redeem`，三种卡型：时长 / 次数 / 余额 |
| 套餐与在线支付 | `/packages` · `/user/packages/purchase` · 易支付（MD5 签名 + 异步回调 + 主动查单 + 对账） |
| 订单管理 | `/user/orders*` · `/admin/orders*`（含人工确认收款、退款） |
| 抖音账号托管 | `/douyin/accounts*`（账号 CRUD、暂停托管、登录态分离） |
| 扫码登录 / 短信登录 | `/douyin/login/browser`、`/douyin/login/sms/*`，**非阻塞 + 前端轮询状态机** |
| 定时任务（多天自动续火花） | `/tasks*`：每天定时、随机抖动、随机文案、发送间隔、每日上限、失败重试 |
| 执行记录与截图 | `/douyin/task-records*`、逐好友结果、失败截图 |
| 好友管理 / 发送台账 | `/friends*`（含批量导入、从抖音拉取会话）、`/douyin/stats/send-history` |
| 运行日志 / 审计日志 | `/user/logs` · `/admin/logs` · `/admin/logs/admin` |
| 站内通知 / 公告 | `/user/notifications*` · `/admin/announcements*` |
| 代理池 | `/admin/proxies*`（含连通性检测，密码对称加密存储） |
| 站点配置 | `/admin/configs`（密钥类读取自动脱敏） |
| 系统环境 / 进程视图 | `/admin/system-environment` · `/admin/browser-processes` |
| 安装向导 | `/api/v1/install/check|execute|latest-migration|test-db` |
| 代理分销 | `/admin/agent-levels*`（保留结构；自托管默认不用） |
| 自动更新 / AI 文案 | 按设计不联网：`/admin/update` 只报当前版本，`/admin/test-ai` 未配置时明确 400 |

---

## 二、技术选型（与目标站的差异，以及为什么）

| 维度 | 目标站 | 本项目 | 原因 |
| --- | --- | --- | --- |
| 后端框架 | 疑似 Laravel/PHP 或 Python 框架 | **纯标准库 HTTP 服务** | 自托管要「拷过去就能跑」：无 pip 依赖、无版本冲突、镜像更小 |
| 数据库 | MySQL + Redis | **SQLite（WAL）** | 单文件、免运维、备份＝拷一个文件；单机规模完全够 |
| 浏览器自动化 | 未公开（推测 Playwright） | **自研 CDP 客户端**（标准库 socket 实现 RFC6455） | 少一层重依赖；协议层可单测 |
| 前端 | Vue 3 + Vite 构建 | **Vue 3 全局构建，免构建** | 服务器上不需要 Node，部署步骤更少 |
| 会话 | 未公开 | **HS256 JWT + sessions 白名单** | 支持「改密/强制下线立即失效」，而不只是等过期 |

**这是刻意取舍，不是妥协**：换来的是「`python main.py serve` 一行启动」和「数据备份＝拷贝文件」。

---

## 三、验证状态（三层，如实分级）

### ✅ 第一层：已经真跑并全绿的（可复现，命令都在下面）

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 地基自检（HTTP/路由/鉴权/404/405/数据库） | `cd spark-shared/backend && python ../tools/foundation_selftest.py` | **17/17** |
| 配额与次数卡（加成叠加/过期作废/refresh 不丢） | `python ../tools/quota_selftest.py` | **14/14** |
| 后端集成探测（真实 HTTP + 真实请求） | `python ../tools/integration_probe.py` | **32/32**（含商业闭环：发卡→兑换→建号→建任务→执行→记录） |
| 最终端到端验收（**真 subprocess 起服务** + 调度器 + demo 模式） | `python ../tools/acceptance_e2e.py` | **36/36**（交付目录内同样 36/36） |
| 前端运行时冒烟（Node vm 上下文 + 最小 DOM 影子 + 真实后端） | `node web/tools/dom_smoke.mjs --base http://127.0.0.1:PORT --root web/static` | **19/19**（含「外壳模板已注入」这条关键断言） |
| 前端行为检查（外壳注入 / 孤儿片段 / 数据绑定 / 自检的自检） | `node web/tools/check_boot.js` · `check_templates.js` · `check_bindings.js` · `python web/tools/selftest_guards.py` | 全绿（含**负向测试**：故意删掉外壳加载会 exit 1） |
| 契约一致性（文档 ↔ 运行时路由） | `python ../tools/check_contract.py` | 一致 ✅ |
| 编码体检（乱码 / BOM） | `python ../tools/check_encoding.py` | 424 文件、可疑 0 |
| 全队测试套件 | `python ../tools/selftest.py` | **15 项全绿**（含零第三方依赖扫描） |
| 用户端接口（A） | `python data/tmp/a_selftest.py` | 184/184 |
| 管理端 + 卡密 + 支付（B） | `cd spark-shared && python pro/tests/b_selftest.py` | **301/301**（卡密 55 · 支付 43 · 商业闭环 46 · HTTP 157） |
| 自动化引擎单测（D） | `cd backend && python -m unittest discover -s tests -t .` | **237 通过**（连跑 5 次稳定） |

**关键闭环已真实验证**（`acceptance_e2e.py` 的输出）：
```
main.py init → serve（含调度器，日志有 [task.scheduler] 调度器启动）
安装体检 installed=true · 环境信息 · 库路径自证（防串库）
管理员登录 → 配置读写 → 生成卡密 → 注册 → 兑换 → 配额生效（limit=3）
建托管账号 → demo 扫码登录：pending → scanned → confirmed → success → 账号回填 online
登录态不泄露（响应只有 has_session 布尔，无 storage_path/cookies）
批量导入好友 → 建任务（有 next_run_at）→ 立即执行 → 执行记录 success=2 fail=0
把 next_run_at 改到过去 → 调度器 60 秒内自动触发（trigger_type=schedule）
运行日志/审计日志/系统环境/浏览器进程视图全部可读 · 服务日志无 Traceback
```

### ⚠️ 第二层：只做了静态/结构校验，需要部署机第一次运行时人工确认

| 项目 | 现状 | 怎么确认 |
| --- | --- | --- |
| **真实抖音页面上的扫码登录与发送** | 协议层、登录状态机、发送与结果校验逻辑都有单测；`demo_mode` 走完整流程。**但本机沙箱禁止启动浏览器子进程**，真实 DOM 交互从未跑过 | 部署机：`SK_DEMO_MODE=0` 起服务 → 建账号 → 扫码 → `python main.py task --account-id N --dry-run`（空跑，只找好友不发送）→ 确认能找到好友后再正式跑一次 |
| Docker 镜像构建 | `deploy/Dockerfile`/compose/nginx/entrypoint 已通过静态规则校验（抓到并修掉了 entrypoint 漏 shebang） | 部署机 `docker compose up -d --build` |
| 容器内 Chromium 启动 | 镜像里装了 chromium + 中文字体，参数含 `--headless=new --remote-allow-origins=*`；**未在容器里真跑过** | `docker compose exec app python main.py check` + 容器内跑一次 `task --dry-run`；起不来见 DEPLOY.md「浏览器起不来」三级处置 |
| 前端 CSS 布局与鼠标交互 | 模块加载/视图注册/模板拉取/路由跳转/接口对接/外壳注入全部真实验证（DOM 冒烟 **19/19** + 三个行为检查脚本）；**真实像素渲染与点击未验证** | 浏览器打开首页，过一遍登录 → 仪表盘 → 各页面 |
| 易支付真实通道 | MD5 签名（与参考实现逐字节比对）、回调验签、金额核对、幂等、对账全部单测通过；**无真实商户号，未对真实网关下单** | 填 `SK_PAY_EASYPAY_*` 后用 1 分钱订单联调 |
| SMTP / 短信真实投递 | 未配置分支已验证（验证码写日志并返回 `dev_code`） | 配好 `SK_SMTP_*` 后点「发送测试邮件」 |

### ❌ 第三层：明确没有做的

| 项目 | 说明 |
| --- | --- |
| 图形验证码（captcha） | 字段接受但未实现（自托管场景无刷号动机）；需要时接第三方即可 |
| 反爬对抗的"保证" | 做了拟人化（随机文案、随机间隔、随机抖动、真实浏览器指纹、隐藏自动化特征），**但不保证不被风控**——这是平台策略决定的，任何同类工具都做不到 |
| 多机横向扩展 | 设计上是单机（SQLite + 进程内调度器）。要多机需换 MySQL + 独立 worker，属另一档工程量 |
| 并发压测 | 未做（自测关闭了限流）；预估单机支撑百级账号，未实测 |

---

## 四、怎么验证（给验收者）

### Windows 用户：直接用 `start.bat`

```bat
cd spark-keeper-pro
start.bat              :: 首次会建库 + 建管理员并把随机密码打印出来，然后启动服务
```

如果双击后**窗口一闪就消失**，那是启动前就报错了（脚本已保证所有失败路径都会停住让你看，
但如果是更早期的问题——比如被安全软件拦截——仍会闪现）。请这样看原因：

```bat
:: 在项目目录按住 Shift + 右键 →「在此处打开 PowerShell/终端」
start.bat --check          :: 环境体检 + 建库迁移，然后停住
start.bat --init-only      :: 只初始化，打印管理员账号密码
start.bat --port 9000      :: 换端口（默认 127.0.0.1:8000，仅本机可访问）
start.bat --lan            :: 允许局域网访问（0.0.0.0:8000）
```

> `.bat` 有三个硬性规则，改动它之前请先读文件头的注释并跑 `python tools/check_bat.py`：
> **纯 ASCII、CRLF 换行、无 UTF-8 BOM**。cmd.exe 是按 GBK 码页读 `.bat` 的，
> 写 UTF-8 中文会变乱码甚至中断解析——所以脚本里的提示是英文，中文提示都由 Python 输出。

### 开发者：完整的命令清单

```bash
# 0. 前提：Python 3.9+（不需要 pip install；Node 只在跑前端行为检查时才需要）
cd spark-shared/backend

# 1. 最快的一条命令：一键跑全部检查（含 400+ 用例、契约、集成、部署产物静态校验）
python ../tools/selftest.py                # 期望：15 项全绿，exit 0

# 2. 端到端验收（真起服务 + 调度器 + demo 模式，约 1 分钟）
python ../tools/acceptance_e2e.py          # 期望：36/36，exit 0

# 3. 人工过一遍界面
python main.py init                        # 记下打印的管理员密码
python main.py serve                       # 浏览器打开 http://127.0.0.1:8000

# 4. 前端运行时冒烟（另开一个终端；注意 cwd 是项目根，不是 backend）
cd ..                                      # 回到 spark-shared/
node web/tools/dom_smoke.mjs --base http://127.0.0.1:8000 --root web/static   # 期望 19/19
node web/tools/check_boot.js               # 外壳注入 + 29 模板非空
node web/tools/check_templates.js          # 孤儿片段检测

# 5. 杂项体检（都很便宜，值得加进 CI）
python tools/check_contract.py             # 文档 ⇄ 运行时路由 一致性
python tools/check_encoding.py             # 乱码 / BOM
python tools/check_bat.py                  # .bat 纯 ASCII + CRLF + 无 BOM + 必须有 pause
```

---

## 五、合规声明（请务必阅读）

本项目是**自托管单租户**系统，供你自己或你明确授权的少数账号使用。

1. **自动化操作抖音属于非官方用法**，可能违反平台用户协议，存在账号被限制或封禁的风险；
   系统做了拟人化但不能保证不被风控。**建议先用小号验证**。
2. **火花需要双方互动**：系统只负责按时替你发消息，对方不回，火花仍会断。
3. **云端托管意味着你的登录态存在服务器上**（`data/sessions/`，权限 600）。
   请确保服务器本身安全：改默认端口、配 HTTPS、限制 SSH、定期备份并加密备份文件。
4. **对外售卖卡密会改变你的法律地位**：你会从「使用者」变成「分发方」，
   同时涉及平台协议、代收付合规与税务问题。请自行确认资质后再决定，
   本交付不为对外经营行为背书。
5. 请勿用于批量营销、骚扰他人或任何形式的垃圾信息传播。
