"""核心元接口：健康检查、版本、路由清单。

这个模块是「永远必须在」的最小集：容器健康检查、前端启动探测、契约自检都依赖它。
特意不放在 ``app/api/install.py`` 里——安装向导属于业务模块，可能被裁剪或写坏，
而健康检查必须始终可用。
"""

from __future__ import annotations

import platform
import sys
import time

from .. import __version__ as _pkg_version
from .. import db
from ..common import timeutil
from ..router import Request, Response, all_routes, ok, route
from ..version import BUILD, PRODUCT, VERSION

_STARTED_AT = time.time()


@route("GET", "/api/v1/health", summary="存活探针", tags=("meta",))
def health(req: Request) -> Response:
    """容器 HEALTHCHECK 与前端启动探测用；不查库以外的东西，保证轻量。

    响应同时给出 ``ok``（布尔）与 ``status``（字符串）两种约定：
    前者便于程序判断，后者便于人看监控面板；两个都留着是为了不让任何调用方踩坑。
    """
    settings = getattr(req.server, "settings", None) if req.server else None
    metrics = getattr(req.server, "metrics", None) if req.server else None
    snapshot: dict = {}
    if metrics is not None:
        try:
            snapshot = metrics.snapshot()
        except Exception:      # noqa: BLE001 - 指标坏了不能导致探针误报服务不可用
            snapshot = {}
    try:
        db_ok = bool(db.health().get("ok"))
    except Exception:          # noqa: BLE001 - 数据库故障要能被探针看到，而不是 500
        db_ok = False
    # 解释器能力：缺 _ssl 时 auth/admin/user 模块整块缺席，探针里必须能看出来，
    # 否则运维只会一直查「为什么登录页报接口不存在」。
    ssl_ok, openssl_version = False, ""
    try:
        import ssl as _ssl_std        # noqa: PLC0415

        ssl_ok = True
        openssl_version = str(getattr(_ssl_std, "OPENSSL_VERSION", ""))
    except Exception:                 # noqa: BLE001
        ssl_ok = False
    return ok(
        {
            "ok": db_ok,
            "db_ok": db_ok,
            "status": "ok" if db_ok else "degraded",
            "product": PRODUCT,
            "version": VERSION,
            "build": BUILD,
            "env": settings.env if settings else "unknown",
            "uptime_seconds": int(snapshot.get("uptime_seconds") or (time.time() - _STARTED_AT)),
            "requests": int(snapshot.get("requests") or 0),
            "errors": int(snapshot.get("errors") or 0),
            "python": platform.python_version(),
            "python_ssl": ssl_ok,
            "openssl": openssl_version,
            "demo_mode": bool(settings.demo_mode) if settings else False,
            "database": db_ok,
            "time": timeutil.utc_now_str(),
        }
    )


@route("GET", "/api/v1/version", summary="版本信息", tags=("meta",))
def version(req: Request) -> Response:
    return ok({"product": PRODUCT, "version": VERSION, "build": BUILD, "package": _pkg_version})


@route("GET", "/api/v1/ping", summary="连通性探测（无需数据库）", tags=("meta",))
def ping(req: Request) -> Response:
    return ok({"pong": True, "ts": int(time.time())})


@route("GET", "/api/v1/runtime", auth="admin", summary="运行指标", tags=("meta",))
def runtime(req: Request) -> Response:
    """管理端排障用：请求量、错误数、慢请求、Python 版本、启动时长。"""
    server = req.server
    metrics = server.metrics.snapshot() if server and hasattr(server, "metrics") else {}
    return ok(
        {
            "metrics": metrics,
            "python": sys.version,
            "argv": sys.argv[:6],
            "started_at": _STARTED_AT,
            "routes": len(all_routes()),
        }
    )
