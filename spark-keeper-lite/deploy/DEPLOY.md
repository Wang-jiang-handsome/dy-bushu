# 部署手册 · spark-keeper（从零到上线）

> **选平台先看这份**：`deploy/DEPLOY_PLATFORMS.md` —— 讲清楚为什么 Netlify/Vercel
> 这类静态托管跑不了本项目（无常驻进程/无持久磁盘/无 Chromium），以及
> VPS+Docker、容器型 PaaS、前端 Netlify + 后端服务器（`SK_CORS_ORIGINS`）三种方案怎么选。

> 目标读者：拿到 `spark-keeper-pro/` 或 `spark-keeper-lite/` 目录、准备把它跑在自己云服务器上的人。
> 全程不需要联网装 Python 包——后端**零第三方依赖**，只要有一个 Python 3.9+ 和一个 Chromium。
>
> **本机已验证 / 未验证**（诚实声明，别把没跑过的当跑过）：
>
> | 项目 | 状态 |
> | --- | --- |
> | 安装向导、健康检查、契约自检、单元/端到端测试 | ✅ 本机实跑通过（见 `tools/selftest.py`） |
> | 冷启动 `python main.py init` → `serve` → `/api/v1/health` | ✅ 本机实跑通过 |
> | 热备份 `VACUUM INTO` | ✅ 本机实跑通过 |
> | `docker build` / `docker compose up` | ❌ 开发机没有 Docker，**未实际构建**；镜像在部署机首次构建时验证 |
> | Chromium 在容器内启动 | ❌ 本机沙箱禁止起浏览器；容器内沙箱参数（见「故障排查」）需部署机确认 |
> | `nginx -t` 配置校验 | ❌ 开发机没有 nginx，示例配置未做语法校验 |
> | 真实抖音扫码登录 / 自动续火花 | ❌ 需在部署机首次运行时确认（本机用 `SK_DEMO_MODE=1` 跑通流程） |

---

> **本文偏「容器与运维细节」**。如果你想知道「到底该传哪些文件、Windows 本机怎么传到 Linux 服务器」，
> 请看更面向操作的 **[docs/DEPLOY_GUIDE.md](docs/DEPLOY_GUIDE.md)**（逐项文件清单 + 两种部署方式 + 排障表）。

## 0. 最短路径（5 分钟）

```bash
# 1) 上传交付目录到服务器
scp -r spark-keeper-pro root@your-server:/opt/

# 2) 进入目录，准备配置
cd /opt/spark-keeper-pro
cp .env.example .env
python3 -c "import secrets;print('SK_SECRET='+secrets.token_urlsafe(48))"   # 把输出填进 .env
vi .env        # 至少改 SK_SECRET、SK_PUBLIC_URL

# 3) 构建并启动
docker compose up -d --build
docker compose logs -f app          # 看到 "启动：python main.py serve" 即可

# 4) 拿初始管理员密码
docker compose exec app cat /app/data/INITIAL_ADMIN.txt

# 5) 验证
curl -s http://127.0.0.1:8000/api/v1/health
python3 tools/selftest.py --only deploy      # 交付产物静态校验（不需要 docker）
```

没有 Docker 也能跑（见第 4 节「裸机部署」）。

---

## 1. 服务器要求

| 项目 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| CPU | 2 核 | 4 核 | 每个 Chromium 实例峰值约 0.5~1 核 |
| 内存 | 2 GB | 4 GB | **每个浏览器实例约 400~700MB**；2GB 机器请把 `SK_MAX_CONCURRENT_BROWSERS` 设为 1 |
| 磁盘 | 20 GB | 40 GB | 截图与登录态会慢慢涨；Docker 镜像约 1.2GB |
| 系统 | Debian 12 / Ubuntu 22.04 / CentOS Stream 9 | Ubuntu 22.04 LTS | 需要能装 chromium 与中文字体 |
| 带宽 | 3 Mbps | 5 Mbps+ | 抖音页面 + 截图 |
| 架构 | x86_64 | x86_64 | arm64 需自行确认 chromium 包名 |

> **地区提示**：服务器需要能正常访问 `douyin.com`。香港/新加坡节点通常可用，
> 国内节点延迟更低但需备案。访问不通的表现是任务一直 `failed` 且日志里有导航超时。

---

## 2. 安装 Docker（官方脚本）

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version && docker compose version
```

国内服务器如果拉不动镜像，给 Docker 配镜像加速（`/etc/docker/daemon.json`）：

```json
{ "registry-mirrors": ["https://docker.mirrors.ustc.edu.cn"] }
```

然后 `systemctl restart docker`。本项目基础镜像只有 `python:3.11-slim`，很小。

---

## 3. 配置说明（`.env`）

完整清单见 `deploy/.env.example`（每一项都有注释）。**必须改的只有两个**：

| 变量 | 说明 |
| --- | --- |
| `SK_SECRET` | JWT 签名与凭据加密密钥，≥32 字符。**不要留空**：留空会自动生成 `data/secret.key`，容器重建后所有登录态失效，多容器还会各生成一份互相不认 |
| `SK_PUBLIC_URL` | 对外访问地址，例如 `https://spark.example.com`。支付回调地址由它拼接 |

容易踩的其它几项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SK_TRUSTED_PROXIES` | `172.16.0.0/12,127.0.0.1` | 只有来自这些地址的 `X-Forwarded-For` 才被采信。写错会导致限流把所有请求算到 Nginx 头上 |
| `SK_MAX_CONCURRENT_BROWSERS` | `2` | 内存不够就降到 1，否则任务会 OOM 被杀 |
| `SK_SCHEDULER_ENABLED` | `1` | 单容器模式保持 1。用 `--profile worker` 起独立 worker 时必须改成 0，否则同一任务会被两个进程同时调度 |
| `SK_DEMO_MODE` | `0` | 演示模式：不启动真浏览器。本地体验可以设 1 |
| `SK_CHROME_PATH` | `/usr/bin/chromium` | 容器内固定；裸机若用 Chrome 写 `/usr/bin/google-chrome` |

---

## 4. 启动

### 4.1 Docker Compose（推荐）

```bash
cd /opt/spark-keeper-pro
docker compose up -d --build
docker compose ps            # STATUS 应为 healthy
docker compose logs -f app
```

启动流程（`deploy/entrypoint.sh` 自动完成）：
1. 检查 `/app/data` 可写 → 2. 等数据库就绪 → 3. `python main.py migrate` →
4. 没有管理员就初始化，密码写入 `/app/data/INITIAL_ADMIN.txt`（权限 600）→ 5. `exec python main.py serve`。

多容器（Web 与调度器分开）：

```bash
# .env 里改成 SK_SCHEDULER_ENABLED=0
docker compose --profile worker up -d --build
docker compose ps            # app + worker 两个容器
```

> 两个容器共用同一个数据卷（SQLite 文件）。WAL 模式下「一写多读」是安全的，
> 但不要为了扩容起多个 app 容器——SQLite 不适合多写者。

### 4.2 裸机部署（不装 Docker）

```bash
apt-get update && apt-get install -y python3 chromium fonts-noto-cjk tzdata
cd /opt/spark-keeper-pro
export SK_DATA_DIR=/opt/spark-keeper-pro/data
python3 main.py init                 # 建库 + 建管理员（密码打印一次，注意抄下来）
python3 main.py check                # 体检：Python/磁盘/浏览器/时区
python3 main.py serve --port 8000    # 前台跑，正式环境用 systemd
```

systemd 单元示例（`/etc/systemd/system/spark.service`）：

```ini
[Unit]
Description=spark-keeper
After=network.target

[Service]
WorkingDirectory=/opt/spark-keeper-pro
Environment=SK_ENV=prod
Environment=SK_DATA_DIR=/opt/spark-keeper-pro/data
EnvironmentFile=/opt/spark-keeper-pro/.env
ExecStart=/usr/bin/python3 main.py serve --port 8000
Restart=always
RestartSec=5
User=spark

[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload && systemctl enable --now spark`。

---

## 5. 域名与 HTTPS

1. 域名 A 记录指到服务器 IP。
2. 装 Nginx 与证书工具：

```bash
apt-get install -y nginx certbot python3-certbot-nginx
cp deploy/nginx.conf /etc/nginx/conf.d/spark.conf
sed -i 's/spark.example.com/你的域名/g' /etc/nginx/conf.d/spark.conf
nginx -t && systemctl reload nginx
certbot --nginx -d 你的域名            # 自动签发并改写 ssl_certificate 路径
```

3. 确认 `docker-compose.yml` 里端口只绑本机（默认 `127.0.0.1:8000`），公网流量一律走 Nginx。

`nginx.conf` 里已经包含：真实 IP 传递（`X-Forwarded-For/Proto`）、WebSocket 升级、
静态资源缓存、`client_max_body_size 10m`、`limit_req` 边缘限流、支付回调放宽限流、
HSTS 与安全响应头。

---

## 6. 首次登录后立刻做的事

1. 打开 `https://你的域名/` → 用 `INITIAL_ADMIN.txt` 里的账号密码登录。
2. **改密码**（个人设置 / 管理端改密），然后删除 `/app/data/INITIAL_ADMIN.txt`。
3. 站点配置：站名、Logo、备案号、客服联系方式、是否开放注册。
4. 套餐与卡密：后台「卡密管理」生成批次（`time` 加天数 / `count` 加次数 / `balance` 加余额）。
5. 若要收在线支付（仅 pro）：见第 8 节。
6. 试跑一次定时任务：后台建任务 → 「立即执行」→ 看执行记录与截图。

---

## 7. 抖音账号托管

* **扫码登录**：后台「抖音账号」→ 新增 → 扫码。二维码在服务端用无头浏览器截屏生成，
  所以要求服务器能访问 `douyin.com`。手机与服务器不在同一网络也能扫。
* **代理**：同 IP 挂多个账号更容易触发风控。后台「代理管理」可为每个账号单独指定代理。
* **并发**：`SK_MAX_CONCURRENT_BROWSERS` 控制同时开几个浏览器；超出的任务排队。
* **登录态文件**：存在 `data/sessions/account_<id>.json`（权限 600），**绝对不要**放进
  Git 或备份到公开位置；换服务器时随数据目录一起迁移即可。
* **失败重试**：单任务超时 `SK_TASK_TIMEOUT`（默认 180s），失败退避 `SK_TASK_RETRY_BACKOFF`。

---

## 8. 在线支付（仅 pro 版）

支付参数存在**站点配置**里（后台页面或 `PUT /api/v1/admin/configs`），不在 `.env`：

| 键 | 说明 |
| --- | --- |
| `pay.enabled` | `1` 开启（默认 `0`，关闭时用户端只显示卡密渠道，不会出现点不动的假按钮） |
| `pay.channel` | 默认渠道：`easypay` / `alipay` / `wechat` |
| `pay.easypay_url` | 网关地址，如 `https://pay.example.com` |
| `pay.easypay_pid` | 商户号 |
| `pay.easypay_key` | 商户密钥（MD5 签名） |
| `pay.easypay_type` | 默认支付类型 `alipay` / `wxpay` |
| `pay.easypay_sign_type` | 默认 `MD5`（RSA 预留未实现） |

回调地址（必须公网可达、必须 HTTPS、不能被 WAF 拦）：

```
https://你的域名/api/v1/pay/notify/easypay
https://你的域名/api/v1/pay/return
```

**回调排障**：每次回调都会原样写进 `payment_notifications` 表（含原始报文、请求头、
验签结果），验签失败也入库。所以「单子没到账」时先查这张表：

```bash
docker compose exec app python -c "
import sys;sys.path.insert(0,'.')
from app import db
for r in db.query_all('SELECT id,order_no,sign_ok,processed,result,created_at FROM payment_notifications ORDER BY id DESC LIMIT 5'): print(r)
"
```

* `sign_ok=0` → 密钥或签名算法不一致（`pay.easypay_key` / `pay.easypay_sign_type`）。
* 表里没有记录 → 回调根本没到服务器：域名解析、防火墙、Nginx 的 `/api/v1/pay/` 路径、
  HTTPS 证书要逐个排查。
* 回调丢失可以让用户点「查单」，或后台「人工确认收款」（会写流水与套餐，幂等）。

---

## 9. 备份与恢复

详见 `docs/RUNBOOK.md`，这里给最短版本：

```bash
# 备份（热备，不锁库）
docker compose exec app python tools/backup.py --keep 14

# 备份文件在数据卷里；拷到宿主机/对象存储
docker compose cp app:/app/data/backups ./backups

# 恢复
docker compose down
docker compose cp ./backups/spark-YYYYmmdd-HHMMSS.db app:/app/data/spark.db
docker compose up -d
```

**自动备份**（宿主机 crontab，每天 03:30）：

```cron
30 3 * * * cd /opt/spark-keeper-pro && docker compose exec -T app python tools/backup.py --keep 14 >> /var/log/spark-backup.log 2>&1
```

---

## 10. 升级

```bash
cd /opt/spark-keeper-pro
docker compose exec app python tools/backup.py --keep 14   # 1. 先备份，务必
docker compose cp app:/app/data/backups ./backups           # 2. 备份拉到宿主机
git pull   # 或上传新的交付目录覆盖（保留 .env 与 data 卷）
docker compose up -d --build                                # 3. entrypoint 会自动 migrate
docker compose logs -f app
```

* 数据库迁移是**幂等**的，`db/migrations/*.sql` 只按版本号执行一次。
* 升级不会动 `.env`；新增变量会在发布说明里列出。
* 回滚：换回旧镜像并恢复上一份备份（新 schema 一般不兼容旧代码，所以回滚必须连库一起回退）。

---

## 11. 日志与监控

```bash
docker compose logs -f --tail 200 app      # 容器日志（json-file，已限制 20m×5）
docker compose exec app python -c "import sys;sys.path.insert(0,'.');from app import db;[print(r) for r in db.query_all('SELECT created_at,level,source,message FROM run_logs ORDER BY id DESC LIMIT 20')]"
```

* 应用日志分两层：进程日志（容器日志）与 `run_logs` 表（页面「运行日志」可筛）。
* 管理员动作写 `audit_logs`（后台「审计日志」）。
* worker 心跳在 `worker_heartbeats`（后台「系统环境」能看到，超过 3 分钟没心跳说明调度器挂了）。
* 建议加的外部监控：`GET /api/v1/health` 200 检查（30s 一次）+ 磁盘使用率 + 容器内存。
* 日志清理：后台「日志清理」按天数删，或 `python -c "from app.common import logging; logging.prune(30)"`。

---

## 12. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 容器起来就退出，日志 `数据目录不可写` | 卷没挂上或宿主目录属主不对。容器内是 uid 10001：`chown -R 10001:10001 ./data` |
| 日志 `等待数据库超时` | 数据卷在慢速/网络盘上。调大 `SK_DB_WAIT_SECONDS`，或把数据目录放到本地盘 |
| `healthy` 一直不出现 | 端口被占（`ss -lntp \| grep 8000`）；或 `SK_SECRET` 太短导致启动报错 |
| **浏览器起不来**（任务全部 failed，日志有 chrome 启动失败） | ①`SK_CHROME_PATH` 指错：`docker compose exec app ls -l /usr/bin/chromium`；②容器沙箱限制：Chromium 需要 user namespace，若报 `Failed to move to new namespace` 或 `No usable sandbox`，按下面第 13 节处理；③`/dev/shm` 太小：compose 已设 `shm_size: 1gb`，裸机加 `--disable-dev-shm-usage` 支持 |
| 截图里中文是方块 | 缺中文字体：`fc-list \| grep -i noto` 应能看到 Noto Sans CJK。Dockerfile 已装 `fonts-noto-cjk`；裸机 `apt-get install fonts-noto-cjk` |
| 时间不对 / 任务在错误时间执行 | 用户时区是任务字段（`timezone`，默认 `Asia/Shanghai`）；容器时区看 `TZ`；`SK_TIMEZONE` 影响站点默认值。三者不一致时会「看起来晚了 8 小时」 |
| 端口占用 | `SK_HTTP_PORT` 换一个，或 `ss -lntp` 找出占用进程 |
| **支付回调不通** | `curl -v https://你的域名/api/v1/pay/notify/easypay`（GET 会返回 `fail` 纯文本，属正常响应）→ 若有响应说明链路通，问题在签名/参数；无响应查 DNS、防火墙、Nginx `/api/v1/pay/` 段 |
| 返回 429 太多 | 应用内限流 `SK_RATE_LIMIT_PER_MINUTE`，边缘 `nginx.conf` 的 `limit_req`。被限流先看是不是被刷 |
| 登录后立刻掉线 | `SK_SECRET` 变了（容器重建 + 未显式设置）→ 显式固定 `SK_SECRET` 并重新登录 |
| 忘记管理员密码 | `docker compose exec app python main.py admin --password '新密码'` |
| 提示 `no_active_package` | 该用户没有生效套餐：后台「授权套餐」或发卡密让用户兑换 |

---

## 13. 容器里 Chromium 的沙箱问题（重点）

Chromium 在容器里运行需要 user namespace。多数云主机默认可用；若日志里出现
`No usable sandbox!` / `Failed to move to new namespace`，按**优先级**处理：

1. **首选**：确认宿主机内核允许非特权 user namespace：

   ```bash
   sysctl kernel.unprivileged_userns_clone     # Debian/Ubuntu，期望 1
   sysctl user.max_user_namespaces             # 期望 > 0
   ```
   为 0 时：`echo 'kernel.unprivileged_userns_clone=1' > /etc/sysctl.d/99-chrome.conf && sysctl --system`。

2. **次选**：给容器放开 seccomp（保持非 root）：

   ```yaml
   security_opt: ["seccomp=unconfined", "no-new-privileges:true"]
   ```

3. **最后手段**：`cap_add: ["SYS_ADMIN"]`（等价于大幅削弱隔离，仅在前面都不行时用）。
   注意：`--no-sandbox` 是 Chromium 侧参数，是否透传由自动化模块决定；当前版本没有
   暴露该开关，如确需请向维护者提出（不要自己改 `app/automation/` 下的代码）。

---

## 14. 上线前安全清单

- [ ] `SK_SECRET` 显式设置且 ≥32 字符，只存在服务器上（不在 Git 里）
- [ ] `SK_ENV=prod`（错误响应不返回堆栈细节）
- [ ] 8000 端口**没有**直接暴露公网（只有 Nginx 443 对外）
- [ ] 全站 HTTPS，`SK_PUBLIC_URL` 用 https
- [ ] `INITIAL_ADMIN.txt` 已删除，管理员密码已改强密码
- [ ] `SK_TRUSTED_PROXIES` 与实际反代网段一致
- [ ] 已配置每日备份并**实际恢复演练过一次**
- [ ] `data/` 目录权限收紧（登录态文件权限 600；不要在备份里泄露）
- [ ] 关闭不需要的功能（不开放注册 / 不开在线支付时 `pay.enabled=0`）
- [ ] 定期看 `audit_logs` 与 `run_logs` 的 error 级别

---

## 15. 自检命令（交付包里就有）

```bash
python3 tools/selftest.py                 # 全部检查一键跑并汇总
python3 tools/selftest.py --only deploy   # 只校验部署产物静态规则
python3 tools/selftest.py --only contract # 接口契约一致性
python3 tools/backup.py --list            # 看已有备份
python3 main.py check                     # 环境体检
```

`tools/selftest.py` 会真起一次 HTTP 服务并跑测试套件；失败项会打印尾部日志，
可以直接贴给维护者。**部署机上跑通它，就等于确认了「安装向导 + 核心接口 + 数据库」都是好的。**
