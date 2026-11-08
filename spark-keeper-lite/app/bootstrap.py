"""启动装配：迁移数据库、写入种子数据、加载路由、创建根管理员。

所有入口（``main.py``、容器启动脚本、测试）都调用 ``init_app()``，
保证「任何方式启动都得到同一个已就绪的应用」。
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

from . import db
from .common import logging as logmod
from .common import timeutil
from .config import Settings, get_settings
from .router import all_routes, install_index
from .version import VERSION

# 会被自动导入的接口模块（顺序无关，模块内部自己注册路由）
# ``app.payment`` 只有 pro 版存在；lite 版缺失时会被 load_api_modules 跳过。
API_MODULES = (
    "app.api.core",
    "app.api.install",
    "app.api.auth",
    "app.api.public",
    "app.api.user",
    "app.api.accounts",
    "app.api.tasks",
    "app.api.packages",
    "app.api.cardkeys",
    "app.api.orders",
    "app.api.admin",
    "app.api.files",
    "app.payment",
)

_loaded = False
_init_report: dict = {}


def load_api_modules(extra: list[str] | None = None) -> list[str]:
    """导入接口模块。

    只跳过「模块自身不存在」的情况（lite 版没有 ``app.payment``），
    模块内部 import 失败必须抛出来——否则会变成「路由悄悄少了一半」的幽灵故障。
    """
    loaded: list[str] = []
    missing: list[str] = []
    broken: list[str] = []
    for name in list(API_MODULES) + list(extra or []):
        try:
            importlib.import_module(name)
            loaded.append(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                missing.append(name)      # lite 版没有 app.payment，属正常
                continue
            broken.append(name)
            logmod.log(
                f"接口模块 {name} 依赖缺失：{exc.name}（该模块的接口不会注册）",
                level="error",
                source="bootstrap",
            )
        except Exception as exc:          # noqa: BLE001 - 一个模块写坏不能让整站起不来
            broken.append(name)
            logmod.log(
                f"接口模块 {name} 加载失败：{type(exc).__name__}: {exc}（该模块的接口不会注册）",
                level="error",
                source="bootstrap",
            )
    if missing:
        logmod.log(f"未安装的可选接口模块（正常）：{', '.join(missing)}", level="info", source="bootstrap")
    load_api_modules.last_report = {"loaded": loaded, "missing": missing, "broken": broken}
    return loaded


#: 启动时检查的「解释器能力」缺失项。缺了这些不是配置问题，是 Python 装残了。
RUNTIME_REQUIREMENTS = (
    ("_ssl", "登录/注册/鉴权接口全部不可用（Python 编译时没带 OpenSSL）"),
    ("sqlite3", "数据库不可用"),
    ("hashlib", "密码哈希与 JWT 签名不可用"),
)


def runtime_problems() -> list[str]:
    """返回「当前 Python 缺了什么」的可读说明列表（正常时为空）。

    真实事故：CentOS 7 上用宝塔装的 Python 没编 OpenSSL → `_ssl` 缺失 →
    auth/admin/user 模块整块注册失败 → 登录页只显示「接口不存在」，完全看不出原因。
    """
    problems: list[str] = []
    for name, impact in RUNTIME_REQUIREMENTS:
        try:
            __import__(name)
        except Exception as exc:                     # noqa: BLE001
            problems.append(f"缺少模块 {name}：{impact}（{type(exc).__name__}）")
    return problems


DEFAULT_SITE_CONFIGS: dict[str, tuple[str, int, str, str]] = {
    "site.name": ("抖音续火花系统", 1, "general", "站点名称"),
    # 注意：前端产物在 web/static/ 下，URL 前缀是根路径，
    # 所以 logo 的正确地址是 /logo.svg —— 写成 /static/logo.svg 会 404
    # （站点若自定义 logo 需要放进 web/static/ 并用根路径引用）。
    "site.logo": ("/logo.svg", 1, "general", "站点 Logo 地址"),
    "site.icp": ("", 1, "general", "备案号"),
    "site.customer_service": ("", 1, "general", "客服联系方式"),
    "site.notice": ("", 1, "general", "登录页公告"),
    "auth.allow_register": ("1", 1, "auth", "是否开放注册"),
    "auth.require_email_verify": ("0", 1, "auth", "注册是否必须邮箱验证"),
    "auth.require_invite": ("0", 1, "auth", "注册是否需要邀请码"),
    "task.default_time": ("08:30", 1, "task", "默认执行时间"),
    "task.default_timezone": ("Asia/Shanghai", 1, "task", "默认时区"),
    "task.max_concurrent": ("2", 0, "task", "并发浏览器上限"),
    "log.retention_days": ("30", 0, "system", "日志保留天数"),
}


def seed_site_configs() -> None:
    """补齐缺失的站点配置项（幂等；不覆盖已有值）。

    额外做一次「已知错误默认值」的纠正：早期版本把 logo 写成 ``/static/logo.svg``，
    而实际文件在 ``/logo.svg``（前端产物目录本身就是 URL 根），会导致每页一个 404。
    只在该键仍然是这个错误值时纠正，管理员自定义过就不动。
    """
    now = timeutil.utc_now_str()
    for key, (value, public, group, desc) in DEFAULT_SITE_CONFIGS.items():
        if not db.query_one("SELECT key FROM site_configs WHERE key=?", (key,)):
            db.insert(
                "site_configs",
                {
                    "key": key,
                    "value": value,
                    "is_public": public,
                    "group_name": group,
                    "description": desc,
                    "updated_at": now,
                },
            )
    # 纠正历史错误值（只改这一种已知错误，避免覆盖用户配置）
    broken = db.query_one("SELECT value FROM site_configs WHERE key='site.logo'")
    if broken and str(broken["value"]).strip() == "/static/logo.svg":
        db.update("site_configs", {"value": "/logo.svg", "updated_at": now}, "key='site.logo'")


def get_config(key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM site_configs WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else default


def set_config(key: str, value: str, *, public: bool | None = None, group: str = "general",
               description: str = "") -> None:
    now = timeutil.utc_now_str()
    if db.query_one("SELECT key FROM site_configs WHERE key=?", (key,)):
        data = {"value": str(value), "updated_at": now}
        if public is not None:
            data["is_public"] = 1 if public else 0
        db.update("site_configs", data, "key=?", (key,))
    else:
        db.insert(
            "site_configs",
            {
                "key": key,
                "value": str(value),
                "is_public": 1 if public else 0,
                "group_name": group,
                "description": description,
                "updated_at": now,
            },
        )


def public_configs(include_keys: bool = True) -> dict:
    rows = db.query_all("SELECT key, value FROM site_configs WHERE is_public=1")
    out = {row["key"]: row["value"] for row in rows}
    if not include_keys:
        return out
    return out


def ensure_admin(settings: Settings | None = None, *, username: str = "", password: str = "",
                 email: str = "", silent: bool = False) -> tuple[bool, str]:
    """确保存在一个可登录的管理员。

    密码来源优先级：参数 > ``SK_ADMIN_PASSWORD`` > 自动生成（打印一次）。
    已存在管理员时**不会**改动其密码，除非显式传入 ``password`` 且用户不存在。
    """
    from .common.security import hash_password, password_problem

    settings = settings or get_settings()
    username = (username or settings.bootstrap_admin_user or "admin").strip()
    existing_admin = db.query_one("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1")
    if existing_admin and not password:
        return False, f"管理员已存在：{existing_admin['username']}"

    password = password or os.environ.get("SK_ADMIN_PASSWORD", "").strip()
    generated = False
    if not password:
        import secrets

        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        password = "".join(secrets.choice(alphabet) for _ in range(12)) + "8a"
        generated = True
    problem = password_problem(password)
    if problem:
        raise ValueError(f"管理员密码不合规：{problem}")

    now = timeutil.utc_now_str()
    user = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if user:
        db.update(
            "users",
            {"password_hash": hash_password(password), "role": "admin", "status": "active", "updated_at": now},
            "id=?",
            (user["id"],),
        )
        action = "更新"
    else:
        db.insert(
            "users",
            {
                "email": email or settings.bootstrap_admin_email or None,
                "username": username,
                "password_hash": hash_password(password),
                "role": "admin",
                "status": "active",
                "nickname": "管理员",
                "quota_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )
        action = "创建"
    if not silent:
        banner = f"  {action}管理员成功：{username}"
        print(banner)
        if generated:
            print(f"  初始密码（仅显示这一次，请立即登录后修改）：{password}")
    return generated, password


def init_app(settings: Settings | None = None, *, migrate_db: bool = True,
             load_routes: bool = True, quiet: bool = False) -> dict:
    """幂等初始化。返回初始化报告（供 /api/v1/install/check 展示）。"""
    global _loaded, _init_report

    started = time.time()
    settings = settings or get_settings()
    settings.ensure_dirs()

    applied: list[str] = []
    if migrate_db:
        applied = db.migrate(verbose=not quiet)
        db.ensure_columns()          # 老库补列（别人已建好表时也要生效）
        seed_site_configs()
        install_index(None)

    loaded: list[str] = []
    if load_routes:
        loaded = load_api_modules()
        _loaded = True

    problems = runtime_problems()
    _init_report = {
        "version": VERSION,
        "env": settings.env,
        "data_dir": str(settings.data_dir),
        "db_path": str(settings.db_path),
        "migrations_applied": applied,
        "api_modules": loaded,
        #: 加载失败的模块（例如缺 _ssl 时的 auth/admin/user）——排障时最关键的信息
        "api_modules_broken": list(getattr(load_api_modules, "last_report", {}).get("broken") or []),
        "routes": len(all_routes()),
        "schema_version": db.query_scalar("SELECT MAX(version) FROM schema_migrations", "", ),
        "took_ms": round((time.time() - started) * 1000, 1),
        # 解释器能力缺失（最典型是缺 _ssl）：不是配置错误，必须让人一眼看到
        "runtime_problems": problems,
        "degraded": bool(problems),
        "ready": True,
    }
    if problems:
        # 用醒目的横幅而不是一行 error：缺 _ssl 时站点基本等于不可用，必须放在最前面
        print("")
        print("  " + "=" * 66)
        print("  [严重] 当前 Python 解释器缺少关键模块，站点功能会大量缺失：")
        for item in problems:
            print(f"    - {item}")
        print("  修法（任选其一）：")
        print("    1) 用自带 OpenSSL 的 Python：")
        print("       curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.11")
        print("       然后用它启动，或重跑 deploy/baota-install.sh --python <上面的路径>")
        print("    2) yum/apt 装 openssl-devel 后重新编译安装 Python")
        print("    3) 直接用 Docker 部署（镜像自带完整 OpenSSL）")
        print("  " + "=" * 66)
        print("")
    if not quiet:
        print(f"  [init] v{VERSION} · 路由 {_init_report['routes']} 条 · "
              f"迁移 {len(applied)} 个 · 耗时 {_init_report['took_ms']}ms")
    return _init_report


def bootstrap(settings: Settings | None = None) -> dict:
    """服务器启动前调用（``serve()`` 内部已调用）。"""
    report = init_app(settings, quiet=True)
    settings = settings or get_settings()
    if not db.query_one("SELECT id FROM users WHERE role='admin'"):
        if settings.env == "dev":
            ensure_admin(settings, silent=settings.env != "dev")
    return report


def initialization_status() -> dict:
    """给安装向导用的体检结果。"""
    settings = get_settings()
    admin = db.query_one("SELECT username, created_at FROM users WHERE role='admin' ORDER BY id LIMIT 1")
    users = int(db.query_scalar("SELECT COUNT(*) FROM users", (), 0) or 0)
    cloud = check_cloud_environment()
    return {
        "installed": bool(admin),
        "admin_username": admin["username"] if admin else "",
        "user_count": users,
        "schema_version": str(db.query_scalar("SELECT MAX(version) FROM schema_migrations", "", ) or ""),
        "database": db.health(),
        "environment": cloud,
        "site": {
            "name": get_config("site.name", "抖音续火花系统"),
            "allow_register": get_config("auth.allow_register", "1") == "1",
        },
        "version": VERSION,
        "ready_for_production": bool(admin) and cloud["python_ok"],
    }


def check_cloud_environment() -> dict:
    """云端环境体检：Python 版本、磁盘、内存、Chrome、时区库、端口。"""
    import platform
    import shutil

    from .automation.chrome import find_browser

    settings = get_settings()
    disk = shutil.disk_usage(str(settings.data_dir))
    mem_total_mb = 0
    try:      # Linux 容器
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    mem_total_mb = int(int(line.split()[1]) / 1024)
                    break
    except OSError:
        mem_total_mb = 0

    browser = find_browser(settings.chrome_path)
    tzdata_ok = True
    try:
        from .common.timeutil import get_tz

        get_tz("Asia/Shanghai")
    except Exception:      # noqa: BLE001
        tzdata_ok = False

    return {
        "python_version": platform.python_version(),
        "python_ok": sys.version_info >= (3, 9),
        "platform": f"{platform.system()} {platform.release()}",
        "disk_free_gb": round(disk.free / 1024 ** 3, 2),
        "disk_total_gb": round(disk.total / 1024 ** 3, 2),
        "memory_total_mb": mem_total_mb,
        "browser_path": browser or "",
        "browser_found": bool(browser),
        "tzdata_ok": tzdata_ok,
        "demo_mode": settings.demo_mode,
        "data_dir": str(settings.data_dir),
        "timezone": settings.timezone,
    }


def reload_routes() -> int:
    """重新加载接口模块（开发模式热重载用）。"""
    from .router import clear_routes

    clear_routes()
    install_index(None)
    load_api_modules()
    return len(all_routes())
