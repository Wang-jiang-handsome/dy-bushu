"""日志：同时写控制台与数据库 ``run_logs``（页面 /app/logs、/admin/logs 的数据源）。

刻意做成「写库失败不影响业务」：日志里的异常不能反过来把请求打挂。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

from .. import db
from . import timeutil

_local = threading.local()
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_console_min = 20


def set_console_level(level: str) -> None:
    global _console_min
    _console_min = _LEVELS.get((level or "info").lower(), 20)


def _context() -> dict:
    ctx = getattr(_local, "context", None)
    if ctx is None:
        ctx = {}
        _local.context = ctx
    return ctx


class log_context:
    """with log_context(user_id=1, source='task.scheduler'): ...

    块内所有 ``log()`` 调用自动带上这些字段，省得每行都重复写。
    """

    def __init__(self, **fields: Any):
        self.fields = fields
        self.previous: dict | None = None

    def __enter__(self) -> "log_context":
        self.previous = dict(_context())
        ctx = _context()
        ctx.update({k: v for k, v in self.fields.items() if v is not None})
        return self

    def __exit__(self, *exc_info) -> None:
        ctx = _context()
        ctx.clear()
        if self.previous:
            ctx.update(self.previous)


def log(message: str, level: str = "info", scope: str = "system", user_id: int | None = None,
        source: str = "", context: dict | None = None, persist: bool = True) -> None:
    level = (level or "info").lower()
    if level not in _LEVELS:
        level = "info"
    ctx = dict(_context())
    if context:
        ctx.update(context)
    if user_id is None:
        user_id = ctx.get("user_id")
    if not source:
        source = str(ctx.get("source") or "")

    if _LEVELS[level] >= _console_min:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        stream = sys.stderr if level in ("warning", "error") else sys.stdout
        tag = f"[{source}] " if source else ""
        print(f"{stamp} {level.upper():<7} {tag}{message}", file=stream, flush=True)

    if not persist:
        return
    try:
        db.insert(
            "run_logs",
            {
                "scope": scope if scope in ("user", "admin", "system", "worker") else "system",
                "level": level,
                "user_id": user_id or None,
                "source": source[:80],
                "message": str(message)[:2000],
                "context": json.dumps(ctx, ensure_ascii=False, default=str)[:4000],
                "created_at": timeutil.utc_now_str(),
            },
        )
    except Exception:      # noqa: BLE001 - 日志写不进去也绝不能影响主流程
        pass


def debug(message: str, **kwargs: Any) -> None:
    log(message, "debug", **kwargs)


def info(message: str, **kwargs: Any) -> None:
    log(message, "info", **kwargs)


def warning(message: str, **kwargs: Any) -> None:
    log(message, "warning", **kwargs)


def error(message: str, **kwargs: Any) -> None:
    log(message, "error", **kwargs)


def audit(action: str, *, scope: str = "user", user: dict | None = None, target: str = "",
          detail: str = "", ip: str = "", user_agent: str = "") -> None:
    """写审计表（管理员操作、关键用户操作）。"""
    try:
        db.insert(
            "audit_logs",
            {
                "user_id": (user or {}).get("id"),
                "username": (user or {}).get("username", ""),
                "scope": scope,
                "action": action,
                "target": target[:200],
                "detail": detail[:2000],
                "ip": ip,
                "user_agent": user_agent[:300],
                "created_at": timeutil.utc_now_str(),
            },
        )
    except Exception:      # noqa: BLE001
        pass


def notify(user_id: int, title: str, content: str = "", level: str = "info") -> None:
    """站内通知。"""
    try:
        db.insert(
            "notifications",
            {
                "user_id": user_id,
                "title": title[:200],
                "content": content[:2000],
                "level": level if level in ("info", "warning", "important") else "info",
                "created_at": timeutil.utc_now_str(),
            },
        )
    except Exception:      # noqa: BLE001
        pass


def prune(days: int = 30) -> int:
    """清理过期日志（调度器每天调用一次）。"""
    cutoff = timeutil.to_db(timeutil.add_days(-abs(int(days))))
    removed = 0
    try:
        removed += db.delete("run_logs", "created_at < ?", (cutoff,))
        removed += db.delete("message_outbox", "status='sent' AND created_at < ?", (cutoff,))
        removed += db.delete("verify_codes", "expire_at < ?", (cutoff,))
        removed += db.delete("sessions", "expire_at < ?", (cutoff,))
    except Exception:      # noqa: BLE001
        pass
    return removed
