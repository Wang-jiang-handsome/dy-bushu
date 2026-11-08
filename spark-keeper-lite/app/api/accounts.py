"""抖音账号与扫码/短信登录接口（契约 §4）。

职责边界
--------
* 账号 CRUD、归属校验、配额校验、``login_sessions`` 状态读写在本模块完成。
* **真正的浏览器动作一律委托** ``app.automation.login``（队友 D）。本模块不 import
  该模块的顶层，只在函数内部延迟 import，缺失时降级成可读错误（见下），
  这样「lite 版 / 本机没装自动化」不会让整个 API 模块 import 失败。
* ``login_sessions`` 表是登录状态的**唯一真源**：前端拿到 ``session_id`` 后一直轮询
  ``/douyin/login/status``，本模块每次都会先把 D 的 ``poll()`` 跑一遍（若可用），
  然后**从数据库读行**返回，绝不依赖 D 的返回结构。

坑（务必先读）
--------------
1. 降级策略：``app.automation.login`` 不存在（或它的依赖缺文件）时，启动类接口
   返回 HTTP 503 + ``data={"status":"failed","message":...}``，文案里写明缺哪个函数。
   既不 500（前端拿到可读原因），也不假装成功（避免前端死等轮询）。
2. 同一个账号可能先后发起多次登录：``/douyin/login/browser`` 会创建新的
   ``login_sessions`` 行。为避免前端拿着旧 session 一直轮询，**同一账号下仍在
   pending 的旧会话会被标记为 canceled** 并写明原因。
3. 扫码登录首包可能没有二维码：D 的 ``start_*`` 必须立即返回，真实模式下二维码是
   后台线程写进 DB 的（1~8 秒）。所以本模块返回行里 ``qrcode_image`` 允许为空串，
   前端必须继续轮询 —— 这一点已写进接口 message，也同步给了前端。
4. 删除账号要连带删登录态文件：``storage_path`` 是用户可写字段，拼接前必须
   ``resolve()`` 后校验仍在 ``settings.session_root`` 之下，否则就是目录穿越删除。
5. 账号的 ``enabled``（暂停托管）与 ``status``（登录态）是两件事，响应里都返回，
   不要用 status 表达「暂停」。
"""

from __future__ import annotations

import importlib
import shutil
import traceback
from datetime import timedelta
from typing import Any, Callable

from .. import db
from ..common import logging as logmod
from ..common import quota as quotamod
from ..common import security, textutil, timeutil, validate
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route

ACCOUNT_STATUS = ("online", "offline", "expired", "banned", "need_verify")
LOGIN_TYPES = ("qrcode", "sms")
LOGIN_SESSION_TTL_SECONDS = 300          # 二维码/短信登录会话 5 分钟
ACTIVE_SESSION_STATES = ("pending", "scanned", "confirmed")
FINAL_SESSION_STATES = ("success", "failed", "canceled", "expired")

# 可排序字段白名单（ORDER BY 不能参数化，必须走映射）
_SORTABLE = {"id": "id", "created_at": "created_at", "updated_at": "updated_at",
             "last_check_at": "last_check_at", "status": "status"}


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _order_by(req: Request, default: str = "-id") -> str:
    """把 ``sort=-created_at`` 解析成 ``"列 DIR"``；白名单外回退默认值。"""
    raw = req.str_field("sort", "", max_len=40).strip() or default
    desc = raw.startswith("-")
    column = _SORTABLE.get(raw.lstrip("+-"))
    if not column:
        desc = default.startswith("-")
        column = _SORTABLE[default.lstrip("+-")]
    return f"{column} {'DESC' if desc else 'ASC'}"


def import_optional(module_name: str) -> Any | None:
    """延迟 import 可选模块；「模块或它的依赖缺失」都返回 None（调用方降级）。

    为什么不只捕获 ``exc.name == module_name``：队友模块内部的依赖（如 cdp.py）缺失
    时同样应该降级成可读错误，而不是让整个 API 层 500。缺哪个依赖会写进日志。
    """
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        logmod.warning(f"可选模块 {module_name} 不可用（缺少 {exc.name}），相关接口降级为 503",
                       source="api.accounts")
        return None


def _login_func(func_name: str) -> Callable | None:
    module = import_optional("app.automation.login")
    func = getattr(module, func_name, None) if module is not None else None
    return func if callable(func) else None


def _unavailable(func_name: str) -> HttpError:
    message = (f"浏览器自动化模块未就绪（app.automation.login.{func_name} 不可用）："
               "本机或该部署未安装该模块，请在带 Chrome 的部署机上重试")
    return HttpError(503, message, code="server_error",
                     data={"status": "failed", "message": message})


def _call_automation(func: Callable, func_name: str, *args, **kwargs) -> dict:
    """调用自动化层函数，并把「可预期失败」转成 503 + ``status=failed``。

    浏览器启动失败的原因很多（没装 Chrome、沙箱禁止子进程、代理连不上、页面改版），
    这些都不该以 500 堆栈的形式丢给前端 —— 前端需要的是能展示给用户的文案。
    真正的编码错误会连堆栈一起写进 ``run_logs``，排障信息不丢。
    """
    try:
        result = func(*args, **kwargs)
    except HttpError:
        raise
    except Exception as exc:      # noqa: BLE001 - 见上：自动化层的失败是业务可预期状态
        message = f"浏览器自动化执行失败（{func_name}）：{type(exc).__name__}: {exc}"
        logmod.error(message, source="api.accounts",
                     context={"traceback": traceback.format_exc()[-2000:]})
        raise HttpError(503, message, code="server_error",
                        data={"status": "failed", "message": message}) from exc
    return result if isinstance(result, dict) else {}


def _fail_latest_pending(account_id: int, method: str, message: str) -> None:
    """启动失败收尾：把该账号还没结束的登录会话落成 failed，别让前端一直轮询。"""
    row = db.query_one(
        "SELECT session_id FROM login_sessions WHERE account_id=? AND method=?"
        " AND status IN ('pending','scanned','confirmed') ORDER BY id DESC LIMIT 1",
        (account_id, method),
    )
    if not row:
        return
    db.update("login_sessions",
              {"status": "failed", "step": "failed", "message": message[:500],
               "finished_at": _now(), "updated_at": _now()},
              "session_id=?", (row["session_id"],))


def _now() -> str:
    return timeutil.utc_now_str()


# --------------------------------------------------------------------------
# 账号读取 / 序列化
# --------------------------------------------------------------------------
def _get_account(req: Request, account_id: str | int) -> dict:
    """按 id 取账号并**强制**校验归属（越权的唯一入口）。"""
    user = req.require_user()
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise HttpError(400, "账号 ID 不正确") from None
    row = db.query_one(
        "SELECT * FROM douyin_accounts WHERE id=? AND user_id=?", (aid, user["id"])
    )
    if not row:
        raise HttpError(404, "抖音账号不存在或不属于当前用户", code="not_found")
    return row


def require_account_id(req: Request) -> dict:
    """从请求里取 ``account_id`` 并返回归属校验过的账号（缺参数时给 400 而不是 404）。"""
    raw = req.field("account_id", None)
    if raw in (None, "", 0, "0"):
        raise HttpError(400, "请提供 account_id")
    return _get_account(req, req.int_field("account_id", 0, minimum=1))


def _account_out(row: dict, *, friend_count: int | None = None, task_count: int | None = None) -> dict:
    """账号对外结构。

    ``storage_path`` 不对外暴露（服务器上的相对路径没必要给前端），只给
    ``has_session`` 布尔值；前端判断「要不要重新扫码」看 status/last_check_at。
    """
    data = {
        "id": row["id"],
        "nickname": row.get("nickname") or "",
        "douyin_uid": row.get("douyin_uid") or "",
        "sec_uid": row.get("sec_uid") or "",
        "avatar": row.get("avatar") or "",
        "phone_masked": row.get("phone_masked") or "",
        "remark": row.get("remark") or "",
        "status": row.get("status") or "offline",
        "enabled": bool(row.get("enabled", 1)),
        "paused": not bool(row.get("enabled", 1)),
        "login_type": row.get("login_type") or "qrcode",
        "proxy_id": row.get("proxy_id"),
        "has_session": bool(row.get("storage_path")),
        "storage_updated_at": row.get("storage_updated_at"),
        "last_check_at": row.get("last_check_at"),
        "last_error": row.get("last_error") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if friend_count is not None:
        data["friend_count"] = friend_count
    if task_count is not None:
        data["task_count"] = task_count
    return data


def _account_counts(account_id: int) -> tuple[int, int]:
    friends = int(db.query_scalar(
        "SELECT COUNT(*) FROM friends WHERE account_id=?", (account_id,), 0) or 0)
    tasks = int(db.query_scalar(
        "SELECT COUNT(*) FROM scheduled_tasks WHERE account_id=?", (account_id,), 0) or 0)
    return friends, tasks


# --------------------------------------------------------------------------
# 登录会话读取 / 序列化
# --------------------------------------------------------------------------
def _get_session(req: Request, session_id: str) -> dict:
    user = req.require_user()
    sid = (session_id or "").strip()
    if not sid:
        raise HttpError(400, "缺少 session_id")
    row = db.query_one(
        "SELECT * FROM login_sessions WHERE session_id=? AND user_id=?", (sid, user["id"])
    )
    if not row:
        raise HttpError(404, "登录会话不存在或不属于当前用户", code="not_found")
    return row


def _expire_if_needed(row: dict) -> dict:
    """把已超时但仍处于进行中的会话落成 expired（前端轮询时顺手收尾）。"""
    if row.get("status") in ACTIVE_SESSION_STATES and timeutil.is_expired(row.get("expire_at")):
        now = _now()
        db.update(
            "login_sessions",
            {"status": "expired", "step": "expired", "message": "登录会话已超时，请重新发起",
             "finished_at": now, "updated_at": now},
            "session_id=?",
            (row["session_id"],),
        )
        row = dict(row)
        row.update({"status": "expired", "step": "expired",
                    "message": "登录会话已超时，请重新发起", "finished_at": now, "updated_at": now})
    return row


def _session_out(row: dict, *, account: dict | None = None) -> dict:
    data = {
        "session_id": row.get("session_id"),
        "method": row.get("method"),
        "status": row.get("status"),
        "step": row.get("step") or "",
        "message": row.get("message") or "",
        "qrcode_data": row.get("qrcode_data") or "",
        "qrcode_image": row.get("qrcode_image") or "",
        "phone": row.get("phone") or "",
        "account_id": row.get("account_id"),
        "expire_at": row.get("expire_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
        "final": row.get("status") in FINAL_SESSION_STATES,
    }
    if account is not None:
        data["account"] = _account_out(account)
    return data


def _cancel_pending_sessions(account_id: int, *, keep: str = "", reason: str = "") -> int:
    """把同账号下仍在进行中的旧登录会话标记为 canceled（新会话取而代之）。"""
    placeholders = ",".join("?" for _ in ACTIVE_SESSION_STATES)
    params: list = [account_id, *ACTIVE_SESSION_STATES]
    sql = (f"SELECT session_id FROM login_sessions WHERE account_id=? AND status IN ({placeholders})")
    if keep:
        sql += " AND session_id<>?"
        params.append(keep)
    rows = db.query_all(sql, params)
    for row in rows:
        db.update(
            "login_sessions",
            {"status": "canceled", "step": "canceled", "message": reason or "已被新的登录会话取代",
             "finished_at": _now(), "updated_at": _now()},
            "session_id=?",
            (row["session_id"],),
        )
    return len(rows)


def _create_placeholder_session(user_id: int, account_id: int, method: str) -> dict:
    """建一条 pending 的登录会话（账号刚创建时用，满足契约返回 login_session_id）。

    真正的浏览器动作由 ``/douyin/login/browser`` 发起，届时这条会被标记 canceled。
    """
    now = _now()
    session_id = security.new_id("ls_")
    db.insert("login_sessions", {
        "session_id": session_id,
        "user_id": user_id,
        "account_id": account_id,
        "method": method,
        "status": "pending",
        "step": "created",
        "message": "等待发起扫码登录",
        "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=LOGIN_SESSION_TTL_SECONDS)),
        "created_at": now,
        "updated_at": now,
    })
    return db.query_one("SELECT * FROM login_sessions WHERE session_id=?", (session_id,)) or {}


def _latest_session_for(account_id: int, method: str, session_id: str = "") -> dict | None:
    """取本次刚被 D 写库的登录会话：优先用它返回的 session_id，否则取该账号最新一条。"""
    if session_id:
        row = db.query_one("SELECT * FROM login_sessions WHERE session_id=?", (session_id,))
        if row:
            return row
    return db.query_one(
        "SELECT * FROM login_sessions WHERE account_id=? AND method=? ORDER BY id DESC LIMIT 1",
        (account_id, method),
    )


# --------------------------------------------------------------------------
# 账号 CRUD
# --------------------------------------------------------------------------
@route("GET", "/api/v1/douyin/accounts", auth="user", summary="抖音账号列表（分页）", tags=("douyin",))
def list_accounts(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["a.user_id=?"]
    params: list = [user["id"]]

    status = req.str_field("status", "", max_len=20)
    if status:
        if status not in ACCOUNT_STATUS:
            raise HttpError(400, "status 取值不正确")
        where.append("a.status=?")
        params.append(status)
    if "enabled" in (req.json(required=False) or {}) or "enabled" in req.query:
        where.append("a.enabled=?")
        params.append(1 if req.bool_field("enabled", True) else 0)
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(a.nickname LIKE ? OR a.douyin_uid LIKE ? OR a.remark LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)

    base = (
        "SELECT a.*, (SELECT COUNT(*) FROM friends f WHERE f.account_id=a.id AND f.enabled=1) AS friend_count,\n"
        "       (SELECT COUNT(*) FROM scheduled_tasks t WHERE t.account_id=a.id) AS task_count\n"
        "  FROM douyin_accounts a\n"
        f" WHERE {' AND '.join(where)} ORDER BY {_order_by(req, '-id')}"
    )
    result = db.paginate(base, params, page, page_size)
    result["items"] = [
        _account_out(row,
                     friend_count=int(row.get("friend_count") or 0),
                     task_count=int(row.get("task_count") or 0))
        for row in result["items"]
    ]
    return ok(result)


@route("POST", "/api/v1/douyin/accounts", auth="user", summary="新增托管账号（占位）", tags=("douyin",))
def create_account(req: Request) -> Response:
    user = req.require_user()
    # 配额：账号位上限（未激活套餐时 ensure() 会先抛 402 no_active_package）
    quotamod.Quota.for_user(user["id"]).ensure_can_add_account()

    login_type = req.str_field("login_type", "qrcode", max_len=16).lower()
    if login_type not in LOGIN_TYPES:
        raise HttpError(400, "login_type 只能是 qrcode 或 sms")
    nickname = req.str_field("nickname", "", max_len=64)
    remark = req.str_field("remark", "", max_len=200)
    proxy_id = req.field("proxy_id", None)
    if proxy_id not in (None, "", 0, "0"):
        try:
            proxy_id = int(proxy_id)
        except (TypeError, ValueError):
            raise HttpError(400, "proxy_id 必须是整数") from None
        if not db.query_one("SELECT id FROM proxies WHERE id=?", (proxy_id,)):
            raise HttpError(400, "指定的代理不存在")
    else:
        proxy_id = None

    now = _now()
    account_id = db.insert("douyin_accounts", {
        "user_id": user["id"],
        # 昵称留空时给个可识别的占位名，登录成功后会由自动化层回填真实昵称
        "nickname": nickname or "未命名账号",
        "remark": remark,
        "enabled": 1,
        "status": "offline",
        "login_type": login_type,
        "proxy_id": proxy_id,
        "created_at": now,
        "updated_at": now,
    })
    session = _create_placeholder_session(user["id"], account_id, login_type)
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (account_id,)) or {}
    logmod.audit("create_douyin_account", user=user, target=str(account_id), ip=req.client_ip)
    return ok({
        "account_id": account_id,
        "login_session_id": session.get("session_id", ""),
        "account": _account_out(account, friend_count=0, task_count=0),
        "message": "账号占位已创建，请调用 /douyin/login/browser 发起扫码登录",
    }, message="账号已创建")


@route("GET", "/api/v1/douyin/accounts/<account_id>", auth="user", summary="账号详情", tags=("douyin",))
def account_detail(req: Request, account_id: str) -> Response:
    row = _get_account(req, account_id)
    friends, tasks = _account_counts(int(row["id"]))
    data = _account_out(row, friend_count=friends, task_count=tasks)
    data["recent_runs"] = db.query_all(
        "SELECT id, run_date, trigger_type, status, started_at, finished_at,"
        "       success_count, fail_count, total_count, message"
        "  FROM task_runs WHERE account_id=? AND user_id=? ORDER BY id DESC LIMIT 5",
        (row["id"], row["user_id"]),
    )
    return ok(data)


@route("PATCH", "/api/v1/douyin/accounts/<account_id>", auth="user",
       summary="改备注/代理/暂停托管/登录态", tags=("douyin",))
def update_account(req: Request, account_id: str) -> Response:
    row = _get_account(req, account_id)
    body = req.json(required=False) or {}
    if not isinstance(body, dict) or not body:
        raise HttpError(400, "请提供要修改的字段")

    data: dict[str, Any] = {"updated_at": _now()}
    if "nickname" in body:
        data["nickname"] = req.str_field("nickname", "", max_len=64) or row["nickname"]
    if "remark" in body:
        data["remark"] = req.str_field("remark", "", max_len=200)
    if "enabled" in body:
        # 暂停托管：保留登录态，只是不参与调度（与 status 语义分离）
        data["enabled"] = 1 if req.bool_field("enabled", True) else 0
    if "status" in body:
        status = req.str_field("status", "", max_len=20)
        if status not in ACCOUNT_STATUS:
            raise HttpError(400, f"status 只能是 {'/'.join(ACCOUNT_STATUS)}")
        data["status"] = status
    if "login_type" in body:
        login_type = req.str_field("login_type", "", max_len=16).lower()
        if login_type not in LOGIN_TYPES:
            raise HttpError(400, "login_type 只能是 qrcode 或 sms")
        data["login_type"] = login_type
    if "proxy_id" in body:
        raw = req.field("proxy_id", None)
        if raw in (None, "", 0, "0"):
            data["proxy_id"] = None
        else:
            try:
                proxy_id = int(raw)
            except (TypeError, ValueError):
                raise HttpError(400, "proxy_id 必须是整数") from None
            if not db.query_one("SELECT id FROM proxies WHERE id=?", (proxy_id,)):
                raise HttpError(400, "指定的代理不存在")
            data["proxy_id"] = proxy_id

    # where 里带 user_id：即使 _get_account 已校验过，也不给「越权写」留任何可能
    db.update("douyin_accounts", data, "id=? AND user_id=?", (row["id"], row["user_id"]))
    fresh = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (row["id"],)) or row
    friends, tasks = _account_counts(int(row["id"]))
    logmod.audit("update_douyin_account", user=req.user, target=str(row["id"]),
                 detail=",".join(k for k in data if k != "updated_at"), ip=req.client_ip)
    return ok(_account_out(fresh, friend_count=friends, task_count=tasks), message="账号已更新")


@route("DELETE", "/api/v1/douyin/accounts/<account_id>", auth="user",
       summary="删除账号（连带任务与登录态文件）", tags=("douyin",))
def delete_account(req: Request, account_id: str) -> Response:
    row = _get_account(req, account_id)
    aid = int(row["id"])
    counts = {
        "tasks": int(db.query_scalar("SELECT COUNT(*) FROM scheduled_tasks WHERE account_id=?", (aid,), 0) or 0),
        "friends": int(db.query_scalar("SELECT COUNT(*) FROM friends WHERE account_id=?", (aid,), 0) or 0),
        "runs": int(db.query_scalar("SELECT COUNT(*) FROM task_runs WHERE account_id=?", (aid,), 0) or 0),
    }
    # 先删文件再删库：库删掉后 storage_path 就查不到了，反了会留垃圾文件
    files_removed = _remove_session_files(row)
    # scheduled_tasks / friends / task_runs / login_sessions / send_history 由外键 CASCADE 清理
    deleted = db.delete("douyin_accounts", "id=? AND user_id=?", (aid, row["user_id"]))
    if not deleted:
        raise HttpError(404, "抖音账号不存在或不属于当前用户", code="not_found")
    logmod.audit("delete_douyin_account", user=req.user, target=str(aid), ip=req.client_ip)
    return ok({"deleted": True, "account_id": aid, "files_removed": files_removed, **counts},
              message="账号及其任务已删除")


def _remove_session_files(account: dict) -> int:
    """删除该账号的登录态文件。防目录穿越：resolve 后必须仍在 session_root 内。"""
    rel = (account.get("storage_path") or "").strip()
    if not rel:
        return 0
    root = get_settings().session_root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logmod.warning(f"登录态路径越界，已跳过删除：{rel}", level="warning", source="api.accounts")
        return 0
    removed = 0
    try:
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)
            removed = 1
        elif candidate.exists():
            candidate.unlink()
            removed = 1
    except OSError as exc:      # 文件被占用/权限不足：不阻断删账号，只记日志
        logmod.warning(f"删除登录态文件失败：{candidate}（{exc}）", source="api.accounts")
    return removed


# --------------------------------------------------------------------------
# 扫码 / 短信登录
# --------------------------------------------------------------------------
@route("GET", "/api/v1/douyin/capabilities", auth="user",
       summary="宿主机能力（是否支持可见浏览器窗口）", tags=("accounts",))
def capabilities(req: Request) -> Response:
    """前端用它决定「在窗口里登录」按钮是否可用。

    云主机 / 容器里没有图形界面时，可见窗口模式必然失败（Chromium 直接退出：
    ``Missing X server or $DISPLAY``），所以按钮要么禁用、要么点了也自动降级。
    """
    from ..automation import cdp as cdp_mod
    from ..automation import chrome as chrome_mod
    from ..automation import session_store

    settings = get_settings()
    display = session_store.display_available(settings)
    browser = ""
    try:
        browser = chrome_mod.find_browser(settings) or ""
    except Exception:                     # noqa: BLE001 - 探测失败不影响接口
        browser = ""
    return ok({
        "display": display,
        "visible_supported": display,
        "headless": bool(getattr(settings, "browser_headless", True)),
        "container": cdp_mod.in_container(),
        "chrome_no_sandbox": str(getattr(settings, "chrome_no_sandbox", "auto") or "auto"),
        "browser": browser,
        "browser_found": bool(browser),
        # 没有图形界面时，前端应提示「用网页扫码 / 导入 Cookie」
        "hint": ("" if display else
                 "服务器没有图形界面：无法弹出浏览器窗口，请用本页二维码扫码，或使用「导入 Cookie」"),
    })


@route("POST", "/api/v1/douyin/login/browser", auth="user",
       summary="启动浏览器扫码登录", tags=("douyin",))
def start_browser_login(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    method = req.str_field("method", "qrcode", max_len=16).lower()
    if method != "qrcode":
        raise HttpError(400, "扫码登录的 method 只能是 qrcode")
    # visible=true：开一个**真实可见**的 Chrome 窗口，让用户直接在窗口里登录。
    # 这是「无头模式下二维码一直渲染不出来」的兜底路径 —— 抖音自己渲染的窗口
    # 不存在被拦的问题，用户扫码/输密码都行，后端只等 sessionid 出现。
    visible = req.bool_field("visible", False)

    func = _login_func("start_qrcode_login")
    if func is None:
        raise _unavailable("start_qrcode_login")

    settings = get_settings()
    try:
        result = _call_automation(func, "start_qrcode_login", account, settings,
                                  visible=visible)
    except HttpError as exc:
        # 启动失败要把刚建的会话落成 failed，否则前端会一直轮询一个永远不会动的会话
        _fail_latest_pending(int(account["id"]), "qrcode", exc.message)
        raise
    session = _latest_session_for(int(account["id"]), "qrcode", str(result.get("session_id") or ""))
    if session is None:
        # D 没写库（或写失败）：不能返回一个前端没法轮询的 id，直接降级
        message = "登录会话未写入数据库（app.automation.login.start_qrcode_login 未落库），请检查自动化模块"
        logmod.error(message, source="api.accounts", user_id=user["id"])
        raise HttpError(503, message, code="server_error",
                        data={"status": "failed", "message": message})

    # 同账号下旧的 pending 会话作废，避免前端拿着旧 session 一直轮询
    _cancel_pending_sessions(int(account["id"]), keep=str(session["session_id"]),
                             reason="已被新的扫码会话取代")
    data = _session_out(session, account=account)
    data["visible"] = visible
    if visible:
        data["message"] = ("已在本机打开浏览器窗口：请在弹出的窗口里用抖音 App 扫码，"
                            "或改用手机号/密码登录；登录成功后会自动保存，无需其他操作。")
    elif not data["qrcode_image"]:
        # 真实模式首包常常还没二维码（后台线程稍后写库），必须提示前端继续轮询
        data["message"] = data["message"] or "正在生成二维码，请继续轮询登录状态"
    return ok(data)


@route("POST", "/api/v1/douyin/login/sms/send", auth="user", summary="发送短信验证码", tags=("douyin",))
def send_sms_code(req: Request) -> Response:
    user = req.require_user()
    account = require_account_id(req)
    good, phone = validate.phone(req.str_field("phone", "", max_len=32))
    if not good:
        raise HttpError(400, phone)

    func = _login_func("start_sms_login")
    if func is None:
        raise _unavailable("start_sms_login")

    try:
        result = _call_automation(func, "start_sms_login", phone, account, get_settings())
    except HttpError as exc:
        _fail_latest_pending(int(account["id"]), "sms", exc.message)
        raise
    session = _latest_session_for(int(account["id"]), "sms", str(result.get("session_id") or ""))
    if session is None:
        message = "短信登录会话未写入数据库（app.automation.login.start_sms_login 未落库）"
        logmod.error(message, source="api.accounts", user_id=user["id"])
        raise HttpError(503, message, code="server_error",
                        data={"status": "failed", "message": message})
    _cancel_pending_sessions(int(account["id"]), keep=str(session["session_id"]),
                             reason="已被新的短信登录会话取代")
    return ok(_session_out(session, account=account), message="验证码已发送")


@route("POST", "/api/v1/douyin/login/sms/submit", auth="user", summary="提交短信验证码", tags=("douyin",))
def submit_sms_code(req: Request) -> Response:
    req.require_user()
    session = _get_session(req, req.str_field("session_id", "", max_len=64))
    if session["status"] in FINAL_SESSION_STATES:
        raise HttpError(409, f"该登录会话已结束（{session['status']}），请重新发起", code="conflict")
    code = req.str_field("code", "", max_len=12)
    if not code:
        raise HttpError(400, "请输入短信验证码")

    func = _login_func("submit_sms_code")
    if func is None:
        raise _unavailable("submit_sms_code")
    try:
        result = _call_automation(func, "submit_sms_code", str(session["session_id"]), code)
    except HttpError as exc:
        db.update("login_sessions",
                  {"status": "failed", "step": "failed", "message": exc.message[:500],
                   "finished_at": _now(), "updated_at": _now()},
                  "session_id=? AND user_id=?", (session["session_id"], req.user_id))
        raise
    row = _expire_if_needed(_get_session(req, str(session["session_id"])))
    if result.get("accepted") is False and row["status"] not in FINAL_SESSION_STATES:
        raise HttpError(400, str(result.get("message") or "验证码不正确"), code="login_failed")
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (row.get("account_id"),)) if row.get("account_id") else None
    return ok(_session_out(row, account=account), message="已提交，正在完成登录")


@route("GET", "/api/v1/douyin/login/status", auth="user", summary="轮询登录状态", tags=("douyin",))
def login_status(req: Request) -> Response:
    req.require_user()
    session_id = req.str_field("session_id", "", max_len=64)
    row = _get_session(req, session_id)
    # 让 D 的后台推进一次（若可用）。它只读/更新 DB，失败也不影响本接口返回 DB 真实状态
    poll = _login_func("poll")
    if poll is not None and row["status"] in ACTIVE_SESSION_STATES:
        try:
            poll(str(row["session_id"]))
        except Exception as exc:      # noqa: BLE001 - 轮询接口绝不能因自动化层异常而 500
            logmod.warning(f"登录轮询推进失败：{type(exc).__name__}: {exc}", source="api.accounts")
    row = _expire_if_needed(_get_session(req, str(session_id)))
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (row["account_id"],)) if row.get("account_id") else None
    return ok(_session_out(row, account=account))


@route("POST", "/api/v1/douyin/login/cancel", auth="user", summary="取消登录", tags=("douyin",))
def cancel_login(req: Request) -> Response:
    req.require_user()
    session = _get_session(req, req.str_field("session_id", "", max_len=64))
    sid = str(session["session_id"])

    func = _login_func("cancel")
    canceled_remote = False
    if func is not None:
        try:
            canceled_remote = bool(func(sid))
        except Exception as exc:      # noqa: BLE001 - 自动化层异常不该让「取消」失败
            logmod.warning(f"通知自动化层取消失败：{type(exc).__name__}: {exc}", source="api.accounts")

    if session["status"] in ACTIVE_SESSION_STATES:
        # 以 DB 为准：即使 D 不可用也要让用户能取消，否则会话会一直挂到超时
        db.update("login_sessions",
                  {"status": "canceled", "step": "canceled", "message": "用户已取消登录",
                   "finished_at": _now(), "updated_at": _now()},
                  "session_id=? AND user_id=?", (sid, req.user_id))
    row = _get_session(req, sid)
    return ok({"canceled": True, "remote_canceled": canceled_remote,
               "session": _session_out(row)}, message="已取消登录")


@route("POST", "/api/v1/douyin/login/refresh", auth="user", summary="重新生成二维码", tags=("douyin",))
def refresh_qrcode(req: Request) -> Response:
    req.require_user()
    session = _get_session(req, req.str_field("session_id", "", max_len=64))
    if session["status"] in FINAL_SESSION_STATES:
        raise HttpError(409, f"该登录会话已结束（{session['status']}），请重新发起", code="conflict")
    if session["method"] != "qrcode":
        raise HttpError(400, "短信登录会话不能刷新二维码")

    func = _login_func("refresh_qrcode")
    if func is None:
        raise _unavailable("refresh_qrcode")
    try:
        _call_automation(func, "refresh_qrcode", str(session["session_id"]))
    except HttpError as exc:
        db.update("login_sessions",
                  {"status": "failed", "step": "failed", "message": exc.message[:500],
                   "finished_at": _now(), "updated_at": _now()},
                  "session_id=? AND user_id=?", (session["session_id"], req.user_id))
        raise
    row = _expire_if_needed(_get_session(req, str(session["session_id"])))
    account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (row["account_id"],)) if row.get("account_id") else None
    return ok(_session_out(row, account=account), message="二维码已刷新")


@route("POST", "/api/v1/douyin/accounts/<account_id>/check", auth="user",
       summary="检测登录态是否有效", tags=("douyin",))
def check_account(req: Request, account_id: str) -> Response:
    account = _get_account(req, account_id)
    settings = get_settings()
    mode = "session_store"
    valid = False

    store = import_optional("app.automation.session_store")
    checker = getattr(store, "is_session_valid", None) if store is not None else None
    if callable(checker):
        try:
            valid = bool(checker(account, settings))
        except Exception as exc:      # noqa: BLE001 - 检测失败按「不可用」处理，不能 500
            logmod.warning(f"登录态检测失败：{type(exc).__name__}: {exc}", source="api.accounts")
            valid = False
    else:
        # 兜底：只看登录态文件是否存在（弱校验，响应里用 mode 标注清楚）
        mode = "file"
        rel = (account.get("storage_path") or "").strip()
        valid = bool(rel) and (settings.session_root / rel).exists()

    now = _now()
    # 检测结果决定用户下一步动作：无效 → expired（去重新扫码）；有效 → online（可跑任务）
    new_status = "online" if valid else "expired"
    db.update("douyin_accounts", {
        "status": new_status,
        "last_check_at": now,
        "last_error": "" if valid else "登录态无效，请重新扫码登录",
        "updated_at": now,
    }, "id=? AND user_id=?", (account["id"], account["user_id"]))
    return ok({
        "account_id": account["id"],
        "valid": valid,
        "status": new_status,
        "mode": mode,
        "last_check_at": now,
        "message": "登录态有效" if valid else "登录态已失效，请重新扫码登录",
    })


@route("POST", "/api/v1/douyin/accounts/<account_id>/cookies", auth="user",
       summary="手动导入 Cookie 登录态（扫码失败时的兜底）", tags=("douyin",))
def import_cookies(req: Request, account_id: str) -> Response:
    """把用户在浏览器里复制的 Cookie 直接写成登录态，绕开扫码自动化。

    为什么需要这个接口：扫码依赖抖音网页的登录弹层与二维码渲染，实测会遇到
    「弹层打开了但二维码图片一直不出现」（风控/网络/版本差异），此时用户完全
    可以自己在浏览器里登录好，把 Cookie 粘过来 —— 这条路径不依赖任何页面结构。

    请求：``{"cookies": "<粘贴内容>", "verify": true}``；``cookies`` 支持
    Cookie 头 / JSON / Netscape cookies.txt 等多种格式（见
    ``session_store.parse_cookie_text``）。``verify`` 默认 true：会开一次浏览器
    让抖音**真的**认一下这个会话，避免「导入成功但任务全失败」。
    """
    account = _get_account(req, account_id)
    settings = get_settings()
    text = req.str_field("cookies", "", max_len=200_000)
    if not text:
        raise HttpError(400, "请粘贴 Cookie 内容（至少要有 sessionid=...）",
                        code="bad_request")
    verify = req.bool_field("verify", True)

    store = import_optional("app.automation.session_store")
    importer = getattr(store, "import_cookies", None) if store is not None else None
    if not callable(importer):
        raise HttpError(503, "当前版本缺少 Cookie 导入能力（app.automation.session_store）",
                        code="server_error")
    try:
        result = importer(account, text, settings)
    except Exception as exc:      # noqa: BLE001 - 解析/落盘失败给可读 400，不让 500
        logmod.warning(f"导入 Cookie 失败：{type(exc).__name__}: {exc}", source="api.accounts")
        raise HttpError(400, f"导入失败：{exc}", code="bad_request") from exc
    if not result.get("ok"):
        # 常见原因：没粘全 / 没有 sessionid / 格式不认识。都不是 500。
        raise HttpError(400, str(result.get("message") or "Cookie 无效"),
                        code="bad_request")

    now = _now()
    storage_name = str(result.get("path") or "")
    # 先按「已导入」落状态：online 表示「有登录态，可尝试跑任务」。
    # 校验明确失败时再降级成 expired，这样用户至少能在界面上看到原因。
    db.update("douyin_accounts", {
        "status": "online",
        "storage_path": storage_name,
        "storage_updated_at": now,
        "last_check_at": now,
        "last_error": "",
        "updated_at": now,
    }, "id=? AND user_id=?", (account["id"], account["user_id"]))

    verification: dict = {"verified": None, "reason": "skipped", "message": "已跳过校验"}
    if verify:
        loginmod = import_optional("app.automation.login")
        verifier = getattr(loginmod, "verify_imported_session", None) if loginmod else None
        if callable(verifier):
            row = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (account["id"],))
            try:
                verification = verifier(row or account, settings)
            except Exception as exc:      # noqa: BLE001
                verification = {"verified": None, "reason": f"error:{type(exc).__name__}",
                                "message": f"已保存，但校验出错（{exc}）"}
        else:
            verification = {"verified": None, "reason": "unavailable",
                            "message": "已保存，但当前版本不支持校验"}

    if verification.get("verified") is False:
        # 校验明确失败：降级为 expired，避免用户以为可以跑任务
        db.update("douyin_accounts", {
            "status": "expired",
            "last_check_at": now,
            "last_error": "导入的 Cookie 未被抖音接受（可能已过期或复制不全）",
            "updated_at": now,
        }, "id=? AND user_id=?", (account["id"], account["user_id"]))
    elif verification.get("verified") is True:
        db.update("douyin_accounts", {
            "status": "online",
            "last_error": "",
            "updated_at": now,
        }, "id=? AND user_id=?", (account["id"], account["user_id"]))

    row = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (account["id"],))
    logmod.info(f"手动导入 Cookie：account={account['id']} 条数={result.get('cookie_count')} "
                f"校验={verification.get('verified')}",
                scope="user", source="api.accounts", user_id=account.get("user_id"))
    return ok({
        "account": _account_out(row or account),
        "cookie_count": int(result.get("cookie_count") or 0),
        "cookie_names": list(result.get("names") or []),
        "verified": verification.get("verified"),
        "verify_reason": str(verification.get("reason") or ""),
        "nickname": str(verification.get("nickname") or ""),
    }, message=str(verification.get("message") or result.get("message") or "已导入"))


# --------------------------------------------------------------------------
# 执行记录
# --------------------------------------------------------------------------
_RUN_COLUMNS = (
    "r.id, r.task_id, r.user_id, r.account_id, a.nickname AS account_nickname, r.run_date,"
    " r.trigger_type, r.status, r.started_at, r.finished_at, r.duration_ms, r.total_count,"
    " r.success_count, r.fail_count, r.message, r.error_code"
)


def run_records_page(req: Request, user_id: int, *, task_id: int = 0) -> dict:
    """执行记录的分页查询（``/douyin/task-records`` 与 ``/tasks/<id>/runs`` 共用）。

    ``user_id`` 是必填的硬过滤条件；``task_id`` 用于任务维度（自带归属校验）。
    筛选参数：account_id / status / start_date / end_date。
    """
    page, page_size = req.page_args()
    where = ["r.user_id=?"]
    params: list = [user_id]
    if task_id:
        where.append("r.task_id=?")
        params.append(task_id)
    account_id = req.int_field("account_id", 0, minimum=0)
    if account_id:
        where.append("r.account_id=?")
        params.append(account_id)
    status = req.str_field("status", "", max_len=20)
    if status:
        if status not in ("running", "success", "partial", "failed", "skipped", "canceled"):
            raise HttpError(400, "status 取值不正确")
        where.append("r.status=?")
        params.append(status)
    start = req.str_field("start_date", "", max_len=20)
    if start:
        where.append("r.run_date>=?")
        params.append(start)
    end = req.str_field("end_date", "", max_len=20)
    if end:
        where.append("r.run_date<=?")
        params.append(end)
    base = (
        f"SELECT {_RUN_COLUMNS}\n"
        "  FROM task_runs r LEFT JOIN douyin_accounts a ON a.id = r.account_id\n"
        f" WHERE {' AND '.join(where)} ORDER BY r.id DESC"
    )
    return db.paginate(base, params, page, page_size)


@route("GET", "/api/v1/douyin/task-records", auth="user", summary="任务执行记录（分页）", tags=("douyin",))
def task_records(req: Request) -> Response:
    user = req.require_user()
    return ok(run_records_page(req, user["id"]))


@route("GET", "/api/v1/douyin/task-records/<run_id>", auth="user",
       summary="单次执行详情（含每个好友的结果）", tags=("douyin",))
def task_record_detail(req: Request, run_id: str) -> Response:
    user = req.require_user()
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise HttpError(400, "执行记录 ID 不正确") from None
    row = db.query_one(
        f"SELECT {_RUN_COLUMNS}\n"
        "  FROM task_runs r LEFT JOIN douyin_accounts a ON a.id = r.account_id\n"
        " WHERE r.id=? AND r.user_id=?",
        (rid, user["id"]),
    )
    if not row:
        raise HttpError(404, "执行记录不存在或不属于当前用户", code="not_found")
    row["screenshots"] = textutil.as_str_list(
        db.query_scalar("SELECT screenshots FROM task_runs WHERE id=?", (rid,), "[]")
    )
    row["items"] = db.query_all(
        "SELECT id, friend_id, friend_name, status, message, error_code, error_detail,"
        "       attempts, duration_ms, screenshot, created_at, finished_at"
        "  FROM task_run_items WHERE run_id=? ORDER BY id",
        (rid,),
    )
    if row.get("task_id"):
        task = db.query_one(
            "SELECT id, name, task_type, time_of_day, repeat_mode, timezone"
            "  FROM scheduled_tasks WHERE id=? AND user_id=?", (row["task_id"], user["id"]))
        row["task"] = task or None
    return ok(row)


@route("POST", "/api/v1/douyin/task-records/batch-set-failed", auth="user",
       summary="批量把卡住的执行记录标记为失败", tags=("douyin",))
def batch_set_failed(req: Request) -> Response:
    """把一直停在 ``running`` 的执行记录收尾成 ``failed``（浏览器进程意外退出后的兜底）。

    为什么需要它：自动化层进程被系统杀掉时没人再更新 ``task_runs``，记录会永远停在
    running —— 前端看起来像「卡住」，而且该账号当天的重跑也会被这条 running 挡掉。

    归属与幂等：SQL 里带 ``user_id``，别人的 id **静默忽略**（只回计数，不暴露存在性）；
    更新时再带一次 ``status='running'``，并发下不会把刚跑成功的记录改成失败。
    ``duration_ms`` 刻意留空：手动收尾没有可信的完成时刻，不编造耗时。
    """
    user = req.require_user()
    body = req.json(required=False) or {}
    if not isinstance(body, dict):
        raise HttpError(400, "请求体必须是 JSON 对象")

    all_running = req.bool_field("all_running", False)
    if all_running:
        rows = db.query_all(
            "SELECT id FROM task_runs WHERE user_id=? AND status='running' ORDER BY id",
            (user["id"],),
        )
    else:
        ids = textutil.as_int_list(req.field("ids", req.field("run_ids", [])))
        if not ids:
            raise HttpError(400, "请提供 ids/run_ids 列表或 all_running=true")
        if len(ids) > 200:
            raise HttpError(400, "单次最多处理 200 条")
        placeholders = ",".join("?" for _ in ids)
        rows = db.query_all(
            f"SELECT id FROM task_runs WHERE user_id=? AND status='running' AND id IN ({placeholders})",
            (user["id"], *ids),
        )

    target_ids = [int(row["id"]) for row in rows]
    updated = 0
    if target_ids:
        placeholders = ",".join("?" for _ in target_ids)
        updated = db.update(
            "task_runs",
            {
                "status": "failed",
                "finished_at": _now(),
                "error_code": "manual_cancel",
                "message": "用户手动标记失败",
            },
            f"user_id=? AND status='running' AND id IN ({placeholders})",
            (user["id"], *target_ids),
        )
    if updated:
        logmod.audit("batch_set_failed", user=user, target="task_runs",
                     detail=f"ids={target_ids} updated={updated}", ip=req.client_ip)
    return ok({"updated": updated, "ids": target_ids, "all_running": all_running},
              message=f"已标记 {updated} 条执行记录为失败")
