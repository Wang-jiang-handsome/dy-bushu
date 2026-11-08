"""HTTP 服务器：标准库 ThreadingHTTPServer + 路由分发 + 静态资源。

职责边界
--------
* 只管「HTTP 层」：解析请求、鉴权、CORS/安全响应头、限流、静态文件、错误兜底。
* 业务全部在 ``app/api/*`` 的处理器里。

为什么不用 FastAPI/Flask
------------------------
自托管要的是「拷过去就能跑」。标准库方案没有编译依赖、没有版本冲突，
容器镜像也更小。代价是要自己写路由/鉴权（已在 ``router.py`` 完成）。
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import db
from .config import Settings, get_settings
from .router import HttpError, Request, Response, all_routes, match

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")

MAX_BODY = 8 * 1024 * 1024          # 8MB（上传截图/logo 足够）
API_PREFIX = "/api/"

# 历史/浏览器惯用路径 → 实际存在的静态文件。只放「确认有对应文件」的映射，
# 命中即 302；目标不存在时自然落到 404，不会造成重定向死循环。
LEGACY_STATIC_ALIASES = {
    "/static/logo.svg": "/logo.svg",          # 早期默认配置写多了一层 static/
    "/static/favicon.ico": "/favicon.svg",    # 浏览器自动请求 /favicon.ico
    "/favicon.ico": "/favicon.svg",
}

# 不需要按分钟限流的路径（登录/验证码另有更严的单独限流）
RATE_LIMIT_EXEMPT = ("/api/v1/health", "/api/v1/install/", "/static/", "/assets/")


# --------------------------------------------------------------------------
# 限流（内存版：单进程够用；多进程请交给反向代理/网关）
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        if self.per_minute <= 0:
            return True, 0
        now = time.time()
        window = 60.0
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                retry = int(window - (now - bucket[0])) + 1
                return False, max(1, retry)
            bucket.append(now)
            if len(self._hits) > 20000:      # 防内存无限增长
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    self._hits.pop(k, None)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# 登录失败锁定（按 用户名+IP）
class LoginGuard:
    def __init__(self, max_fail: int, lock_seconds: int):
        self.max_fail = max_fail
        self.lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        items = [t for t in self._fails.get(key, []) if now - t < self.lock_seconds]
        self._fails[key] = items
        return items

    def locked_for(self, key: str) -> int:
        if self.max_fail <= 0:
            return 0
        now = time.time()
        with self._lock:
            items = self._prune(key, now)
            if len(items) >= self.max_fail:
                return int(self.lock_seconds - (now - items[0])) + 1
            return 0

    def record_fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            self._fails[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# --------------------------------------------------------------------------
# 静态文件
# --------------------------------------------------------------------------
def _has_content_hash(name: str) -> bool:
    """判断文件名里是否带内容指纹（如 app.9f2c31ab.js / index-BykcusW7.js）。

    带指纹的文件内容一变文件名就变，可以放心长缓存 + immutable；
    不带指纹的（app.js、style.css）必须每次协商，否则用户会长时间看到旧代码。
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for part in stem.replace("-", ".").replace("_", ".").split("."):
        if len(part) >= 8 and all(c in "0123456789abcdefABCDEF" for c in part):
            return True
    return False


class StaticFiles:
    """托管前端构建产物，并对 SPA 路由做 index.html 回退。"""

    def __init__(self, root: Path, spa_fallback: bool = True):
        self.root = root
        self.spa_fallback = spa_fallback

    def resolve(self, url_path: str) -> Path | None:
        rel = url_path.lstrip("/")
        if not rel:
            rel = "index.html"
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None                       # 目录穿越
        if candidate.is_file():
            return candidate
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate / "index.html"
        return None

    def response(self, url_path: str, request_headers: dict[str, str]) -> Response | None:
        path = self.resolve(url_path)
        if path is None:
            if self.spa_fallback and not url_path.startswith(("/api/", "/static/", "/assets/")):
                path = self.resolve("/index.html")
            if path is None:
                return None
        try:
            data = path.read_bytes()
            stat = path.stat()
        except OSError:
            return None
        ctype, _ = mimetypes.guess_type(str(path))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
            "image/svg+xml",
            "application/manifest+json",
        ):
            ctype += "; charset=utf-8"
        headers: dict[str, str] = {}

        # ------------------------------------------------------------------
        # 缓存策略（这里踩过一次真实的坑，别改回纯 max-age）
        # ------------------------------------------------------------------
        # 早期版本对所有 js/css 一律 `Cache-Control: public, max-age=86400`
        # 且不给 ETag/Last-Modified —— 浏览器在 24 小时内**根本不会来问服务器**，
        # 于是「磁盘上的前端已更新、用户看到的仍是旧代码」，表现为
        # 「明明修好了，还是登录后不跳转」。自托管场景要的是「改完刷新就生效」。
        #
        # 现在的策略：
        #   * 带内容指纹的静态资源（/assets/xxx.<hash>.js）→ 长缓存 + immutable
        #   * 其它静态资源 → no-cache + ETag「每次都协商」：没变返回 304（很快），
        #     变了立刻拿到新内容。自托管带宽充裕，用一点协商换「不漏更新」很划算。
        etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        if "immutable" in str(path) or _has_content_hash(path.name):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Cache-Control"] = "no-cache"
            headers["ETag"] = etag
        if request_headers.get("if-none-match") == etag:
            return Response(status=304, raw=b"", content_type=ctype, headers=headers)

        body = data
        accept = request_headers.get("accept-encoding", "")
        if len(data) > 1024 and "gzip" in accept and path.suffix in (".html", ".js", ".css", ".json", ".svg"):
            body = gzip.compress(data, 6)
            headers["Content-Encoding"] = "gzip"
        return Response(status=200, raw=body, content_type=ctype, headers=headers)


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------
class SparkHandler(BaseHTTPRequestHandler):
    server_version = "SparkKeeper"
    protocol_version = "HTTP/1.1"

    # --- 让日志走我们自己的 logger（app/common/logging.py），而不是 stderr 裸打印 ---
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        settings = self.server.settings           # type: ignore[attr-defined]
        if settings.env == "dev":
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    # --- 工具 ---
    @property
    def settings(self) -> Settings:
        return self.server.settings               # type: ignore[attr-defined]

    def _client_ip(self) -> str:
        settings = self.settings
        forwarded = self.headers.get("x-forwarded-for", "")
        if forwarded and self._peer_trusted():
            return forwarded.split(",")[0].strip()
        real = self.headers.get("x-real-ip", "")
        if real and self._peer_trusted():
            return real.strip()
        return self.client_address[0]

    def _peer_trusted(self) -> bool:
        """只有来自可信代理的对端才采信 X-Forwarded-For，否则可被伪造。"""
        settings = self.settings
        if not settings.trusted_proxies:
            return False
        peer = self.client_address[0]
        return peer in settings.trusted_proxies or peer in ("127.0.0.1", "::1")

    def _scheme(self) -> str:
        return (self.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()

    # --- GET/HEAD/POST/... 统一入口 ---
    def do_GET(self) -> None:            # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:           # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:           # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:            # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:          # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:         # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:        # noqa: N802
        self._send(
            Response(status=204, raw=b"", headers=self._cors_headers())
        )

    def _cors_headers(self) -> dict[str, str]:
        """跨域响应头。

        默认**不开**跨域（同源部署最安全）。两种放行方式：

        1. ``SK_ENV=dev``：开发时任意来源都放行，免得本地 5173 调 8000 被拦；
        2. ``SK_CORS_ORIGINS=https://a.com,https://b.com``：生产白名单。

        为什么需要 2：把前端静态文件放到 Netlify/Vercel/对象存储这类静态托管上，
        后端继续跑在有 Python + Chromium 的服务器上 —— 这时浏览器发的是跨域请求。
        鉴权走 ``Authorization`` 头（JWT，不用 Cookie），所以不需要
        ``Allow-Credentials``，白名单精确匹配即可。
        """
        origin = self.headers.get("origin", "")
        settings = self.settings
        allowlist = tuple(getattr(settings, "cors_origins", ()) or ())
        if origin and settings.env == "dev":
            allow = origin
        elif origin and origin.rstrip("/") in allowlist:
            allow = origin
        else:
            allow = ""
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Token, X-Requested-With",
            "Access-Control-Max-Age": "600",
        }
        if allow:
            headers["Access-Control-Allow-Origin"] = allow
            headers["Vary"] = "Origin"
        return headers

    def _missing_api_response(self, path: str) -> Response:
        """接口没注册时的响应。

        如果缺的是 **``_ssl`` 模块**（Python 编译时没带 OpenSSL），那 auth/admin/user
        这些模块会整块缺席 —— 用户看到的是「接口不存在」，根本猜不到真正原因。
        真实事故：CentOS 7 上用宝塔装的 Python 没编 OpenSSL，登录页一直报「接口不存在」。
        所以这里把它翻译成 503 + ``python_missing_ssl``，并直接给出修法。
        """
        try:
            import _ssl  # noqa: F401
            has_ssl = True
        except Exception:                          # noqa: BLE001
            has_ssl = False
        if not has_ssl:
            return Response.json(
                {
                    "code": "python_missing_ssl",
                    "message": (
                        "服务端 Python 缺少 _ssl 模块（编译时没有 OpenSSL），"
                        "登录/鉴权相关接口无法注册。修法三选一：\n"
                        "1) 换用自带 OpenSSL 的 Python："
                        "curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.11，"
                        "然后用它的路径启动（或重跑 deploy/baota-install.sh --python <路径>）；\n"
                        "2) 先 yum install -y openssl-devel，再重新编译/安装 Python；\n"
                        "3) 直接用 Docker 部署（镜像自带完整 OpenSSL）。"
                    ),
                    "path": path,
                },
                status=503,
            )
        return Response.json({"code": "not_found", "message": "接口不存在"}, status=404)

    def _handle(self, method: str) -> None:
        started = time.time()
        try:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")

            # 限流
            limiter: RateLimiter = self.server.limiter          # type: ignore[attr-defined]
            if not any(path.startswith(p) for p in RATE_LIMIT_EXEMPT):
                allowed, retry = limiter.allow(self._client_ip())
                if not allowed:
                    self._send(
                        Response.json(
                            {"code": "too_many_requests", "message": "请求过于频繁，请稍后再试"},
                            status=429,
                            headers={"Retry-After": str(retry)},
                        )
                    )
                    return

            # 静态资源
            static: StaticFiles = self.server.static          # type: ignore[attr-defined]
            if not path.startswith(API_PREFIX):
                # 兼容历史路径：早期默认配置把 logo 写成 /static/logo.svg
                # （多写了一层 static/，实际文件在 web/static/logo.svg → URL 根 /logo.svg），
                # 以及浏览器自动请求的 /favicon.ico（实际只有 favicon.svg）。
                # 只做这几条重定向，并且**先确认目标文件存在**，避免重定向成死循环。
                if method in ("GET", "HEAD") and path in LEGACY_STATIC_ALIASES:
                    target = LEGACY_STATIC_ALIASES[path]
                    if static is not None and static.resolve(target) is not None:
                        self._send(Response.redirect(target, 302))
                        return
                if method in ("GET", "HEAD") and static is not None:
                    resp = static.response(path, {k.lower(): v for k, v in self.headers.items()})
                    if resp is not None:
                        self._send(resp, method)
                        return
                if method not in ("GET", "HEAD"):
                    self._send(self._missing_api_response(path))
                    return
                self._send(Response.json(
                    {"code": "not_found", "message": "页面不存在"}, status=404))
                return

            # 读 body
            body = b""
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                self._send(Response.json(
                    {"code": "payload_too_large", "message": "请求体过大"}, status=413))
                return
            if length:
                body = self.rfile.read(length)

            entry, params, path_exists = match(method, path)
            if entry is None:
                if path_exists:
                    self._send(Response.json(
                        {"code": "method_not_allowed", "message": "请求方法不被允许"}, status=405))
                else:
                    self._send(self._missing_api_response(path))
                return

            request = Request(
                method=method,
                path=path,
                params=params,
                query={k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()},
                raw_query=parts.query,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                client_ip=self._client_ip(),
                scheme=self._scheme(),
                server=self.server,
            )
            self.server.authenticate(request, entry)    # type: ignore[attr-defined]
            result = entry.handler(request, **params)
            if result is None:
                result = Response.json({"code": 0, "message": "ok", "data": None})
            if isinstance(result, (dict, list)):
                result = Response.json(result)
            self._send(result, method)
        except HttpError as exc:
            self._send(Response.json(
                {"code": exc.code, "message": exc.message, "data": exc.data},
                status=exc.status,
                headers=self._cors_headers(),
            ))
        except BrokenPipeError:      # 客户端提前断开：正常现象，不记错误
            return
        except Exception as exc:     # noqa: BLE001 - 兜底，避免线程崩掉
            traceback.print_exc()
            self.server.record_error(exc, self.path)     # type: ignore[attr-defined]
            payload = {"code": "server_error", "message": "服务器内部错误"}
            if self.settings.env == "dev":
                payload["detail"] = f"{type(exc).__name__}: {exc}"
            try:
                self._send(Response.json(payload, status=500))
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            self.server.metrics.record(self.path, time.time() - started)   # type: ignore[attr-defined]

    def _send(self, resp: Response, method: str = "GET") -> None:
        try:
            body = resp.raw
            if body is None:
                body = json.dumps(resp.body, ensure_ascii=False, default=str).encode("utf-8")
            headers = {
                "Content-Type": resp.content_type,
                "Content-Length": str(len(body)),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Frame-Options": "SAMEORIGIN",
            }
            headers.update(resp.headers)
            headers.update(self._cors_headers())
            self.send_response(resp.status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


class Metrics:
    """极简运行指标（管理端 /admin/system-environment 展示）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.slow: deque[tuple[str, float]] = deque(maxlen=50)
        self.started_at = time.time()
        self.last_error: str = ""

    def record(self, path: str, seconds: float) -> None:
        ms = seconds * 1000
        with self.lock:
            self.requests += 1
            self.total_ms += ms
            self.max_ms = max(self.max_ms, ms)
            if ms > 1000:
                self.slow.append((path, round(ms, 1)))

    def record_error(self, exc: Exception, path: str = "") -> None:
        with self.lock:
            self.errors += 1
            self.last_error = f"{path} {type(exc).__name__}: {exc}"

    def snapshot(self) -> dict:
        with self.lock:
            avg = (self.total_ms / self.requests) if self.requests else 0.0
            return {
                "requests": self.requests,
                "errors": self.errors,
                "avg_ms": round(avg, 2),
                "max_ms": round(self.max_ms, 2),
                "uptime_seconds": int(time.time() - self.started_at),
                "slow": list(self.slow),
                "last_error": self.last_error,
            }


class SparkServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, settings: Settings, static: StaticFiles | None = None):
        self.settings = settings
        self.static = static
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.login_guard = LoginGuard(settings.max_login_fail, settings.login_lock_seconds)
        self.metrics = Metrics()
        self.authenticator = default_authenticator
        self.version = _version()
        super().__init__((settings.host, settings.port), SparkHandler)

    # 由 bootstrap 注入（避免 api 层反向 import 服务器）
    def authenticate(self, request: Request, entry) -> None:
        self.authenticator(request, entry)

    def record_error(self, exc: Exception, path: str = "") -> None:
        self.metrics.record_error(exc, path)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def _version() -> str:
    from .version import VERSION

    return VERSION


# --------------------------------------------------------------------------
# 默认鉴权：由 bootstrap 覆写（这里给出可直接使用的实现）
# --------------------------------------------------------------------------
def default_authenticator(request: Request, entry) -> None:
    """校验 JWT 并加载用户；``user``/``admin`` 级别未登录直接 401。"""
    level = getattr(entry, "auth", "public")
    token = request.bearer_token()
    if token:
        from .common.security import TokenError, verify_jwt

        try:
            payload = verify_jwt(token)
        except TokenError as exc:
            if level != "public":
                raise HttpError(401, exc.message) from exc
            payload = None
        if payload:
            session = db.query_one(
                "SELECT * FROM sessions WHERE jti=? AND revoked_at IS NULL", (payload.get("jti"),)
            )
            if session is None and level != "public":
                raise HttpError(401, "登录状态已失效，请重新登录")
            user = db.query_one("SELECT * FROM users WHERE id=?", (payload.get("sub"),))
            if user:
                request.auth = payload
                request.user = user
    if level == "user":
        request.require_user()
    elif level == "admin":
        request.require_admin()
    elif level == "public" and request.user and request.user.get("status") != "active":
        request.user = None    # 公开接口里带着被禁用账号的 token，就当没登录


def serve(settings: Settings | None = None, open_browser: bool = False) -> None:
    """启动服务器（阻塞）。"""
    from .bootstrap import bootstrap

    settings = settings or get_settings()
    settings.ensure_dirs()
    bootstrap(settings)
    static = StaticFiles(settings.static_dir) if settings.static_dir.exists() else None
    httpd = SparkServer(settings, static)
    url = f"http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{httpd.server_port}"
    print(f"  抖音续火花系统已启动  ->  {url}")
    if static is None:
        print("  [提示] 未找到前端产物 web/static，接口可用但页面 404。")
    for warning in settings.warnings():
        print(f"  [警告] {warning}")
    if settings.demo_mode:
        print("  [提示] 演示模式已开启：不会真的启动浏览器。")
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在停止……")
    finally:
        httpd.shutdown()
        httpd.server_close()


__all__ = [
    "SparkServer",
    "SparkHandler",
    "StaticFiles",
    "RateLimiter",
    "LoginGuard",
    "Metrics",
    "serve",
    "all_routes",
]
