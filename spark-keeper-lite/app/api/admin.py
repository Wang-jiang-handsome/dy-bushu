"""管理端接口（契约第 7 节）。

组织方式
--------
按业务分区排列：概览统计 → 用户 → 卡密 → 套餐 → 订单 → 抖音账号 → 任务 → 日志 →
公告 → 站点配置 → 代理 → 系统。所有接口 ``auth="admin"``，列表统一分页 + 筛选。

三条硬规则
----------
1. **关键动作必须写审计**：改角色/状态、调余额、重置密码、授权套餐、删用户、
   卡密生成/停用/删除、订单确认收款/退款、配置保存、代理增删改、日志清理。
   审计用 ``app.common.logging.audit``（写 ``audit_logs``）。
2. **财务字段只走既有流水**：调余额写 ``balance_logs(kind='adjust')``；订单发放一律
   走 ``cardkeys.fulfill_order``（幂等 + 事务内重读），管理端不自己加天数。
3. **可选组件缺失要优雅降级**：自动化引擎（队友 D 的 ``app.worker.*``）在 lite 版或
   未装配时不存在，相关接口返回明确的"未就绪"而不是 500。
"""

from __future__ import annotations

import datetime
import importlib
import re
import socket
import time
from typing import Any

from .. import bootstrap as boot
from .. import db
from ..common import auth as authmod
from ..common import logging as logmod
from ..common import mailer
from ..common import quota as quota_mod
from ..common import textutil
from ..common import timeutil
from ..common import validate
from ..common.security import (
    encrypt,
    hash_password,
    password_problem,
    random_code,
)
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route
from ..version import BUILD, PRODUCT, VERSION
from . import cardkeys
from .public import package_public

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
ROLES = ("user", "agent", "admin")
USER_STATUSES = ("active", "disabled")
ACCOUNT_STATUSES = ("online", "offline", "expired", "banned", "need_verify")
LOG_LEVELS = ("debug", "info", "warning", "error")
LOG_SCOPES = ("user", "admin", "system", "worker")
ANNOUNCEMENT_LEVELS = ("info", "warning", "important")
SECRET_KEY_HINT = re.compile(r"(key|secret|password|token|passwd)", re.I)
MASKED = "******"


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def _pid(raw: Any, label: str = "ID") -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HttpError(400, f"{label}不合法") from None
    if value <= 0:
        raise HttpError(400, f"{label}不合法")
    return value


def _optional_module(name: str):
    """导入可选模块（自动化/worker）。缺失时返回 None 并记一条警告。

    只吞 ImportError：语法错误之类的真问题要让它响，别把坏代码藏起来。
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        logmod.warning(f"可选模块 {name} 不可用：{exc}", scope="admin", source="admin")
        return None


def _audit(req: Request, action: str, target: str = "", detail: str = "") -> None:
    admin = req.user or {}
    logmod.audit(action, scope="admin", user=admin, target=target, detail=detail, ip=req.client_ip)


def _fnum(value: Any, label: str, minimum: float, maximum: float, default: float = 0.0) -> float:
    """浮点参数解析（比 int_field 宽松但仍有边界，避免 "abc" 变成 500）。"""
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"{label}必须是数字") from None
    if number < minimum or number > maximum:
        raise HttpError(400, f"{label}需在 {minimum}~{maximum} 之间")
    return number


def _int_filter(value: Any, label: str = "ID") -> int | None:
    """可选的 ID 筛选参数：空值返回 None，非法值 400（不要让它变成 500）。"""
    if value in (None, ""):
        return None
    return _pid(value, label)


def _tcp_reachable(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str, int]:
    """TCP 连通性探测（标准库 socket）。返回 (是否可达, 错误说明, 耗时毫秒)。"""
    started = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "", int(round((time.time() - started) * 1000))
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}", int(round((time.time() - started) * 1000))


def _tz() -> str:
    return boot.get_config("task.default_timezone", get_settings().timezone) or "Asia/Shanghai"


def _day_start_utc(days_ago: int = 0) -> str:
    """站点时区「今天 00:00」对应的 UTC 时间串（统计口径统一用它）。"""
    moment = timeutil.local_now(_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    return timeutil.to_db((moment - datetime.timedelta(days=days_ago)).astimezone(timeutil.UTC))


def _mask_config_value(key: str, value: str, reveal: bool) -> str:
    if reveal or not SECRET_KEY_HINT.search(key or ""):
        return value
    return MASKED if value else ""


def _user_admin(row: dict) -> dict:
    """管理端用户视图（含套餐、配额、账号/任务数量）。"""
    quota = None
    try:
        quota = quota_mod.Quota.for_user(int(row["id"])).summary()
    except HttpError:
        quota = None
    return {
        "id": row.get("id"),
        "username": row.get("username"),
        "nickname": row.get("nickname") or "",
        "email": row.get("email") or "",
        "phone": row.get("phone") or "",
        "role": row.get("role"),
        "status": row.get("status"),
        "avatar": row.get("avatar") or "",
        "balance_cents": int(row.get("balance_cents") or 0),
        "package_id": row.get("package_id"),
        "package_name": row.get("package_name") or "",
        "package_code": row.get("package_code") or "",
        "package_expire_at": row.get("package_expire_at") or "",
        "commission_rate": float(row.get("commission_rate") or 0),
        "parent_agent_id": row.get("parent_agent_id"),
        "account_count": int(row.get("account_count") or 0),
        "task_count": int(row.get("task_count") or 0),
        "paid_order_count": int(row.get("paid_order_count") or 0),
        "last_login_at": row.get("last_login_at") or "",
        "last_login_ip": row.get("last_login_ip") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
        "quota": quota,
    }


USER_ADMIN_SELECT = (
    "SELECT u.*, p.name AS package_name, p.code AS package_code,"
    " (SELECT COUNT(*) FROM douyin_accounts a WHERE a.user_id=u.id) AS account_count,"
    " (SELECT COUNT(*) FROM scheduled_tasks t WHERE t.user_id=u.id) AS task_count,"
    " (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.id AND o.status='paid') AS paid_order_count"
    " FROM users u LEFT JOIN packages p ON p.id=u.package_id"
)

ORDER_ADMIN_SELECT = (
    "SELECT o.*, p.name AS package_name, u.username AS username, u.nickname AS nickname,"
    " u.email AS user_email FROM orders o"
    " LEFT JOIN packages p ON p.id=o.package_id"
    " LEFT JOIN users u ON u.id=o.user_id"
)


# ==========================================================================
# 1. 概览统计
# ==========================================================================
@route("GET", "/api/v1/admin/stats", auth="admin", summary="管理端概览统计", tags=("admin",))
def admin_stats(req: Request) -> Response:
    tz = _tz()
    today = timeutil.local_date_str(tz)
    today_start = _day_start_utc(0)
    week_start = _day_start_utc(7)

    users = {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM users", (), 0) or 0),
        "today_new": int(db.query_scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", (today_start,), 0) or 0),
        "active_7d": int(db.query_scalar(
            "SELECT COUNT(*) FROM users WHERE last_login_at>=?", (week_start,), 0) or 0),
        "disabled": int(db.query_scalar("SELECT COUNT(*) FROM users WHERE status='disabled'", (), 0) or 0),
    }
    accounts = {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM douyin_accounts", (), 0) or 0),
        "online": int(db.query_scalar("SELECT COUNT(*) FROM douyin_accounts WHERE status='online'", (), 0) or 0),
        "expired": int(db.query_scalar(
            "SELECT COUNT(*) FROM douyin_accounts WHERE status IN ('expired','banned')", (), 0) or 0),
    }
    tasks = {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM scheduled_tasks", (), 0) or 0),
        "enabled": int(db.query_scalar("SELECT COUNT(*) FROM scheduled_tasks WHERE enabled=1", (), 0) or 0),
        "today_runs": int(db.query_scalar("SELECT COUNT(*) FROM task_runs WHERE run_date=?", (today,), 0) or 0),
        "today_success": int(db.query_scalar(
            "SELECT COUNT(*) FROM task_runs WHERE run_date=? AND status IN ('success','partial')",
            (today,), 0) or 0),
    }
    orders = {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM orders", (), 0) or 0),
        "paid": int(db.query_scalar("SELECT COUNT(*) FROM orders WHERE status='paid'", (), 0) or 0),
        "pending": int(db.query_scalar("SELECT COUNT(*) FROM orders WHERE status='pending'", (), 0) or 0),
        "refunded": int(db.query_scalar("SELECT COUNT(*) FROM orders WHERE status='refunded'", (), 0) or 0),
        "revenue_cents": int(db.query_scalar(
            "SELECT COALESCE(SUM(payable_cents),0) FROM orders WHERE status='paid'", (), 0) or 0),
        "today_revenue_cents": int(db.query_scalar(
            "SELECT COALESCE(SUM(payable_cents),0) FROM orders WHERE status='paid' AND paid_at>=?",
            (today_start,), 0) or 0),
    }
    cards = {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM card_keys", (), 0) or 0),
        "used": int(db.query_scalar("SELECT COUNT(*) FROM card_keys WHERE status='used'", (), 0) or 0),
        "unused": int(db.query_scalar("SELECT COUNT(*) FROM card_keys WHERE status='unused'", (), 0) or 0),
        "disabled": int(db.query_scalar("SELECT COUNT(*) FROM card_keys WHERE status='disabled'", (), 0) or 0),
    }

    trend: list[dict] = []
    for day in timeutil.recent_dates(7, tz):
        runs = db.query_one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('success','partial') THEN 1 ELSE 0 END) AS ok"
            " FROM task_runs WHERE run_date=?",
            (day,),
        ) or {}
        money = db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='paid' THEN payable_cents ELSE 0 END),0) AS rev"
            " FROM orders WHERE substr(created_at,1,10)=?",
            (day,),
        ) or {}
        trend.append(
            {
                "date": day,
                "runs": int(runs.get("n") or 0),
                "success": int(runs.get("ok") or 0),
                "orders": int(money.get("n") or 0),
                "revenue_cents": int(money.get("rev") or 0),
            }
        )

    return ok(
        {
            "users": users,
            "accounts": accounts,
            "tasks": tasks,
            "orders": orders,
            "cards": cards,
            "trend": trend,
        }
    )


# ==========================================================================
# 2. 用户管理
# ==========================================================================
@route("GET", "/api/v1/admin/users", auth="admin", summary="用户列表", tags=("admin", "users"))
def admin_users(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    role = req.str_field("role", "", max_len=16)
    status = req.str_field("status", "", max_len=16)
    package_id = _int_filter(req.field("package_id"), "套餐 ID")
    start_date = req.str_field("start_date", "", max_len=20)
    end_date = req.str_field("end_date", "", max_len=20)

    sql = USER_ADMIN_SELECT
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(u.username LIKE ? OR u.nickname LIKE ? OR u.email LIKE ? OR u.phone LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    if role:
        if role not in ROLES:
            raise HttpError(400, "角色取值不正确")
        where.append("u.role=?")
        params.append(role)
    if status:
        if status not in USER_STATUSES:
            raise HttpError(400, "状态取值不正确")
        where.append("u.status=?")
        params.append(status)
    if package_id:
        where.append("u.package_id=?")
        params.append(package_id)
    if start_date:
        where.append("u.created_at>=?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("u.created_at<=?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY u.id DESC"

    result = db.paginate(sql, params, page, page_size)
    result["items"] = [_user_admin(row) for row in result["items"]]
    return ok(result)


@route("POST", "/api/v1/admin/users", auth="admin", summary="新建用户", tags=("admin", "users"))
def admin_create_user(req: Request) -> Response:
    username = req.str_field("username", "", max_len=32).strip()
    password = req.str_field("password", "", max_len=128)
    if not USERNAME_RE.match(username):
        raise HttpError(400, "用户名需为 3~32 位字母、数字、下划线、点或短横线")
    problem = password_problem(password)
    if problem:
        raise HttpError(400, problem)
    role = req.str_field("role", "user", max_len=16) or "user"
    if role not in ROLES:
        raise HttpError(400, "角色取值不正确")
    status = req.str_field("status", "active", max_len=16) or "active"
    if status not in USER_STATUSES:
        raise HttpError(400, "状态取值不正确")
    email = req.str_field("email", "", max_len=128)
    phone = req.str_field("phone", "", max_len=32)
    nickname = req.str_field("nickname", "", max_len=64)
    balance_cents = req.int_field("balance_cents", 0)
    package_code = req.str_field("package_code", "", max_len=32)
    days = req.int_field("days", 0, minimum=0, maximum=36500)

    package = None
    if package_code:
        package = db.query_one("SELECT * FROM packages WHERE code=?", (package_code,))
        if not package:
            raise HttpError(400, f"套餐 {package_code} 不存在")

    now = timeutil.utc_now_str()
    expire_at = None
    if package:
        expire_at = quota_mod.merge_expire(None, days or int(package["days"] or 0))

    with db.transaction():
        if db.query_one("SELECT id FROM users WHERE username=?", (username,)):
            raise HttpError(409, "该用户名已存在", code="conflict")
        if email and db.query_one("SELECT id FROM users WHERE email=?", (email,)):
            raise HttpError(409, "该邮箱已被使用", code="conflict")
        if phone and db.query_one("SELECT id FROM users WHERE phone=?", (phone,)):
            raise HttpError(409, "该手机号已被使用", code="conflict")
        user_id = db.insert(
            "users",
            {
                "email": email or None,
                "phone": phone or None,
                "username": username,
                "password_hash": hash_password(password),
                "role": role,
                "status": status,
                "nickname": nickname or username,
                "balance_cents": int(balance_cents),
                "package_id": package["id"] if package else None,
                "package_expire_at": expire_at,
                "commission_rate": 0,
                "quota_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )
        if balance_cents:
            db.insert(
                "balance_logs",
                {
                    "user_id": user_id,
                    "change_cents": int(balance_cents),
                    "balance_after": int(balance_cents),
                    "kind": "adjust",
                    "ref_type": "manual",
                    "ref_id": str(req.user_id),
                    "remark": "管理员创建用户时初始化余额",
                    "created_at": now,
                },
            )
    quota_mod.Quota.refresh(user_id)
    _audit(req, "admin_user_create", target=username,
           detail=f"角色={role} 套餐={(package or {}).get('code', '-')} 余额={balance_cents}")
    row = db.query_one(USER_ADMIN_SELECT + " WHERE u.id=?", (user_id,))
    return ok(_user_admin(row or {}), "用户已创建")


@route("GET", "/api/v1/admin/users/<user_id>", auth="admin", summary="用户详情", tags=("admin", "users"))
def admin_user_detail(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    row = db.query_one(USER_ADMIN_SELECT + " WHERE u.id=?", (uid,))
    if not row:
        raise HttpError(404, "用户不存在")
    data = _user_admin(row)
    data["accounts"] = db.query_all(
        "SELECT id, nickname, douyin_uid, status, login_type, last_check_at, last_error, created_at"
        " FROM douyin_accounts WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (uid,),
    )
    data["tasks"] = db.query_all(
        "SELECT id, name, enabled, time_of_day, repeat_mode, last_run_at, last_status, next_run_at"
        " FROM scheduled_tasks WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (uid,),
    )
    data["orders"] = [
        cardkeys.order_public(order, with_user=True)
        for order in db.query_all(
            ORDER_ADMIN_SELECT + " WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20", (uid,)
        )
    ]
    data["card_redemptions"] = cardkeys.redemption_rows(uid, 1, 20)["items"]
    data["balance_logs"] = db.query_all(
        "SELECT id, change_cents, balance_after, kind, ref_type, ref_id, remark, created_at"
        " FROM balance_logs WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (uid,),
    )
    data["sessions"] = int(db.query_scalar(
        "SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL AND expire_at>?",
        (uid, timeutil.utc_now_str()), 0) or 0)
    return ok(data)


@route("PUT", "/api/v1/admin/users/<user_id>", auth="admin", summary="修改用户资料/角色/状态", tags=("admin", "users"))
def admin_update_user(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    user = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HttpError(404, "用户不存在")
    now = timeutil.utc_now_str()
    update: dict[str, Any] = {"updated_at": now}
    detail: list[str] = []

    # 登录用户名（可以改，但要过三道关）：
    #   1) 格式校验（与注册一致：3~24 位，中英文/数字/下划线/短横线）
    #   2) 唯一性 —— users.username 上有 UNIQUE 索引，先查友好报错
    #   3) 改完撤销该用户所有会话，避免「旧令牌还带着旧身份」的困惑
    # 注意：这是"账号标识"而不是"显示名"，显示名请改 nickname。
    if req.field("username") is not None:
        new_name = req.str_field("username", "", max_len=32)
        ok_fmt, fixed = validate.username(new_name)
        if not ok_fmt:
            raise HttpError(400, fixed or "用户名格式不正确")
        if fixed != user["username"]:
            clash = db.query_one("SELECT id FROM users WHERE username=? AND id<>?", (fixed, uid))
            if clash:
                raise HttpError(409, f"用户名「{fixed}」已被占用", code="conflict")
            update["username"] = fixed
            detail.append(f"username={user['username']}->{fixed}")

    for field_name, column, max_len in (
        ("nickname", "nickname", 64),
        ("avatar", "avatar", 255),
    ):
        if req.field(field_name) is not None:
            update[column] = req.str_field(field_name, "", max_len=max_len)
            detail.append(f"{field_name}={update[column]}")
    for field_name, column, check_sql in (
        ("email", "email", "SELECT id FROM users WHERE email=? AND id<>?"),
        ("phone", "phone", "SELECT id FROM users WHERE phone=? AND id<>?"),
    ):
        if req.field(field_name) is None:
            continue
        value = req.str_field(field_name, "", max_len=128)
        if value and db.query_one(check_sql, (value, uid)):
            raise HttpError(409, f"{field_name} 已被其他用户使用", code="conflict")
        update[column] = value or None
        detail.append(f"{field_name}={value or '-'}")
    if req.field("role") is not None:
        role = req.str_field("role", "", max_len=16)
        if role not in ROLES:
            raise HttpError(400, "角色取值不正确")
        if role != "admin" and user["role"] == "admin" and db.query_scalar(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'", (), 0
        ) <= 1:
            raise HttpError(409, "系统必须保留至少一个管理员", code="conflict")
        update["role"] = role
        detail.append(f"role={user['role']}->{role}")
    if req.field("status") is not None:
        status = req.str_field("status", "", max_len=16)
        if status not in USER_STATUSES:
            raise HttpError(400, "状态取值不正确")
        if status == "disabled" and int(uid) == req.user_id:
            raise HttpError(400, "不能禁用自己的账号")
        update["status"] = status
        detail.append(f"status={user['status']}->{status}")
    if req.field("commission_rate") is not None:
        rate = _fnum(req.field("commission_rate"), "佣金比例", 0, 1, 0)
        update["commission_rate"] = rate
        detail.append(f"commission_rate={rate}")
    if req.field("balance_cents") is not None:
        # 直接改余额必须走 /balance 接口留流水，这里拒绝以免绕过审计
        raise HttpError(400, "调整余额请使用 POST /admin/users/<id>/balance（会强制写流水）")

    db.update("users", update, "id=?", (uid,))
    if "role" in update or "status" in update or "username" in update:
        # 角色/状态/用户名变化要立刻生效：撤销所有会话，避免旧 token 继续带旧身份
        revoked = authmod.revoke_all_sessions(uid)
        detail.append(f"撤销会话 {revoked} 个")
    quota_mod.Quota.refresh(uid)
    _audit(req, "admin_user_update", target=str(user["username"]), detail=" ".join(detail))
    row = db.query_one(USER_ADMIN_SELECT + " WHERE u.id=?", (uid,))
    return ok(_user_admin(row or {}))


@route(
    "POST",
    "/api/v1/admin/users/<user_id>/reset-password",
    auth="admin",
    summary="重置用户密码",
    tags=("admin", "users"),
)
def admin_reset_password(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    user = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HttpError(404, "用户不存在")
    password = req.str_field("new_password", "", max_len=128) or req.str_field("password", "", max_len=128)
    generated = False
    if not password:
        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        password = "".join(random_code(1, alphabet) for _ in range(12)) + "8a"
        generated = True
    problem = password_problem(password)
    if problem:
        raise HttpError(400, problem)
    db.update(
        "users",
        {"password_hash": hash_password(password), "updated_at": timeutil.utc_now_str()},
        "id=?",
        (uid,),
    )
    revoked = authmod.revoke_all_sessions(uid)
    _audit(req, "admin_reset_password", target=str(user["username"]),
           detail=f"撤销会话 {revoked} 个 初始密码是否自动生成={generated}")
    logmod.notify(uid, "密码已被管理员重置", "如非本人操作，请立即联系管理员。")
    return ok(
        {
            "user_id": uid,
            "username": user["username"],
            "password": password if generated else "",
            "password_generated": generated,
            "sessions_revoked": revoked,
        },
        "密码已重置" + ("（请把自动生成的初始密码交给用户）" if generated else ""),
    )


@route(
    "POST",
    "/api/v1/admin/users/<user_id>/grant-package",
    auth="admin",
    summary="手动授权套餐",
    tags=("admin", "users"),
)
def admin_grant_package(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    user = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HttpError(404, "用户不存在")
    package_id = req.field("package_id")
    package_code = req.str_field("package_code", "", max_len=32)
    package = None
    if package_id:
        package = db.query_one("SELECT * FROM packages WHERE id=?", (int(package_id),))
    elif package_code:
        package = db.query_one("SELECT * FROM packages WHERE code=?", (package_code,))
    if not package:
        raise HttpError(400, "请指定有效的套餐（package_id 或 package_code）")
    days = req.int_field("days", int(package["days"] or 0), minimum=1, maximum=36500)
    mode = req.str_field("mode", "extend", max_len=16) or "extend"
    remark = req.str_field("remark", "", max_len=200) or "管理员手动授权"

    before = user.get("package_expire_at")
    after = (
        timeutil.add_days_str(days)
        if mode == "reset"
        else quota_mod.merge_expire(before, days)
    )
    db.update(
        "users",
        {"package_id": package["id"], "package_expire_at": after, "updated_at": timeutil.utc_now_str()},
        "id=?",
        (uid,),
    )
    quota_mod.Quota.refresh(uid)
    _audit(req, "admin_grant_package", target=str(user["username"]),
           detail=f"套餐={package['code']} 天数={days} 模式={mode} {before or '-'} -> {after} 备注={remark}")
    logmod.notify(uid, "套餐已开通", f"{package['name']} 已由管理员开通 {days} 天，到期时间 {after}")
    row = db.query_one(USER_ADMIN_SELECT + " WHERE u.id=?", (uid,))
    return ok(
        {
            "user": _user_admin(row or {}),
            "package_name": package["name"],
            "days": days,
            "expire_before": before or "",
            "expire_after": after,
        },
        "已授权套餐",
    )


@route(
    "POST",
    "/api/v1/admin/users/<user_id>/balance",
    auth="admin",
    summary="调整余额（正负均可，强制备注）",
    tags=("admin", "users"),
)
def admin_adjust_balance(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    user = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HttpError(404, "用户不存在")
    change = req.int_field("change_cents", 0)
    if change == 0:
        raise HttpError(400, "调整金额不能为 0")
    remark = req.str_field("remark", "", max_len=200).strip()
    if not remark:
        raise HttpError(400, "请填写调整备注（审计要求）")
    before = int(user["balance_cents"] or 0)
    after = before + change
    if after < 0:
        raise HttpError(400, f"调整后余额为负数（当前 {before} 分），请先确认金额")
    now = timeutil.utc_now_str()
    with db.transaction():
        db.update("users", {"balance_cents": after, "updated_at": now}, "id=?", (uid,))
        db.insert(
            "balance_logs",
            {
                "user_id": uid,
                "change_cents": change,
                "balance_after": after,
                "kind": "adjust",
                "ref_type": "manual",
                "ref_id": str(req.user_id),
                "remark": remark,
                "created_at": now,
            },
        )
    _audit(req, "admin_adjust_balance", target=str(user["username"]),
           detail=f"{before} -> {after}（{change:+d} 分）备注={remark}")
    logmod.notify(uid, "余额变动", f"余额 {change:+d} 分（{change / 100:+.2f} 元），当前 {after / 100:.2f} 元")
    return ok({"user_id": uid, "balance_before": before, "balance_after": after, "change_cents": change})


@route("DELETE", "/api/v1/admin/users/<user_id>", auth="admin", summary="删除用户（连带数据）", tags=("admin", "users"))
def admin_delete_user(req: Request, user_id: str) -> Response:
    uid = _pid(user_id, "用户 ID")
    user = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HttpError(404, "用户不存在")
    if uid == req.user_id:
        raise HttpError(400, "不能删除自己的账号")
    if user["role"] == "admin" and int(db.query_scalar(
        "SELECT COUNT(*) FROM users WHERE role='admin'", (), 0
    ) or 0) <= 1:
        raise HttpError(409, "系统必须保留至少一个管理员", code="conflict")

    force = req.bool_field("force", False)
    financial = {
        "订单": int(db.query_scalar("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,), 0) or 0),
        "兑换记录": int(db.query_scalar("SELECT COUNT(*) FROM card_redemptions WHERE user_id=?", (uid,), 0) or 0),
        "余额流水": int(db.query_scalar("SELECT COUNT(*) FROM balance_logs WHERE user_id=?", (uid,), 0) or 0),
    }
    existing = {k: v for k, v in financial.items() if v}
    if existing and not force:
        summary = "、".join(f"{k} {v} 条" for k, v in existing.items())
        raise HttpError(
            409,
            f"该用户存在财务记录（{summary}）。默认保留以便对账，请改用「禁用」；"
            "确实要彻底删除请传 force=true",
            code="conflict",
        )
    with db.transaction():
        if force:
            # 财务表没有 ON DELETE CASCADE，force 删除时按依赖顺序手工清理
            db.delete("card_redemptions", "user_id=?", (uid,))
            db.delete("balance_logs", "user_id=?", (uid,))
            db.delete("orders", "user_id=?", (uid,))
            db.delete("sessions", "user_id=?", (uid,))
            db.delete("notifications", "user_id=?", (uid,))
        db.delete("users", "id=?", (uid,))
    _audit(req, "admin_user_delete", target=str(user["username"]),
           detail=f"force={force} 清理财务记录={existing or '无'}")
    return ok({"user_id": uid, "deleted": True, "forced": force})


# ==========================================================================
# 3. 卡密管理
# ==========================================================================
@route("GET", "/api/v1/admin/card-keys", auth="admin", summary="卡密列表", tags=("admin", "cards"))
def admin_card_list(req: Request) -> Response:
    page, page_size = req.page_args()
    package_id = req.field("package_id")
    result = cardkeys.list_cards(
        page,
        page_size,
        keyword=req.str_field("keyword", "", max_len=64),
        status=req.str_field("status", "", max_len=16),
        card_type=req.str_field("card_type", "", max_len=16),
        batch_no=req.str_field("batch_no", "", max_len=40),
        package_id=int(package_id) if package_id else None,
        start_date=req.str_field("start_date", "", max_len=20),
        end_date=req.str_field("end_date", "", max_len=20),
    )
    return ok(result)


@route("GET", "/api/v1/admin/card-keys/stats", auth="admin", summary="卡密统计", tags=("admin", "cards"))
def admin_card_stats(req: Request) -> Response:
    return ok(cardkeys.card_stats())


@route("POST", "/api/v1/admin/card-keys/generate", auth="admin", summary="批量生成卡密", tags=("admin", "cards"))
def admin_card_generate(req: Request) -> Response:
    card_type = req.str_field("card_type", "time", max_len=16) or "time"
    bound = req.field("bound_user_id")
    result = cardkeys.generate_cards(
        package_code=req.str_field("package_code", "", max_len=32)
        or req.str_field("package", "monthly", max_len=32),
        card_type=card_type,
        days=req.int_field("days", 30, minimum=0, maximum=36500),
        count=req.int_field("count", 10, minimum=1, maximum=cardkeys.MAX_BATCH_SIZE),
        use_limit=req.int_field("use_limit", 1, minimum=1, maximum=1000),
        amount_cents=req.int_field("amount_cents", 0, minimum=0, maximum=100_000_000),
        batch_note=req.str_field("note", "", max_len=200) or req.str_field("batch_note", "", max_len=200),
        created_by=req.user_id,
        prefix=req.str_field("prefix", "", max_len=8),
        batch_no=req.str_field("batch_no", "", max_len=40),
        bound_user_id=int(bound) if bound else None,
        expire_days=req.int_field("expire_days", 0, minimum=0, maximum=3650),
    )
    _audit(req, "admin_card_generate", target=result["batch_no"],
           detail=f"{result['card_type']} x{result['count']} 权益={result['benefit_text']}")
    return ok(result, f"已生成 {result['count']} 张卡密")


@route(
    "POST",
    "/api/v1/admin/card-keys/<card_id>/status",
    auth="admin",
    summary="启用/停用卡密",
    tags=("admin", "cards"),
)
def admin_card_status(req: Request, card_id: str) -> Response:
    cid = _pid(card_id, "卡密 ID")
    status = req.str_field("status", "", max_len=16) or req.str_field("action", "", max_len=16)
    if status in ("enable", "active", "1", "true"):
        status = "unused"
    elif status in ("disable", "disabled", "0", "false"):
        status = "disabled"
    card = cardkeys.change_card_status(cid, status, req.user, req.client_ip)
    _audit(req, "admin_card_status", target=card["code"], detail=f"status={status}")
    return ok(card, "卡密状态已更新")


@route("DELETE", "/api/v1/admin/card-keys/<card_id>", auth="admin", summary="删除未使用卡密", tags=("admin", "cards"))
def admin_card_delete(req: Request, card_id: str) -> Response:
    cid = _pid(card_id, "卡密 ID")
    result = cardkeys.delete_card(cid, req.user, req.client_ip)
    _audit(req, "admin_card_delete", target=str(cid), detail="删除未使用卡密")
    return ok(result, "卡密已删除")


@route("POST", "/api/v1/admin/card-keys/export", auth="admin", summary="导出卡密（txt/csv）", tags=("admin", "cards"))
def admin_card_export(req: Request) -> Response:
    result = cardkeys.export_cards(
        fmt=req.str_field("format", "csv", max_len=8) or req.str_field("fmt", "csv", max_len=8),
        batch_no=req.str_field("batch_no", "", max_len=40),
        status=req.str_field("status", "", max_len=16),
        card_type=req.str_field("card_type", "", max_len=16),
        keyword=req.str_field("keyword", "", max_len=64),
        limit=req.int_field("limit", 5000, minimum=1, maximum=50000),
    )
    _audit(req, "admin_card_export", target=result["filename"], detail=f"导出 {result['count']} 条")
    return ok(result)


# ==========================================================================
# 4. 套餐管理
# ==========================================================================
@route("GET", "/api/v1/admin/packages", auth="admin", summary="套餐列表（含停用）", tags=("admin", "packages"))
def admin_packages(req: Request) -> Response:
    rows = db.query_all("SELECT * FROM packages ORDER BY sort_order, id")
    items = []
    for row in rows:
        data = package_public(row)
        data["order_count"] = int(db.query_scalar(
            "SELECT COUNT(*) FROM orders WHERE package_id=?", (row["id"],), 0) or 0)
        data["active_user_count"] = int(db.query_scalar(
            "SELECT COUNT(*) FROM users WHERE package_id=?", (row["id"],), 0) or 0)
        items.append(data)
    return ok({"items": items})


def _package_payload(req: Request, *, is_create: bool) -> dict:
    """套餐创建/整体更新共用的字段解析（PUT 不允许改 code，它是稳定标识）。"""
    data: dict[str, Any] = {"updated_at": timeutil.utc_now_str()}
    if is_create:
        code = req.str_field("code", "", max_len=32).strip()
        if not re.match(r"^[a-z0-9_\-]{2,32}$", code):
            raise HttpError(400, "套餐 code 需为 2~32 位小写字母、数字、下划线或短横线")
        data["code"] = code
    name = req.str_field("name", "", max_len=64).strip()
    if not name:
        raise HttpError(400, "套餐名称不能为空")
    data["name"] = name
    for field_name, column, minimum, maximum, default in (
        ("days", "days", 1, 36500, 30),
        ("price_cents", "price_cents", 0, 100_000_000, 0),
        ("account_limit", "account_limit", 0, 1000, 1),
        ("daily_task_limit", "daily_task_limit", 0, 1000, 1),
        ("sort_order", "sort_order", -9999, 9999, 0),
    ):
        data[column] = req.int_field(field_name, default, minimum=minimum, maximum=maximum)
    raw_original = req.field("original_cents")
    data["original_cents"] = (
        req.int_field("original_cents", 0, minimum=0) if raw_original not in (None, "") else None
    )
    data["description"] = req.str_field("description", "", max_len=500)
    data["features_json"] = textutil.dumps(
        textutil.as_str_list(req.field("features") or req.field("features_json"))
    )
    data["is_active"] = 1 if req.bool_field("is_active", True) else 0
    return data


@route("POST", "/api/v1/admin/packages", auth="admin", summary="新建套餐", tags=("admin", "packages"))
def admin_create_package(req: Request) -> Response:
    data = _package_payload(req, is_create=True)
    data["created_at"] = timeutil.utc_now_str()
    if db.query_one("SELECT id FROM packages WHERE code=?", (data["code"],)):
        raise HttpError(409, "该套餐 code 已存在", code="conflict")
    package_id = db.insert("packages", data)
    _audit(req, "admin_package_create", target=data["code"],
           detail=f"名称={data['name']} 天数={data['days']} 价格={data['price_cents']}")
    row = db.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
    return ok(package_public(row or {}), "套餐已创建")


@route("PUT", "/api/v1/admin/packages/<package_id>", auth="admin", summary="修改套餐", tags=("admin", "packages"))
def admin_update_package(req: Request, package_id: str) -> Response:
    pid = _pid(package_id, "套餐 ID")
    current = db.query_one("SELECT * FROM packages WHERE id=?", (pid,))
    if not current:
        raise HttpError(404, "套餐不存在")
    data = _package_payload(req, is_create=False)
    db.update("packages", data, "id=?", (pid,))
    _audit(req, "admin_package_update", target=current["code"],
           detail=f"天数={data.get('days')} 价格={data.get('price_cents')} 启用={data.get('is_active')}")
    row = db.query_one("SELECT * FROM packages WHERE id=?", (pid,))
    return ok(package_public(row or {}), "套餐已保存")


@route("DELETE", "/api/v1/admin/packages/<package_id>", auth="admin", summary="删除套餐（有订单时改为停用）", tags=("admin", "packages"))
def admin_delete_package(req: Request, package_id: str) -> Response:
    pid = _pid(package_id, "套餐 ID")
    current = db.query_one("SELECT * FROM packages WHERE id=?", (pid,))
    if not current:
        raise HttpError(404, "套餐不存在")
    orders = int(db.query_scalar("SELECT COUNT(*) FROM orders WHERE package_id=?", (pid,), 0) or 0)
    cards = int(db.query_scalar("SELECT COUNT(*) FROM card_keys WHERE package_id=?", (pid,), 0) or 0)
    users = int(db.query_scalar("SELECT COUNT(*) FROM users WHERE package_id=?", (pid,), 0) or 0)
    if orders or cards or users:
        db.update("packages", {"is_active": 0, "updated_at": timeutil.utc_now_str()}, "id=?", (pid,))
        _audit(req, "admin_package_disable", target=current["code"],
               detail=f"有引用（订单 {orders} / 卡密 {cards} / 用户 {users}），改为停用")
        return ok(
            {"id": pid, "deleted": False, "disabled": True, "orders": orders, "cards": cards, "users": users},
            "该套餐已被订单/卡密/用户引用，已改为「停用」而不是删除",
        )
    db.delete("packages", "id=?", (pid,))
    _audit(req, "admin_package_delete", target=current["code"], detail="删除无引用套餐")
    return ok({"id": pid, "deleted": True})


# ==========================================================================
# 5. 订单管理
# ==========================================================================
@route("GET", "/api/v1/admin/orders", auth="admin", summary="订单列表", tags=("admin", "orders"))
def admin_orders(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    status = req.str_field("status", "", max_len=16)
    channel = req.str_field("channel", "", max_len=16)
    user_id = _int_filter(req.field("user_id"), "用户 ID")
    start_date = req.str_field("start_date", "", max_len=20)
    end_date = req.str_field("end_date", "", max_len=20)

    sql = ORDER_ADMIN_SELECT
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(o.order_no LIKE ? OR o.subject LIKE ? OR u.username LIKE ? OR o.trade_no LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    if status:
        where.append("o.status=?")
        params.append(status)
    if channel:
        where.append("o.channel=?")
        params.append(channel)
    if user_id:
        where.append("o.user_id=?")
        params.append(user_id)
    if start_date:
        where.append("o.created_at>=?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("o.created_at<=?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC"

    result = db.paginate(sql, params, page, page_size)
    result["items"] = [cardkeys.order_public(row, with_user=True) for row in result["items"]]
    summary = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='paid' THEN payable_cents ELSE 0 END),0) AS rev"
        " FROM orders"
    ) or {}
    result["summary"] = {
        "total": int(summary.get("n") or 0),
        "revenue_cents": int(summary.get("rev") or 0),
    }
    return ok(result)


@route(
    "POST",
    "/api/v1/admin/orders/<order_no>/confirm",
    auth="admin",
    summary="人工确认收款（补单）",
    tags=("admin", "orders"),
)
def admin_confirm_order(req: Request, order_no: str) -> Response:
    order = db.query_one("SELECT * FROM orders WHERE order_no=?", (order_no,))
    if not order:
        raise HttpError(404, "订单不存在")
    trade_no = req.str_field("trade_no", "", max_len=64)
    remark = req.str_field("remark", "", max_len=200) or "管理员人工确认收款"
    channel = req.str_field("channel", "", max_len=16)
    result = cardkeys.fulfill_order(
        order_no,
        trade_no=trade_no,
        operator=req.user,
        channel=channel,
        remark=remark,
    )
    _audit(
        req,
        "admin_order_confirm",
        target=order_no,
        detail=(
            f"金额={result['order'].get('payable_cents')} 天数={result['days_added']} "
            f"交易号={trade_no or '-'} 幂等命中={result['already_paid']}"
        ),
    )
    message = "订单已是已支付状态（幂等，未重复发放）" if result["already_paid"] else "已确认收款并发放套餐"
    return ok(
        {
            "order": cardkeys.order_public(result["order"], with_user=True),
            "already_paid": result["already_paid"],
            "days_added": result["days_added"],
            "expire_before": result["expire_before"],
            "expire_after": result["expire_after"],
            "balance_paid_cents": result["balance_paid_cents"],
        },
        message,
    )


@route(
    "POST",
    "/api/v1/admin/orders/<order_no>/refund",
    auth="admin",
    summary="标记退款（记录，不自动回退第三方通道）",
    tags=("admin", "orders"),
)
def admin_refund_order(req: Request, order_no: str) -> Response:
    order = db.query_one("SELECT * FROM orders WHERE order_no=?", (order_no,))
    if not order:
        raise HttpError(404, "订单不存在")
    if order["status"] != "paid":
        raise HttpError(409, f"只有已支付订单能退款（当前 {order['status']}）", code="conflict")
    reason = req.str_field("reason", "", max_len=200) or "管理员标记退款"
    refund_balance = req.bool_field("refund_balance", order["channel"] == "balance")
    now = timeutil.utc_now_str()
    refunded_cents = 0
    with db.transaction():
        if refund_balance and order["channel"] == "balance" and int(order["payable_cents"] or 0) > 0:
            user = db.query_one("SELECT * FROM users WHERE id=?", (order["user_id"],))
            if user:
                amount = int(order["payable_cents"])
                after = int(user["balance_cents"] or 0) + amount
                db.update("users", {"balance_cents": after, "updated_at": now}, "id=?", (user["id"],))
                db.insert(
                    "balance_logs",
                    {
                        "user_id": user["id"],
                        "change_cents": amount,
                        "balance_after": after,
                        "kind": "refund",
                        "ref_type": "order",
                        "ref_id": order_no,
                        "remark": f"订单退款退回余额：{reason}",
                        "created_at": now,
                    },
                )
                refunded_cents = amount
        db.update(
            "orders",
            {"status": "refunded", "updated_at": now, "remark": f"{order.get('remark') or ''} | 退款：{reason}".strip(" |")[:400]},
            "order_no=?",
            (order_no,),
        )
    _audit(req, "admin_order_refund", target=order_no,
           detail=f"原因={reason} 退回余额={refunded_cents} 分（第三方通道未自动回退）")
    logmod.notify(order["user_id"], "订单已退款", f"订单 {order_no} 已标记退款：{reason}")
    return ok({"order_no": order_no, "refunded": True, "balance_refunded_cents": refunded_cents},
              "已标记退款（第三方通道需自行处理）")


# ==========================================================================
# 6. 抖音账号总览
# ==========================================================================
def _account_admin(row: dict) -> dict:
    """账号视图：**绝不返回 storage_path**（登录态文件泄露等于账号被接管）。"""
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "username": row.get("username") or "",
        "user_nickname": row.get("user_nickname") or "",
        "nickname": row.get("nickname") or "",
        "douyin_uid": row.get("douyin_uid") or "",
        "avatar": row.get("avatar") or "",
        "phone_masked": row.get("phone_masked") or "",
        "remark": row.get("remark") or "",
        "status": row.get("status"),
        "enabled": bool(row.get("enabled", 1)),
        "login_type": row.get("login_type") or "qrcode",
        "proxy_id": row.get("proxy_id"),
        "proxy_name": row.get("proxy_name") or "",
        "has_session": bool(row.get("storage_path")),
        "storage_updated_at": row.get("storage_updated_at") or "",
        "last_check_at": row.get("last_check_at") or "",
        "last_error": row.get("last_error") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


@route("GET", "/api/v1/admin/douyin-accounts", auth="admin", summary="全部抖音账号", tags=("admin", "accounts"))
def admin_accounts(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    status = req.str_field("status", "", max_len=16)
    user_id = _int_filter(req.field("user_id"), "用户 ID")

    sql = (
        "SELECT a.*, u.username AS username, u.nickname AS user_nickname, p.name AS proxy_name"
        " FROM douyin_accounts a LEFT JOIN users u ON u.id=a.user_id"
        " LEFT JOIN proxies p ON p.id=a.proxy_id"
    )
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(a.nickname LIKE ? OR a.douyin_uid LIKE ? OR u.username LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if status:
        if status not in ACCOUNT_STATUSES:
            raise HttpError(400, "账号状态取值不正确")
        where.append("a.status=?")
        params.append(status)
    if user_id:
        where.append("a.user_id=?")
        params.append(user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id DESC"
    result = db.paginate(sql, params, page, page_size)
    result["items"] = [_account_admin(row) for row in result["items"]]
    return ok(result)


@route(
    "POST",
    "/api/v1/admin/douyin-accounts/<account_id>/status",
    auth="admin",
    summary="修改账号状态/启用开关",
    tags=("admin", "accounts"),
)
def admin_account_status(req: Request, account_id: str) -> Response:
    aid = _pid(account_id, "账号 ID")
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (aid,))
    if not account:
        raise HttpError(404, "账号不存在")
    update: dict[str, Any] = {"updated_at": timeutil.utc_now_str()}
    detail: list[str] = []
    if req.field("status") is not None:
        status = req.str_field("status", "", max_len=16)
        if status not in ACCOUNT_STATUSES:
            raise HttpError(400, "账号状态取值不正确")
        update["status"] = status
        detail.append(f"status={account['status']}->{status}")
    if req.field("enabled") is not None:
        update["enabled"] = 1 if req.bool_field("enabled", True) else 0
        detail.append(f"enabled={bool(update['enabled'])}")
    if req.field("remark") is not None:
        update["remark"] = req.str_field("remark", "", max_len=200)
        detail.append("remark 已更新")
    if len(update) == 1:
        raise HttpError(400, "请提供 status / enabled / remark 中至少一项")
    db.update("douyin_accounts", update, "id=?", (aid,))
    _audit(req, "admin_account_status", target=f"account#{aid}", detail=" ".join(detail))
    row = db.query_one(
        "SELECT a.*, u.username AS username, u.nickname AS user_nickname, p.name AS proxy_name"
        " FROM douyin_accounts a LEFT JOIN users u ON u.id=a.user_id"
        " LEFT JOIN proxies p ON p.id=a.proxy_id WHERE a.id=?",
        (aid,),
    )
    return ok(_account_admin(row or {}), "账号已更新")


# ==========================================================================
# 7. 定时任务与执行记录
# ==========================================================================
def _task_admin(row: dict) -> dict:
    gap = textutil.as_min_max(row.get("send_gap_seconds"))
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "username": row.get("username") or "",
        "account_id": row.get("account_id"),
        "account_nickname": row.get("account_nickname") or "",
        "name": row.get("name") or "",
        "task_type": row.get("task_type") or "spark",
        "enabled": bool(row.get("enabled")),
        "time_of_day": row.get("time_of_day") or "",
        "repeat_mode": row.get("repeat_mode") or "daily",
        "weekdays": textutil.as_int_list(row.get("weekdays")),
        "interval_minutes": int(row.get("interval_minutes") or 0),
        "jitter_seconds": int(row.get("jitter_seconds") or 0),
        "timezone": row.get("timezone") or "",
        "friend_mode": row.get("friend_mode") or "all",
        "friend_ids": textutil.as_int_list(row.get("friend_ids")),
        "messages": textutil.as_str_list(row.get("messages_json")),
        "send_gap_seconds": {"min": gap[0], "max": gap[1]},
        "daily_limit": int(row.get("daily_limit") or 0),
        "max_retry": int(row.get("max_retry") or 0),
        "last_run_at": row.get("last_run_at") or "",
        "last_status": row.get("last_status") or "",
        "next_run_at": row.get("next_run_at") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _run_admin(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "task_id": row.get("task_id"),
        "task_name": row.get("task_name") or "",
        "user_id": row.get("user_id"),
        "username": row.get("username") or "",
        "account_id": row.get("account_id"),
        "account_nickname": row.get("account_nickname") or "",
        "run_date": row.get("run_date") or "",
        "trigger_type": row.get("trigger_type") or "",
        "status": row.get("status") or "",
        "started_at": row.get("started_at") or "",
        "finished_at": row.get("finished_at") or "",
        "duration_ms": int(row.get("duration_ms") or 0),
        "total_count": int(row.get("total_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "fail_count": int(row.get("fail_count") or 0),
        "message": row.get("message") or "",
        "error_code": row.get("error_code") or "",
        "created_at": row.get("created_at") or "",
    }


TASK_ADMIN_SELECT = (
    "SELECT t.*, u.username AS username, a.nickname AS account_nickname"
    " FROM scheduled_tasks t LEFT JOIN users u ON u.id=t.user_id"
    " LEFT JOIN douyin_accounts a ON a.id=t.account_id"
)

RUN_ADMIN_SELECT = (
    "SELECT r.*, u.username AS username, a.nickname AS account_nickname, t.name AS task_name"
    " FROM task_runs r LEFT JOIN users u ON u.id=r.user_id"
    " LEFT JOIN douyin_accounts a ON a.id=r.account_id"
    " LEFT JOIN scheduled_tasks t ON t.id=r.task_id"
)


@route("GET", "/api/v1/admin/scheduled-tasks", auth="admin", summary="全部定时任务", tags=("admin", "tasks"))
def admin_scheduled_tasks(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    enabled = req.field("enabled")
    user_id = _int_filter(req.field("user_id"), "用户 ID")
    account_id = _int_filter(req.field("account_id"), "账号 ID")

    sql = TASK_ADMIN_SELECT
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(t.name LIKE ? OR u.username LIKE ? OR a.nickname LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if enabled is not None and str(enabled) != "":
        where.append("t.enabled=?")
        params.append(1 if str(enabled).lower() in ("1", "true", "yes", "on") else 0)
    if user_id:
        where.append("t.user_id=?")
        params.append(user_id)
    if account_id:
        where.append("t.account_id=?")
        params.append(account_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.id DESC"
    result = db.paginate(sql, params, page, page_size)
    result["items"] = [_task_admin(row) for row in result["items"]]
    return ok(result)


def _tasks_batch(req: Request) -> Response:
    ids = textutil.as_int_list(req.field("ids") or req.field("task_ids") or [])
    action = (req.str_field("action", "", max_len=16) or "").lower()
    if not ids:
        raise HttpError(400, "请提供要操作的任务 ids")
    if action not in ("enable", "disable", "delete"):
        raise HttpError(400, "action 只能是 enable / disable / delete")
    if len(ids) > 500:
        raise HttpError(400, "单次最多操作 500 个任务")
    placeholders = ",".join("?" for _ in ids)
    now = timeutil.utc_now_str()
    if action == "delete":
        affected = db.delete("scheduled_tasks", f"id IN ({placeholders})", ids)
    else:
        affected = db.update(
            "scheduled_tasks",
            {"enabled": 1 if action == "enable" else 0, "updated_at": now},
            f"id IN ({placeholders})",
            ids,
        )
    _audit(req, f"admin_tasks_{action}", target=f"{len(ids)} 个任务",
           detail=f"实际影响 {affected} 条 ids={ids[:50]}")
    return ok({"action": action, "requested": len(ids), "affected": affected},
              f"已{'删除' if action == 'delete' else ('启用' if action == 'enable' else '停用')} {affected} 个任务")


@route("POST", "/api/v1/admin/scheduled-tasks/batch", auth="admin", summary="批量启用/停用/删除任务", tags=("admin", "tasks"))
def admin_tasks_batch(req: Request) -> Response:
    return _tasks_batch(req)


@route(
    "POST",
    "/api/v1/admin/task-manage/scheduled-tasks/batch",
    auth="admin",
    summary="批量任务操作（兼容目标站路径）",
    tags=("admin", "tasks"),
)
def admin_tasks_batch_alias(req: Request) -> Response:
    return _tasks_batch(req)


@route("POST", "/api/v1/admin/tasks/<task_id>/run", auth="admin", summary="管理员代跑一次", tags=("admin", "tasks"))
def admin_run_task(req: Request, task_id: str) -> Response:
    tid = _pid(task_id, "任务 ID")
    task = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (tid,))
    if not task:
        raise HttpError(404, "任务不存在")
    runner = _optional_module("app.worker.runner")
    if runner is None or not hasattr(runner, "enqueue_run"):
        raise HttpError(
            503,
            "自动化执行引擎未就绪（app.worker.runner.enqueue_run 不可用）：请在装配了"
            "自动化模块的部署环境执行，或用命令行 python main.py task --account-id <id>",
            code="server_error",
        )
    dry_run = req.bool_field("dry_run", False)
    outcome = runner.enqueue_run(
        account_id=int(task["account_id"]),
        task_id=tid,
        trigger_type="manual",
        dry_run=dry_run,
    )
    _audit(req, "admin_task_run", target=f"task#{tid}",
           detail=f"account={task['account_id']} dry_run={dry_run} -> {outcome}")
    return ok(outcome, "已提交执行")


def _task_records(req: Request) -> Response:
    page, page_size = req.page_args()
    status = req.str_field("status", "", max_len=16)
    user_id = _int_filter(req.field("user_id"), "用户 ID")
    account_id = _int_filter(req.field("account_id"), "账号 ID")
    task_id = _int_filter(req.field("task_id"), "任务 ID")
    start_date = req.str_field("start_date", "", max_len=20)
    end_date = req.str_field("end_date", "", max_len=20)
    keyword = req.str_field("keyword", "", max_len=64)

    sql = RUN_ADMIN_SELECT
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("r.status=?")
        params.append(status)
    if user_id:
        where.append("r.user_id=?")
        params.append(user_id)
    if account_id:
        where.append("r.account_id=?")
        params.append(account_id)
    if task_id:
        where.append("r.task_id=?")
        params.append(task_id)
    if start_date:
        where.append("r.run_date>=?")
        params.append(start_date)
    if end_date:
        where.append("r.run_date<=?")
        params.append(end_date)
    if keyword:
        where.append("(a.nickname LIKE ? OR u.username LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC"
    result = db.paginate(sql, params, page, page_size)
    result["items"] = [_run_admin(row) for row in result["items"]]
    return ok(result)


@route("GET", "/api/v1/admin/tasks", auth="admin", summary="任务执行记录（全站）", tags=("admin", "records"))
def admin_task_records(req: Request) -> Response:
    return _task_records(req)


@route("GET", "/api/v1/admin/task-records", auth="admin", summary="任务执行记录（别名）", tags=("admin", "records"))
def admin_task_records_alias(req: Request) -> Response:
    return _task_records(req)


# ==========================================================================
# 8. 日志
# ==========================================================================
@route("GET", "/api/v1/admin/logs", auth="admin", summary="运行日志", tags=("admin", "logs"))
def admin_logs(req: Request) -> Response:
    page, page_size = req.page_args()
    scope = req.str_field("scope", "", max_len=16)
    level = req.str_field("level", "", max_len=16)
    keyword = req.str_field("keyword", "", max_len=128)
    user_id = _int_filter(req.field("user_id"), "用户 ID")
    start_date = req.str_field("start_date", "", max_len=20)
    end_date = req.str_field("end_date", "", max_len=20)

    sql = ("SELECT l.*, u.username AS username FROM run_logs l"
           " LEFT JOIN users u ON u.id=l.user_id")
    where: list[str] = []
    params: list[Any] = []
    if scope:
        if scope not in LOG_SCOPES:
            raise HttpError(400, "scope 取值不正确")
        where.append("l.scope=?")
        params.append(scope)
    if level:
        if level not in LOG_LEVELS:
            raise HttpError(400, "level 取值不正确")
        where.append("l.level=?")
        params.append(level)
    if keyword:
        where.append("(l.message LIKE ? OR l.source LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])
    if user_id:
        where.append("l.user_id=?")
        params.append(user_id)
    if start_date:
        where.append("l.created_at>=?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("l.created_at<=?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.id DESC"
    return ok(db.paginate(sql, params, page, page_size))


@route("GET", "/api/v1/admin/logs/admin", auth="admin", summary="管理员操作审计日志", tags=("admin", "logs"))
def admin_audit_logs(req: Request) -> Response:
    page, page_size = req.page_args()
    action = req.str_field("action", "", max_len=64)
    keyword = req.str_field("keyword", "", max_len=128)
    scope = req.str_field("scope", "", max_len=16)
    user_id = _int_filter(req.field("user_id"), "用户 ID")
    start_date = req.str_field("start_date", "", max_len=20)
    end_date = req.str_field("end_date", "", max_len=20)

    sql = "SELECT * FROM audit_logs"
    where: list[str] = []
    params: list[Any] = []
    if action:
        where.append("action=?")
        params.append(action)
    if scope:
        where.append("scope=?")
        params.append(scope)
    if keyword:
        where.append("(action LIKE ? OR target LIKE ? OR detail LIKE ? OR username LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    if user_id:
        where.append("user_id=?")
        params.append(user_id)
    if start_date:
        where.append("created_at>=?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("created_at<=?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return ok(db.paginate(sql, params, page, page_size))


@route("DELETE", "/api/v1/admin/logs", auth="admin", summary="清理日志（按天数）", tags=("admin", "logs"))
def admin_logs_cleanup(req: Request) -> Response:
    days = req.int_field("days", 30, minimum=1, maximum=3650)
    target = req.str_field("target", "all", max_len=16) or "all"
    cutoff = timeutil.to_db(timeutil.add_days(-days))
    removed_logs = 0
    removed_audit = 0
    if target in ("all", "run", "logs"):
        removed_logs = db.delete("run_logs", "created_at < ?", (cutoff,))
    if target in ("all", "audit"):
        removed_audit = db.delete("audit_logs", "created_at < ?", (cutoff,))
    if target == "all":
        # 顺带清掉过期验证码与已发队列，避免小机器上这些表无限增长
        db.delete("verify_codes", "expire_at < ?", (cutoff,))
        db.delete("message_outbox", "status='sent' AND created_at < ?", (cutoff,))
    _audit(req, "admin_logs_cleanup", target=target,
           detail=f"保留 {days} 天，删除运行日志 {removed_logs} 条 / 审计日志 {removed_audit} 条")
    return ok({"days": days, "target": target, "removed_run_logs": removed_logs, "removed_audit_logs": removed_audit})


# ==========================================================================
# 9. 公告
# ==========================================================================
def _announcement_payload(req: Request) -> dict:
    title = req.str_field("title", "", max_len=128).strip()
    if not title:
        raise HttpError(400, "公告标题不能为空")
    level = req.str_field("level", "info", max_len=16) or "info"
    if level not in ANNOUNCEMENT_LEVELS:
        raise HttpError(400, "公告级别只能是 info / warning / important")
    publish_at = req.str_field("publish_at", "", max_len=20)
    expire_at = req.str_field("expire_at", "", max_len=20)
    return {
        "title": title,
        "content": req.str_field("content", "", max_len=20000),
        "level": level,
        "is_pinned": 1 if req.bool_field("is_pinned", False) else 0,
        "is_active": 1 if req.bool_field("is_active", True) else 0,
        "publish_at": publish_at or None,
        "expire_at": expire_at or None,
        "updated_at": timeutil.utc_now_str(),
    }


@route("GET", "/api/v1/admin/announcements", auth="admin", summary="公告列表", tags=("admin", "announcements"))
def admin_announcements(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    is_active = req.field("is_active")
    sql = "SELECT a.*, u.username AS created_by_name FROM announcements a LEFT JOIN users u ON u.id=a.created_by"
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(a.title LIKE ? OR a.content LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])
    if is_active is not None and str(is_active) != "":
        where.append("a.is_active=?")
        params.append(1 if str(is_active).lower() in ("1", "true", "yes", "on") else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.is_pinned DESC, a.id DESC"
    return ok(db.paginate(sql, params, page, page_size))


@route("POST", "/api/v1/admin/announcements", auth="admin", summary="新建公告", tags=("admin", "announcements"))
def admin_create_announcement(req: Request) -> Response:
    data = _announcement_payload(req)
    now = timeutil.utc_now_str()
    data["created_by"] = req.user_id
    data["created_at"] = now
    announcement_id = db.insert("announcements", data)
    _audit(req, "admin_announcement_create", target=data["title"], detail=f"id={announcement_id}")
    row = db.query_one("SELECT * FROM announcements WHERE id=?", (announcement_id,))
    return ok(row, "公告已创建")


@route("PUT", "/api/v1/admin/announcements/<announcement_id>", auth="admin", summary="修改公告", tags=("admin", "announcements"))
def admin_update_announcement(req: Request, announcement_id: str) -> Response:
    aid = _pid(announcement_id, "公告 ID")
    if not db.query_one("SELECT id FROM announcements WHERE id=?", (aid,)):
        raise HttpError(404, "公告不存在")
    data = _announcement_payload(req)
    db.update("announcements", data, "id=?", (aid,))
    _audit(req, "admin_announcement_update", target=data["title"], detail=f"id={aid}")
    return ok(db.query_one("SELECT * FROM announcements WHERE id=?", (aid,)), "公告已保存")


@route("DELETE", "/api/v1/admin/announcements/<announcement_id>", auth="admin", summary="删除公告", tags=("admin", "announcements"))
def admin_delete_announcement(req: Request, announcement_id: str) -> Response:
    aid = _pid(announcement_id, "公告 ID")
    row = db.query_one("SELECT * FROM announcements WHERE id=?", (aid,))
    if not row:
        raise HttpError(404, "公告不存在")
    db.delete("announcements", "id=?", (aid,))
    _audit(req, "admin_announcement_delete", target=row["title"], detail=f"id={aid}")
    return ok({"id": aid, "deleted": True})


# ==========================================================================
# 10. 站点配置
# ==========================================================================
@route("GET", "/api/v1/admin/configs", auth="admin", summary="站点配置（分组返回）", tags=("admin", "configs"))
def admin_configs(req: Request) -> Response:
    reveal = req.bool_field("reveal", False)
    rows = db.query_all("SELECT * FROM site_configs ORDER BY group_name, key")
    items = [
        {
            "key": row["key"],
            "value": _mask_config_value(str(row["key"]), str(row["value"] or ""), reveal),
            "masked": bool(not reveal and SECRET_KEY_HINT.search(str(row["key"])) and row["value"]),
            "is_public": bool(row["is_public"]),
            "group": row["group_name"] or "general",
            "description": row["description"] or "",
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["group"], []).append(item)
    return ok(
        {
            "items": items,
            "groups": [{"group": name, "items": group_items} for name, group_items in sorted(groups.items())],
            "values": {item["key"]: item["value"] for item in items},
        }
    )


@route("PUT", "/api/v1/admin/configs", auth="admin", summary="批量保存配置", tags=("admin", "configs"))
def admin_save_configs(req: Request) -> Response:
    payload = req.json(required=True)
    if not isinstance(payload, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")

    # 三种入参都要收：前端表单、脚本调用、以及契约里写的列表形式，
    # 少收一种就会变成「前端按契约发、后端 400」这种最难查的对接事故（真发生过）。
    #   1) {"items": [{"key": "site.name", "value": "x", "is_public": 1}, ...]}  ← 契约写法
    #   2) {"configs": [...]}                                                    ← 兼容别名
    #   3) {"site.name": "x", "site.icp": "y"}                                   ← 扁平对象（脚本最方便）
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("configs")
    if raw is None:
        raw = {k: v for k, v in payload.items() if k not in ("items", "configs", "group")}
    if isinstance(raw, list):
        merged: dict = {}
        for item in raw:
            if isinstance(item, dict):
                key = str(item.get("key") or "").strip()
                if key:
                    merged[key] = item
        raw_items: dict = merged
    elif isinstance(raw, dict):
        raw_items = raw
    else:
        raw_items = {}
    if not raw_items:
        raise HttpError(400, "没有要保存的配置项")
    if len(raw_items) > 200:
        raise HttpError(400, "单次最多保存 200 项配置")

    changed: list[str] = []
    for key, value in raw_items.items():
        key = str(key).strip()
        if not re.match(r"^[a-zA-Z0-9_.\-]{2,64}$", key):
            raise HttpError(400, f"配置键不合法：{key}")
        # 列表写法允许每项单独带 is_public / group_name / description
        public: bool | None = None
        group = "general"
        description = ""
        if isinstance(value, dict):
            if value.get("is_public") is not None:
                public = bool(value.get("is_public"))
            group = str(value.get("group_name") or value.get("group") or "general")
            description = str(value.get("description") or "")
            value = value.get("value", "")
        text = "" if value is None else str(value)
        if text == MASKED:
            continue              # 前端把脱敏占位符回传了 -> 视为"不修改"
        before = boot.get_config(key, "")
        if before == text:
            # 值没变，但 public/分组信息可能变了（管理员改"是否公开"时会走到这里）
            if public is not None:
                boot.set_config(key, text, public=public, group=group, description=description)
                changed.append(f"{key}: 公开性 -> {'公开' if public else '私有'}")
            continue
        boot.set_config(key, text, public=public, group=group, description=description)
        changed.append(f"{key}: {_mask_config_value(key, before, False)} -> {_mask_config_value(key, text, False)}")
    if changed:
        _audit(req, "admin_config_save", target=f"{len(changed)} 项",
               detail="; ".join(changed)[:1800])
    return ok({"saved": len(changed), "changed": changed}, f"已保存 {len(changed)} 项配置")


# ==========================================================================
# 11. 代理管理
# ==========================================================================
def _proxy_public(row: dict, *, with_secret: bool = False) -> dict:
    data = {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "scheme": row.get("scheme") or "http",
        "host": row.get("host") or "",
        "port": int(row.get("port") or 0),
        "username": row.get("username") or "",
        "has_password": bool(row.get("password_enc")),
        "region": row.get("region") or "",
        "status": row.get("status") or "active",
        "last_check_at": row.get("last_check_at") or "",
        "last_check_ok": None if row.get("last_check_ok") is None else bool(row["last_check_ok"]),
        "remark": row.get("remark") or "",
        "account_count": int(row.get("account_count") or 0),
        "created_at": row.get("created_at") or "",
    }
    if with_secret:
        data["password"] = MASKED if row.get("password_enc") else ""
    return data


PROXY_SELECT = (
    "SELECT p.*, (SELECT COUNT(*) FROM douyin_accounts a WHERE a.proxy_id=p.id) AS account_count"
    " FROM proxies p"
)
SCHEMES = ("http", "https", "socks5")


def _validate_proxy(body: dict, *, partial: bool) -> dict:
    data: dict[str, Any] = {}
    if not partial or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise HttpError(400, "代理名称不能为空")
        data["name"] = name[:64]
    if not partial or "scheme" in body:
        scheme = str(body.get("scheme") or "http").strip().lower()
        if scheme not in SCHEMES:
            raise HttpError(400, "协议只能是 http / https / socks5")
        data["scheme"] = scheme
    if not partial or "host" in body:
        host = str(body.get("host") or "").strip()
        if not host or len(host) > 190:
            raise HttpError(400, "代理主机不能为空")
        data["host"] = host
    if not partial or "port" in body:
        try:
            port = int(body.get("port") or 0)
        except (TypeError, ValueError):
            raise HttpError(400, "代理端口必须是整数") from None
        if port < 1 or port > 65535:
            raise HttpError(400, "代理端口需在 1~65535 之间")
        data["port"] = port
    for field_name in ("username", "region", "remark"):
        if not partial or field_name in body:
            data[field_name] = str(body.get(field_name) or "")[:190]
    if not partial or "password" in body:
        password = str(body.get("password") or "")
        if password == MASKED:
            password = ""          # 占位符 = 不修改
        if password:
            data["password_enc"] = encrypt(password)
    if not partial or "status" in body:
        status = str(body.get("status") or "active").strip().lower()
        if status not in ("active", "disabled", "dead"):
            raise HttpError(400, "代理状态只能是 active / disabled / dead")
        data["status"] = status
    return data


@route("GET", "/api/v1/admin/proxies", auth="admin", summary="代理列表", tags=("admin", "proxies"))
def admin_proxies(req: Request) -> Response:
    page, page_size = req.page_args()
    keyword = req.str_field("keyword", "", max_len=64)
    status = req.str_field("status", "", max_len=16)
    sql = PROXY_SELECT
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(p.name LIKE ? OR p.host LIKE ? OR p.region LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if status:
        where.append("p.status=?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.id DESC"
    result = db.paginate(sql, params, page, page_size)
    result["items"] = [_proxy_public(row, with_secret=True) for row in result["items"]]
    return ok(result)


@route("POST", "/api/v1/admin/proxies", auth="admin", summary="新增代理", tags=("admin", "proxies"))
def admin_create_proxy(req: Request) -> Response:
    body = req.json(required=True)
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")
    data = _validate_proxy(body, partial=False)
    data["status"] = data.get("status", "active")
    data["created_at"] = timeutil.utc_now_str()
    proxy_id = db.insert("proxies", data)
    _audit(req, "admin_proxy_create", target=f"{data.get('scheme')}://{data.get('host')}:{data.get('port')}",
           detail=f"id={proxy_id} name={data.get('name')}")
    row = db.query_one(PROXY_SELECT + " WHERE p.id=?", (proxy_id,))
    return ok(_proxy_public(row or {}, with_secret=True), "代理已添加")


@route("PUT", "/api/v1/admin/proxies/<proxy_id>", auth="admin", summary="修改代理", tags=("admin", "proxies"))
def admin_update_proxy(req: Request, proxy_id: str) -> Response:
    pid = _pid(proxy_id, "代理 ID")
    current = db.query_one("SELECT * FROM proxies WHERE id=?", (pid,))
    if not current:
        raise HttpError(404, "代理不存在")
    body = req.json(required=True)
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")
    data = _validate_proxy(body, partial=True)
    if data:
        db.update("proxies", data, "id=?", (pid,))
    _audit(req, "admin_proxy_update", target=f"proxy#{pid}", detail=f"字段={list(data.keys())}")
    row = db.query_one(PROXY_SELECT + " WHERE p.id=?", (pid,))
    return ok(_proxy_public(row or {}, with_secret=True), "代理已保存")


@route("DELETE", "/api/v1/admin/proxies/<proxy_id>", auth="admin", summary="删除代理", tags=("admin", "proxies"))
def admin_delete_proxy(req: Request, proxy_id: str) -> Response:
    pid = _pid(proxy_id, "代理 ID")
    current = db.query_one("SELECT * FROM proxies WHERE id=?", (pid,))
    if not current:
        raise HttpError(404, "代理不存在")
    used = int(db.query_scalar("SELECT COUNT(*) FROM douyin_accounts WHERE proxy_id=?", (pid,), 0) or 0)
    with db.transaction():
        if used:
            # 外键没有 ON DELETE SET NULL，先把引用解开再删，避免删不掉还报外键错
            db.update("douyin_accounts", {"proxy_id": None, "updated_at": timeutil.utc_now_str()},
                      "proxy_id=?", (pid,))
        db.delete("proxies", "id=?", (pid,))
    _audit(req, "admin_proxy_delete", target=f"proxy#{pid}",
           detail=f"解绑账号 {used} 个 name={current['name']}")
    return ok({"id": pid, "deleted": True, "unbound_accounts": used})


@route("POST", "/api/v1/admin/proxies/<proxy_id>/check", auth="admin", summary="测试代理连通性", tags=("admin", "proxies"))
def admin_check_proxy(req: Request, proxy_id: str) -> Response:
    pid = _pid(proxy_id, "代理 ID")
    proxy = db.query_one("SELECT * FROM proxies WHERE id=?", (pid,))
    if not proxy:
        raise HttpError(404, "代理不存在")
    scheme = str(proxy["scheme"] or "http")
    reachable, error, latency_ms = _tcp_reachable(str(proxy["host"]), int(proxy["port"]))
    if scheme == "socks5":
        # 标准库没有 SOCKS5 客户端；这里只做 TCP 可达性，真正的代理握手由浏览器完成
        message = "TCP 可达（SOCKS5 握手由浏览器完成）" if reachable else error
    else:
        message = "连接成功" if reachable else error
    now = timeutil.utc_now_str()
    db.update(
        "proxies",
        {
            "last_check_at": now,
            "last_check_ok": 1 if reachable else 0,
            "status": "active" if reachable else "dead",
        },
        "id=?",
        (pid,),
    )
    _audit(req, "admin_proxy_check", target=f"proxy#{pid}", detail=message[:200])
    return ok(
        {
            "id": pid,
            "ok": reachable,
            "latency_ms": latency_ms,
            "message": message,
            "host": f"{proxy['host']}:{proxy['port']}",
        },
        message,
    )


# ==========================================================================
# 12. 系统信息 / 分销等级 / 测试工具
# ==========================================================================
@route("GET", "/api/v1/admin/system-environment", auth="admin", summary="环境体检 + 指标 + 心跳", tags=("admin", "system"))
def admin_system_environment(req: Request) -> Response:
    settings = get_settings()
    metrics = {}
    metrics_obj = getattr(req.server, "metrics", None)
    if metrics_obj is not None:
        try:
            metrics = metrics_obj.snapshot()
        except Exception:      # noqa: BLE001 - 指标读取失败不影响体检结果返回
            metrics = {}
    heartbeats = db.query_all("SELECT * FROM worker_heartbeats ORDER BY beat_at DESC LIMIT 20")
    counts = {
        "users": int(db.query_scalar("SELECT COUNT(*) FROM users", (), 0) or 0),
        "accounts": int(db.query_scalar("SELECT COUNT(*) FROM douyin_accounts", (), 0) or 0),
        "tasks": int(db.query_scalar("SELECT COUNT(*) FROM scheduled_tasks", (), 0) or 0),
        "cards": int(db.query_scalar("SELECT COUNT(*) FROM card_keys", (), 0) or 0),
        "orders": int(db.query_scalar("SELECT COUNT(*) FROM orders", (), 0) or 0),
    }
    pool = _optional_module("app.worker.pool")
    return ok(
        {
            "environment": boot.check_cloud_environment(),
            "database": db.health(),
            "metrics": metrics,
            "heartbeats": heartbeats,
            "counts": counts,
            "engine_ready": bool(pool),
            "settings": {
                "env": settings.env,
                "timezone": settings.timezone,
                "demo_mode": settings.demo_mode,
                "scheduler_enabled": settings.scheduler_enabled,
                "max_concurrent_browsers": settings.max_concurrent_browsers,
                "access_token_ttl": settings.access_token_ttl,
                "rate_limit_per_minute": settings.rate_limit_per_minute,
                "data_dir": str(settings.data_dir),
                "db_path": str(settings.db_path),
            },
        }
    )


@route("GET", "/api/v1/admin/browser-processes", auth="admin", summary="浏览器进程/并发占用", tags=("admin", "system"))
def admin_browser_processes(req: Request) -> Response:
    settings = get_settings()
    pool = _optional_module("app.worker.pool")
    if pool is None or not hasattr(pool, "snapshot"):
        return ok(
            {
                "engine_ready": False,
                "items": [],
                "active": 0,
                "limit": settings.max_concurrent_browsers,
                "message": "自动化引擎未装配（lite 版或未完成构建），没有浏览器进程",
            }
        )
    snapshot = pool.snapshot()
    if isinstance(snapshot, dict):
        data = dict(snapshot)
        items = data.get("items") or data.get("processes") or []
    else:
        items = list(snapshot or [])
        data = {}
    return ok(
        {
            "engine_ready": True,
            "items": items,
            "active": int(data.get("active") or len(items)),
            "limit": int(data.get("limit") or settings.max_concurrent_browsers),
            **{k: v for k, v in data.items() if k not in ("items", "processes", "active", "limit")},
        }
    )


@route(
    "POST",
    "/api/v1/admin/system/browser-processes/cleanup",
    auth="admin",
    summary="清理残留浏览器进程",
    tags=("admin", "system"),
)
def admin_cleanup_browser_processes(req: Request) -> Response:
    pool = _optional_module("app.worker.pool")
    cleaner = None
    for name in ("cleanup", "cleanup_stale", "kill_stale", "force_cleanup", "release_all"):
        if pool is not None and hasattr(pool, name):
            cleaner = getattr(pool, name)
            break
    if cleaner is None:
        raise HttpError(
            400,
            "自动化引擎未提供清理接口（app.worker.pool 未装配）：本机无需清理",
        )
    result = cleaner()
    _audit(req, "admin_browser_cleanup", target="browser-processes", detail=str(result)[:300])
    return ok({"result": result}, "已触发清理")


def _int_range(value: Any, label: str, minimum: int, maximum: int, default: int) -> int:
    """整数参数解析（带边界与 400，不让 "abc" 变成 500）。"""
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"{label}必须是整数") from None
    if number < minimum or number > maximum:
        raise HttpError(400, f"{label}需在 {minimum}~{maximum} 之间")
    return number


def _agent_level_public(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "level": int(row.get("level") or 1),
        "commission_rate": float(row.get("commission_rate") or 0),
        "card_discount": float(row.get("card_discount") or 1),
        "daily_card_quota": int(row.get("daily_card_quota") or 0),
        "remark": row.get("remark") or "",
        "created_at": row.get("created_at") or "",
    }


@route("GET", "/api/v1/admin/agent-levels", auth="admin", summary="分销等级列表", tags=("admin", "agent"))
def admin_agent_levels(req: Request) -> Response:
    rows = db.query_all("SELECT * FROM agent_levels ORDER BY level, id")
    items = []
    for row in rows:
        data = _agent_level_public(row)
        data["user_count"] = int(db.query_scalar(
            "SELECT COUNT(*) FROM users WHERE agent_level_id=?", (row["id"],), 0) or 0)
        items.append(data)
    return ok({"items": items, "enabled": bool(items), "message": "" if items else "自托管版可以不用分销等级"})


@route(
    "GET",
    "/api/v1/admin/agent-levels/<level_id>",
    auth="admin",
    summary="分销等级详情",
    tags=("admin", "agent"),
)
def admin_agent_level_detail(req: Request, level_id: str) -> Response:
    lid = _pid(level_id, "等级 ID")
    row = db.query_one("SELECT * FROM agent_levels WHERE id=?", (lid,))
    if not row:
        raise HttpError(404, "等级不存在")
    data = _agent_level_public(row)
    data["user_count"] = int(db.query_scalar(
        "SELECT COUNT(*) FROM users WHERE agent_level_id=?", (lid,), 0) or 0)
    return ok(data)


def _agent_level_update(lid: int, req: Request) -> Response:
    if not db.query_one("SELECT id FROM agent_levels WHERE id=?", (lid,)):
        raise HttpError(404, "等级不存在")
    data = _agent_level_payload(req)
    clash = db.query_one("SELECT id FROM agent_levels WHERE name=? AND id<>?", (data["name"], lid))
    if clash:
        raise HttpError(409, f"等级名称「{data['name']}」已被占用", code="conflict")
    db.update("agent_levels", data, "id=?", (lid,))
    _audit(req, "admin_agent_level_update", target=data["name"], detail=f"id={lid}")
    return ok(_agent_level_public(db.query_one("SELECT * FROM agent_levels WHERE id=?", (lid,)) or {}),
              "等级已保存")


def _agent_level_payload(req: Request) -> dict:
    name = req.str_field("name", "", max_len=32).strip()
    if not name:
        raise HttpError(400, "等级名称不能为空")
    rate = _fnum(req.field("commission_rate"), "佣金比例", 0, 1, 0)
    discount = _fnum(req.field("card_discount"), "拿卡折扣", 0.01, 1, 1)
    return {
        "name": name,
        "level": req.int_field("level", 1, minimum=1, maximum=99),
        "commission_rate": rate,
        "card_discount": discount,
        "daily_card_quota": req.int_field("daily_card_quota", 0, minimum=0, maximum=100000),
        "remark": req.str_field("remark", "", max_len=200),
    }


@route("POST", "/api/v1/admin/agent-levels", auth="admin", summary="新增分销等级", tags=("admin", "agent"))
def admin_create_agent_level(req: Request) -> Response:
    data = _agent_level_payload(req)
    if db.query_one("SELECT id FROM agent_levels WHERE name=?", (data["name"],)):
        raise HttpError(409, "该等级名称已存在", code="conflict")
    data["created_at"] = timeutil.utc_now_str()
    level_id = db.insert("agent_levels", data)
    _audit(req, "admin_agent_level_create", target=data["name"], detail=f"id={level_id}")
    return ok(_agent_level_public(db.query_one("SELECT * FROM agent_levels WHERE id=?", (level_id,)) or {}),
              "等级已创建")


@route("PUT", "/api/v1/admin/agent-levels/<level_id>", auth="admin", summary="修改分销等级", tags=("admin", "agent"))
def admin_update_agent_level(req: Request, level_id: str) -> Response:
    return _agent_level_update(_pid(level_id, "等级 ID"), req)


@route(
    "POST",
    "/api/v1/admin/agent-levels/<level_id>",
    auth="admin",
    summary="修改分销等级（POST 别名）",
    tags=("admin", "agent"),
)
def admin_update_agent_level_post(req: Request, level_id: str) -> Response:
    """有些客户端/老面板用 POST 提交修改，这里做成 PUT 的别名，避免 405。"""
    return _agent_level_update(_pid(level_id, "等级 ID"), req)


@route("DELETE", "/api/v1/admin/agent-levels/<level_id>", auth="admin", summary="删除分销等级", tags=("admin", "agent"))
def admin_delete_agent_level(req: Request, level_id: str) -> Response:
    lid = _pid(level_id, "等级 ID")
    row = db.query_one("SELECT * FROM agent_levels WHERE id=?", (lid,))
    if not row:
        raise HttpError(404, "等级不存在")
    used = int(db.query_scalar("SELECT COUNT(*) FROM users WHERE agent_level_id=?", (lid,), 0) or 0)
    if used:
        raise HttpError(409, f"该等级下还有 {used} 个用户，请先调整他们的等级", code="conflict")
    db.delete("agent_levels", "id=?", (lid,))
    _audit(req, "admin_agent_level_delete", target=row["name"], detail=f"id={lid}")
    return ok({"id": lid, "deleted": True})


@route("PUT", "/api/v1/admin/agent-levels", auth="admin", summary="批量保存分销等级", tags=("admin", "agent"))
def admin_batch_save_agent_levels(req: Request) -> Response:
    """批量保存：``{"items":[{"id":1,"level":2,"commission_rate":0.1}, ...]}``。

    契约把 ``GET/POST/PUT/DELETE /admin/agent-levels[/<id>]`` 声明成一整组，
    这条路径就承担「批量改」（例如前端拖拽排序后一次提交）。
    """
    payload = req.json(required=True)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise HttpError(400, "请提供 items 数组（每项含 id 与要修改的字段）")
    if len(items) > 100:
        raise HttpError(400, "单次最多保存 100 个等级")
    updated: list[int] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            raise HttpError(400, "items 每项都必须含 id")
        lid = _pid(item["id"], "等级 ID")
        row = db.query_one("SELECT * FROM agent_levels WHERE id=?", (lid,))
        if not row:
            raise HttpError(404, f"等级 {lid} 不存在")
        data: dict[str, Any] = {}
        if item.get("name") not in (None, ""):
            name = str(item["name"]).strip()[:32]
            if db.query_one("SELECT id FROM agent_levels WHERE name=? AND id<>?", (name, lid)):
                raise HttpError(409, f"等级名称「{name}」已被占用", code="conflict")
            data["name"] = name
        if "level" in item:
            data["level"] = _int_range(item["level"], "等级序号", 1, 99, int(row["level"] or 1))
        if "commission_rate" in item:
            data["commission_rate"] = _fnum(item["commission_rate"], "佣金比例", 0, 1, 0)
        if "card_discount" in item:
            data["card_discount"] = _fnum(item["card_discount"], "拿卡折扣", 0.01, 1, 1)
        if "daily_card_quota" in item:
            data["daily_card_quota"] = _int_range(
                item["daily_card_quota"], "每日领卡上限", 0, 100000,
                int(row["daily_card_quota"] or 0),
            )
        if "remark" in item:
            data["remark"] = str(item["remark"] or "")[:200]
        if data:
            db.update("agent_levels", data, "id=?", (lid,))
            updated.append(lid)
    _audit(req, "admin_agent_level_batch_save", target=f"{len(updated)} 个等级",
           detail=f"ids={updated[:50]}")
    return ok({"updated": updated, "count": len(updated)}, f"已保存 {len(updated)} 个等级")


@route("DELETE", "/api/v1/admin/agent-levels", auth="admin", summary="批量删除分销等级", tags=("admin", "agent"))
def admin_batch_delete_agent_levels(req: Request) -> Response:
    """批量删除：``{"ids":[1,2]}``；仍有用户挂在该等级下的会被跳过并在 blocked 里说明。"""
    payload = req.json(required=False)
    raw_ids = payload.get("ids") if isinstance(payload, dict) else None
    ids = textutil.as_int_list(raw_ids if raw_ids else req.field("ids"))
    if not ids:
        raise HttpError(400, "请提供要删除的等级 ids")
    deleted: list[int] = []
    blocked: list[dict] = []
    for lid in ids[:200]:
        row = db.query_one("SELECT * FROM agent_levels WHERE id=?", (lid,))
        if not row:
            continue
        used = int(db.query_scalar("SELECT COUNT(*) FROM users WHERE agent_level_id=?", (lid,), 0) or 0)
        if used:
            blocked.append({"id": lid, "name": row["name"], "user_count": used})
            continue
        db.delete("agent_levels", "id=?", (lid,))
        deleted.append(lid)
    _audit(req, "admin_agent_level_batch_delete", target=f"{len(deleted)} 个等级",
           detail=f"deleted={deleted[:50]} blocked={blocked[:10]}")
    message = f"已删除 {len(deleted)} 个等级"
    if blocked:
        message += f"，{len(blocked)} 个因仍有用户被跳过"
    return ok({"deleted": deleted, "blocked": blocked}, message)


@route("GET", "/api/v1/admin/test-email", auth="admin", summary="邮件配置与队列状态", tags=("admin", "system"))
def admin_test_email_status(req: Request) -> Response:
    health = mailer.health()
    recent = db.query_all(
        "SELECT id, channel, target, subject, scene, status, attempts, last_error, created_at, sent_at"
        " FROM message_outbox ORDER BY id DESC LIMIT 20"
    )
    return ok({**health, "recent": recent})


@route("POST", "/api/v1/admin/test-email", auth="admin", summary="发送测试邮件", tags=("admin", "system"))
def admin_test_email(req: Request) -> Response:
    target = req.str_field("to", "", max_len=128).strip()
    if not target or "@" not in target:
        raise HttpError(400, "请提供收件邮箱（to 参数）")
    health = mailer.health()
    if not health.get("smtp_configured"):
        raise HttpError(
            400,
            "SMTP 未配置：请在 .env 里设置 SK_SMTP_HOST / SK_SMTP_PORT / SK_SMTP_USER / "
            "SK_SMTP_PASSWORD / SK_SMTP_FROM",
        )
    site = boot.get_config("site.name", PRODUCT)
    sent, message = mailer.send_email(
        target,
        f"【{site}】测试邮件",
        f"这是一封测试邮件。\n\n发送时间（UTC）：{timeutil.utc_now_str()}\n"
        "收到即代表邮件通道可用。",
    )
    _audit(req, "admin_test_email", target=target, detail=f"sent={sent} {message}")
    return ok({"sent": sent, "to": target, "message": message or "已发送", **health})


@route("GET", "/api/v1/admin/test-ai", auth="admin", summary="AI 文案功能状态", tags=("admin", "system"))
def admin_test_ai_status(req: Request) -> Response:
    return ok(
        {
            "enabled": False,
            "provider": "",
            "model": "",
            "message": "自托管版未内置 AI 文案供应商；如需启用，请实现 app/common/ai.py 并在 .env 配置密钥",
        }
    )


@route("POST", "/api/v1/admin/test-ai", auth="admin", summary="测试 AI 文案生成", tags=("admin", "system"))
def admin_test_ai(req: Request) -> Response:
    raise HttpError(
        400,
        "AI 文案功能未配置：自托管版默认关闭（未内置供应商实现），"
        "可在 .env 里配置 SK_AI_* 并自行接入；当前不影响续火花文案的随机挑选功能",
    )


@route("POST", "/api/v1/admin/change-password", auth="admin", summary="管理员改密", tags=("admin", "system"))
def admin_change_password(req: Request) -> Response:
    user = req.require_admin()
    old_password = req.str_field("old_password", "", max_len=128) or req.str_field("current_password", "", max_len=128)
    new_password = req.str_field("new_password", "", max_len=128)
    authmod.change_password(user, old_password, new_password, ip=req.client_ip)
    return ok({"changed": True}, "密码已修改，所有会话已失效，请重新登录")


@route("GET", "/api/v1/admin/update", auth="admin", summary="版本与更新检查", tags=("admin", "system"))
def admin_update(req: Request) -> Response:
    return ok(
        {
            "current_version": VERSION,
            "build": BUILD,
            "product": PRODUCT,
            "latest_version": VERSION,
            "update_available": False,
            "offline": True,
            "message": "自托管版不联网检查更新；升级方式见 deploy/DEPLOY.md（替换代码 + python main.py migrate）",
        }
    )


@route("GET", "/api/v1/admin/license1", auth="admin", summary="授权信息", tags=("admin", "system"))
def admin_license(req: Request) -> Response:
    return ok(
        {
            "license": "self-hosted",
            "type": "self-hosted",
            "edition": "pro" if _optional_module("app.payment") else "lite",
            "expire_at": "",
            "max_users": 0,
            "message": "自托管授权：无用户数限制、无到期时间、不联网校验",
        }
    )

