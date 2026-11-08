"""路由注册表与请求上下文（极简 Web 框架，标准库之上 300 行）。

为什么需要它
------------
目标站有近 80 个接口。用 ``if path == ...`` 堆在 do_GET 里不可维护，所以拆出
一张路由表，让每个功能模块自己注册：

    from ..router import route, Request, Response

    @route("POST", "/auth/user/login")
    def login(req: Request) -> Response:
        ...

路由表的另一个用途是**自动生成接口清单**（``GET /api/v1/_routes``），
前端与文档都以它为准，避免「文档写了但没实现」。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote

Handler = Callable[["Request", Any], "Response"]


class HttpError(Exception):
    """业务异常：抛出后由服务器统一转成 JSON 错误响应。"""

    def __init__(self, status: int, message: str, code: str = "", data: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or _default_code(status)
        self.data = data


def _default_code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        402: "payment_required",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        422: "unprocessable",
        429: "too_many_requests",
        500: "server_error",
    }.get(status, "error")


@dataclass
class Response:
    """处理器返回值。默认 JSON。"""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None            # 非 None 时直接写字节（文件下载/静态资源）
    content_type: str = "application/json; charset=utf-8"

    def header(self, key: str, value: str) -> "Response":
        self.headers[key] = value
        return self

    @staticmethod
    def json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> "Response":
        return Response(status=status, body=data, headers=headers or {})

    @staticmethod
    def text(content: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return Response(
            status=status,
            raw=content.encode("utf-8"),
            content_type=content_type,
        )

    @staticmethod
    def bytes(content: bytes, content_type: str = "application/octet-stream", status: int = 200,
              filename: str | None = None) -> "Response":
        headers = {}
        if filename:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return Response(status=status, raw=content, content_type=content_type, headers=headers)

    @staticmethod
    def redirect(url: str, status: int = 302) -> "Response":
        return Response(status=status, headers={"Location": url}, raw=b"")


def ok(data: Any = None, message: str = "ok") -> Response:
    return Response.json({"code": 0, "message": message, "data": data})


def fail(message: str, status: int = 400, code: str = "", data: Any = None) -> Response:
    return Response.json(
        {"code": code or _default_code(status), "message": message, "data": data},
        status=status,
    )


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, str]
    raw_query: str
    headers: dict[str, str]
    body: bytes
    client_ip: str
    scheme: str
    server: Any = None                  # HttpServer 实例，用于访问 settings/db
    _json: Any = None
    _json_parsed: bool = False
    user: dict | None = None
    auth: dict | None = None            # JWT payload

    # ---- 输入解析 ----
    def json(self, required: bool = True, default: Any = None) -> Any:
        if not self._json_parsed:
            self._json_parsed = True
            if not self.body:
                self._json = None
            else:
                ctype = (self.headers.get("content-type") or "").lower()
                try:
                    if "json" in ctype or self.body[:1] in (b"{", b"["):
                        self._json = json.loads(self.body.decode("utf-8"))
                    else:
                        # 兼容 application/x-www-form-urlencoded
                        form = parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
                        self._json = {k: v[0] for k, v in form.items()}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HttpError(400, f"请求体不是合法 JSON：{exc}") from exc
        if self._json is None:
            if required:
                raise HttpError(400, "缺少请求参数")
            return default if default is not None else {}
        return self._json

    def field(self, name: str, default: Any = None) -> Any:
        data = self.json(required=False) or {}
        if isinstance(data, dict) and name in data and data[name] is not None:
            return data[name]
        if name in self.query:
            return self.query[name]
        return default

    def str_field(self, name: str, default: str = "", max_len: int = 4096) -> str:
        value = self.field(name, default)
        if value is None:
            return default
        text = str(value).strip()
        if len(text) > max_len:
            raise HttpError(400, f"字段 {name} 过长（上限 {max_len} 字符）")
        return text

    def int_field(self, name: str, default: int = 0, minimum: int | None = None,
                  maximum: int | None = None) -> int:
        value = self.field(name, default)
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise HttpError(400, f"字段 {name} 必须是整数") from None
        if minimum is not None and num < minimum:
            raise HttpError(400, f"字段 {name} 不能小于 {minimum}")
        if maximum is not None and num > maximum:
            raise HttpError(400, f"字段 {name} 不能大于 {maximum}")
        return num

    def bool_field(self, name: str, default: bool = False) -> bool:
        value = self.field(name, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def page_args(self) -> tuple[int, int]:
        return (
            self.int_field("page", 1, minimum=1),
            self.int_field("page_size", 20, minimum=1, maximum=200),
        )

    def bearer_token(self) -> str:
        auth = self.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (self.headers.get("x-token") or "").strip()

    @property
    def user_id(self) -> int:
        return int((self.user or {}).get("id") or 0)

    def require_user(self) -> dict:
        if not self.user:
            raise HttpError(401, "请先登录")
        if self.user.get("status") != "active":
            raise HttpError(403, "账号已被禁用，请联系管理员")
        return self.user

    def require_admin(self) -> dict:
        user = self.require_user()
        if user.get("role") != "admin":
            raise HttpError(403, "需要管理员权限")
        return user


# --------------------------------------------------------------------------
# 路由表
# --------------------------------------------------------------------------
@dataclass
class RouteEntry:
    method: str
    path: str
    handler: Handler
    auth: str                 # public | user | admin
    summary: str
    regex: re.Pattern
    param_names: list[str]
    tags: tuple[str, ...] = ()
    origin: str = ""          # 定义位置 "文件:行号"，用于诊断重复注册

    def describe(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "auth": self.auth,
            "summary": self.summary,
            "tags": list(self.tags),
            "origin": self.origin,
        }


_ROUTES: list[RouteEntry] = []
# 同一 method+path 被不同文件重复注册的记录（(被覆盖的, 覆盖者的)）
_OVERRIDES: list[tuple[RouteEntry, RouteEntry]] = []
_PARAM_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")

# 接口前缀。所有业务接口都在 /api/v1 下，但**注册时不必手写**：
# 写 ``@route("POST", "/auth/user/login")`` 与写 ``/api/v1/auth/user/login`` 等价。
# 这样做的原因：多人并行开发时前缀写法极易不一致（实测出现过同一个仓库里
# 一部分接口带前缀、一部分不带，前端按契约调用会 404）。统一在这里补齐，
# 路由清单与契约（docs/API_CONTRACT.md）永远以带前缀的完整路径呈现。
API_PREFIX = "/api/v1"


def normalize_path(path: str) -> str:
    path = "/" + (path or "").lstrip("/")
    if path.startswith(API_PREFIX + "/") or path == API_PREFIX:
        return path
    if path == "/":
        return path
    return API_PREFIX + path


def route(method: str, path: str, *, auth: str = "public", summary: str = "",
          tags: Iterable[str] = ()) -> Callable[[Handler], Handler]:
    """装饰器：注册一个路由。

    auth: ``public`` 免登录 / ``user`` 需登录 / ``admin`` 需管理员。
    path 支持 ``<name>`` 形式的路径参数，如 ``/admin/users/<user_id>``。
    前缀 ``/api/v1`` 会自动补齐（写成带前缀的完整路径也可以）。
    """
    if auth not in ("public", "user", "admin"):
        raise ValueError(f"未知的 auth 级别：{auth}")
    method = method.upper()
    path = normalize_path(path)
    param_names = _PARAM_RE.findall(path)
    regex = re.compile("^" + _PARAM_RE.sub(r"(?P<\1>[^/]+)", path) + "$")

    def decorator(func: Handler) -> Handler:
        def _origin(fn: Callable | None = None) -> str:
            target = fn or func
            code = getattr(target, "__code__", None)
            if code is None:
                return getattr(target, "__module__", "?")
            return f"{code.co_filename}:{code.co_firstlineno}"

        entry = RouteEntry(
            method=method,
            path=path,
            handler=func,
            auth=auth,
            summary=summary or (func.__doc__ or "").strip().splitlines()[0] if (summary or func.__doc__) else "",
            regex=regex,
            param_names=param_names,
            tags=tuple(tags),
            origin=_origin(),
        )
        # 同一 method+path 被注册两次时**覆盖**（便于热重载与测试），但这几乎总是
        # 意外事故：实测出现过两个模块同时注册 /api/v1/health，响应体形状随 import
        # 顺序漂移，排查花了很久。所以覆盖时留一条 warning + 记进 _OVERRIDES，
        # 由 tools/check_contract.py 汇总报出。
        for i, existing in enumerate(_ROUTES):
            if existing.method == method and existing.path == path:
                if existing.origin != entry.origin:
                    _OVERRIDES.append((existing, entry))
                    try:
                        from .common import logging as logmod

                        logmod.log(
                            f"路由被覆盖：{method} {path} ← {entry.origin}（原 {existing.origin}）",
                            level="warning",
                            source="router",
                        )
                    except Exception:      # noqa: BLE001 - 日志失败不影响注册
                        pass
                _ROUTES[i] = entry
                break
        else:
            _ROUTES.append(entry)
        return func

    return decorator


def get(method: str, path: str, **kwargs) -> Callable[[Handler], Handler]:
    return route(method, path, **kwargs)


def all_routes() -> list[RouteEntry]:
    return list(_ROUTES)


def route_overrides() -> list[dict]:
    """返回被覆盖过的路由（应恒为空；非空说明有两个文件抢注同一接口）。"""
    return [
        {
            "method": new.method,
            "path": new.path,
            "overwritten": old.origin,
            "overwritten_by": new.origin,
        }
        for old, new in _OVERRIDES
    ]


def clear_routes() -> None:
    _ROUTES.clear()
    _OVERRIDES.clear()


def match(method: str, path: str) -> tuple[RouteEntry | None, dict[str, str], bool]:
    """匹配路由。返回 (entry, path_params, path_exists_other_method)。

    先按原路径精确匹配；不中时再试「补全 /api/v1 前缀」的形式，这样
    ``/api/v1/auth/login`` 与 ``/auth/login`` 都能命中同一条路由。
    两条路径全部落空时才需要判断是 404 还是 405，所以这里的第二次尝试
    只在第一次完全没命中时发生，不会拖慢正常请求。
    """
    method = method.upper()
    for candidate in (path, normalize_path(path)):
        path_exists = False
        for entry in _ROUTES:
            m = entry.regex.match(candidate)
            if not m:
                continue
            path_exists = True
            if entry.method == method or (entry.method == "GET" and method == "HEAD"):
                return entry, {k: unquote(v) for k, v in m.groupdict().items()}, True
        if path_exists:
            return None, {}, True
    return None, {}, False


class Router:
    """按前缀挂载到 HttpServer 上的路由集合（供测试单独构造）。"""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.routes: list[RouteEntry] = []

    def add(self, entry: RouteEntry) -> None:
        self.routes.append(entry)

    def dispatch(self, req: Request) -> Response:
        for entry in self.routes:
            m = entry.regex.match(req.path)
            if m:
                return entry.handler(req, **m.groupdict())
        raise HttpError(404, "接口不存在")


def install_index(server) -> None:
    """把路由清单暴露出来，前端/文档/自检都以它为准。

    同时暴露「被覆盖的路由」：正常情况下应为空数组，非空即代表两个模块
    抢注了同一个 method+path（响应体会随 import 顺序变化，属于隐患）。
    """

    @route("GET", "/api/v1/_routes", summary="列出全部已注册接口", tags=("meta",))
    def _routes(req: Request) -> Response:
        overrides = route_overrides()
        return ok(
            {
                "count": len(_ROUTES),
                "routes": [e.describe() for e in _ROUTES],
                "overrides": overrides,
                "override_count": len(overrides),
            }
        )


def uptime_hint() -> float:
    return time.time()
