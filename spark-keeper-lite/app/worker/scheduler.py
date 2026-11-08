"""调度器：到点入队、每日去重、配额、失败重试、心跳与维护。

一个 tick 按顺序做这些事
------------------------
1. **回收残留**：把「进程崩过、永远停在 running」的执行记录标记为失败；
2. **找到期任务**：``scheduled_tasks.enabled=1`` 且 ``douyin_accounts.enabled=1``
   且 ``douyin_accounts.status='online'`` 且 ``next_run_at<=now``；
3. **抖动**：对 ``jitter_seconds`` 做**确定性随机**（用 task_id+next_run_at 做种子），
   这样每个 tick 看到的抖动值一致 —— 否则每次 tick 重新随机，任务会被无限推迟；
4. **每日去重**：当天已有 ``success/partial`` 记录 → 直接顺延到下一次，不再跑；
5. **配额**：``Quota.refresh(user_id)`` + ``ensure_can_run_today()``，超限则
   写一条 ``skipped`` 记录（每天只写一条，避免刷屏）并把 ``next_run_at``
   推到「一会儿之后再看看」——**不是推到明天**，因为用户当天升级套餐后应该能补跑；
6. **入队**：``runner.enqueue_run(account_id, task_id, 'schedule')``；
7. **失败重试**：当天失败且没有成功的任务，按 ``max_retry``/``task_retry_backoff``
   用 ``trigger_type='retry'`` 再入队；
8. **心跳 + 维护**：写 ``worker_heartbeats``，定期 ``mailer.flush()`` / ``logging.prune()``。

三个 enabled 的语义（不要混）
----------------------------
* ``scheduled_tasks.enabled`` —— **任务级**开关（用户在任务页点「停用」）；
* ``douyin_accounts.enabled`` —— **账号级**暂停托管（用户点「暂停」）；
* ``douyin_accounts.status``    —— **登录态**（online/offline/expired/need_verify）。

只有三者都满足才执行。账号维度不满足时**不推进 next_run_at**：这样账号重新
上线后能补跑（同时用限流日志提示原因，而不是每个 tick 刷一条）。
"""

from __future__ import annotations

import random
import threading
import time
from datetime import timedelta
from typing import Any

from .. import db
from ..common import logging as sklog
from ..common import timeutil
from ..router import HttpError
from . import runner

#: 单次 tick 最多处理多少个到期任务（防止积压时一次 tick 跑太久）
MAX_DISPATCH_PER_TICK = 30
#: 配额超限后多久再检查一次（秒）。用户升级套餐后最多等这么久就能补跑。
QUOTA_RETRY_SECONDS = 600
#: 维护任务（清日志/发队列/清孤儿登录态）的最小间隔（秒）
MAINTENANCE_INTERVAL = 300.0
#: 「账号不可用」这类原因最多多久记一条日志（避免刷爆 run_logs）
REASON_LOG_INTERVAL = 3600.0

_reason_logged: dict[int, float] = {}
_state_lock = threading.RLock()
_last_maintenance = 0.0


# --------------------------------------------------------------------------
# 任务查询
# --------------------------------------------------------------------------
def due_tasks(settings: Any, *, now: Any = None, limit: int = MAX_DISPATCH_PER_TICK) -> list[dict]:
    """取「到点且账号可用」的任务。

    SQL 里直接做 INNER JOIN：账号被停用/离线的任务根本不返回，也就不会被误跑。
    """
    now = now or timeutil.now_utc()
    return db.query_all(
        """
        SELECT t.*, a.status AS account_status, a.enabled AS account_enabled,
               a.nickname AS account_nickname, a.user_id AS account_user_id
          FROM scheduled_tasks t
          JOIN douyin_accounts a ON a.id = t.account_id
         WHERE t.enabled = 1
           AND a.enabled = 1
           AND a.status = 'online'
           AND (t.next_run_at IS NULL OR t.next_run_at <= ?)
         ORDER BY t.next_run_at ASC
         LIMIT ?
        """,
        (timeutil.to_db(now), int(limit)),
    )


def blocked_tasks(*, now: Any = None, limit: int = 100) -> list[dict]:
    """到点了但账号不可用的任务（只用来记日志，不推进 next_run_at）。"""
    now = now or timeutil.now_utc()
    return db.query_all(
        """
        SELECT t.id, t.account_id, t.name, t.next_run_at, t.user_id,
               a.status AS account_status, a.enabled AS account_enabled
          FROM scheduled_tasks t
          JOIN douyin_accounts a ON a.id = t.account_id
         WHERE t.enabled = 1
           AND (a.enabled = 0 OR a.status <> 'online')
           AND t.next_run_at IS NOT NULL
           AND t.next_run_at <= ?
         ORDER BY t.next_run_at ASC
         LIMIT ?
        """,
        (timeutil.to_db(now), int(limit)),
    )


def success_today(account_id: int, run_date: str) -> dict | None:
    """当天该账号是否已经**成功**跑过一次（新索引 ux_runs_daily_success 的业务含义）。

    注意：判断依据是**查记录**，不是「等唯一索引报冲突」——索引只是幂等兜底，
    业务策略必须显式可读（skipped/failed 不算「已经发过」）。
    """
    # 只认**自动/重试**触发的成功：手动点「执行」只是自测，不该把当天的自动额度吃掉。
    # 以前不区分 trigger_type，用户白天手动测几次，晚上自动执行就被判「今天已成功」顺延到明天
    # —— 用户看到的就是「自动执行没有正常运行」。
    # 重复发送风险由「按好友的当日幂等」（sent_today / ux_runs_daily_success）兜底。
    return db.query_one(
        "SELECT id, status, trigger_type, started_at FROM task_runs "
        "WHERE account_id=? AND run_date=? AND status IN ('success','partial') "
        "  AND trigger_type <> 'manual' "
        "ORDER BY id DESC LIMIT 1",
        (int(account_id), str(run_date)),
    )


def skipped_today(task_id: int, run_date: str) -> int:
    return int(db.query_scalar(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND run_date=? AND status='skipped'",
        (int(task_id), str(run_date)), 0) or 0)


def retry_runs_today(task_id: int, run_date: str) -> int:
    return int(db.query_scalar(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND run_date=? AND trigger_type='retry'",
        (int(task_id), str(run_date)), 0) or 0)


# --------------------------------------------------------------------------
# 抖动与顺延
# --------------------------------------------------------------------------
def jitter_for(task: dict) -> int:
    """确定性的抖动秒数。

    用 ``(task_id, next_run_at)`` 做种子：同一轮到期时间内每个 tick 得到的结果
    相同（不会无限推迟），而不同任务/不同轮次之间又是分散的（不会整点齐发）。
    """
    configured = int(task.get("jitter_seconds") or 0)
    if configured <= 0:
        return 0
    seed = f"{task.get('id')}:{task.get('next_run_at')}"
    return random.Random(seed).randint(0, min(configured, 3600))


def effective_due(task: dict) -> Any:
    """实际到期时刻 = ``next_run_at`` + 抖动。"""
    base = timeutil.parse_db(task.get("next_run_at"))
    if base is None:
        return None
    return base + timedelta(seconds=jitter_for(task))


def next_run_after(task: dict, *, after: Any = None, status: str = "") -> str:
    """用 Lead 已实现的 ``timeutil.next_run_time`` 算下一次执行时间。"""
    after = after or timeutil.now_utc()
    # last_run_at 只在跑过之后才传：interval 模式要用它做锚点
    last_run_at = task.get("last_run_at") if status else None
    moment = timeutil.next_run_time(
        str(task.get("time_of_day") or "08:30"),
        str(task.get("timezone") or "Asia/Shanghai"),
        str(task.get("repeat_mode") or "daily"),
        task.get("weekdays"),
        int(task.get("interval_minutes") or 1440),
        after=after,
        last_run_at=last_run_at,
    )
    return timeutil.to_db(moment)


def save_task_schedule(task: dict, *, next_run_at: str | None = None,
                       last_status: str | None = None, ran: bool = False) -> None:
    data: dict[str, Any] = {"updated_at": timeutil.utc_now_str()}
    if next_run_at is not None:
        data["next_run_at"] = next_run_at
    if last_status is not None:
        data["last_status"] = str(last_status)[:20]
    if ran:
        data["last_run_at"] = timeutil.utc_now_str()
    try:
        db.update("scheduled_tasks", data, "id=?", (int(task["id"]),))
    except Exception as exc:      # noqa: BLE001 - 调度状态回写失败不该中断 tick
        sklog.warning(f"回写任务 {task.get('id')} 调度状态失败：{exc}",
                      scope="worker", source="task.scheduler")


def _quota_retry_time(task: dict, *, now: Any = None) -> str:
    """配额超限后的下一次检查时间。

    * 同一天内：``now + QUOTA_RETRY_SECONDS``（这样当天升级套餐能立刻补跑）；
    * 跨天（比如 23:55 再等 10 分钟就到第二天了）：不能把任务挪到第二天的
      00:05 —— 那时用户设的 ``time_of_day`` 还没到。此时按正常规则顺延。
    """
    now = now or timeutil.now_utc()
    tz = str(task.get("timezone") or "Asia/Shanghai")
    retry_at = now + timedelta(seconds=QUOTA_RETRY_SECONDS)
    today = timeutil.local_date_str(tz, at=now)
    if timeutil.local_date_str(tz, at=retry_at) == today:
        return timeutil.to_db(retry_at)
    return next_run_after(task, after=now)


# --------------------------------------------------------------------------
# 单个到期任务
# --------------------------------------------------------------------------
def handle_due_task(task: dict, settings: Any, *, now: Any = None) -> dict:
    """处理一个到期任务，返回 ``{action, reason}``。

    ``action`` ∈ ``init``（首次没排过时间）/ ``success_done``（今天已成功）/
    ``quota``（配额用尽）/ ``dispatched``（已入队）/ ``busy``（账号在跑）/
    ``error``。
    """
    now = now or timeutil.now_utc()
    tz = str(task.get("timezone") or "Asia/Shanghai")
    run_date = timeutil.local_date_str(tz, at=now)
    task_id = int(task.get("id") or 0)
    account_id = int(task.get("account_id") or 0)

    # ① 新任务从来没有排过时间：**不要立刻跑**（否则新建任务的瞬间就会发一条消息），
    #    只把 next_run_at 排上。
    if not task.get("next_run_at"):
        save_task_schedule(task, next_run_at=next_run_after(task, after=now))
        return {"action": "init", "reason": "首次排期"}

    # ② 今天已经成功过 → 顺延（这是「每天只发一次」的真正判定）
    done = success_today(account_id, run_date)
    if done:
        save_task_schedule(task, next_run_at=next_run_after(task, after=now),
                           last_status=str(done.get("status")))
        sklog.info(f"任务 {task_id} 今天已成功执行（run={done['id']}），顺延到下一次",
                   scope="worker", source="task.scheduler", user_id=task.get("user_id"))
        return {"action": "success_done", "reason": f"今日已成功 run={done['id']}"}

    # ③ 配额检查：套餐 + 次数卡加成
    user_id = int(task.get("user_id") or 0)
    try:
        from ..common.quota import Quota
        quota = Quota.refresh(user_id)
        quota.ensure_can_run_today()
    except HttpError as exc:
        reason = str(getattr(exc, "message", "") or exc)
        first_time = skipped_today(task_id, run_date) == 0
        if first_time:
            _record_skipped(task, run_date, f"今日执行次数已达上限，已自动顺延（{reason}）")
            sklog.info(f"任务 {task_id} 因配额跳过：{reason}",
                       scope="worker", source="task.scheduler", user_id=user_id)
        save_task_schedule(task, next_run_at=_quota_retry_time(task, now=now),
                           last_status="skipped")
        return {"action": "quota", "reason": reason}
    except Exception as exc:      # noqa: BLE001 - 配额模块异常不能拖垮调度
        sklog.warning(f"配额检查异常，本次按放行处理：{type(exc).__name__}: {exc}",
                      scope="worker", source="task.scheduler")

    # ④ 入队
    result = runner.enqueue_run(account_id, task_id, "schedule", settings=settings)
    if result.get("status") == "rejected":
        if result.get("error") == "task_running":
            # 上一次还在跑：不动 next_run_at，下个 tick 再看
            return {"action": "busy", "reason": "账号正在执行上一个任务"}
        _record_skipped(task, run_date, f"无法入队：{result.get('message') or result.get('error')}")
        save_task_schedule(task, next_run_at=next_run_after(task, after=now),
                           last_status="skipped")
        return {"action": "error", "reason": str(result.get("message") or result.get("error"))}

    save_task_schedule(task, next_run_at=next_run_after(task, after=now), ran=True,
                       last_status="running")
    return {"action": "dispatched", "reason": f"run={result.get('run_id')}"}


def _record_skipped(task: dict, run_date: str, message: str) -> int:
    """写一条 ``skipped`` 执行记录（不消耗配额，也不占用「已成功」槽位）。"""
    from ..automation import task_runner
    try:
        run_id = task_runner.create_run(task, {"id": int(task.get("account_id") or 0),
                                              "user_id": int(task.get("user_id") or 0)},
                                        "schedule", status="skipped", message=message)
        return run_id
    except Exception as exc:      # noqa: BLE001 - 记录失败不影响顺延
        sklog.warning(f"写 skipped 记录失败：{exc}", scope="worker", source="task.scheduler")
        return 0


# --------------------------------------------------------------------------
# 失败重试
# --------------------------------------------------------------------------
def retry_candidates(settings: Any, *, now: Any = None) -> list[dict]:
    """今天失败过、还没有成功、且该重试的任务。"""
    now = now or timeutil.now_utc()
    # 注意：0 是合法值（表示「失败后立刻重试」），不能用 `or 60` —— 那会把 0 变成 60。
    raw_backoff = getattr(settings, "task_retry_backoff", 60)
    try:
        backoff = max(0, int(raw_backoff))
    except (TypeError, ValueError):
        backoff = 60
    rows = db.query_all(
        """
        SELECT t.*, a.status AS account_status, a.enabled AS account_enabled
          FROM scheduled_tasks t
          JOIN douyin_accounts a ON a.id = t.account_id
         WHERE t.enabled = 1 AND a.enabled = 1 AND a.status = 'online'
         ORDER BY t.id
        """
    )
    out: list[dict] = []
    for task in rows:
        tz = str(task.get("timezone") or "Asia/Shanghai")
        run_date = timeutil.local_date_str(tz, at=now)
        if success_today(int(task["account_id"]), run_date):
            continue
        failed = db.query_one(
            "SELECT id, started_at, finished_at FROM task_runs "
            "WHERE task_id=? AND run_date=? AND status='failed' "
            "ORDER BY id DESC LIMIT 1",
            (int(task["id"]), run_date),
        )
        if not failed:
            continue
        if retry_runs_today(int(task["id"]), run_date) >= max(0, int(task.get("max_retry") or 0)):
            continue
        finished = timeutil.parse_db(failed.get("finished_at") or failed.get("started_at"))
        if finished and (now - finished).total_seconds() < backoff:
            continue
        out.append({**task, "_failed_run": failed["id"], "_run_date": run_date})
    return out


def handle_retry(task: dict, settings: Any) -> dict:
    result = runner.enqueue_run(int(task["account_id"]), int(task["id"]), "retry",
                                settings=settings)
    if result.get("status") == "rejected":
        return {"action": "retry_busy", "reason": str(result.get("error"))}
    sklog.info(f"任务 {task['id']} 失败重试已入队（run={result.get('run_id')}）",
               scope="worker", source="task.scheduler", user_id=task.get("user_id"))
    return {"action": "retried", "reason": f"run={result.get('run_id')}"}


# --------------------------------------------------------------------------
# 心跳 / 维护
# --------------------------------------------------------------------------
def heartbeat(settings: Any, status: str = "idle", detail: dict | None = None) -> None:
    """写 worker 心跳（``/admin/system-environment`` 展示）。"""
    from ..version import VERSION
    now = timeutil.utc_now_str()
    worker_id = str(getattr(settings, "worker_id", "") or "worker")
    payload = {
        "worker_id": worker_id, "kind": "scheduler", "pid": _pid(),
        "version": str(VERSION), "status": status[:20],
        "detail": timeutil.utc_now_str() if detail is None else _json_detail(detail),
        "started_at": _started_at(settings), "beat_at": now,
    }
    try:
        # 走 db.upsert：SQLite >= 3.24 用原生 ON CONFLICT；老版本（CentOS 7 的
        # 3.7.17）自动降级成 UPDATE→INSERT，否则心跳永远写不进去。
        db.upsert("worker_heartbeats", payload, conflict_columns=("worker_id",),
                  update_columns=("kind", "pid", "version", "status", "detail", "beat_at"))
    except Exception as exc:      # noqa: BLE001 - 心跳失败不能让调度器停摆
        sklog.debug(f"写心跳失败：{exc}", scope="worker", source="task.scheduler")


def _json_detail(detail: Any) -> str:
    import json
    try:
        return json.dumps(detail, ensure_ascii=False)[:1000]
    except (TypeError, ValueError):
        return "{}"


def _pid() -> int:
    import os
    return os.getpid()


def _started_at(settings: Any) -> str:
    value = getattr(settings, "_worker_started_at", None)
    if not value:
        value = timeutil.utc_now_str()
        try:
            setattr(settings, "_worker_started_at", value)
        except Exception:         # noqa: BLE001 - 只读 settings 就退化成本次时间
            pass
    return str(value)


def maintenance(settings: Any, *, force: bool = False) -> dict:
    """定期维护：发邮件队列、清日志、清孤儿登录态。"""
    global _last_maintenance
    now = time.monotonic()
    with _state_lock:
        if not force and (now - _last_maintenance) < MAINTENANCE_INTERVAL:
            return {"skipped": True}
        _last_maintenance = now
    result: dict[str, Any] = {"skipped": False}
    try:
        from ..common import mailer
        result["mail_sent"] = mailer.flush(limit=20)
    except Exception as exc:      # noqa: BLE001 - 维护失败只记日志
        result["mail_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from ..common import logging as sklogging
        result["logs_pruned"] = sklogging.prune(days=int(getattr(settings, "log_keep_days", 30) or 30))
    except Exception as exc:      # noqa: BLE001
        result["prune_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from ..automation import session_store
        result["sessions"] = session_store.cleanup_sessions(settings)
    except Exception as exc:      # noqa: BLE001
        result["session_error"] = f"{type(exc).__name__}: {exc}"
    return result


# --------------------------------------------------------------------------
# tick
# --------------------------------------------------------------------------
def tick(settings: Any, *, now: Any = None) -> dict:
    """跑一轮调度（单测直接调它，不用起线程）。"""
    started = time.monotonic()
    now = now or timeutil.now_utc()
    stats: dict[str, Any] = {"due": 0, "dispatched": 0, "skipped": 0, "init": 0,
                             "busy": 0, "retried": 0, "recovered": 0, "errors": []}

    stats["recovered"] = runner.recover_stale_runs(settings)

    tasks = due_tasks(settings, now=now)
    for task in tasks:
        effective = effective_due(task)
        if effective is not None and effective > now:
            continue                     # 抖动还没到，下个 tick 再看
        stats["due"] += 1
        try:
            result = handle_due_task(task, settings, now=now)
        except Exception as exc:         # noqa: BLE001 - 单个任务出错不能中断整轮
            stats["errors"].append(f"task {task.get('id')}: {type(exc).__name__}: {exc}")
            sklog.error(f"处理到期任务 {task.get('id')} 失败：{type(exc).__name__}: {exc}",
                        scope="worker", source="task.scheduler")
            continue
        action = result.get("action")
        if action == "dispatched":
            stats["dispatched"] += 1
        elif action == "init":
            stats["init"] += 1
        elif action == "busy":
            stats["busy"] += 1
        else:
            stats["skipped"] += 1

    for task in blocked_tasks(now=now):
        _log_blocked(task, now)

    for task in retry_candidates(settings, now=now):
        try:
            result = handle_retry(task, settings)
            if result.get("action") == "retried":
                stats["retried"] += 1
        except Exception as exc:         # noqa: BLE001
            stats["errors"].append(f"retry {task.get('id')}: {type(exc).__name__}: {exc}")

    stats["duration_ms"] = int((time.monotonic() - started) * 1000)
    heartbeat(settings, status="running" if stats["dispatched"] else "idle",
              detail={"due": stats["due"], "dispatched": stats["dispatched"],
                      "queue": runner.queue_snapshot()})
    maintenance(settings)
    return stats


def _log_blocked(task: dict, now: Any) -> None:
    """账号不可用的任务：限流记一条 info（不推进 next_run_at，保留补跑机会）。"""
    task_id = int(task.get("id") or 0)
    with _state_lock:
        last = _reason_logged.get(task_id, 0.0)
        if now is not None:
            current = time.monotonic()
        else:                                             # pragma: no cover
            current = time.monotonic()
        if current - last < REASON_LOG_INTERVAL:
            return
        _reason_logged[task_id] = current
    reason = "账号已暂停托管" if not task.get("account_enabled") else \
        f"账号登录态为 {task.get('account_status')}"
    sklog.info(f"任务 {task_id} 到点但未执行：{reason}（恢复后会自动补跑）",
               scope="worker", source="task.scheduler", user_id=task.get("user_id"))


# --------------------------------------------------------------------------
# 线程入口
# --------------------------------------------------------------------------
def run_forever(settings: Any) -> None:
    """阻塞式调度循环（``main.py worker`` 用）。"""
    # 至少 1 秒：0/负数会变成忙轮询把 CPU 打满；同理由这里不用 `or 20`。
    try:
        interval = max(1.0, float(getattr(settings, "scheduler_tick_seconds", 20)))
    except (TypeError, ValueError):
        interval = 20.0
    sklog.info(f"调度器启动：每 {interval:.0f}s 一次 tick，worker_id={settings.worker_id}",
               scope="worker", source="task.scheduler")
    try:
        while True:
            try:
                stats = tick(settings)
                if stats.get("dispatched") or stats.get("retried") or stats.get("errors"):
                    sklog.debug(f"tick 结束：{stats}", scope="worker", source="task.scheduler")
            except KeyboardInterrupt:
                raise
            except Exception as exc:      # noqa: BLE001 - 调度循环必须活着
                sklog.error(f"tick 异常：{type(exc).__name__}: {exc}",
                            scope="worker", source="task.scheduler")
            time.sleep(interval)
    except KeyboardInterrupt:
        sklog.info("调度器已停止（KeyboardInterrupt）", scope="worker", source="task.scheduler")


def start_background(settings: Any) -> threading.Thread | None:
    """起一个 daemon 线程跑调度器（``main.py serve`` 用）。"""
    if not bool(getattr(settings, "scheduler_enabled", True)):
        return None
    thread = threading.Thread(target=run_forever, args=(settings,),
                              name="sk-scheduler", daemon=True)
    thread.start()
    return thread


def reset_state() -> None:
    """测试用：清掉限流日志与维护计时。"""
    global _last_maintenance
    with _state_lock:
        _reason_logged.clear()
        _last_maintenance = 0.0
