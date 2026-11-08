# 运维手册 · spark-keeper RUNBOOK

> 面向**已经在跑这套系统的人**。部署从零开始看 `deploy/DEPLOY.md`。
> 本文所有命令都在项目根目录执行；Docker 部署用 `docker compose exec app <命令>` 形式给出，
> 裸机部署把前缀去掉直接跑。
>
> 约定：`<命令>` 里的 `python` 在容器内就是 `python`；宿主机裸机可能是 `python3`。

---

## 1. 命令速查

| 目的 | 命令 |
| --- | --- |
| 看服务状态 | `docker compose ps` |
| 看日志（跟随） | `docker compose logs -f --tail 200 app` |
| 重启 | `docker compose restart app` |
| 进容器 | `docker compose exec app sh` |
| 环境体检 | `docker compose exec app python main.py check` |
| 数据库迁移 | `docker compose exec app python main.py migrate` |
| 健康检查 | `curl -s http://127.0.0.1:8000/api/v1/health` |
| 热备份 | `docker compose exec app python tools/backup.py --keep 14` |
| 列出备份 | `docker compose exec app python tools/backup.py --list` |
| 改管理员密码 | `docker compose exec app python main.py admin --password '新密码'` |
| 命令行发卡 | `docker compose exec app python main.py card --package monthly --days 30 --count 10` |
| 手动跑任务 | `docker compose exec app python main.py task --account-id 3` |
| 只探测不发送 | `docker compose exec app python main.py task --account-id 3 --dry-run` |
| 抖音页面结构诊断 | `docker compose exec app python main.py diagnose --account-id 3 --out /app/data/diagnostics` |
| 自动获取抖音 Cookie（需图形界面，宿主机跑） | `python tools/get_cookie.py --account 3` |
| 一键自检 | `docker compose exec app python tools/selftest.py` |
| 契约自检 | `docker compose exec app python tools/check_contract.py` |

---

## 2. 启动与停止

### Docker

```bash
docker compose up -d --build          # 启动/更新
docker compose stop                   # 停止（保留容器与数据）
docker compose down                   # 删除容器（数据卷保留）
docker compose down -v                # ⚠️ 连数据卷一起删，等于清库
docker compose --profile worker up -d # 多容器：Web + 独立调度器
```

### 裸机（systemd）

```bash
systemctl start|stop|restart|status spark
journalctl -u spark -f --since "10 min ago"
```

### 启动是否正常，看三件事

1. `docker compose ps` 的 STATUS 是 `healthy`（不是 `starting`）。
2. `curl -s http://127.0.0.1:8000/api/v1/health` 返回 `code:0`，且 `db_ok`/`database` 为真。
3. 后台「系统环境」页的 worker 心跳在 3 分钟内（超过说明调度器没在跑）。

---

## 3. 日常巡检

**每天**（2 分钟）

- [ ] `docker compose ps` 有没有反复重启
- [ ] 磁盘余量：`df -h`（截图会涨；低于 15% 要清理）
- [ ] 后台「运行日志」筛 `error`，看有没有连续失败
- [ ] 昨晚的定时任务是否成功（执行记录页按日期筛）

**每周**（10 分钟）

- [ ] 备份文件存在且能恢复（至少每季度真恢复一次到测试目录）
- [ ] 检查「抖音账号」有没有 `expired`（登录态掉了要重新扫码）
- [ ] `docker compose logs --since 7d app | grep -i '错误\|error' | tail -50`
- [ ] 审计日志里有没有非本人操作

**每月**

- [ ] 升级到最新交付版本（先备份）
- [ ] 清理旧日志（后台「日志清理」或 `logging.prune(30)`）
- [ ] 复核套餐/卡密/订单对账

---

## 4. 备份与恢复

### 4.1 为什么必须用 `tools/backup.py` 而不是 `cp`

库开着 WAL，直接拷 `spark.db` 可能拿到「主库有新数据、WAL 里没有」的半截状态。
`tools/backup.py` 用 `VACUUM INTO` 在读事务里导出一致性快照，并自动：
校验 `integrity_check`、检查关键表存在、只保留最近 N 份、打印 schema 版本。

```bash
# 备份到 data/backups/spark-YYYYmmdd-HHMMSS-ffffff.db（保留最近 14 份）
docker compose exec app python tools/backup.py --keep 14

# 单文件输出（脚本里方便解析）
docker compose exec app python tools/backup.py --json
```

### 4.2 备份策略（建议）

| 层级 | 频率 | 保留 | 位置 |
| --- | --- | --- | --- |
| 本地快照 | 每天 03:30 | 14 份 | `data/backups/`（数据卷内） |
| 异地副本 | 每天 04:00 | 30 份 | 对象存储/另一台机器 |
| 恢复演练 | 每季度 | — | 临时目录 |

宿主机 crontab：

```cron
30 3 * * * cd /opt/spark-keeper-pro && docker compose exec -T app python tools/backup.py --keep 14 >> /var/log/spark-backup.log 2>&1
0  4 * * * cd /opt/spark-keeper-pro && docker compose cp app:/app/data/backups ./offsite/ && find ./offsite -name '*.db' -mtime +30 -delete
```

### 4.3 恢复

```bash
cd /opt/spark-keeper-pro
docker compose stop app worker 2>/dev/null || docker compose stop app

# 方式 A：从数据卷里的备份恢复
docker compose cp app:/app/data/backups/spark-20260816-033000-123456.db /tmp/restore.db   # 先取出来（容器已停则用 docker cp）
docker run --rm -v spark-keeper-pro_spark-data:/data -v /tmp:/src alpine \
    sh -c "cp /src/restore.db /data/spark.db && rm -f /data/spark.db-wal /data/spark.db-shm"

# 方式 B：宿主机直接改数据卷目录（推荐，路径用 docker volume inspect 查）
docker volume inspect spark-keeper-pro_spark-data --format '{{ .Mountpoint }}'

docker compose up -d
curl -s http://127.0.0.1:8000/api/v1/health
```

**恢复后必须做**：登录一次、看用户/订单数量对不对、跑一次 `/api/v1/install/check`。
恢复的是**旧库**：恢复前请把当前库也备份一份（`--prefix spark-before-restore`），否则改错了没退路。

> 恢复时一定要删掉旧的 `-wal` / `-shm` 文件，否则 SQLite 会拿旧 WAL 覆盖你刚放进去的主库。

---

## 5. 迁移到新机器

1. **旧机**：停服务 → 备份 → 打包数据目录

   ```bash
   docker compose stop app worker 2>/dev/null || docker compose stop app
   docker compose exec app python tools/backup.py --keep 3   # 容器停了就用下面的 tar 方式
   docker compose run --rm --entrypoint sh app -c "tar czf /app/data/migrate.tgz -C /app/data ."
   docker compose cp app:/app/data/migrate.tgz ./migrate.tgz
   ```

   裸机更简单：`tar czf migrate.tgz -C /opt/spark-keeper-pro data`

2. **新机**：装 Docker → 上传交付目录与 `migrate.tgz` → 解包到数据卷 → 起服务

   ```bash
   cd /opt/spark-keeper-pro && cp .env.example .env   # 保持 SK_SECRET 与旧机一致！
   docker compose up -d --build                        # 先让卷被创建
   docker compose stop app
   docker compose cp ./migrate.tgz app:/app/data/migrate.tgz
   docker compose run --rm --entrypoint sh app -c "tar xzf /app/data/migrate.tgz -C /app && rm /app/data/migrate.tgz"
   docker compose up -d
   ```

3. **注意**：
   * `SK_SECRET` 必须与旧机相同，否则所有登录态失效（用户要重新登录）。
   * `data/sessions/*.json` 一起迁过去，抖音登录态才不会掉；换 IP 或换地区仍可能要求重新扫码。
   * 迁移后改 DNS，确认 HTTPS 证书已在新机签好。

---

## 6. 数据库 schema 升级

```bash
docker compose exec app python tools/backup.py        # 先备份
docker compose exec app python main.py migrate        # 幂等：只跑没执行过的迁移
docker compose exec app python -c "import sys;sys.path.insert(0,'.');from app import db;print(db.health())"
```

* 迁移文件在 `db/migrations/*.sql`，按文件名版本号顺序执行，记录在 `schema_migrations` 表。
* 容器启动时 `entrypoint.sh` 已经自动跑过一次，手动跑一般只用于排障。
* **不要**手工改 `schema_migrations`；**不要**改已经发布过的迁移文件（校验和有记录）。

---

## 7. 卡密与对账

```bash
# 命令行发一批月卡（10 张，30 天）
docker compose exec app python main.py card --package monthly --type time --days 30 --count 10 --note "运营批次"

# 余额卡：每张充值 19.00 元（1900 分）
docker compose exec app python main.py card --package monthly --type balance --amount 1900 --count 5

# 次数卡：每张可用 5 次
docker compose exec app python main.py card --package monthly --type count --use-limit 5 --count 5
```

对账口径（都在后台页面可见，也可直接查表）：

| 问题 | 看哪里 |
| --- | --- |
| 这批卡发出多少、兑了多少 | 后台「卡密管理」按批次筛；表 `card_batches` / `card_keys` |
| 谁在什么时候兑的 | 表 `card_redemptions`（含 IP、兑换前后到期时间） |
| 钱有没有到 | 表 `orders`（`status=paid`）+ `payment_notifications`（回调原文） |
| 余额怎么变的 | 表 `balance_logs`（正数入账、负数出账，带 `balance_after`） |

> 卡密重复兑换会返回 409 `card_used`，这是**预期行为**（原子扣减），不是 bug。

---

## 8. 定时任务排障

按「现象 → 命令」的顺序查：

```bash
# 1) 任务是否到点、next_run_at 对不对
docker compose exec app python -c "import sys;sys.path.insert(0,'.');from app import db;[print(r) for r in db.query_all('SELECT id,name,enabled,time_of_day,repeat_mode,last_run_at,last_status,next_run_at FROM scheduled_tasks ORDER BY id')]"

# 2) 立刻手动跑一次（会真的发消息，注意！）
docker compose exec app python main.py task --account-id 3

# 3) 只探测好友、不发送（dry-run）
docker compose exec app python main.py task --account-id 3 --dry-run

# 4) 跑一次但发现选择器不对（抖音改版了）→ 导出页面结构
docker compose exec app python main.py diagnose --account-id 3 --out /app/data/diagnostics
# 产出 JSON + HTML + 截图，HTML 能直接看哪一步没命中

# 5) 看这次执行明细
docker compose exec app python -c "import sys;sys.path.insert(0,'.');from app import db;[print(r) for r in db.query_all('SELECT id,status,started_at,finished_at,total_count,success_count,fail_count,message FROM task_runs ORDER BY id DESC LIMIT 5')]"
```

常见原因：

| 现象 | 原因 |
| --- | --- |
| 任务没触发 | 账号 `status != online`；`enabled=0`；`next_run_at` 是过去时间但调度器没跑（看 worker 心跳） |
| 日志 `task_running` | 同一账号已有执行中的任务（并发保护），等它结束 |
| 全部失败且提示找不到元素 | 抖音页面改版 → 跑 `diagnose`，把结果反馈给维护者更新 `selectors.py` |
| 只成功一部分 | 好友昵称对不上（重名/改名）→ 用 `sec_uid` 精确匹配；或该好友当天已发过（幂等跳过） |
| 同一天重复执行了两次 | 检查是不是同时开了 app 内置调度器和独立 worker（`SK_SCHEDULER_ENABLED` 必须只在一处为 1） |
| 提示额度不足 `task_daily_limit` | 套餐的每日次数用完了，升级套餐或发卡 |

---

## 9. 登录态失效

现象：账号状态变 `expired`，任务失败提示未登录。

```bash
# 看登录态文件是否还在、更新时间
docker compose exec app ls -l /app/data/sessions/
```

处理：后台「抖音账号」→ 对应账号 → 重新扫码登录。**不要**手动编辑 session JSON 文件
（格式由 `session_store` 决定，改坏了账号会直接不可用）。

### 扫码一直不出二维码怎么办

现象：点了「扫码登录」，弹窗一直停在「正在生成二维码…」，最后报「没有在页面上找到二维码」。
这是抖音网页端的登录弹层/二维码渲染问题（风控、网络、版本差异），不是本系统的 bug。

**方式一：网页里点「在窗口里登录」（推荐，无需命令行）**

后台「抖音账号」→ 点「扫码登录」→ 弹窗底部点 **「在窗口里登录」**。
后端会在**运行本服务的那台电脑**上弹出一个真实浏览器窗口（Windows 上**优先用 Edge**，
其次 Chrome/Chromium；可用 `SK_CHROME_PATH` 指定）：

1. 窗口会直接打开抖音首页并置到前台；
2. 在里面用抖音 App 扫码，或改用手机号/密码登录；
3. 登录成功后网页会自动继续（检测到 sessionid 就保存），无需其他操作。

> 适用于「服务跑在本机」的场景。如果服务在远端服务器或 Docker 容器里，
> 窗口会出现在服务器那边（通常看不到），请改用下面的方式二/三。

**方式二：用自带的一键取 Cookie 工具**

它会打开一个真实浏览器窗口：你自己登录，工具自动抓 Cookie 并写库：

```bash
# 直接写进本机账号（最省事，写完后台立刻变 online）
python tools/get_cookie.py --account 1

# 只打印，粘到网页「导入 Cookie」里
python tools/get_cookie.py

# 写到远端实例
python tools/get_cookie.py --api-url http://127.0.0.1:8000 --token <令牌> --account 1
```

Windows 上也可以直接双击项目根目录的 **`get-cookie.bat`**（等价于 `python tools/get_cookie.py`）。

工具行为：启动一个**可见的** Chrome 窗口 → 你用抖音 App 扫码（或手机号+密码）登录 →
工具检测到 `sessionid` 后自动抓取整份 Cookie 并落库 → 关闭窗口。
浏览器配置目录默认 `data/profiles/cookie_helper`，**第二次运行往往几秒就完成**
（上次还登录着）。常用参数：

| 参数 | 作用 |
| --- | --- |
| `--account N` | 直接写入本机账号 N（推荐） |
| `--api-url` + `--token` | 写入远端实例的账号 |
| `--out cookie.txt` | 存文件（`.json` 存完整数组，其它后缀存头格式） |
| `--timeout 600` | 等你登录的最长秒数（默认 300） |
| `--profile DIR` | 复用/指定浏览器配置目录 |
| `--clean-profile` | 抓完就删掉该配置目录 |

**方式三：手动导入浏览器 Cookie**

1. 用你自己的 Chrome / Edge 打开 <https://www.douyin.com> 并确认已登录；
2. `F12` → **Application（应用）** → **Cookies** → `https://www.douyin.com`；
3. 找到 `sessionid` 这一行，复制它的 Value（`sid_guard`、`ttwid` 一起复制更稳）；
4. 后台「抖音账号」→ 该账号 → **导入 Cookie**，粘贴成
   `sessionid=xxxxx; sid_guard=yyyyy; ttwid=zzzzz`（也支持扩展导出的 JSON、
   Netscape `cookies.txt`）；
5. 勾选「导入后立即校验」→ 系统会开一次无头浏览器，让抖音确认这个登录态真的有效。

接口层等价命令（`api` 前缀见契约 §0）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/douyin/accounts/3/cookies \
  -H "Authorization: Bearer <你的令牌>" -H 'Content-Type: application/json' \
  -d '{"cookies": "sessionid=xxxxx; ttwid=yyyy", "verify": true}'
```

响应 `verified`：`true` = 抖音接受了该登录态；`false` = 已保存但抖音不认
（多半已过期或复制不全，账号会被标回 `expired`）；`null` = 已保存但本机浏览器
起不来、没能校验。

> ⚠️ Cookie 等价于账号密码。只粘贴到你自己部署的这台实例，不要发到群里或截图外传。

---

## 9.1 执行次数（每日上限）

自托管单租户默认**不限次**：仪表盘显示「今日 N/∞ 次」，ensure_can_run_today()
直接放行，套餐里的 daily_task_limit 不再拦截任务。

想恢复「按套餐计次」（例如对外售卖时），在 .env 里设：

`ash
SK_UNLIMITED_TASK_RUNS=0
`

重启后生效，界面会回到 N/套餐次数，超额返回 429 	ask_daily_limit。
（账号数上限仍由套餐的 account_limit 控制，不受这个开关影响。）

---

## 10. 应急处理

| 场景 | 处置 |
| --- | --- |
| 被刷接口/CC 攻击 | 临时在 `nginx.conf` 把 `limit_req` 收紧并 `reload`；必要时只放行自己的 IP |
| 怀疑账号被盗 | 立即改管理员密码（`main.py admin --password`），检查 `audit_logs`，全站改 `SK_SECRET` 使所有令牌失效 |
| 密钥泄露（`SK_SECRET`） | 换新密钥 → 重启 → 所有用户需重新登录；支付密钥同时换 |
| 数据库损坏 | 停服务 → `python -c "..."` 跑 `PRAGMA integrity_check` → 从最近备份恢复 |
| 磁盘写满 | 清理 `data/screenshots/`（按天删）与旧备份；`docker system prune -f` 清构建缓存 |
| 服务被滥用（给别人大量发消息） | 停调度器：`.env` 里 `SK_SCHEDULER_ENABLED=0` → 重启 app；再查 `audit_logs` |

---

## 11. 常用 SQL（只读排障）

```bash
docker compose exec app python -c "
import sys; sys.path.insert(0,'.')
from app import db
sql = '''SELECT status, COUNT(*) n FROM orders GROUP BY status'''
for row in db.query_all(sql): print(row)
"
```

| 目的 | SQL |
| --- | --- |
| 今日执行统计 | `SELECT status, COUNT(*) FROM task_runs WHERE run_date=date('now') GROUP BY status` |
| 7 天成功率 | `SELECT run_date, SUM(success_count), SUM(fail_count) FROM task_runs WHERE run_date >= date('now','-7 day') GROUP BY run_date` |
| 未支付订单 | `SELECT order_no, amount_cents, created_at FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 20` |
| 回调异常 | `SELECT id, channel, order_no, sign_ok, processed, substr(result,1,60) FROM payment_notifications WHERE sign_ok=0 OR processed=0 ORDER BY id DESC LIMIT 20` |
| 慢/失败接口 | `SELECT created_at, source, substr(message,1,80) FROM run_logs WHERE level='error' ORDER BY id DESC LIMIT 20` |
| 卡密状态分布 | `SELECT status, COUNT(*) FROM card_keys GROUP BY status` |

---

## 12. 未验证项（诚实清单）

以下内容**本机开发环境无法验证**，请在部署机上确认后补记：

1. `docker build` 与 `docker compose up`：开发机没有 Docker，镜像未实际构建过。
2. 容器内 Chromium 启动与扫码登录：本机沙箱禁止启动浏览器，自动化流程只做了
   单元测试与 `SK_DEMO_MODE=1` 的假实现验证。
3. `nginx.conf` 未做 `nginx -t` 语法校验（开发机没有 nginx）。
4. 真实支付渠道（易支付）的下单与回调：只验证了签名/验签逻辑与「回调必入库」，
   未连真实网关。
5. 邮件/短信真实投递：未配置 SMTP/短信通道，验证的是「未配置时不阻塞主流程」。

发现与上面不符的现象，先看 `deploy/DEPLOY.md` 第 12 节故障排查，再跑 `python tools/selftest.py` 收集证据。
