#!/usr/bin/env python
"""一键获取抖音登录 Cookie —— 独立小工具（零第三方依赖）。

它解决什么问题
--------------
网页端「扫码登录」依赖抖音自己的登录弹层和二维码渲染，实测会碰到
「弹层打开了但二维码图片一直不出现」（风控 / 网络 / 版本差异）。
本工具换一条完全不依赖页面结构的路线：

    打开一个**真实可见**的 Chrome 窗口 → 你自己扫码（或密码/短信）登录
    → 工具轮询到 sessionid 出现 → 自动把整份 Cookie 抓出来交给你。

因为窗口是真实浏览器、你亲手登录，所以不存在无头特征被识别的问题。

四种用法
--------
1) 只打印 Cookie（粘到网页「导入 Cookie」里）::

       python tools/get_cookie.py

2) 直接写入本机账号（**推荐**，写入后后台立刻变 online，不用复制粘贴）::

       python tools/get_cookie.py --account 1

3) 写入远端实例（服务跑在别的机器/容器里）::

       python tools/get_cookie.py --api-url http://127.0.0.1:8000 --token <用户令牌> --account 1

4) 存成文件（备份/给别人）::

       python tools/get_cookie.py --out cookie.txt

常用选项
--------
``--profile DIR``   复用同一个浏览器配置目录（默认 ``data/profiles/cookie_helper``）。
                    第二次运行时如果上次还登录着，工具会**直接**抓到 Cookie，一秒完成。
``--timeout 300``   等待你完成登录的最长秒数（默认 300）。
``--headless``      无头模式（不推荐：抖音更容易风控，看不到窗口也没法扫码）。
``--json``          打印 JSON 数组而不是 ``name=value; ...`` 头格式。
``--clean-profile`` 抓完就删掉这个临时浏览器配置目录。

退出码
------
``0`` 成功；``1`` 等待超时 / 浏览器异常；``2`` 环境不满足（没装浏览器、
数据库没初始化等）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# 路径：既支持开发树（backend/app），也支持交付树（app 在根目录）
# --------------------------------------------------------------------------
TOOL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOL_DIR.parent


def _bootstrap_path() -> Path:
    """把包含 ``app`` 包的目录塞进 sys.path，返回项目根。"""
    candidates = [PROJECT_ROOT, PROJECT_ROOT / "backend"]
    for item in candidates:
        if (item / "app" / "__init__.py").exists():
            if str(item) not in sys.path:
                sys.path.insert(0, str(item))
            return PROJECT_ROOT
    print("✗ 找不到 app 包：请在项目根目录运行本工具（依赖 app/ 与 web/ 目录）",
          file=sys.stderr)
    raise SystemExit(2)


_bootstrap_path()

from app.common import timeutil                       # noqa: E402
from app.config import get_settings                   # noqa: E402
from app.automation import cdp as cdp_mod             # noqa: E402
from app.automation import session_store              # noqa: E402

LOGIN_URL = "https://www.douyin.com/"
#: 抓 Cookie 时只取这些域名下的（抖音主站 + 其静态域）
DOUYIN_DOMAIN_HINTS = ("douyin.com", "douyinpic.com", "douyinstatic.com", "bytedance.com")


# --------------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------------
def is_login_cookie(cookie: dict) -> bool:
    """这条 cookie 是不是「已登录」的标志（sessionid 等）。"""
    name = str((cookie or {}).get("name") or "")
    value = str((cookie or {}).get("value") or "").strip()
    return name in session_store.LOGIN_COOKIE_NAMES and bool(value)


def pick_douyin_cookies(cookies: list[dict]) -> list[dict]:
    """挑出抖音相关域名的 cookie（打印/导出用；session 文件里保留全部）。"""
    out: list[dict] = []
    for cookie in cookies or []:
        domain = str(cookie.get("domain") or "")
        if any(hint in domain for hint in DOUYIN_DOMAIN_HINTS):
            out.append(cookie)
    return out


def cookie_header(cookies: list[dict]) -> str:
    """拼成 ``sessionid=xxx; ttwid=yyy`` 形式（网页「导入 Cookie」直接可粘）。"""
    pairs = []
    for cookie in cookies or []:
        name = str(cookie.get("name") or "").strip()
        if not name:
            continue
        pairs.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(pairs)


def has_login_cookie(cookies: list[dict]) -> bool:
    return any(is_login_cookie(c) for c in cookies or [])


def summarize(cookies: list[dict]) -> dict:
    """不含 cookie 值的摘要（用于打印/日志）。"""
    names = [str(c.get("name") or "") for c in cookies or [] if c.get("name")]
    return {
        "count": len(names),
        "has_sessionid": "sessionid" in names,
        "names": names,
    }


# --------------------------------------------------------------------------
# 浏览器
# --------------------------------------------------------------------------
def launch_browser(settings, profile: Path, headless: bool):
    """开一个真实窗口（默认 headful），返回 (chrome, session)。"""
    settings.browser_headless = bool(headless)
    chrome = cdp_mod.launch_chrome(settings, profile, start_url="about:blank")
    session = cdp_mod.create_page(chrome, LOGIN_URL)
    session.enable_domains(network=True)
    return chrome, session


def wait_for_login(session, *, timeout: float, poll: float = 1.0) -> list[dict]:
    """轮询直到出现登录态 cookie；返回当时的全部 cookie。

    超时返回空列表。每 2 秒打一个点，让用户知道工具还活着。
    """
    deadline = time.monotonic() + max(10.0, float(timeout))
    last_dot = 0.0
    while time.monotonic() < deadline:
        try:
            cookies = session.get_cookies() or []
        except cdp_mod.CDPError as exc:
            print(f"\n✗ 浏览器连接断开（窗口被关掉了？）：{exc}", file=sys.stderr)
            return []
        if has_login_cookie(cookies):
            return list(cookies)
        now = time.monotonic()
        if now - last_dot >= 2.0:
            print(".", end="", flush=True)
            last_dot = now
        time.sleep(poll)
    return []


# --------------------------------------------------------------------------
# 落地
# --------------------------------------------------------------------------
def save_to_local_account(account_id: int, cookies: list[dict], state: dict,
                          settings) -> dict:
    """直接写进本机账号：session 文件 + 数据库状态。需要数据库已初始化。"""
    from app import bootstrap, db

    try:
        bootstrap.init_app(settings, quiet=True)
    except Exception as exc:                  # noqa: BLE001
        return {"ok": False, "message": f"数据库未初始化或不可用：{exc}"}
    row = db.query_one("SELECT * FROM douyin_accounts WHERE id=?", (int(account_id),))
    if not row:
        return {"ok": False,
                "message": f"账号 #{account_id} 不存在。可以先在后台「新增账号」，"
                           f"或改用 --api-url 模式"}
    storage_name = session_store.session_filename(int(account_id))
    session_store.save_state(int(account_id), {
        "cookies": cookies,
        "origins": dict(state.get("origins") or {}),
        "user_agent": str(state.get("user_agent") or ""),
        "url": str(state.get("url") or LOGIN_URL),
    }, settings)
    now = timeutil.utc_now_str()
    db.update("douyin_accounts", {
        "status": "online",
        "storage_path": storage_name,
        "storage_updated_at": now,
        "last_check_at": now,
        "last_error": "",
        "updated_at": now,
    }, "id=?", (int(account_id),))
    return {"ok": True, "storage_path": storage_name,
            "message": f"已写入账号 #{account_id}（{row.get('nickname') or '未命名'}），状态：online"}


def post_to_api(api_url: str, token: str, account_id: int, header: str,
                *, verify: bool = True, timeout: float = 120.0) -> dict:
    """调用远端实例的「导入 Cookie」接口。"""
    url = api_url.rstrip("/") + f"/api/v1/douyin/accounts/{int(account_id)}/cookies"
    body = json.dumps({"cookies": header, "verify": bool(verify)}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
        data = payload.get("data") or {}
        return {"ok": True, "verified": data.get("verified"),
                "message": str(payload.get("message") or "已导入")}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            payload = {}
        return {"ok": False, "message": str(payload.get("message") or f"HTTP {exc.code}")}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "message": f"连不上 {url}：{exc}"}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="get_cookie.py",
        description="打开真实浏览器窗口登录抖音，自动抓取登录 Cookie",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--account", type=int, default=0,
                        help="抖音账号 ID（后台账号列表里那一列数字）")
    parser.add_argument("--api-url", default="",
                        help="远端实例地址，例如 http://127.0.0.1:8000（需配 --token）")
    parser.add_argument("--token", default="",
                        help="用户登录本站后的 access_token（配合 --api-url）")
    parser.add_argument("--out", default="", help="把结果写到文件（.json 或 .txt）")
    parser.add_argument("--profile", default="",
                        help="浏览器配置目录（默认 data/profiles/cookie_helper）")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="等待登录的最长秒数（默认 300）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（不推荐：抖音更容易风控）")
    parser.add_argument("--json", action="store_true", help="打印 JSON 数组而不是头格式")
    parser.add_argument("--clean-profile", action="store_true",
                        help="结束后删除本次的浏览器配置目录")
    parser.add_argument("--no-verify", action="store_true",
                        help="写接口时不要求远端校验（默认会校验）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if not getattr(settings, "data_dir", None):
        print("✗ 读不到数据目录配置", file=sys.stderr)
        return 2

    profile = Path(args.profile).expanduser() if args.profile else \
        Path(settings.data_dir) / "profiles" / "cookie_helper"
    print("=" * 66)
    print("  抖音 Cookie 获取工具")
    print("=" * 66)
    print(f"  浏览器配置：{profile}")
    print(f"  登录地址  ：{LOGIN_URL}")
    if args.account and args.api_url:
        print(f"  写入目标  ：远端 {args.api_url} 的账号 #{args.account}")
    elif args.account:
        print(f"  写入目标  ：本机账号 #{args.account}")
    elif args.out:
        print(f"  保存到    ：{args.out}")
    else:
        print("  输出方式  ：打印到屏幕（可粘到网页「导入 Cookie」）")
    print()
    print("  接下来会弹出一个 Chrome 窗口：")
    print("    1) 在里面用抖音 App 扫码，或用手机号/密码登录；")
    print("    2) 登录成功后**工具会自动继续**，无需其他操作；")
    print(f"    3) 最多等 {int(args.timeout)} 秒，Ctrl+C 可随时中止。")
    print("-" * 66)

    chrome = None
    try:
        try:
            chrome, session = launch_browser(settings, profile, args.headless)
        except Exception as exc:                       # noqa: BLE001
            print(f"✗ 浏览器启动失败：{exc}", file=sys.stderr)
            print("  请确认已安装 Chrome/Edge，或用 SK_CHROME_PATH 指定路径。",
                  file=sys.stderr)
            return 2

        print("  已打开浏览器窗口，等待你完成登录", end="", flush=True)
        cookies = wait_for_login(session, timeout=args.timeout)
        if not cookies:
            print()
            print("✗ 超时：没有检测到登录成功的 Cookie。", file=sys.stderr)
            print("  请确认：① 窗口里确实显示已登录（右上角出现头像）；"
                  "② 没有关掉窗口；③ 用 --timeout 加大等待时间。", file=sys.stderr)
            return 1

        # 让页面把其余 cookie（ttwid 等）落全，再整体抓一遍
        time.sleep(1.5)
        try:
            state = session_store.capture_state(session,
                                                extra_origins=[session_store.MAIN_ORIGIN])
            all_cookies = list(state.get("cookies") or cookies)
        except Exception:                              # noqa: BLE001
            state, all_cookies = {}, cookies
        douyin_cookies = pick_douyin_cookies(all_cookies)
        header = cookie_header(douyin_cookies)
        info = summarize(all_cookies)
        print()
        print(f"✓ 登录成功！抓到 {info['count']} 条 cookie"
              f"（含 sessionid={info['has_sessionid']}）")

        # ---- 落地：远端接口 / 本机账号 / 文件 / 屏幕 ----
        wrote_somewhere = False
        if args.account and args.api_url:
            if not args.token:
                print("✗ 用 --api-url 时必须同时提供 --token", file=sys.stderr)
                return 2
            result = post_to_api(args.api_url, args.token, args.account, header,
                                 verify=not args.no_verify)
            print(("✓ " if result.get("ok") else "✗ ") + str(result.get("message")))
            wrote_somewhere = bool(result.get("ok"))
            if not result.get("ok"):
                return 1
        elif args.account:
            result = save_to_local_account(args.account, all_cookies, state, settings)
            print(("✓ " if result.get("ok") else "✗ ") + str(result.get("message")))
            if not result.get("ok"):
                return 1
            wrote_somewhere = True

        if args.out:
            target = Path(args.out).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() == ".json":
                target.write_text(json.dumps(all_cookies, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            else:
                target.write_text(header + "\n", encoding="utf-8")
            print(f"✓ 已保存到 {target}")
            wrote_somewhere = True

        if not wrote_somewhere:
            print()
            print("  把下面这一行整段粘到网页「抖音账号 → 导入 Cookie」里：")
            print("-" * 66)
            if args.json:
                print(json.dumps(douyin_cookies, ensure_ascii=False, indent=1))
            else:
                print(header)
            print("-" * 66)
        return 0
    except KeyboardInterrupt:
        print("\n已中止。", file=sys.stderr)
        return 1
    finally:
        if chrome is not None:
            try:
                chrome.close()
            except Exception:                          # noqa: BLE001
                pass
        if args.clean_profile:
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
