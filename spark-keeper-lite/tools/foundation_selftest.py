"""地基自测（Lead 维护）：进程内起真实 SparkServer，验证 HTTP/路由/鉴权/数据库。

为什么在这里而不是 backend/tests：``backend/tests`` 归自动化工程师 D，
``spark-shared/tests`` 归测试工程师 E；这份是 Lead 验收地基用的独立自检。

运行：
    cd spark-shared/backend
    python ../tools/foundation_selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _detect_backend(root: Path) -> Path:
    """开发树后端在 ``root/backend``；交付树后端就在 ``root``。"""
    for candidate in (root / "backend", root):
        if (candidate / "main.py").is_file() and (candidate / "app").is_dir():
            return candidate
    return root / "backend"


BACKEND = _detect_backend(ROOT)
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("SK_DATA_DIR", str(BACKEND / "data"))
os.environ.setdefault("SK_ENV", "dev")

from app import bootstrap as boot          # noqa: E402
from app import db                          # noqa: E402
from app.config import get_settings         # noqa: E402
from app.http_server import SparkServer     # noqa: E402
from app.router import Response, ok, route  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


@route("GET", "/api/v1/_selftest/echo", summary="自测回显", tags=("selftest",))
def _echo(req) -> Response:
    return ok({"method": req.method, "query": req.query, "ip": req.client_ip})


@route("POST", "/api/v1/_selftest/post", summary="自测提交", tags=("selftest",))
def _post(req) -> Response:
    return ok({"received": req.json(required=False), "int": req.int_field("n", 0, minimum=0)})


@route("GET", "/api/v1/_selftest/secret", auth="user", summary="自测需登录", tags=("selftest",))
def _secret(req) -> Response:
    return ok({"user": req.user_id})


@route("GET", "/api/v1/_selftest/<item_id>", summary="自测路径参数", tags=("selftest",))
def _item(req, item_id: str) -> Response:
    return ok({"item_id": item_id})


def call(base: str, path: str, method: str = "GET", body: dict | None = None,
         token: str = "") -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"_raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload, dict(exc.headers)


def main() -> int:
    settings = get_settings()
    settings.host = "127.0.0.1"
    settings.port = 0
    settings.rate_limit_per_minute = 0
    settings.ensure_dirs()

    report = boot.init_app(settings, quiet=True)
    print(f"  路由 {report['routes']} 条 · schema {report['schema_version']} · "
          f"API 模块 {len(report['api_modules'])} 个")
    print(f"  静态目录 {settings.static_dir}  存在={settings.static_dir.exists()}")

    httpd = SparkServer(settings, static=None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    print(f"  服务已启动 {base}")

    try:
        status, body, _ = call(base, "/api/v1/health")
        check("health 200", status == 200, f"status={status}")
        check("health ok", bool(body.get("data", {}).get("ok")), str(body.get("data"))[:90])

        status, body, _ = call(base, "/api/v1/_routes")
        routes = body.get("data", {}).get("routes", [])
        check("路由清单可读", status == 200 and len(routes) > 8, f"{len(routes)} 条")
        check("含 core health 路由", any(r["path"] == "/api/v1/health" for r in routes))

        status, body, _ = call(base, "/api/v1/_selftest/echo?a=1&b=%E4%B8%AD%E6%96%87")
        check("GET query 解码", status == 200 and body["data"]["query"]["b"] == "中文")

        status, body, _ = call(base, "/api/v1/_selftest/post", "POST", {"hello": "世界", "n": 3})
        check("POST JSON 解析", status == 200 and body["data"]["received"]["hello"] == "世界")

        status, body, _ = call(base, "/api/v1/_selftest/post", "POST", {"n": -1})
        check("字段范围校验 400", status == 400, str(body.get("message")))

        status, body, _ = call(base, "/api/v1/_selftest/abc-123")
        check("路径参数解析", status == 200 and body["data"]["item_id"] == "abc-123")

        status, _, _ = call(base, "/api/v1/_selftest/nope/nope")
        check("未知接口 404", status == 404, f"status={status}")

        status, _, _ = call(base, "/api/v1/_selftest/echo", "POST")
        check("方法不允许 405", status == 405, f"status={status}")

        status, body, _ = call(base, "/api/v1/_selftest/secret")
        check("未登录 401", status == 401, str(body.get("message")))

        status, body, _ = call(base, "/api/v1/_selftest/secret", token="bad.token.value")
        check("非法 token 401", status == 401, f"status={status}")

        status, _, headers = call(base, "/api/v1/_selftest/echo", "OPTIONS")
        check("OPTIONS 204", status == 204)
        check("安全响应头", headers.get("X-Content-Type-Options") == "nosniff")

        health = db.health()
        check("数据库健康", bool(health.get("ok")) and health["tables"] > 20, str(health)[:140])
        check("站点配置种子", boot.get_config("site.name") != "")

        metrics = httpd.metrics.snapshot()
        check("请求指标记录", metrics["requests"] >= 10, str(metrics["requests"]))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"\n  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
