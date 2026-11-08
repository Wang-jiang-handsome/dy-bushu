-- ============================================================================
--  spark-keeper 初始数据库结构 (SQLite / PostgreSQL 兼容子集)
--  设计约定：
--   * 时间一律存 UTC 的 ISO8601 字符串（'YYYY-MM-DD HH:MM:SS'），只有需要
--     按天聚合的地方才另存本地日期字段（如 run_date）。
--   * 金额一律用「分」存整数（amount_cents），避免浮点误差。
--   * 布尔用 INTEGER 0/1。
--   * 枚举用 TEXT + CHECK 约束，便于阅读与迁移。
--   * JSON 字段用 TEXT 存（SQLite 无 JSON 类型），读写走 app/common/jsonstore.py
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. 用户与鉴权
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT,
    phone             TEXT,
    username          TEXT    NOT NULL UNIQUE,
    password_hash     TEXT    NOT NULL,          -- pbkdf2_sha256$iters$salt$hash
    password_algo     TEXT    NOT NULL DEFAULT 'pbkdf2_sha256',
    role              TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'agent', 'admin')),
    status            TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    nickname          TEXT,
    avatar            TEXT,
    balance_cents     INTEGER NOT NULL DEFAULT 0,
    agent_level_id    INTEGER,                   -- 分销等级（自托管版通常为空）
    parent_agent_id   INTEGER,                   -- 上级代理
    commission_rate   REAL    NOT NULL DEFAULT 0,-- 分销佣金比例 0~1
    package_id        INTEGER,                   -- 当前生效套餐
    package_expire_at TEXT,                      -- 套餐到期时间（UTC）
    quota_json        TEXT    NOT NULL DEFAULT '{}', -- 权益/配额快照（见 common/quota.py）
    last_login_at     TEXT,
    last_login_ip     TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
-- 注意（SQLite 版本兼容）：原来这里写的是**部分索引**
--     ... ON users(email) WHERE email IS NOT NULL
-- 部分索引要 SQLite 3.8.0+；CentOS 7 自带的是 3.7.17，会直接报
-- `near "WHERE": syntax error`（真实事故）。而 SQLite 的唯一索引本来就把
-- 多个 NULL 视为互不相同，所以去掉 WHERE 后语义完全等价、还能兼容老版本。
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS ix_users_created  ON users(created_at);
CREATE INDEX IF NOT EXISTS ix_users_parent   ON users(parent_agent_id);

-- 服务端会话（JWT 之外再做一层 jti 白名单，支持「强制下线」与「改密后失效」）
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jti          TEXT    NOT NULL UNIQUE,
    token_kind   TEXT    NOT NULL DEFAULT 'access' CHECK (token_kind IN ('access', 'refresh')),
    issued_at    TEXT    NOT NULL,
    expire_at    TEXT    NOT NULL,
    revoked_at   TEXT,
    user_agent   TEXT,
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user   ON sessions(user_id, expire_at);
CREATE INDEX IF NOT EXISTS ix_sessions_expire ON sessions(expire_at);

-- 邮箱/短信验证码（注册、找回密码、登录二次校验共用）
CREATE TABLE IF NOT EXISTS verify_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene       TEXT    NOT NULL CHECK (scene IN ('register', 'reset_password', 'login', 'bind')),
    channel     TEXT    NOT NULL CHECK (channel IN ('email', 'sms')),
    target      TEXT    NOT NULL,               -- 邮箱或手机号
    code_hash   TEXT    NOT NULL,               -- 只存哈希，不存明文
    attempts    INTEGER NOT NULL DEFAULT 0,
    used_at     TEXT,
    expire_at   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS ix_verify_lookup ON verify_codes(target, scene, created_at);

-- ---------------------------------------------------------------------------
-- 2. 套餐 / 发卡 / 兑换
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS packages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT    NOT NULL UNIQUE,      -- 稳定标识，如 monthly / quarterly
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    days           INTEGER NOT NULL DEFAULT 30,
    price_cents    INTEGER NOT NULL DEFAULT 0,
    original_cents INTEGER,
    account_limit  INTEGER NOT NULL DEFAULT 1,   -- 可绑定抖音账号数
    daily_task_limit INTEGER NOT NULL DEFAULT 3,-- 每天定时任务执行次数上限
    features_json  TEXT    NOT NULL DEFAULT '[]',
    sort_order     INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS card_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no     TEXT    NOT NULL UNIQUE,
    package_id   INTEGER REFERENCES packages(id),
    card_type    TEXT    NOT NULL DEFAULT 'time' CHECK (card_type IN ('time', 'count', 'balance')),
    days         INTEGER NOT NULL DEFAULT 30,    -- time 型：加多少天
    use_limit    INTEGER NOT NULL DEFAULT 1,     -- count 型：总共可用次数
    amount_cents INTEGER NOT NULL DEFAULT 0,     -- balance 型：充值面额
    quantity     INTEGER NOT NULL DEFAULT 0,
    note         TEXT    NOT NULL DEFAULT '',
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS card_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,       -- 展示给用户的卡密（规范化大写）
    code_hash     TEXT    NOT NULL,              -- sha256(code)，查卡用
    batch_id      INTEGER REFERENCES card_batches(id),
    package_id    INTEGER REFERENCES packages(id),
    card_type     TEXT    NOT NULL DEFAULT 'time' CHECK (card_type IN ('time', 'count', 'balance')),
    days          INTEGER NOT NULL DEFAULT 30,
    use_limit     INTEGER NOT NULL DEFAULT 1,
    used_count    INTEGER NOT NULL DEFAULT 0,
    amount_cents  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'unused'
                  CHECK (status IN ('unused', 'used', 'disabled', 'expired')),
    bound_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 定向卡：只能该用户兑换
    used_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 兑换者；用户被删则置空留档
    used_at       TEXT,
    expire_at     TEXT,                          -- 卡密本身的激活截止时间
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_card_keys_status  ON card_keys(status, created_at);
CREATE INDEX IF NOT EXISTS ix_card_keys_batch   ON card_keys(batch_id);
CREATE INDEX IF NOT EXISTS ix_card_keys_bound   ON card_keys(bound_user_id);
CREATE INDEX IF NOT EXISTS ix_card_keys_usedby  ON card_keys(used_by, used_at);

-- 卡密兑换流水（审计用，卡密被兑换/扣次都要留痕）
-- 注意：卡密与流水都是「钱」的记录，删除用户时**不能跟着删**，也不该阻止删除，
-- 所以外键用 SET NULL 保留孤儿记录（0003 迁移会给老库重建这几张表）。
CREATE TABLE IF NOT EXISTS card_redemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_key_id  INTEGER NOT NULL REFERENCES card_keys(id),
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('activate', 'renew', 'topup', 'consume')),
    days_added   INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    package_id   INTEGER REFERENCES packages(id),
    expire_before TEXT,
    expire_after  TEXT,
    ip           TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_redemptions_user ON card_redemptions(user_id, created_at);

-- 定向/已用卡密指向的用户被删掉时置空，而不是留下悬挂引用或阻止删除
CREATE INDEX IF NOT EXISTS ix_card_keys_bound_user ON card_keys(bound_user_id);
CREATE INDEX IF NOT EXISTS ix_card_keys_used_user  ON card_keys(used_by);

-- ---------------------------------------------------------------------------
-- 3. 订单与支付
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no       TEXT    NOT NULL UNIQUE,      -- 业务单号（对外）
    user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 删用户保留订单（财务留档）
    package_id     INTEGER REFERENCES packages(id),
    subject        TEXT    NOT NULL,
    amount_cents   INTEGER NOT NULL,
    payable_cents  INTEGER NOT NULL,
    channel        TEXT    NOT NULL DEFAULT 'card'  -- card / easypay / alipay / wechat / balance / manual
                   CHECK (channel IN ('card', 'easypay', 'alipay', 'wechat', 'balance', 'manual')),
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'paid', 'failed', 'canceled', 'refunded', 'expired')),
    trade_no       TEXT,                          -- 第三方交易号
    pay_url        TEXT,                          -- 收银台跳转地址
    pay_payload    TEXT    NOT NULL DEFAULT '{}', -- 渠道原始返回
    paid_at        TEXT,
    expire_at      TEXT,
    notify_count   INTEGER NOT NULL DEFAULT 0,
    last_notify_at TEXT,
    remark         TEXT    NOT NULL DEFAULT '',
    client_ip      TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_user    ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_status  ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_trade   ON orders(trade_no);

-- 支付回调原始报文（对账/排障的最终依据，绝不丢）
CREATE TABLE IF NOT EXISTS payment_notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT    NOT NULL,
    order_no    TEXT,
    trade_no    TEXT,
    sign_ok     INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    raw_body    TEXT    NOT NULL DEFAULT '',
    raw_headers TEXT    NOT NULL DEFAULT '{}',
    result      TEXT    NOT NULL DEFAULT '',
    ip          TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notify_order ON payment_notifications(order_no, created_at);

-- 资金流水（余额充值/消费/佣金）
CREATE TABLE IF NOT EXISTS balance_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 删用户保留流水（财务留档）
    change_cents INTEGER NOT NULL,               -- 正数入账、负数出账
    balance_after INTEGER NOT NULL,
    kind         TEXT    NOT NULL,               -- topup / purchase / commission / refund / adjust
    ref_type     TEXT,                           -- order / card / manual
    ref_id       TEXT,
    remark       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_balance_user ON balance_logs(user_id, created_at);

-- 分销等级（自托管版可不用，保留以便单租户也能开「下级」）
CREATE TABLE IF NOT EXISTS agent_levels (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    level          INTEGER NOT NULL DEFAULT 1,
    commission_rate REAL   NOT NULL DEFAULT 0,
    card_discount  REAL    NOT NULL DEFAULT 1,   -- 拿卡折扣，1 = 原价
    daily_card_quota INTEGER NOT NULL DEFAULT 0,-- 每天可领卡数量，0 = 不限制
    remark         TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- 4. 抖音账号 / 登录态 / 代理
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    scheme      TEXT    NOT NULL DEFAULT 'http' CHECK (scheme IN ('http', 'https', 'socks5')),
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    username    TEXT,
    password_enc TEXT,                            -- 对称加密存储，见 common/crypto.py
    region      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'dead')),
    last_check_at TEXT,
    last_check_ok INTEGER,
    remark      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proxies_status ON proxies(status);

-- 账号：一条记录 = 一个抖音账号在该平台上的托管实例
CREATE TABLE IF NOT EXISTS douyin_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    nickname      TEXT    NOT NULL DEFAULT '',
    douyin_uid    TEXT,                           -- 抖音号/uid（登录后回填）
    sec_uid       TEXT,
    avatar        TEXT,
    phone_masked  TEXT,
    remark        TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,     -- 0 = 暂停托管（保留登录态，不参与调度）
    status        TEXT    NOT NULL DEFAULT 'offline'
                  CHECK (status IN ('online', 'offline', 'expired', 'banned', 'need_verify')),
    login_type    TEXT    NOT NULL DEFAULT 'qrcode' CHECK (login_type IN ('qrcode', 'sms')),
    proxy_id      INTEGER REFERENCES proxies(id),
    storage_path  TEXT,                           -- 登录态文件（cookies+localStorage）相对路径
    storage_updated_at TEXT,
    last_check_at TEXT,
    last_error    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accounts_user   ON douyin_accounts(user_id, status);

-- 扫码/短信登录会话（前端每 1~2 秒轮询一次状态）
CREATE TABLE IF NOT EXISTS login_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id    INTEGER REFERENCES douyin_accounts(id) ON DELETE CASCADE,
    method        TEXT    NOT NULL CHECK (method IN ('qrcode', 'sms')),
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'scanned', 'confirmed', 'success', 'failed', 'canceled', 'expired')),
    qrcode_data   TEXT,                           -- 二维码内容（前端本地渲染成图）
    qrcode_image  TEXT,                           -- 或直接给 base64 图
    phone         TEXT,
    message       TEXT    NOT NULL DEFAULT '',
    step          TEXT    NOT NULL DEFAULT '',    -- 细分进度：send_sms / verify_sms / waiting_scan ...
    expire_at     TEXT    NOT NULL,
    finished_at   TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_sessions_user ON login_sessions(user_id, created_at);

-- ---------------------------------------------------------------------------
-- 5. 定时任务与执行记录
-- ---------------------------------------------------------------------------
-- 一个 douyin_account 通常对应一条 active 的定时任务（自托管版默认如此，但结构上允许多条）
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id     INTEGER NOT NULL REFERENCES douyin_accounts(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL DEFAULT '自动续火花',
    task_type      TEXT    NOT NULL DEFAULT 'spark' CHECK (task_type IN ('spark', 'message', 'check')),
    enabled        INTEGER NOT NULL DEFAULT 1,
    cron_expr      TEXT,                          -- 高级用法；为空则用下面的 time+repeat
    time_of_day    TEXT    NOT NULL DEFAULT '08:30', -- HH:MM（按 timezone 解释）
    repeat_mode    TEXT    NOT NULL DEFAULT 'daily'
                   CHECK (repeat_mode IN ('daily', 'weekdays', 'weekends', 'custom', 'interval')),
    weekdays       TEXT    NOT NULL DEFAULT '[]', -- custom 模式：["1","3","5"]
    interval_minutes INTEGER NOT NULL DEFAULT 1440, -- interval 模式
    jitter_seconds INTEGER NOT NULL DEFAULT 300,  -- 随机抖动，避免整点集中触发
    timezone       TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
    friend_mode    TEXT    NOT NULL DEFAULT 'all' CHECK (friend_mode IN ('all', 'selected')),
    friend_ids     TEXT    NOT NULL DEFAULT '[]', -- 指定好友（friend 表的 id）
    messages_json  TEXT    NOT NULL DEFAULT '[]', -- 候选文案，随机挑一条
    send_gap_seconds TEXT  NOT NULL DEFAULT '{"min":3,"max":9}',
    daily_limit    INTEGER NOT NULL DEFAULT 1,    -- 每个好友每天最多发几条
    max_retry      INTEGER NOT NULL DEFAULT 2,
    last_run_at    TEXT,
    next_run_at    TEXT,
    last_status    TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_user    ON scheduled_tasks(user_id, enabled);
CREATE INDEX IF NOT EXISTS ix_tasks_account ON scheduled_tasks(account_id);
CREATE INDEX IF NOT EXISTS ix_tasks_next    ON scheduled_tasks(enabled, next_run_at);

-- 好友（续火花对象）
CREATE TABLE IF NOT EXISTS friends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL REFERENCES douyin_accounts(id) ON DELETE CASCADE,
    nickname    TEXT    NOT NULL,
    sec_uid     TEXT,
    remark      TEXT    NOT NULL DEFAULT '',
    spark_days  INTEGER NOT NULL DEFAULT 0,       -- 已知连续天数（尽力回填，可能为 0）
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_sent_at TEXT,
    last_status  TEXT   NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_friends_account ON friends(account_id, enabled);
CREATE UNIQUE INDEX IF NOT EXISTS ux_friends_unique ON friends(account_id, nickname);

-- 任务执行记录（一次「跑一遍」= 一条 task_runs，每个好友一条 task_run_items）
CREATE TABLE IF NOT EXISTS task_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER REFERENCES scheduled_tasks(id) ON DELETE SET NULL,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES douyin_accounts(id) ON DELETE CASCADE,
    run_date      TEXT    NOT NULL,               -- 按任务时区的 YYYY-MM-DD，用于「每天只跑一次」
    trigger_type  TEXT    NOT NULL DEFAULT 'schedule' CHECK (trigger_type IN ('schedule', 'manual', 'retry')),
    status        TEXT    NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped', 'canceled')),
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    duration_ms   INTEGER,
    total_count   INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    message       TEXT    NOT NULL DEFAULT '',
    error_code    TEXT    NOT NULL DEFAULT '',
    screenshots   TEXT    NOT NULL DEFAULT '[]',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_task   ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS ix_runs_user   ON task_runs(user_id, started_at);
CREATE INDEX IF NOT EXISTS ix_runs_date   ON task_runs(account_id, run_date);
-- 同一账号同一天同一计划任务，最多只能有**一次成功**的执行（防重复发送）。
-- 注意：不能用 (account_id, run_date, trigger_type) 直接唯一 —— skipped（配额顺延）
-- 与 failed（浏览器起不来）不是「已经发过了」，它们必须能重试。
-- 0002 迁移会把这里的老约束换成下面的部分唯一索引（老库自动升级）。
CREATE INDEX IF NOT EXISTS ix_runs_date   ON task_runs(account_id, run_date);
DROP INDEX IF EXISTS ux_runs_daily;

CREATE TABLE IF NOT EXISTS task_run_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    friend_id   INTEGER REFERENCES friends(id) ON DELETE SET NULL,
    friend_name TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'success', 'failed', 'skipped')),
    message     TEXT    NOT NULL DEFAULT '',      -- 实际发出的文案
    error_code  TEXT    NOT NULL DEFAULT '',
    error_detail TEXT   NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    screenshot  TEXT,
    created_at  TEXT    NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_items_run ON task_run_items(run_id);

-- 发送台账（幂等依据：同一账号+好友+日期只能成功一次）
CREATE TABLE IF NOT EXISTS send_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES douyin_accounts(id) ON DELETE CASCADE,
    friend_id  INTEGER REFERENCES friends(id) ON DELETE SET NULL,
    friend_key TEXT    NOT NULL,                  -- sec_uid 优先，否则昵称
    send_date  TEXT    NOT NULL,
    message    TEXT    NOT NULL DEFAULT '',
    run_id     INTEGER REFERENCES task_runs(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_send_history ON send_history(account_id, friend_key, send_date);

-- ---------------------------------------------------------------------------
-- 6. 运营：公告 / 配置 / 通知 / 审计日志
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    level      TEXT    NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'important')),
    is_pinned  INTEGER NOT NULL DEFAULT 0,
    is_active  INTEGER NOT NULL DEFAULT 1,
    publish_at TEXT,
    expire_at  TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ann_active ON announcements(is_active, is_pinned, created_at);

-- 站点键值配置（管理员在后台改，前端 /auth/public-configs 读白名单部分）
CREATE TABLE IF NOT EXISTS site_configs (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    is_public   INTEGER NOT NULL DEFAULT 0,       -- 1 = 允许未登录读取
    group_name  TEXT NOT NULL DEFAULT 'general',
    description TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

-- 站内通知（任务成功/失败、套餐到期提醒等）
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    level      TEXT    NOT NULL DEFAULT 'info',
    read_at    TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, read_at, created_at);

-- 操作日志（管理员动作、关键用户动作）
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT    NOT NULL DEFAULT '',
    scope      TEXT    NOT NULL DEFAULT 'user' CHECK (scope IN ('user', 'admin', 'system', 'worker')),
    action     TEXT    NOT NULL,
    target     TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    ip         TEXT,
    user_agent TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_scope ON audit_logs(scope, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_user  ON audit_logs(user_id, created_at);

-- 运行日志（页面 /app/logs 与 /admin/logs 的数据源；worker 也往里写）
CREATE TABLE IF NOT EXISTS run_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL DEFAULT 'user' CHECK (scope IN ('user', 'admin', 'system', 'worker')),
    level      TEXT    NOT NULL DEFAULT 'info' CHECK (level IN ('debug', 'info', 'warning', 'error')),
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source     TEXT    NOT NULL DEFAULT '',       -- 模块名，如 task.scheduler / cdp.login
    message    TEXT    NOT NULL,
    context    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runlogs_scope ON run_logs(scope, created_at);
CREATE INDEX IF NOT EXISTS ix_runlogs_user  ON run_logs(user_id, created_at);

-- worker 心跳（管理端 /admin/system-environment 展示）
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'scheduler',
    pid         INTEGER,
    version     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'idle',
    detail      TEXT NOT NULL DEFAULT '{}',
    started_at  TEXT NOT NULL,
    beat_at     TEXT NOT NULL
);

-- 邮件 / 短信发送队列与记录（SMTP 常被限流，先入队再发）
CREATE TABLE IF NOT EXISTS message_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT    NOT NULL CHECK (channel IN ('email', 'sms')),
    target      TEXT    NOT NULL,
    subject     TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    scene       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NOT NULL DEFAULT '',
    scheduled_at TEXT   NOT NULL,
    sent_at     TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON message_outbox(status, scheduled_at);

-- ---------------------------------------------------------------------------
-- 7. 首次运行的种子数据
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO packages
    (code, name, description, days, price_cents, original_cents, account_limit,
     daily_task_limit, features_json, sort_order, is_active, created_at, updated_at)
VALUES
    ('trial',   '体验卡', '7 天体验，1 个账号，每天 1 次自动续火花', 7,   0,    990,  1, 1,
     '["1 个抖音账号","每天 1 次自动续火花","保留 7 天执行记录"]', 10, 1, datetime('now'), datetime('now')),
    ('monthly', '月卡',   '30 天，2 个账号，每天 3 次', 30, 1900, 2900, 2, 3,
     '["2 个抖音账号","每天 3 次自动续火花","完整执行记录与截图","失败自动重试"]', 20, 1, datetime('now'), datetime('now')),
    ('quarter', '季卡',   '90 天，5 个账号，每天 6 次', 90, 4900, 8700, 5, 6,
     '["5 个抖音账号","每天 6 次自动续火花","完整执行记录与截图","失败自动重试","随机文案与发送间隔"]', 30, 1, datetime('now'), datetime('now')),
    ('yearly',  '年卡',   '365 天，10 个账号，每天 12 次', 365, 15900, 29000, 10, 12,
     '["10 个抖音账号","每天 12 次自动续火花","完整执行记录与截图","失败自动重试","随机文案与发送间隔","优先技术支持"]', 40, 1, datetime('now'), datetime('now'));

INSERT OR IGNORE INTO site_configs (key, value, is_public, group_name, description, updated_at) VALUES
    ('site.name',            '抖音续火花系统', 1, 'general',  '站点名称',                 datetime('now')),
    ('site.logo',            '/logo.svg',    1, 'general',  '站点 Logo 地址（相对 URL 根，前端产物在 web/static）', datetime('now')),
    ('site.icp',             '',              1, 'general',  '备案号（页脚展示）',        datetime('now')),
    ('site.customer_service', '',             1, 'general',  '客服联系方式',             datetime('now')),
    ('site.announcement',    '',              1, 'general',  '首页公告（纯文本，可空）',   datetime('now')),
    ('auth.allow_register',  '1',             1, 'auth',     '是否开放注册',             datetime('now')),
    ('auth.require_email_verify', '0',        1, 'auth',     '注册是否必须邮箱验证',       datetime('now')),
    ('pay.enabled',          '0',             1, 'payment',  '是否启用在线支付',          datetime('now')),
    ('pay.channel',          'easypay',       1, 'payment',  '支付渠道',                 datetime('now')),
    ('task.default_time',    '08:30',         1, 'task',     '默认执行时间 HH:MM',        datetime('now')),
    ('task.default_timezone','Asia/Shanghai', 1, 'task',     '默认时区',                 datetime('now')),
    ('task.max_concurrent',  '2',             0, 'task',     '同时执行的浏览器实例上限',   datetime('now')),
    ('system.installed_at',  '',              0, 'system',   '安装完成时间',             datetime('now'));
