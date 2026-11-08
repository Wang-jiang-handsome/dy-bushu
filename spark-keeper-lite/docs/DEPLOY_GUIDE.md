# 服务端部署指南（Windows 开发机 → Linux 云服务器）

> 面向本项目的实际用户：**本地是 Windows（`spark-keeper-lite` / `spark-keeper-pro` 目录），要把系统放到一台 Linux 云服务器上长期运行**。
>
> 本文回答两件事：
> 1. **应该部署哪些文件**（传什么、不传什么，逐项列清）
> 2. **怎么一步步部署**（Docker / 裸机两条路，都给出可直接复制的命令）
>
> 速查：本文档所在位置 `<交付目录>/docs/DEPLOY_GUIDE.md`；另有一份更偏「容器细节」的手册为 `<交付目录>/DEPLOY.md`。

---

## 0. 三分钟速览

```text
① 本地先自检：  python tools/verify_delivery.py --all
② 上传这 7 项： main.py  app/  db/  web/  deploy/  VERSION  （以及 .env.example、start.sh、docs/、tools/）
② 服务器：      cp .env.example .env  → 填 SK_SECRET / SK_PUBLIC_URL
③ 启动：        docker compose up -d --build      （或裸机 systemd，见第 5 节）
④ 拿密码：      docker compose exec app cat /app/data/INITIAL_ADMIN.txt
⑤ 访问：        https://你的域名  → 登录 → 立刻改密码
```

---

## 1. 先选部署方式

| | 方式 A：Docker Compose（**推荐**） | 方式 B：裸机 + systemd |
|---|---|---|
| 适合谁 | 绝大多数云服务器 | 不能用 Docker 的机器（公司限制 / 已装宝塔） |
| 浏览器依赖 | 镜像里自带 Chromium + 中文字体，**不用自己装** | 需要自己 `apt install chromium fonts-noto-cjk` |
| 升级 | `docker compose up -d --build` | 覆盖代码文件 + `systemctl restart` |
| 环境隔离 | 好（容器内非 root 用户 10001） | 一般（跟系统 Python 共用） |
| 资源占用 | 略高（镜像约 1 GB） | 略低 |
| 本文位置 | 第 4 节 | 第 5 节 |

> 两种方式**数据都在 `data/`**，随时可以从一种迁到另一种。

---

## 2. 应该部署哪些文件（重点）

### 2.1 必须上传（少了跑不起来）

| 路径 | 说明 | 大小 |
|---|---|---|
| `main.py` | 唯一入口（`init` / `serve` / `migrate` / `check` / `diagnose` …） | 小 |
| `app/` | 全部后端代码（API、自动化、调度器、安全） | 中 |
| `db/` | 数据库迁移脚本（启动时自动执行） | 小 |
| `web/` | 前端静态文件（`web/static/` 下的 HTML/CSS/JS/vendor） | 中 |
| `deploy/` | `Dockerfile`、`docker-compose`、`nginx.conf`、`entrypoint.sh` | 小 |
| `.env.example` | 配置模板（复制成 `.env` 再改） | 小 |
| `VERSION` | 版本号（升级/排障时确认版本） | 极小 |

### 2.2 建议一起上传（强烈推荐，不是必需）

| 路径 | 为什么建议带上 |
|---|---|
| `docker-compose.yml` | 根目录直接 `docker compose up -d`（Docker 方式必需） |
| `.dockerignore` | 防止把 `data/`、`.env` 打进镜像 |
| `nginx.conf` | HTTPS 反代模板，改个域名就能用 |
| `start.sh` | 裸机一键启动（找 Python → 建目录 → 初始化 → 起服务） |
| `docs/` | `RUNBOOK.md` 运维手册、`API_CONTRACT.md` 接口契约、本文 |
| `tools/` | `backup.py` 备份、`selftest.py` 自检、`get_cookie.py` 抓 Cookie、`diagnose` 辅助 |
| `README.md` | 使用说明 |

### 2.3 **绝对不要上传**（会导致数据覆盖/泄漏/体积膨胀）

| 路径 | 原因 |
|---|---|
| `data/` | **你的线上数据**：`spark.db`、`secret.key`、登录态 Cookie、截图、备份。上传本地旧数据会覆盖服务器数据 |
| `data/sessions/` | 抖音登录态（等于别人的账号钥匙），**绝不能进镜像或仓库** |
| `.env` | 含有 `SK_SECRET` 和 SMTP/短信密钥；服务器上单独写 |
| `tests/` | 开发测试集，生产不需要（也别在服务器上跑，会建测试库） |
| `__pycache__/`、`*.pyc` | Python 缓存，跨机器/跨版本无意义 |
| `.git/` | 版本历史，生产不需要 |
| 任何 `*.db`、`*.db-wal`、`*.db-shm` | 数据库文件（属于 `data/`） |
| `INITIAL_ADMIN.txt` | 初始密码文件；服务器上会重新生成，传上去等于泄露密码 |

> **一句话记法**：`data/`、`.env`、`tests/`、`__pycache__/`、`.git/` 永远不传；其余代码目录全传。

### 2.4 上传方式（Windows → Linux，任选一种）

**方式一：`scp` / `sftp`（最简单，PowerShell 里直接跑）**

```powershell
# 在 Windows 的「交付目录的上一级」执行（把 D:\DeppSeek WorkRegion 换成你的实际路径）
scp -r "D:\DeppSeek WorkRegion\spark-keeper-lite" root@你的服务器IP:/opt/spark-keeper
```

**方式二：`rsync`（可续传、自动跳过忽略项，推荐大目录）**

```bash
# 在 Windows 的 Git Bash / WSL 里执行
rsync -avz --progress \
  --exclude 'data/' --exclude '.env' --exclude 'tests/' --exclude '__pycache__/' \
  --exclude '.git/' --exclude '*.pyc' \
  "/d/DeppSeek WorkRegion/spark-keeper-lite/" root@你的服务器IP:/opt/spark-keeper/
```

**方式三：WinSCP / FileZilla 图形界面**
直接把文件夹拖上去，**手动排除** `data`、`.env`、`tests`、`__pycache__`、`.git`。

**方式四：先压缩再上传（目标机没网/传输慢时）**

```powershell
# 本地：打成 zip（先排除大目录）
Compress-Archive -Path "D:\DeppSeek WorkRegion\spark-keeper-lite\*" `
                 -DestinationPath "$env:TEMP\spark-keeper.zip" -Force
scp "$env:TEMP\spark-keeper.zip" root@你的服务器IP:/opt/
```

```bash
# 服务器
mkdir -p /opt/spark-keeper && cd /opt && unzip -o spark-keeper.zip -d spark-keeper
```

### 2.5 如果你想迁移已有数据（本地 → 服务器）

> 默认**不要迁**：服务器首启会自己建库、建管理员。只有当你已经在本地积累了账号/好友/任务，想搬过去时才做。

```bash
# 1) 服务器上先停服务（Docker）
docker compose stop
# 2) 本地停掉 start.bat（必须！否则 SQLite 还在写）
# 3) 只传这三样（其余 data/ 内容不带）
scp "本地\data\spark.db"               root@IP:/opt/spark-keeper/data/
scp "本地\data\secret.key"             root@IP:/opt/spark-keeper/data/
scp -r "本地\data\sessions\*"          root@IP:/opt/spark-keeper/data/sessions/
# 4) 权限（Docker 方式容器内用户是 10001）
chown -R 10001:10001 /opt/spark-keeper/data
# 5) 启动
docker compose up -d
```

> `secret.key` 决定 JWT 签名与登录态加密：**换掉它，所有用户都会被迫重新登录，已保存的抖音登录态也可能失效**。迁移时一定要带上。

### 2.6 一个容易踩的坑：在本地跑过自检后再上传

`tools/verify_delivery.py`、`selftest.py` 会在**交付目录里生成 `data/`**（测试库、截图）。上传前务必确认：

```powershell
# 本地删除这些运行时产物（保留交付包里的空骨架）
Remove-Item -Recurse -Force ".\spark-keeper-lite\data\tmp"," .\spark-keeper-lite\data\*.db" -ErrorAction SilentlyContinue
```

如果嫌麻烦，直接用 `rsync` 的 `--exclude 'data/'` 最稳。

---

## 3. 服务器准备

### 3.1 配置建议

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| CPU | 1 核 | 2 核 | 每次任务要起一个无头浏览器 |
| 内存 | 1 GB | 2~4 GB | Chromium 单实例约 300~600 MB；`SK_MAX_CONCURRENT_BROWSERS=2` 时更吃内存 |
| 磁盘 | 10 GB | 20 GB+ | 截图会累积（可在后台清理） |
| 系统 | Ubuntu 22.04 / Debian 12 | 同 | CentOS/Alpine 也能跑，但下面的命令按 apt 写 |

> 内存 1 GB 的机器请把 `SK_MAX_CONCURRENT_BROWSERS` 设为 `1`，并考虑加 1~2 GB swap。

### 3.2 端口与防火墙

| 端口 | 用途 | 是否对外 |
|---|---|---|
| 22 | SSH | 可以只对你自己的 IP 开放 |
| 80 | HTTP（跳转 HTTPS / Let's Encrypt 验证） | **开放** |
| 443 | HTTPS | **开放** |
| 8000 | 应用本体 | **不要对公网开放**（只监听 `127.0.0.1`，由 Nginx 反代） |

```bash
# Ubuntu ufw 示例
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 3.3 时区（务必设置）

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl | head -3
```

> 定时任务的「每天 23:24」是按 `SK_TIMEZONE`（默认 `Asia/Shanghai`）算的，服务器时区错了不影响业务，但日志时间会让你排障崩溃。

### 3.4 裸机额外依赖（Docker 方式跳过）

```bash
sudo apt update
sudo apt install -y python3 python3-venv chromium fonts-noto-cjk tzdata ca-certificates unzip
# 验证 Chromium 可执行
which chromium || which chromium-browser || which google-chrome
```

> **中文字体必须装**：没有 `fonts-noto-cjk`，抖音页面截图全是方块，线上排障等于瞎。
> Python 只用标准库，**不需要 `pip install` 任何东西**。

---

## 4. 方式 A：Docker Compose 部署（推荐）

### 4.1 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker --version && docker compose version
```

### 4.2 上传代码并写配置

```bash
sudo mkdir -p /opt/spark-keeper
cd /opt/spark-keeper
# （此时用第 2.4 节的方法把文件传进来）
ls    # 应该看到 main.py / app / db / web / deploy / docker-compose.yml ...

cp .env.example .env
# 生成随机密钥并写进 .env
python3 -c "import secrets;print('SK_SECRET='+secrets.token_urlsafe(48))"
```

用编辑器改 `.env`，**最少改这三行**：

```dotenv
SK_SECRET=把上面生成的整行粘进来
SK_PUBLIC_URL=https://你的域名.com
TZ=Asia/Shanghai
```

常用可选：

```dotenv
SK_HTTP_PORT=8000            # 宿主机监听端口（默认只绑 127.0.0.1）
SK_BIND=127.0.0.1            # 公网访问走 Nginx，不要改成 0.0.0.0
SK_MAX_CONCURRENT_BROWSERS=2 # 并发浏览器数，小内存改 1
SK_UNLIMITED_TASK_RUNS=1     # 1=不限制每天执行次数（自用推荐）
SK_SCHEDULER_ENABLED=1       # 1=容器内启动定时调度器
```

### 4.3 启动

```bash
cd /opt/spark-keeper
docker compose up -d --build

# 看启动日志（第一次会迁移数据库、建管理员）
docker compose logs -f app
# Ctrl+C 退出日志查看，不影响容器

# 健康检查
curl -fsS http://127.0.0.1:8000/api/v1/health && echo
```

### 4.4 拿初始管理员密码

```bash
docker compose exec app cat /app/data/INITIAL_ADMIN.txt
# 输出示例：
#   username=admin
#   password=xxxxxxxxxxxxxxxx
```

> 登录后立刻改密码，然后删掉这个文件：
> `docker compose exec app rm -f /app/data/INITIAL_ADMIN.txt`

### 4.5 可选：把「调度器」拆成独立容器

单容器（默认）已经同时跑「Web + 调度器」，**大多数情况不用拆**。只有在想单独重启 Web 而不打断任务时才拆：

```bash
# 1) .env 里关掉 app 容器的调度器
SK_SCHEDULER_ENABLED=0

# 2) 启动 worker profile（只跑调度器，不起 Web）
docker compose --profile worker up -d
```

> ⚠️ **同一个账号只能有一个调度器**。两个都开着会出现「同一分钟发两条」，也可能互相抢 Chrome profile 导致任务失败。

### 4.6 常用运维命令

```bash
docker compose ps                 # 状态/健康
docker compose restart app        # 重启（不重建镜像）
docker compose down               # 停止（数据卷保留）
docker compose down -v            # ⚠️ 连数据卷一起删，会丢数据
docker compose logs --tail 200 app
docker compose exec app python main.py check        # 环境体检
docker compose exec app python tools/selftest.py    # 交付自检
```

---

## 5. 方式 B：裸机 + systemd

### 5.1 建用户和目录

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin spark
sudo mkdir -p /opt/spark-keeper
sudo chown spark:spark /opt/spark-keeper
```

### 5.2 上传并初始化

```bash
cd /opt/spark-keeper
# （上传文件，见 2.4）
cp .env.example .env
python3 -c "import secrets;print('SK_SECRET='+secrets.token_urlsafe(48))"   # 写进 .env
```

`.env` 关键项（裸机）：

```dotenv
SK_ENV=prod
SK_HOST=127.0.0.1          # 只监听本机，公网走 Nginx
SK_PORT=8000
SK_DATA_DIR=/opt/spark-keeper/data
SK_PUBLIC_URL=https://你的域名.com
SK_TIMEZONE=Asia/Shanghai
SK_SECRET=上面生成的值
SK_CHROME_PATH=/usr/bin/chromium    # which chromium 的结果
SK_BROWSER_HEADLESS=1
SK_SCHEDULER_ENABLED=1
SK_UNLIMITED_TASK_RUNS=1
TZ=Asia/Shanghai
```

首次初始化 + 体检：

```bash
cd /opt/spark-keeper
sudo -u spark python3 main.py check      # 环境体检：Python/磁盘/浏览器/时区
sudo -u spark python3 main.py init       # 建库 + 建管理员（密码只打印一次）
sudo cat data/INITIAL_ADMIN.txt          # 也可以从这里读
```

试跑前台（确认没问题再交给 systemd）：

```bash
sudo -u spark python3 main.py serve --host 127.0.0.1 --port 8000
# 另开一个终端： curl -fsS http://127.0.0.1:8000/api/v1/health
```

### 5.3 systemd 服务

```bash
sudo tee /etc/systemd/system/spark-keeper.service >/dev/null <<'EOF'
[Unit]
Description=SparkKeeper (Douyin spark keeper)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=spark
Group=spark
WorkingDirectory=/opt/spark-keeper
EnvironmentFile=/opt/spark-keeper/.env
ExecStart=/usr/bin/python3 /opt/spark-keeper/main.py serve --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
# 浏览器和 SQLite 都需要写 data/
ReadWritePaths=/opt/spark-keeper/data
# 崩溃时留一手
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spark-keeper

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now spark-keeper
sudo systemctl status spark-keeper --no-pager
sudo journalctl -u spark-keeper -f      # 看日志
```

> **注意**：`EnvironmentFile` 里的值不要加引号；`SK_DATA_DIR` 里的目录属主必须是 `spark`。

### 5.4 裸机升级 / 备份

```bash
# 升级：只替换代码，不碰 data/
sudo systemctl stop spark-keeper
sudo rsync -a --exclude 'data/' --exclude '.env' 新版本/ /opt/spark-keeper/
sudo chown -R spark:spark /opt/spark-keeper
sudo systemctl start spark-keeper      # 启动时自动跑数据库迁移
```

---

## 6. Nginx + HTTPS

### 6.1 装 Nginx 和证书

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 6.2 用项目自带的模板（改 3 处即可）

```bash
sudo cp /opt/spark-keeper/deploy/nginx.conf /etc/nginx/sites-available/spark-keeper.conf
sudo vim /etc/nginx/sites-available/spark-keeper.conf
```

要改的地方：

1. `server_name spark.example.com;` → 你的域名（两处：80 和 443）
2. `ssl_certificate ... /etc/letsencrypt/live/spark.example.com/...` → 你的域名路径
3. `upstream spark_backend { server 127.0.0.1:8000; }` → 若应用换了端口，这里同步改

启用并测试：

```bash
sudo ln -sf /etc/nginx/sites-available/spark-keeper.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 6.3 申请免费证书

```bash
sudo certbot --nginx -d 你的域名.com
# 自动续期由 certbot.timer 负责，验证：
sudo certbot renew --dry-run
```

懒人版（没有域名证书时先自签，浏览器会警告）：模板里已写好注释，把 `ssl_certificate` 指向自签证书即可。

### 6.4 必需的三项反代设置（模板里已有，别删）

```nginx
client_max_body_size 10m;          # 上传 Cookie / 批量导入友好
proxy_buffering off;               # 「执行任务」这类接口可能长时间无输出
proxy_read_timeout 120s;           # 起浏览器 + 打开会话可能 > 60s
```

> **别把 8000 端口直接暴露到公网**：项目没有在应用层做 HTTP→HTTPS 强制跳转，裸奔会泄露会话 Cookie。

---

## 7. 首次登录后必须做的 6 件事

1. **改管理员密码**：右下角用户菜单 → 修改密码；然后删掉 `data/INITIAL_ADMIN.txt`。
2. **确认套餐/额度**：自用建议 `.env` 里 `SK_UNLIMITED_TASK_RUNS=1`（不限制每天执行次数）。
3. **添加抖音账号**（三种方式）：
   - **网页内扫码**：账号页 → 「扫码登录」，二维码会显示在网页里（服务器无头也能出图，用手机抖音扫）。
   - **窗口登录**：需要服务器有图形界面（远程桌面/VNC）才可用，云服务器一般没有。
   - **导入 Cookie（服务器最稳）**：在本机 Windows 跑 `get-cookie.bat` 或
     `python tools/get_cookie.py`，把抓到的 Cookie 文本粘到「导入 Cookie」里。
4. **同步好友**：好友管理 → 「从抖音同步」，然后**删掉不需要的**（支持勾选批量删除）。
5. **建任务**：定时任务 → 新建 → 选账号、时间、好友范围（全部/指定）、随机文案。
6. **确认调度器在跑**：任务列表「下次执行」应显示时间 + 抖动窗口（如 `10:41 ~ 10:46`）。

> **关于抖动**：任务默认带 ±5 分钟随机抖动（防机器行为风控）。「10:41 的任务」实际在 10:41~10:46 之间跑，**不是没执行**。不想抖动就在任务高级设置里把抖动设为 0。

> **关于卡密/支付**：`pro` 版有在线支付与发卡；`lite` 版只有卡密（无支付接口，访问 `/api/v1/pay/*` 返回 404 属正常）。
> 生成卡密：`python main.py card --package monthly --count 10`。

---

## 8. 日常运维

### 8.1 日志

| 位置 | 内容 |
|---|---|
| 页面「执行记录」 | 每个好友的结果、实际发送内容、失败原因、截图 |
| `data/spark.db` → `run_logs` | 系统/调度/任务日志（页面「系统日志」可查） |
| `docker compose logs -f app` | 进程级 stdout（崩溃栈在这） |
| `journalctl -u spark-keeper -f` | 裸机方式同上 |
| `data/diagnostics/` | `main.py diagnose` 导出的页面结构报告（排障用） |

### 8.2 备份（**必须做**）

```bash
# 方式一：项目自带工具（热备，不锁库）
python3 tools/backup.py --keep 14            # 默认写到 data/backups/
python3 tools/backup.py --list

# 方式二：Docker 里
docker compose exec app python tools/backup.py --keep 14

# 方式三：sqlite 官方备份
sqlite3 data/spark.db ".backup 'data/backups/spark-$(date +%F).db'"
```

**备份必须包含**：`data/spark.db`、`data/secret.key`、`data/sessions/`。
（`screenshots/`、`uploads/` 可选；`profiles/` 是浏览器缓存，可以不备，重登会重建。）

定时任务示例（每天 04:10）：

```bash
sudo crontab -u spark -e
# 加入：
10 4 * * * cd /opt/spark-keeper && /usr/bin/python3 tools/backup.py --keep 14 >> data/backups/cron.log 2>&1
```

### 8.3 恢复

```bash
docker compose stop            # 或 sudo systemctl stop spark-keeper
cp data/backups/spark-2026-10-18.db data/spark.db
rm -f data/spark.db-wal data/spark.db-shm     # 一定要删，否则旧 WAL 会重放
chown -R 10001:10001 data                     # Docker 方式
docker compose start
```

### 8.4 升级

```bash
# 1) 先备份
docker compose exec app python tools/backup.py
# 2) 上传新代码（**不要覆盖 data/ 和 .env**）
# 3) 重建并启动（启动时自动跑数据库迁移）
docker compose up -d --build
# 4) 验证
docker compose exec app python tools/verify_delivery.py --all
curl -fsS http://127.0.0.1:8000/api/v1/health
```

裸机升级见 5.4。**回滚**：把代码目录换回旧版本、`data/` 保持不动、重启即可（数据库迁移不会回滚，但通常向后兼容）。

### 8.5 多实例禁忌

- **同一 `data/`（同一 SQLite 库）不能被两个进程同时写**：会 `database is locked`。
- **同一抖音账号不能被两个进程同时驱动 Chrome**：Chrome 对同一 `user-data-dir` 是单例锁，后启动的会把先启动的顶掉，表现为任务中途 `CDP 连接已关闭`。
- 结论：**一台机器只跑一个 Web + 一个调度器**；要横向扩容必须「每个实例独立 data 目录 + 不同账号」。

---

## 9. 故障排查表

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| 打开域名 502 | 应用没起 / 端口不对 | `docker compose ps`、`curl 127.0.0.1:8000/api/v1/health` |
| 页面能开但登录后立刻掉线 | `SK_SECRET` 变了（重启时重新生成） | `.env` 里把 `SK_SECRET` 固定写成一行长随机串 |
| 定时任务「到点没跑」 | ① ±5 分钟抖动 ② 任务被停用 ③ 服务没运行 | 看任务列表抖动窗口；确认「已启用」；`docker compose ps` |
| 任务报 `conversation_not_opened` | 会话列表虚拟滚动、好友排在后面 | 已自动滚动/搜索兜底；仍失败就 `main.py diagnose --account-id N` 看报告 |
| 任务报 `input_failed` | 抖音输入框改版 | 跑 diagnose，把 `report.json` 发给开发者 |
| 任务中途 `CDP 连接已关闭` | 同一 profile 被第二个进程抢占 / 内存不足 | 只跑一个调度器；调小 `SK_MAX_CONCURRENT_BROWSERS`；加 swap |
| 浏览器起不来（容器里） | sandbox / `/dev/shm` 太小 | compose 已配 `shm_size: 1gb`；容器内 root 会自动加 `--no-sandbox`；万不得已再加 `security_opt` |
| 截图全是方块 | 缺中文字体 | `apt install fonts-noto-cjk`（Docker 镜像已装） |
| `database is locked` | 两个进程写同一个库 / 磁盘慢 | 只跑一个进程；调大 `SK_DB_BUSY_TIMEOUT_MS=8000` |
| 上传 Cookie 提示「登录态无效」 | Cookie 过期 / 抓的是另一个账号 | 本机重新 `get-cookie`；确认抓的是同一抖音账号 |
| 返回 402 | 套餐不含该能力/额度不足 | 后台发卡并在「我的套餐」兑换，或设 `SK_UNLIMITED_TASK_RUNS=1` |
| 返回 429 `task_daily_limit` | 当日次数用尽 | 等次日 / 升级套餐 |
| 权限拒绝（data 写不进去） | 目录属主不对 | Docker：`chown -R 10001:10001 data`；裸机：`chown -R spark:spark data` |
| 日志时间差 8 小时 | 容器/系统时区 | `TZ=Asia/Shanghai`、`timedatectl set-timezone Asia/Shanghai` |

排障三件套：

```bash
python main.py check                                  # 环境体检
python main.py diagnose --account-id 1                # 抖音页面结构报告
python tools/selftest.py                              # 交付自检（16 项）
```

---

## 10. 上线前安全清单

- [ ] `SK_SECRET` 已固定为长随机串（≥32 字节），并纳入备份
- [ ] `SK_ENV=prod`，`SK_DEMO_MODE=0`
- [ ] 应用只监听 `127.0.0.1:8000`，公网仅 80/443
- [ ] 已配置 HTTPS 且证书自动续期（`certbot renew --dry-run` 通过）
- [ ] 管理员密码已改，`data/INITIAL_ADMIN.txt` 已删除
- [ ] `data/` 权限最小化（Docker `10001` / 裸机 `spark`），不含全局可读
- [ ] 防火墙只放 22/80/443；SSH 已禁用密码登录（改用密钥）
- [ ] 每日自动备份 + 至少一次**真实恢复演练**
- [ ] 日志保留天数 `SK_LOG_KEEP_DAYS` 已按磁盘容量设置

---

## 11. 让所有人都能访问（公网发布 + 多用户）

前面 1~10 节解决的是「服务能在服务器上跑起来」。这一节解决你真正关心的问题：**别人打开浏览器输入网址就能用**。

### 11.1 先分清两种目标

| 目标 | 典型特征 | 要做的事 |
|---|---|---|
| **A. 自用 / 小圈子**（推荐先做这个） | 只有你 + 几个朋友，每人 1~2 个抖音账号 | 开放注册 + 发卡密 + 一台 2C4G 机器就够 |
| **B. 对外公开服务** | 陌生人自助注册付费，可能几十上百用户 | 在 A 的基础上加：套餐/额度、卡密批量发、监控告警、容量规划（见 11.6） |

项目**天生是多用户**的：每个用户只能看到自己的抖音账号 / 好友 / 任务 / 执行记录（有自动化测试覆盖越权访问），管理员后台统一管卡密、套餐、用户、日志。

### 11.2 公网访问必须打通的 4 层（按顺序检查，缺一层就打不开）

| 层 | 检查点 | 常见错 |
|---|---|---|
| ① DNS | 域名 A 记录指向服务器公网 IP | 解析没生效（`ping 域名` 看 IP 对不对） |
| ② 备案 | **中国大陆服务器**：域名必须完成 ICP 备案，否则 80/443 会被运营商拦截 | 没备案 → 换香港/海外服务器，或先备案（约 1~3 周） |
| ③ 云安全组 | 放行入方向 **22 / 80 / 443** | 只放了 22，网页打不开 |
| ④ 应用监听 | 容器内 `SK_HOST=0.0.0.0`；宿主机映射 `SK_BIND` 不要只绑 127.0.0.1（走 Nginx 时才绑 127.0.0.1） | 端口映射成 `127.0.0.1:8000`，外网访问不到 |

> 判断方法：在服务器上 `curl -I http://127.0.0.1:8000`（应用正常）→ 在本地 `curl -I http://公网IP:8000`（安全组正常）→ 浏览器访问域名（DNS/备案/Nginx 正常）。

### 11.3 最快跑通（临时方案，不带 HTTPS）

想立刻验证「别人能不能打开」，可以先跳过 Nginx：

```bash
cd /opt/spark-keeper
# .env 里把绑定的宿主地址改成 0.0.0.0（默认是 127.0.0.1，只有本机能访问）
SK_BIND=0.0.0.0
SK_HTTP_PORT=8000

docker compose up -d
```

云控制台安全组放行 **8000**，然后让朋友访问 `http://你的公网IP:8000`。

⚠️ 这个方式**没有 HTTPS**，登录 Cookie 会明文传输，且 8000 端口暴露在公网。**只用来临时验证，验证完立刻改回**：

```dotenv
SK_BIND=127.0.0.1
SK_HTTP_PORT=8000
```

### 11.4 正式方案：Nginx + HTTPS（所有人可访问）

```bash
# 1) 应用只监听本机
#    .env: SK_BIND=127.0.0.1  SK_HTTP_PORT=8000
docker compose up -d

# 2) Nginx 反代（模板在 deploy/nginx.conf，改域名即可）
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp /opt/spark-keeper/deploy/nginx.conf /etc/nginx/sites-available/spark-keeper.conf
sudo vim /etc/nginx/sites-available/spark-keeper.conf   # 改 server_name 与证书路径（两处）
sudo ln -sf /etc/nginx/sites-available/spark-keeper.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 3) 免费证书（自动续期）
sudo certbot --nginx -d 你的域名.com
sudo certbot renew --dry-run
```

关键点：

- `.env` 里 **`SK_PUBLIC_URL` 必须是 `https://你的域名`**（邮件里的链接、支付回调都靠它）
- Nginx 必须保留 `proxy_buffering off`、`proxy_read_timeout 120s`（「执行任务」接口会长时间无输出，缓冲会超时）
- 配了 HTTPS 后，**只把 443 对外**，80 只做跳转；8000 永远不要开在公网

验证：

```bash
curl -I https://你的域名                # 200/302
curl -fsS https://你的域名/api/v1/health && echo
```

### 11.5 应用层：让「所有人」能注册、能用自己的账号

全部在**管理后台**点，不用改代码：

1. **开放注册**（默认已开）
   后台 → **系统配置** → `auth` 分组 → 「是否开放注册」= `1`（对应 `auth.allow_register`）。
   想收紧成邀请制：设为 `0`，或把「注册是否必须邮箱验证」(`auth.require_email_verify`) 设为 `1` 并配好 `SK_SMTP_*`。
2. **配套餐**（决定每个用户能用多少）
   后台 → **套餐管理**：每个套餐可设 **价格 / 天数 / 账号上限 / 每日次数**。
   - 账号上限：用户最多绑几个抖音账号
   - 每日次数：每个账号每天自动执行上限（手动执行也计入）
   - 体验卡把价格设 0，走卡密开通流程
3. **发卡密**
   后台 → **卡密管理** → 选套餐批量生成；或命令行：
   ```bash
   docker compose exec app python main.py card --package monthly --count 50
   ```
   把卡密发给朋友 → 他们注册后在「卡密兑换」输入即可开通。
4. **每个用户自己绑号**
   用户登录 → 「抖音账号」→ 扫码 / 导入 Cookie → 建任务。
   管理员可在 **用户管理 / 账号管理 / 执行记录** 里看到所有人的情况。
5. **防滥用（对外公开必配）**
   - `SK_RATE_LIMIT_PER_MINUTE`（默认 240，公网建议 120~240）
   - 登录失败锁定：`SK_MAX_LOGIN_FAIL=8`、`SK_LOGIN_LOCK_SECONDS=600`
   - 套餐的「账号上限 / 每日次数」是最有效的成本闸门，别给无限

### 11.6 容量规划（很多人同时用会不会崩）

浏览器自动化是资源大头：**一个无头 Chromium 大约 300~600 MB 内存**。

| 机器 | 建议 `SK_MAX_CONCURRENT_BROWSERS` | 能支撑的规模（经验值） |
|---|---|---|
| 2 核 2 GB | 1 | ~10 个账号、错峰执行 |
| 2 核 4 GB | 2 | ~30~50 个账号 |
| 4 核 8 GB | 3~4 | ~100 个账号 |
| 再往上 | — | **拆多台**（每台独立 `data/`、独立域名/端口、不同用户） |

降低峰值的三招（都不改代码）：

1. **错峰**：让不同用户的执行时间分散（任务时间 + 抖动本身就是为此设计的）
2. **限并发**：`SK_MAX_CONCURRENT_BROWSERS` 调到机器能承受的值，拿不到额度时任务会排队/跳过而不是把机器打爆
3. **限账号**：套餐「账号上限」控制单用户规模

> ⚠️ **不要为了扩容在同一台机器起两个实例**：同一 `data/` 的 SQLite 会被两个进程同时写（`database is locked`），同一抖音账号的 Chrome profile 也会互相顶掉（表现为任务中途 `CDP 连接已关闭`）。要扩容就加机器、各自独立 `data/`。

### 11.7 上线后验证清单（找个朋友真实测一遍）

- [ ] 手机 4G（不用公司 WiFi）打开 `https://你的域名` 能出登录页
- [ ] 「注册」能创建账号（密码强度符合要求）
- [ ] 「卡密兑换」能开通套餐，「我的套餐」显示到期时间
- [ ] 「抖音账号」扫码能登录成功（状态变「在线」）
- [ ] 「好友管理 → 从抖音同步」能拉到会话
- [ ] 建任务 → 点「执行」能成功发出一条
- [ ] 到点后自动执行（注意 **±5 分钟抖动窗口**，不要看到点没跑就以为坏了）
- [ ] 换第二个用户注册，确认**互相看不到对方的数据**（账号/好友/记录）
- [ ] 后台「系统配置 / 套餐 / 卡密 / 用户 / 日志」都能正常打开

### 11.8 对外服务前再想三件事

1. **合规**：自动化操作社交平台属于「辅助工具」范畴，请只用于自己的账号或已授权账号；公开售卖前先确认当地法规与平台规则。
2. **数据安全**：`data/` 里是所有用户的抖音登录态（等同于账号钥匙）。务必：只给 `spark`/`10001` 读、每日备份、**不要**把 `data/` 放进任何 Git 仓库或网盘同步目录。
3. **出事能救**：配好每日备份 + 至少演练一次恢复；后台「系统日志 / 执行记录」是你排查用户投诉的第一现场。

---

## 附录 A：环境变量速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `SK_ENV` | `dev` | `prod` 会关闭调试细节 |
| `SK_HOST` / `SK_PORT` | `127.0.0.1` / `8000` | 监听地址/端口 |
| `SK_DATA_DIR` | `./data` | **数据根目录**（库、密钥、登录态、截图） |
| `SK_PUBLIC_URL` | 空 | 站点对外地址，用于邮件链接/支付回调 |
| `SK_SECRET` | 随机生成 | JWT/加密密钥，**必须固定** |
| `SK_TIMEZONE` | `Asia/Shanghai` | 任务时间按此计算 |
| `SK_ACCESS_TTL` / `SK_REFRESH_TTL` | `7200` / `1209600` | 令牌有效期（秒） |
| `SK_RATE_LIMIT_PER_MINUTE` | `240` | 接口限流 |
| `SK_MAX_LOGIN_FAIL` / `SK_LOGIN_LOCK_SECONDS` | `8` / `600` | 登录失败锁定 |
| `SK_SMTP_*` | 空 | 邮件通道（未配置时验证码打印到日志） |
| `SK_SMS_PROVIDER` / `SK_SMS_*` | 空 | 短信通道 |
| `SK_CHROME_PATH` | 自动探测 | Chromium/Chrome 可执行文件路径 |
| `SK_BROWSER_HEADLESS` | `1` | 无头模式（服务器必须 1） |
| `SK_BROWSER_WIDTH` / `SK_BROWSER_HEIGHT` | `1366` / `900` | 窗口尺寸 |
| `SK_MAX_CONCURRENT_BROWSERS` | `2` | 并发浏览器上限 |
| `SK_UNLIMITED_TASK_RUNS` | `1` | 1=不限每日执行次数 |
| `SK_TASK_TIMEOUT` | `180` | 单次任务超时（秒） |
| `SK_TASK_RETRY_BACKOFF` | `60` | 失败重试间隔（秒） |
| `SK_DEMO_MODE` | `0` | 1=演示模式（不启真实浏览器） |
| `SK_SCHEDULER_ENABLED` | `1` | 1=启动内置调度器 |
| `SK_SCHEDULER_TICK` | `20` | 调度轮询间隔（秒） |
| `SK_LOG_KEEP_DAYS` | `30` | 日志保留天数 |
| `SK_DB_BUSY_TIMEOUT_MS` | `8000` | SQLite 锁等待 |
| `SK_DB_WAIT_SECONDS` | `30` | 容器启动等数据库就绪的秒数 |
| `TZ` | `Asia/Shanghai` | 容器/进程时区 |

## 附录 B：目录结构说明

```text
/opt/spark-keeper/
├── main.py                 # 入口：init / serve / worker / migrate / admin / card / task / check / diagnose
├── app/                    # 后端代码（api / automation / worker / common）
├── db/                     # SQLite 迁移脚本（启动时自动执行）
├── web/static/             # 前端静态资源（含 vendor 的 Vue）
├── deploy/                 # Dockerfile / compose / nginx.conf / entrypoint.sh
├── docs/                   # 本文、RUNBOOK、API 契约
├── tools/                  # backup / selftest / verify_delivery / get_cookie …
├── docker-compose.yml
├── .env                    # ← 你的配置（自己写，不入库）
└── data/                   # ← 全部运行时数据（必须持久化 + 备份）
    ├── spark.db            # 主数据库（WAL 模式，另有 -wal/-shm）
    ├── secret.key          # 加密密钥（迁移/恢复必须带上）
    ├── INITIAL_ADMIN.txt   # 初始管理员密码（登录后删）
    ├── sessions/           # 抖音登录态（等同于账号钥匙，严格保密）
    ├── screenshots/        # 执行截图
    ├── uploads/            # 上传文件
    ├── backups/            # 备份产物
    ├── diagnostics/        # diagnose 报告
    └── profiles/           # Chrome 用户目录（可丢弃，重登重建）
```

## 附录 C：常用命令速查

```bash
# ── CLI（Docker 方式在命令前加：docker compose exec app）──
python main.py check                               # 环境体检
python main.py init                                # 初始化（幂等）
python main.py serve --host 127.0.0.1 --port 8000  # 启动（含调度器）
python main.py worker                              # 只跑调度器
python main.py migrate                             # 手动迁移数据库
python main.py admin --username admin --password 新密码   # 重置管理员
python main.py card --package monthly --count 20   # 生成卡密
python main.py task --account-id 1 --dry-run       # 试跑一个账号（不发送）
python main.py diagnose --account-id 1             # 导出页面结构报告

# ── 自检 ──
python tools/selftest.py                           # 交付自检
python tools/verify_delivery.py --all              # 交付一致性（上传前在本地跑）

# ── 服务 ──
docker compose up -d --build                       # 启动/更新
docker compose logs -f app                         # 看日志
docker compose restart app                         # 重启
systemctl restart spark-keeper                     # 裸机重启
journalctl -u spark-keeper -f                      # 裸机日志

# ── 备份 ──
python tools/backup.py --keep 14
```

---

## 一句话总结

**传代码目录（`main.py` + `app/` + `db/` + `web/` + `deploy/` + 配置文件），不传 `data/`；服务器上用 Docker 起、Nginx 上 HTTPS、每天备份 `data/`。**
