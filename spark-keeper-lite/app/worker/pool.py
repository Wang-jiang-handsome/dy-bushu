"""浏览器并发闸门：限制同时运行的 Chrome 实例数。

为什么需要
----------
一个 Chrome 实例（含渲染进程）在 1C1G 的云主机上就能吃掉 300MB+。自托管场景
必须限制并发，否则「3 个账号同时到点」会把机器打爆，表现为所有任务一起超时。

设计
----
* ``threading.BoundedSemaphore`` 做真正的互斥（``release`` 多调一次会抛错，
  能立刻暴露「重复释放」这种会悄悄放大并发额度的 bug）。
* 一张**活跃登记表**（key → 进程信息）给管理端 ``/admin/browser-processes``
  与「清理残留进程」用。表里**只放进程元信息**，绝不放 cookies/登录态路径。
* 闸门是**进程内**的。多进程部署（``serve`` + 独立 ``worker`` 容器）时，
  各自的闸门独立计数，此时靠 ``settings.max_concurrent_browsers`` 按进程规划；
  自托管单机场景下通常只有一个 worker 进程，不构成问题（已在文档里注明）。

可测性
------
纯 threading，无浏览器依赖，本机可完整单测（含并发上限、超时、重复释放）。
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any


class PoolStateError(RuntimeError):
    """闸门使用错误（重复释放 / 释放未持有的 key）。"""


class BrowserPool:
    """并发闸门 + 活跃进程登记表。"""

    def __init__(self, limit: int = 2, *, name: str = "browser") -> None:
        self.name = name
        self.limit = max(1, int(limit or 1))
        self._sem = threading.BoundedSemaphore(self.limit)
        self._active: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.acquired_total = 0
        self.released_total = 0
        self.rejected_total = 0

    # ---- 闸门 ----
    def acquire(self, key: str, *, kind: str = "task", timeout: float = 0.0,
                account_id: int | None = None) -> bool:
        """尝试占用一个额度。

        ``timeout=0`` 表示「拿不到立刻返回 False」（不阻塞调用线程）；
        ``timeout>0`` 会等最多这么久（立即执行/登录这类用户可感知的操作更适合排队等待）。
        同一 key 重复占用会直接拒绝：它意味着上层逻辑有问题（同一账号并发执行），
        放行会真的把两个浏览器指向同一个 profile 目录。
        """
        key = str(key)
        with self._lock:
            if key in self._active:
                self.rejected_total += 1
                return False
        ok = self._sem.acquire(timeout=max(0.0, float(timeout))) if timeout > 0 \
            else self._sem.acquire(blocking=False)
        if not ok:
            self.rejected_total += 1
            return False
        with self._lock:
            self._active[key] = {
                "key": key, "kind": kind, "account_id": account_id,
                "acquired_at": time.time(), "pid": 0, "port": 0,
                "profile": "", "browser": "", "profile_dir": "",
            }
            self.acquired_total += 1
        return True

    def register(self, key: str, **info: Any) -> None:
        """补充登记进程信息（拿到 pid 之后调用）。"""
        with self._lock:
            item = self._active.get(str(key))
            if item is None:
                return
            item.update({k: v for k, v in info.items() if v is not None})

    def release(self, key: str) -> bool:
        """释放额度。未持有的 key 返回 ``False``（不抛错，因为异常路径里会重复调用）。"""
        key = str(key)
        with self._lock:
            item = self._active.pop(key, None)
        if item is None:
            return False
        try:
            self._sem.release()
        except ValueError:        # pragma: no cover - BoundedSemaphore 超发才会到这里
            raise PoolStateError(f"并发闸门被重复释放：{key}") from None
        self.released_total += 1
        return True

    # ---- 快照 / 清理 ----
    def snapshot(self) -> dict:
        """管理端 ``GET /admin/browser-processes`` 的数据源。

        **只含进程元信息**：pid/端口/类型/占用时长/账号 id/profile 文件名。
        没有 cookies、没有登录态内容、没有绝对路径。
        """
        now = time.time()
        with self._lock:
            items = []
            for item in self._active.values():
                age = max(0.0, now - float(item.get("acquired_at") or now))
                items.append({
                    "key": item.get("key", ""),
                    "kind": item.get("kind", ""),
                    "account_id": item.get("account_id"),
                    "pid": int(item.get("pid") or 0),
                    "port": int(item.get("port") or 0),
                    "profile": item.get("profile", ""),
                    "browser": item.get("browser", ""),
                    "age_seconds": int(age),
                    "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(float(item.get("acquired_at") or now))),
                })
        in_use = len(items)
        return {
            "limit": self.limit,
            "in_use": in_use,
            "available": max(0, self.limit - in_use),
            "waiting": 0,
            "items": sorted(items, key=lambda x: -x["age_seconds"]),
            "acquired_total": self.acquired_total,
            "released_total": self.released_total,
            "rejected_total": self.rejected_total,
            "name": self.name,
        }

    def cleanup(self, *, force: bool = False, max_age_seconds: int = 0) -> dict:
        """清理登记表里「进程已经不存在」的残留占用。

        ``max_age_seconds>0`` 时连「占用超过 N 秒」的也一起清（应对浏览器卡死）。
        ``force=True`` 会尝试真的结束进程（``os.kill``；Windows 上等价于强杀）。
        返回 ``{checked, alive, stale, killed, released}``——给管理端点「清理」用。
        """
        now = time.time()
        checked = alive = stale = killed = released = 0
        with self._lock:
            keys = list(self._active.keys())
        for key in keys:
            with self._lock:
                item = self._active.get(key)
            if item is None:
                continue
            checked += 1
            pid = int(item.get("pid") or 0)
            age = now - float(item.get("acquired_at") or now)
            is_alive = _pid_alive(pid) if pid else True
            too_old = bool(max_age_seconds) and age > max_age_seconds
            if is_alive and not too_old:
                alive += 1
                continue
            stale += 1
            if force and pid and is_alive and _kill_pid(pid):
                killed += 1
            if self.release(key):
                released += 1
        return {"checked": checked, "alive": alive, "stale": stale,
                "killed": killed, "released": released}

    def set_limit(self, limit: int) -> bool:
        """运行时改并发上限。

        只允许**调大**：调小需要重建信号量，而已发出的额度无法收回，贸然重建会
        让在跑的浏览器脱离闸门控制（更危险）。调小请改配置后重启 worker。
        """
        limit = max(1, int(limit or 1))
        with self._lock:
            if limit <= self.limit:
                return False
            extra = limit - self.limit
            for _ in range(extra):
                self._sem.release()
            self.limit = limit
            return True

    def entry(self, key: str) -> dict | None:
        """取某个 key 的登记信息（副本；没有返回 ``None``）。"""
        with self._lock:
            item = self._active.get(str(key))
            return dict(item) if item else None

    def force_close(self, key: str, *, kinds: tuple[str, ...] | None = None) -> dict:
        """强制关掉某个 key 的浏览器并释放额度（「并发已满」的自愈手段）。

        ``kinds`` 限定**允许强关**的浏览器类型：登录/校验用的浏览器随时可以杀
        （它们只是等人扫码），但**任务**浏览器绝不能杀——那会把正在发送的任务
        打断。调用方按场景传：登录流程传 ``("login", "verify")``。

        返回 ``{"closed","killed","released","kind","reason"}``；杀不掉也会释放
        额度（进程已经死掉但登记表残留时，这一步正好把额度收回来）。
        """
        key = str(key)
        entry = self.entry(key)
        if not entry:
            return {"closed": False, "killed": False, "released": False,
                    "kind": "", "reason": "not_registered"}
        kind = str(entry.get("kind") or "")
        if kinds is not None and kind not in kinds:
            return {"closed": False, "killed": False, "released": False,
                    "kind": kind, "reason": f"kind={kind or 'unknown'} 不在允许强关范围"}
        pid = int(entry.get("pid") or 0)
        killed = bool(_kill_pid(pid)) if pid and _pid_alive(pid) else False
        released = self.release(key)
        return {"closed": bool(killed or released), "killed": killed,
                "released": released, "kind": kind, "reason": ""}

    def reset(self) -> None:
        """测试用：清空登记表并重建信号量。"""
        with self._lock:
            self._active.clear()
            self._sem = threading.BoundedSemaphore(self.limit)


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（不引 psutil）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:      # noqa: BLE001 - 拿不到就按「活着」处理（宁可少清理）
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _kill_pid(pid: int) -> bool:
    """尽力结束进程（失败返回 False，不抛）。"""
    try:
        if os.name != "nt":
            os.kill(int(pid), signal.SIGTERM)
        else:
            os.kill(int(pid), signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# 进程内单例
# --------------------------------------------------------------------------
_pools: dict[int, BrowserPool] = {}
_pool_lock = threading.Lock()


def get_pool(settings: Any = None) -> BrowserPool:
    """按并发上限取进程内共享的闸门。

    为什么按 limit 缓存而不是全局一个：``max_concurrent_browsers`` 可以在
    配置里调，测试也会用不同上限；但同一个 limit 必须共用同一个信号量，
    否则两处各建一个闸门，实际并发就是 2 倍。
    """
    limit = int(getattr(settings, "max_concurrent_browsers", 2) or 2)
    with _pool_lock:
        pool = _pools.get(limit)
        if pool is None:
            pool = BrowserPool(limit)
            _pools[limit] = pool
        return pool


def snapshot() -> dict:
    """模块级快捷方式：管理端 ``app.worker.pool.snapshot()``（契约签名）。"""
    with _pool_lock:
        pools = list(_pools.values())
    if not pools:
        # 还没有人拿过闸门：返回一个「按当前配置」的空快照，前端不会 500
        from ..config import get_settings
        return get_pool(get_settings()).snapshot()
    primary = pools[0]
    merged = primary.snapshot()
    if len(pools) > 1:
        # 多上限并存（只在配置热改或测试里出现）：合并展示，别让管理端只看到一半
        for extra in pools[1:]:
            item = extra.snapshot()
            merged["limit"] += item["limit"]
            merged["in_use"] += item["in_use"]
            merged["available"] += item["available"]
            merged["items"] = merged["items"] + item["items"]
    return merged


def cleanup(force: bool = False, max_age_seconds: int = 0) -> dict:
    """模块级快捷方式：清理所有闸门里的残留占用。"""
    with _pool_lock:
        pools = list(_pools.values())
    total = {"checked": 0, "alive": 0, "stale": 0, "killed": 0, "released": 0}
    for pool in pools:
        result = pool.cleanup(force=force, max_age_seconds=max_age_seconds)
        for key in total:
            total[key] += int(result.get(key) or 0)
    return total


def reset_pools() -> None:
    """测试用：丢弃所有闸门。"""
    with _pool_lock:
        _pools.clear()
