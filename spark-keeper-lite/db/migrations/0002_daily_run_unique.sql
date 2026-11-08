-- ============================================================================
--  0002 · 修正「每天只发一次」的唯一约束
--
--  背景（由调度器实现时发现的真实缺陷）：
--  0001 里的 ux_runs_daily 约束是
--      UNIQUE(account_id, run_date, trigger_type) WHERE trigger_type='schedule'
--  它把「今天这个账号已经安排过了」当成唯一性的依据。但 task_runs 里除了成功
--  记录，还有 skipped（配额用尽顺延）与 failed（浏览器起不来/登录态失效）——
--  这两类**并不是「已经发过了」**，却会占用当天的槽位，后果是：
--    * 配额用完后升级套餐，当天无法补跑；
--    * 一次失败后当天自动重试被唯一索引顶掉（INSERT 直接报约束冲突）。
--
--  修正后的不变量（更贴近业务）：
--      「同一个账号、同一天、同一个计划任务，最多只有一次**成功**（或部分成功）的执行」
--  失败与跳过允许存在多条，由调度器基于「今天是否已有 success 记录」来决定是否重试，
--  从而把业务互斥从「数据库约束」升级为「数据库约束（幂等兜底）+ 调度器判断（业务策略）」。
-- ============================================================================

DROP INDEX IF EXISTS ux_runs_daily;

-- ⚠️ 版本兼容：这条唯一索引原本直接写在这里，但它是**部分索引**
--     CREATE UNIQUE INDEX ux_runs_daily_success ... WHERE trigger_type='schedule' ...
-- 部分索引需要 SQLite 3.8.0+。CentOS 7 自带 SQLite 3.7.17 → 建库阶段直接
-- `near "WHERE": syntax error`（真实事故，用户机器上就是这么炸的）。
--
-- 现在改成：迁移里只建「无条件可用」的查询索引；
-- 真正的唯一约束由 app/db.py::ensure_daily_unique_index() 在
-- 「检测到 SQLite >= 3.8」时补建（老版本跳过，当日幂等由调度器代码保证）。

-- 查「今天是否已经成功跑过」是调度器每个 tick 都做的事，给它一个更贴合的索引
CREATE INDEX IF NOT EXISTS ix_runs_success_lookup
    ON task_runs(account_id, run_date, status, trigger_type);
