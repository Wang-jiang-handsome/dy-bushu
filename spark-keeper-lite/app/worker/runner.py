"""任务执行队列：把「谁该跑了」变成「真正去跑」，且同一账号不并发。

为什么要有队列
--------------
调度器是**单线程 tick**：如果 tick 里直接同步跑任务，一次任务 3 分钟就会把
整轮调度卡住（其它到点任务全部延后）。所以 tick 只负责「入队」，由**后台工作
线程**消费队列去跑浏览器。

账号级互斥放在数据库（``task_runs`` 里 status='running' 的行）而不是内存里：
``serve`` 与独立 ``worker`` 容器同时跑时，内存锁挡不住跨进程并发，而两个浏览器
指向同一个 profile 目录会互相踩坏登录态。

对外契约（队友 A / CLI 都在用）
-------------------------------
* ``enqueue_run(account_id, task_id, trigger_type, dry_run=False) -> dict``
  立即返回 ``{"run_id": int, "status": "queued"|"running"|"rejected", "error", "message"}``
* ``run_task_now(account_id, trigger_type, dry_run) -> dict`` 同步执行，返回执行结果
* ``start_workers(settings)`` / ``stop_workers()`` / ``queue_snapshot()``
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable

from .. import db
from ..common import logging as sklog
from ..common import timeutil

#: 工作线程取队列的等待时间（用于及时响应 stop 信号）。
#: 必须**远小于** stop_workers 的 join 预算：如果线程正阻塞在 ``queue.get(timeout)``，
#: 它要等这次 get 超时才会看到停止标志。早期版本用 1.0s，而 stop_workers 只 join 1.0s，
#: 结果「停下来之后 snapshot 仍看到 2 个 worker」——单测表现为间歇性失败，
#: 生产表现为容器重启时 worker 还抱着任务不放。
POLL_SECONDS = 0.2
#: 超过「任务超时 ×2 + 60 秒」还停在 running 的行，视为进程崩溃残留
STALE_GRACE_SECONDS = 60

_queue: "queue.Queue[tuple]" = queue.Queue()
_workers: list[threading.Thread] = []
_running_accounts: set[int] = set()
_stats = {"enqueued": 0, "started": 0, "finished": 0, "failed": 0}
_stop = threading.Event()
_lock = threading.RLock()
#: 测试注入点：替换掉真正的浏览器执行函数
_executor: Callable[..., dict] | None = None


class TaskBusy(RuntimeError):
    """同一账号已有任务在执行。"""


# --------------------------------------------------------------------------
# 账号 / 任务解析
# --------------------------------------------------------------------------
def load_account(account_id: int) -> dict | None:
    return db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (int(account_id),))


def resolve_task(account_id: int, task_id: int | None = None) -> dict | None:
    """找要执行的任务。

    ``task_id`` 给了就用它（即使它被停用 —— 用户点「立即执行」是明确的意图）；
    没给就取该账号**第一条启用中的**任务（CLI ``main.py task`` 的用法）。
    """
    if task_id:
        return db.query_one("SELECT * FROM scheduled_tasks WHERE id=? AND account_id=?",
                            (int(task_id), int(account_id)))
    return db.query_one(
        "SELECT * FROM scheduled_tasks WHERE account_id=? AND enabled=1 ORDER BY id LIMIT 1",
        (int(account_id),),
    )


def stale_seconds(settings: Any) -> int:
    timeout = int(getattr(settings, "task_timeout_seconds", 180) or 180)
    return max(60, timeout * 2 + STALE_GRACE_SECONDS)


def running_run(account_id: int, settings: Any = None, *, recover: bool = True) -> dict | None:
    """该账号当前是否有正在执行的 run。发现**残留**（进程崩过）就顺手标记失败。"""
    row = db.query_one(
        "SELECT * FROM task_runs WHERE account_id=? AND status='running' "
        "ORDER BY id DESC LIMIT 1",
        (int(account_id),),
    )
    if not row:
        return None
    if settings is None or not recover:
        return row
    started = timeutil.parse_db(row.get("started_at"))
    if started and (timeutil.now_utc() - started).total_seconds() > stale_seconds(settings):
        mark_run_failed(int(row["id"]), "timeout", "执行超时（worker 可能重启过），已自动标记失败")
        return None
    return row


def mark_run_failed(run_id: int, error_code: str, message: str) -> None:
    """把一条 run 标成失败（超时回收用）。"""
    now = timeutil.utc_now_str()
    try:
        db.update("task_runs", {
            "status": "failed", "error_code": error_code[:50], "message": message[:500],
            "finished_at": now,
        }, "id=?", (int(run_id),))
    except Exception as exc:      # noqa: BLE001 - 回收失败不能把调度器带崩
        sklog.warning(f"标记 run={run_id} 失败时出错：{exc}", scope="worker",
                      source="task.runner")


def recover_stale_runs(settings: Any) -> int:
    """回收所有残留的 running 行（调度器每个 tick 调一次）。"""
    rows = db.query_all(
        "SELECT id, account_id, started_at FROM task_runs WHERE status='running'"
    )
    now = timeutil.now_utc()
    limit = stale_seconds(settings)
    recovered = 0
    for row in rows:
        started = timeutil.parse_db(row.get("started_at"))
        if started is None:
            continue
        if (now - started).total_seconds() <= limit:
            continue
        mark_run_failed(int(row["id"]), "timeout",
                        f"执行超过 {limit}s 未结束（worker 重启或浏览器卡死），已自动标记失败")
        recovered += 1
        sklog.warning(f"回收残留执行记录 run={row['id']} account={row['account_id']}",
                      scope="worker", source="task.scheduler")
    return recovered


# --------------------------------------------------------------------------
# 入队 / 同步执行
# --------------------------------------------------------------------------
def enqueue_run(account_id: int, task_id: int | None, trigger_type: str,
                dry_run: bool = False, *, autostart: bool | None = None,
                settings: Any = None) -> dict:
    """把一次执行放进队列，**立即返回**。

    返回 ``{"run_id": int, "status": "queued"|"running"|"rejected", "error": str,
    "message": str}``。``run_id`` 一定是 int（失败时为 0），队友 A 可直接透传。
    """
    if settings is None:
        from ..config import get_settings
        settings = get_settings()
    account_id = int(account_id or 0)
    account = load_account(account_id)
    if not account:
        return {"run_id": 0, "status": "rejected", "error": "not_found",
                "message": "抖音账号不存在", "account_id": account_id, "task_id": None}
    task = resolve_task(account_id, int(task_id) if task_id else None)
    if not task:
        return {"run_id": 0, "status": "rejected", "error": "no_task",
                "message": "该账号没有可执行的定时任务", "account_id": account_id,
                "task_id": None}
    busy = running_run(account_id, settings, recover=True)
    if busy:
        return {"run_id": int(busy["id"]), "status": "rejected", "error": "task_running",
                "message": "该账号已有任务在执行中", "account_id": account_id,
                "task_id": int(task["id"])}

    from ..automation import task_runner      # 局部导入：避免与 automation 循环依赖
    run_id = task_runner.create_run(task, account, str(trigger_type or "manual"))
    _queue.put((run_id, int(task["id"]), account_id, str(trigger_type or "manual"),
                bool(dry_run)))
    with _lock:
        _stats["enqueued"] += 1
    should_start = autostart
    if should_start is None:
        should_start = bool(getattr(settings, "worker_autostart", True))
    if should_start:
        start_workers(settings)
    sklog.info(f"任务入队：run={run_id} account={account_id} task={task['id']} "
               f"trigger={trigger_type} dry_run={dry_run}",
               scope="worker", source="task.runner", user_id=task.get("user_id"),
               context={"run_id": run_id, "account_id": account_id})
    return {"run_id": run_id, "status": "queued", "error": "", "message": "",
            "account_id": account_id, "task_id": int(task["id"])}


def run_task_now(account_id: int, trigger_type: str = "manual",
                 dry_run: bool = False, *, settings: Any = None) -> dict:
    """同步执行一次（CLI ``main.py task`` 与「立即执行」的兜底路径）。

    返回的就是 ``execute_task`` 的结果 dict（status/success_count/fail_count/message）。
    """
    if settings is None:
        from ..config import get_settings
        settings = get_settings()
    account_id = int(account_id or 0)
    account = load_account(account_id)
    if not account:
        return {"run_id": 0, "status": "failed", "error_code": "not_found",
                "success_count": 0, "fail_count": 0, "message": "抖音账号不存在",
                "total_count": 0, "skipped_count": 0, "items": []}
    task = resolve_task(account_id, None)
    if not task:
        return {"run_id": 0, "status": "skipped", "error_code": "no_task",
                "success_count": 0, "fail_count": 0, "message": "该账号没有可执行的定时任务",
                "total_count": 0, "skipped_count": 0, "items": []}
    busy = running_run(account_id, settings, recover=True)
    if busy:
        return {"run_id": int(busy["id"]), "status": "skipped", "error_code": "task_running",
                "success_count": 0, "fail_count": 0,
                "message": "该账号已有任务在执行中，请稍后再试",
                "total_count": 0, "skipped_count": 0, "items": []}

    from ..automation import task_runner
    run_id = task_runner.create_run(task, account, str(trigger_type or "manual"))
    return _execute_item((run_id, int(task["id"]), account_id,
                          str(trigger_type or "manual"), bool(dry_run)), settings)


def _execute_item(item: tuple, settings: Any) -> dict:
    """执行队列里的一项（工作线程与 run_task_now 共用）。"""
    run_id, task_id, account_id, trigger_type, dry_run = item
    with _lock:
        _running_accounts.add(int(account_id))
        _stats["started"] += 1
    from ..automation import task_runner
    outcome: dict = {}
    try:
        task = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (int(task_id),))
        account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (int(account_id),))
        if not task or not account:
            mark_run_failed(run_id, "deleted",
                            "任务或账号已被删除，本次执行已取消")
            with _lock:
                _stats["failed"] += 1
            return {"run_id": run_id, "status": "failed", "error_code": "deleted",
                    "success_count": 0, "fail_count": 0, "total_count": 0,
                    "skipped_count": 0, "message": "任务或账号已被删除", "items": []}
        if _executor is not None:
            outcome = _executor(task, account, settings, dry_run, run_id=run_id,
                                trigger_type=trigger_type) or {}
        else:
            outcome = task_runner.execute_task(task, account, settings, dry_run,
                                              run_id=run_id, trigger_type=trigger_type)
    except Exception as exc:      # noqa: BLE001 - 工作线程绝不能因为一次异常退出
        mark_run_failed(run_id, "unexpected", f"未预期错误：{type(exc).__name__}: {exc}")
        # 真实浏览器里的异常几乎无法复现，堆栈必须记下来（/admin/logs 的 context 字段）
        sklog.error(f"执行 run={run_id} 时未预期异常：{type(exc).__name__}: {exc}",
                    scope="worker", source="task.runner",
                    context={"run_id": run_id, "account_id": account_id,
                             "task_id": task_id,
                             "traceback": traceback.format_exc()[-2000:]})
        with _lock:
            _stats["failed"] += 1
        outcome = {"run_id": run_id, "status": "failed", "error_code": "unexpected",
                   "success_count": 0, "fail_count": 0, "total_count": 0,
                   "skipped_count": 0, "message": str(exc), "items": []}
    else:
        with _lock:
            if str(outcome.get("status")) == "failed":
                _stats["failed"] += 1
    finally:
        with _lock:
            _running_accounts.discard(int(account_id))
            _stats["finished"] += 1
    outcome.setdefault("run_id", run_id)
    _reflect_outcome(outcome, task_id, trigger_type, dry_run)
    return outcome


def _reflect_outcome(outcome: dict, task_id: int, trigger_type: str,
                     dry_run: bool) -> None:
    """把执行结果回填到 ``scheduled_tasks``（幂等；task_runner 也写过一次）。

    为什么 runner 还要再写一次：契约要求「任何触发方式执行结束后
    ``last_run_at/last_status`` 都要反映最新结果」，而执行器可能是注入的假实现
    （测试）或别的实现；在这里兜一层，契约就不依赖执行器的内部行为。
    """
    try:
        from ..automation import task_runner
        task = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (int(task_id),))
        if not task:
            return
        task_runner.backfill_task_schedule(task, str(outcome.get("status") or ""),
                                           trigger_type=str(trigger_type),
                                           dry_run=bool(dry_run))
    except Exception as exc:      # noqa: BLE001 - 缓存列回填失败不能影响执行结果
        sklog.debug(f"回填任务 {task_id} 调度缓存失败：{exc}",
                    scope="worker", source="task.runner")


# --------------------------------------------------------------------------
# 工作线程
# --------------------------------------------------------------------------
def start_workers(settings: Any, size: int | None = None) -> int:
    """启动后台工作线程（幂等）。

    线程数默认等于 ``max_concurrent_browsers``：再多也没用，真正的并发上限由
    浏览器闸门（``worker/pool.py``）控制，多出来的线程只会排队等闸门。
    """
    if size is None:
        size = max(1, int(getattr(settings, "max_concurrent_browsers", 2) or 2))
    with _lock:
        alive = [thread for thread in _workers if thread.is_alive()]
        _workers[:] = alive
        need = max(0, int(size) - len(alive))
        _stop.clear()
        for index in range(need):
            thread = threading.Thread(target=_worker_loop, args=(settings,),
                                      name=f"sk-worker-{len(alive) + index + 1}", daemon=True)
            thread.start()
            _workers.append(thread)
        return len(_workers)


def stop_workers(timeout: float = 5.0) -> int:
    """停止工作线程，返回**仍在运行的线程数**（0 = 全部停下）。

    实现要点：所有线程共用一个 deadline，而不是把 timeout 平分给每个线程 ——
    平分会让每个线程只分到 ``timeout/N``，第一个还没退出就到点了（早期版本的
    间歇性 bug 就出在这里）。线程内部用 0.2s 的短轮询取队列，所以停止信号
    最迟 ``POLL_SECONDS`` 之后生效。

    返回值让调用方（与测试）可以不靠 sleep 就判断「真的停了没有」。
    """
    _stop.set()
    with _lock:
        threads = list(_workers)
    deadline = time.monotonic() + max(0.1, float(timeout))
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)
    with _lock:
        _workers[:] = [thread for thread in _workers if thread.is_alive()]
        return len(_workers)


def _worker_loop(settings: Any) -> None:                        # pragma: no cover - 线程体
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=POLL_SECONDS)
        except queue.Empty:
            continue
        try:
            _execute_item(item, settings)
        finally:
            try:
                _queue.task_done()
            except ValueError:
                pass


def queue_snapshot() -> dict:
    """队列与线程状态（管理端与测试用）。"""
    with _lock:
        workers = [t for t in _workers if t.is_alive()]
        return {
            "queued": _queue.qsize(),
            "workers": len(workers),
            "worker_names": [t.name for t in workers],
            "running_accounts": sorted(_running_accounts),
            "stats": dict(_stats),
        }


def drain_queue(*, timeout: float = 30.0) -> bool:
    """等队列跑空（测试与优雅停机用）。返回是否在超时前清空。"""
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if _queue.empty() and not _running_accounts:
            return True
        time.sleep(0.05)
    return _queue.empty() and not _running_accounts


def set_executor(executor: Callable[..., dict] | None) -> None:
    """测试注入：替换真正的浏览器执行函数（本机不能起子进程）。

    生产代码**不要**调用它。
    """
    global _executor
    with _lock:
        _executor = executor


def reset_state() -> None:
    """测试用：清空队列与统计（不碰数据库）。"""
    global _executor
    _executor = None
    _stop.clear()
    with _lock:
        while not _queue.empty():
            try:
                _queue.get_nowait()
                _queue.task_done()
            except queue.Empty:
                break
        _workers.clear()
        _running_accounts.clear()
        _stats.update({"enqueued": 0, "started": 0, "finished": 0, "failed": 0})
