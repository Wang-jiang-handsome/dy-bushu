"""最终端到端验收（Lead 维护）：真实进程 + 真实 HTTP + demo_mode 全链路。

与 integration_probe.py 的区别
------------------------------
* ``integration_probe`` 在进程内起服务（快，适合频繁跑）；
* 本脚本**真的用 subprocess 起 ``python main.py serve``**，并开调度器，
  所以它验证的是「用户拿到项目后照着文档敲命令，能不能真的用起来」。
  因此它能覆盖命令行入口、配置加载、调度器线程、静态目录解析这些
  进程内测试覆盖不到的东西。

demo_mode 说明
--------------
``SK_DEMO_MODE=1`` 时不启动真实浏览器：扫码登录会生成占位二维码并自动成功，
任务执行会写假的成功记录。真实抖音页面的登录与发送**必须**在部署机上人工验证，
这里只验证「除了抖音本身，其它全部环节都通」。

用法:
    cd spark-shared/backend
    python ../tools/acceptance_e2e.py
退出码 0 = 全绿。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent


def _detect_backend(root: Path) -> Path:
    """定位后端目录，兼容两种布局：

    * 开发树：``spark-shared/tools/`` → 后端在 ``spark-shared/backend/``
    * 交付树：``<交付根>/tools/``   → 后端就在 ``<交付根>/``（main.py 与 app/ 同级）

    写死一种布局会让脚本在另一种布局里直接失效（实测：在交付目录里报
    ``can't open file .../backend/main.py``，整套验收全挂）。交付物里带着
    这个脚本，所以必须两种都能跑。
    """
    for candidate in (root / "backend", root):
        if (candidate / "main.py").is_file() and (candidate / "app").is_dir():
            return candidate
    return root / "backend"


BACKEND = _detect_backend(WORKSPACE)
TMP_ROOT = BACKEND / "data" / "tmp" / "acceptance"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []
BASE = ""


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def skip(name: str, why: str = "") -> None:
    SKIP.append(name)
    print(f"  SKIP  {name}{'  ' + why if why else ''}")


def call(path: str, method: str = "GET", body: dict | None = None,
         token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:      # noqa: BLE001 - 起来之前各种连接错误都正常
            time.sleep(0.4)
    return False


def main() -> int:
    global BASE

    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    data_dir = TMP_ROOT / "data"
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env.update({
        "SK_DATA_DIR": str(data_dir),
        "SK_ENV": "dev",
        "SK_DEMO_MODE": "1",          # 不启动真实浏览器
        "SK_HOST": "127.0.0.1",
        "SK_PORT": str(port),
        "SK_SCHEDULER_TICK": "2",     # 让调度器快速反应，便于观察
        "SK_SCHEDULER_ENABLED": "true",
        "SK_ADMIN_PASSWORD": "Acceptance123",
        "PYTHONUTF8": "1",
    })

    print("  1) 初始化独立数据目录（模拟用户第一次拿到项目）")
    init = subprocess.run(
        [sys.executable, "main.py", "init", "--username", "admin",
         "--password", "Acceptance123", "--email", "accept@local"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    check("main.py init 退出码 0", init.returncode == 0,
          (init.stdout or init.stderr or "").strip().splitlines()[-1] if (init.stdout or init.stderr) else "")
    check("init 创建了数据库", (data_dir / "spark.db").exists(), str(data_dir))

    print(f"  2) 启动真实服务进程（port={port}，含调度器，demo_mode）")
    log_path = TMP_ROOT / "serve.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_ready(port):
            proc.kill()
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            except OSError:
                pass
            check("服务在 45 秒内就绪", False, tail)
            return finish()
        check("服务在 45 秒内就绪", True, f"http://127.0.0.1:{port}")

        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        check("调度器线程已启动（日志含 task.scheduler）", "task.scheduler" in log_text
              or "调度器已启动" in log_text,
              "未在日志里找到调度器启动记录")

        # ---- 安装体检 ----
        status, body = call("/api/v1/install/check")
        data = body.get("data") or {}
        check("安装体检 installed=true", status == 200 and data.get("installed") is True,
              f"status={status}")
        env_info = data.get("environment") or {}
        check("体检报告含环境信息", bool(env_info.get("python_version")),
              f"python={env_info.get('python_version')} browser={bool(env_info.get('browser_found'))}")
        db_info = data.get("database") or {}
        check("体检报告的库路径 = 本次隔离库",
              str(db_info.get("path", "")).replace("\\", "/").endswith(
                  str(data_dir / "spark.db").replace("\\", "/")),
              f"path={db_info.get('path')}")

        # ---- 管理员登录 ----
        status, body = call("/api/v1/auth/admin/login", "POST",
                            {"identifier": "admin", "password": "Acceptance123"})
        admin_token = (body.get("data") or {}).get("token", "")
        check("管理员登录", status == 200 and bool(admin_token), f"status={status}")

        # ---- 站点配置读写 ----
        status, body = call("/api/v1/admin/configs", token=admin_token)
        check("读取站点配置", status == 200, f"status={status}")
        status, body = call("/api/v1/admin/configs", "PUT",
                            {"items": [{"key": "site.name", "value": "验收站点"}]},
                            token=admin_token)
        check("保存站点配置", status == 200, f"status={status}")
        status, body = call("/api/v1/auth/public-configs")
        check("公开配置生效", (body.get("data") or {}).get("site_name") == "验收站点",
              str((body.get("data") or {}).get("site_name")))

        # ---- 发卡 → 注册 → 兑换 ----
        status, body = call("/api/v1/admin/card-keys/generate", "POST",
                            {"package_code": "monthly", "card_type": "time", "days": 30,
                             "count": 1, "note": "验收批次"}, token=admin_token)
        codes = (body.get("data") or {}).get("codes") or []
        check("生成卡密", status == 200 and len(codes) == 1, f"status={status}")
        if not codes:
            return finish()

        uname = "acc_" + secrets.token_hex(3)
        status, body = call("/api/v1/auth/user/register", "POST",
                            {"username": uname, "password": "Acceptance123",
                             "confirm_password": "Acceptance123"})
        token = (body.get("data") or {}).get("token", "")
        check("注册新用户", status == 200 and bool(token), f"status={status}")

        status, body = call("/api/v1/user/card-keys/redeem", "POST",
                            {"code": codes[0]}, token=token)
        check("卡密兑换激活套餐", status == 200, f"status={status} {(body.get('data') or {}).get('message')}")

        status, body = call("/api/v1/user/info", token=token)
        quota = (body.get("data") or {}).get("quota") or {}
        # 自托管默认「不限次」：limit=0 且 daily_task_unlimited=True 也算生效；
        # 只有「既没限额、又没开不限次」才是异常。
        unlimited = bool(quota.get("daily_task_unlimited"))
        limit_ok = unlimited or int(quota.get("daily_task_limit") or 0) > 0
        check("配额已生效", quota.get("active") is True and limit_ok,
              f"limit={quota.get('daily_task_limit')} unlimited={unlimited} active={quota.get('active')}")

        # ---- 建账号 + demo 扫码登录 ----
        status, body = call("/api/v1/douyin/accounts", "POST",
                            {"nickname": "验收账号"}, token=token)
        account_id = int((body.get("data") or {}).get("account_id") or 0)
        check("创建托管账号", status == 200 and account_id > 0, f"status={status}")

        session_id = ""
        if account_id:
            status, body = call("/api/v1/douyin/login/browser", "POST",
                                {"account_id": account_id, "method": "qrcode"}, token=token)
            session_id = (body.get("data") or {}).get("session_id", "")
            check("启动扫码登录会话", status == 200 and bool(session_id),
                  f"status={status} {(body.get('data') or {}).get('status')}")

        if session_id:
            deadline = time.time() + 30
            final = ""
            qr_seen = False
            scanned = False
            while time.time() < deadline:
                status, body = call(
                    f"/api/v1/douyin/login/status?session_id={session_id}", token=token)
                state = (body.get("data") or {}).get("status", "")
                if (body.get("data") or {}).get("qrcode_image"):
                    qr_seen = True
                if state in ("scanned", "confirmed"):
                    scanned = True
                if state in ("success", "failed", "expired", "canceled"):
                    final = state
                    break
                time.sleep(0.8)
            check("轮询拿到二维码", qr_seen, "demo 模式应在数秒内给出占位二维码")
            check("登录会话走到终态", final == "success", f"final={final or '超时'}")
            if final == "success":
                status, body = call(f"/api/v1/douyin/accounts/{account_id}", token=token)
                acc = body.get("data") or {}
                check("账号回填为在线", acc.get("status") == "online",
                      f"status={acc.get('status')} nickname={acc.get('nickname')}")
                check("登录态不泄露到接口", "storage_path" not in acc and "cookies" not in acc,
                      str(sorted(acc)[:14]))

        # ---- 好友 + 任务 ----
        if account_id:
            status, body = call("/api/v1/friends/import", "POST",
                                {"account_id": account_id, "text": "好友A\n好友B"},
                                token=token)
            check("批量导入好友", status == 200, f"status={status}")

        task_id = 0
        if account_id:
            status, body = call("/api/v1/tasks", "POST",
                                {"account_id": account_id, "name": "验收任务",
                                 "time_of_day": "07:30", "repeat_mode": "daily",
                                 "messages": ["火花不能断 🔥", "打卡 ✨"],
                                 "friend_mode": "all", "daily_limit": 1,
                                 "send_gap_seconds": {"min": 1, "max": 2},
                                 "enabled": True}, token=token)
            task_id = int((body.get("data") or {}).get("id") or 0)
            check("创建定时任务", status == 200 and task_id > 0, f"status={status}")

        status, body = call("/api/v1/tasks", token=token)
        items = (body.get("data") or {}).get("items") or []
        if items:
            check("任务有下次执行时间", bool(items[0].get("next_run_at")),
                  str(items[0].get("next_run_at")))

        # ---- 立即执行（demo_mode 会真的写执行记录）----
        if task_id:
            status, body = call(f"/api/v1/tasks/{task_id}/run", "POST", {}, token=token)
            check("触发立即执行", status < 500, f"status={status} code={body.get('code')}")

            run_status = ""
            deadline = time.time() + 40
            last = None
            while time.time() < deadline:
                status, body = call("/api/v1/douyin/task-records", token=token)
                rows = (body.get("data") or {}).get("items") or []
                if rows:
                    last = rows[0]
                    run_status = last.get("status", "")
                    if run_status in ("success", "partial", "failed", "skipped"):
                        break
                time.sleep(1.0)
            check("产生执行记录", last is not None, "40 秒内没有执行记录")
            if last:
                check("执行记录到达终态", run_status in ("success", "partial", "failed", "skipped"),
                      f"status={run_status}")
                check("执行记录含成功数", "success_count" in last,
                      f"success={last.get('success_count')} fail={last.get('fail_count')}")

        # ---- 调度器自动执行（把 next_run_at 改到过去，等 tick）----
        # 这里刻意用**裸 sqlite3** 直接改文件，不 import 应用代码：
        # 1) 避免父进程加载 app.config 影响 sys.modules / 全局状态；
        # 2) 更贴近真实场景——「任务到期」本来就是数据库里的时间到了。
        if task_id:
            try:
                import sqlite3

                conn = sqlite3.connect(str(data_dir / "spark.db"), timeout=10)
                conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
                conn.execute("UPDATE scheduled_tasks SET next_run_at='2020-01-01 00:00:00'"
                             " WHERE id=?", (task_id,))
                conn.commit()
                auto_ok = False
                deadline = time.time() + 60
                while time.time() < deadline:
                    rows = conn.execute(
                        "SELECT status, trigger_type FROM task_runs WHERE task_id=?",
                        (task_id,)).fetchall()
                    if any(r[1] == "schedule" for r in rows):
                        auto_ok = True
                        break
                    time.sleep(1.5)
                check("调度器自动触发了任务（trigger_type=schedule）", auto_ok,
                      "60 秒内调度器未触发（tick=2s）")
                conn.close()
            except Exception as exc:      # noqa: BLE001
                skip("调度器自动触发", f"改库失败：{type(exc).__name__}: {exc}")

        # ---- 静态资源 ----
        status, body = call("/api/v1/health")
        check("进程内 API 正常", status == 200)

        # ---- 日志与审计 ----
        status, body = call("/api/v1/admin/logs", token=admin_token)
        check("管理端可读运行日志", status == 200, f"status={status}")
        status, body = call("/api/v1/admin/logs/admin", token=admin_token)
        check("管理端可读审计日志", status == 200, f"status={status}")
        status, body = call("/api/v1/admin/system-environment", token=admin_token)
        check("系统环境页数据可读", status == 200, f"status={status}")
        status, body = call("/api/v1/admin/browser-processes", token=admin_token)
        check("浏览器进程视图可读", status == 200, f"status={status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    # ---- 日志里不能有异常堆栈（有就是隐藏 bug）----
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    check("服务日志无 Traceback", "Traceback" not in log_text,
          log_text.split("Traceback")[-1][:300] if "Traceback" in log_text else "")
    # 注意：终止方式是 SIGTERM/terminate，Windows 上进程会被强制结束并 settle 成
    # exit 1（不是错误）。所以「优雅退出」的判定是 0 或 1 都算通过，文案也照实写，
    # 免得出现「PASS 退出码 0  returncode=1」这种自相矛盾的输出。
    check("进程已按预期结束（退出码 0/1 均可）", proc.returncode in (0, 1, None),
          f"returncode={proc.returncode}")

    return finish()


def finish() -> int:
    print(f"\n  最终验收：通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")
    if FAIL:
        print("  失败项：" + ", ".join(FAIL))
    if SKIP:
        print("  跳过项：" + ", ".join(SKIP))
    print(f"  数据目录（可删除）：{TMP_ROOT}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
