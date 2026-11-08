-- ============================================================================
--  0003 · 修复「删除用户时外键约束失败」
--
--  背景（真实缺陷，由 Lead 的集成探测发现）：
--  0001 里这三张表的外键指向 users 却**没有 ON DELETE 动作**：
--      card_keys.bound_user_id · card_keys.used_by
--      card_redemptions.user_id（NOT NULL，无动作）
--      orders.user_id（NOT NULL，无动作）
--      balance_logs.user_id（NOT NULL，无动作）
--  SQLite 对「无动作」外键在执行 DELETE 时会立即报 FOREIGN KEY constraint failed，
--  于是一旦用户兑换过卡 / 下过单 / 有过余额流水，管理员就**永远删不掉这个用户**，
--  前端表现是「删除失败」且没有任何可读原因。
--
--  修复策略（财务记录不能随用户删除而丢失）：
--  * card_keys.bound_user_id / used_by  → ON DELETE SET NULL（卡密留档，只是不知道是谁用的）
--  * card_redemptions.user_id           → ON DELETE SET NULL + 允许 NULL
--  * orders.user_id                     → ON DELETE SET NULL + 允许 NULL（订单是财务凭证，必须留）
--  * balance_logs.user_id               → ON DELETE SET NULL + 允许 NULL（资金流水必须留）
--
--  SQLite 不支持 ALTER TABLE 修改外键，所以只能「建新表 → 拷数据 → 换名」重建。
--  本迁移对全新库是幂等的（新库在 0001 里已经是正确结构，这里重建一次结果相同）。
-- ============================================================================

PRAGMA foreign_keys = OFF;

-- ---------------------------------------------------------------------------
-- card_keys
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_keys_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,
    code_hash     TEXT    NOT NULL,
    batch_id      INTEGER REFERENCES card_batches(id),
    package_id    INTEGER REFERENCES packages(id),
    card_type     TEXT    NOT NULL DEFAULT 'time' CHECK (card_type IN ('time', 'count', 'balance')),
    days          INTEGER NOT NULL DEFAULT 30,
    use_limit     INTEGER NOT NULL DEFAULT 1,
    used_count    INTEGER NOT NULL DEFAULT 0,
    amount_cents  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'unused'
                  CHECK (status IN ('unused', 'used', 'disabled', 'expired')),
    bound_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at       TEXT,
    expire_at     TEXT,
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);
INSERT OR IGNORE INTO card_keys_new
    (id, code, code_hash, batch_id, package_id, card_type, days, use_limit, used_count,
     amount_cents, status, bound_user_id, used_by, used_at, expire_at, note, created_at)
SELECT id, code, code_hash, batch_id, package_id, card_type, days, use_limit, used_count,
       amount_cents, status, bound_user_id, used_by, used_at, expire_at, note, created_at
FROM card_keys;
DROP TABLE card_keys;
ALTER TABLE card_keys_new RENAME TO card_keys;
CREATE INDEX IF NOT EXISTS ix_card_keys_status    ON card_keys(status, created_at);
CREATE INDEX IF NOT EXISTS ix_card_keys_batch     ON card_keys(batch_id);
CREATE INDEX IF NOT EXISTS ix_card_keys_bound     ON card_keys(bound_user_id);
CREATE INDEX IF NOT EXISTS ix_card_keys_usedby    ON card_keys(used_by, used_at);

-- ---------------------------------------------------------------------------
-- card_redemptions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_redemptions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_key_id   INTEGER NOT NULL REFERENCES card_keys(id),
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('activate', 'renew', 'topup', 'consume')),
    days_added    INTEGER NOT NULL DEFAULT 0,
    amount_cents  INTEGER NOT NULL DEFAULT 0,
    package_id    INTEGER REFERENCES packages(id),
    expire_before TEXT,
    expire_after  TEXT,
    ip            TEXT,
    created_at    TEXT    NOT NULL
);
INSERT OR IGNORE INTO card_redemptions_new
    (id, card_key_id, user_id, kind, days_added, amount_cents, package_id,
     expire_before, expire_after, ip, created_at)
SELECT id, card_key_id, user_id, kind, days_added, amount_cents, package_id,
       expire_before, expire_after, ip, created_at
FROM card_redemptions;
DROP TABLE card_redemptions;
ALTER TABLE card_redemptions_new RENAME TO card_redemptions;
CREATE INDEX IF NOT EXISTS ix_redemptions_user ON card_redemptions(user_id, created_at);

-- ---------------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no       TEXT    NOT NULL UNIQUE,
    user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    package_id     INTEGER REFERENCES packages(id),
    subject        TEXT    NOT NULL,
    amount_cents   INTEGER NOT NULL,
    payable_cents  INTEGER NOT NULL,
    channel        TEXT    NOT NULL DEFAULT 'card'
                   CHECK (channel IN ('card', 'easypay', 'alipay', 'wechat', 'balance', 'manual')),
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'paid', 'failed', 'canceled', 'refunded', 'expired')),
    trade_no       TEXT,
    pay_url        TEXT,
    pay_payload    TEXT    NOT NULL DEFAULT '{}',
    paid_at        TEXT,
    expire_at      TEXT,
    notify_count   INTEGER NOT NULL DEFAULT 0,
    last_notify_at TEXT,
    remark         TEXT    NOT NULL DEFAULT '',
    client_ip      TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
INSERT OR IGNORE INTO orders_new
    (id, order_no, user_id, package_id, subject, amount_cents, payable_cents, channel, status,
     trade_no, pay_url, pay_payload, paid_at, expire_at, notify_count, last_notify_at, remark,
     client_ip, created_at, updated_at)
SELECT id, order_no, user_id, package_id, subject, amount_cents, payable_cents, channel, status,
       trade_no, pay_url, pay_payload, paid_at, expire_at, notify_count, last_notify_at, remark,
       client_ip, created_at, updated_at
FROM orders;
DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;
CREATE INDEX IF NOT EXISTS ix_orders_user   ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_trade  ON orders(trade_no);

-- ---------------------------------------------------------------------------
-- balance_logs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS balance_logs_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    change_cents  INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    ref_type      TEXT,
    ref_id        TEXT,
    remark        TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);
INSERT OR IGNORE INTO balance_logs_new
    (id, user_id, change_cents, balance_after, kind, ref_type, ref_id, remark, created_at)
SELECT id, user_id, change_cents, balance_after, kind, ref_type, ref_id, remark, created_at
FROM balance_logs;
DROP TABLE balance_logs;
ALTER TABLE balance_logs_new RENAME TO balance_logs;
CREATE INDEX IF NOT EXISTS ix_balance_user ON balance_logs(user_id, created_at);

PRAGMA foreign_keys = ON;
