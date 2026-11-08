# 接口契约 v1（冻结版）

> **这份文档是前后端并行开发的唯一依据。** 任何一方要改接口，先改这里，
> 再改代码。运行时以 `GET /api/v1/_routes` 输出的实际路由表为准，
> `tools/check_contract.py` 会自动比对两者并在不一致时报错。

## 0. 全局约定

> ### ⚠️ 路径前缀规则（最容易踩的坑，先看这条）
> **本文档所有路径都是「相对路径」，调用时必须拼上前缀 `/api/v1`。**
> 例如契约里写 `/auth/user/login`，实际请求的是 **`POST /api/v1/auth/user/login`**。
>
> * 前端：`CFG.apiBase`（默认 `/api/v1`）已经在 `request()` 里统一拼接，
>   业务代码只写相对路径，**不要**再手写 `/api/v1`。
> * 后端：`@route("POST", "/auth/user/login")` 即可，`app/router.py` 会自动补全前缀；
>   写成带前缀的完整路径也一样（不会重复拼接）。
> * 运行时以 **`GET /api/v1/_routes`** 返回的完整路径为准；`tools/check_contract.py`
>   会做双向比对，任何一边漏了都会被检查出来。

> ### ⚠️ 数路由要用哪个数（口径统一，避免各说各话）
> `GET /api/v1/_routes` 的响应里：
> * `count` = **method+path 组合数**（同一路径的 GET 与 POST 各算一条）
> * 唯一 **URL 路径数** = `len({r.path for r in routes})`，比 `count` 小
>   （当前：109 条路径 / 136 个 method+path 组合）
>
> 讨论「有多少个接口」时请说明口径；`check_contract.py` 用的是「路径 + 方法」比对，
> 所以它报的数字接近 `count`。

| 项目 | 约定 |
| --- | --- |
| 前缀 | 所有接口以 `/api/v1` 开头（Web 页面与静态资源不走这个前缀） |
| 成功响应 | `{"code": 0, "message": "ok", "data": {...}}`，HTTP 200 |
| 失败响应 | `{"code": "unauthorized", "message": "请先登录"}` + 对应的 HTTP 状态码 |
| 分页响应 | `data = {"items": [...], "total": 0, "page": 1, "page_size": 20, "pages": 0}` |
| 鉴权 | `Authorization: Bearer <access_token>`（也接受 `X-Token`） |
| 时间 | 一律返回 UTC 字符串 `YYYY-MM-DD HH:MM:SS`；字段名以 `_at` 结尾 |
| 金额 | 一律用「分」的整数，字段名以 `_cents` 结尾 |
| 方法 | 查询用 `GET`，创建用 `POST`，整体更新用 `PUT`，局部更新用 `PATCH`，删除用 `DELETE` |
| **PUT vs PATCH 语义** | **`PUT` = 整字段覆盖**（未传的字段回落默认值，客户端必须提交完整对象）；**`PATCH` = 只改传了的字段**。当前只有 `PATCH /douyin/accounts/<id>` 用局部语义，其余更新接口一律 `PUT` 整覆盖。前端编辑表单必须按此提交。 |
| 参数位置 | 请求体 JSON；`page`/`page_size` 等查询参数走 query string |
| 布尔 | 请求可传 `true/false` 或 `1/0`；响应统一返回 `true/false` |

**错误码（`code` 字段）白名单**

```
bad_request  unauthorized  payment_required  forbidden  not_found
method_not_allowed  conflict  payload_too_large  too_many_requests  server_error
no_active_package  account_limit  task_daily_limit  card_invalid  card_used
task_running  need_verify  login_failed  locked  pay_disabled  pay_channel_error
```

**状态码的选择（前端按 `code` 分支，不要只看 HTTP 状态）**

| 场景 | HTTP | `code` | 前端应做什么 |
| --- | --- | --- | --- |
| 请求过频（限流中间件） | 429 | `too_many_requests` | 稍后重试，可读 `Retry-After` |
| 登录失败锁定 | 429 | `locked` | 提示等待 |
| **今日执行次数用完 / 套餐不支持执行** | 429 | `task_daily_limit` | **引导升级套餐**（不是"稍后重试"） |
| 套餐未激活或已过期 | 402 | `no_active_package` | 引导兑换卡密或购买套餐 |
| 账号位已满 | 402 | `account_limit` | 引导升级套餐 |
| 余额不足（用余额下单） | 402 | `payment_required` | 引导充值 |

> 为什么「今日次数用完」用 429：它在语义上确实是「本周期内请求过多，明天再试」，
> 且 `Retry-After` 可以带上到明天 0 点的秒数。**前端必须用 `code === 'task_daily_limit'`
> 区分它与限流的 `too_many_requests`**，两者的用户引导完全不同（升级套餐 vs 稍后重试）。

**分页/筛选通用 query**

```
page=1&page_size=20&keyword=&status=&start_date=&end_date=&sort=-created_at
```

**角色**：`user`（普通用户）/ `agent`（有下级）/ `admin`（管理端）。
`auth` 列含义：`public` 免登录 · `user` 需登录 · `admin` 需管理员。

---

## 1. 安装与健康检查

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/api/v1/health` | public | 存活探针，返回版本/运行时长/请求数 |
| GET | `/api/v1/install/check` | public | 安装体检：环境、数据库、管理员是否存在、是否可进生产 |
| POST | `/api/v1/install/execute` | public | 一键初始化（仅当尚无管理员时允许调用） |
| GET | `/api/v1/install/latest-migration` | public | 已应用/待应用的迁移列表 |
| POST | `/api/v1/install/test-db` | public | 测试数据库可写（建临时表再删除） |
| POST | `/api/v1/install/test-smtp` | admin | 发送测试邮件 |

`GET /api/v1/install/check` → `data`：

```json
{
  "installed": true, "admin_username": "admin", "user_count": 1,
  "schema_version": "0001",
  "database": {"ok": true, "path": "data/spark.db", "tables": 40, "size_bytes": 1024, "journal_mode": "wal"},
  "environment": {"python_version": "3.11.0", "python_ok": true, "platform": "Linux 5.15",
                  "disk_free_gb": 12.3, "browser_path": "/usr/bin/chromium", "browser_found": true,
                  "tzdata_ok": true, "demo_mode": false, "timezone": "Asia/Shanghai"},
  "site": {"name": "抖音续火花系统", "allow_register": true},
  "version": "1.0.0", "ready_for_production": true
}
```

---

## 2. 认证 `/auth`

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/auth/public-configs` | public | 站点公开配置（站名、logo、备案、是否开放注册、是否启用支付、支付渠道） |
| POST | `/auth/send-verify-code` | public | 发送邮箱/短信验证码 |
| POST | `/auth/verify-code` | public | 校验验证码（注册/重置前预校验） |
| POST | `/auth/user/register` | public | 注册 |
| POST | `/auth/user/login` | public | 登录 |
| POST | `/auth/admin/login` | public | 管理端登录（非管理员一律拒绝） |
| POST | `/auth/refresh` | public | 用 refresh_token 换新 token（旧 refresh 立即作废） |
| POST | `/auth/logout` | user | 退出（吊销当前会话） |
| POST | `/auth/forgot-password` | public | 发送重置验证码，返回 `reset_token` |
| POST | `/auth/reset-password` | public | 用 `reset_token` 重置密码 |

**登录**

```
POST /auth/user/login
{"identifier": "admin", "password": "xxx", "remember": true}
→ 200 {"code":0,"data":{"token":"...","access_token":"...","refresh_token":"...",
        "expires_in":7200,"refresh_expires_in":2592000,"token_type":"Bearer","user":{...}}}
→ 401 {"code":"unauthorized","message":"账号或密码错误"}
→ 429 {"code":"locked","message":"失败次数过多，请稍后再试（约 300 秒后重试）"}
```

> `remember=true` 只延长 **refresh_token** 到 30 天（`REMEMBER_REFRESH_TTL`），
> access_token 仍保持短有效期（默认 2 小时）。`refresh_expires_in` 是给前端决定
> 「存 localStorage 还是 sessionStorage」用的。

`user` 对象（登录/`/user/info` 共用）：

```json
{
  "id": 1, "username": "admin", "nickname": "管理员", "email": "", "phone": "",
  "role": "admin", "status": "active", "avatar": "", "balance_cents": 0,
  "package_id": 2, "package_expire_at": "2026-09-16 00:00:00",
  "created_at": "2026-08-16 00:00:00", "last_login_at": "2026-08-16 12:00:00",
  "quota": {"active": true, "package_code": "monthly", "package_name": "月卡",
            "expire_at": "...", "expire_text": "还剩 30 天",
            "account_limit": 2, "account_used": 1,
            "daily_task_limit": 5, "daily_task_used": 0,
            "bonus_task_count": 2, "bonus_expire_at": "2026-10-16 00:00:00",
            "role": "admin"}
}
```

> **配额语义（重要，前端要按这个显示）**
> * `daily_task_limit` = **套餐每日次数 + 次数卡加成**（`bonus_task_count`）。
>   两者相加才是用户今天能跑的次数上限，前端提示文案用这个值。
> * `active` = 套餐未过期 **或** 次数卡加成仍在有效期内。所以「没套餐但有次数卡」
>   的用户也能用，不要只看 `package_expire_at` 判断。
> * `bonus_task_count` 由「次数卡」累加而来，过期自动作废；`account_limit`
>   不受次数卡影响（次数卡只加执行次数，不加账号位）。

**注册**

```
POST /auth/user/register
{"username":"tom","password":"abc12345","confirm_password":"abc12345",
 "email":"","phone":"","verify_code":"","nickname":"","invite_code":""}
→ 200 data 同登录（注册即登录）
```

**验证码**

```
POST /auth/send-verify-code
{"scene":"register","channel":"email","target":"a@b.com","captcha":"<可选图形验证码>"}
→ 200 {"code":0,"data":{"sent":true,"expire_in":600,"dev_code":"123456"}}
```
> 未配置 SMTP/短信时 `sent=false` 且返回 `dev_code`，方便本地跑通流程；
> 生产环境请配置 SMTP 与短信通道（见 `.env.example`）。

---

## 3. 用户中心 `/user`

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/user/info` | user | 当前用户信息（含配额快照） |
| GET | `/user/dashboard` | user | 首页汇总卡片（账号数/任务状态/近 7 天执行统计/最近记录） |
| POST | `/user/change-password` | user | 改密（改后所有会话失效，需重新登录） |
| PUT | `/user/profile` | user | 修改昵称/邮箱/手机号/头像 |
| GET | `/user/logs` | user | 我的运行日志（分页，支持 level/source/keyword） |
| GET | `/user/notifications` | user | 站内通知（分页） |
| POST | `/user/notifications/read` | user | 标记已读（`ids:[]` 或 `all:true`） |
| GET | `/user/announcements` | user | 公告列表 |

**仪表盘**

```json
{
  "accounts": {"total": 2, "online": 1, "expired": 1},
  "tasks": {"total": 2, "enabled": 2, "next_run_at": "2026-08-17 00:30:00"},
  "today": {"runs": 1, "success": 1, "failed": 0, "friends": 5, "sent": 5},
  "trend": [{"date": "2026-08-10", "success": 3, "failed": 0}],
  "recent_runs": [{"id": 1, "account_nickname": "小号", "status": "success",
                   "started_at": "...", "success_count": 5, "fail_count": 0}],
  "quota": {"active": true, "expire_text": "还剩 30 天", "account_limit": 2,
            "account_used": 2, "daily_task_limit": 3, "daily_task_used": 1}
}
```

---

## 4. 抖音账号 `/douyin`

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/douyin/accounts` | user | 账号列表（分页；`status` 筛选） |
| POST | `/douyin/accounts` | user | 新增托管账号（创建占位记录，返回 `account_id` 与 `login_session_id`） |
| GET | `/douyin/accounts/<account_id>` | user | 账号详情 |
| PATCH | `/douyin/accounts/<account_id>` | user | 改备注 / 代理 / `enabled`（暂停托管）/ `status`（登录态） |
| DELETE | `/douyin/accounts/<account_id>` | user | 删除账号（连带任务与登录态文件） |
| POST | `/douyin/login/browser` | user | 启动浏览器扫码登录，返回 `session_id` + 二维码；`visible=true` 时改为**开可见窗口由用户手动登录**（二维码渲染不出来时的兜底） |
| GET | `/douyin/capabilities` | user | 宿主机能力（`display`/`visible_supported`/`container`/`browser_found`）：前端据此决定能否「在窗口里登录」 |
| POST | `/douyin/login/sms/send` | user | 发送短信验证码 |
| POST | `/douyin/login/sms/submit` | user | 提交短信验证码完成登录 |
| GET | `/douyin/login/status` | user | 轮询登录状态（`session_id` 参数） |
| POST | `/douyin/login/cancel` | user | 取消登录 |
| POST | `/douyin/login/refresh` | user | 重新生成二维码 |
| POST | `/douyin/accounts/<account_id>/check` | user | 检测登录态是否有效 |
| POST | `/douyin/accounts/<account_id>/cookies` | user | 手动导入 Cookie 登录态（扫码失败时的兜底；支持 Cookie 头 / JSON / Netscape 多种格式，`verify=true` 会真机校验） |
| GET | `/douyin/task-records` | user | 任务执行记录（分页，支持账号/状态/日期） |
| GET | `/douyin/task-records/<run_id>` | user | 单次执行详情（含每个好友的结果） |
| GET | `/douyin/stats/send-history` | user | 发送台账（哪天给谁发过） |
| POST | `/douyin/task-records/batch-set-failed` | user | 批量把卡住的任务标记失败（目标站同名接口） |

**启动扫码登录**

```
POST /douyin/login/browser
{"account_id": 3, "method": "qrcode"}
→ 200 {"code":0,"data":{"session_id":"ls_ab12...","method":"qrcode",
        "qrcode_image":"data:image/png;base64,...","qrcode_data":"https://...",
        "expire_at":"2026-08-16 12:05:00","status":"pending"}}
```

**轮询登录状态**

```
GET /douyin/login/status?session_id=ls_ab12
→ 200 {"code":0,"data":{"status":"confirmed","step":"saving_session","message":"登录成功",
        "account":{"id":3,"nickname":"小号","douyin_uid":"123456","avatar":"https://..."}}}
```
`status` 取值：`pending`（等待扫码）· `scanned`（已扫码待确认）· `confirmed`（已确认，正在保存）·
`success` · `failed` · `canceled` · `expired`。

**账号对象**（`/douyin/accounts*` 共用）

```json
{
  "id": 3, "user_id": 2, "nickname": "小号", "douyin_uid": "123456", "sec_uid": "MS4wLjABAAAA...",
  "avatar": "https://...", "phone_masked": "138****8888", "remark": "主用小号",
  "status": "online", "enabled": true, "login_type": "qrcode", "proxy_id": null,
  "task_count": 1, "status_text": "在线", "last_check_at": "...", "last_error": "",
  "created_at": "...", "updated_at": "..."
}
```

> **两个开关语义完全不同，前端必须分开显示：**
> * `enabled=false` → **暂停托管**（保留登录态，调度器不再执行该账号的任务）
> * `status` → **登录态**：`online`（可用）/ `offline` / `expired`（需重新扫码）/
>   `banned` / `need_verify`
> * `storage_path`、cookies、localStorage **永不返回**（隐私红线）。
>
> `PATCH /douyin/accounts/<id>` 可改 `remark` / `proxy_id` / `enabled` / `status`。

---

## 5. 定时任务 `/tasks` 与好友 `/friends`

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/tasks` | user | 定时任务列表 |
| POST | `/tasks` | user | 新建定时任务 |
| GET | `/tasks/<task_id>` | user | 任务详情 |
| PUT | `/tasks/<task_id>` | user | 更新任务（整字段覆盖） |
| DELETE | `/tasks/<task_id>` | user | 删除任务 |
| POST | `/tasks/<task_id>/toggle` | user | 启用/停用 |
| POST | `/tasks/<task_id>/run` | user | 立即执行一次（计入每日次数） |
| POST | `/tasks/<task_id>/preview` | user | 预览下 5 次执行时间与随机文案 |
| GET | `/tasks/<task_id>/runs` | user | 该任务的执行记录 |
| GET | `/friends` | user | 好友列表（按 `account_id` 筛选） |
| POST | `/friends` | user | 添加好友 |
| POST | `/friends/import` | user | 批量导入（每行一个昵称，可选 `昵称,sec_uid`） |
| PUT | `/friends/<friend_id>` | user | 修改好友 |
| DELETE | `/friends/<friend_id>` | user | 删除好友 |
| POST | `/friends/batch-delete` | user | 批量删除好友（body `{"ids":[1,2,3]}`，单次 ≤ 500） |
| POST | `/friends/sync` | user | 从抖音拉取最近会话好友（需账号在线） |
| GET | `/douyin/stats/send-history` | user | 发送台账（哪天给谁发过） |

**任务对象**

```json
{
  "id": 1, "account_id": 3, "account_nickname": "小号", "name": "自动续火花",
  "task_type": "spark", "enabled": true, "time_of_day": "08:30",
  "repeat_mode": "daily", "weekdays": [1,2,3,4,5], "interval_minutes": 1440,
  "jitter_seconds": 300, "timezone": "Asia/Shanghai",
  "friend_mode": "all", "friend_ids": [1,2], "friend_count": 5,
  "messages": ["火花不能断 🔥", "打卡 ✨"], "send_gap_seconds": {"min":3,"max":9},
  "daily_limit": 1, "max_retry": 2, "cron_expr": "",
  "last_run_at": "2026-08-16 00:30:00", "last_status": "success",
  "next_run_at": "2026-08-17 00:30:00", "created_at": "...", "updated_at": "..."
}
```

**创建/更新任务的请求体**（字段与上面一一对应，均可选）

```json
{"account_id":3, "name":"自动续火花", "time_of_day":"08:30", "repeat_mode":"daily",
 "weekdays":[], "interval_minutes":1440, "jitter_seconds":300, "timezone":"Asia/Shanghai",
 "friend_mode":"all", "friend_ids":[1,2],
 "messages":["火花不能断 🔥"], "send_gap_seconds":{"min":3,"max":9},
 "daily_limit":1, "max_retry":2, "enabled":true}
```

**执行记录**

```
GET /douyin/task-records?page=1&page_size=20&account_id=3&status=success&start_date=2026-08-01
data.items[] = {"id":1,"task_id":1,"account_id":3,"account_nickname":"小号",
  "run_date":"2026-08-16","trigger_type":"schedule","status":"success",
  "started_at":"...","finished_at":"...","duration_ms":18342,
  "total_count":5,"success_count":5,"fail_count":0,"message":"完成",
  "items":[{"friend_name":"小明","status":"success","message":"火花不能断 🔥","error_code":""}]}
```
> `items` 只在单条详情里返回，列表接口不返回以减小响应体。

---

## 6. 套餐 / 卡密 / 订单 / 支付

| 方法 | 路径 | auth | 说明 |
| --- | --- | --- | --- |
| GET | `/packages` | public | 套餐列表（价格、权益、是否启用） |
| GET | `/user/packages` | user | 套餐列表 + 我的当前套餐与限制 |
| GET | `/user/packages/limit-info` | user | 配额详情（账号数/今日次数） |
| GET | `/user/packages/limit-status` | user | 轻量配额状态（前端轮询） |
| POST | `/user/packages/purchase` | user | 下单购买（返回订单与收银台地址） |
| POST | `/user/packages/recharge` | user | 余额充值下单 |
| GET | `/user/packages/pay-methods` | user | 可用支付方式（`pay.enabled=0` 时只返回卡密） |
| POST | `/user/card-keys/redeem` | user | 卡密兑换（激活/续期/充值余额） |
| GET | `/user/card-keys` | user | 我的兑换记录 |
| GET | `/user/orders` | user | 我的订单（分页） |
| GET | `/user/orders/<order_no>` | user | 订单详情（含支付状态轮询用字段） |
| POST | `/user/orders/<order_no>/cancel` | user | 取消未支付订单 |
| POST | `/user/orders/<order_no>/query` | user | 主动向支付通道查单（补偿回调丢失） |
| GET | `/pay/return` | public | 支付同步跳回页（重定向到前端） |
| GET/POST | `/pay/notify/<channel>` | public | 支付异步回调（**仅 pro 版注册**） |

**兑换卡密**

```
POST /user/card-keys/redeem
{"code":"ABCD-EFGH-..."}
→ 200 {"code":0,"data":{"kind":"renew","days_added":30,"package_name":"月卡",
        "expire_before":"2026-08-16 00:00:00","expire_after":"2026-09-15 00:00:00",
        "message":"月卡已续期 30 天"}}
→ 400 {"code":"card_invalid","message":"卡密不存在或已被停用"}
→ 409 {"code":"card_used","message":"该卡密已被使用（2026-08-01 12:00:00）"}
```

**下单**

```
POST /user/packages/purchase
{"package_id":2,"channel":"easypay"}
→ 200 {"code":0,"data":{"order_no":"SK20260816...","amount_cents":1900,
        "status":"pending","pay_url":"https://pay.example.com/...","channel":"easypay",
        "qr_code":"","expire_at":"..."}}
```
`channel` 取值：`card`（仅生成订单，用卡密支付）· `easypay` · `alipay` · `wechat` ·
`balance`（余额支付，立即完成）· `manual`（人工确认，pro 版后台点「确认收款」）。

**支付方式**

```json
{"items":[{"channel":"card","name":"卡密兑换","enabled":true,"icon":"key"},
          {"channel":"easypay","name":"支付宝/微信","enabled":false,"icon":"qr"}],
 "online_pay_enabled": false}
```

---

## 7. 管理端 `/admin`（全部需要 admin）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/stats` | 概览：用户/账号/任务/订单/收入趋势 |
| GET | `/admin/users` | 用户列表（keyword/role/status/套餐） |
| POST | `/admin/users` | 新建用户 |
| GET | `/admin/users/<user_id>` | 用户详情（含账号、任务、订单、卡密记录） |
| PUT | `/admin/users/<user_id>` | 改资料/角色/状态 |
| POST | `/admin/users/<user_id>/reset-password` | 重置密码 |
| POST | `/admin/users/<user_id>/grant-package` | 手动授权套餐 |
| POST | `/admin/users/<user_id>/balance` | 调整余额（正负均可，强制备注） |
| DELETE | `/admin/users/<user_id>` | 删除用户（连带数据） |
| GET | `/admin/card-keys` | 卡密列表（批次/状态/套餐筛选） |
| GET | `/admin/card-keys/stats` | 卡密统计（各状态数量、批次汇总） |
| POST | `/admin/card-keys/generate` | 批量生成卡密 |
| POST | `/admin/card-keys/<card_id>/status` | 启用/停用卡密 |
| DELETE | `/admin/card-keys/<card_id>` | 删除未使用卡密 |
| POST | `/admin/card-keys/export` | 导出卡密（txt/csv，返回下载内容） |
| GET | `/admin/packages` | 套餐列表 |
| POST | `/admin/packages` | 新建套餐 |
| PUT | `/admin/packages/<package_id>` | 修改套餐 |
| DELETE | `/admin/packages/<package_id>` | 删除套餐（有订单时改为停用） |
| GET | `/admin/orders` | 订单列表（状态/渠道/日期/关键词） |
| POST | `/admin/orders/<order_no>/confirm` | 人工确认收款（补单，写流水与套餐） |
| POST | `/admin/orders/<order_no>/refund` | 标记退款（记录，不自动回退通道） |
| GET | `/admin/douyin-accounts` | 全部抖音账号 |
| POST | `/admin/douyin-accounts/<account_id>/status` | 修改账号状态 |
| GET | `/admin/scheduled-tasks` | 全部定时任务 |
| POST | `/admin/scheduled-tasks/batch` | 批量启用/停用/删除任务 |
| POST | `/admin/task-manage/scheduled-tasks/batch` | 同上（兼容目标站路径） |
| POST | `/admin/tasks/<task_id>/run` | 管理员代跑一次 |
| GET | `/admin/tasks` | 任务执行记录（全站） |
| GET | `/admin/task-records` | 同上（别名） |
| GET | `/admin/logs` | 运行日志（scope/level/时间/keyword） |
| GET | `/admin/logs/admin` | 管理员操作审计日志 |
| DELETE | `/admin/logs` | 清理日志（按天数） |
| GET | `/admin/announcements` | 公告列表 |
| POST | `/admin/announcements` | 新建公告 |
| PUT | `/admin/announcements/<announcement_id>` | 修改公告 |
| DELETE | `/admin/announcements/<announcement_id>` | 删除公告 |
| GET | `/admin/configs` | 站点配置（分组返回） |
| PUT | `/admin/configs` | 批量保存配置 |
| GET | `/admin/proxies` | 代理列表 |
| POST | `/admin/proxies` | 新增代理 |
| PUT | `/admin/proxies/<proxy_id>` | 修改代理 |
| DELETE | `/admin/proxies/<proxy_id>` | 删除代理 |
| POST | `/admin/proxies/<proxy_id>/check` | 测试代理连通性 |
| GET | `/admin/system-environment` | 环境体检 + 运行指标 + worker 心跳 |
| GET | `/admin/browser-processes` | 当前浏览器进程/并发占用 |
| POST | `/admin/system/browser-processes/cleanup` | 清理残留进程 |
| GET | `/admin/agent-levels` | 分销等级列表（自托管版可为空） |
| GET/POST/PUT/DELETE | `/admin/agent-levels[/<id>]` | 分销等级增删改 |
| GET | `/admin/test-email` | 查看邮件配置与队列状态 |
| POST | `/admin/test-email` | 发送测试邮件（`to` 参数） |
| GET | `/admin/test-ai` | AI 文案功能状态（未配置时返回 `enabled:false`） |
| POST | `/admin/test-ai` | 测试 AI 文案生成（未配置时返回 400 与说明） |
| POST | `/admin/change-password` | 管理员改密（等同 `/user/change-password`） |
| GET | `/admin/update` | 当前版本与更新检查（离线：只报当前版本） |
| GET | `/admin/license1` | 授权信息占位（自托管版为 `self-hosted`） |

**卡密生成**

```
POST /admin/card-keys/generate
{"package_code":"monthly","card_type":"time","days":30,"count":50,
 "use_limit":1,"amount_cents":0,"note":"8 月批次","batch_no":"","prefix":""}
→ 200 {"code":0,"data":{"batch_no":"B20260816XXXX","count":50,
        "codes":["ABCDEFGHJK...", ...],"package_name":"月卡","benefit_text":"30 天套餐（可兑换 1 次）"}}
```

**卡密类型语义（重要，发卡前必须看懂）**

| 卡型 | `days` 含义 | `use_limit` 含义 | 兑换行为 |
| --- | --- | --- | --- |
| `time` | 给套餐加多少天 | **可兑换次数**（默认 1） | 每次消耗 1 次，续期从原到期时间往后叠加 |
| `count` | **加成有效天数**（默认 30） | **每天额外执行次数** | **一次性**（兑一次即 `status='used'`），调用 `grant_bonus_tasks` |
| `balance` | 忽略 | **可兑换次数**（默认 1） | 每次加 `amount_cents` 到余额并写流水 |

> `time` 卡是「卖时长」，`count` 卡是「卖每天的自动化次数」，两者叠加：
> `daily_task_limit = 套餐每日次数 + 次数卡加成`。次数卡**不增加账号位**（`account_limit` 不变）。
> 请用响应里的 `benefit_text` 给买家展示权益，不要让用户自己猜。

**批量保存站点配置（注意入参形状）**

```
PUT /admin/configs
# 推荐（契约写法 1）：列表
{"items":[{"key":"site.name","value":"抖音续火花系统","is_public":1},
          {"key":"pay.easypay.key","value":"secret"}]}
# 也可（写法 2）：扁平对象，脚本调用最方便
{"site.name":"抖音续火花系统","site.icp":"京ICP备..."}
→ 200 {"code":0,"data":{"saved":2,"changed":["site.name: ..."]}}
```
> 密钥类配置读取时会被脱敏成占位符；**把占位符原样回传会被当作「不修改」**，
> 所以前端可以安全地把整个表单提交回来。`is_public` 控制该键是否出现在
> 未登录可读的 `/auth/public-configs` 里（密钥必须保持 `is_public=0`）。

**批量导入好友（注意入参形状）**

```
POST /friends/import
{"account_id":3,"text":"好友A\n好友B,MS4wLjABAAAA..."}   ← 每行「昵称」或「昵称,sec_uid」
# 或者结构化写法：
{"account_id":3,"friends":[{"nickname":"好友A","sec_uid":""}]}
→ 200 {"code":0,"data":{"imported":2,"skipped":0,"errors":[]}}
```

**统计**

```json
{"users": {"total": 10, "today_new": 2, "active_7d": 6},
 "accounts": {"total": 8, "online": 5, "expired": 2},
 "tasks": {"total": 8, "enabled": 7, "today_runs": 6, "today_success": 5},
 "orders": {"total": 12, "paid": 9, "pending": 3, "revenue_cents": 17100, "today_revenue_cents": 1900},
 "cards": {"total": 100, "used": 40, "unused": 60},
 "trend": [{"date":"2026-08-10","runs":5,"success":5,"orders":1,"revenue_cents":1900}]}
```

---

## 8. 静态资源与前端路由

| 路径 | 说明 |
| --- | --- |
| `/` | 前端 SPA（`web/static/index.html`，非 `/api/` 前缀的未知路径回退到它） |
| `/static/*` | 前端静态资源（js/css/图片/字体） |
| `/assets/*` | 同上（Vite 风格产物路径） |
| `/api/v1/files/screenshot/<run_id>/<name>` | 任务截图（需登录且校验归属） |
| `/api/v1/files/export/cards/<batch_no>.csv` | 卡密导出（admin） |

**前端路由（hash 路由，避免服务端 rewrite 依赖）**

```
#/login  #/register  #/forgot           未登录区
#/dashboard                             仪表盘
#/accounts  #/accounts/:id              抖音账号
#/tasks  #/tasks/new  #/tasks/:id       定时任务
#/friends  #/records  #/records/:id     好友与执行记录
#/packages  #/orders  #/card             套餐 / 订单 / 卡密兑换
#/logs  #/settings                       日志 / 个人设置
#/admin                                  管理端（admin 角色）
#/admin/users  #/admin/cards  #/admin/packages  #/admin/orders
#/admin/accounts  #/admin/tasks  #/admin/records  #/admin/logs
#/admin/announcements  #/admin/configs  #/admin/proxies  #/admin/system
```

## 9. 前端可依赖的全局

* `window.__SPARK__ = {version, apiBase: "/api/v1", siteName, allowRegister, onlinePayEnabled}` —
  由 `web/static/index.html` 注入，前端启动时读取。
* 令牌存 `localStorage.spark_token` / `spark_refresh` / `spark_user`。
* 401 时前端自动尝试 `/auth/refresh` 一次，失败则跳 `#/login`。

## 10. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-08-16 | v1 | 首版冻结：对齐目标站功能清单（用户端 40 项 + 管理端 45 项 + 安装向导 5 项） |
| 2026-08-16 | v1.1 | ① 新增 `douyin_accounts.enabled`（暂停托管，与 `status` 登录态分离），`PATCH` 可改；② 登录 `remember` 延长 refresh 至 30 天并返回 `refresh_expires_in`；③ 配额新增次数卡加成 `bonus_task_count`/`bonus_expire_at`，`daily_task_limit` = 套餐 + 加成，`active` 改为「套餐有效或加成有效」；④ 冻结 `/friends/sync` 依赖的自动化签名 `app.automation.douyin.sync_friends(account, settings)`；⑤ 新增 `GET /api/v1/ping`、`/version`、`/runtime` 元接口 |

## 11. 接口归属（并行开发用，避免重复注册被覆盖）

| 模块 | 文件 | 覆盖的路由前缀 |
| --- | --- | --- |
| 核心元接口 | `api/core.py` | `/api/v1/health`、`/version`、`/ping`、`/runtime`、`/_routes` |
| 认证 | `api/auth.py` | `/auth/*` |
| 用户中心 | `api/user.py` | `/user/info`、`/user/dashboard`、`/user/change-password`、`/user/profile`、`/user/logs`、`/user/notifications*`、`/user/announcements` |
| 抖音账号 | `api/accounts.py` | `/douyin/*`（含登录会话、执行记录、台账） |
| 定时任务与好友 | `api/tasks.py` | `/tasks*`、`/friends*` |
| 套餐与订单（用户侧） | `api/packages.py`、`api/orders.py` | `/packages`、`/user/packages/*`、`/user/orders*` |
| 卡密兑换 | `api/cardkeys.py` | `/user/card-keys*`、`/admin/card-keys*`、卡密纯函数 `generate_cards`/`redeem_card`/`fulfill_order` |
| 管理端 | `api/admin.py` | `/admin/*`（除卡密） |
| 安装向导与站点配置 | `api/install.py`、`api/public.py` | `/api/v1/install/*`、`/auth/public-configs` |
| 文件 | `api/files.py` | `/api/v1/files/*` |
| 支付（仅 pro） | `app/payment/routes.py` | `/pay/notify/<channel>`、`/pay/return` |
