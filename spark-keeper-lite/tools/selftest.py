#!/usr/bin/env python
"""一键自检：把项目里所有可自动化的检查串起来，最后给一张汇总表。

为什么要有它
------------
交付标准是「别人能复现」。手动一条条敲命令容易漏，也容易「跑了一半就看结果」。
这里把每一环都变成可重复的子进程调用，失败时打印尾部输出，方便直接贴给同事。

检查项
------
1. ``no-third-party``  零第三方依赖（硬约束：pip 装不了包，import 第三方必炸）
2. ``contract``        接口契约一致性（docs/API_CONTRACT.md ⇄ 运行时路由表）
3. ``unit``            本仓库端到端测试套件（tests/，unittest）
4. ``foundation``      Lead 维护的地基自检（HTTP/路由/鉴权/数据库）
5. ``automation``      自动化引擎单测（backend/tests/，队友 D，缺失则跳过）
6. ``backup``          数据库热备份可用（造一个临时库真实备份一次）
7. ``env``             环境体检（信息项，不判失败）

用法
----
    python tools/selftest.py                # 全部跑一遍
    python tools/selftest.py --list
    python tools/selftest.py --only contract --only unit
    python tools/selftest.py --json
    python tools/selftest.py --in-process   # 不起子进程（沙箱禁止 spawn 时用）

退出码：0 全部通过 · 1 有失败项
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent


def _detect_layout(start: Path) -> tuple[Path, Path]:
    """返回 ``(项目根, 后端目录)``，兼容开发树（root/backend/app）与交付树（root/app）。"""
    for root in (start, start.parent, start.parent.parent):
        if (root / "app" / "config.py").is_file():
            return root, root
        if (root / "backend" / "app" / "config.py").is_file():
            return root, root / "backend"
    return start.parent, start.parent / "backend"


PROJECT_ROOT, BACKEND_DIR = _detect_layout(TOOLS_DIR)
TESTS_DIR = PROJECT_ROOT / "tests"
TMP_ROOT = BACKEND_DIR / "data" / "tmp"         # 系统 TEMP 不可写，统一放这里


def detect_edition() -> str:
    """这份代码是 pro 还是 lite：有支付模块就是 pro。

    lite 版按设计没有 ``app/payment``，契约里那两条 ``/pay/*`` 就该「不存在」，
    所以契约检查必须换到 lite 档位，否则会把设计差异报成缺陷。
    """
    has_payment = ((BACKEND_DIR / "app" / "payment").is_dir()
                   or (PROJECT_ROOT / "pro" / "app" / "payment").is_dir())
    return "pro" if has_payment else "lite"


EDITION = detect_edition()

# 允许 import 的「本地包」：标准库之外的合法名字只有这些
LOCAL_PACKAGES = {"app", "tests", "tools", "pro", "payment", "backend", "config"}
SKIP_DIRS = {"__pycache__", "data", ".venv", "node_modules", "vendor", ".git"}


# --------------------------------------------------------------------------
# 检查 1：零第三方依赖
# --------------------------------------------------------------------------
def check_no_third_party() -> tuple[bool, str, str]:
    """静态扫描 import，任何非标准库、非本地模块的名字都是硬违规。"""
    stdlib = set(sys.stdlib_module_names)
    roots = [r for r in (BACKEND_DIR, PROJECT_ROOT / "pro", TESTS_DIR, TOOLS_DIR) if r.exists()]
    # 本地模块（tests/e2e_harness.py、tools/backup.py 这类）按文件名识别，不算第三方
    local = set(LOCAL_PACKAGES)
    for root in roots:
        for path in root.rglob("*.py"):
            if not any(part in SKIP_DIRS for part in path.parts):
                local.add(path.stem)

    violations: list[str] = []
    scanned = 0
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            scanned += 1
            try:
                # utf-8-sig：Windows 编辑器会往文件头塞 BOM（U+FEFF）。Python 的
                # import 机制能容忍它，但 ast.parse 会直接报「non-printable character」，
                # 所以这里按 import 的真实行为解码，避免误报成「第三方依赖」。
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (OSError, SyntaxError) as exc:
                violations.append(f"{path.relative_to(PROJECT_ROOT)} 解析失败: {exc}")
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name not in stdlib and name not in local:
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} import {name}")
    if violations:
        return False, f"发现 {len(violations)} 处第三方依赖（pip 装不了，部署必炸）", "\n".join(violations)
    return True, f"扫描 {scanned} 个文件，全部只用标准库", ""


# --------------------------------------------------------------------------
# 检查 6：热备份可用
# --------------------------------------------------------------------------
def check_backup() -> tuple[bool, str, str]:
    """真造一个临时库、真备份一次（备份脚本坏了必须在交付前发现）。"""
    sys.path.insert(0, str(TOOLS_DIR))
    import backup as backup_mod                        # noqa: PLC0415

    work = TMP_ROOT / f"selftest_backup_{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db_path, out_dir = work / "spark.db", work / "backups"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);"
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY);"
            "INSERT INTO users(username) VALUES('admin');"
            "INSERT INTO schema_migrations VALUES('0001');"
        )
        conn.commit()
        conn.close()

        first = backup_mod.hot_backup(db_path, out_dir, keep=2)
        for index in range(4):                       # 多备几次，验证保留策略真的在清理
            backup_mod.hot_backup(db_path, out_dir, keep=2)
        remaining = backup_mod.list_backups(out_dir)
        if remaining and len(remaining) > 2:
            return False, f"保留策略失效：应只留 2 份，实际 {len(remaining)} 份", ""
        return (True, f"备份成功（{first['mechanism']}）· 校验 {first['integrity']} · "
                      f"表 {first['tables']} 张 · 保留 {len(remaining)} 份", "")
    except Exception as exc:      # noqa: BLE001 - 任何异常都算这项检查失败
        return False, f"{type(exc).__name__}: {exc}", ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 检查 7：环境体检（信息项）
# --------------------------------------------------------------------------
def check_env() -> tuple[bool, str, str]:
    sys.path.insert(0, str(BACKEND_DIR))
    os.environ.setdefault("SK_DATA_DIR", str(TMP_ROOT / f"selftest_env_{os.getpid()}"))
    from app import bootstrap                          # noqa: PLC0415

    report = bootstrap.check_cloud_environment()
    detail = (f"python {report['python_version']} · {report['platform']} · "
              f"磁盘剩余 {report['disk_free_gb']}GB · "
              f"浏览器 {'有' if report['browser_found'] else '无（自动续火花需要它）'}")
    return True, detail, json.dumps(report, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 检查 8：部署产物静态校验
#     本机没有 docker / nginx / sh（沙箱禁子进程与命名管道），所以用静态规则
#     把「一定会炸」的问题挡在交付前：CRLF 的 .sh、缺 HEALTHCHECK、缺
#     X-Forwarded-* 头、.env 里写了代码根本不认的键（这个坑真实踩过）。
# --------------------------------------------------------------------------
COMPOSE_ONLY_KEYS = {"SK_HTTP_PORT", "SK_BIND", "SK_VERSION", "SK_DB_WAIT_SECONDS", "TZ"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def check_deploy() -> tuple[bool, str, str]:
    deploy_dir = PROJECT_ROOT / "deploy"
    problems: list[str] = []
    notes: list[str] = []

    if not deploy_dir.is_dir():
        return False, f"缺少部署目录 {deploy_dir}", ""

    # ---- 1. shell 脚本：CRLF 会让 Linux 报 ': not found'，必须在交付前挡住 ----
    for name in ("entrypoint.sh", "start.sh"):
        path = deploy_dir / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", "replace")
        if b"\r\n" in raw:
            problems.append(f"{name}: 含 CRLF 换行（Linux 下会执行失败，必须转成 LF）")
        if not text.startswith("#!"):
            problems.append(f"{name}: 缺少 shebang（Docker ENTRYPOINT 会直接执行失败）")
        elif "/bin/sh" in text.splitlines()[0] and "env sh" not in text.splitlines()[0]:
            # `$(( ))` 是 POSIX 算术展开，合法；裸 `(( ))` / `[[ ]]` / `$'...'` 才是 bashism
            for pattern, label in ((r"(?<!\$)\(\([^)]", "(( )) 算术命令"),
                                   (r"\[\[", "[[ ]] 条件"),
                                   (r"\$'", "$'...' 转义")):
                if re.search(pattern, text):
                    problems.append(f"{name}: 声明 #!/bin/sh 却用了 bash 专有语法：{label}")
        if "set -eu" not in text and "set -e" not in text:
            problems.append(f"{name}: 缺少 set -e/-eu（出错会继续跑下去）")

    # ---- 2. Dockerfile ----
    dockerfile = deploy_dir / "Dockerfile"
    if not dockerfile.is_file():
        problems.append("缺少 deploy/Dockerfile")
    else:
        text = _read(dockerfile)
        for needle, why in (
            ("FROM python:3.11-slim", "基础镜像必须是 python:3.11-slim"),
            ("HEALTHCHECK", "必须带健康检查"),
            ("/api/v1/health", "健康检查必须打 /api/v1/health"),
            ("VOLUME", "必须声明数据卷"),
            ("CMD", "必须给默认启动命令"),
            ("chromium", "必须安装 chromium（自动化引擎要用）"),
            ("fonts-noto-cjk", "必须装中文字体（否则截图全方块）"),
            ("tzdata", "必须装 tzdata（否则时区退回固定偏移）"),
        ):
            if needle not in text:
                problems.append(f"Dockerfile: 缺少 {needle}（{why}）")
        user_lines = [row.strip() for row in text.splitlines()
                      if row.strip().upper().startswith("USER ")]
        if not user_lines:
            problems.append("Dockerfile: 没有 USER 指令，容器会以 root 运行（跑浏览器自动化太危险）")
        elif any("root" in row.lower() for row in user_lines):
            problems.append(f"Dockerfile: USER 不能是 root：{user_lines}")

    # ---- 3. compose（lite 交付包里只有 docker-compose.lite.yml）----
    compose_files = [p for p in (deploy_dir / "docker-compose.yml", deploy_dir / "docker-compose.lite.yml")
                     if p.is_file()]
    if not compose_files:
        problems.append("缺少 deploy/docker-compose.yml（或 docker-compose.lite.yml）")
    # 「根目录也有 compose」是**交付树**的要求（用户会直接在交付根 `docker compose up`）；
    # 开发树（spark-shared/）的 compose 只在 deploy/ 下，不能算缺陷。
    is_delivery_tree = BACKEND_DIR == PROJECT_ROOT
    if is_delivery_tree and not (PROJECT_ROOT / "docker-compose.yml").is_file():
        problems.append("交付根目录缺少 docker-compose.yml（用户会直接在这里 up）")
    for path in compose_files:
        name = path.name
        text = _read(path)
        if "\t" in text:
            problems.append(f"{name}: 含制表符（YAML 缩进不允许 tab）")
        for needle, why in (
            ("services:", "必须有 services"),
            ("healthcheck:", "必须有健康检查"),
            ("volumes:", "必须挂数据卷"),
            ("restart:", "必须有重启策略"),
            ("env_file:", "必须读 .env"),
        ):
            if needle not in text:
                problems.append(f"{name}: 缺少 {needle}（{why}）")

    # ---- 4. nginx ----
    nginx = deploy_dir / "nginx.conf"
    if not nginx.is_file():
        problems.append("缺少 deploy/nginx.conf")
    else:
        text = _read(nginx)
        for needle, why in (
            ("proxy_set_header X-Forwarded-For", "反代必须传真实 IP，否则限流/审计失真"),
            ("proxy_set_header X-Forwarded-Proto", "反代必须传协议，否则回调地址拼成 http"),
            ("client_max_body_size", "必须放开上传体积"),
            ("limit_req_zone", "必须有限流 zone"),
            ("map $http_upgrade $connection_upgrade", "必须支持 WebSocket 升级"),
            ("ssl_certificate", "必须给 HTTPS 示例"),
            ("proxy_pass http://spark_backend", "必须反代到后端 upstream"),
        ):
            if needle not in text:
                problems.append(f"nginx.conf: 缺少 {needle}（{why}）")

    # ---- 5. .env.example 的每个键都要真的被代码/compose 读取 ----
    env_example = deploy_dir / ".env.example"
    config_py = BACKEND_DIR / "app" / "config.py"
    if not env_example.is_file():
        problems.append("缺少 deploy/.env.example")
    elif not config_py.is_file():
        notes.append("找不到 app/config.py，跳过 .env 键名交叉校验")
    else:
        # 代码实际会读的 SK_* 有两个来源：
        #   1) app/config.py 里的 _env(...)（正规路径）
        #   2) 其它模块直接 os.environ.get("SK_...")（例如 bootstrap.ensure_admin 读 SK_ADMIN_PASSWORD）
        # 只扫 (1) 会把 (2) 误报成「代码根本不读的键」——这个假阳性踩过一次。
        # 注意把 _env_float / _env_str 也算进来：新加的 SK_CDP_PAGE_TIMEOUT 用的是
        # _env_float()，旧正则只认 _env/_env_int/_env_bool，会误报「代码根本不读」。
        known = set(re.findall(
            r'_env(?:_int|_bool|_float|_str)?\(\s*"(SK_[A-Z0-9_]+)"', _read(config_py)))
        env_read_re = re.compile(r"environ(?:\.get\(|\[)\s*[\"'](SK_[A-Z0-9_]+)[\"']")
        # 只扫**应用代码**（app/ 与 main.py）：tools/ 里也有 os.environ 读取
        # （例如 selftest 自己的 SK_NODE），那是工具开关，不该要求写进应用的 .env.example。
        app_sources = [py for py in (BACKEND_DIR / "app").rglob("*.py")
                       if not any(part in SKIP_DIRS for part in py.parts)]
        if (BACKEND_DIR / "main.py").is_file():
            app_sources.append(BACKEND_DIR / "main.py")
        for py in app_sources:
            known |= set(env_read_re.findall(_read(py)))
        known |= COMPOSE_ONLY_KEYS
        # 注释掉的键也算「已说明」：.env.example 里大量可选键就是这样写的
        declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", _read(env_example), re.MULTILINE))
        unknown = sorted(declared - known)
        if unknown:
            problems.append(".env.example 里有代码根本不读的键（写了也没用，会误导运维）："
                            + ", ".join(unknown))
        # 反向也必须查：代码读的键一定要在 .env.example 里出现，否则运维不知道有这个开关
        # （真实案例：SK_LOG_KEEP_DAYS 被 getattr 兜底，靠人肉眼发现才补上）
        missing = sorted(known - declared)
        if missing:
            problems.append(".env.example 缺少代码实际会读的键（运维将无法配置）："
                            + ", ".join(missing))
        notes.append(f".env.example 声明 {len(declared)} 个变量，代码/compose 读取 {len(known)} 个，双向一致")

    if problems:
        return False, f"部署产物有 {len(problems)} 处问题", "\n".join(problems + [""] + notes)
    return True, f"部署产物静态校验通过（{len(notes)} 条提示）", "\n".join(notes)


def _free_port() -> int:
    """让系统分配一个空闲端口（绑定后立刻释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_node() -> str | None:
    """找 node 可执行文件。交付机上没有 node 就跳过前端运行时冒烟，不作为硬依赖。"""
    env = os.environ.get("SK_NODE", "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _last_meaningful(rows: list[str]) -> str:
    """取最后一行「有内容」的输出（跳过 === / --- 这类纯分隔行，报告里更好读）。"""
    for row in reversed(rows):
        text = row.strip()
        if text and set(text) - set("=-— "):
            return text
    return ""


def _run_node_script(name: str, extra: list[str] | None = None) -> tuple:
    """跑 ``web/tools/<name>``（Lead 维护的前端静态/行为检查）。没有 node 就跳过。"""
    node = _find_node()
    script = PROJECT_ROOT / "web" / "tools" / name
    if node is None:
        return ("skip", f"未找到 node（可用 SK_NODE=<路径> 指定）；{name} 跳过", "")
    if not script.is_file():
        return ("skip", f"未随包提供：{script.relative_to(PROJECT_ROOT).as_posix()}", "")
    proc = subprocess.run([node, str(script), *(extra or [])], cwd=str(PROJECT_ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    rows = [row for row in (proc.stdout or "").splitlines() if row.strip()]
    tail = "\n".join(rows[-25:])
    last = _last_meaningful(rows)
    if proc.returncode == 0:
        return (True, f"{name} 通过（{last[:70]}）", tail)
    return (False, f"{name} 退出码 {proc.returncode}", tail)


def check_frontend_boot() -> tuple:
    """前端启动行为：外壳是否真的注入、29 个视图模板是否非空、片段请求数对不对。

    C 修过一个「整站白屏」的真 bug（根组件没有 template，layout.html 从未被加载），
    当时所有静态检查都绿——因为没人断言「mount 之后外壳真的存在」。这条就是补它。
    """
    return _run_node_script("check_boot.js")


def check_frontend_fragments() -> tuple:
    """孤儿视图片段检测：views/ 下每个 .html 都必须被代码引用。"""
    return _run_node_script("check_templates.js")


def check_admin_session() -> tuple:
    """管理员刷新页面后是否仍是管理员（check_admin_session.js）。

    真实事故：管理员登录后台，刷新一下变普通用户界面。
    根因是 `pick(null, data, ['user','profile'])` 在缺键时把**键数组**当成默认值返回，
    把 state.user 写成 `["user","profile"]` → role 丢失 → isAdmin()=false。
    这条检查用 fetch 桩真跑 app.js 的 boot()，断言 role/isAdmin 在刷新后仍然正确。
    """
    return _run_node_script("check_admin_session.js")


def check_dom_smoke() -> tuple:
    """前端运行时冒烟（Lead 的 web/tools/dom_smoke.mjs）：Node vm + 最小 DOM 跑真实产物。

    需要「Node + 一个运行中的后端」，所以这里**用子进程起真实 `main.py serve`**
    （随机空闲端口、隔离数据目录、--no-scheduler），跑完杀进程删目录；
    **没有 node 就跳过**——交付机不一定装 Node，不能让它变成硬依赖。

    为什么不用进程内 SparkServer：实测把 node 与进程内服务放一起会互相等待
    （自检整体卡死、0 CPU），换成子进程 + 硬超时后稳定。教训：自检工具最怕
    「没有超时的等待」，任何外部命令都必须能被打断。

    **登录链路是硬断言**：先用 `main.py admin --password` 在隔离库里造一个已知口令的
    管理员，再把 `SK_SMOKE_USER` / `SK_SMOKE_PASSWORD` 传给 node——这样
    「登录 200 → 令牌落盘 → 后续请求带 Authorization → 退出清令牌」每次自检都真跑。
    这条防线来自一个真实事故：`pick()` 参数错位导致令牌永远是 undefined，
    而当时所有检查全绿，因为**没有任何断言真的登录过一次**。
    """
    node = _find_node()
    script = PROJECT_ROOT / "web" / "tools" / "dom_smoke.mjs"
    static_rel = "web/static"
    if node is None:
        return ("skip", "未找到 node（可用 SK_NODE=<路径> 指定）；前端运行时冒烟跳过", "")
    if not script.is_file():
        return ("skip", f"未随包提供：{script.relative_to(PROJECT_ROOT).as_posix()}", "")
    if not (PROJECT_ROOT / static_rel / "index.html").is_file():
        return ("skip", "前端产物 web/static/index.html 不存在", "")

    smoke_user = os.environ.get("SK_SMOKE_USER", "admin")
    smoke_password = os.environ.get("SK_SMOKE_PASSWORD") or "SmokeAdmin!2026a"
    work = TMP_ROOT / f"selftest_dom_{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "SK_PROJECT_DIR": str(PROJECT_ROOT), "SK_DATA_DIR": str(work),
        "SK_SECRET": "selftest-dom-secret-0123456789abcdef", "SK_ENV": "dev",
        "SK_DEMO_MODE": "1", "SK_SCHEDULER_ENABLED": "0", "SK_RATE_LIMIT_PER_MINUTE": "0",
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    server: subprocess.Popen | None = None
    try:
        # ---- 1. 隔离库里造一个已知口令的管理员（登录断言的前提）----
        admin = subprocess.run(
            [sys.executable, "main.py", "admin", "--username", smoke_user,
             "--password", smoke_password],
            cwd=str(BACKEND_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=120)
        if admin.returncode != 0:
            tail = "\n".join((admin.stdout or "").splitlines()[-8:])
            return (False, f"创建测试管理员失败（exit={admin.returncode}）", tail)

        # ---- 2. 起真实服务 ----
        port = _free_port()
        server = subprocess.Popen(
            [sys.executable, "main.py", "serve", "--port", str(port), "--no-scheduler"],
            cwd=str(BACKEND_DIR), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline and server.poll() is None:
            try:
                with urllib.request.urlopen(f"{base}/api/v1/health", timeout=3) as resp:   # noqa: S310
                    if resp.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.4)
        if not ready:
            return (False, f"起后端失败（端口 {port}，进程退出码 {server.poll()}）", "")

        # ---- 3. 跑前端冒烟（带登录凭据）----
        node_env = dict(env)
        node_env["SK_SMOKE_USER"] = smoke_user
        node_env["SK_SMOKE_PASSWORD"] = smoke_password
        proc = subprocess.run([node, str(script), "--base", base, "--root", static_rel],
                              cwd=str(PROJECT_ROOT), env=node_env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
        rows = [row for row in (proc.stdout or "").splitlines() if row.strip()]
        tail = "\n".join(rows[-25:])
        last = _last_meaningful(rows)
        # 登录断言被跳过 = 这条防线失效（历史事故：令牌永远是 undefined，而检查全绿）。
        # 我们已经提供了凭据，所以「跳过」只能说明前端没暴露 api.auth 或登录真的坏了。
        if "跳过登录链路断言" in (proc.stdout or ""):
            return (False, "登录链路断言被跳过——「登录 → 令牌落盘」这条防线失效了", tail)
        if proc.returncode == 0:
            summary = next((row.strip() for row in rows if "冒烟：通过" in row), "")
            login = "含登录链路断言" if any("登录后的请求带上了 Authorization" in r for r in rows) else "（未含登录断言）"
            return (True, f"dom_smoke {login}：{summary or last[:60]}", tail)
        return (False, f"dom_smoke 退出码 {proc.returncode}", tail)
    except subprocess.TimeoutExpired:
        return (False, "dom_smoke 超过 180s 未结束（已中止）", "")
    except Exception as exc:      # noqa: BLE001 - 检查项自己炸了也算失败
        return (False, f"{type(exc).__name__}: {exc}", "")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 子进程检查
# --------------------------------------------------------------------------
@dataclass
class Check:
    key: str
    title: str
    cmd: list[str] = field(default_factory=list)
    cwd: Path = PROJECT_ROOT
    func: object = None                  # 有 func 时优先在进程内跑
    optional_if_missing: Path | None = None
    precondition: object = None          # callable() -> str|None，返回原因则跳过
    timeout: int = 1200


def _automation_needs_dev_layout() -> str | None:
    """队友 D 的单测在 <后端>/tests/。交付树把 backend 拍平到根，那里只有本套件，
    再 discover 一次等于把 tests/ 跑两遍——如实跳过。"""
    automation_tests = BACKEND_DIR / "tests"
    if not automation_tests.exists():
        return None          # 由 optional_if_missing 报「尚未提供」
    if automation_tests == TESTS_DIR:
        return "交付树里 backend 已拍平，没有独立的自动化单测目录（用 tests/ 套件覆盖）"
    return None


def _integration_needs_dev_layout() -> str | None:      # noqa: D401 - 保留名字便于对照历史
    """``integration_probe.py`` 现在是布局自适应的，**两种布局都真跑**。

    交付树里它会跳过 7 项（管理员密码、部分探针依赖开发库），但**失败必须为 0**。
    所以这里不再跳过：有失败会直接体现在 subprocess 退出码上（exit 0 = 通过）。
    """
    return None


CHECKS: list[Check] = [
    Check("no-third-party", "零第三方依赖（硬约束）", func=check_no_third_party),
    Check("encoding", "编码体检（乱码 / 意外 BOM，Lead 维护）",
          cmd=[sys.executable, "tools/check_encoding.py"],
          optional_if_missing=TOOLS_DIR / "check_encoding.py", timeout=180),
    Check("bat", "启动脚本体检（.bat 纯 ASCII/CRLF/无 BOM/pause 分支，Lead 维护）",
          cmd=[sys.executable, "tools/check_bat.py"],
          optional_if_missing=TOOLS_DIR / "check_bat.py", timeout=180),
    Check("contract", f"接口契约一致性（文档 ⇄ 运行时路由，档位 {EDITION}）",
          cmd=[sys.executable, "tools/check_contract.py", "--profile", EDITION], timeout=180),
    Check("unit", "端到端测试套件 tests/",
          cmd=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
          timeout=1800),
    Check("integration", "集成探测（Lead 维护，真实 HTTP 全链路）",
          cmd=[sys.executable, str(TOOLS_DIR / "integration_probe.py")],
          cwd=BACKEND_DIR, optional_if_missing=TOOLS_DIR / "integration_probe.py",
          precondition=_integration_needs_dev_layout, timeout=600),
    Check("foundation", "地基自检（Lead 维护）",
          cmd=[sys.executable, str(TOOLS_DIR / "foundation_selftest.py")],
          cwd=BACKEND_DIR, optional_if_missing=TOOLS_DIR / "foundation_selftest.py",
          timeout=300),
    Check("acceptance", "最终验收 E2E（Lead 维护，真 subprocess + 调度器 + demo_mode）",
          cmd=[sys.executable, str(TOOLS_DIR / "acceptance_e2e.py")],
          cwd=BACKEND_DIR, optional_if_missing=TOOLS_DIR / "acceptance_e2e.py",
          timeout=900),
    Check("quota", "配额/次数卡自检（Lead 维护）",
          cmd=[sys.executable, str(TOOLS_DIR / "quota_selftest.py")],
          cwd=BACKEND_DIR, optional_if_missing=TOOLS_DIR / "quota_selftest.py",
          timeout=300),
    Check("dom_smoke", "前端运行时冒烟（Lead 的 dom_smoke.mjs，需要 Node + 起服务）",
          func=check_dom_smoke),
    Check("frontend_boot", "前端外壳/视图注册行为（check_boot.js，需要 Node）",
          func=check_frontend_boot),
    Check("admin_session", "管理员刷新后仍是管理员（check_admin_session.js，需要 Node）",
          func=check_admin_session),
    Check("frontend_fragments", "视图片段无孤儿（check_templates.js，需要 Node）",
          func=check_frontend_fragments),
    Check("automation", "自动化引擎单测 backend/tests/（队友 D）",
          cmd=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
          cwd=BACKEND_DIR, optional_if_missing=BACKEND_DIR / "tests",
          precondition=_automation_needs_dev_layout, timeout=1800),
    Check("backup", "数据库热备份（VACUUM INTO + 保留策略）", func=check_backup),
    Check("deploy", "部署产物静态校验（Dockerfile/compose/nginx/.env）", func=check_deploy),
    Check("env", "环境体检", func=check_env),
]


@dataclass
class Result:
    key: str
    title: str
    status: str          # PASS / FAIL / SKIP
    seconds: float
    detail: str
    output: str = ""


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONPATH", str(BACKEND_DIR))
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout,   # noqa: S603
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout or ""


def run_check(check: Check, in_process: bool = False) -> Result:
    started = time.time()
    if check.optional_if_missing is not None and not check.optional_if_missing.exists():
        return Result(check.key, check.title, "SKIP", 0.0,
                      f"未随包提供：{check.optional_if_missing.relative_to(PROJECT_ROOT)}"
                      "（开发树专用或尚未提供）")
    if check.precondition is not None:
        reason = check.precondition()                  # type: ignore[operator]
        if reason:
            return Result(check.key, check.title, "SKIP", 0.0, reason)
    if in_process or check.func is not None:
        try:
            ok, detail, output = check.func()          # type: ignore[operator]
        except Exception as exc:      # noqa: BLE001 - 检查项自己炸了也算失败
            import traceback

            return Result(check.key, check.title, "FAIL", time.time() - started,
                          f"{type(exc).__name__}: {exc}", traceback.format_exc())
        if isinstance(ok, str) and ok == "skip":       # 检查项自己声明「本机不适用」
            return Result(check.key, check.title, "SKIP", time.time() - started, detail, "")
        return Result(check.key, check.title, "PASS" if ok else "FAIL",
                      time.time() - started, detail, output)
    try:
        code, output = _run(check.cmd, check.cwd, check.timeout)
    except subprocess.TimeoutExpired:
        return Result(check.key, check.title, "FAIL", time.time() - started,
                      f"超时（>{check.timeout}s）")
    except OSError as exc:            # 沙箱禁止 spawn 时给出可操作的提示
        return Result(check.key, check.title, "FAIL", time.time() - started,
                      f"无法启动子进程（{exc}）；可加 --in-process 重试")
    detail = "命令通过" if code == 0 else f"退出码 {code}"
    return Result(check.key, check.title, "PASS" if code == 0 else "FAIL",
                  time.time() - started, detail, output)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _tail(text: str, lines: int = 40) -> str:
    rows = [row for row in (text or "").splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键自检并汇总")
    parser.add_argument("--only", action="append", default=[], help="只跑指定检查（可重复）")
    parser.add_argument("--list", action="store_true", help="列出所有检查项")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    parser.add_argument("--in-process", action="store_true",
                        help="全部在进程内跑（沙箱禁止启动子进程时使用）")
    parser.add_argument("--verbose", action="store_true", help="打印每个检查的完整输出")
    args = parser.parse_args(argv)

    checks = [c for c in CHECKS if not args.only or c.key in args.only]
    if args.list:
        for check in CHECKS:
            mark = " (信息项/进程内)" if check.func is not None else ""
            print(f"  {check.key:<16} {check.title}{mark}")
        return 0
    if not checks:
        print(f"没有匹配的检查项：{args.only}", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"  spark-keeper 自检 · 项目根 {PROJECT_ROOT}")
    print(f"  版本档位 {EDITION} · Python {sys.version.split()[0]} · 检查项 {len(checks)} 个"
          f"{' · 进程内模式' if args.in_process else ''}")
    print("=" * 78)

    results: list[Result] = []
    for check in checks:
        print(f"\n>>> {check.key}: {check.title}")
        result = run_check(check, in_process=args.in_process)
        results.append(result)
        icon = {"PASS": "PASS ✅", "FAIL": "FAIL ❌", "SKIP": "SKIP ⏭"}[result.status]
        print(f"    {icon}  {result.detail}   ({result.seconds:.1f}s)")
        if result.output and (args.verbose or result.status == "FAIL"):
            print("    " + "-" * 70)
            for row in _tail(result.output, 80 if args.verbose else 40).splitlines():
                print(f"    | {row}")
            print("    " + "-" * 70)
        sys.stdout.flush()

    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]

    if args.json:
        print(json.dumps({
            "ok": not failed,
            "results": [{"key": r.key, "title": r.title, "status": r.status,
                         "seconds": round(r.seconds, 2), "detail": r.detail} for r in results],
        }, ensure_ascii=False, indent=2))

    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    for result in results:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭"}[result.status]
        print(f"  {icon} {result.key:<16} {result.seconds:>7.1f}s  {result.detail}")
    print("-" * 78)
    if failed:
        print(f"  结论：{len(failed)} 项失败 / {len(results)} 项检查"
              f"（跳过 {len(skipped)} 项）—— 失败项：{', '.join(r.key for r in failed)}")
        return 1
    print(f"  结论：全部通过 ✅（{len(results)} 项检查，跳过 {len(skipped)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
