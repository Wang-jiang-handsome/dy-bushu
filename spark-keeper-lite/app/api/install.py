"""安装向导（契约第 1 节）。

注意：``GET /api/v1/health`` **不在这里**，由 ``app/api/core.py`` 提供。
路由表是「后注册覆盖先注册」，多一个 health 定义就会把公共探针的响应结构改掉，
所以这里只保留 ``/install/*``。

为什么安装接口是 public 的
--------------------------
自托管系统的第一次启动发生在「还没有任何账号」的时刻，此时没有管理员可以鉴权。
所以 ``/install/*`` 必须免登录，但要用两道闸门管住：

* ``/install/execute`` **只允许在没有任何管理员时调用**，一旦装好就永久 409；
* 其余接口只读或只做无害探测（``test-db`` 建临时表马上删）。

``/install/check`` 直接复用 ``bootstrap.initialization_status()``，保证安装页看到的
体检结果与后端启动时的判断完全一致（两处各写一份迟早会不一致）。
"""

from __future__ import annotations

import re
import sqlite3
import string
import time

from .. import bootstrap as boot
from .. import db
from ..common import logging as logmod
from ..common import mailer
from ..common import timeutil
from ..common.security import password_problem, random_code
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route
from ..version import PRODUCT, VERSION

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")


@route("GET", "/api/v1/install/check", auth="public", summary="安装体检", tags=("install",))
def install_check(req: Request) -> Response:
    return ok(boot.initialization_status())


@route("POST", "/api/v1/install/execute", auth="public", summary="一键初始化", tags=("install",))
def install_execute(req: Request) -> Response:
    """创建第一个管理员并落库站点信息；已有管理员时拒绝。"""
    settings = get_settings()
    if db.query_one("SELECT id FROM users WHERE role='admin'"):
        raise HttpError(409, "系统已完成初始化，请直接登录（忘记密码用命令行 main.py admin 重置）", code="conflict")

    username = (req.str_field("username", "admin", max_len=32) or "admin").strip()
    password = req.str_field("password", "", max_len=128)
    email = req.str_field("email", "", max_len=128)
    site_name = req.str_field("site_name", "", max_len=64)
    if not _USERNAME_RE.match(username):
        raise HttpError(400, "用户名需为 3~32 位字母、数字、下划线、点或短横线")
    if email and "@" not in email:
        raise HttpError(400, "邮箱格式不正确")
    if password:
        problem = password_problem(password)
        if problem:
            raise HttpError(400, problem)

    generated, secret = boot.ensure_admin(
        settings, username=username, password=password, email=email, silent=True
    )
    if site_name:
        boot.set_config("site.name", site_name, public=True, group="general", description="站点名称")
    boot.set_config(
        "system.installed_at", timeutil.utc_now_str(), group="system", description="安装完成时间"
    )
    logmod.audit("install_execute", scope="system", target=username, detail=f"ip={req.client_ip}")
    logmod.info(f"安装向导完成初始化，管理员 {username}", scope="system", source="install")

    status = boot.initialization_status()
    return ok(
        {
            "installed": True,
            "admin_username": username,
            "password_generated": bool(generated),
            "password": secret if generated else "",
            "site_name": boot.get_config("site.name"),
            "schema_version": status.get("schema_version", ""),
            "ready_for_production": status.get("ready_for_production", False),
        },
        "初始化完成，请立即登录并修改初始密码",
    )


@route(
    "GET",
    "/api/v1/install/latest-migration",
    auth="public",
    summary="已应用/待应用的迁移列表",
    tags=("install",),
)
def latest_migration(req: Request) -> Response:
    applied_rows = db.query_all(
        "SELECT version, name, applied_at, checksum FROM schema_migrations ORDER BY version"
    )
    applied = {str(row["version"]) for row in applied_rows}
    discovered = db.discover_migrations()
    pending = [
        {"version": version, "name": name, "file": path.name}
        for version, name, path in discovered
        if version not in applied
    ]
    return ok(
        {
            "current_version": max(applied) if applied else "",
            "applied": applied_rows,
            "pending": pending,
            "total_files": len(discovered),
            "up_to_date": not pending,
        }
    )


@route(
    "POST",
    "/api/v1/install/test-db",
    auth="public",
    summary="测试数据库可写（建临时表再删除）",
    tags=("install",),
)
def install_test_db(req: Request) -> Response:
    """建一张临时表写一行再删掉——只有真的写进去才算通过。

    表名由固定前缀 + 随机小写字母拼成，不含任何用户输入；下面三行 DDL 用了
    f-string 是**唯一**允许的场景（SQLite 不支持参数化表名），所以额外做了
    字符集断言，确保拼进去的东西一定安全。
    """
    suffix = random_code(8, string.ascii_lowercase)
    if not suffix.isalpha() or not suffix.islower():
        # random_code 只从小写字母表取字符，这里断言是为了让"表名安全"这件事可被审计
        raise HttpError(500, "无法生成安全的探测表名", code="server_error")
    probe = f"probe_{suffix}"
    started = time.time()
    conn = db.get_conn()
    try:
        with db.transaction():
            conn.execute(f"CREATE TABLE {probe} (id INTEGER PRIMARY KEY, note TEXT)")  # sql-safe-table-name
            conn.execute(f"INSERT INTO {probe} (note) VALUES (?)", ("ok",))             # sql-safe-table-name
            row = conn.execute(f"SELECT COUNT(*) FROM {probe}").fetchone()              # sql-safe-table-name
            rows = int(row[0]) if row else 0
            conn.execute(f"DROP TABLE {probe}")                                        # sql-safe-table-name
    except sqlite3.Error as exc:
        # 表名由固定前缀 + 随机小写字母拼出，不含用户输入，因此这里的拼接是安全的
        raise HttpError(500, f"数据库不可写：{exc}", code="server_error") from exc
    return ok(
        {
            "ok": True,
            "writable": True,
            "rows": rows,
            "took_ms": round((time.time() - started) * 1000, 1),
            "journal_mode": str(db.query_scalar("PRAGMA journal_mode", (), "unknown")),
            "path": str(get_settings().db_path),
        }
    )


@route("POST", "/api/v1/install/test-smtp", auth="admin", summary="发送测试邮件", tags=("install",))
def install_test_smtp(req: Request) -> Response:
    """发一封测试邮件。未配置 SMTP 时明确报错，而不是假装成功。"""
    target = req.str_field("to", "", max_len=128) or req.str_field("email", "", max_len=128)
    if not target or "@" not in target:
        raise HttpError(400, "请提供收件邮箱")
    health_info = mailer.health()
    if not health_info.get("smtp_configured"):
        raise HttpError(
            400,
            "SMTP 未配置：请在 .env 里设置 SK_SMTP_HOST / SK_SMTP_PORT / SK_SMTP_USER / "
            "SK_SMTP_PASSWORD / SK_SMTP_FROM",
        )
    subject = f"【{boot.get_config('site.name', PRODUCT)}】SMTP 测试邮件"
    body = (
        f"这是一封来自 {PRODUCT} v{VERSION} 的测试邮件。\n\n"
        f"发送时间（UTC）：{timeutil.utc_now_str()}\n"
        "收到本邮件说明 SMTP 配置可用，验证码与到期提醒可以正常发出。"
    )
    sent, message = mailer.send_email(target, subject, body)
    logmod.audit(
        "install_test_smtp",
        scope="admin",
        user=req.require_admin(),
        target=target,
        detail=f"sent={sent} {message}",
        ip=req.client_ip,
    )
    return ok({"sent": sent, "to": target, "message": message or "已发送", **health_info})
