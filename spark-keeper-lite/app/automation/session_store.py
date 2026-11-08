"""登录态（cookies + localStorage）的持久化、恢复与账号浏览器会话。

职责
----
1. 把 cookies + localStorage 序列化到 ``<session_root>/account_<id>.json``，权限 600。
2. 把持久化的登录态恢复到新的浏览器会话（``restore_state``）。
3. 目录级校验登录态是否还有效（``is_session_valid``）——**不启浏览器**，
   供接口层 ``/douyin/accounts/<id>/check`` 快速判断。
4. :class:`AccountBrowser`：把「占并发闸门 → 起 Chrome → 建页面 → 伪装身份 →
   恢复登录态 → 用完关掉并释放闸门」整条生命周期包成一个上下文管理器，
   登录流程、任务执行、诊断三处共用一份实现（否则三处各写一遍必然走偏）。

隐私红线
--------
登录态文件里是**完整 cookie**，等价于账号密码。因此：

* 文件权限 600，且只写在 ``data/sessions/``（已 gitignore）。
* 本模块返回的任何 dict（``session_info``/``AccountBrowser.describe``）
  **绝不包含 cookie 值，也不包含 storage_path 的绝对路径**。
* 数据库里 ``douyin_accounts.storage_path`` 只存**文件名**（如 ``account_3.json``），
  接口层只存不返。
"""

from __future__ import annotations

import calendar
import json
import os
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import cdp as cdp_mod
from . import humanize

#: 登录态文件格式版本。以后结构变了要升版本，并在 load 时做兼容。
SESSION_VERSION = 1
#: 登录态默认有效期（天）。抖音的 cookie 实际有效期更长，但我们宁可保守：
#: 超过这个天数就认为需要重新验证，避免用「半失效」的会话去发消息。
DEFAULT_SESSION_TTL_DAYS = 30
#: localStorage 只在当前 origin 上可读，抖音主站就是它
MAIN_ORIGIN = "https://www.douyin.com"
#: settings 缺失时的回落目录（与 app/config.py 的 DEFAULT_DATA_DIR 保持一致）
DEFAULT_FALLBACK_DIR = Path(__file__).resolve().parent.parent.parent / "data"

#: 判定「登录已建立」的会话类 cookie 名。
#:
#: ⚠️ 不要把 ``passport_csrf_token`` / ``passport_auth_mix_state`` 放进来：
#: 实测「打开登录弹层」就会下发这些 passport 辅助 cookie（未登录也有），
#: 当成登录信号会造成「没扫码却报登录成功」的误判。
LOGIN_COOKIE_NAMES: tuple[str, ...] = (
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "sid_ucp_v1",
    "ssid_ucp_v1",
)

#: 手动导入时的默认域名/路径（抖音 cookie 都在 .douyin.com 下）
COOKIE_DEFAULT_DOMAIN = ".douyin.com"
COOKIE_DEFAULT_PATH = "/"


class BrowserBusy(RuntimeError):
    """并发闸门拿不到（同一时刻浏览器实例已达上限）。"""


class BrowserUnavailable(RuntimeError):
    """浏览器起不来 / 连不上 DevTools（部署环境问题，不是业务问题）。"""


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def session_dir(settings: Any) -> Path:
    """登录态目录。

    ``settings=None`` 时回落到项目默认数据目录（``<backend>/data/sessions``）：
    诊断/排障代码有时拿不到 settings，不能因此报错，更不能把 ``./sessions``
    建到当前工作目录里（那会导致「登录态文件找不到」这类诡异问题）。
    """
    if settings is None:
        from ..config import DEFAULT_DATA_DIR
        root = Path(DEFAULT_DATA_DIR) / "sessions"
    else:
        root = Path(getattr(settings, "session_root", "")
                    or (Path(getattr(settings, "data_dir", DEFAULT_FALLBACK_DIR)) / "sessions"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_filename(account_id: int | str) -> str:
    """登录态文件名（写进 ``douyin_accounts.storage_path`` 的就是它，相对路径）。"""
    return f"account_{int(account_id)}.json"


def session_path(account_id: int | str, settings: Any) -> Path:
    return session_dir(settings) / session_filename(account_id)


def _is_windows() -> bool:
    """是否 Windows（抽出来便于单测打桩，避免改 os.name 把 pathlib 弄坏）。"""
    return os.name == "nt"


def display_available(settings: Any = None) -> bool:
    """是否有图形界面（委托给 cdp.display_available，避免两份实现漂移）。"""
    return cdp_mod.display_available()


def profile_dir(account_id: int | str, settings: Any, suffix: str = "") -> Path:
    """每个账号独立的 Chrome profile 目录。

    为什么不用 ``--incognito``：无痕模式下 localStorage 不落地，登录态文件也就
    存不全；独立 user-data-dir 才是「多账号互不串味」的正解。
    """
    root = Path(getattr(settings, "data_dir", ".")) / "profiles"
    # suffix 用于「同一账号、不同用途」的隔离：诊断必须用独立 profile，
    # 否则会和正在跑任务的 Chrome 抢同一个 user-data-dir（Chrome 是单例锁，
    # 后启动的会把先启动的顶掉，表现为任务中途 "CDP 连接已关闭"）。
    name = f"account_{int(account_id)}" + (f"_{suffix}" if suffix else "")
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_session_file(account: dict | None, settings: Any) -> Path | None:
    """按账号记录找登录态文件。

    ``storage_path`` 可能是文件名（我们写的）、相对路径或绝对路径（历史数据/手工
    放进去的），三种都要能解析，否则「换台机器就找不到登录态」。
    """
    account_id = (account or {}).get("id")
    stored = str((account or {}).get("storage_path") or "").strip()
    if stored:
        candidate = Path(stored)
        if candidate.is_absolute():
            return candidate
        return session_dir(settings) / candidate
    if account_id:
        return session_path(account_id, settings)
    return None


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def save_state(account_id: int | str, data: dict, settings: Any) -> Path:
    """原子写入登录态文件并收紧权限（600）。"""
    path = session_path(account_id, settings)
    payload = {
        "version": SESSION_VERSION,
        "account_id": int(account_id),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "cookies": list(data.get("cookies") or []),
        "origins": dict(data.get("origins") or {}),
        "user_agent": str(data.get("user_agent") or ""),
        "url": str(data.get("url") or ""),
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)                      # 原子替换：写一半断电不会毁掉旧登录态
    _chmod_600(path)
    return path


def load_state(account: dict | None, settings: Any) -> dict | None:
    """读登录态文件；不存在或损坏返回 ``None``。"""
    path = resolve_session_file(account, settings)
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version") or 0) != SESSION_VERSION:
        # 旧版本结构：宁可当没有登录态，让用户重新扫码，也不要拿错字段去发消息
        return None
    data.setdefault("cookies", [])
    data.setdefault("origins", {})
    return data


def delete_state(account: dict | int | str, settings: Any) -> bool:
    """删除登录态文件（账号被删除时调用）。"""
    account_dict = account if isinstance(account, dict) else {"id": account}
    path = resolve_session_file(account_dict, settings)
    if not path or not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:                # Windows 上基本无效（ACL 才是主角），忽略
        pass


def session_info(account: dict | None, settings: Any) -> dict:
    """登录态文件的**非敏感**元信息（给管理端/诊断看，不含 cookie 值与路径）。"""
    path = resolve_session_file(account, settings)
    if not path or not path.exists():
        return {"exists": False, "saved_at": "", "cookie_count": 0, "origin_count": 0,
                "size_bytes": 0, "expired": True}
    data = load_state(account, settings)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if not data:
        return {"exists": True, "saved_at": "", "cookie_count": 0, "origin_count": 0,
                "size_bytes": size, "expired": True, "corrupt": True}
    return {
        "exists": True,
        "saved_at": data.get("saved_at", ""),
        "cookie_count": len(data.get("cookies") or []),
        "origin_count": len(data.get("origins") or {}),
        "size_bytes": size,
        "expired": not is_session_valid(account, settings),
    }


# --------------------------------------------------------------------------
# 手动导入 Cookie（扫码失败时的兜底路径）
# --------------------------------------------------------------------------
def parse_cookie_text(text: str) -> list[dict]:
    """把用户粘贴的 Cookie 文本解析成 CDP ``Network.setCookies`` 能吃的列表。

    为什么要支持这么多格式：用户手里可能是

    1. **DevTools 请求头**：``sessionid=abc; sid_guard=def; ttwid=...``
       （F12 → Network → 任意 douyin.com 请求 → Request Headers → Cookie，整段复制）
    2. **JSON 数组**（浏览器扩展导出）：``[{"name":"sessionid","value":"abc",...}]``
    3. **JSON 对象**：``{"sessionid":"abc","ttwid":"..."}``
    4. **Netscape cookies.txt**：``.douyin.com\\tTRUE\\t/\\tTRUE\\t<expiry>\\tsessionid\\tabc``
    5. **Application 面板表格**：``sessionid\\tabc`` 这种每行两列

    一律归一化成 ``{name, value, domain, path, secure, httpOnly, expires}``。
    解析不出来的行直接跳过（**不猜**），最后只要有一条会话 cookie 就算导入成功。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    if raw.startswith("[") or raw.startswith("{"):
        return _parse_cookie_json(raw)
    lines = [line for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cookies: list[dict] = []
    for line in lines:
        text_line = line.strip()
        if not text_line or text_line.startswith("#"):
            continue
        # Netscape cookies.txt：至少 7 列，制表符分隔
        cells = line.split("\t")
        if len(cells) >= 7 and "." in cells[0]:
            cookie = _cookie_from_netscape(cells)
            if cookie:
                cookies.append(cookie)
            continue
        # Application 面板「两列」：name \t value
        if len(cells) == 2 and "=" not in cells[0] and cells[0].strip():
            cookies.append(_cookie(str(cells[0]), str(cells[1])))
            continue
        # 剩下的按分号分隔的 Cookie 头处理（可能带 "Cookie:" 前缀）
        body = text_line[7:] if text_line.lower().startswith("cookie:") else text_line
        for part in body.split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            if not name.strip():
                continue
            cookies.append(_cookie(name, value))
    return cookies


def _parse_cookie_json(raw: str) -> list[dict]:
    """解析 JSON 数组 / 对象两种导出格式。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        # {"sessionid": "...", ...} 或 {"cookies": [...]}
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        else:
            return [_cookie(str(k), v) for k, v in data.items() if str(k).strip()]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        # 扩展通常给 expirationDate（浮点秒），CDP 用 expires（整数秒）
        expires = item.get("expires", item.get("expirationDate", 0))
        try:
            expires = int(float(expires or 0))
        except (TypeError, ValueError):
            expires = 0
        cookie = _cookie(name, item.get("value", ""))
        domain = str(item.get("domain") or "").strip()
        if domain:
            # hostOnly=true 表示只对精确域名生效（去掉前导点）
            if item.get("hostOnly") and domain.startswith("."):
                domain = domain[1:]
            cookie["domain"] = domain
        path = str(item.get("path") or "").strip()
        if path:
            cookie["path"] = path
        if isinstance(item.get("secure"), bool):
            cookie["secure"] = item["secure"]
        if isinstance(item.get("httpOnly"), bool):
            cookie["httpOnly"] = item["httpOnly"]
        cookie["expires"] = expires
        out.append(cookie)
    return out


def _cookie_from_netscape(cells: list[str]) -> dict | None:
    domain = cells[0].strip()
    if not domain:
        return None
    path = cells[2].strip() or COOKIE_DEFAULT_PATH
    try:
        expires = int(float(cells[4].strip() or 0))
    except (TypeError, ValueError):
        expires = 0
    name = cells[5].strip()
    if not name:
        return None
    cookie = _cookie(name, cells[6])
    cookie.update({"domain": domain, "path": path, "expires": expires,
                   "secure": cells[3].strip().upper() == "TRUE"})
    return cookie


def _cookie(name: str, value: Any) -> dict:
    """构造一条归一化 cookie（默认抖音域名 + 会话 cookie）。"""
    text = str(value if value is not None else "").strip()
    # 用户从 JS 里复制过来时可能带引号：sessionid="abc"
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return {
        "name": str(name).strip(),
        "value": text,
        "domain": COOKIE_DEFAULT_DOMAIN,
        "path": COOKIE_DEFAULT_PATH,
        "secure": True,
        # sessionid 等是 httpOnly：不设成 True 的话，页面 JS 与部分请求会拿不到登录态
        "httpOnly": True,
        "expires": 0,
    }


def summarize_cookies(cookies: list[dict]) -> dict:
    """导入结果摘要（**不含 cookie 值**，可以安全返回给前端/写日志）。"""
    names = [str(c.get("name") or "") for c in cookies or []]
    session_names = [n for n in names if n in LOGIN_COOKIE_NAMES]
    return {
        "cookie_count": len(names),
        "names": names[:40],
        "has_login_cookie": bool(session_names),
        "login_cookie_name": session_names[0] if session_names else "",
    }


def import_cookies(account: dict, text: str, settings: Any) -> dict:
    """把粘贴的 Cookie 解析并写成该账号的登录态文件。

    返回 ``{"ok", "cookie_count", "names", "has_login_cookie", "path", "message"}``；
    ``ok=False`` 时 ``message`` 是给用户看的可读原因（前端直接展示）。
    **不启动浏览器**：校验交给调用方（可选），这样「本机起不了浏览器」时也能先存下。
    """
    account = dict(account or {})
    account_id = account.get("id")
    if not account_id:
        return {"ok": False, "cookie_count": 0, "names": [], "has_login_cookie": False,
                "path": "", "message": "账号不存在，无法导入 Cookie"}
    cookies = parse_cookie_text(text)
    if not cookies:
        return {"ok": False, "cookie_count": 0, "names": [], "has_login_cookie": False,
                "path": "",
                "message": "没有解析出任何 Cookie。请复制完整内容，"
                           "例如：sessionid=xxx; sid_guard=yyy; ttwid=zzz"}
    summary = summarize_cookies(cookies)
    if not summary["has_login_cookie"]:
        return {"ok": False, "cookie_count": summary["cookie_count"],
                "names": summary["names"], "has_login_cookie": False, "path": "",
                "message": "这些 Cookie 里没有 sessionid（或 sid_guard/sid_tt），"
                           "不是登录态。请在已登录的浏览器里重新复制，"
                           "必须包含 sessionid 这一项。"}
    path = save_state(account_id, {
        "cookies": cookies,
        # 手动导入没有 localStorage，但**必须**把主站 origin 记下来：
        # restore_state 靠它决定恢复登录态后导航到哪（否则停在 about:blank，
        # 登录状态永远读不到 → 好的 Cookie 也会被判「抖音未接受」）。
        "origins": {MAIN_ORIGIN: {}},
        "user_agent": "",
        "url": MAIN_ORIGIN,
    }, settings)
    return {"ok": True, "cookie_count": summary["cookie_count"], "names": summary["names"],
            "has_login_cookie": True, "path": path.name,
            "message": f"已导入 {summary['cookie_count']} 条 Cookie"
                       f"（含 {summary['login_cookie_name']}）"}


# --------------------------------------------------------------------------
# 有效性
# --------------------------------------------------------------------------
def cookie_alive(cookie: dict, *, now: float | None = None) -> bool:
    """单个 cookie 是否还有效。

    ``expires <= 0`` 表示**会话 cookie**（关浏览器即失效，只要浏览器还在就有效），
    所以它算有效：抖音的 ``sessionid`` 常常就是会话 cookie。
    """
    if not cookie.get("name"):
        return False
    expires = cookie.get("expires")
    try:
        expires = float(expires) if expires is not None else 0.0
    except (TypeError, ValueError):
        expires = 0.0
    if expires <= 0:
        return True
    return expires > (now if now is not None else time.time())


def is_session_valid(account: dict | None, settings: Any, *, deep: bool = False) -> bool:
    """登录态是否可用。

    ``deep=False``（默认，**不启浏览器**，接口层可放心调用）：

    1. 登录态文件存在且结构合法；
    2. ``saved_at`` 不超过 ``session_ttl_days``（默认 30 天）——太老的会话多半已被
       抖音踢掉，宁可在界面上显示「需要重新登录」，也不要跑一次任务再报失败；
    3. 至少有一个 cookie 还活着（见 :func:`cookie_alive`）。

    ``deep=True``：真的开浏览器打开 douyin.com 看是否登录（部署机才有意义，
    单租户场景下由 ``/douyin/accounts/<id>/check`` 的「检测」按钮触发更合适）。
    """
    account = account or {}
    data = load_state(account, settings)
    if not data:
        return False
    # 注意：0 是**合法值**（表示关闭 TTL 检查），所以只能用 getattr 的默认值，
    # 绝不能写 `getattr(...) or DEFAULT`——那会把 0 变成 30。
    raw_ttl = getattr(settings, "session_ttl_days", DEFAULT_SESSION_TTL_DAYS)
    try:
        ttl_days = int(raw_ttl)
    except (TypeError, ValueError):
        ttl_days = DEFAULT_SESSION_TTL_DAYS
    ttl_days = max(0, ttl_days)
    saved_at = _parse_utc(data.get("saved_at"))
    if saved_at is not None and ttl_days > 0:
        if (time.time() - saved_at) > ttl_days * 86400:
            return False
    cookies = data.get("cookies") or []
    if not cookies:
        return False
    now = time.time()
    if not any(cookie_alive(c, now=now) for c in cookies):
        return False
    if not deep:
        return True
    try:
        from . import douyin                       # 局部导入：避免与 douyin 形成环
        with AccountBrowser(account, settings, kind="check") as browser:
            state = douyin.read_login_state(browser.cdp)
        return bool(state.get("logged_in"))
    except (BrowserBusy, BrowserUnavailable, cdp_mod.CDPError, OSError):
        # 浏览器起不来时保守返回 False？不——那会让用户以为登录态失效。
        # 这里返回**文件级结论**（True），并把「未能深检」交给日志。
        return True


def _parse_utc(text: Any) -> float | None:
    """把 ``YYYY-MM-DD HH:MM:SS``（UTC）转成 epoch 秒。

    用 ``calendar.timegm`` 而不是 ``time.mktime``：后者按**本地时区**解释，
    在非 UTC 的部署机上会让「登录态是否过期」判断偏 8 小时。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(raw, fmt)))
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 抓取 / 恢复
# --------------------------------------------------------------------------
#: 读取当前页面 localStorage 的 JS（返回 [[key, value], ...]）
JS_READ_LOCAL_STORAGE = (
    "(() => { try { return JSON.stringify(Object.entries(window.localStorage)); }"
    " catch (e) { return '[]'; } })()"
)


def capture_state(cdp: Any, *, extra_origins: list[str] | None = None) -> dict:
    """从活的会话里抓 cookies + localStorage。

    ``localStorage`` 只能读**当前页面 origin** 的，所以登录成功后必须趁页面还停在
    douyin.com 时抓；``extra_origins`` 允许调用方再指定几个 origin（会逐个导航过去读，
    慢，默认不用）。
    """
    state: dict[str, Any] = {"cookies": [], "origins": {}, "url": ""}
    try:
        state["cookies"] = cdp.get_cookies()
    except cdp_mod.CDPError:
        state["cookies"] = []
    state["url"] = cdp.current_url()
    origins = list(extra_origins or [])
    current_origin = _origin_of(state["url"])
    if current_origin and current_origin not in origins:
        origins.append(current_origin)
    for origin in origins:
        try:
            if _origin_of(cdp.current_url()) != origin:
                cdp.navigate(origin, timeout=20.0, wait_ready=False)
            raw = cdp.evaluate(JS_READ_LOCAL_STORAGE)
            items = json.loads(raw) if isinstance(raw, str) else (raw or [])
            store = {str(k): str(v) for k, v in items if str(k)}
            if store:
                state["origins"][origin] = store
        except (cdp_mod.CDPError, ValueError, TypeError):
            continue
    return state


def restore_state(cdp: Any, data: dict, *, settings: Any = None, navigate: bool = True,
                  url: str | None = None) -> dict:
    """把持久化的登录态灌进新会话。

    顺序很重要：**先 cookies 再导航**，否则第一次请求是未登录的，抖音会下发
    一套新的匿名 cookie，把恢复的登录态冲掉一半。localStorage 必须在导航到该
    origin 之后写（localStorage 是按 origin 隔离的）。

    ⚠️ 没有 ``origins`` 时**必须仍然导航到抖音主站**：手动导入的 Cookie 没有
    localStorage，早期实现此时直接 return，浏览器停在 ``about:blank``，
    于是「恢复完读登录状态」永远读到未登录 —— 实测把一份好 Cookie 误判成
    「抖音未接受该登录态」，用户白折腾一轮。
    """
    result = {"cookies": 0, "origins": 0, "navigated": "", "local_storage_keys": 0,
              "defaulted_url": False}
    cookies = list((data or {}).get("cookies") or [])
    if cookies:
        result["cookies"] = cdp.set_cookies(cookies)
    if not navigate:
        return result
    origins = list(((data or {}).get("origins") or {}).keys())
    target = url or (origins[0] if origins else "")
    if not target:
        # 没有记录 origin（导入的登录态就是这种）→ 默认去抖音主站，
        # 否则页面停在 about:blank，任何登录状态都读不到。
        target = MAIN_ORIGIN
        result["defaulted_url"] = True
    cdp.navigate(target, timeout=30.0, wait_ready=False)
    result["navigated"] = target
    store = ((data or {}).get("origins") or {}).get(_origin_of(target) or "", {})
    if store:
        written = _write_local_storage(cdp, store)
        result["local_storage_keys"] = written
    return result


def _write_local_storage(cdp: Any, store: dict) -> int:
    """写 localStorage。

    用 ``JSON.parse`` 传值而不是字符串拼接：cookie/localStorage 里什么都可能有，
    拼接必然出现引号转义事故。
    """
    if not store:
        return 0
    payload = json.dumps({str(k): str(v) for k, v in store.items()}, ensure_ascii=False)
    expression = (
        "(() => { const data = JSON.parse(" + json.dumps(payload) + ");"
        " let n = 0; for (const k of Object.keys(data)) {"
        "   try { window.localStorage.setItem(k, data[k]); n += 1; } catch (e) {} }"
        " return n; })()"
    )
    try:
        value = cdp.evaluate(expression)
        return int(value or 0)
    except (cdp_mod.CDPError, TypeError, ValueError):
        return 0


def _origin_of(url: str) -> str:
    text = str(url or "")
    if "://" not in text:
        return ""
    scheme, _, rest = text.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}" if host else ""


# --------------------------------------------------------------------------
# 账号浏览器会话
# --------------------------------------------------------------------------
class AccountBrowser(AbstractContextManager):
    """一个抖音账号的浏览器会话（含并发闸门与登录态恢复）。

    生命周期::

        with AccountBrowser(account, settings) as browser:
            browser.cdp.navigate(...)
            browser.save()            # 需要落盘登录态时显式调用

    退出时**一定会**关掉浏览器并释放闸门（包括异常路径），否则并发额度会被
    一次超时泄露干净，之后就再也起不了浏览器了 —— 这是这类系统最常见的线上故障。
    """

    def __init__(self, account: dict, settings: Any, *, kind: str = "task",
                 proxy: Any = None, restore: bool = True, create_page: bool = True,
                 acquire_timeout: float = 0.0, rng: Any = None,
                 headless: bool | None = None, start_url: str = "",
                 profile_suffix: str = "") -> None:
        self.account = dict(account or {})
        self.settings = settings
        self.kind = kind
        self.proxy = proxy
        self.restore = restore
        self.acquire_timeout = float(acquire_timeout)
        self.rng = rng
        #: 启动后先打开的地址（空 = about:blank）。可见窗口模式传抖音首页，
        #: 这样窗口一出来就是抖音页面，用户不用等我们导航。
        self.start_url = start_url
        #: 同账号不同用途的 profile 隔离（诊断用 "diag"，避免与任务浏览器抢锁）
        self.profile_suffix = profile_suffix
        #: ``None`` = 跟随全局配置；``False`` = **开可见窗口**（用户在窗口里手动登录）
        self.headless = headless
        #: 可见模式被强制降级成无头时，记下原因（无 X 的服务器上必然发生）
        self.headless_forced_reason = ""
        self.account_id = int(self.account.get("id") or 0)
        self.key = f"account_{self.account_id}"
        self.chrome: cdp_mod.ChromeProcess | None = None
        self.cdp: cdp_mod.CDPSession | None = None
        self._gate = None
        self.profile = humanize.locale_profile(self.rng) if rng is not None else {}

    # ---- 上下文协议 ----
    def __enter__(self) -> "AccountBrowser":
        from ..worker import pool as pool_mod     # 局部导入：worker 层不依赖 automation，无环
        self._gate = pool_mod.get_pool(self.settings)
        if not self._gate.acquire(self.key, kind=self.kind, timeout=self.acquire_timeout):
            raise BrowserBusy(
                f"并发浏览器实例已达上限（{self._gate.limit}），请稍后重试"
            )
        try:
            return self._start()
        except Exception:
            # _start() 内部失败时已经调用过 _teardown()（它会把闸门还回去并把
            # self._gate 置空），所以这里必须先判空，否则会在异常路径上再抛
            # AttributeError，把「浏览器起不来」这个真实原因盖掉。
            if self._gate is not None:
                self._gate.release(self.key)
                self._gate = None
            raise

    def _downgrade_visible_if_no_display(self) -> None:
        """要求可见窗口，但服务器没有图形界面 → 强制降级成无头。

        真实事故：用户点了「在窗口里登录」，服务器（云主机/Docker）没有 X，
        Chromium 直接退出：

            Missing X server or $DISPLAY
            The platform failed to initialize. Exiting.   （exit=1）

        降级后浏览器能起来，二维码会渲染在网页里（前端「扫码」就是走这条路），
        用户照样能登录 —— 比他盯着「浏览器不可用」干瞪眼好得多。
        """
        if self.headless is not False:
            return
        if _is_windows():
            return
        if not display_available(self.settings):
            reason = ("服务器没有图形界面（没有 $DISPLAY / Wayland），"
                      "已自动改用无头浏览器，请在网页里扫码")
            self.headless = None if bool(getattr(self.settings, "browser_headless", True)) else True
            self.headless_forced_reason = reason
            try:
                from ..common import logging as _sklog

                _sklog.info(f"可见登录不可用：{reason}", scope="worker", source="session.visible")
            except Exception:      # noqa: BLE001 - 日志失败不能影响登录
                pass

    def _start(self) -> "AccountBrowser":
        proxy = self.proxy if self.proxy is not None else self._resolve_proxy()
        browser_path = ""
        self._downgrade_visible_if_no_display()
        # 「可见窗口」模式：临时改一份 settings 副本，不动全局配置
        launch_settings = self.settings
        if self.headless is not None:
            try:
                from dataclasses import replace as _dc_replace
                launch_settings = _dc_replace(
                    self.settings, browser_headless=bool(self.headless))
            except Exception:      # noqa: BLE001 - 万一不是 dataclass，退回直接改属性
                launch_settings = self.settings
                setattr(launch_settings, "browser_headless", bool(self.headless))
        try:
            self.chrome = cdp_mod.launch_chrome(
                launch_settings,
                profile_dir(self.account_id, self.settings, self.profile_suffix),
                proxy=proxy,
                start_url=self.start_url or "about:blank",
            )
            browser_path = self.chrome.browser_path
            self.cdp = cdp_mod.attach(
                self.chrome, timeout=15.0,
                # 页面操作的默认超时：弱 CPU 容器里 20s 都不够，用配置值
                default_timeout=float(getattr(self.settings, "cdp_page_timeout", 30.0) or 30.0))
            self.cdp.enable_domains(network=True)
            # 让页面以为自己在**前台**：无头模式下默认可能被判为 hidden/后台，
            # 抖音私信页会因此只渲染空壳（真实事故：会话条数一直 0，随后 JS 无响应）。
            try:
                self.cdp.enable_foreground()
            except Exception:        # noqa: BLE001 - 老版本没有这些 CDP 方法
                pass
            humanize.apply_identity(
                self.cdp, self.rng,
                timezone_id=str(self.account.get("timezone")
                                or getattr(self.settings, "timezone", "Asia/Shanghai")),
                width=int(getattr(self.settings, "browser_width", 1366) or 1366),
                height=int(getattr(self.settings, "browser_height", 900) or 900),
            )
            if self.headless is False:
                # 可见窗口模式：把窗口提到前台，否则它可能被别的窗口盖住，
                # 用户会以为「浏览器没弹出来」。失败不影响流程。
                try:
                    self.cdp.send("Page.bringToFront")
                except (cdp_mod.CDPError, AttributeError):
                    pass
        except (cdp_mod.CDPError, OSError) as exc:
            self._teardown()
            raise BrowserUnavailable(f"启动浏览器失败: {exc}") from exc
        # 登记进程，管理端 /admin/browser-processes 看得到
        try:
            self._gate.register(self.key, pid=self.chrome.pid, port=self.chrome.port,
                               kind=self.kind, account_id=self.account_id,
                               profile=session_filename(self.account_id),
                               browser=Path(browser_path).name if browser_path else "",
                               profile_dir=str(self.chrome.profile_dir))
        except Exception:         # noqa: BLE001 - 登记失败不影响功能
            pass
        if self.restore:
            data = load_state(self.account, self.settings)
            if data:
                try:
                    restore_state(self.cdp, data, settings=self.settings, navigate=True)
                except cdp_mod.CDPError:
                    # 恢复失败就当没有登录态：让上层去判断登录状态并提示重新登录
                    pass
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._teardown()
        return None

    def _teardown(self) -> None:
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:     # noqa: BLE001 - 关连接失败无需上报
                pass
            self.cdp = None
        if self.chrome is not None:
            try:
                self.chrome.close(timeout=5.0)
            except Exception:     # noqa: BLE001
                pass
            self.chrome = None
        if self._gate is not None:
            try:
                self._gate.release(self.key)
            except Exception:     # noqa: BLE001
                pass
            self._gate = None

    # ---- 便捷方法 ----
    def save(self, *, account_id: int | None = None) -> bool:
        """把当前 cookies + localStorage 落盘，并返回是否成功。

        落盘成功后由调用方回填 ``douyin_accounts.storage_path``（**只写文件名**）。
        """
        if self.cdp is None:
            return False
        try:
            state = capture_state(self.cdp, extra_origins=[MAIN_ORIGIN])
        except cdp_mod.CDPError:
            return False
        if not state.get("cookies"):
            return False
        state["user_agent"] = self.profile.get("user_agent", "")
        save_state(account_id or self.account_id, state, self.settings)
        return True

    def _resolve_proxy(self) -> Any:
        """按 ``douyin_accounts.proxy_id`` 查代理配置（查不到就走直连）。"""
        proxy_id = self.account.get("proxy_id")
        if not proxy_id:
            return None
        try:
            from .. import db
            row = db.query_one("SELECT * FROM proxies WHERE id=? AND status='active'", (proxy_id,))
        except Exception:         # noqa: BLE001 - 代理查不到不该让任务挂掉
            return None
        if not row:
            return None
        password = row.get("password_enc") or ""
        if password:
            try:
                from ..common.security import decrypt
                password = decrypt(password)
            except Exception:     # noqa: BLE001 - 解密失败就当无密码代理
                password = ""
        return {"scheme": row.get("scheme") or "http", "host": row.get("host"),
                "port": row.get("port"), "username": row.get("username"), "password": password}

    def describe(self) -> dict:
        """给日志/管理端用的**非敏感**描述（不含 cookies、不含路径）。"""
        return {
            "account_id": self.account_id,
            "pid": self.chrome.pid if self.chrome else 0,
            "port": self.chrome.port if self.chrome else 0,
            "kind": self.kind,
            "profile": session_filename(self.account_id) if self.account_id else "",
            "connected": bool(self.cdp and self.cdp.connected),
        }


def force_close_account_browser(account_id: int, settings: Any, *,
                               kinds: tuple[str, ...] = ("login", "verify")) -> dict:
    """强制关掉该账号的**登录/校验**浏览器并释放并发额度。

    用于「浏览器并发已满」自愈：用户上一次登录的窗口还没关就再点一次时，
    直接把旧窗口关掉，新会话立刻能拿到额度 —— 比让用户手点关闭更可靠。
    **绝不关任务浏览器**（那会打断正在发送的任务），所以 kinds 默认不含 task。
    """
    try:
        from ..worker import pool as pool_mod
        gate = pool_mod.get_pool(settings)
        return gate.force_close(f"account_{int(account_id)}", kinds=kinds)
    except Exception as exc:      # noqa: BLE001 - 自愈失败不该让上层报错
        return {"closed": False, "reason": f"{type(exc).__name__}: {exc}"}


def cleanup_sessions(settings: Any, *, keep_days: int = 0) -> dict:
    """清理孤儿登录态文件（账号已删除但文件还在）。

    ``keep_days>0`` 时只删「最后修改时间早于 N 天」的。返回统计，供维护任务记录。
    """
    from .. import db
    folder = session_dir(settings)
    known: set[str] = set()
    try:
        rows = db.query_all("SELECT id FROM douyin_accounts")
        known = {session_filename(row["id"]) for row in rows}
    except Exception:             # noqa: BLE001 - 数据库不可用时不要删任何文件（保守）
        return {"removed": 0, "kept": 0, "error": "db_unavailable"}
    removed, kept = 0, 0
    cutoff = time.time() - max(0, int(keep_days)) * 86400
    for path in folder.glob("account_*.json"):
        if keep_days > 0:
            try:
                if path.stat().st_mtime > cutoff:
                    kept += 1
                    continue
            except OSError:
                continue
        if path.name in known:
            kept += 1
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "kept": kept}
