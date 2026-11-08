"""用户中心接口（契约 §3）：个人信息、仪表盘、改密、资料、日志、通知、公告。

职责边界
--------
只读 ``users`` / ``douyin_accounts`` / ``scheduled_tasks`` / ``task_runs`` /
``send_history`` / ``run_logs`` / ``notifications`` / ``announcements``，不做自动化动作。
配额判断统一走 ``common/quota.py``，用户对象统一走 ``common/auth.public_user()``
（避免在这里手拼字段——漏一个 ``password_hash`` 就是安全事故）。

坑
--
1. 所有查询必须带 ``user_id`` 条件。这里每个 SQL 的第一个参数都是登录用户 id，
   绝不允许「先用 id 查出记录再判断归属」那种两段式写法。
2. 仪表盘的「今天」按**站点默认时区**算（``task.default_timezone``），与调度器写
   ``task_runs.run_date`` 的口径一致；用 UTC 日期会和调度器对不上，出现「今天已跑
   0 次」但实际跑过的假象。
3. ``send_history`` 没有 ``user_id`` 列，只能 join ``douyin_accounts`` 再按
   ``a.user_id`` 过滤（``send_history.account_id`` 是外键）。
4. 排序字段来自 query，必须走白名单映射后再拼 SQL —— 虽然本仓库 SQL 全参数化，
   但 ``ORDER BY`` 无法参数化，直接拼 query 就是注入点。
"""

from __future__ import annotations

from .. import db
from ..bootstrap import get_config
from ..common import auth as authlib
from ..common import logging as logmod
from ..common import quota as quotamod
from ..common import textutil, timeutil, validate
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route

# 可排序字段白名单：query 里的 sort 只能命中这里的键
_SORTABLE = {
    "id": "id",
    "created_at": "created_at",
    "read_at": "read_at",
    "level": "level",
    "is_pinned": "is_pinned",
    "publish_at": "publish_at",
}


def _order_by(req: Request, default: str = "-id") -> str:
    """把 ``sort=-created_at`` 解析成安全的排序片段（白名单外回退默认值）。

    只返回 ``"列 [DESC]"``；调用方自己拼 ``ORDER BY``（多字段排序时更好读）。
    """
    raw = req.str_field("sort", "", max_len=40).strip()
    if not raw:
        raw = default
    desc = raw.startswith("-")
    column = _SORTABLE.get(raw.lstrip("+-"))
    if not column:
        desc = default.startswith("-")
        column = _SORTABLE[default.lstrip("+-")]
    return f"{column} {'DESC' if desc else 'ASC'}"


def _date_filter(column: str, req: Request, where: list[str], params: list) -> None:
    """通用 start_date/end_date 过滤（日期字符串比较，闭区间）。"""
    start = req.str_field("start_date", "", max_len=20)
    end = req.str_field("end_date", "", max_len=20)
    if start:
        where.append(f"{column} >= ?")
        params.append(start)
    if end:
        # 传的是日期时按「含当天」处理，避免用户理解成 00:00:00 之后就不含当天
        where.append(f"{column} <= ?")
        params.append(f"{end} 23:59:59" if len(end) == 10 else end)


# --------------------------------------------------------------------------
# 个人信息
# --------------------------------------------------------------------------
@route("GET", "/api/v1/user/info", auth="user", summary="当前用户信息（含配额快照）", tags=("user",))
def info(req: Request) -> Response:
    user = req.require_user()
    # 每次读个人信息顺手刷新配额快照：套餐到期/被管理员改动后前端能立刻看到
    quotamod.Quota.refresh(user["id"])
    fresh = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],)) or user
    data = authlib.public_user(fresh)
    data["unread_notifications"] = int(db.query_scalar(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user["id"],), 0) or 0)
    return ok(data)


@route("GET", "/api/v1/user/dashboard", auth="user", summary="首页汇总卡片", tags=("user",))
def dashboard(req: Request) -> Response:
    user = req.require_user()
    uid = user["id"]
    settings = get_settings()
    tz = get_config("task.default_timezone", settings.timezone)
    today = timeutil.local_date_str(tz)

    # --- 账号 ---
    by_status: dict[str, int] = {}
    for row in db.query_all(
        "SELECT status, COUNT(*) AS n FROM douyin_accounts WHERE user_id=? GROUP BY status", (uid,)
    ):
        by_status[str(row["status"])] = int(row["n"] or 0)
    accounts = {
        "total": sum(by_status.values()),
        "online": by_status.get("online", 0),
        "offline": by_status.get("offline", 0),
        "expired": by_status.get("expired", 0),
        "banned": by_status.get("banned", 0),
        "need_verify": by_status.get("need_verify", 0),
        # 暂停托管（enabled=0）的账号数：和登录态失效是两件事，分开给前端
        "paused": int(db.query_scalar(
            "SELECT COUNT(*) FROM douyin_accounts WHERE user_id=? AND enabled=0", (uid,), 0) or 0),
    }

    # --- 定时任务 ---
    tasks = {
        "total": int(db.query_scalar(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE user_id=?", (uid,), 0) or 0),
        "enabled": int(db.query_scalar(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE user_id=? AND enabled=1", (uid,), 0) or 0),
        "next_run_at": str(db.query_scalar(
            "SELECT MIN(next_run_at) FROM scheduled_tasks"
            " WHERE user_id=? AND enabled=1 AND next_run_at IS NOT NULL", (uid,), "") or ""),
    }

    # --- 今天 ---
    run_row = db.query_one(
        "SELECT COUNT(*) AS total,\n"
        "       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,\n"
        "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed\n"
        "  FROM task_runs WHERE user_id=? AND run_date=?",
        (uid, today),
    ) or {}
    sent_row = db.query_one(
        "SELECT COUNT(*) AS sent, COUNT(DISTINCT h.friend_key) AS friends\n"
        "  FROM send_history h JOIN douyin_accounts a ON a.id = h.account_id\n"
        " WHERE a.user_id=? AND h.send_date=?",
        (uid, today),
    ) or {}
    today_block = {
        "date": today,
        "runs": int(run_row.get("total") or 0),
        "success": int(run_row.get("success") or 0),
        "failed": int(run_row.get("failed") or 0),
        "friends": int(sent_row.get("friends") or 0),
        "sent": int(sent_row.get("sent") or 0),
    }

    # --- 近 7 天趋势（缺的日期补 0，前端画图不用自己补） ---
    rows = db.query_all(
        "SELECT run_date AS date,\n"
        "       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,\n"
        "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed\n"
        "  FROM task_runs WHERE user_id=? AND run_date>=?\n"
        " GROUP BY run_date ORDER BY run_date",
        (uid, timeutil.recent_dates(7, tz)[0]),
    )
    trend_map = {str(r["date"]): r for r in rows}
    trend = [
        {
            "date": day,
            "success": int((trend_map.get(day) or {}).get("success") or 0),
            "failed": int((trend_map.get(day) or {}).get("failed") or 0),
        }
        for day in timeutil.recent_dates(7, tz)
    ]

    # --- 最近执行记录 ---
    recent_runs = db.query_all(
        "SELECT r.id, r.task_id, r.account_id, a.nickname AS account_nickname,\n"
        "       r.run_date, r.trigger_type, r.status, r.started_at, r.finished_at,\n"
        "       r.duration_ms, r.total_count, r.success_count, r.fail_count, r.message\n"
        "  FROM task_runs r LEFT JOIN douyin_accounts a ON a.id = r.account_id\n"
        " WHERE r.user_id=? ORDER BY r.id DESC LIMIT 5",
        (uid,),
    )

    quota_summary = quotamod.Quota.for_user(uid).summary()
    return ok({
        "accounts": accounts,
        "tasks": tasks,
        "today": today_block,
        "trend": trend,
        "recent_runs": recent_runs,
        "quota": quota_summary,
        "timezone": tz,
    })


# --------------------------------------------------------------------------
# 改密 / 资料
# --------------------------------------------------------------------------
@route("POST", "/api/v1/user/change-password", auth="user",
       summary="修改密码（所有会话失效）", tags=("user",))
def change_password(req: Request) -> Response:
    user = req.require_user()
    old_password = str(req.field("old_password") or "")
    new_password = str(req.field("new_password") or "")
    confirm = str(req.field("confirm_password") or "")
    if confirm and confirm != new_password:
        raise HttpError(400, "两次输入的新密码不一致")
    authlib.change_password(user, old_password, new_password, ip=req.client_ip)
    # change_password 内部已 revoke_all_sessions：改密后必须重新登录（前端要清 token）
    return ok({"changed": True, "relogin": True}, message="密码已修改，请用新密码重新登录")


@route("PUT", "/api/v1/user/profile", auth="user", summary="修改昵称/邮箱/手机号/头像", tags=("user",))
def update_profile(req: Request) -> Response:
    user = req.require_user()
    uid = user["id"]
    body = req.json(required=False) or {}
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")

    data: dict[str, object] = {"updated_at": timeutil.utc_now_str()}

    if "nickname" in body:
        nickname = req.str_field("nickname", "", max_len=64) or user.get("nickname") or user["username"]
        data["nickname"] = nickname

    if "email" in body:
        raw = req.str_field("email", "", max_len=160)
        if not raw:
            data["email"] = None
        else:
            good, email = validate.email(raw)
            if not good:
                raise HttpError(400, email)
            exists = db.query_one("SELECT id FROM users WHERE email=? AND id<>?", (email, uid))
            if exists:
                raise HttpError(409, "该邮箱已被其他账号使用", code="conflict")
            data["email"] = email

    if "phone" in body:
        raw = req.str_field("phone", "", max_len=32)
        if not raw:
            data["phone"] = None
        else:
            good, phone = validate.phone(raw)
            if not good:
                raise HttpError(400, phone)
            exists = db.query_one("SELECT id FROM users WHERE phone=? AND id<>?", (phone, uid))
            if exists:
                raise HttpError(409, "该手机号已被其他账号使用", code="conflict")
            data["phone"] = phone

    if "avatar" in body:
        avatar = req.str_field("avatar", "", max_len=500)
        # 只接受 http(s) 外链或站内绝对路径：避免 javascript:/data: 这类 XSS 载体进库
        if avatar and not (validate.looks_like_url(avatar) or avatar.startswith("/")):
            raise HttpError(400, "头像地址必须是 http(s) 链接或站内路径")
        data["avatar"] = avatar

    db.update("users", data, "id=?", (uid,))
    fresh = db.query_one("SELECT * FROM users WHERE id=?", (uid,)) or user
    logmod.audit("update_profile", user=user, detail=",".join(k for k in data if k != "updated_at"), ip=req.client_ip)
    return ok(authlib.public_user(fresh), message="资料已更新")


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
@route("GET", "/api/v1/user/logs", auth="user", summary="我的运行日志（分页）", tags=("user",))
def my_logs(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["user_id=?"]
    params: list = [user["id"]]

    level = req.str_field("level", "", max_len=16).lower()
    if level:
        if level not in ("debug", "info", "warning", "error"):
            raise HttpError(400, "level 取值不正确")
        where.append("level=?")
        params.append(level)
    source = req.str_field("source", "", max_len=80)
    if source:
        where.append("source LIKE ?")
        params.append(f"%{source}%")
    keyword = req.str_field("keyword", "", max_len=200)
    if keyword:
        where.append("(message LIKE ? OR context LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    _date_filter("created_at", req, where, params)

    base = (
        "SELECT id, scope, level, source, message, context, created_at"
        f"  FROM run_logs WHERE {' AND '.join(where)} ORDER BY {_order_by(req, '-id')}"
    )
    result = db.paginate(base, params, page, page_size)
    for item in result["items"]:
        item["context"] = textutil.as_dict(item.get("context"), {})
    return ok(result)


# --------------------------------------------------------------------------
# 通知 / 公告
# --------------------------------------------------------------------------
@route("GET", "/api/v1/user/notifications", auth="user", summary="站内通知（分页）", tags=("user",))
def notifications(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["user_id=?"]
    params: list = [user["id"]]
    if req.bool_field("unread", False):
        where.append("read_at IS NULL")
    base = (
        "SELECT id, title, content, level, read_at, created_at"
        f"  FROM notifications WHERE {' AND '.join(where)} ORDER BY {_order_by(req, '-id')}"
    )
    result = db.paginate(base, params, page, page_size)
    result["unread"] = int(db.query_scalar(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user["id"],), 0) or 0)
    for item in result["items"]:
        item["is_read"] = bool(item.get("read_at"))
    return ok(result)


@route("POST", "/api/v1/user/notifications/read", auth="user",
       summary="标记通知已读（ids 或 all）", tags=("user",))
def read_notifications(req: Request) -> Response:
    user = req.require_user()
    now = timeutil.utc_now_str()
    if req.bool_field("all", False):
        updated = db.update(
            "notifications", {"read_at": now}, "user_id=? AND read_at IS NULL", (user["id"],)
        )
        return ok({"updated": updated, "all": True}, message=f"已标记 {updated} 条为已读")

    ids = textutil.as_int_list(req.field("ids", []))
    if not ids:
        raise HttpError(400, "请提供 ids 列表或 all=true")
    if len(ids) > 500:
        raise HttpError(400, "单次最多标记 500 条")
    placeholders = ",".join("?" for _ in ids)
    # user_id 条件必须在：否则改一下 ids 就能标记别人的通知
    updated = db.update(
        "notifications",
        {"read_at": now},
        f"user_id=? AND id IN ({placeholders}) AND read_at IS NULL",
        (user["id"], *ids),
    )
    return ok({"updated": updated, "ids": ids}, message=f"已标记 {updated} 条为已读")


@route("GET", "/api/v1/user/announcements", auth="user", summary="公告列表（分页）", tags=("user",))
def announcements(req: Request) -> Response:
    req.require_user()
    page, page_size = req.page_args()
    now = timeutil.utc_now_str()
    where = [
        "is_active=1",
        "(publish_at IS NULL OR publish_at='' OR publish_at<=?)",
        "(expire_at IS NULL OR expire_at='' OR expire_at>?)",
    ]
    params: list = [now, now]
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(title LIKE ? OR content LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    # 置顶优先，其次按发布时间/创建时间倒序
    base = (
        "SELECT id, title, content, level, is_pinned, publish_at, expire_at, created_at"
        f"  FROM announcements WHERE {' AND '.join(where)}"
        f" ORDER BY is_pinned DESC, {_order_by(req, '-created_at')}"
    )
    result = db.paginate(base, params, page, page_size)
    for item in result["items"]:
        item["is_pinned"] = bool(item.get("is_pinned"))
    return ok(result)
