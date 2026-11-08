"""集成烟雾测试（Lead 维护）：随队友交付增量跑，尽早发现跨模块问题。

只依赖标准库。用法：
    cd spark-shared/backend
    python ../tools/integration_probe.py

设计要点
--------
* 用「进程内真实 HTTP 服务 + urllib 真请求」而不是直接调用处理函数，
  这样鉴权中间件、路由匹配、JSON 序列化、状态码都在测试范围内。
* 先读 ``GET /api/v1/_routes`` 拿运行时路由表，**就绪的接口才断言**，
  未就绪的记为 SKIP——队友并行开发期间，这个脚本不会因为「别人还没写完」而红。
* 就绪判断要补 ``/api/v1`` 前缀：路由表里是完整路径，而请求用相对路径。
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
    """定位后端目录，兼容两种布局（与 acceptance_e2e / foundation_selftest 一致）：

    * 开发树：``spark-shared/tools/`` → 后端在 ``spark-shared/backend/``
    * 交付树：``<交付根>/tools/``   → 后端就在 ``<交付根>/``

    写死一种布局会让脚本在另一种布局里直接 ModuleNotFoundError（实测在交付
    目录里 exit 1），而交付物里带着这个脚本，所以必须两种都能跑。
    """
    for candidate in (root / "backend", root):
        if (candidate / "main.py").is_file() and (candidate / "app").is_dir():
            return candidate
    return root / "backend"


BACKEND = _detect_backend(ROOT)
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("SK_DATA_DIR", str(BACKEND / "data"))
os.environ.setdefault("SK_ENV", "dev")

from app import bootstrap as boot              # noqa: E402
from app import db                             # noqa: E402
from app.config import get_settings            # noqa: E402
from app.http_server import SparkServer, StaticFiles  # noqa: E402
from app.router import API_PREFIX, all_routes  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def skip(name: str, why: str = "") -> None:
    SKIP.append(name)
    print(f"  SKIP  {name}{'  ' + why if why else ''}")


def call(base: str, path: str, method: str = "GET", body: dict | None = None,
         token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    settings = get_settings()
    settings.host = "127.0.0.1"
    settings.port = 0
    settings.rate_limit_per_minute = 0
    settings.max_login_fail = 0        # 探测期间不要触发登录锁定
    settings.ensure_dirs()
    boot.init_app(settings, quiet=True)

    report = boot.load_api_modules.last_report
    # 带上静态资源（前端 SPA），这样探测脚本同时覆盖「页面能不能打开」
    static = StaticFiles(settings.static_dir) if settings.static_dir.exists() else None
    httpd = SparkServer(settings, static=static)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    # ---- 运行时路由表：决定哪些断言可以跑 ----
    _, routes_body = call(base, API_PREFIX + "/_routes")
    api_paths = {r["path"] for r in routes_body.get("data", {}).get("routes", [])}
    module_paths = {r.path for r in all_routes()}

    def ready(rel_path: str) -> bool:
        return (API_PREFIX + rel_path) in api_paths

    print(f"  已加载模块 {len(report['loaded'])} 个，未安装 {len(report['missing'])} 个，"
          f"加载失败 {len(report['broken'])} 个")
    if report["broken"]:
        print("  [警告] 加载失败的模块（其接口不会注册）：" + ", ".join(report["broken"]))
    print(f"  运行时路由 {len(api_paths)} 条（模块内 {len(module_paths)} 条）")
    if module_paths - api_paths:
        print(f"  [警告] 路由表缺少 {len(module_paths - api_paths)} 条："
              f"{sorted(module_paths - api_paths)[:8]}")
    print(f"  服务 {base}")

    try:
        # ---- 基础 ----
        status, body = call(base, API_PREFIX + "/health")
        data = body.get("data") or {}
        healthy = data.get("ok") is True or data.get("status") == "ok"
        check("health 走通", status == 200 and healthy,
              f"status={status} keys={sorted(data)[:6]}")

        status, body = call(base, API_PREFIX + "/ping")
        check("ping 走通", status == 200 and body["data"]["pong"] is True)

        if ready("/install/check"):
            status, body = call(base, API_PREFIX + "/install/check")
            check("安装体检", status == 200 and "environment" in body.get("data", {}),
                  f"status={status}")
        else:
            skip("安装体检", "模块未就绪")

        if ready("/auth/public-configs"):
            status, body = call(base, API_PREFIX + "/auth/public-configs")
            check("公开站点配置", status == 200 and bool(body.get("data", {}).get("site_name")),
                  f"status={status}")
        else:
            skip("公开站点配置", "模块未就绪")

        # ---- 注册 → 登录 ----
        import secrets as _s

        uname = "probe_" + _s.token_hex(4)
        password = "Probe12345"
        token = ""
        if ready("/auth/user/register"):
            status, body = call(base, API_PREFIX + "/auth/user/register", "POST",
                                {"username": uname, "password": password,
                                 "confirm_password": password})
            token = (body.get("data") or {}).get("token", "")
            check("注册即登录", status == 200 and bool(token),
                  f"status={status} {body.get('message', '')}")

            status, body = call(base, API_PREFIX + "/auth/user/register", "POST",
                                {"username": uname, "password": password,
                                 "confirm_password": password})
            check("重复用户名 409", status == 409, f"status={status}")

            status, body = call(base, API_PREFIX + "/auth/user/register", "POST",
                                {"username": uname + "x", "password": "abcdefgh",
                                 "confirm_password": "abcdefgh"})
            check("弱口令 400", status == 400, f"status={status} {body.get('message', '')}")
        else:
            skip("注册", "模块未就绪")

        if ready("/auth/user/login"):
            status, body = call(base, API_PREFIX + "/auth/user/login", "POST",
                                {"identifier": uname, "password": password})
            login_token = (body.get("data") or {}).get("token", "")
            token = login_token or token
            check("登录返回令牌", status == 200 and bool(login_token),
                  f"status={status} {body.get('message', '')}")

            status, body = call(base, API_PREFIX + "/auth/user/login", "POST",
                                {"identifier": uname, "password": "WrongPass123"})
            check("错误密码 401", status == 401, f"status={status}")
        else:
            skip("登录", "模块未就绪")

        # ---- 鉴权中间件 ----
        if ready("/user/info"):
            status, body = call(base, API_PREFIX + "/user/info", token=token)
            check("带令牌访问 200", status == 200, f"status={status}")
            status, body = call(base, API_PREFIX + "/user/info")
            check("无令牌 401", status == 401, f"status={status}")
            status, body = call(base, API_PREFIX + "/user/info", token="bad.token.value")
            check("伪造令牌 401", status == 401, f"status={status}")
        else:
            skip("鉴权中间件", "模块未就绪")

        # ---- 未激活套餐必须 402（而不是 500 / 放行）----
        if ready("/douyin/accounts"):
            status, body = call(base, API_PREFIX + "/douyin/accounts", "POST",
                                {"nickname": "探针账号"}, token=token)
            check("未激活套餐建账号 402", status == 402,
                  f"status={status} code={body.get('code')}")
        else:
            skip("额度门禁", "模块未就绪")

        # ---- 卡密：无效卡不能 500 ----
        if ready("/user/card-keys/redeem"):
            status, body = call(base, API_PREFIX + "/user/card-keys/redeem", "POST",
                                {"code": "AAAABBBBCCCCDDDDEEEE"}, token=token)
            check("无效卡密 4xx（非 500）", 400 <= status < 500,
                  f"status={status} code={body.get('code')}")
        else:
            skip("卡密兑换", "模块未就绪")

        # ---- 管理端越权 ----
        if ready("/admin/stats"):
            status, body = call(base, API_PREFIX + "/admin/stats", token=token)
            check("普通用户访问管理端 403", status == 403, f"status={status}")

            admin = db.query_one("SELECT username FROM users WHERE role='admin' LIMIT 1")
            if admin:
                status, body = call(base, API_PREFIX + "/auth/admin/login", "POST",
                                    {"identifier": admin["username"],
                                     "password": os.environ.get("SK_ADMIN_PASSWORD",
                                                                "ProbeAdmin123")})
                admin_token = (body.get("data") or {}).get("token", "")
                check("管理员登录", status == 200 and bool(admin_token),
                      f"status={status} {body.get('message', '')}")
                if admin_token:
                    status, body = call(base, API_PREFIX + "/admin/stats", token=admin_token)
                    check("管理员访问统计 200", status == 200, f"status={status}")
            else:
                skip("管理员登录", "库里没有 admin")
        else:
            skip("管理端越权", "模块未就绪")

        # ---- 前端静态资源（页面能不能打开）----
        index = settings.static_dir / "index.html"
        check("前端 index.html 存在", index.is_file(),
              str(settings.static_dir) if index.is_file() else "缺少 web/static/index.html")

        for rel, expect in (("/", "html"),
                            ("/assets/app.js", "javascript"),
                            ("/assets/style.css", "css"),
                            ("/vendor/vue.global.prod.js", "javascript")):
            rel_path = settings.static_dir / rel.lstrip("/")
            if rel != "/" and not rel_path.is_file():
                skip(f"静态 {rel}", "文件不存在")
                continue
            ctype = ""
            status = 0
            try:
                req = urllib.request.Request(base + rel)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
                    ctype = resp.headers.get("Content-Type", "")
                    resp.read(2048)
            except urllib.error.HTTPError as exc:
                status = exc.code
            check(f"静态 {rel} 200 + 类型含 {expect}",
                  status == 200 and expect in ctype, f"status={status} ctype={ctype}")

        status, _ = call(base, API_PREFIX + "/health")
        check("静态资源不干扰 API", status == 200, f"status={status}")

        # ---- 商业闭环：管理员发卡 → 用户兑换 → 建账号 → 建任务 → 执行 ----
        closing_loop(settings, base, ready)

        # ---- 清理探针账号，避免污染开发库 ----
        db.delete("users", "username=?", (uname,))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"\n  通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")
    if FAIL:
        print("  失败项：" + ", ".join(FAIL))
    if SKIP:
        print("  跳过项（对应模块尚未就绪）：" + ", ".join(SKIP))
    return 1 if FAIL else 0


def closing_loop(settings, base: str, ready) -> None:
    """把「发卡 → 兑换 → 建号 → 建任务 → 立即执行 → 查记录」跑成一条闭环。

    这是这个产品真正要卖的东西：用户兑卡拿到套餐与账号位，托管抖音账号，
    然后系统按点自动发消息。任何一环断了，产品都是不可用的。
    """
    import secrets as _s

    print("  --- 商业闭环 ---")
    if not (ready("/admin/card-keys/generate") and ready("/user/card-keys/redeem")
            and ready("/douyin/accounts") and ready("/tasks")):
        skip("商业闭环", "相关接口未就绪")
        return

    admin = db.query_one("SELECT username FROM users WHERE role='admin' LIMIT 1")
    if not admin:
        skip("商业闭环", "库里没有 admin（先跑 python main.py admin）")
        return
    status, body = call(base, API_PREFIX + "/auth/admin/login", "POST",
                        {"identifier": admin["username"],
                         "password": os.environ.get("SK_ADMIN_PASSWORD", "ProbeAdmin123")})
    admin_token = (body.get("data") or {}).get("token", "")
    if not admin_token:
        check("闭环：管理员登录", False, f"status={status} {body.get('message', '')}")
        return

    # 1) 发卡
    status, body = call(base, API_PREFIX + "/admin/card-keys/generate", "POST",
                        {"package_code": "monthly", "card_type": "time", "days": 30,
                         "count": 1, "note": "闭环探针"}, token=admin_token)
    codes = (body.get("data") or {}).get("codes") or []
    check("闭环：管理员生成卡密", status == 200 and len(codes) == 1,
          f"status={status} {body.get('message', '')}")
    if not codes:
        return
    code = codes[0]

    # 2) 用户兑换
    uname = "loop_" + _s.token_hex(4)
    status, body = call(base, API_PREFIX + "/auth/user/register", "POST",
                        {"username": uname, "password": "Probe12345",
                         "confirm_password": "Probe12345"})
    token = (body.get("data") or {}).get("token", "")
    if not token:
        check("闭环：注册兑换用户", False, f"status={status}")
        return
    status, body = call(base, API_PREFIX + "/user/card-keys/redeem", "POST",
                        {"code": code}, token=token)
    check("闭环：卡密兑换成功", status == 200, f"status={status} {body.get('message', '')}")

    # 3) 重复兑换必须 409
    status, body = call(base, API_PREFIX + "/user/card-keys/redeem", "POST",
                        {"code": code}, token=token)
    check("闭环：重复兑换 409 card_used",
          status == 409 and body.get("code") == "card_used",
          f"status={status} code={body.get('code')}")

    # 4) 有套餐后建账号
    status, body = call(base, API_PREFIX + "/douyin/accounts", "POST",
                        {"nickname": "闭环账号", "remark": "探针"}, token=token)
    account_id = int((body.get("data") or {}).get("account_id") or 0)
    check("闭环：建托管账号", status == 200 and account_id > 0,
          f"status={status} {body.get('message', '')}")
    if not account_id:
        db.delete("users", "username=?", (uname,))
        return

    # 5) 建定时任务
    status, body = call(base, API_PREFIX + "/tasks", "POST",
                        {"account_id": account_id, "name": "闭环任务",
                         "time_of_day": "08:30", "repeat_mode": "daily",
                         "messages": ["火花不能断 🔥"], "friend_mode": "all",
                         "enabled": True},
                        token=token)
    task_id = int((body.get("data") or {}).get("id") or 0)
    check("闭环：建定时任务", status == 200 and task_id > 0,
          f"status={status} {body.get('message', '')}")
    if task_id:
        status, body = call(base, API_PREFIX + f"/tasks/{task_id}/preview", "POST", {}, token=token)
        check("闭环：预览下次执行时间", status == 200, f"status={status} {body.get('message', '')}")

        status, body = call(base, API_PREFIX + f"/tasks/{task_id}/run", "POST",
                            {"dry_run": True}, token=token)
        # 账号未登录（离线）时拒绝执行是正确的；这里只要求「不是 5xx」
        check("闭环：立即执行不崩（4xx 也算通过）", status < 500,
              f"status={status} code={body.get('code')}")

        status, body = call(base, API_PREFIX + "/douyin/task-records", token=token)
        check("闭环：执行记录可读", status == 200 and "items" in (body.get("data") or {}),
              f"status={status}")

    # 6) 仪表盘与限制状态
    if ready("/user/dashboard"):
        status, body = call(base, API_PREFIX + "/user/dashboard", token=token)
        data = body.get("data") or {}
        check("闭环：仪表盘可读", status == 200 and "quota" in data, f"status={status}")

    # 7) 清理（删用户会级联删账号/任务）
    db.delete("users", "username=?", (uname,))


if __name__ == "__main__":
    sys.exit(main())
