"""抖音站点层：URL、登录态判定、会话（好友）抓取、``sync_friends``。

分层说明
--------
* ``selectors.py`` 只放「怎么找元素」（纯选择器 + JS）。
* 本模块放「在抖音上具体怎么做」：打开哪个地址、怎么判断登录成功、
  怎么把会话列表变成好友列表、demo_mode 的假数据长什么样。
* ``task_runner.py`` 放「一次任务的业务流程」（发送、幂等、执行记录）。

为什么把 URL 集中在这里
----------------------
抖音改过私信页地址（前作实测：``/messages`` 会返回「页面不存在」，
``/chat`` 才是现行地址），所以按优先级试多个地址，并把「哪个地址真正可用」
写进返回值，方便排障。

置信度标注
----------
* URL 列表：中（来自前作 ``src/browser.py`` 的实测记录 + 公开资料）。
* 登录态判定信号：中（``data-e2e``/头像选择器来自前作实测；「已扫码待确认」
  文案是公开资料 + 推断，真机首次运行请跑 ``main.py diagnose`` 校准）。
* ``uid`` 提取：低（抖音把 uid 放在内联 JSON 里，字段名各版本不同，取不到就留空，
  **绝不编造**）。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from . import selectors, session_store
from .cdp import CDPError

HOME_URL = "https://www.douyin.com/"

#: **发送续火花必须使用的私信页地址**（唯一权威来源）。
#:
#: 为什么强制这一个：``?isPopup=1`` 才是抖音私信弹层形态，会话列表与输入框
#: 都按这个页面渲染；不带参数或换别的路径时，页面结构/可用性都可能不同。
#: ``task_runner`` 发送前**只** navigate 到这里，并核对 ``current_url()``。
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

#: 单次页面探测（是否存在某个元素）的超时。
#: 曾经写死 2 秒 —— 在 CPU 被限流的容器里，一条简单的 Runtime.evaluate 都可能超过 2s，
#: 于是登录直接报「等待 CDP 响应超时（2s）」（真实事故）。8 秒仍远小于整体等待预算。
PROBE_TIMEOUT = 15.0

#: 私信页候选地址（按优先级）。``CHAT_URL`` 永远第一，其余仅在历史版本中兜底。
#: 注意：真实发送流程走 :func:`open_chat`（只用 ``CHAT_URL``），不使用这个列表。
MESSAGE_URLS: tuple[str, ...] = (
    CHAT_URL,
    "https://www.douyin.com/chat",
    "https://www.douyin.com/messages",
)

USER_URL = "https://www.douyin.com/user/{sec_uid}"

#: 判定「已经登录」的正向信号组（头像/昵称）
LOGIN_INFO_GROUP = "login_user_info"
#: 判定「未登录」的反向信号组
LOGIN_REQUIRED_GROUP = "login_required"

#: 登录成功后抖音一定会下发的会话 cookie 名（单一来源：``session_store``）。
#:
#: 为什么用它而不是 DOM：**扫码确认后页面会刷新，DOM 选择器随版本变**，而
#: ``sessionid`` 这类 cookie 是登录成功的硬标志。
#:
#: ⚠️ 注意别把 ``passport_csrf_token`` / ``passport_auth_mix_state`` 放进来：
#: 实测「打开登录弹层」就会下发这些 passport 辅助 cookie（未登录也有），
#: 会造成「没扫码却报登录成功」的误判。只保留真正的会话类 cookie。
LOGIN_COOKIE_NAMES: tuple[str, ...] = session_store.LOGIN_COOKIE_NAMES

#: 登录进度细分 step（写进 login_sessions.step，前端据此显示「正在启动浏览器…」）
STEP_LAUNCHING = "launching_browser"
STEP_OPENING = "opening_login_page"
STEP_WAITING_QRCODE = "waiting_qrcode"
STEP_QRCODE_READY = "qrcode_ready"
STEP_WAITING_SCAN = "waiting_scan"
STEP_SCANNED = "waiting_confirm"
STEP_CONFIRMED = "saving_session"
STEP_SAVING = "saving_session"
STEP_DONE = "done"
STEP_SENDING_SMS = "sending_sms_code"
STEP_VERIFYING_SMS = "verifying_sms_code"

#: 会话列表项文本 → 好友结构的启发式：抖音每项大约长这样
#: ``昵称\n最后一条消息\n时间``，但各版本顺序/行数不定。只取第一行当昵称，
#: 剩下的拼成 last_message —— 不做过度解析，改版也不容易全错。
MAX_NICKNAME_LEN = 24


# --------------------------------------------------------------------------
# demo_mode：完全假实现（不启浏览器）
# --------------------------------------------------------------------------
#: 演示用的假会话。**只有 demo_mode 才会返回它们**，真实模式永远走页面。
DEMO_NICKNAMES: tuple[str, ...] = ("小明", "小红", "阿强", "老王", "小雨", "阿May")

DEMO_LAST_MESSAGES: tuple[str, ...] = ("火花不能断 🔥", "在吗", "[图片]", "哈哈", "打卡 ✨")


def is_demo(settings: Any) -> bool:
    """是否演示模式（``SK_DEMO_MODE=1`` / ``settings.demo_mode``）。"""
    return bool(getattr(settings, "demo_mode", False))


def demo_conversations(account: dict | None, *, count: int = 3,
                       rng: random.Random | None = None) -> list[dict]:
    """演示模式下的假会话列表（同一个账号结果稳定，方便前端联调复现）。"""
    account_id = int((account or {}).get("id") or 0)
    rng = rng or random.Random(f"demo-conversations-{account_id}")
    names = list(DEMO_NICKNAMES)
    rng.shuffle(names)
    out: list[dict] = []
    for index, nickname in enumerate(names[: max(1, min(count, len(names)))]):
        out.append({
            "nickname": nickname,
            "sec_uid": f"DEMO_SEC_UID_{account_id}_{index + 1}",
            "avatar": "",
            "last_message": rng.choice(DEMO_LAST_MESSAGES),
            "unread": 1 if index == 0 else 0,
            "demo": True,
        })
    return out


# --------------------------------------------------------------------------
# 页面操作
# --------------------------------------------------------------------------
def open_home(cdp: Any, *, timeout: float = 30.0) -> dict:
    """打开抖音首页（登录页/二维码都在首页弹层里）。"""
    cdp.navigate(HOME_URL, timeout=timeout, wait_ready=False)
    return {"ok": True, "url": cdp.current_url()}


#: 点「登录」入口的 JS：优先 ``button`` 标签、要求可见且尺寸像按钮。
#:
#: 实测（2026-10 抖音 PC 版）：未登录时右上角是一个
#: ``button.semi-button.semi-button-primary``，文本恰为「登录」。
#: 旧实现靠选择器组找「login_sms_entry / login_required_text」，实测全部命中 0，
#: 弹层永远打不开 —— 这是「扫码没反应」的另一个根因。
LOGIN_ENTRY_CLICK_JS = """(() => {
  const nodes = [...document.querySelectorAll('button, div, span, a')].filter((el) => {
    const t = (el.innerText || '').trim();
    return t === '登录' || t === '立即登录';
  });
  const visible = [];
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.width > 320 || r.height > 140) continue;   // 排除整块容器
    visible.push({ el, r });
  }
  if (!visible.length) return { ok: false, reason: 'no_login_entry' };
  const picked = visible.find((o) => o.el.tagName === 'BUTTON') || visible[0];
  picked.el.click();
  return {
    ok: true, tag: picked.el.tagName,
    cls: String(picked.el.className || '').slice(0, 60),
    rect: { x: picked.r.x, y: picked.r.y, width: picked.r.width, height: picked.r.height,
            cx: picked.r.left !== undefined ? picked.r.left + picked.r.width / 2 : 0,
            cy: picked.r.top !== undefined ? picked.r.top + picked.r.height / 2 : 0 },
  };
})()"""


def open_login_panel(cdp: Any, *, timeout: float = 20.0) -> dict:
    """确保登录弹层可见（循环重试，直到超时）。

    实测（2026-10 抖音 PC 版）时序：
    * 未登录时右上角有「登录」按钮（``button.semi-button-primary``）；
    * 点击后约 2~6 秒弹出登录弹层（``article#douyin_login_comp_flat_panel``）；
    * 弹层内二维码位置先是抖音 logo 占位 SVG，**约 3 秒后**才换成真二维码图片。

    为什么必须循环重试：首页在加载完之前是**没有**「登录」按钮的（React 还在挂载）。
    旧实现只点一次、点不到就等，于是「页面慢一点 → 弹层永远打不开 → 报『找不到
    二维码』」。实机复现过：同样代码，一次成功、一次 `not_found`，差别只是首页
    渲染快慢。现在每轮都重新探测按钮并点击，直到超时。
    """
    deadline = time.monotonic() + max(6.0, float(timeout))
    clicks = 0
    last_cls = ""
    last_click = 0.0
    while time.monotonic() < deadline:
        # 1) 弹层已经打开？
        if selectors.find_first(cdp, "login_qrcode_container", timeout=PROBE_TIMEOUT):
            via = f"clicked_login_entry×{clicks}" if clicks else "already_visible"
            return {"ok": True, "via": via, "clicks": clicks, "button_class": last_cls}
        # 2) 找「登录」按钮并点击。两次点击之间至少隔 1.5s：
        #    连点有可能把刚弹出的弹层又关掉（抖音的登录按钮是 toggle 语义）。
        now = time.monotonic()
        if now - last_click >= 1.5:
            try:
                result = cdp.evaluate(LOGIN_ENTRY_CLICK_JS)
            except CDPError:
                result = None
            if isinstance(result, dict) and result.get("ok"):
                clicks += 1
                last_cls = str(result.get("cls") or "")
                last_click = now
            else:
                # 按钮还没渲染出来：兜底试旧选择器组（其它版本可能用得上）
                for name in ("login_sms_entry", "login_required_text"):
                    if selectors.click_group(cdp, name, timeout=1.5):
                        clicks += 1
                        last_cls = f"fallback:{name}"
                        last_click = time.monotonic()
                        break
        time.sleep(0.5)
    return {"ok": False, "via": "not_found", "clicks": clicks, "button_class": last_cls}


def is_chat_url(url: Any) -> bool:
    """该地址是不是私信页（``/chat``）。

    历史版本要求必须带 ``isPopup=1``，但实测抖音**经常把 query 丢掉**、只留
    ``https://www.douyin.com/chat`` —— 页面其实是同一个私信页（会话列表照样渲染）。
    真实事故：任务因此报「等待会话列表超时（当前页面：https://www.douyin.com/chat）」。
    所以这里只看路径；是否真的可用由「会话列表能不能找到」决定。
    """
    text = str(url or "")
    if not text:
        return False
    base = text.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return base in ("https://www.douyin.com/chat", "http://www.douyin.com/chat")


_WALL_WORDS = (
    ("captcha", ("请完成安全验证", "拖动滑块", "滑动验证", "验证码", "captcha", "验证你是真人")),
    ("login", ("扫码登录", "登录后可查看", "立即登录", "验证码登录")),
    ("network", ("网络异常", "网络连接", "无法连接", "加载失败", "请重试")),
)


def _foreground(cdp: Any) -> None:
    """尽力让页面处于前台/可见状态（无头模式下默认可能被判后台，导致不渲染）。"""
    try:
        cdp.enable_foreground()
    except Exception:                     # noqa: BLE001 - 老版本/已断开都忽略
        pass


def page_snapshot(cdp: Any, *, timeout: float = 20.0) -> dict:
    """收集当前页面的证据：URL/标题/readyState/正文片段/会话条数/是否有安全验证。

    为什么需要：真实事故里任务只报「等待会话列表超时」，用户完全不知道页面是
    空白、卡在验证码、还是登录失效。把这些写进错误消息，排障时间从小时降到分钟。
    """
    def _js(expr: str, default: Any = "") -> Any:
        try:
            value = cdp.evaluate(expr, timeout=timeout)
        except Exception:                     # noqa: BLE001 - 取证失败不能影响主流程
            return default
        return value if value is not None else default

    snap: dict = {
        "url": _js("location.href"),
        "title": _js("document.title"),
        "ready": _js("document.readyState"),
        "body": str(_js(
            "document.body ? document.body.innerText.slice(0, 800) : ''") or ""),
        "conversations": 0,
        "editors": 0,
    }
    try:
        snap["conversations"] = int(_js(
            "document.querySelectorAll('[data-e2e=\"conversation-item\"]').length", 0) or 0)
    except (TypeError, ValueError):
        snap["conversations"] = 0
    try:
        snap["editors"] = int(_js(
            "document.querySelectorAll('[data-slate-editor=\"true\"]').length", 0) or 0)
    except (TypeError, ValueError):
        snap["editors"] = 0
    snap["wall"] = detect_wall(snap)
    return snap


def detect_wall(snapshot: dict) -> str:
    """从页面快照判断「被什么挡住了」：captcha / login / network / 空页面。"""
    text = f"{snapshot.get('title') or ''}\n{snapshot.get('body') or ''}"
    for kind, words in _WALL_WORDS:
        if any(word in text for word in words):
            return kind
    if not str(snapshot.get("body") or "").strip():
        return "blank"
    return ""


def summarize_snapshot(snapshot: dict) -> str:
    """把快照压成一行可读摘要（放进错误消息）。"""
    body = " ".join(str(snapshot.get("body") or "").split())[:160]
    return (f"标题=«{str(snapshot.get('title') or '')[:40]}» "
            f"ready={snapshot.get('ready') or '?'} "
            f"会话条数={snapshot.get('conversations')} "
            f"输入框={snapshot.get('editors')} "
            f"阻塞={snapshot.get('wall') or '无'} "
            f"正文=«{body}»")


def save_snapshot_evidence(cdp: Any, settings: Any, snapshot: dict, *, tag: str = "chat") -> str:
    """把截图 + 摘要写到 ``data/diagnostics/<tag>_<时间>/``，返回目录（尽力而为）。"""
    try:
        import json as _json
        from ..common import timeutil as _timeutil

        data_dir = getattr(settings, "data_dir", None)
        if not data_dir:
            return ""
        stamp = _timeutil.utc_now_str().replace(":", "").replace("-", "").replace(" ", "_")
        folder = Path(data_dir) / "diagnostics" / f"{tag}_{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page.json").write_text(
            _json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            png = cdp.capture_screenshot(format="png")
            if png:
                import base64 as _b64

                (folder / "page.png").write_bytes(_b64.b64decode(png))
        except Exception:                     # noqa: BLE001 - 截图失败不算错误
            pass
        return str(folder)
    except Exception:                         # noqa: BLE001
        return ""


def open_chat(cdp: Any, *, timeout: float = 30.0, settings: Any = None) -> dict:
    """打开私信页 —— **只**使用 :data:`CHAT_URL`（发送续火花的唯一入口）。

    与旧的 ``open_messages``（会依次试 3 个地址）不同：这里固定导航到
    ``https://www.douyin.com/chat?isPopup=1``，不换地址、不猜兜底。原因：
    发送逻辑依赖该页面的会话列表与输入框结构，换地址等于换页面，宁可
    明确失败（把真实 URL 报出来）也不要悄悄在别的页面上发消息。

    返回 ``{"ok","url","canonical","reason"}``：
    ``canonical=False`` 表示抖音把页面重定向到了别处（多半是登录态失效），
    调用方应把它当作失败/告警处理。
    """
    _foreground(cdp)
    try:
        cdp.navigate(CHAT_URL, timeout=timeout, wait_ready=False)
        _foreground(cdp)
    except CDPError as exc:
        return {"ok": False, "url": "", "canonical": False,
                "reason": f"打开 {CHAT_URL} 失败：{exc}"}

    total = max(20.0, min(90.0, float(timeout) * 2))
    deadline = time.monotonic() + total
    retried_nav = False
    reloaded = False
    js_stalled = False
    while time.monotonic() < deadline:
        try:
            if selectors.find_first(cdp, "conversation_item", timeout=PROBE_TIMEOUT):
                return _chat_result(cdp, "conversation_list")
            if selectors.find_first(cdp, "message_input", timeout=PROBE_TIMEOUT):
                return _chat_result(cdp, "message_input")
        except CDPError:
            # 渲染进程假死：容器 CPU 被限流时常见。重载一次并重新前台化再试。
            js_stalled = True
            if not reloaded:
                reloaded = True
                try:
                    cdp.navigate(CHAT_URL, timeout=timeout, wait_ready=False)
                    _foreground(cdp)
                except CDPError:
                    pass
                time.sleep(2.0)
                continue
            break
        if _page_missing(cdp):
            return {"ok": False, "url": cdp.current_url(), "canonical": False,
                    "reason": f"{CHAT_URL} 打开后显示「页面不存在」"}
        login_gate = _login_wall(cdp)
        if login_gate:
            return {"ok": False, "url": cdp.current_url(), "canonical": False,
                    "reason": login_gate}
        # 情况 A：抖音把 ?isPopup=1 丢了 → 页面还是私信页，但可能没渲染完，补一次导航
        if not retried_nav and not is_chat_url(cdp.current_url()):
            retried_nav = True
            try:
                cdp.navigate(CHAT_URL, timeout=timeout, wait_ready=False)
            except CDPError:
                pass
            time.sleep(1.0)
            continue
        # 情况 B：等了 2/3 预算还没列表 → 整页 reload 一次（SPA 偶发白屏）
        if not reloaded and time.monotonic() > deadline - total / 3:
            reloaded = True
            try:
                cdp.evaluate("location.reload()")
            except CDPError:
                pass
            time.sleep(2.0)
            continue
        time.sleep(0.6)
    snap = page_snapshot(cdp)
    folder = save_snapshot_evidence(cdp, settings, snap, tag="chat_unavailable") if settings else ""
    result = _chat_result(cdp, "")
    result["ok"] = False
    result["reason"] = (f"等待会话列表超时（当前页面：{result['url']}；已补一次导航/重载）\n"
                        f"      页面取证：{summarize_snapshot(snap)}")
    if js_stalled:
        result["reason"] += ("\\n      → 渲染进程无响应（JS 超时）：容器 CPU 被限流或页面太重。"
                              "建议：给容器多分配 CPU/内存，或把 SK_MAX_CONCURRENT_BROWSERS 设为 1")
    if snap.get("wall") in ("captcha", "login"):
        result["reason"] += ("\n      → 抖音要求" +
                             ("安全验证（滑块/验证码）" if snap["wall"] == "captcha"
                              else "重新登录") +
                             "，无头浏览器常见于机房 IP；请换用「导入 Cookie」或换网络")
    if folder:
        result["reason"] += f"\n      证据已保存：{folder}"
    result["snapshot"] = snap
    return result


def _chat_result(cdp: Any, via: str) -> dict:
    url = cdp.current_url()
    return {"ok": True, "url": url, "canonical": is_chat_url(url), "via": via, "reason": ""}


def open_messages(cdp: Any, *, timeout: float = 30.0) -> dict:
    """按优先级打开私信页，返回真正可用的地址。

    保留给「诊断/排障」使用；**发送续火花请用 :func:`open_chat`**（强制 CHAT_URL）。
    """
    last_error = ""
    for url in MESSAGE_URLS:
        try:
            cdp.navigate(url, timeout=timeout, wait_ready=False)
        except CDPError as exc:
            last_error = f"{url} → {exc}"
            continue
        # 等会话列表或输入框出现；出现任何一个就认为页面可用
        deadline = time.monotonic() + min(20.0, timeout)
        while time.monotonic() < deadline:
            if selectors.find_first(cdp, "conversation_item", timeout=10.0):
                return {"ok": True, "url": cdp.current_url(), "reason": ""}
            if selectors.find_first(cdp, "message_input", timeout=10.0):
                return {"ok": True, "url": cdp.current_url(), "reason": ""}
            if _page_missing(cdp):
                last_error = f"{url} → 页面不存在"
                break
            time.sleep(0.6)
        last_error = last_error or f"{url} → 等待会话列表超时"
    return {"ok": False, "url": cdp.current_url(), "reason": last_error or "私信页打不开"}


def _login_wall(cdp: Any) -> str:
    """检测「未登录/登录态失效」导致的私信页不可用，返回原因（空串=没检测到）。

    典型表现：URL 跳到 passport、页面出现「登录」「扫码登录」弹层、或会话列表一直空。
    这时再等下去毫无意义，直接告诉用户重新登录。
    """
    url = str(cdp.current_url() or "")
    if "passport" in url or "/login" in url:
        return f"页面被重定向到登录页（{url}），登录态可能已失效，请重新扫码登录"
    try:
        text = str(cdp.evaluate(
            "document.body ? document.body.innerText.slice(0, 600) : ''") or "")
    except CDPError:
        return ""
    if "扫码登录" in text and "发送消息" not in text:
        return "私信页显示登录弹窗（未登录或登录态失效），请重新扫码登录"
    return ""


def _page_missing(cdp: Any) -> bool:
    """检测抖音的「页面不存在」提示（错误的 URL 会渲染这个）。"""
    try:
        text = str(cdp.evaluate("document.body ? document.body.innerText.slice(0, 200) : ''") or "")
    except CDPError:
        return False
    return any(word in text for word in ("页面不存在", "你访问的页面不见了", "404", "内容不存在"))


def has_login_cookie(cdp: Any) -> bool:
    """是否已经拿到抖音的登录会话 cookie（登录成功的**硬信号**）。

    为什么需要它：扫码并在手机上确认后，抖音会下发 ``sessionid`` 等 cookie，
    同时页面会刷新。刷新期间 DOM 上的头像/昵称可能读不到，只靠选择器判定就会
    一直停在「等待扫码」——用户看到的现象正是「扫了码没反应」。
    实测：未登录时只有 ``UIFID_TEMP/ttwid/__ac_*`` 等匿名 cookie，没有 sessionid。
    """
    try:
        cookies = cdp.get_cookies()          # Network.getAllCookies，含 httpOnly
    except Exception:      # noqa: BLE001 - 这是个「尽力而为」的信号：
        # cookie 读取失败（CDP 断链、替身对象不完整…）不应让登录轮询线程直接死掉，
        # 返回 False 让上层继续用 DOM 信号判定，最坏只是慢一拍。
        return False
    for cookie in cookies or []:
        name = str(cookie.get("name") or "")
        if name in LOGIN_COOKIE_NAMES and str(cookie.get("value") or "").strip():
            return True
    return False


def read_login_state(cdp: Any, *, timeout: float | None = None,
                     use_cookie: bool = True) -> dict:
    """读当前登录状态（多信号，按可信度排序）。

    判定顺序：

    1. **会话 cookie 已下发** → ``logged_in=True``（最强信号；扫码确认后页面
       会刷新，DOM 头像可能读不到，所以不能再只靠选择器）；
    2. 出现「未登录」信号（登录弹层/二维码）→ ``logged_in=False``；
    3. 找到登录后的头像/昵称 → ``logged_in=True``；
    4. 两个都没有 → ``logged_in=False`` 但 ``reason='no_signal'``（页面没加载完）。

    ``use_cookie=False`` 时**跳过 cookie 信号**，只信页面 DOM。用于「手动导入
    Cookie 后的真机校验」：cookie 是我们自己塞进去的，拿它自证登录成功等于没验证，
    必须看抖音页面是否真的认这个会话（登录弹层出现 = 不认）。
    """
    payload = selectors.payload_for(
        LOGIN_INFO_GROUP,
        loginRequired=list(selectors.GROUPS[LOGIN_REQUIRED_GROUP].selectors),
    )
    raw = cdp.call(selectors.read_login_user_js(), payload, timeout=timeout)
    if not isinstance(raw, dict):
        raw = {}
    nickname = str(raw.get("nickname") or "").strip()
    avatar = str(raw.get("avatar") or "").strip()
    logged_out = bool(raw.get("loggedOut"))
    if use_cookie and has_login_cookie(cdp):
        # cookie 优先：即使 DOM 抢读失败（页面正在刷新）也认定登录成功
        reason = "session_cookie"
        logged_in = True
    elif logged_out:
        reason = "login_required_signal"
        logged_in = False
    elif nickname or avatar:
        logged_in = True
        reason = "user_info_visible"
    else:
        logged_in = False
        reason = "no_signal"
    return {
        "logged_in": logged_in,
        "nickname": nickname,
        "avatar": avatar,
        "uid": str(raw.get("uid") or "").strip(),
        "reason": reason,
        "url": str(raw.get("url") or ""),
        "selector": str(raw.get("selector") or ""),
    }


def login_page_state(cdp: Any, *, timeout: float | None = None) -> str:
    """登录页当前处于哪一步（给 ``login.py`` 的状态机用）。

    返回 ``pending``（等扫码）/ ``scanned``（已扫码待确认）/ ``logged_in``
    （已经登录）/ ``unknown``。

    判定顺序（按可信度，全部实测过）：

    1. **会话 cookie 已下发** → ``logged_in``。扫码确认后页面会刷新，
       DOM 读数不可靠，这是唯一稳定的成功信号；
    2. 弹层里出现「扫描成功/请在手机上确认」文案 → ``scanned``；
    3. 找到真二维码图片 / 二维码容器 / 短信登录入口 → ``pending``；
    4. 都没有 → ``unknown``。
    """
    if has_login_cookie(cdp):
        return "logged_in"
    if selectors.text_present(cdp, "login_scan_success_text", timeout=timeout):
        return "scanned"
    if selectors.find_first(cdp, "login_qrcode_image", timeout=timeout):
        return "pending"
    if selectors.find_first(cdp, "login_qrcode_container", timeout=timeout):
        return "pending"
    if selectors.text_present(cdp, "login_sms_entry_text", timeout=timeout):
        return "pending"
    state = read_login_state(cdp, timeout=timeout)
    if state.get("logged_in"):
        return "logged_in"
    return "unknown"


def wait_for_login(cdp: Any, *, timeout: float = 120.0, interval: float = 1.0,
                   on_state: Any = None) -> dict:
    """轮询等待登录完成。

    ``on_state(state)`` 每次状态变化时回调（``login.py`` 用它更新
    ``login_sessions.status/step``，这样前端能显示「已扫码，请在手机上确认」）。
    """
    deadline = time.monotonic() + max(1.0, timeout)
    last = ""
    while time.monotonic() < deadline:
        try:
            state = login_page_state(cdp)
        except CDPError:
            state = "unknown"
        if state != last:
            last = state
            if callable(on_state):
                try:
                    on_state(state)
                except Exception:        # noqa: BLE001 - 回调异常不能打断等待
                    pass
        if state == "logged_in":
            info = read_login_state(cdp)
            return {"ok": True, "state": state, "info": info, "reason": ""}
        time.sleep(interval)
    return {"ok": False, "state": last or "unknown", "info": {}, "reason": "登录超时"}


def capture_login_identity(cdp: Any, account: dict, settings: Any) -> dict:
    """登录成功后抓取身份信息并落盘登录态。

    返回 ``{nickname, avatar, douyin_uid, saved}``；``saved`` 表示登录态是否落盘。
    **不返回 storage_path / cookies**（由调用方按需回填数据库）。
    """
    info = read_login_state(cdp)
    saved = False
    try:
        state = session_store.capture_state(cdp, extra_origins=[session_store.MAIN_ORIGIN])
        if state.get("cookies"):
            session_store.save_state(account.get("id") or 0, state, settings)
            saved = True
    except CDPError:
        saved = False
    return {
        "nickname": info.get("nickname") or "",
        "avatar": info.get("avatar") or "",
        "douyin_uid": info.get("uid") or "",
        "saved": saved,
    }


# --------------------------------------------------------------------------
# 会话列表 → 好友列表
# --------------------------------------------------------------------------
#: 昵称后面紧跟的「火焰数 / 时间 / 未读数」都以「空格 + 数字」开头，
#: 例如真实 innerText：``李森桦 138 18:31 分享[视频]``。
#: 用它在第一个数字段处截断，还原出真实昵称（``李森桦``）。
_NICKNAME_TAIL_RE = re.compile(
    r"\s+(?:\d|前天|昨天|今天|刚刚|在线|分钟前|小时前|\d+分钟前|\d+小时前)"
)


def _clean_nickname(first_line: str) -> str:
    """从会话项第一行里还原昵称（去掉火焰数、时间、未读数）。"""
    text = str(first_line or "").strip()
    match = _NICKNAME_TAIL_RE.search(text)
    if match:
        text = text[: match.start()].strip()
    # 这里**不截断**：超长说明选择器命中了整块容器，调用方会整条丢弃，
    # 截断反而会让「脏数据」看起来像正常昵称。
    return text


def parse_conversations(texts: list[str]) -> list[dict]:
    """把会话项的可见文本解析成结构化数据（纯函数，可单测）。

    真实结构（2026-10 实机）::

        [data-e2e="conversation-item"]
          innerText = "李森桦\n138\n18:31\n分享[视频]"

    但同一个会话会被**多个选择器家族**各抓到一次（``[data-e2e=...]`` /
    ``.conversationConversationItemwrapper`` / 嵌套子元素），文本粒度不同::

        "李森桦 138 18:31 分享[视频]"   ← 整行
        "李森桦 138 18:31"              ← 标题行
        "李森桦"                        ← 昵称 div

    因此必须：**先按「空格+数字」截断出昵称，再按昵称去重**。否则同一个人会出现
    三条记录，批量发送时可能给同一个人发多次（实测抓到 81 条、实际只有 14 个会话）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in texts or []:
        lines = [line.strip() for line in str(raw or "").split("\n") if line.strip()]
        if not lines:
            continue
        nickname = _clean_nickname(lines[0])
        # 第一行明显长于昵称上限 → 说明选择器命中了整块容器（把最后一条消息也读进来了），
        # 这种条目直接丢弃：宁可漏一个好友，也不能拿未知文本去匹配、去发送。
        if not nickname or len(nickname) > MAX_NICKNAME_LEN or nickname in seen:
            continue
        seen.add(nickname)
        out.append({
            "nickname": nickname,
            "sec_uid": "",
            "avatar": "",
            "last_message": " ".join(lines[1:])[:120],
            "unread": 0,
            "demo": False,
        })
    return out


def open_conversation(cdp: Any, nickname: str, *, timeout: float = 15.0) -> dict:
    """在私信页**打开与某个好友的会话**，等输入框就绪。

    这是发送流程里之前缺失的一环：抖音私信页默认不选中任何会话，右侧聊天面板
    （编辑器 / 发送按钮）根本不会渲染。

    会话列表是**虚拟滚动**（实机只渲染 14 行，好友多了就找不到），所以查找顺序是：

    1. 当前视口里找文本包含昵称的会话行 → 真实鼠标点击 → 核对标题；
    2. 找不到就**滚动列表**继续找（最多几屏）；
    3. 还找不到就用**搜索框**搜昵称，再从结果里点开；
    4. 全失败才返回 ``ok=False`` 并说明卡在哪一步（绝不盲发）。

    返回 ``{"ok","nickname","via","header","reason"}``。
    """
    want = str(nickname or "").strip()
    if not want:
        return {"ok": False, "nickname": "", "via": "", "header": "", "reason": "空昵称"}

    # 1) 先看是不是已经在目标会话里（连续发多个好友时省一次点击）
    header = _chat_header(cdp)
    if header and want in header:
        if _editor_ready(cdp, timeout=3.0):
            return {"ok": True, "nickname": want, "via": "already_open",
                    "header": header, "reason": ""}

    deadline = time.monotonic() + max(12.0, float(timeout))
    last_reason = ""

    # 2) 先等会话列表渲染出来。任务里刚 open_chat 完就立刻找，列表可能还是空的
    #    （实测偶发：同一份登录态有时 0.4s 打开、有时列表还没渲染 → 误报找不到）。
    rows = _wait_conversation_rows(cdp, timeout=10.0)
    if rows <= 0:
        return {"ok": False, "nickname": want, "via": "no_rows", "header": _chat_header(cdp),
                "reason": "私信会话列表没有渲染出来（可能页面没加载完或未登录）"}

    # ⚠️ 先滚回顶部：任务流程里 open_chat 之后会 harvest_conversations()，
    # 它为了抓全量把列表滚到了底部 —— 虚拟列表此时只渲染底部那些行，顶部的好友
    # 根本不在 DOM 里，直接找必然失败（实测「明明有七小只却报找不到」）。
    _scroll_conversations_top(cdp)

    # 3) 视口内查找（重试两次）→ 滚动 → 再查找……
    for _round in range(8):
        for _attempt in range(2):
            clicked = _find_and_click_row(cdp, want)
            if clicked:
                confirmed = _confirm_conversation(cdp, want, clicked, timeout=timeout)
                if confirmed.get("ok"):
                    return confirmed
                last_reason = str(confirmed.get("reason") or "")
                break
            time.sleep(0.8)              # 列表可能还在逐条渲染
        if time.monotonic() > deadline:
            break
        try:
            moved = cdp.call(selectors.scroll_list_js(), {
                "items": list(selectors.GROUPS["conversation_item"].selectors),
                "group": selectors.payload_for("conversation_list")["group"],
            })
        except CDPError:
            moved = None
        if not isinstance(moved, dict) or not moved.get("moved"):
            # 已经滚到底：回到顶部再试一次（有些版本列表是逆序/懒渲染）
            _scroll_conversations_top(cdp)
            clicked = _find_and_click_row(cdp, want)
            if clicked:
                confirmed = _confirm_conversation(cdp, want, clicked, timeout=timeout)
                if confirmed.get("ok"):
                    confirmed["via"] = "scrolled_top+" + str(confirmed.get("via") or "")
                    return confirmed
            break

    # 4) 搜索框兜底（虚拟列表滚完还没有，多半是名字被挤到很下面/排序变了）
    searched = _open_by_search(cdp, want, timeout=timeout)
    if searched.get("ok"):
        return searched
    if searched.get("reason"):
        last_reason = str(searched.get("reason"))

    # 5) 搜索可能把列表过滤空了：清掉搜索词后再找一次
    try:
        selectors.set_input_value(cdp, "search_input", "")
    except CDPError:
        pass
    time.sleep(1.0)
    clicked = _find_and_click_row(cdp, want)
    if clicked:
        confirmed = _confirm_conversation(cdp, want, clicked, timeout=timeout)
        if confirmed.get("ok"):
            confirmed["via"] = "retry_after_search+" + str(confirmed.get("via") or "")
            return confirmed
    return {"ok": False, "nickname": want, "via": "not_found", "header": _chat_header(cdp),
            "reason": last_reason or
                      f"会话列表里找不到「{want}」（已滚动列表并尝试搜索）"}


def _scroll_conversations_top(cdp: Any) -> dict:
    """把会话列表滚回顶部（虚拟列表只渲染视口内的行，位置决定能不能找到人）。"""
    try:
        return cdp.call(selectors.scroll_list_top_js(), {
            "items": list(selectors.GROUPS["conversation_item"].selectors),
        }) or {}
    except CDPError:
        return {}


def _wait_conversation_rows(cdp: Any, *, timeout: float = 10.0) -> int:
    """等会话列表渲染出至少一行，返回最后看到的行数。"""
    deadline = time.monotonic() + max(2.0, float(timeout))
    count = 0
    while time.monotonic() < deadline:
        try:
            count = int(cdp.evaluate(
                "document.querySelectorAll('[data-e2e=\"conversation-item\"],"
                " .conversationConversationItemwrapper').length") or 0)
        except CDPError:
            count = 0
        if count > 0:
            return count
        time.sleep(0.5)
    return count


def _find_and_click_row(cdp: Any, want: str) -> dict:
    """在当前渲染出来的会话行里找到目标并**用真实鼠标点开**。找不到返回 ``{}``。"""
    try:
        result = cdp.call(selectors.open_conversation_js(),
                          selectors.payload_for("conversation_item", nickname=want))
    except CDPError:
        return {}
    if not isinstance(result, dict) or not result.get("found"):
        return {}
    rect = result.get("rect") or {}
    if rect:
        # 实机验证：`el.click()` 打不开面板，必须走 CDP 真实鼠标事件
        try:
            selectors.click_at(cdp, rect)
        except CDPError:
            pass
    return result


def _confirm_conversation(cdp: Any, want: str, clicked: dict, *, timeout: float) -> dict:
    """点击后确认：编辑器就绪 + 标题是目标好友（防发错人）。"""
    if not _editor_ready(cdp, timeout=max(4.0, float(timeout) / 2)):
        return {"ok": False, "nickname": want, "via": "clicked_conversation",
                "header": _chat_header(cdp),
                "reason": "点开会话后输入框仍未出现（聊天面板没渲染完或结构改版）"}
    header = _chat_header(cdp)
    if header and want not in header:
        return {"ok": False, "nickname": want, "via": "clicked_conversation",
                "header": header,
                "reason": f"打开的会话是「{header}」，与目标「{want}」不一致，已中止以免发错人"}
    return {"ok": True, "nickname": want, "via": "clicked_conversation",
            "header": header, "reason": ""}


def _open_by_search(cdp: Any, want: str, *, timeout: float = 15.0) -> dict:
    """用私信页搜索框搜昵称，再从结果里点开（虚拟列表兜底）。"""
    hit = selectors.find_first(cdp, "search_input")
    if not hit or not hit.get("rect"):
        return {"ok": False, "reason": ""}
    try:
        selectors.click_at(cdp, hit["rect"])
        time.sleep(0.2)
        selectors.set_input_value(cdp, "search_input", want)
        time.sleep(0.6)
        cdp.press_key("Enter")
    except CDPError as exc:
        return {"ok": False, "reason": f"搜索框操作失败：{exc}"}
    deadline = time.monotonic() + max(4.0, float(timeout) / 2)
    while time.monotonic() < deadline:
        clicked = _find_and_click_row(cdp, want)
        if clicked:
            confirmed = _confirm_conversation(cdp, want, clicked, timeout=timeout)
            if confirmed.get("ok"):
                confirmed["via"] = "searched+" + str(confirmed.get("via") or "")
                return confirmed
        time.sleep(0.8)
    # 搜索没结果：把搜索框清掉，别影响后续
    try:
        selectors.set_input_value(cdp, "search_input", "")
    except CDPError:
        pass
    return {"ok": False, "reason": f"搜索「{want}」也没有匹配的会话"}


def _chat_header(cdp: Any) -> str:
    """读聊天面板顶部标题（读不到返回空串，不算错误）。"""
    try:
        result = cdp.call(selectors.chat_header_js(),
                          {"selectors": list(selectors.GROUPS["chat_header"].selectors)})
    except CDPError:
        return ""
    if isinstance(result, dict) and result.get("found"):
        return str(result.get("text") or "").strip()
    return ""


def _editor_ready(cdp: Any, *, timeout: float = 6.0) -> bool:
    """等输入区（编辑器或输入容器）出现。"""
    deadline = time.monotonic() + max(1.5, float(timeout))
    while time.monotonic() < deadline:
        if selectors.find_first(cdp, "message_input", timeout=4.0):
            return True
        if selectors.find_first(cdp, "chat_editor_area", timeout=PROBE_TIMEOUT):
            return True
        time.sleep(0.5)
    return False


def harvest_conversations(cdp: Any, *, scroll: bool = True, max_scroll: int = 10,
                          limit: int = 200) -> list[dict]:
    """抓取当前私信页的会话列表（必要时滚动加载）。"""
    collected: list[dict] = []
    seen: set[str] = set()
    empty_rounds = 0
    for _round in range(max(1, max_scroll) if scroll else 1):
        texts = selectors.visible_texts(cdp, "conversation_item", limit=limit)
        for item in parse_conversations(texts):
            if item["nickname"] in seen:
                continue
            seen.add(item["nickname"])
            collected.append(item)
        if not scroll:
            break
        before = len(collected)
        moved = cdp.call(selectors.scroll_list_js(), {
            "items": list(selectors.GROUPS["conversation_item"].selectors),
            "group": selectors.payload_for("conversation_list")["group"],
        })
        if not isinstance(moved, dict) or not moved.get("moved"):
            break
        time.sleep(0.8)
        # 连续两屏没有新增会话就认为到底了，别把 10 屏都白滚一遍（省 8 秒）
        if len(collected) == before:
            empty_rounds += 1
            if empty_rounds >= 2:
                break
        else:
            empty_rounds = 0
    # 抓完把列表滚回顶部：否则后面 open_conversation 找顶部好友会找不到。
    _scroll_conversations_top(cdp)
    return collected


def list_conversations(account: dict, settings: Any, *, kind: str = "sync") -> dict:
    """抓取某账号的会话列表。

    返回 ``{items, count, source, message}``；``source`` 为 ``demo`` / ``page`` / ``none``。
    demo_mode 下返回假数据（不启浏览器），本机与 CI 可完整联调。
    """
    if is_demo(settings):
        items = demo_conversations(account)
        return {"items": items, "count": len(items), "source": "demo", "message": ""}
    if not session_store.is_session_valid(account, settings):
        return {"items": [], "count": 0, "source": "none",
                "message": "登录态文件不存在或已过期，请重新登录该账号"}
    try:
        with session_store.AccountBrowser(account, settings, kind=kind) as browser:
            state = read_login_state(browser.cdp)
            if not state.get("logged_in"):
                return {"items": [], "count": 0, "source": "none",
                        "message": "抖音页面显示未登录（" + str(state.get("reason")) + "），请重新登录"}
            opened = open_messages(browser.cdp)
            if not opened.get("ok"):
                return {"items": [], "count": 0, "source": "none",
                        "message": "打不开私信页：" + str(opened.get("reason"))}
            items = harvest_conversations(browser.cdp)
            if items:
                browser.save()          # 顺手刷新登录态时间戳
            return {"items": items, "count": len(items), "source": "page", "message": ""}
    except session_store.BrowserBusy as exc:
        return {"items": [], "count": 0, "source": "none", "message": f"浏览器并发已满：{exc}"}
    except session_store.BrowserUnavailable as exc:
        return {"items": [], "count": 0, "source": "none", "message": f"浏览器不可用：{exc}"}
    except CDPError as exc:
        return {"items": [], "count": 0, "source": "none", "message": f"页面操作失败：{exc}"}


def sync_friends(account: dict, settings: Any) -> dict:
    """``/friends/sync`` 的自动化入口（契约签名，队友 A 直接调用）。

    返回 ``{"friends": [{"nickname","sec_uid","avatar"}], "count": int, "message": ""}``。

    **不抛业务异常**：拿不到时返回空列表 + ``message`` 说明原因（「没浏览器」/
    「账号离线」/「页面结构变了」三种原因接口层要给出不同提示，message 比异常类型
    更好传递）。只有编程错误（参数类型错）才会抛。
    """
    result = list_conversations(account, settings, kind="sync")
    friends = [{"nickname": item.get("nickname", ""),
                "sec_uid": item.get("sec_uid", ""),
                "avatar": item.get("avatar", "")}
               for item in result.get("items", []) if item.get("nickname")]
    return {"friends": friends, "count": len(friends), "message": result.get("message", "")}


def check_account(account: dict, settings: Any, *, timeout: float = 40.0) -> dict:
    """检测账号登录态（``/douyin/accounts/<id>/check`` 与任务前置校验用）。

    分两层：先看登录态文件（快、不启浏览器），demo_mode 直接按数据库状态返回。
    真实模式再开浏览器确认一次，并把结果回填 ``douyin_accounts``。

    返回 ``{valid, status, nickname, avatar, douyin_uid, reason, message}``
    —— **绝不含 storage_path / cookies**。
    """
    from .. import db                              # 局部导入：避免模块级依赖数据库
    from ..common import timeutil

    account_id = int((account or {}).get("id") or 0)
    now = timeutil.utc_now_str()
    if is_demo(settings):
        valid = str((account or {}).get("status") or "offline") == "online"
        return {
            "valid": valid,
            "status": "online" if valid else "offline",
            "nickname": (account or {}).get("nickname") or "演示账号",
            "avatar": (account or {}).get("avatar") or "",
            "douyin_uid": (account or {}).get("douyin_uid") or f"demo_{account_id}",
            "reason": "demo_mode" if valid else "demo_mode_offline",
            "message": "演示模式：未启动浏览器，按账号状态返回" if valid
                       else "演示模式：账号未设为在线",
        }
    if not session_store.is_session_valid(account, settings):
        if account_id:
            db.update("douyin_accounts",
                      {"status": "expired", "last_check_at": now, "updated_at": now,
                       "last_error": "登录态文件缺失或已过期"},
                      "id=?", (account_id,))
        return {"valid": False, "status": "expired", "nickname": "", "avatar": "",
                "douyin_uid": "", "reason": "session_file_invalid",
                "message": "登录态文件不存在或已过期，请重新登录"}
    try:
        with session_store.AccountBrowser(account, settings, kind="check") as browser:
            state = read_login_state(browser.cdp, timeout=timeout)
            if state.get("logged_in"):
                browser.save()
                if account_id:
                    db.update("douyin_accounts", {
                        "status": "online",
                        "nickname": state.get("nickname") or (account or {}).get("nickname") or "",
                        "avatar": state.get("avatar") or "",
                        "douyin_uid": state.get("uid") or (account or {}).get("douyin_uid") or "",
                        "last_check_at": now, "updated_at": now, "last_error": "",
                        "storage_updated_at": now,
                    }, "id=?", (account_id,))
                return {"valid": True, "status": "online",
                        "nickname": state.get("nickname") or "", "avatar": state.get("avatar") or "",
                        "douyin_uid": state.get("uid") or "", "reason": state.get("reason", ""),
                        "message": "登录态有效"}
            status = "need_verify" if state.get("reason") == "login_required_signal" else "offline"
            if account_id:
                db.update("douyin_accounts",
                          {"status": status, "last_check_at": now, "updated_at": now,
                           "last_error": str(state.get("reason") or "")[:200]},
                          "id=?", (account_id,))
            return {"valid": False, "status": status, "nickname": "", "avatar": "",
                    "douyin_uid": "", "reason": state.get("reason", ""),
                    "message": "抖音页面显示未登录，请重新扫码"}
    except session_store.BrowserBusy as exc:
        return {"valid": True, "status": str((account or {}).get("status") or "online"),
                "nickname": "", "avatar": "", "douyin_uid": "",
                "reason": "busy", "message": f"浏览器忙，未做深度检测：{exc}"}
    except (session_store.BrowserUnavailable, CDPError) as exc:
        return {"valid": True, "status": str((account or {}).get("status") or "online"),
                "nickname": "", "avatar": "", "douyin_uid": "",
                "reason": "browser_unavailable",
                "message": f"浏览器不可用，仅按登录态文件判断：{exc}"}
