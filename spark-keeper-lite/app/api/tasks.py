"""定时任务与好友接口（契约 §5）：``/tasks*``、``/friends*``、发送台账。

职责边界
--------
* 任务的增删改查、字段校验、``next_run_at`` 计算、好友 CRUD、批量导入、发送台账。
* 「立即执行一次」只做**校验 + 入队**，真正跑任务委托
  ``app.worker.runner.enqueue_run()``（队友 D，延迟 import，缺失时 503）。
* 好友同步委托 ``app.automation.douyin.sync_friends()``（同样延迟 import）。

坑（改代码前必读）
------------------
1. ``weekdays`` / ``friend_ids`` / ``messages_json`` / ``send_gap_seconds`` 在库里都是
   TEXT（SQLite 无 JSON 类型），读写一律走 ``textutil`` 的容错函数，别手写 json.loads
   ——历史数据里可能有空串或半截 JSON。
2. ``PUT /tasks/<id>`` 是**整字段覆盖**（契约 §0：整体更新用 PUT）：请求体里没带的
   字段会回落成默认值，而不是保留旧值。前端编辑表单要提交完整对象。这里刻意不
   做「只改传了的字段」，否则 PUT/PATCH 语义就混了。
3. ``next_run_at`` 必须由服务端算：停用时置 NULL，启用/改时间时用
   ``timeutil.next_run_time()`` 重算；``interval`` 模式的锚点是 ``last_run_at``。
4. ``/tasks/<id>/run`` 的拦截顺序：账号归属 → 账号可跑（暂停/离线会直接失败，
   省一次浏览器启动）→ 配额（402/429）→ 是否已有 running（409 task_running）
   → 入队。顺序反了会出现「提示要升级套餐，实际是账号离线」这种误导文案。
5. 好友昵称有唯一索引 ``(account_id, nickname)``：插入前先查重，并用
   ``textutil.normalize_name`` 比对，避免「小明」和「小明 」（尾随空格）重复入库。
"""

from __future__ import annotations

import sqlite3
import traceback
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import db
from ..bootstrap import get_config
from ..common import logging as logmod
from ..common import quota as quotamod
from ..common import textutil, timeutil, validate
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route
from .accounts import import_optional, require_account_id, run_records_page

TASK_TYPES = ("spark", "message", "check")
REPEAT_MODES = ("daily", "weekdays", "weekends", "custom", "interval")
#: 好友批量删除单次上限（前端「全选」也按这个数分批；防一次删太多把库锁久了）
MAX_BATCH_FRIEND_DELETE = 500
FRIEND_MODES = ("all", "selected")

_SORTABLE = {"id": "t.id", "created_at": "t.created_at", "updated_at": "t.updated_at",
             "next_run_at": "t.next_run_at", "last_run_at": "t.last_run_at",
             "name": "t.name", "enabled": "t.enabled"}

# ``scheduled_tasks.last_run_at / last_status`` 是「最近一次执行」的**缓存列**，
# 目前只有调度器（scheduled 触发）会回填，手动执行（``POST /tasks/<id>/run``）不会 ——
# 于是契约里的 last_run_at/last_status 会一直是空/旧值。这里在读侧补一层：拿该任务
# 最新一条 task_runs（真源）与缓存列比较，取更近的那个，避免前端「上次执行」列空白。
# 缓存列一旦被执行器回填，这里自然就以缓存列为准（更新的那一刻起两边一致）。
_TASK_SELECT = (
    "SELECT t.*, a.nickname AS account_nickname, a.status AS account_status,\n"
    "       a.enabled AS account_enabled,\n"
    "       (SELECT COUNT(*) FROM friends f WHERE f.account_id=t.account_id AND f.enabled=1)"
    "         AS account_friend_count,\n"
    "       (SELECT r.status FROM task_runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1)"
    "         AS latest_run_status,\n"
    "       (SELECT r.started_at FROM task_runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1)"
    "         AS latest_run_at\n"
    "  FROM scheduled_tasks t\n"
    "  LEFT JOIN douyin_accounts a ON a.id = t.account_id AND a.user_id = t.user_id"
)


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _order_by(req: Request, default: str = "-id") -> str:
    raw = req.str_field("sort", "", max_len=40).strip() or default
    desc = raw.startswith("-")
    column = _SORTABLE.get(raw.lstrip("+-"))
    if not column:
        desc = default.startswith("-")
        column = _SORTABLE[default.lstrip("+-")]
    return f"{column} {'DESC' if desc else 'ASC'}"


def _automation_call(module_name: str, func_name: str, code: str, *args, **kwargs) -> dict:
    """调用可选模块（自动化/执行器），缺失或运行失败都降级成可读的 503。"""
    module = import_optional(module_name)
    func = getattr(module, func_name, None) if module is not None else None
    if not callable(func):
        message = f"功能尚未就绪（{module_name}.{func_name} 不可用），请在部署环境使用"
        raise HttpError(503, message, code="server_error",
                        data={"status": "failed", "message": message})
    try:
        result = func(*args, **kwargs)
    except HttpError:
        raise
    except Exception as exc:      # noqa: BLE001 - 自动化层的失败是可预期状态，不该变 500
        message = f"执行失败（{module_name}.{func_name}）：{type(exc).__name__}: {exc}"
        logmod.error(message, source="api.tasks",
                     context={"traceback": traceback.format_exc()[-2000:]})
        raise HttpError(503, message, code="server_error",
                        data={"status": "failed", "message": message}) from exc
    return result if isinstance(result, dict) else {}


def _get_task(req: Request, task_id: str | int) -> dict:
    user = req.require_user()
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise HttpError(400, "任务 ID 不正确") from None
    row = db.query_one(f"{_TASK_SELECT} WHERE t.id=? AND t.user_id=?", (tid, user["id"]))
    if not row:
        raise HttpError(404, "定时任务不存在或不属于当前用户", code="not_found")
    return row


def _get_friend(req: Request, friend_id: str | int) -> dict:
    user = req.require_user()
    try:
        fid = int(friend_id)
    except (TypeError, ValueError):
        raise HttpError(400, "好友 ID 不正确") from None
    row = db.query_one(
        "SELECT f.*, a.nickname AS account_nickname FROM friends f"
        " LEFT JOIN douyin_accounts a ON a.id = f.account_id"
        " WHERE f.id=? AND f.user_id=?",
        (fid, user["id"]),
    )
    if not row:
        raise HttpError(404, "好友不存在或不属于当前用户", code="not_found")
    return row


# --------------------------------------------------------------------------
# 任务字段解析
# --------------------------------------------------------------------------
def _task_defaults() -> dict:
    settings = get_settings()
    return {
        "name": "自动续火花",
        "task_type": "spark",
        "enabled": True,
        "time_of_day": get_config("task.default_time", "08:30"),
        "repeat_mode": "daily",
        "weekdays": [],
        "interval_minutes": 1440,
        "jitter_seconds": 300,
        "timezone": get_config("task.default_timezone", settings.timezone),
        "friend_mode": "all",
        "friend_ids": [],
        "messages": [],
        "send_gap_seconds": {"min": 3, "max": 9},
        "daily_limit": 1,
        "max_retry": 2,
        "cron_expr": "",
    }


def _normalize_hhmm(raw: str, field: str = "time_of_day") -> str:
    text = (raw or "").strip().replace("：", ":")
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
    except (ValueError, IndexError):
        raise HttpError(400, f"{field} 格式应为 HH:MM（如 08:30）") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HttpError(400, f"{field} 不是合法时刻")
    return f"{hour:02d}:{minute:02d}"


def _validate_timezone(raw: str) -> str:
    name = (raw or "").strip() or get_config("task.default_timezone", get_settings().timezone)
    if len(name) > 64:
        raise HttpError(400, "timezone 过长")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        # Windows 上可能没有 tzdata：内置固定偏移兜底名单里的名字同样接受
        if name not in timeutil.FALLBACK_OFFSETS:
            raise HttpError(400, f"未知时区：{name}") from None
    return name


def _validate_friend_ids(user_id: int, account_id: int, ids: list[int]) -> list[int]:
    """校验 friend_ids 都属于该用户的该账号（防止把别人的好友塞进自己的任务）。"""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.query_all(
        f"SELECT id FROM friends WHERE user_id=? AND account_id=? AND id IN ({placeholders})",
        (user_id, account_id, *ids),
    )
    found = {int(r["id"]) for r in rows}
    missing = sorted(i for i in ids if i not in found)
    if missing:
        raise HttpError(400, f"好友不存在或不属于该账号：{missing}")
    return sorted(found)


def _collect_fields(req: Request, user_id: int, account_id: int, *, last_run_at: str = "") -> dict:
    """把请求体解析成 scheduled_tasks 的领域字段。

    请求体里没带的字段一律回落**默认值**（PUT 是整字段覆盖，见模块头坑 2）；
    所以这里不需要「保留旧值」的分支，前端提交完整对象即可。
    """
    fields = _task_defaults()
    body = req.json(required=False) or {}
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")

    fields["name"] = req.str_field("name", fields["name"], max_len=64) or fields["name"]
    task_type = req.str_field("task_type", fields["task_type"], max_len=16)
    if task_type not in TASK_TYPES:
        raise HttpError(400, f"task_type 只能是 {'/'.join(TASK_TYPES)}")
    fields["task_type"] = task_type
    fields["enabled"] = req.bool_field("enabled", fields["enabled"])
    fields["time_of_day"] = _normalize_hhmm(
        req.str_field("time_of_day", fields["time_of_day"], max_len=8))

    mode = req.str_field("repeat_mode", fields["repeat_mode"], max_len=16)
    if mode not in REPEAT_MODES:
        raise HttpError(400, f"repeat_mode 只能是 {'/'.join(REPEAT_MODES)}")
    fields["repeat_mode"] = mode

    raw_weekdays = req.field("weekdays", fields["weekdays"])
    weekdays = timeutil.parse_weekdays(raw_weekdays)
    if raw_weekdays not in (None, "", []) and not weekdays:
        raise HttpError(400, "weekdays 需为 1~7 的数组（1=周一）")
    fields["weekdays"] = weekdays

    fields["interval_minutes"] = req.int_field(
        "interval_minutes", fields["interval_minutes"], minimum=1, maximum=10080)
    fields["jitter_seconds"] = req.int_field(
        "jitter_seconds", fields["jitter_seconds"], minimum=0, maximum=3600)
    fields["timezone"] = _validate_timezone(req.str_field("timezone", fields["timezone"], max_len=64))

    friend_mode = req.str_field("friend_mode", fields["friend_mode"], max_len=16)
    if friend_mode not in FRIEND_MODES:
        raise HttpError(400, f"friend_mode 只能是 {'/'.join(FRIEND_MODES)}")
    fields["friend_mode"] = friend_mode
    fields["friend_ids"] = _validate_friend_ids(
        user_id, account_id, textutil.as_int_list(req.field("friend_ids", fields["friend_ids"])))

    messages = textutil.as_str_list(req.field("messages", fields["messages"]))
    if len(messages) > 50:
        raise HttpError(400, "候选文案最多 50 条")
    if any(len(m) > 200 for m in messages):
        raise HttpError(400, "单条文案最长 200 字")
    fields["messages"] = messages

    low, high = textutil.as_min_max(req.field("send_gap_seconds", fields["send_gap_seconds"]))
    if high > 600:
        raise HttpError(400, "发送间隔最长 600 秒")
    fields["send_gap_seconds"] = {"min": low, "max": high}

    fields["daily_limit"] = req.int_field("daily_limit", fields["daily_limit"], minimum=1, maximum=50)
    fields["max_retry"] = req.int_field("max_retry", fields["max_retry"], minimum=0, maximum=10)
    fields["cron_expr"] = req.str_field("cron_expr", fields["cron_expr"], max_len=120)

    if fields["repeat_mode"] == "custom" and not fields["weekdays"]:
        raise HttpError(400, "repeat_mode=custom 时必须提供 weekdays")
    if fields["friend_mode"] == "selected" and not fields["friend_ids"]:
        raise HttpError(400, "friend_mode=selected 时至少要选择 1 个好友")

    fields["last_run_at"] = last_run_at
    return fields


def _next_run_at(fields: dict) -> str | None:
    """停用 → NULL；否则按 time_of_day/repeat_mode 算出下一次执行时刻（UTC 字符串）。"""
    if not fields.get("enabled"):
        return None
    moment = timeutil.next_run_time(
        fields["time_of_day"],
        fields["timezone"],
        fields["repeat_mode"],
        fields["weekdays"],
        fields["interval_minutes"],
        last_run_at=fields.get("last_run_at") or None,
    )
    return timeutil.to_db(moment)


def _fields_to_columns(fields: dict) -> dict:
    """领域字段 → 数据库列（JSON 字段序列化）。"""
    return {
        "name": fields["name"],
        "task_type": fields["task_type"],
        "enabled": 1 if fields["enabled"] else 0,
        "time_of_day": fields["time_of_day"],
        "repeat_mode": fields["repeat_mode"],
        "weekdays": textutil.dumps(fields["weekdays"]),
        "interval_minutes": fields["interval_minutes"],
        "jitter_seconds": fields["jitter_seconds"],
        "timezone": fields["timezone"],
        "friend_mode": fields["friend_mode"],
        "friend_ids": textutil.dumps(fields["friend_ids"]),
        "messages_json": textutil.dumps(fields["messages"]),
        "send_gap_seconds": textutil.dumps(fields["send_gap_seconds"]),
        "daily_limit": fields["daily_limit"],
        "max_retry": fields["max_retry"],
        "cron_expr": fields["cron_expr"],
        "next_run_at": _next_run_at(fields),
    }


def _weekdays_from_row(raw: Any) -> list[int]:
    """把库里的 ``weekdays`` 还原成 ISO 星期列表（1=周一）。

    坑：库里存的是 JSON 数组字符串 ``'[1, 3, 5]'``。若直接交给
    ``timeutil.parse_weekdays()``，它会当成逗号分隔串切成 ``['[1',' 3',' 5]']``，
    结果只剩 ``[3]`` —— 曾经因此把周一周五静默丢掉。所以先用 ``as_int_list``
    走 JSON 解析，失败再回退到 ``parse_weekdays``（兼容 ``'mon,fri'`` 这类手填值）。
    """
    days = sorted({d for d in textutil.as_int_list(raw) if 1 <= d <= 7})
    if days:
        return days
    return [d for d in timeutil.parse_weekdays(raw) if 1 <= d <= 7]


def _latest_run_summary(row: dict) -> tuple[str | None, str]:
    """最近一次执行的 (last_run_at, last_status)。

    缓存列（``scheduled_tasks.last_run_at/last_status``）只由调度器回填；手动执行不会写它，
    所以这里跟该任务最新一条 ``task_runs``（真源）比时间，取更新的那个。ISO 字符串
    可以直接字典序比较（都是 UTC ``YYYY-MM-DD HH:MM:SS``）。
    """
    cached_at = row.get("last_run_at") or None
    cached_status = row.get("last_status") or ""
    latest_at = row.get("latest_run_at") or None
    latest_status = row.get("latest_run_status") or ""
    if latest_at and (not cached_at or str(latest_at) > str(cached_at)):
        return latest_at, latest_status
    return cached_at, cached_status


def _task_out(row: dict) -> dict:
    """任务对外结构（JSON 字段解析成真类型 + 文案风险提示）。"""
    messages = textutil.as_str_list(row.get("messages_json"))
    friend_mode = row.get("friend_mode") or "all"
    friend_ids = textutil.as_int_list(row.get("friend_ids"))
    friend_count = (len(friend_ids) if friend_mode == "selected"
                    else int(row.get("account_friend_count") or 0))
    low, high = textutil.as_min_max(row.get("send_gap_seconds"))
    last_run_at, last_status = _latest_run_summary(row)
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "account_nickname": row.get("account_nickname") or "",
        "account_status": row.get("account_status") or "",
        "account_enabled": bool(row.get("account_enabled", 1)),
        "name": row.get("name") or "",
        "task_type": row.get("task_type") or "spark",
        "enabled": bool(row.get("enabled", 1)),
        "time_of_day": row.get("time_of_day") or "08:30",
        "repeat_mode": row.get("repeat_mode") or "daily",
        "weekdays": _weekdays_from_row(row.get("weekdays")),
        "interval_minutes": int(row.get("interval_minutes") or 1440),
        "jitter_seconds": int(row.get("jitter_seconds") or 0),
        "timezone": row.get("timezone") or "Asia/Shanghai",
        "friend_mode": friend_mode,
        "friend_ids": friend_ids,
        "friend_count": friend_count,
        "messages": messages,
        "message_warnings": textutil.message_warnings(messages),
        "send_gap_seconds": {"min": low, "max": high},
        "daily_limit": int(row.get("daily_limit") or 1),
        "max_retry": int(row.get("max_retry") or 0),
        "cron_expr": row.get("cron_expr") or "",
        "last_run_at": last_run_at,
        "last_status": last_status,
        "next_run_at": row.get("next_run_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


# --------------------------------------------------------------------------
# 任务 CRUD
# --------------------------------------------------------------------------
@route("GET", "/api/v1/tasks", auth="user", summary="定时任务列表", tags=("tasks",))
def list_tasks(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["t.user_id=?"]
    params: list = [user["id"]]
    account_id = req.int_field("account_id", 0, minimum=0)
    if account_id:
        where.append("t.account_id=?")
        params.append(account_id)
    if "enabled" in req.query:
        where.append("t.enabled=?")
        params.append(1 if req.bool_field("enabled", True) else 0)
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(t.name LIKE ? OR a.nickname LIKE ?)")
        params.extend([f"%{keyword}%"] * 2)
    base = f"{_TASK_SELECT} WHERE {' AND '.join(where)} ORDER BY {_order_by(req, '-id')}"
    result = db.paginate(base, params, page, page_size)
    result["items"] = [_task_out(row) for row in result["items"]]
    return ok(result)


@route("POST", "/api/v1/tasks", auth="user", summary="新建定时任务", tags=("tasks",))
def create_task(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    fields = _collect_fields(req, user["id"], int(account["id"]))
    now = timeutil.utc_now_str()
    columns = _fields_to_columns(fields)
    columns.update({
        "user_id": user["id"],
        "account_id": int(account["id"]),
        "created_at": now,
        "updated_at": now,
    })
    task_id = db.insert("scheduled_tasks", columns)
    row = db.query_one(f"{_TASK_SELECT} WHERE t.id=?", (task_id,)) or {}
    logmod.audit("create_task", user=user, target=str(task_id), ip=req.client_ip)
    return ok(_task_out(row), message="任务已创建")


@route("GET", "/api/v1/tasks/<task_id>", auth="user", summary="任务详情", tags=("tasks",))
def task_detail(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    data = _task_out(row)
    data["recent_runs"] = db.query_all(
        "SELECT id, run_date, trigger_type, status, started_at, finished_at,"
        "       success_count, fail_count, total_count, message"
        "  FROM task_runs WHERE task_id=? AND user_id=? ORDER BY id DESC LIMIT 5",
        (row["id"], row["user_id"]),
    )
    return ok(data)


@route("PUT", "/api/v1/tasks/<task_id>", auth="user", summary="更新任务（整字段覆盖）", tags=("tasks",))
def update_task(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    user = req.require_user()
    # account_id 缺省时保持原账号（换账号要显式传，避免误把任务挪到别的账号）
    raw_account = req.field("account_id", None)
    if raw_account in (None, "", 0, "0"):
        account = db.query_one("SELECT * FROM douyin_accounts WHERE id=? AND user_id=?",
                               (row["account_id"], user["id"]))
        if not account:
            raise HttpError(404, "任务关联的抖音账号不存在", code="not_found")
    else:
        account = require_account_id(req)

    fields = _collect_fields(req, user["id"], int(account["id"]), last_run_at=row.get("last_run_at") or "")
    columns = _fields_to_columns(fields)
    columns.update({
        "account_id": int(account["id"]),
        "updated_at": timeutil.utc_now_str(),
    })
    db.update("scheduled_tasks", columns, "id=? AND user_id=?", (row["id"], user["id"]))
    fresh = db.query_one(f"{_TASK_SELECT} WHERE t.id=?", (row["id"],)) or row
    logmod.audit("update_task", user=user, target=str(row["id"]), ip=req.client_ip)
    return ok(_task_out(fresh), message="任务已更新")


@route("DELETE", "/api/v1/tasks/<task_id>", auth="user", summary="删除任务", tags=("tasks",))
def delete_task(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    # task_runs.task_id 是 ON DELETE SET NULL：执行历史要留着（审计/对账），只解除关联
    deleted = db.delete("scheduled_tasks", "id=? AND user_id=?", (row["id"], row["user_id"]))
    if not deleted:
        raise HttpError(404, "定时任务不存在或不属于当前用户", code="not_found")
    logmod.audit("delete_task", user=req.user, target=str(row["id"]), ip=req.client_ip)
    return ok({"deleted": True, "task_id": row["id"]}, message="任务已删除")


@route("POST", "/api/v1/tasks/<task_id>/toggle", auth="user", summary="启用/停用任务", tags=("tasks",))
def toggle_task(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    if "enabled" in (req.json(required=False) or {}) or "enabled" in req.query:
        enabled = req.bool_field("enabled", not bool(row.get("enabled", 1)))
    else:
        enabled = not bool(row.get("enabled", 1))       # 不传就取反
    now = timeutil.utc_now_str()
    data: dict[str, Any] = {"enabled": 1 if enabled else 0, "updated_at": now}
    if not enabled:
        data["next_run_at"] = None                      # 停用后不再排期
    db.update("scheduled_tasks", data, "id=? AND user_id=?", (row["id"], row["user_id"]))
    if enabled:
        fields = _task_fields_from_row(row)
        fields["enabled"] = True
        db.update("scheduled_tasks", {"next_run_at": _next_run_at(fields), "updated_at": now},
                  "id=? AND user_id=?", (row["id"], row["user_id"]))
    fresh = db.query_one(f"{_TASK_SELECT} WHERE t.id=?", (row["id"],)) or row
    return ok(_task_out(fresh), message="任务已启用" if enabled else "任务已停用")


def _task_fields_from_row(row: dict) -> dict:
    """把数据库行还原成领域字段（用于重算 next_run_at）。"""
    fields = _task_defaults()
    fields.update({
        "name": row.get("name") or fields["name"],
        "task_type": row.get("task_type") or fields["task_type"],
        "enabled": bool(row.get("enabled", 1)),
        "time_of_day": row.get("time_of_day") or fields["time_of_day"],
        "repeat_mode": row.get("repeat_mode") or fields["repeat_mode"],
        "weekdays": _weekdays_from_row(row.get("weekdays")),
        "interval_minutes": int(row.get("interval_minutes") or fields["interval_minutes"]),
        "jitter_seconds": int(row.get("jitter_seconds") or 0),
        "timezone": row.get("timezone") or fields["timezone"],
        "friend_mode": row.get("friend_mode") or fields["friend_mode"],
        "friend_ids": textutil.as_int_list(row.get("friend_ids")),
        "messages": textutil.as_str_list(row.get("messages_json")),
        "send_gap_seconds": dict(zip(("min", "max"), textutil.as_min_max(row.get("send_gap_seconds")))),
        "daily_limit": int(row.get("daily_limit") or 1),
        "max_retry": int(row.get("max_retry") or 0),
        "cron_expr": row.get("cron_expr") or "",
        "last_run_at": row.get("last_run_at") or "",
    })
    return fields


# --------------------------------------------------------------------------
# 立即执行 / 预览
# --------------------------------------------------------------------------
@route("POST", "/api/v1/tasks/<task_id>/run", auth="user", summary="立即执行一次", tags=("tasks",))
def run_task(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    user = req.require_user()
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=? AND user_id=?",
                           (row["account_id"], user["id"]))
    if not account:
        raise HttpError(404, "任务关联的抖音账号不存在", code="not_found")
    # 先做「能不能跑」的判定，避免用户被误导成「要升级套餐」而实际是账号离线
    if not account.get("enabled", 1):
        raise HttpError(409, "该抖音账号已暂停托管，请先在账号页启用", code="conflict")
    if account.get("status") == "need_verify":
        raise HttpError(409, "该账号需要短信二次验证，请先到账号页完成验证", code="need_verify")
    if account.get("status") != "online":
        raise HttpError(409, f"账号当前状态为 {account.get('status')}，请先扫码登录成功后再执行",
                        code="conflict")

    quotamod.Quota.for_user(user["id"]).ensure_can_run_today()

    running = db.query_one(
        "SELECT id FROM task_runs WHERE user_id=? AND task_id=? AND status='running'"
        " ORDER BY id DESC LIMIT 1",
        (user["id"], row["id"]),
    )
    if running:
        raise HttpError(409, f"任务正在执行中（run #{running['id']}），请稍后再试", code="task_running")

    result = _automation_call("app.worker.runner", "enqueue_run", "server_error",
                              int(account["id"]), int(row["id"]), "manual", dry_run=False)
    state = str(result.get("status") or "")
    if state == "rejected":
        error = str(result.get("error") or "")
        message = str(result.get("message") or "任务未能入队")
        if error == "task_running":
            raise HttpError(409, message, code="task_running")
        if error == "not_found":
            raise HttpError(404, message, code="not_found")
        raise HttpError(409, message, code="conflict")
    logmod.audit("run_task", user=user, target=str(row["id"]), ip=req.client_ip)
    return ok({
        "run_id": int(result.get("run_id") or 0),
        "task_id": row["id"],
        "account_id": row["account_id"],
        "status": state or "queued",
        "queued": True,
        "message": str(result.get("message") or "已加入执行队列"),
    }, message="已触发执行")


@route("POST", "/api/v1/tasks/<task_id>/preview", auth="user",
       summary="预览未来 5 次执行时间与随机文案", tags=("tasks",))
def preview_task(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    fields = _task_fields_from_row(row)
    account = db.query_one("SELECT nickname FROM douyin_accounts WHERE id=?", (row["account_id"],))
    friend = db.query_one(
        "SELECT nickname FROM friends WHERE account_id=? AND enabled=1 ORDER BY id LIMIT 1",
        (row["account_id"],),
    )
    friend_name = (friend or {}).get("nickname") or "好友"
    variables = {
        "nickname": friend_name,
        "friend": friend_name,
        "friend_name": friend_name,
        "account": (account or {}).get("nickname") or "",
        "date": timeutil.local_date_str(fields["timezone"]),
        "time": fields["time_of_day"],
    }

    # 未来 5 次：非 interval 模式用 after 递推；interval 模式必须把上一次结果当作
    # 新锚点（否则 next_run_time 每次都从 last_run_at 算出同一个时刻）
    runs: list[str] = []
    after = timeutil.now_utc()
    anchor = fields.get("last_run_at") or ""
    for _ in range(5):
        moment = timeutil.next_run_time(
            fields["time_of_day"], fields["timezone"], fields["repeat_mode"],
            fields["weekdays"], fields["interval_minutes"], after=after,
            last_run_at=anchor or None,
        )
        runs.append(timeutil.to_db(moment))
        after = moment
        anchor = timeutil.to_db(moment)

    messages = textutil.preview_messages(fields["messages"], variables, count=5)
    return ok({
        "task_id": row["id"],
        "enabled": bool(row.get("enabled", 1)),
        "timezone": fields["timezone"],
        "time_of_day": fields["time_of_day"],
        "repeat_mode": fields["repeat_mode"],
        "weekdays": fields["weekdays"],
        "next_runs": runs,
        "next_runs_local": [timeutil.format_local(item, fields["timezone"]) for item in runs],
        "messages": messages,
        "message_warnings": textutil.message_warnings(fields["messages"]),
        "friend_mode": fields["friend_mode"],
        "friend_count": (len(fields["friend_ids"]) if fields["friend_mode"] == "selected"
                         else int(row.get("account_friend_count") or 0)),
        "note": "" if row.get("enabled", 1) else "任务当前已停用，以上时间为启用后的预估",
    })


@route("GET", "/api/v1/tasks/<task_id>/runs", auth="user", summary="该任务的执行记录", tags=("tasks",))
def task_runs(req: Request, task_id: str) -> Response:
    row = _get_task(req, task_id)
    return ok(run_records_page(req, row["user_id"], task_id=int(row["id"])))


# --------------------------------------------------------------------------
# 好友
# --------------------------------------------------------------------------
@route("GET", "/api/v1/friends", auth="user", summary="好友列表（分页）", tags=("friends",))
def list_friends(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["f.user_id=?"]
    params: list = [user["id"]]
    account_id = req.int_field("account_id", 0, minimum=0)
    if account_id:
        where.append("f.account_id=?")
        params.append(account_id)
    if "enabled" in req.query:
        where.append("f.enabled=?")
        params.append(1 if req.bool_field("enabled", True) else 0)
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(f.nickname LIKE ? OR f.remark LIKE ? OR f.sec_uid LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    base = (
        "SELECT f.*, a.nickname AS account_nickname\n"
        "  FROM friends f LEFT JOIN douyin_accounts a ON a.id = f.account_id\n"
        f" WHERE {' AND '.join(where)} ORDER BY f.id DESC"
    )
    result = db.paginate(base, params, page, page_size)
    for item in result["items"]:
        item["enabled"] = bool(item.get("enabled", 1))
    return ok(result)


def _friend_payload(req: Request, *, base_friend: dict | None = None) -> dict:
    """好友新增/修改的字段解析（新增时 nickname 必填）。

    **PUT 语义特例（已与 Lead 确认）**：契约 §0 的默认规则是「PUT = 整字段覆盖」，
    但 ``PUT /friends/<id>`` 只覆盖**请求体里出现过的**字段（``base_friend`` 非空时
    未传的字段保持原值）。原因：好友编辑表单通常只让用户改一个字段（备注/启用/昵称），
    整覆盖会把没传的 ``remark``/``enabled`` 悄悄清空，属于数据丢失。
    除本接口外，全站其它 PUT 一律是整字段覆盖 —— 新接口不要照抄这里的局部语义，
    除非同样有「表单只提交部分字段」的正当理由。
    """
    data: dict[str, Any] = {"updated_at": timeutil.utc_now_str()}
    body = req.json(required=False) or {}
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")

    if base_friend is None or "nickname" in body:
        nickname = req.str_field("nickname", "", max_len=64)
        if not nickname:
            raise HttpError(400, "请填写好友昵称")
        data["nickname"] = nickname
    if "sec_uid" in body or base_friend is None:
        good, sec_uid = validate.sec_uid(req.str_field("sec_uid", "", max_len=160))
        if not good:
            raise HttpError(400, sec_uid)
        data["sec_uid"] = sec_uid or None
    if "remark" in body or base_friend is None:
        data["remark"] = req.str_field("remark", "", max_len=200)
    if "enabled" in body or base_friend is None:
        data["enabled"] = 1 if req.bool_field("enabled", True) else 0
    if "spark_days" in body:
        data["spark_days"] = req.int_field("spark_days", 0, minimum=0, maximum=100000)
    return data


@route("POST", "/api/v1/friends", auth="user", summary="添加好友", tags=("friends",))
def create_friend(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    data = _friend_payload(req)
    nickname = str(data["nickname"])
    if _find_friend_by_name(int(account["id"]), nickname):
        raise HttpError(409, f"该账号下已存在好友「{nickname}」", code="conflict")
    now = timeutil.utc_now_str()
    data.update({
        "user_id": user["id"],
        "account_id": int(account["id"]),
        "created_at": now,
    })
    try:
        friend_id = db.insert("friends", data)
    except sqlite3.IntegrityError as exc:
        # 唯一索引 (account_id, nickname) 兜底：并发插入同名好友
        raise HttpError(409, f"该账号下已存在好友「{nickname}」", code="conflict") from exc
    row = _get_friend(req, friend_id)
    return ok(row, message="好友已添加")


def _find_friend_by_name(account_id: int, nickname: str) -> dict | None:
    """按规范化昵称查重（「小明」与「小明 」视为同一人，唯一索引拦不住后者）。"""
    target = textutil.normalize_name(nickname)
    for row in db.query_all("SELECT id, nickname FROM friends WHERE account_id=?", (account_id,)):
        if textutil.normalize_name(row["nickname"]) == target:
            return row
    return None


@route("POST", "/api/v1/friends/import", auth="user", summary="批量导入好友", tags=("friends",))
def import_friends(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    entries = _parse_import_entries(req)
    if not entries:
        raise HttpError(400, "没有解析到任何好友，请检查导入内容")

    aid = int(account["id"])
    existing = {textutil.normalize_name(row["nickname"])
                for row in db.query_all("SELECT nickname FROM friends WHERE account_id=?", (aid,))}
    now = timeutil.utc_now_str()
    imported = 0
    skipped = 0
    errors: list[str] = []
    for nickname, sec_uid in entries:
        key = textutil.normalize_name(nickname)
        if not key:
            continue
        if key in existing:
            skipped += 1
            continue
        good, sec = validate.sec_uid(sec_uid)
        if not good:
            errors.append(f"{nickname}：{sec}（已导入但未记录 sec_uid）")
            sec = ""
        try:
            db.insert("friends", {
                "user_id": user["id"], "account_id": aid, "nickname": nickname[:64],
                "sec_uid": sec or None, "remark": "", "enabled": 1,
                "created_at": now, "updated_at": now,
            })
        except sqlite3.IntegrityError:
            skipped += 1
            continue
        existing.add(key)
        imported += 1
    return ok({"imported": imported, "skipped": skipped, "total": len(entries),
               "errors": errors[:50]},
              message=f"导入完成：新增 {imported}，跳过 {skipped}")


def _parse_import_entries(req: Request) -> list[tuple[str, str]]:
    """支持两种入参：``text``（每行「昵称」或「昵称,sec_uid」）与 ``friends`` 数组。"""
    raw_list = req.field("friends", None)
    if isinstance(raw_list, list):
        out: list[tuple[str, str]] = []
        for item in raw_list:
            if isinstance(item, dict):
                out.append((str(item.get("nickname") or "").strip(), str(item.get("sec_uid") or "").strip()))
            else:
                out.append((str(item).strip(), ""))
        return [(n, s) for n, s in out if n]

    text = req.str_field("text", "", max_len=20000)
    entries: list[tuple[str, str]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        nickname, _, sec_uid = line.partition(",")
        nickname = nickname.strip()
        if nickname:
            entries.append((nickname, sec_uid.strip()))
    return entries


@route("PUT", "/api/v1/friends/<friend_id>", auth="user", summary="修改好友", tags=("friends",))
def update_friend(req: Request, friend_id: str) -> Response:
    row = _get_friend(req, friend_id)
    data = _friend_payload(req, base_friend=row)
    new_name = data.get("nickname")
    if new_name and textutil.normalize_name(new_name) != textutil.normalize_name(row["nickname"]):
        clash = _find_friend_by_name(int(row["account_id"]), str(new_name))
        if clash and int(clash["id"]) != int(row["id"]):
            raise HttpError(409, f"该账号下已存在好友「{new_name}」", code="conflict")
    try:
        db.update("friends", data, "id=? AND user_id=?", (row["id"], row["user_id"]))
    except sqlite3.IntegrityError as exc:
        raise HttpError(409, "该账号下已存在同名好友", code="conflict") from exc
    return ok(_get_friend(req, row["id"]), message="好友已更新")


@route("DELETE", "/api/v1/friends/<friend_id>", auth="user", summary="删除好友", tags=("friends",))
def delete_friend(req: Request, friend_id: str) -> Response:
    row = _get_friend(req, friend_id)
    # send_history.friend_id 是 SET NULL：台账要留痕，不能因为删好友就丢发送记录
    deleted = db.delete("friends", "id=? AND user_id=?", (row["id"], row["user_id"]))
    if not deleted:
        raise HttpError(404, "好友不存在或不属于当前用户", code="not_found")
    return ok({"deleted": True, "friend_id": row["id"]}, message="好友已删除")


@route("POST", "/api/v1/friends/batch-delete", auth="user",
       summary="批量删除好友", tags=("friends",))
def batch_delete_friends(req: Request) -> Response:
    """一次删除多个好友（好友管理页的批量删除）。

    ``send_history.friend_id`` 是 ``ON DELETE SET NULL``：台账留痕，删好友不丢发送记录。
    只删「属于当前用户」的行 —— 传别人的 id 会被忽略（既不删也不报错，避免泄漏存在性）。
    """
    user = req.require_user()
    body = req.json(required=False) or {}
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")
    raw = body.get("ids", body.get("friend_ids", []))
    if not isinstance(raw, list) or not raw:
        raise HttpError(400, "请选择要删除的好友")
    if len(raw) > MAX_BATCH_FRIEND_DELETE:
        raise HttpError(400, f"一次最多删除 {MAX_BATCH_FRIEND_DELETE} 个好友")
    ids: list[int] = []
    for value in raw:
        try:
            friend_id = int(value)
        except (TypeError, ValueError):
            continue
        if friend_id > 0 and friend_id not in ids:
            ids.append(friend_id)
    if not ids:
        raise HttpError(400, "请选择要删除的好友")

    placeholders = ",".join("?" * len(ids))
    params: tuple = tuple([user["id"], *ids])
    deleted = db.delete("friends", f"user_id=? AND id IN ({placeholders})", params)
    return ok({"deleted": int(deleted or 0), "requested": len(ids)},
              message=f"已删除 {int(deleted or 0)} 个好友")


@route("POST", "/api/v1/friends/sync", auth="user", summary="从抖音同步最近会话好友", tags=("friends",))
def sync_friends(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    if account.get("status") != "online":
        raise HttpError(409, f"账号当前状态为 {account.get('status')}，请先扫码登录成功后再同步",
                        code="conflict")

    result = _automation_call("app.automation.douyin", "sync_friends", "server_error",
                              account, get_settings())
    items = result.get("friends")
    if not isinstance(items, list):
        items = result.get("items") if isinstance(result.get("items"), list) else []
    aid = int(account["id"])
    existing = {textutil.normalize_name(row["nickname"])
                for row in db.query_all("SELECT nickname FROM friends WHERE account_id=?", (aid,))}
    now = timeutil.utc_now_str()
    created = 0
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        nickname = str(item.get("nickname") or "").strip()
        if not nickname:
            continue
        key = textutil.normalize_name(nickname)
        if key in existing:
            skipped += 1
            continue
        good, sec_uid = validate.sec_uid(str(item.get("sec_uid") or ""))
        try:
            db.insert("friends", {
                "user_id": user["id"], "account_id": aid, "nickname": nickname[:64],
                "sec_uid": (sec_uid or None) if good else None,
                "remark": "", "enabled": 1, "created_at": now, "updated_at": now,
            })
        except sqlite3.IntegrityError:
            skipped += 1
            continue
        existing.add(key)
        created += 1
    return ok({"synced": len(items), "created": created, "skipped": skipped,
               "message": str(result.get("message") or "")},
              message=f"同步完成：新增 {created} 个好友")


# --------------------------------------------------------------------------
# 发送台账
# --------------------------------------------------------------------------
@route("GET", "/api/v1/douyin/stats/send-history", auth="user",
       summary="发送台账（哪天给谁发过）", tags=("douyin",))
def send_history(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    # send_history 没有 user_id 列：必须 join douyin_accounts 再按 a.user_id 过滤
    where = ["a.user_id=?"]
    params: list = [user["id"]]
    account_id = req.int_field("account_id", 0, minimum=0)
    if account_id:
        where.append("h.account_id=?")
        params.append(account_id)
    friend_id = req.int_field("friend_id", 0, minimum=0)
    if friend_id:
        where.append("h.friend_id=?")
        params.append(friend_id)
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(h.friend_key LIKE ? OR f.nickname LIKE ?)")
        params.extend([f"%{keyword}%"] * 2)
    start = req.str_field("start_date", "", max_len=20)
    if start:
        where.append("h.send_date>=?")
        params.append(start)
    end = req.str_field("end_date", "", max_len=20)
    if end:
        where.append("h.send_date<=?")
        params.append(end)

    base = (
        "SELECT h.id, h.account_id, a.nickname AS account_nickname, h.friend_id,\n"
        "       f.nickname AS friend_name, h.friend_key, h.send_date, h.message,\n"
        "       h.run_id, h.created_at\n"
        "  FROM send_history h\n"
        "  JOIN douyin_accounts a ON a.id = h.account_id\n"
        "  LEFT JOIN friends f ON f.id = h.friend_id\n"
        f" WHERE {' AND '.join(where)} ORDER BY h.id DESC"
    )
    result = db.paginate(base, params, page, page_size)
    tz = get_config("task.default_timezone", get_settings().timezone)
    today = timeutil.local_date_str(tz)
    summary = db.query_one(
        "SELECT COUNT(*) AS total_today, COUNT(DISTINCT h.friend_key) AS friends_today\n"
        "  FROM send_history h JOIN douyin_accounts a ON a.id = h.account_id\n"
        " WHERE a.user_id=? AND h.send_date=?",
        (user["id"], today),
    ) or {}
    result["summary"] = {
        "date": today,
        "sent_today": int(summary.get("total_today") or 0),
        "friends_today": int(summary.get("friends_today") or 0),
    }
    return ok(result)
