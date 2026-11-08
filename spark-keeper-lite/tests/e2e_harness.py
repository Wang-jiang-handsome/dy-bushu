"""端到端测试脚手架：在同一进程内起**真实** HTTP 服务，用 urllib 打真实请求。

为什么是进程内而不是 ``python main.py serve``
--------------------------------------------
本机沙箱**禁止启动子进程**，所以测试不能 fork 一个 ``main.py serve``。
``ThreadingHTTPServer`` 在同一进程的 daemon 线程里 ``serve_forever()`` 完全
等价于真实服务（同一个 Handler、同一个鉴权、同一个路由表），只是少了进程边界。
端口用 ``0`` 让系统分配，避免并发跑测试时抢占端口。

数据隔离
--------
每个 harness 实例用 ``backend/data/tmp/e2e_<名字>_<pid>/`` 当 ``SK_DATA_DIR``：
* 系统 TEMP 在沙箱里不可写，所以临时目录必须放在工作区内；
* 不碰开发库 ``backend/data/spark.db``，测试随便造数据；
* ``stop()`` 会删掉目录（Windows 上句柄释放有延迟，带重试）。

已知约束（踩过的坑，别再踩）
----------------------------
1. ``app/config.py`` 的 ``get_settings()`` 是**进程级单例**，``app/router.py`` 的
   路由表是**模块级全局**。因此：每个进程同时只跑一个 harness，测试之间
   **串行**执行（unittest 默认就是串行）；不要调用 ``clear_routes()``，
   否则会把别人已注册的路由清掉。
2. ``app/db.py`` 的连接是 thread-local，且发现 ``db_path`` 变了会自动重连，
   所以换 harness 换数据目录是安全的；但主线程用完要 ``close_conn()``。
3. 沙箱里 ``chrome`` 起不来，所以 ``demo_mode=True``：自动化层走假实现，
   端到端流程仍可验证（真实浏览器只能在部署机确认）。
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent                 # spark-shared/ 或 spark-keeper-pro/
# 两种布局都要能跑：开发树是 root/backend/app，交付树把 backend 拍平到 root/app
BACKEND_DIR = (PROJECT_ROOT / "backend"
               if (PROJECT_ROOT / "backend" / "app" / "config.py").is_file()
               else PROJECT_ROOT)
TMP_ROOT = BACKEND_DIR / "data" / "tmp"         # 系统 TEMP 不可写，统一放这里

# 固定密钥：让 JWT 在同一个测试进程里可复现，也不去写 data/secret.key
FIXED_SECRET = "e2e-test-secret-not-for-production-0123456789abcdef"

# 默认管理员口令（仅测试夹具使用）
ADMIN_PASSWORD = "AdminTest!2026"
USER_PASSWORD = "UserTest!2026"

_APP_READY = False
_SETUP_LOCK = threading.Lock()
# 数据目录必须**每个实例唯一**：Windows 上 sqlite 句柄释放有延迟，
# 复用同名目录会出现「上一个用例的管理员还在库里」这种幽灵故障。
_DIR_SEQ = itertools.count(1)


def ensure_backend_on_path() -> None:
    """把 ``spark-shared/backend`` 放到 sys.path，使 ``import app.*`` 可用。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def _base_env() -> None:
    """进程级环境变量（只在第一次设置，值恒定，不会互相干扰）。"""
    os.environ.setdefault("SK_SECRET", FIXED_SECRET)
    os.environ.setdefault("SK_ENV", "dev")
    os.environ.setdefault("SK_DEMO_MODE", "1")
    os.environ.setdefault("SK_SCHEDULER_ENABLED", "0")


def cleanup_dir(path: Path, attempts: int = 12) -> bool:
    """删临时目录。Windows 上 sqlite/日志句柄释放有延迟，所以要重试。"""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.15)
    return not path.exists()


@dataclass
class ApiResponse:
    """一次 HTTP 调用的结果（4xx/5xx 不抛异常，方便断言状态码）。"""

    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes = b""
    method: str = ""
    path: str = ""

    @property
    def data(self) -> Any:
        return (self.body or {}).get("data") if isinstance(self.body, dict) else None

    @property
    def code(self) -> Any:
        return (self.body or {}).get("code") if isinstance(self.body, dict) else None

    @property
    def message(self) -> str:
        return str((self.body or {}).get("message") or "") if isinstance(self.body, dict) else ""

    def __repr__(self) -> str:      # 断言失败时打印得更短
        return f"<{self.method} {self.path} -> {self.status} code={self.code!r} {self.message[:60]!r}>"


class AppHarness:
    """只初始化应用（迁移 + 路由 + 种子），不起 HTTP 服务。"""

    def __init__(self, name: str = "case", **overrides: Any):
        self.name = name
        self.overrides = overrides
        self.data_dir = TMP_ROOT / f"e2e_{name}_{os.getpid()}_{next(_DIR_SEQ)}"
        self.settings: Any = None
        self.report: dict = {}
        self._started = False

    # ---- 生命周期 ----
    def start(self) -> "AppHarness":
        global _APP_READY
        ensure_backend_on_path()
        _base_env()
        # 目录名带序号，天然不重复；再显式确认没有残留库文件
        cleanup_dir(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        leftover = self.data_dir / "spark.db"
        if leftover.exists():       # 理论上不会发生，发生了就是隔离失效，必须炸出来
            raise RuntimeError(f"测试数据目录未清理干净：{leftover}")
        os.environ["SK_DATA_DIR"] = str(self.data_dir)

        with _SETUP_LOCK:
            from app import bootstrap, db                       # noqa: PLC0415
            from app.config import load_settings, set_settings   # noqa: PLC0415

            cfg = load_settings(
                None,
                overrides={
                    "env": "dev",
                    "host": "127.0.0.1",
                    "port": 0,
                    "data_dir": self.data_dir,
                    "db_path": self.data_dir / "spark.db",
                    "secret": FIXED_SECRET,
                    "demo_mode": True,
                    "scheduler_enabled": False,
                    "worker_id": "e2e-test",
                    # 默认关限流：端到端链路会打几十个请求，不关会自伤自己的测试。
                    # 需要验证限流的用例显式传 rate_limit_per_minute=N。
                    "rate_limit_per_minute": 0,
                    **self.overrides,
                },
            )
            set_settings(cfg)
            db.close_conn()
            self.settings = cfg
            self.report = bootstrap.init_app(cfg, quiet=True)
            _APP_READY = True
        self._started = True
        return self

    def stop(self) -> None:
        if not self._started:
            return
        try:
            ensure_backend_on_path()
            from app import db                                  # noqa: PLC0415

            db.close_conn()
        except Exception:      # noqa: BLE001 - 清理阶段不掩盖真正的失败
            pass
        if not self.overrides.get("keep_files"):
            cleanup_dir(self.data_dir)
        self._started = False

    def __enter__(self) -> "AppHarness":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---- 自省 ----
    def route_methods(self) -> dict[str, set[str]]:
        """运行时路由表（模块级全局，进程内直接可读，不必发请求）。"""
        from app.router import all_routes                       # noqa: PLC0415

        out: dict[str, set[str]] = {}
        for entry in all_routes():
            out.setdefault(entry.path, set()).add(entry.method)
        return out

    def has_route(self, method: str, path: str) -> bool:
        """判断某接口是否已注册（用于「队友还没写完」时优雅跳过）。"""
        from app.router import all_routes                       # noqa: PLC0415

        method = method.upper()
        for entry in all_routes():
            if entry.method == method and entry.regex.match(path):
                return True
        return False

    # ---- 数据夹具（绕过 HTTP，让安全/越权用例不依赖登录接口是否写完）----
    def create_user(self, username: str = "u1", password: str = USER_PASSWORD,
                    role: str = "user", package: str | None = "monthly", **fields: Any) -> dict:
        """建用户。``package="monthly"`` 会顺手挂一个生效套餐。

        为什么要挂套餐：没有套餐的用户调 `/douyin/accounts` 会被额度检查拦成
        402 ``no_active_package``（这是**正确**的产品行为）。大多数用例关心的是
        别的逻辑，所以给个默认套餐；专门验证额度门槛的用例传 ``package=None``。
        """
        from app import db                                      # noqa: PLC0415
        from app.common import timeutil                         # noqa: PLC0415
        from app.common.security import hash_password           # noqa: PLC0415

        now = timeutil.utc_now_str()
        data = {
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
            "status": "active",
            "nickname": username,
            "quota_json": "{}",
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        user_id = db.insert("users", data)
        user = db.query_one("SELECT * FROM users WHERE id=?", (user_id,)) or {}
        if package:
            user = self.grant_package(user, package)
        return user

    def grant_package(self, user: dict, package_code: str = "monthly", days: int = 30) -> dict:
        """给用户挂一个生效套餐（直接改库，等价于「管理员授权套餐」的结果）。

        关键一步是 ``Quota.refresh()``：只写 ``users.package_id`` 而不刷新
        ``quota_json`` 缓存，额度会一直是 0（缓存里 account_limit=0），
        于是加账号报 ``account_limit``——这个坑踩过一次。
        """
        from app import db                                      # noqa: PLC0415
        from app.common import timeutil                         # noqa: PLC0415
        from app.common.quota import Quota                      # noqa: PLC0415

        pkg = db.query_one("SELECT * FROM packages WHERE code=?", (package_code,))
        if pkg is None:
            raise RuntimeError(f"套餐不存在：{package_code}（迁移里应 seed 了 trial/monthly/quarter/yearly）")
        db.update(
            "users",
            {
                "package_id": pkg["id"],
                "package_expire_at": timeutil.add_days_str(days),
                "updated_at": timeutil.utc_now_str(),
            },
            "id=?",
            (user["id"],),
        )
        Quota.refresh(user["id"])       # 重算并落库权益快照
        return db.query_one("SELECT * FROM users WHERE id=?", (user["id"],)) or user

    def create_admin(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
        return self.create_user(username=username, password=password, role="admin")

    def token_for(self, user: dict) -> str:
        from app.common.auth import issue_tokens                # noqa: PLC0415

        return issue_tokens(user, ip="127.0.0.1", user_agent="e2e-harness")["access_token"]

    def db_query(self, sql: str, params: tuple = ()) -> list[dict]:
        from app import db                                      # noqa: PLC0415

        return db.query_all(sql, params)

    def db_one(self, sql: str, params: tuple = ()) -> dict | None:
        from app import db                                      # noqa: PLC0415

        return db.query_one(sql, params)


class ServerHarness(AppHarness):
    """在 daemon 线程里起真实 ``SparkServer``，用 urllib 发请求。"""

    def __init__(self, name: str = "case", static: bool | None = None, http_log: bool = False,
                 **overrides: Any):
        super().__init__(name, **overrides)
        self.httpd: Any = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        # static=None 时自动探测前端产物目录（不存在就按「无前端」跑接口测试）
        self.want_static = static
        self.static_mounted = False
        self.static_index = False
        self.http_log = http_log

    def start(self) -> "ServerHarness":
        super().start()
        from app.http_server import SparkHandler, SparkServer, StaticFiles   # noqa: PLC0415

        static = None
        if self.want_static is not False:
            static_dir = Path(self.settings.static_dir)
            if static_dir.exists():
                static = StaticFiles(static_dir)
                self.static_mounted = True
                self.static_index = (static_dir / "index.html").is_file()
        if not self.http_log:
            # 只是静音「访问日志」——500 的 traceback 走的是 traceback.print_exc()，
            # 依然会打印，所以不会掩盖故障。目的是让测试报告能看清断言结果。
            SparkHandler.log_message = lambda self, fmt, *args: None      # type: ignore[assignment]
        self.httpd = SparkServer(self.settings, static)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.verify_isolation()
        return self

    def verify_isolation(self) -> None:
        """确认「我们请求到的服务」就是「我们自己起的、连隔离库的那个」。

        为什么必须在启动时查一次：Windows 的 SO_REUSEADDR 语义与 Linux 不同，
        两个进程可以同时绑 ``0.0.0.0:同一个端口``，**先绑的那个**接走连接。
        一旦请求打到队友的服务上，我们就会在别人的库里建用户、改配置，
        而断言失败的信息还会误导排查方向（队友真实踩过：卡密在对方库里兑换失败）。

        本 harness 用 ``port=0`` 让系统分配临时端口，理论上不会撞；但这条守卫
        便宜（一次请求）且能挡住「以后有人改成固定端口/子进程起服务」的回归，
        所以保留，且**不匹配就直接炸**，绝不继续跑。
        """
        from app import db                                      # noqa: PLC0415

        expected_db = str(self.settings.db_path)
        health = db.health()
        if health.get("ok") and str(health.get("path")) != expected_db:
            raise RuntimeError(
                f"进程内数据库连的不是隔离库：期望 {expected_db}，实际 {health.get('path')}")

        if not self.has_route("GET", "/api/v1/install/check"):
            return                       # 安装接口还没实现时无法从 HTTP 侧确认，跳过
        resp = self.get("/api/v1/install/check")
        reported = ((resp.data or {}).get("database") or {}).get("path") or ""
        if resp.status == 200 and reported and Path(reported) != Path(expected_db):
            raise RuntimeError(
                "e2e 服务自报的数据库不是隔离库，说明请求打到了别的进程/别的库：\n"
                f"  期望：{expected_db}\n  实际：{reported}\n"
                "  已中止测试（继续跑会污染别人的数据）。")

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        super().stop()

    # ---- HTTP ----
    def call(self, method: str, path: str, body: Any = None, token: str = "",
             headers: dict[str, str] | None = None, timeout: float = 20.0) -> ApiResponse:
        url = self.base_url + path
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - 本机回环
                raw = resp.read()
                return ApiResponse(self._parse_status(resp.status), self._parse_body(raw),
                                   {k.lower(): v for k, v in resp.headers.items()}, raw,
                                   method.upper(), path)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                return ApiResponse(exc.code, self._parse_body(raw),
                                   {k.lower(): v for k, v in (exc.headers or {}).items()}, raw,
                                   method.upper(), path)
            finally:
                exc.close()          # 不 close 会触发 ResourceWarning（HTTPError 是响应对象）

    @staticmethod
    def _parse_status(status: Any) -> int:
        try:
            return int(status)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_body(raw: bytes) -> Any:
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"_raw": raw[:400].decode("utf-8", "replace")}

    def get(self, path: str, token: str = "", **kw: Any) -> ApiResponse:
        return self.call("GET", path, token=token, **kw)

    def post(self, path: str, body: Any = None, token: str = "", **kw: Any) -> ApiResponse:
        return self.call("POST", path, body, token=token, **kw)

    def put(self, path: str, body: Any = None, token: str = "", **kw: Any) -> ApiResponse:
        return self.call("PUT", path, body, token=token, **kw)

    def patch(self, path: str, body: Any = None, token: str = "", **kw: Any) -> ApiResponse:
        return self.call("PATCH", path, body, token=token, **kw)

    def delete(self, path: str, token: str = "", **kw: Any) -> ApiResponse:
        return self.call("DELETE", path, token=token, **kw)

    # ---- 常用链路封装（接口没写完时抛 AssertionError，由用例 skip 掉）----
    def login(self, identifier: str, password: str) -> ApiResponse:
        return self.post("/api/v1/auth/user/login", {"identifier": identifier, "password": password})

    def register(self, username: str, password: str = USER_PASSWORD, **extra: Any) -> ApiResponse:
        payload = {
            "username": username,
            "password": password,
            "confirm_password": password,
            "email": "",
            "phone": "",
            "verify_code": "",
            "nickname": username,
            "invite_code": "",
            **extra,
        }
        return self.post("/api/v1/auth/user/register", payload)

    def access_token_of(self, response: ApiResponse) -> str:
        data = response.data or {}
        return str(data.get("access_token") or data.get("token") or "")


def json_dump(obj: Any) -> str:
    """测试输出里打印中文 JSON。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
