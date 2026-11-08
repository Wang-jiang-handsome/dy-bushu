# 部署平台怎么选：Netlify / Vercel 能不能用？

> 面向「我想把它挂到网上、关掉电脑也能自动续火花」的场景。
> 想直接照抄命令的看第 3 节；只想知道 Netlify 行不行的看第 1、2 节。

---

## 1. 一句话结论

**不能整站部署到 Netlify / Vercel 这类静态 + Serverless 平台。**

本项目不是「静态网页 + 几个接口」，而是一个**常驻进程**：

```
Python 常驻服务（自带 HTTP 服务）
  ├── SQLite 数据库（data/spark.db，WAL 模式）
  ├── 调度器线程（每 20 秒 tick，到点自动发消息）
  ├── 浏览器池（启动 Chromium，用 CDP 驱动抖音私信页）
  └── 文件存储（登录态 sessions/、执行截图 screenshots/）
```

Netlify 提供的是「静态文件 + 按需执行的函数」，没有常驻进程、没有持久磁盘、
更没有可以驱动抖音的浏览器。

---

## 2. 为什么不行（需求逐条对照）

| 本项目必须的能力 | Netlify / Vercel 给什么 | 结论 |
|---|---|---|
| **调度器常驻**（每 20s 检查到期任务） | 函数只有在被调用时才运行；定时函数最短粒度 1 分钟，且跑完就销毁 | ❌ 无法常驻 |
| **单次执行可能 30~120 秒**（启动浏览器 + 打开会话 + 输入 + 校验） | 同步函数免费计划约 10 秒硬超时，付费也远不够稳定跑完一次发送 | ❌ 超时被杀 |
| **持久化 SQLite**（账号、好友、任务、发送台账） | 函数文件系统临时且只读/随实例销毁；平台数据库是另一套东西，接不进来 | ❌ 数据丢失 |
| **Chromium 浏览器**（CDP 驱动抖音私信） | 不提供浏览器二进制，也不能 apt 安装 | ❌ 根本发不出去 |
| **扫码登录**（真实浏览器会话 + 网页里显示二维码） | 同上 | ❌ |
| **执行截图落盘**（data/screenshots） | 无常驻磁盘 | ❌ |
| **内存 ≥ 1 GB**（一个 Chromium 就要几百 MB） | 函数内存小而短命 | ❌ |

参考：[Netlify Functions 配置（超时/运行时限制）](https://docs.netlify.com/build/functions/configuration/?fn-language=js)、
[什么时候该从 Vercel/Netlify 换成真正的服务器](https://snapdeploy.dev/blog/vercel-netlify-to-container-when-you-need-a-server)。

> 顺带说一句：把「浏览器」放到你自己的服务器上、前端放 Netlify，是**可行**的
> （见第 3.3 节，后端已支持 `SK_CORS_ORIGINS`），但那台服务器才是关键，
> Netlify 只是帮你托管了几个静态文件，收益很小。

---

## 3. 三种可落地的方案

| 方案 | 能不能跑全功能 | 难度 | 参考成本 | 适合谁 |
|---|---|---|---|---|
| **A. 一台 VPS + Docker**（推荐） | ✅ 全部 | ★★ | ¥24~60/月（2 核 2G 轻量云） | 想稳定 7×24 跑定时任务 |
| **B. 容器型 PaaS**（Zeabur / Railway / Fly.io / Render） | ✅ 需「持久卷 + ≥1GB + 不休眠」 | ★ | $5~7/月起，免费额度基本不够 | 不想碰 Linux |
| **C. Netlify 前端 + 服务器后端** | ⚠️ 能用但更麻烦 | ★★★ | 仍要一台服务器 | 已经有服务器、只想要 Netlify 的域名/HTTPS |
| ❌ Netlify / Vercel 整站 | ❌ | — | — | 不适合本项目 |

---

### 3.1 方案 A：VPS + Docker（推荐，约 15 分钟）

**准备**：Ubuntu 22.04/24.04，2 核 2G 起（1G 内存建议 `SK_MAX_CONCURRENT_BROWSERS=1`），
安全组放行 80/443。

```bash
# 1) 服务器上装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2) 把交付目录传上去（在你自己的电脑上执行）
#    spark-keeper-lite 或 spark-keeper-pro 整个目录，注意 data/ 不用传
scp -r spark-keeper-lite root@<服务器IP>:/opt/

# 3) 配置环境变量
cd /opt/spark-keeper-lite
cp deploy/.env.example .env
vi .env
#    至少改这几项：
#      SK_SECRET=<32 位以上随机串>
#      SK_PUBLIC_URL=https://你的域名
#      TZ=Asia/Shanghai
#      SK_MAX_CONCURRENT_BROWSERS=1      # 2G 内存只能跑 1 个浏览器
#      SK_SCHEDULER_ENABLED=1            # 单容器模式：Web 进程内跑调度器

# 4) 构建并启动（首次会自动迁移、初始化管理员）
docker compose up -d --build
docker compose logs -f app          # 看到 "调度器已启动" 就对了

# 5) 首次管理员账号
cat data/INITIAL_ADMIN.txt          # 用户名 admin + 随机密码（登录后请改掉并删除该文件）
```

**域名 + HTTPS**（两种任选）：

```bash
# 方式一：Caddy（最省事，自动签证书）
apt install -y caddy
cat >/etc/caddy/Caddyfile <<'EOF'
spark.example.com {
    reverse_proxy 127.0.0.1:8000 {
        transport http {
            read_timeout 300s      # 扫码轮询/发送等待都比较久，别用默认短超时
        }
    }
}
EOF
systemctl reload caddy
```

```bash
# 方式二：nginx + certbot（仓库里已带 deploy/nginx.conf，可直接改用）
cp deploy/nginx.conf /etc/nginx/sites-available/spark-keeper
ln -s /etc/nginx/sites-available/spark-keeper /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d spark.example.com
```

> 反向代理超时必须放大（`proxy_read_timeout 300s`），否则「扫码等待」「发送校验」
> 会被 nginx 提前掐断，表现为前端一直转圈或 502。

**上线后**：浏览器打开 `https://你的域名` → 登录 → 「抖音账号」→「在窗口里登录」
（服务器上没有屏幕也能扫码：二维码会**渲染在网页里**，用手机抖音扫）→ 建任务 → 启用。

---

### 3.2 方案 B：容器型 PaaS（不想碰 Linux）

支持「用仓库里的 Dockerfile 部署 + 挂持久卷」的平台都可以：
**Zeabur、Railway、Fly.io、Render（付费盘）、Koyeb**。

```bash
# 以 Fly.io 为例
fly launch --dockerfile deploy/Dockerfile --name spark-keeper
fly volumes create spark_data --size 3          # 持久卷！不给卷数据一重启就没了
fly scale memory 1024                          # 至少 1GB，Chromium 很吃内存
# fly.toml 里挂载：[mounts] source="spark_data" destination="/app/data"
fly secrets set SK_ENV=prod SK_DATA_DIR=/app/data SK_SECRET=<随机串> TZ=Asia/Shanghai
fly deploy
```

**选平台时逐条确认**（不满足就别用）：

1. **持久卷**能挂到 `/app/data`（SQLite、登录态、截图都在这里）；
2. **内存可到 1~2 GB**，且**允许运行 Chromium**（有些平台限制沙箱/需 `--no-sandbox`，
   本项目已自动带该参数）；
3. **不会自动休眠/缩容到 0**（Render 免费实例会休眠，定时任务直接失效）；
4. **副本数固定为 1**（SQLite 单写，多副本会写坏）；
5. **没有单次请求/进程时长上限**（一次发送可能要 1~2 分钟）。

> 一句话：免费档基本都踩雷（休眠 / 无持久盘 / 无浏览器）。想省钱就选 2 核 2G 的
> 轻量云 VPS，比在 PaaS 上凑免费额度更省心。

---

### 3.3 方案 C：前端放 Netlify、后端放服务器（分体部署）

后端**已支持**：`SK_CORS_ORIGINS` 环境变量白名单（默认关闭）。
鉴权走 `Authorization` 头（JWT），不依赖 Cookie，所以跨域是干净的。

**第 1 步：后端照 3.1 部署**，并在 `.env` 里加前端来源：

```bash
SK_CORS_ORIGINS=https://spark.example.netlify.app
docker compose up -d          # 改完 env 需要重启容器
```

**第 2 步：把前端静态文件发到 Netlify**

- 发布目录：交付目录里的 `web/static`（里面是 `index.html` + `assets/`）
- 构建命令：留空（本项目前端**没有构建步骤**，Vue 是内置的全局构建版本）
- 前端是 hash 路由（`#/tasks`），不需要额外的 SPA 重写规则

**第 3 步：告诉前端后端在哪**（`index.html` 的 `<head>` 里加一段，放在加载 `assets/app.js` 之前）：

```html
<script>window.__SPARK__ = { apiBase: 'https://api.example.com/api/v1' };</script>
```

**第 4 步：验证跨域真的通了**

```bash
curl -i -H "Origin: https://spark.example.netlify.app" \
     https://api.example.com/api/v1/health | grep -i access-control
# 必须看到：Access-Control-Allow-Origin: https://spark.example.netlify.app
```

**代价（想清楚再选）**：两个域名要维护；二维码、截图、文件下载都变成跨域请求；
排查问题时多一层；而且要改 `index.html` 注入配置（不然前端还是打同源 `/api/v1`）。
**如果只是想要 HTTPS 和好看的域名，方案 A + Caddy/Cloudflare 更简单。**

---

### 3.4 免费方案（0 元跑 7×24）

先说不推荐的：**Render 免费实例 15 分钟无流量就休眠、免费档没有持久磁盘**
（[Render 免费档说明](https://render.com/docs/free)），任务到点根本没进程可跑；
**Koyeb 的免费 Starter 已对新用户关闭**；Railway/Fly.io 早已没有长期免费额度；
HuggingFace Spaces、GitHub Codespaces 的磁盘都是临时的（重启即丢库）。
Serverless 平台的免费额度对本项目**一律无效**——它要的是常驻进程 + 持久盘 + 浏览器。

真正能用的免费方案，按「省心程度」排序：

| 方案 | 免费程度 | 能跑满功能 | 稳定性 | 折腾度 |
|---|---|---|---|---|
| **① 自己电脑 / 旧笔记本 24/7** | 完全免费（只费电） | ✅ | ★★★ | ★ |
| **② Oracle Cloud 永久免费 ARM** | 永久免费（需信用卡验证） | ✅ | ★★ | ★★ |
| ③ Google Cloud e2-micro 永久免费 | 永久免费（需信用卡） | ⚠️ 1GB 内存要加 swap | ★★ | ★★★ |
| ④ 旧安卓手机 + Termux | 完全免费 | ⚠️ 要自己装 chromium | ★ | ★★★★ |

#### ① 自己的电脑 24/7（最推荐，0 元）

你已经在这台机器上跑通了，只需要解决「关机/休眠就停」：

```bat
rem 1) 禁止睡眠与休眠（插电时）
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15

rem 2) 开机自动启动（管理员 PowerShell，注册一个登录时任务）
schtasks /create /tn "SparkKeeper" /tr "\"C:\Users\huawei\Desktop\dy\spark-keeper-lite\start.bat\"" /sc onlogon /rl highest /f

rem 3) 关掉「快速启动」造成的时间漂移（可选，控制面板→电源选项）
```

注意三点：

- **Windows 自动更新会在凌晨重启**，重启后如果没登录，`onlogon` 任务不会跑。
  要么配置自动登录，要么把任务改成 `onstart`（需要服务化，比较复杂）。
- 笔记本合盖默认睡眠 → 电源选项里把「合上盖子」设为「不采取任何操作」。
- 定时任务的时间点别设在凌晨更新窗口（比如别用 03:00），用 10:00 或 14:00 更稳。

#### ② Oracle Cloud 永久免费（真正的免费云服务器）

Oracle 的 Always Free 现在给的是 **ARM Ampere A1 合计约 2 OCPU / 12GB**
（2025 年被悄悄砍过一半，见 [InfoQ 报道](https://www.infoq.cn/article/0YV6CYGw8sskGUbEDVIa)），
另加 2 台 AMD 1GB 微实例。跑一个 Chromium 完全够。

```bash
# 1) 注册 Oracle Cloud（要信用卡验证，免费额度不会被扣费），创建 Ubuntu 22.04 ARM 实例
#    2) SSH 上去装 Docker（同 3.1）
curl -fsSL https://get.docker.com | sh

# 3) 传交付目录、配 .env（同 3.1），ARM 上 Chromium 由 Debian 源提供，Dockerfile 直接可用
cd /opt/spark-keeper-lite && cp deploy/.env.example .env && vi .env
docker compose up -d --build
```

必须在 Oracle 控制台的**安全列表**里放行 80/443，否则外网访问不到；
免费实例长期空闲可能被回收，建议每周有一次访问（定时任务本身就等于持续活动）。

#### ③ Google Cloud e2-micro 永久免费（1GB，勉强）

`e2-micro` 在 `us-west1/us-central1/us-east1` 永久免费（30GB 标准盘）。内存只有 1GB，
Chromium 容易 OOM，务必加 2GB swap 并把并发降到 1：

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# .env：SK_MAX_CONCURRENT_BROWSERS=1
```

#### ④ 旧安卓手机 + Termux（极端省钱）

能跑，但坑多：Termux 里 `pkg install python chromium` 后把 `SK_CHROME_PATH` 指到
`$PREFIX/bin/chromium`；手机要常插电、关省电策略、给 Termux 加电池白名单。
仅适合动手能力强、不想花一分钱的情况。

> 免费方案共同的坑：CPU 是共享/限速的（浏览器启动慢 2~3 倍）、IP 可能被抖音风控
> （多个账号共用同一出口 IP 容易触发验证）、以及**免费实例被平台回收后数据全丢**。
> 所以无论选哪个，`data/` 一定要定期备份（`tools/backup.py`）。

---

### 3.5 宝塔面板（aaPanel）部署

宝塔本质是「Nginx + 面板 + 进程/计划任务管理」，所以两条路都能走：

| 路线 | 适合 | 复杂度 |
|---|---|---|
| **A. 宝塔 Docker**（推荐） | 想一条命令起全栈、升级简单 | ★★ |
| B. 宝塔本机 Python + systemd | 不想装 Docker、机器只有 2G 内存 | ★★★ |

**机器要求**：2 核 2G 起（宝塔面板自己就吃 300~500MB，2G 跑 Chromium 只能并发 1），
系统建议 Ubuntu 22.04 / 24.04。宝塔面板端口（8888）与网站端口（80/443）
都要在**云厂商安全组**里放行——很多人卡在「宝塔能开、网站打不开」，就是安全组没放行。

#### 路线 A：宝塔 Docker（推荐）

1. 宝塔左侧 **Docker** → 首次进入按提示安装（或在 SSH 里 `curl -fsSL https://get.docker.com | sh`）
2. **文件** → 进入 `/www/wwwroot/` → 上传交付包 zip → 解压
   - 上传的是 `spark-keeper-lite/`（或 `-pro/`）里的**内容**，不要上传 `data/`
   - 解压后形如：`/www/wwwroot/spark-keeper/{main.py,app/,web/,deploy/,docker-compose.yml}`
3. SSH 终端：

```bash
cd /www/wwwroot/spark-keeper
cp deploy/.env.example .env
vi .env
#   SK_SECRET=<32 位随机串>
#   SK_PUBLIC_URL=https://你的域名
#   TZ=Asia/Shanghai
#   SK_MAX_CONCURRENT_BROWSERS=1
#   SK_SCHEDULER_ENABLED=1
docker compose up -d --build
docker compose logs -f app        # 看到「调度器已启动」即可 Ctrl+C
cat data/INITIAL_ADMIN.txt        # 首次管理员密码（登录后改掉并删文件）
```

4. 宝塔 **网站** → 添加站点（域名，类型选纯静态即可，别选 Node 项目）→
   站点设置 → **反向代理** → 目标 `http://127.0.0.1:8000` → 保存
5. 站点设置 → **SSL** → Let's Encrypt 一键申请 → 打开「强制 HTTPS」

> Docker 路线下 `data/` 在命名卷 `spark-data` 里，文件管理器看不到；
> 备份用下面的「备份」小节命令。

#### 路线 B：宝塔本机 Python + systemd（不用 Docker）

**推荐用一键脚本**（仓库自带 `deploy/baota-install.sh`，幂等，可重复执行）：

```bash
cd /www/wwwroot/spark-keeper

bash deploy/baota-install.sh --check     # ① 只体检，不改任何东西
bash deploy/baota-install.sh --dry-run   # ② 看它准备做什么
bash deploy/baota-install.sh             # ③ 真正安装（root）

# 常用参数
bash deploy/baota-install.sh --user www --port 8100
bash deploy/baota-install.sh --skip-packages    # 已自己装好 chromium
bash deploy/baota-install.sh --uninstall        # 停服务 + 删 unit，data/ 保留

# 系统 Python 不是 3.9+（比如 CentOS 7 只有 3.6）时，先装一个 3.11 再指给脚本：
curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.11
bash deploy/baota-install.sh --python "$(uv python find 3.11)"
# 或用宝塔「Python 项目管理器」装的：
bash deploy/baota-install.sh --python /www/server/python_manager/versions/3.11*/bin/python3
```

脚本做的事：校验 Python/结构/端口 → 按发行版装 Chromium+中文字体 → 建 `data/` 并改属主为 `www`
→ 生成 `.env`（只补缺失项、自动随机 `SK_SECRET`、**把模板里的 Docker 路径 `/app/data` 纠正成本机目录**）
→ 渲染 systemd 单元并 `enable --now` → 轮询 `/api/v1/health` 确认存活 → 打印管理员账号与反代配置。

> 它**不能**替你做两件事：宝塔里加站点 + 反向代理（含 `proxy_read_timeout 300s`），以及安全组放行 80/443。

想手工一步步来，就按下面 1~6 做：

1. **软件商店** → 确认装了 **Nginx**
2. 装 Chromium 与中文字体（本项目**零第三方 Python 依赖**，不用 pip 装东西）：

```bash
# Ubuntu / Debian
apt update && apt install -y chromium fonts-noto-cjk tzdata
# CentOS / RHEL（需 EPEL）
yum install -y epel-release && yum install -y chromium google-noto-sans-cjk-fonts

which chromium chromium-browser      # 记下路径，Debian 系可能叫 chromium-browser
```

3. 上传代码到 `/www/wwwroot/spark-keeper`（同路线 A 第 2 步）
4. 建目录、给权限（宝塔站点用户是 `www`）：

```bash
cd /www/wwwroot/spark-keeper
mkdir -p data/sessions data/screenshots data/uploads
cp deploy/.env.example .env
chown -R www:www data && chmod -R u+rwX data
```

5. **systemd 守护**（仓库已带单元文件 `deploy/spark-keeper.service`）：

```bash
vi deploy/spark-keeper.service     # 核对 User=www、WorkingDirectory、SK_DATA_DIR
cp deploy/spark-keeper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now spark-keeper
systemctl status spark-keeper --no-pager -l
journalctl -u spark-keeper -f      # 看日志
```

> 不想用 systemd，也可用宝塔 **「进程守护管理器」（Supervisor 插件）**：
> 启动命令 `/usr/bin/python3 /www/wwwroot/spark-keeper/main.py serve --host 127.0.0.1 --port 8000`，
> 运行目录 `/www/wwwroot/spark-keeper`，用户 `www`，
> 环境变量加 `SK_ENV=prod`、`SK_DATA_DIR=/www/wwwroot/spark-keeper/data`、`TZ=Asia/Shanghai`。
> ⚠️ 别用宝塔「Python 项目管理器」：它会强制建虚拟环境并包一层启动脚本，
> 常见结果是「装了 chromium 却提示找不到」。本项目零依赖，systemd/Supervisor 更省事。

6. Nginx 反向代理（站点 → 设置 → 反向代理 → `http://127.0.0.1:8000`），
   再把下面几行加进站点**配置文件**（站点设置 → 配置文件）：

```nginx
# 浏览器自动化接口可能长时间无输出，默认 60s 会被 Nginx 掐断 → 502/504
proxy_read_timeout 300s;
proxy_send_timeout 300s;
proxy_buffering off;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

# 上传截图/头像，与 http_server.MAX_BODY 对齐
client_max_body_size 10m;
```

改完点「重载配置」（等价 `nginx -t && nginx -s reload`）。

#### 端口在哪配置（一张表）

默认监听 `127.0.0.1:8000`（只给 Nginx 反代用，不直接对外）。要改端口，**这四处必须一致**：

| 位置 | 怎么改 | 说明 |
|---|---|---|
| **① 安装脚本** | `bash deploy/baota-install.sh --port 8100` | 最省事：会重写 systemd 单元、`.env` 并重启服务 |
| **② systemd 单元** | `/etc/systemd/system/spark-keeper.service` 里 `ExecStart=... --port 8100`（和 `Environment=SK_PORT=8100`） | 手改后 `systemctl daemon-reload && systemctl restart spark-keeper` |
| **③ `.env`** | `SK_PORT=8100`、`SK_HOST=127.0.0.1` | ⚠️ 服务启动用的是 `main.py serve --port N` 命令行参数，**命令行优先于环境变量**，所以只改 `.env` 不生效 |
| **④ 宝塔反向代理** | 站点设置 → 反向代理 → 目标 URL 改成 `http://127.0.0.1:8100` | 不改会 502 |

**Windows 本机**同理：`start.bat --port 8100`（脚本里是把 `--port` 传给 `main.py serve`），或提前设环境变量 `SK_PORT=8100`。

> 顺带说明一个曾经的误报：`--check` 以前只要发现端口有人监听就报「端口已被占用」。
> 但**服务装好后端口本来就是自己占着的**（unit 是 active），重跑体检就会误报。
> 现在会先判断占用者是不是本项目自己的 systemd 服务：
>
> ```
> [✓] 端口 8000 正被本项目的 spark-keeper 服务使用（已在运行，属正常）
> ```
>
> 只有被**别的进程**（nginx、其它 Python 服务等）占着时才算冲突，并打印占用者的 PID 与命令行。

#### Docker 拉不动镜像 / 构建超时（国内网络必看）

典型报错：

```
[+] Building 30.1s (2/2) FINISHED
 => ERROR [app] resolve image config for docker-image://docker.io/docker/dockerfile:1
failed to do request: Head "https://registry-1.docker.io/v2/docker/dockerfile/manifests/1":
                        dial tcp 168.143.171.186:443: i/o timeout
```

意思是构建第一步要去 `docker.io` 拉一个「Dockerfile 前端镜像」，国内直连经常超时。
**本项目已经处理掉这一层**：`deploy/Dockerfile` 里**故意不写**
`# syntax=docker/dockerfile:1`（本文件没用到任何 BuildKit 专属语法），所以不会再拉那个镜像。

剩下要解决的是**拉基础镜像** `python:3.11-slim`。两种办法：

**① 配镜像加速器（推荐，一次配好长期有效）**

```bash
cd /www/wwwroot/dy/spark-keeper-lite

bash deploy/setup-docker-mirror.sh --check      # 先看现状（有没有配过）
bash deploy/setup-docker-mirror.sh              # 写入加速器并重启 docker，最后实测拉 hello-world
```

脚本做的事：备份 `/etc/docker/daemon.json` → **合并**写入 3~4 个国内加速器
（保留你原有的 `data-root` 等配置）→ `systemctl restart docker` → 实测 `docker pull hello-world`
→ 失败时给出下一步建议。回滚：`bash deploy/setup-docker-mirror.sh --restore`。

> 宝塔用户也可以走面板：**Docker → 设置 → 镜像加速**，填同样的地址。

**② 顺手换掉 Debian 源**（否则 `apt-get update` 一样会卡在 `deb.debian.org`）

```bash
SK_APT_MIRROR=mirrors.aliyun.com docker compose build
docker compose up -d
```

`SK_APT_MIRROR` 是 Dockerfile 的构建参数（`ARG APT_MIRROR`），会把 `deb.debian.org`
替换成你指定的镜像站（阿里云 / 清华 / 中科大等）。留空就用官方源。

**③ 实在连不上：离线导入镜像**

```bash
# 在能上网的电脑上（同架构 x86_64）
docker pull python:3.11-slim
docker save python:3.11-slim | gzip > python-3.11-slim.tgz

# 传到服务器后
gunzip -c python-3.11-slim.tgz | docker load
cd /www/wwwroot/dy/spark-keeper-lite && docker compose up -d --build
```

> 判断是否已经通了：`docker pull python:3.11-slim` 能完成就说明网络没问题。
> 提示：`docker compose build` 期间如果卡在 `apt-get update`，就是第 ② 步没做。

#### CentOS 7 / RHEL 7 特别说明（重要）

很多宝塔用户用的是 CentOS 7，这类机器**不能原生部署**，有两个死结：

| 死结 | 说明 |
|---|---|
| 系统 Python 只有 **3.6** | 本项目要求 3.9+（脚本会直接拒绝：`需要 Python 3.9 及以上（当前 3.6）`） |
| **glibc 2.17** | 新版 Chromium 从 114 起要求 glibc ≥ 2.28；EPEL7 那个老 chromium 已经跟不上抖音前端 |
| 官方 yum 源已下线 | CentOS 7 于 2024-06 EOL，`yum install` 基本全 404（脚本已不再尝试） |
| 系统 **SQLite 3.7.17** | 不支持部分索引（3.8+）与 UPSERT（3.24+）。即使用宝塔另装了 Python 3.11，只要它链的是系统 libsqlite3，建库仍会报 `near "WHERE": syntax error`（**已修**，见本节末尾） |
| **Python 缺 `_ssl`** | 宝塔编译的 Python 常没带 OpenSSL → `app.api.auth` / `admin` / `user` 整块注册失败，登录页只显示「接口不存在」。自查：`python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"`，报 `No module named '_ssl'` 就是这个问题 |

**所以 CentOS 7 上请直接走 Docker（路线 A）**，镜像里自带 Python 3.11 + Chromium：

```bash
curl -fsSL https://get.docker.com | sh        # 宝塔「Docker」菜单也能装
systemctl enable --now docker

cd /www/wwwroot/spark-keeper
cp deploy/.env.example .env && vi .env        # 改 SK_SECRET / SK_PUBLIC_URL / SK_MAX_CONCURRENT_BROWSERS
docker compose up -d --build                  # 不需要本机有 Python 3.9 或 chromium
docker compose logs -f app
```

> 如果 `get.docker.com` 装不上（老 yum 源问题），先把源换成 vault 再装：
> ```bash
> sed -i 's|^mirrorlist=|#mirrorlist=|g; s|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*.repo
> yum clean all && yum makecache
> ```
> **关于 SQLite 3.7.17（已修，v1.0.x 起）**：早期版本把「部分索引」写死在迁移文件里，
> 在 CentOS 7 上会 `OperationalError: near "WHERE": syntax error`，建库直接失败。
> 现在：迁移只用老版本也认的 SQL；部分唯一索引改由
> `app/db.py::ensure_daily_unique_index()` 按版本决定是否补建（老版本跳过、只记一条日志）；
> 调度器心跳改成 `db.upsert()`（老版本自动降级为 UPDATE→INSERT）。
> 因此 **CentOS 7 上原生部署也能建库启动**，只是少了「数据库层的当日唯一兜底」，
> 当日幂等仍由调度器代码保证。
>
> 装完可以用脚本体检确认：`bash deploy/baota-install.sh --check` 会打印
> `SQLite：3.7.17（偏老）——已自动降级…`。

> **长期建议**：CentOS 7 已停止维护，尽早换 Ubuntu 22.04 LTS 或 Rocky Linux 9。

脚本检测到 CentOS 7 时会明确提示这一点，并且跳过注定失败的 yum 安装。

#### 宝塔专属坑（按出现频率排序）

| 现象 | 原因 | 处理 |
|---|---|---|
| 站点 502 | 后端没起来 / 端口不对 | `systemctl status spark-keeper` 或 `docker compose ps`，确认监听 8000 |
| 扫码转圈后 504 | Nginx 默认 60s 超时 | 加 `proxy_read_timeout 300s` |
| 页面能开、接口 500 | `data/` 不属于 `www`，SQLite 写不进去 | `chown -R www:www data` |
| 浏览器启动失败 | 没装 chromium / 路径不对 | `which chromium` 后设 `SK_CHROME_PATH`；或改用路线 A（镜像自带） |
| 截图中文字全是方块 | 缺中文字体 | `apt install -y fonts-noto-cjk` |
| 建了站点却打不开 | 站点类型/首页不对 | 此类站点只用「反向代理」，不依赖根目录文件 |
| 计划任务不执行 | 服务没跑 / 任务停用 / 抖动未到 | 确认服务在跑、任务 `enabled=1`、等到「时间 + 抖动」 |
| 面板与站点抢端口 | 面板改过 80/443 | 面板设置里换成 8888/8889 之类 |

#### 备份（宝塔计划任务，每天一次）

宝塔 **计划任务** → 「Shell 脚本」→ 每天 04:00：

```bash
# 路线 B（本机目录）
cd /www/wwwroot/spark-keeper && /usr/bin/python3 tools/backup.py --out /www/backup/spark --keep 14

# 路线 A（Docker 命名卷）
docker run --rm -v spark-keeper-lite_spark-data:/d -v /www/backup/spark:/b \
  alpine sh -c 'tar czf /b/data-$(date +%F).tgz -C /d .'
```

最关键的是 `data/spark.db`、`data/sessions/`（登录态）、`data/screenshots/`。

---

## 4. 上线检查清单

- [ ] `TZ=Asia/Shanghai`（任务时间按这个时区算）
- [ ] `data/` 在**持久卷**上，并且有备份（`data/spark.db` + `data/sessions/` 最重要）
- [ ] `SK_MAX_CONCURRENT_BROWSERS=1`（2G 内存）或 `2`（4G 内存）
- [ ] 反向代理超时 ≥ 300s（扫码/发送都很慢）
- [ ] 只跑 **1 个副本**（SQLite 单写）
- [ ] HTTPS 已开（登录 Cookie 不能明文过网）
- [ ] 任务列表里任务是**启用**状态、时间对照时区没错
- [ ] 首次登录后改掉 `INITIAL_ADMIN.txt` 里的随机密码，并删掉该文件

## 5. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 容器起不来 / 一直重启 | 内存不足被 OOM（Chromium 吃内存） | 加内存，或降到 1 个并发浏览器 |
| 扫码后没反应、发送超时 | 反向代理超时太短 | nginx `proxy_read_timeout 300s` |
| 定时任务不执行 | 平台把实例休眠了 / 任务被停用 / 抖动窗口没到 | 关掉休眠、确认任务启用、等到「时间 + 抖动」 |
| 重启后数据全没了 | `data/` 没挂持久卷 | 挂卷，别用容器内临时目录 |
| 浏览器启动失败 | 容器内没权限起 Chromium | 本项目已带 `--no-sandbox`；确认镜像用 `deploy/Dockerfile` 构建 |
| 多人同时用导致锁库 | 换成多副本了 | 固定 1 副本；需要扩容请先换 PostgreSQL（当前版本不含） |

## 6. 相关文档

- `deploy/DEPLOY.md` —— Docker / 环境变量 / 备份恢复的细节
- `docs/DEPLOY_GUIDE.md` —— 从零到上线的长版指南
- `docs/RUNBOOK.md` —— 线上运维手册（排障步骤、登录三种方式、配额说明）
- `deploy/nginx.conf`、`deploy/docker-compose.lite.yml`、`docker-compose.yml` —— 可直接用的配置
