#!/usr/bin/env python
"""交付目录冷启动验证：真的跑 ``python main.py init`` / ``serve``，再用 urllib 打接口。

为什么必须「真起进程」
----------------------
单元测试用的是进程内 ``SparkServer``（快、稳），但用户拿到交付包后执行的命令是
``python main.py init`` / ``python main.py serve``。这两条路径会额外牵出：
CLI 参数解析、``.env`` 读取、``SK_PROJECT_DIR`` 对前端产物的定位、调度器后台线程、
迁移执行顺序。这些只有在**真进程**里才会暴露，所以交付验证必须真起进程。

验证内容
--------
1. 交付目录结构完整、**不含** data/ 与 __pycache__（运行产物不能进交付包）
2. 冷启动：``main.py init`` 建库建管理员，且库里真的有 admin
3. ``main.py serve --port <空闲端口>`` 起得来（轮询 /api/v1/health 直到 200）
4. ``/api/v1/health`` 200 且自证数据库可用
5. ``/api/v1/install/check`` 200，且它自报的 ``database.path`` **就是**我们的库（防串库）
6. ``GET /`` 200 且是 HTML（验证 SK_PROJECT_DIR → web/static 定位正确）
7. 版本差异：pro 版 ``/api/v1/pay/return`` 必须不是 404；lite 版 ``/api/v1/pay/notify/easypay`` 必须 404
8. 关掉进程、删掉临时数据目录（交付目录恢复「干净」状态）

用法
----
    python tools/verify_delivery.py --dir ../spark-keeper-pro
    python tools/verify_delivery.py --all
    python tools/verify_delivery.py --dir ../spark-keeper-lite --keep-data --verbose

退出码：0 全部通过 · 1 有失败项 · 2 交付目录不存在/结构不完整
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
WORKSPACE = TOOLS_DIR.parent.parent          # 交付目录与 spark-shared 的公共父目录

REQUIRED = (
    "main.py", "app", "db", "web", "deploy", "VERSION", "README.md",
    "start.sh", "start.bat", ".env.example", "docker-compose.yml", ".gitignore",
    "deploy/Dockerfile", "deploy/entrypoint.sh", "deploy/nginx.conf", "deploy/DEPLOY.md",
    "tools/selftest.py", "tools/backup.py", "tools/check_contract.py",
    "docs/API_CONTRACT.md", "docs/RUNBOOK.md",
    # 前端本地兜底依赖：断网环境没有它页面打不开，必须随包
    "web/static/vendor/vue.global.prod.js",
)
# 交付包里**不允许**存在的东西（data/ 只允许空骨架 + .gitkeep）
FORBIDDEN = ("__pycache__", ".env", "secret.key", "INITIAL_ADMIN.txt")
PAYMENT_FILES = ("__init__.py", "gateway.py", "easypay.py", "routes.py")


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.notes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        (self.passed if ok else self.failed).append(name)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  note  {text}")


def free_port() -> int:
    """让系统分配一个空闲端口。绑定后立刻释放——同端口被别人抢走是极小概率事件。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, timeout: float = 15.0,
         headers: dict[str, str] | None = None) -> tuple[int, bytes, str, dict[str, str]]:
    """返回 (状态码, 响应体, content-type, 响应头小写字典)。4xx/5xx 不抛异常。"""
    req = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - 本机回环
            return (int(resp.status), resp.read(), (resp.headers.get("content-type") or ""),
                    {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        try:
            return (int(exc.code), exc.read(), (exc.headers.get("content-type") or ""),
                    {k.lower(): v for k, v in (exc.headers or {}).items()})
        finally:
            exc.close()


def check_static_cache(base: str, static_dir: Path, report: "Report") -> None:
    """静态资源缓存策略断言（真实事故：前端修好了，用户浏览器还在跑旧 app.js）。

    前端产物是「改了要立刻生效」的东西，所以必须满足：
      1. 无内容指纹的资源（app.js/style.css/index.html/vendor/）**不发 max-age=86400**，
         否则浏览器 24 小时不来问服务器，任何修复都「修了等于没修」；
      2. 必须有 **ETag**，并且带 If-None-Match 复请求要返回 **304**（没变就别重传）；
      3. ETag 必须**随内容变化**（证明不是硬编码）。
    第 3 条用一个临时探针文件验证（不动交付件，验证完删掉）。
    """
    status, _body, _ctype, headers = http("GET", f"{base}/assets/app.js")
    etag = headers.get("etag", "")
    cache = headers.get("cache-control", "")
    report.check("静态资源带 ETag", status == 200 and bool(etag),
                 f"status={status} ETag={etag or '(缺失)'}")
    report.check("静态资源不禁用协商缓存（Cache-Control 不含 max-age=86400）",
                 "max-age=86400" not in cache and "no-cache" in cache,
                 f"Cache-Control={cache or '(缺失)'}")

    status, _body, _ctype, _headers = http("GET", f"{base}/assets/app.js",
                                           headers={"If-None-Match": etag} if etag else None)
    report.check("带 If-None-Match 复请求返回 304（没变就不重传）", status == 304,
                 f"status={status}（etag={etag or '无'}）")

    # 入口页同样要协商缓存，否则用户连「新版页面」都拿不到
    status, _body, _ctype, idx = http("GET", f"{base}/index.html")
    report.check("index.html 也走协商缓存（ETag + no-cache）",
                 status == 200 and bool(idx.get("etag")) and "max-age=86400" not in idx.get("cache-control", ""),
                 f"status={status} ETag={idx.get('etag', '(缺失)')} "
                 f"Cache-Control={idx.get('cache-control', '(缺失)')}")

    # ETag 随内容变化：用临时探针文件（写入 → 读取 → 改内容 → 再读 → 比对 → 删除）
    probe = static_dir / "assets" / "_cache_probe.js"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("// cache probe A\n", encoding="utf-8")
        _s, _b, _c, first = http("GET", f"{base}/assets/_cache_probe.js")
        probe.write_text("// cache probe B（内容更长，保证 ETag 一定变）\n" * 3, encoding="utf-8")
        _s2, _b2, _c2, second = http("GET", f"{base}/assets/_cache_probe.js")
        report.check("ETag 随内容变化（不是硬编码）",
                     bool(first.get("etag")) and first.get("etag") != second.get("etag"),
                     f"{first.get('etag')} → {second.get('etag')}")
    finally:
        probe.unlink(missing_ok=True)


def wait_health(base: str, timeout: float = 45.0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, body, _, _hdrs = http("GET", f"{base}/api/v1/health", timeout=3)
            if status == 200:
                return True, body.decode("utf-8", "replace")
            last = f"HTTP {status}"
        except (urllib.error.URLError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.4)
    return False, last


def read_output(proc: subprocess.Popen, sink: list[str]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        sink.append(line.rstrip())


def verify(delivery: Path, keep_data: bool = False, verbose: bool = False,
           timeout: float = 60.0, data_dir_override: str = "") -> Report:
    report = Report()
    print("=" * 78)
    print(f"  交付目录冷启动验证：{delivery}")
    print("=" * 78)

    if not delivery.is_dir():
        report.check("交付目录存在", False, str(delivery))
        return report

    # ---- 1. 结构与打包卫生 ----
    missing = [name for name in REQUIRED if not (delivery / name).exists()]
    report.check("交付文件齐全", not missing,
                 f"{len(REQUIRED)} 项检查" if not missing else f"缺少 {missing}")
    leftovers = [name for name in FORBIDDEN if (delivery / name).exists()]
    # data/ 允许存在，但只能是空骨架（.gitkeep）；有数据库/密钥就是打包事故
    data_dir = delivery / "data"
    if data_dir.exists():
        strays = [str(p.relative_to(delivery)) for p in data_dir.rglob("*")
                  if p.is_file() and p.name != ".gitkeep"]
        leftovers.extend(strays)
    report.check("无运行产物/私密文件（data 只有 .gitkeep、无 __pycache__/.env/secret.key）",
                 not leftovers, "" if not leftovers else f"发现 {leftovers[:6]}（应从交付包剔除）")

    # ---- 1b. pro/lite 的内容差异必须与版本一致 ----
    payment_dir = delivery / "app" / "payment"
    version_text = (delivery / "VERSION").read_text(encoding="utf-8-sig")
    edition = next((row.split("=", 1)[1].strip() for row in version_text.splitlines()
                    if row.startswith("EDITION=")), "?")
    if edition == "pro":
        found = [name for name in PAYMENT_FILES if (payment_dir / name).is_file()] if payment_dir.is_dir() else []
        report.check("pro 版含 app/payment/（支付模块已从 pro/ 拍平合并进来）",
                     len(found) == len(PAYMENT_FILES),
                     f"payment 目录 {len(found)}/{len(PAYMENT_FILES)} 个关键文件")
    else:
        report.check("lite 版不含 app/payment/（按设计无支付模块）", not payment_dir.exists(),
                     "已正确排除" if not payment_dir.exists() else "竟然存在 payment 目录")
    # 打包不干净是严重问题，但**不提前返回**：继续跑功能验证，一次给出两类信息
    # （「产物脏 + 功能正常」和「产物脏 + 功能也坏」的处置完全不同）。
    report.note("VERSION: " + " · ".join(row for row in version_text.strip().splitlines()
                                         if not row.startswith("#")))

    # 默认用交付目录内的 data/（最贴近真实使用）；包内 data 被别的进程占用时，
    # 可以用 --data-dir 指定到外面，先把功能验证跑完。
    data_dir = Path(data_dir_override).resolve() if data_dir_override else (delivery / "data")
    in_package_data = data_dir == (delivery / "data")
    if not in_package_data:
        report.note(f"数据目录改到包外：{data_dir}（包内 data/ 可能被占用）")
    env = dict(os.environ)
    # 必须清掉外部可能残留的 SK_PROJECT_DIR（否则会把「别人设过」的值带进来，
    # 掩盖自动推导的缺陷——Lead 就踩过一次这种污染）。下面的 GET / 断言会自动判定是否有效。
    env.pop("SK_PROJECT_DIR", None)
    env.update({
        "SK_DATA_DIR": str(data_dir),
        "SK_SECRET": "delivery-verify-secret-0123456789abcdef",
        "SK_ENV": "dev",
        "SK_DEMO_MODE": "1",              # 不真的开浏览器
        "SK_SCHEDULER_ENABLED": "1",      # 连调度器启动路径一起验证
        "SK_RATE_LIMIT_PER_MINUTE": "0",
        "PYTHONUTF8": "1",
        # 不让验证过程往交付目录里写 __pycache__（交付包必须保持干净，
        # 这也和 Dockerfile 里的 PYTHONDONTWRITEBYTECODE=1 一致）
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    python = sys.executable
    proc: subprocess.Popen | None = None
    output: list[str] = []
    try:
        # ---- 2. init ----
        init = subprocess.run([python, "main.py", "init"], cwd=str(delivery), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if verbose:
            print("  ---- main.py init ----\n" + "\n".join(
                f"  | {row}" for row in (init.stdout or "").splitlines()[-25:]))
        admin_ok = False
        if (data_dir / "spark.db").exists():
            probe = subprocess.run(
                [python, "-c",
                 "import sys;sys.path.insert(0,'.');from app import db;"
                 "print((db.query_one(\"SELECT username FROM users WHERE role='admin' LIMIT 1\") or {}).get('username',''))"],
                cwd=str(delivery), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=timeout)
            admin_ok = "admin" in (probe.stdout or "")
        report.check("冷启动 main.py init（建库 + 建管理员）", init.returncode == 0 and admin_ok,
                     f"exit={init.returncode} admin={'有' if admin_ok else '无'}")

        # ---- 3. serve ----
        port = free_port()
        base = f"http://127.0.0.1:{port}"

        # ---- 3a. 一键启动脚本的「双击等价位」检查（Windows）----
        # 用户报过「start.bat 黑窗口一闪就没」：那是启动前就报错、又没 pause 导致的静默失败。
        # 这里用 `cmd /c start.bat --check`（体检 + 迁移后退出）走一遍真实入口，
        # 断言 exit 0；SK_NO_PAUSE=1 保证无人值守时不卡在按键等待。
        bat = delivery / "start.bat"
        if os.name != "nt":
            report.note("非 Windows：跳过 start.bat 入口检查")
        elif not bat.is_file():
            report.check("start.bat 存在且能被 cmd 执行", False, "缺少 start.bat")
        else:
            bat_env = dict(env)
            bat_env["SK_NO_PAUSE"] = "1"
            try:
                entry = subprocess.run(["cmd", "/c", "start.bat", "--check"], cwd=str(delivery),
                                       env=bat_env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                       errors="replace", timeout=180)
                tail = "\n".join((entry.stdout or "").splitlines()[-12:])
                report.check("cmd /c start.bat --check（双击等价位）退出码 0",
                             entry.returncode == 0,
                             f"exit={entry.returncode}" + ("" if entry.returncode == 0
                                                           else f"；输出尾部：{tail[-200:]}"))
                if verbose and tail:
                    print("  ---- start.bat --check ----\n" + "\n".join(f"  | {r}" for r in tail.splitlines()))
            except subprocess.TimeoutExpired:
                report.check("cmd /c start.bat --check（双击等价位）退出码 0", False,
                             "超过 180s 未结束（可能在等待按键；检查 SK_NO_PAUSE 分支）")
            except OSError as exc:
                report.note(f"无法执行 cmd：{exc}（跳过 start.bat 入口检查）")

        proc = subprocess.Popen([python, "main.py", "serve", "--port", str(port)],
                                cwd=str(delivery), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
        threading.Thread(target=read_output, args=(proc, output), daemon=True).start()
        ok, detail = wait_health(base, timeout)
        report.check(f"main.py serve 起来并能响应 /api/v1/health（端口 {port}）", ok,
                     detail[:120] if ok else f"未就绪：{detail}")
        if not ok:
            print("  ---- 服务端输出 ----\n" + "\n".join(f"  | {row}" for row in output[-30:]))
            return report

        # ---- 4. 健康检查 ----
        status, body, _, _hdrs = http("GET", f"{base}/api/v1/health")
        payload = json.loads(body.decode("utf-8"))
        data = payload.get("data") or {}
        db_flag = data.get("db_ok", data.get("database", data.get("ok")))
        report.check("health 返回契约结构（code=0 + 版本 + 数据库可用）",
                     status == 200 and payload.get("code") == 0 and bool(data.get("version"))
                     and bool(db_flag), json.dumps(data, ensure_ascii=False)[:160])

        # ---- 5. install/check 自证是同一个库 ----
        status, body, _, _hdrs = http("GET", f"{base}/api/v1/install/check")
        check_data = (json.loads(body.decode("utf-8")).get("data") or {}) if status == 200 else {}
        reported = str((check_data.get("database") or {}).get("path") or "")
        same_db = bool(reported) and Path(reported) == (data_dir / "spark.db")
        report.check("install/check 200 且数据库路径与本次数据目录一致",
                     status == 200 and same_db,
                     f"status={status} path={reported or '(空)'}")
        report.check("install/check 报告已安装（管理员存在）", bool(check_data.get("installed")),
                     f"tables={((check_data.get('database') or {}).get('tables'))} "
                     f"schema={check_data.get('schema_version')}")

        # ---- 6. 前端产物（SK_PROJECT_DIR 定位）----
        status, body, ctype, _hdrs = http("GET", f"{base}/")
        report.check("GET / 返回前端 index.html", status == 200 and b"<" in body
                     and "text/html" in ctype, f"status={status} type={ctype}")

        # ---- 6b. 静态资源缓存策略（前端修复必须能到用户眼前）----
        check_static_cache(base, delivery / "web" / "static", report)

        # ---- 7. 版本差异 ----
        if edition == "pro":
            status, _, _, _hdrs = http("GET", f"{base}/api/v1/pay/return")
            report.check("pro 版：/api/v1/pay/return 存在（非 404）", status != 404,
                         f"status={status}")
        else:
            status, body, _, _hdrs = http("GET", f"{base}/api/v1/pay/notify/easypay")
            code = ""
            try:
                code = str((json.loads(body.decode("utf-8")) or {}).get("code") or "")
            except ValueError:
                pass
            report.check("lite 版：/api/v1/pay/notify/easypay 必须 404（无支付模块）",
                         status == 404 and code == "not_found", f"status={status} code={code!r}")
            status, _, _, _hdrs = http("GET", f"{base}/api/v1/health")
            report.check("lite 版：核心接口不受影响", status == 200, f"status={status}")

        report.note(f"进程输出 {len(output)} 行；含 traceback={'是' if any('Traceback' in r for r in output) else '否'}")
        return report
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if not keep_data:
            for _ in range(10):
                shutil.rmtree(data_dir, ignore_errors=True)
                if not data_dir.exists():
                    break
                time.sleep(0.2)
            if data_dir.exists():
                print(f"  note  数据目录未能删除（可手动清理或关掉占用它的进程）：{data_dir}")
            elif in_package_data:
                # 交付包里 data/ 应当是「空骨架」：把 .gitkeep 放回去，
                # 否则验证一次就把交付产物的目录结构改掉了。
                for name in ("sessions", "screenshots", "uploads"):
                    folder = data_dir / name
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / ".gitkeep").write_text("", encoding="utf-8", newline="\n")
        # 兜底：万一本机环境仍生成了字节码缓存，验证完要清掉，保证交付包干净
        purged = 0
        for cache in delivery.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
            purged += 1
        for stray in list(delivery.rglob("*.pyc")) + list(delivery.rglob("*.pyo")):
            stray.unlink(missing_ok=True)
            purged += 1
        if purged:
            print(f"  note  已清理验证过程产生的字节码缓存（{purged} 处）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交付目录冷启动验证（真起 main.py serve）")
    parser.add_argument("--dir", default="", help="交付目录路径（默认与 spark-shared 同级推断）")
    parser.add_argument("--all", action="store_true", help="验证 spark-keeper-pro 与 spark-keeper-lite")
    parser.add_argument("--keep-data", action="store_true", help="保留验证用的 data/（排障用）")
    parser.add_argument("--verbose", action="store_true", help="打印服务端输出")
    parser.add_argument("--timeout", type=float, default=60.0, help="启动等待秒数（默认 60）")
    parser.add_argument("--data-dir", default="",
                        help="把验证用的数据目录放到包外（包内 data/ 被别的进程占用时用）")
    args = parser.parse_args(argv)

    targets: list[Path] = []
    if args.all or not args.dir:
        targets = [WORKSPACE / f"spark-keeper-{edition}" for edition in ("pro", "lite")]
    else:
        targets = [Path(args.dir).expanduser().resolve()]

    reports: list[tuple[Path, Report]] = []
    for target in targets:
        report = verify(target, keep_data=args.keep_data, verbose=args.verbose, timeout=args.timeout,
                        data_dir_override=args.data_dir)
        reports.append((target, report))

    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    failed_total = 0
    for target, report in reports:
        failed_total += len(report.failed)
        mark = "✅" if not report.failed else "❌"
        print(f"  {mark} {target.name:<20} 通过 {len(report.passed)} / "
              f"{len(report.passed) + len(report.failed)}")
        for name in report.failed:
            print(f"      - 失败：{name}")
    print("-" * 78)
    if failed_total:
        print(f"  结论：{failed_total} 项失败 ❌")
        return 1
    print("  结论：全部通过 ✅（冷启动链路：init → serve → 探针 → 契约 → 前端 → 版本差异）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
