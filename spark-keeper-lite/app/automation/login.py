"""抖音登录：扫码登录 + 短信登录（**非阻塞**接口 + 后台 daemon 线程推进）。

跨模块契约（队友 A 直接调用，签名冻结）
---------------------------------------
* ``start_qrcode_login(account, settings) -> dict``
* ``start_sms_login(phone, account, settings) -> dict``
* ``submit_sms_code(session_id, code) -> dict``
* ``poll(session_id) -> dict``   ← **只读数据库**（外加把过期会话标成 expired）
* ``cancel(session_id) -> bool``
* ``refresh_qrcode(session_id) -> dict``

**启动类函数必须立即返回**：它们只做三件事——写 ``login_sessions`` 行、起 daemon
线程、返回。浏览器启动/截图/等待扫码全在后台线程里做，前端靠 ``poll()`` 轮询
（真实模式首包 ``qrcode_image`` 可能是空串，因为截图要等浏览器起来；demo 模式
首包就有图）。

状态机（写进 ``login_sessions.status`` / ``step``）
---------------------------------------------------
``pending`` →（已扫码）``scanned`` →（已确认）``confirmed`` → ``success``
异常分支：``failed`` / ``canceled`` / ``expired``。``step`` 细分进度让前端能显示
「正在启动浏览器…」「已扫码，请在手机上确认」，而不是只有一个转圈。

demo_mode
---------
``settings.demo_mode=True``（``SK_DEMO_MODE=1``）时**完全不启动浏览器**：用纯
标准库生成一张「看起来像二维码」的 PNG 占位图，按固定节奏把状态推进到 success，
并写入一份假的登录态文件 —— 这样本机/CI 能端到端验证「扫码 → 轮询 → 成功」的
前端交互，也能让 ``/douyin/accounts/<id>/check`` 返回 valid。
真实路径与演示路径在代码里是**两条清晰分离的分支**（``_demo_*`` / ``_real_*``）。

未在本机验证的部分
------------------
真实分支的所有页面交互（登录弹层、二维码定位截图、短信表单填写、登录成功判定）
只能在部署机验证；本机通过 demo 分支 + 纯函数单测覆盖状态机与数据落库。
"""

from __future__ import annotations

import base64
import hashlib
import random
import secrets
import struct
import threading
import time
import zlib
from datetime import timedelta
from typing import Any

from .. import db
from ..common import logging as sklog
from ..common import timeutil
from . import cdp as cdp_mod
from . import douyin, selectors, session_store

#: 登录会话总时长（秒）。扫码登录超过这个时间没结果就标记 expired。
LOGIN_TIMEOUT_SECONDS = 180
#: 等二维码出现的最长时间（真实模式）。
#: 实测时序：页面加载约 4s + 点「登录」约 1s + 弹层渲染约 2~6s + 二维码替换占位图约 3s，
#: 慢机上会接近 20s；给 30s 太紧（一到超时就报「没找到二维码」），改 60s。
QRCODE_WAIT_SECONDS = 60.0
#: 轮询登录状态的间隔
POLL_INTERVAL_SECONDS = 1.2
#: demo 模式每一步的默认间隔（秒），测试里会调小
DEMO_STEP_SECONDS = 1.0
#: session_id 前缀（契约里的 ``ls_xxx``）
SESSION_PREFIX = "ls_"

_state_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}
_stop_flags: dict[str, threading.Event] = {}
_pending_codes: dict[str, str] = {}
_refresh_flags: dict[str, bool] = {}
#: 该登录会话是否用了「可见窗口」模式（刷新二维码时要保持）
_visible_flags: dict[str, bool] = {}


# --------------------------------------------------------------------------
# 占位二维码（纯标准库生成 PNG）
# --------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack("!I", len(data)) + tag + data
            + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def placeholder_qr_png(seed: str, modules: int = 29, scale: int = 6) -> bytes:
    """生成一张「长得像二维码」的占位 PNG（demo 模式专用）。

    为什么自己画：项目零第三方依赖，没有二维码库；而演示模式需要一张**真的能
    在浏览器里显示的图片**（不是坏图占位符），否则前端联调看不出版式问题。

    画法：29×29 的模块矩阵，四角画定位方块（像真二维码），其余模块由
    ``seed`` 的哈希决定（同一个 session 结果稳定），最后按 ``scale`` 放大。
    """
    rng = random.Random(hashlib.sha256(str(seed).encode("utf-8")).hexdigest())
    grid = [[0] * modules for _ in range(modules)]
    for row in range(modules):
        for col in range(modules):
            grid[row][col] = 1 if rng.random() < 0.45 else 0

    def finder(top: int, left: int) -> None:
        for dr in range(7):
            for dc in range(7):
                row, col = top + dr, left + dc
                if not (0 <= row < modules and 0 <= col < modules):
                    continue
                edge = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                grid[row][col] = 1 if (edge or core) else 0

    finder(0, 0)
    finder(0, modules - 7)
    finder(modules - 7, 0)

    size = modules * scale
    expanded: list[bytes] = []
    for row in range(modules):
        line = bytearray()
        for col in range(modules):
            pixel = b"\x00\x00\x00" if grid[row][col] else b"\xff\xff\xff"
            line.extend(pixel * scale)          # 横向放大
        row_bytes = b"\x00" + bytes(line)       # 每行开头是 PNG 的 filter byte
        expanded.extend([row_bytes] * scale)    # 纵向放大
    raw = b"".join(expanded)
    header = struct.pack("!IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(raw, 6)) + _png_chunk(b"IEND", b""))


def png_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _ttl(settings: Any) -> int:
    """登录会话时长（秒）。

    ``0`` 是合法值（用于测试/强制立即超时），所以**不能**写
    ``getattr(...) or LOGIN_TIMEOUT_SECONDS`` —— 那会把 0 变成 180。
    """
    raw = getattr(settings, "login_timeout_seconds", LOGIN_TIMEOUT_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = LOGIN_TIMEOUT_SECONDS
    return max(0, value)


def _wait_seconds(settings: Any, name: str, default: float) -> float:
    """同类语义的浮点配置读取（0 也表示「不等」）。"""
    raw = getattr(settings, name, default)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------
# 会话行读写
# --------------------------------------------------------------------------
def new_session_id() -> str:
    return SESSION_PREFIX + secrets.token_hex(8)


def fetch_session(session_id: str) -> dict | None:
    return db.query_one("SELECT * FROM login_sessions WHERE session_id=?", (str(session_id),))


def update_session(session_id: str, **fields: Any) -> None:
    """更新登录会话行（``updated_at`` 自动带上）。"""
    if not fields:
        return
    data = {key: value for key, value in fields.items() if value is not None}
    data["updated_at"] = timeutil.utc_now_str()
    try:
        db.update("login_sessions", data, "session_id=?", (str(session_id),))
    except Exception as exc:      # noqa: BLE001 - 状态写失败会让前端一直转圈，必须记日志
        sklog.error(f"更新登录会话失败 session={session_id}: {exc}",
                    scope="user", source="login.session")


def create_session(session_id: str, account: dict, method: str, *,
                   ttl: int = LOGIN_TIMEOUT_SECONDS, phone: str = "",
                   step: str = "pending", qrcode_image: str = "",
                   qrcode_data: str = "", message: str = "") -> dict:
    """创建 ``login_sessions`` 行（**必须在 start_* 返回之前完成**）。"""
    now = timeutil.utc_now_str()
    db.insert("login_sessions", {
        "session_id": session_id,
        "user_id": int(account.get("user_id") or 0),
        "account_id": int(account.get("id") or 0) or None,
        "method": "qrcode" if method == "qrcode" else "sms",
        "status": "pending",
        "qrcode_data": qrcode_data or None,
        "qrcode_image": qrcode_image or None,
        "phone": phone or None,
        "message": message[:500],
        "step": step[:50],
        "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=ttl)),
        "finished_at": None,
        "created_at": now,
        "updated_at": now,
    })
    return fetch_session(session_id) or {}


def _expire_if_needed(session: dict) -> dict:
    """``poll()`` 里把过期会话落到 expired（读接口允许做这一次状态修正）。"""
    status = str(session.get("status") or "")
    if status in ("pending", "scanned", "confirmed") and \
            timeutil.is_expired(session.get("expire_at")):
        update_session(session["session_id"], status="expired", step="expired",
                       message="登录超时，请重新发起", finished_at=timeutil.utc_now_str())
        session = dict(session)
        session["status"] = "expired"
        session["step"] = "expired"
        session["message"] = "登录超时，请重新发起"
    return session


def poll(session_id: str) -> dict:
    """查询登录状态（**只读数据库**，前端每 1~2 秒调一次）。

    返回里带 ``account`` 子对象，字段只有 id/nickname/douyin_uid/avatar/status
    —— **绝不含 storage_path / cookies**（隐私红线）。
    """
    session = fetch_session(session_id)
    if not session:
        return {"session_id": str(session_id), "status": "failed", "step": "not_found",
                "message": "登录会话不存在或已被清理", "found": False}
    session = _expire_if_needed(session)
    account_info: dict | None = None
    account_id = session.get("account_id")
    if account_id:
        row = db.query_one(
            "SELECT id, nickname, douyin_uid, avatar, status FROM douyin_accounts WHERE id=?",
            (int(account_id),),
        )
        if row:
            account_info = {
                "id": row["id"], "nickname": row.get("nickname") or "",
                "douyin_uid": row.get("douyin_uid") or "",
                "avatar": row.get("avatar") or "", "status": row.get("status") or "",
            }
    return {
        "found": True,
        "session_id": session["session_id"],
        "method": session.get("method") or "qrcode",
        "status": session.get("status") or "pending",
        "step": session.get("step") or "",
        "message": session.get("message") or "",
        "qrcode_image": session.get("qrcode_image") or "",
        "qrcode_data": session.get("qrcode_data") or "",
        "phone": session.get("phone") or "",
        "expire_at": session.get("expire_at") or "",
        "finished_at": session.get("finished_at") or "",
        "account": account_info,
    }


# --------------------------------------------------------------------------
# 线程登记
# --------------------------------------------------------------------------
def _register(session_id: str, target: Any, *args: Any) -> threading.Thread:
    stop = threading.Event()
    with _state_lock:
        _stop_flags[session_id] = stop
        _prune_locked()
    thread = threading.Thread(target=target, args=(session_id, stop, *args),
                              name=f"sk-login-{session_id[-6:]}", daemon=True)
    with _state_lock:
        _threads[session_id] = thread
    thread.start()
    return thread


def _prune_locked() -> None:
    """清掉已经结束很久的会话登记（内存不涨）。"""
    for session_id in [sid for sid, thread in _threads.items() if not thread.is_alive()]:
        _threads.pop(session_id, None)
        _stop_flags.pop(session_id, None)
        _refresh_flags.pop(session_id, None)
        _visible_flags.pop(session_id, None)
        _pending_codes.pop(session_id, None)


def _thread_alive(session_id: str) -> bool:
    with _state_lock:
        thread = _threads.get(session_id)
    return bool(thread and thread.is_alive())


def _sleep(seconds: float, stop: threading.Event) -> bool:
    """可被取消打断的 sleep。返回 ``False`` 表示被取消了。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if stop.is_set():
            return False
        time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
    return not stop.is_set()


def _take_refresh(session_id: str) -> bool:
    with _state_lock:
        flag = bool(_refresh_flags.pop(session_id, False))
    return flag


def _steal_code(session_id: str) -> str:
    with _state_lock:
        return _pending_codes.pop(session_id, "")


# --------------------------------------------------------------------------
# 二维码登录
# --------------------------------------------------------------------------
def start_qrcode_login(account: dict, settings: Any, *, visible: bool = False) -> dict:
    """发起扫码登录（立即返回）。

    ``visible=True`` 时开一个**真实可见**的 Chrome 窗口：二维码弹层由抖音自己
    渲染在窗口里，用户直接在窗口里扫码（或改用密码登录），后端只负责等
    ``sessionid`` 出现并保存登录态。这是「网页里点一下就完成登录」的兜底路径，
    也是无头模式下二维码一直不出来时最可靠的方案。

    返回 ``{"session_id","method","status","step","expire_at","qrcode_image",
    "qrcode_data","message"}``；``login_sessions`` 行在**返回前**已写好。
    """
    account = dict(account or {})
    # 同一账号只能有一个浏览器：先把上一轮没结束的登录会话取消，否则新会话会撞
    # 上「浏览器并发已满」（用户看到的就是这句话）。
    cancel_account_sessions(int(account.get("id") or 0), reason="发起新的扫码登录")
    session_id = new_session_id()
    ttl = _ttl(settings)
    image = ""
    data_uri = ""
    if douyin.is_demo(settings):
        # 演示模式：同步生成占位二维码，首包就有图（前端不必等）
        image = png_data_url(placeholder_qr_png(session_id))
        data_uri = f"demo://qrcode/{session_id}"
    visible_mode = bool(visible) and not douyin.is_demo(settings)
    # 云主机 / 容器里没有图形界面：开可见窗口必然失败
    #   Missing X server or $DISPLAY / The platform failed to initialize
    # 与其让用户看到「浏览器不可用」，不如自动降级成无头 + 在网页里扫码。
    visible_reason = ""
    if visible_mode and not session_store.display_available(settings):
        visible_mode = False
        visible_reason = ("服务器没有图形界面，无法弹出浏览器窗口（已自动改用网页扫码，"
                          "二维码会显示在本页）")
    if visible_reason:
        session = create_session(session_id, account, "qrcode", ttl=ttl,
                                 step=douyin.STEP_LAUNCHING if not image else douyin.STEP_WAITING_SCAN,
                                 qrcode_image=image, qrcode_data=data_uri,
                                 message=visible_reason)
    else:
        session = create_session(session_id, account, "qrcode", ttl=ttl,
                                 step=douyin.STEP_LAUNCHING if not image else douyin.STEP_WAITING_SCAN,
                                 qrcode_image=image, qrcode_data=data_uri,
                                 message=("正在打开浏览器窗口，请在弹出的窗口里登录抖音…"
                                          if visible_mode else
                                          ("正在准备二维码…" if not image else "请用抖音 App 扫码")))
    if visible_mode:
        with _state_lock:
            _visible_flags[session_id] = True
    if douyin.is_demo(settings):
        _register(session_id, _demo_qrcode_worker, account, settings)
    elif visible_mode:
        _register(session_id, _real_qrcode_worker, account, settings, True)
    else:
        _register(session_id, _real_qrcode_worker, account, settings)
    sklog.info(f"发起扫码登录：session={session_id} account={account.get('id')} "
               f"demo={douyin.is_demo(settings)} visible={visible_mode}",
               scope="user", source="login.qrcode", user_id=account.get("user_id"))
    return _start_payload(session)


def _start_payload(session: dict) -> dict:
    return {
        "session_id": session.get("session_id", ""),
        #: 实际是否开了可见窗口（服务器无图形界面时会降级为 False）
        "visible": bool(_visible_flags.get(session.get("session_id", ""))),
        "method": session.get("method", "qrcode"),
        "status": session.get("status", "pending"),
        "step": session.get("step", ""),
        "expire_at": session.get("expire_at", ""),
        "qrcode_image": session.get("qrcode_image") or "",
        "qrcode_data": session.get("qrcode_data") or "",
        "message": session.get("message") or "",
    }


def refresh_qrcode(session_id: str) -> dict:
    """重新生成二维码（老的二维码过期时前端调它）。"""
    session = fetch_session(session_id)
    if not session:
        return {"ok": False, "session_id": str(session_id), "status": "failed",
                "message": "登录会话不存在", "qrcode_image": ""}
    status = str(session.get("status") or "")
    if str(session.get("method")) != "qrcode":
        return {"ok": False, "session_id": session_id, "status": status,
                "message": "短信登录不支持刷新二维码", "qrcode_image": ""}
    if status in ("success", "canceled", "failed"):
        return {"ok": False, "session_id": session_id, "status": status,
                "message": "登录会话已结束，请重新发起登录",
                "qrcode_image": session.get("qrcode_image") or ""}
    ttl = _session_ttl(session)
    with _state_lock:
        _refresh_flags[session_id] = True
    alive = _thread_alive(session_id)
    expire_at = timeutil.to_db(timeutil.now_utc() + timedelta(seconds=ttl))
    update_session(session_id, status="pending", step=douyin.STEP_WAITING_QRCODE,
                   message="正在刷新二维码…", qrcode_data=None, expire_at=expire_at)
    if not alive:
        # 线程已经结束（比如上一轮超时）：重新起一个，否则会永远停在「刷新中」。
        # demo 模式必须起 demo 线程，否则本机会因为「没有浏览器」直接失败。
        from ..config import get_settings
        settings = get_settings()
        account = db.query_one("SELECT * FROM douyin_accounts WHERE id=?",
                               (session.get("account_id"),)) or {}
        target = _demo_qrcode_worker if douyin.is_demo(settings) else _real_qrcode_worker
        with _state_lock:
            still_visible = bool(_visible_flags.get(session_id))
        if (not douyin.is_demo(settings)) and still_visible:
            _register(session_id, target, account, settings, True)
        else:
            _register(session_id, target, account, settings)
    sklog.info(f"刷新二维码：session={session_id}", scope="user", source="login.qrcode")
    return {"ok": True, "session_id": session_id, "status": "pending",
            "step": douyin.STEP_WAITING_QRCODE, "message": "正在刷新二维码…",
            "qrcode_image": session.get("qrcode_image") or "",
            "expire_at": expire_at}


def _session_ttl(session: dict) -> int:
    """从会话行反推 TTL（``refresh_qrcode`` 的签名里没有 settings）。"""
    created = timeutil.parse_db(session.get("created_at"))
    expire = timeutil.parse_db(session.get("expire_at"))
    if created and expire:
        seconds = int((expire - created).total_seconds())
        if seconds > 0:
            return seconds
    return LOGIN_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# 短信登录
# --------------------------------------------------------------------------
def start_sms_login(phone: str, account: dict, settings: Any) -> dict:
    """发起短信登录（立即返回；后台线程去填手机号并点「发送验证码」）。"""
    account = dict(account or {})
    cancel_account_sessions(int(account.get("id") or 0), reason="发起新的短信登录")
    phone = str(phone or "").strip()
    session_id = new_session_id()
    ttl = _ttl(settings)
    session = create_session(session_id, account, "sms", ttl=ttl, phone=phone,
                             step=douyin.STEP_LAUNCHING, message="正在发送短信验证码…")
    if douyin.is_demo(settings):
        _register(session_id, _demo_sms_worker, account, settings, phone)
    else:
        _register(session_id, _real_sms_worker, account, settings, phone)
    sklog.info(f"发起短信登录：session={session_id} account={account.get('id')} "
               f"phone={phone[:3]}****{phone[-2:] if len(phone) > 4 else ''}",
               scope="user", source="login.sms", user_id=account.get("user_id"))
    return _start_payload(session)


def submit_sms_code(session_id: str, code: str) -> dict:
    """提交短信验证码（交接给后台线程去页面里输入）。

    验证码**不落库**（``login_sessions`` 没有这个字段，也不该有）：放在进程内的
    一次性交接槽里，由后台线程取走。若线程已经结束，返回 ``accepted=False``。
    """
    code = str(code or "").strip()
    session = fetch_session(session_id)
    if not session:
        return {"accepted": False, "ok": False, "session_id": str(session_id),
                "status": "failed", "message": "登录会话不存在"}
    status = str(session.get("status") or "")
    if str(session.get("method")) != "sms":
        return {"accepted": False, "ok": False, "session_id": session_id, "status": status,
                "message": "该会话不是短信登录"}
    if status in ("success", "canceled", "failed", "expired"):
        return {"accepted": False, "ok": False, "session_id": session_id, "status": status,
                "message": "登录会话已结束，请重新发起"}
    if not code or not code.isdigit():
        return {"accepted": False, "ok": False, "session_id": session_id, "status": status,
                "message": "验证码格式不正确"}
    with _state_lock:
        _pending_codes[session_id] = code
    update_session(session_id, step=douyin.STEP_VERIFYING_SMS, message="正在校验验证码…")
    return {"accepted": True, "ok": True, "session_id": session_id, "status": status,
            "message": "验证码已提交"}


def cancel_account_sessions(account_id: int, *, keep: str = "",
                            reason: str = "已被新的登录会话取代") -> int:
    """取消该账号**所有进行中的登录会话**，返回取消条数。

    为什么必须有它：一个账号同时只能有一个浏览器（并发闸门按 ``account_N`` 计数）。
    用户上一次扫码/窗口登录没结束时又点一次，新会话就会撞上「浏览器并发已满」。
    这里先点名取消旧会话：置 stop 标记（工作线程下一轮轮询就会退出并释放浏览器），
    新会话再带 ``acquire_timeout`` 等它让位，就不会出现「已满，请稍后重试」。
    """
    try:
        rows = db.query_all(
            "SELECT session_id FROM login_sessions WHERE account_id=?"
            " AND status IN ('pending','scanned','confirmed')", (int(account_id),))
    except Exception:      # noqa: BLE001 - 数据库异常不该让「发起登录」整体失败
        return 0
    canceled = 0
    for row in rows or []:
        session_id = str(row.get("session_id") or "")
        if not session_id or session_id == str(keep or ""):
            continue
        try:
            if cancel(session_id):
                canceled += 1
        except Exception:      # noqa: BLE001
            continue
    if canceled:
        sklog.info(f"已取消账号 {account_id} 的 {canceled} 个旧登录会话（{reason}）",
                   scope="user", source="login.session")
    return canceled


def cancel(session_id: str) -> bool:
    """取消登录（返回 ``True`` 表示确实从进行中状态取消了）。"""
    session = fetch_session(session_id)
    if not session:
        return False
    status = str(session.get("status") or "")
    if status in ("success", "failed", "canceled", "expired"):
        return False
    with _state_lock:
        stop = _stop_flags.get(session_id)
    if stop is not None:
        stop.set()
    update_session(session_id, status="canceled", step="canceled", message="已取消登录",
                   finished_at=timeutil.utc_now_str())
    sklog.info(f"取消登录：session={session_id}", scope="user", source="login.session")
    return True


# --------------------------------------------------------------------------
# demo 模式的后台流程（不启浏览器）
# --------------------------------------------------------------------------
def _demo_step(settings: Any) -> float:
    return max(0.0, float(getattr(settings, "demo_step_seconds", DEMO_STEP_SECONDS)
                          or DEMO_STEP_SECONDS))


def _demo_qrcode_worker(session_id: str, stop: threading.Event, account: dict,
                        settings: Any) -> None:
    """演示模式扫码流程：pending → scanned → confirmed → success。"""
    step = _demo_step(settings)
    # ① 等二维码（演示模式图片已就绪，这一步只是让状态流转更接近真实节奏）
    if not _sleep(step, stop):
        return _demo_canceled(session_id)
    update_session(session_id, step=douyin.STEP_WAITING_SCAN, message="请用抖音 App 扫码")
    # ② 等待扫码
    if not _sleep(step * 1.5, stop):
        return _demo_canceled(session_id)
    update_session(session_id, status="scanned", step=douyin.STEP_SCANNED,
                   message="已扫码，请在手机上确认")
    # ③ 等待确认
    if not _sleep(step * 1.5, stop):
        return _demo_canceled(session_id)
    update_session(session_id, status="confirmed", step=douyin.STEP_SAVING,
                   message="登录成功，正在保存登录态…")
    if not _sleep(step, stop):
        return _demo_canceled(session_id)
    # ④ 保存登录态并回填账号
    identity = {"nickname": account.get("nickname") or "演示账号",
                "avatar": account.get("avatar") or "",
                "douyin_uid": account.get("douyin_uid") or f"demo_{account.get('id')}"}
    _finish_success(session_id, account, settings, identity, demo=True)


def _demo_sms_worker(session_id: str, stop: threading.Event, account: dict,
                     settings: Any, phone: str) -> None:
    """演示模式短信流程：等验证码 →（收到后）success。"""
    step = _demo_step(settings)
    if not _sleep(step, stop):
        return _demo_canceled(session_id)
    update_session(session_id, step=douyin.STEP_VERIFYING_SMS,
                   message=f"验证码已发送至 {phone}（演示模式：任意 6 位数字均可）")
    ttl = _ttl(settings)
    deadline = time.monotonic() + ttl
    code = ""
    while time.monotonic() < deadline:
        code = _steal_code(session_id)
        if code:
            break
        if stop.is_set():
            return _demo_canceled(session_id)
        time.sleep(0.1)
    if not code:
        update_session(session_id, status="expired", step="expired",
                       message="验证码超时，请重新发起登录",
                       finished_at=timeutil.utc_now_str())
        return
    update_session(session_id, status="confirmed", step=douyin.STEP_SAVING,
                   message="验证码正确，正在保存登录态…")
    if not _sleep(step, stop):
        return _demo_canceled(session_id)
    identity = {"nickname": account.get("nickname") or f"演示账号{phone[-4:]}",
                "avatar": "", "douyin_uid": f"demo_{account.get('id')}"}
    _finish_success(session_id, account, settings, identity, demo=True)


def _demo_canceled(session_id: str) -> None:
    session = fetch_session(session_id) or {}
    if str(session.get("status")) == "canceled":
        return
    update_session(session_id, status="canceled", step="canceled", message="已取消登录",
                   finished_at=timeutil.utc_now_str())


# --------------------------------------------------------------------------
# 真实浏览器流程
# --------------------------------------------------------------------------
def _real_qrcode_worker(session_id: str, stop: threading.Event, account: dict,
                        settings: Any, visible: bool | None = None) -> None:
    """真实扫码流程。

    ``visible=True`` 时开**可见窗口**（用户自己扫码/密码登录），此时：
    * 不需要我们截图二维码（窗口里看得见），但仍然照常抓图给网页显示；
    * 等待登录的时间放宽（人在操作，反应慢一点很正常）。
    """
    ttl = _ttl(settings)
    headless = False if visible else None
    account_id = int(account.get("id") or 0)
    try:
        browser_cm = session_store.AccountBrowser(
            account, settings, kind="login", restore=False, headless=headless,
            # acquire_timeout：旧会话被取消后，它的浏览器要一两秒才真正释放；
            # 这里等一会儿而不是立刻报「并发已满」。
            acquire_timeout=30.0,
            start_url=(douyin.HOME_URL if visible else ""))
        try:
            browser = browser_cm.__enter__()
        except session_store.BrowserBusy as exc:
            # 自愈：并发已满时**自动关掉该账号残留的登录/校验浏览器**（绝不杀任务浏览器），
            # 然后重试一次。用户要的就是「已满自动关闭」，而不是让他自己去点。
            closed = session_store.force_close_account_browser(account_id, settings)
            if not closed.get("closed"):
                raise
            sklog.warning(
                f"并发已满，已自动关闭该账号的旧登录窗口（kind={closed.get('kind') or '?'}），"
                f"正在重试：{exc}",
                scope="user", source="login.session", user_id=account.get("user_id"))
            time.sleep(0.8)
            browser_cm = session_store.AccountBrowser(
                account, settings, kind="login", restore=False, headless=headless,
                acquire_timeout=20.0,
                start_url=(douyin.HOME_URL if visible else ""))
            browser = browser_cm.__enter__()
        try:
            cdp = browser.cdp
            update_session(session_id, step=douyin.STEP_OPENING,
                           message=("已打开浏览器窗口，请在弹出的窗口里登录抖音…"
                                    if visible else "正在打开抖音…"))
            douyin.open_home(cdp)
            if not _wait_qrcode(session_id, cdp, stop, settings):
                return
            _wait_login_loop(session_id, stop, browser, account, settings, ttl)
        finally:
            try:
                browser_cm.__exit__(None, None, None)
            except Exception:      # noqa: BLE001 - 清理异常不覆盖真实结果
                pass
    except session_store.BrowserBusy as exc:
        _fail(session_id,
              "浏览器并发已满：该账号正在执行任务，暂时无法登录。"
              "请等任务跑完（或到「运行日志」确认任务结束后）再试。"
              f"（{exc}）")
    except session_store.BrowserUnavailable as exc:
        _fail(session_id, f"浏览器不可用：{exc}")
    except cdp_mod.CDPError as exc:
        _fail(session_id, f"页面操作失败：{exc}")
    except Exception as exc:      # noqa: BLE001 - 线程内任何异常都要落库，否则前端一直转圈
        _fail(session_id, f"未预期错误：{type(exc).__name__}: {exc}")


def _wait_qrcode(session_id: str, cdp: Any, stop: threading.Event, settings: Any) -> bool:
    """等二维码出现并截图落库。返回是否成功拿到二维码。

    可见窗口模式下，用户也可能**不扫码、直接用手机号/密码登录**：此时页面上
    根本没有二维码，但登录态已经建立。所以每轮先看会话 cookie —— 出现了就
    直接放行，交给 :func:`_wait_login_loop` 收尾，避免「用户已登录却被判失败」。
    """
    update_session(session_id, step=douyin.STEP_WAITING_QRCODE, message="正在获取二维码…")
    deadline = time.monotonic() + _wait_seconds(settings, "qrcode_wait_seconds",
                                                QRCODE_WAIT_SECONDS)
    while time.monotonic() < deadline:
        if stop.is_set():
            _demo_canceled(session_id)
            return False
        if douyin.has_login_cookie(cdp):
            # 用户已经登录成功（扫码/密码/短信都算）：不必再等二维码
            update_session(session_id, status="scanned", step=douyin.STEP_SCANNED,
                           message="已登录，正在保存登录态…")
            return True
        if _take_refresh(session_id):
            # 刷新请求：先尝试点页面上的「点击刷新」，拿不到就重新打开登录弹层
            selectors.click_group(cdp, "login_refresh_qrcode")
            selectors.click_group(cdp, "login_refresh_text")
            time.sleep(0.8)
        image = _capture_qrcode(cdp)
        if image:
            update_session(session_id, status="pending", step=douyin.STEP_QRCODE_READY,
                           qrcode_image=image, message="请用抖音 App 扫码",
                           expire_at=timeutil.to_db(
                               timeutil.now_utc() + timedelta(seconds=_ttl(settings))))
            return True
        douyin.open_login_panel(cdp, timeout=10.0)
        time.sleep(POLL_INTERVAL_SECONDS)
    # 超时前最后确认一次：可能刚好在最后一刻登录成功
    if douyin.has_login_cookie(cdp):
        return True
    # 失败前取证：页面到底长什么样（空白/验证码/登录失效），并把截图落盘。
    # 真实事故里用户只看到「没有在页面上找到二维码」，完全没法判断原因。
    summary = ""
    folder = ""
    try:
        snap = douyin.page_snapshot(cdp)
        summary = douyin.summarize_snapshot(snap)
        folder = douyin.save_snapshot_evidence(cdp, settings, snap, tag="login_no_qrcode")
    except Exception:                     # noqa: BLE001 - 取证失败不能盖住真实错误
        pass
    message = "没有在页面上找到二维码"
    if summary:
        message += f"\n      页面取证：{summary}"
    if folder:
        message += f"\n      证据已保存：{folder}"
    _fail(session_id, message)
    return False


def _capture_qrcode(cdp: Any) -> str:
    """截取**真二维码图片**区域，返回 data URL（还没渲染出来时返回空串）。

    为什么只认 ``login_qrcode_image`` 而不回落到容器：

    实测（2026-10 抖音 PC 版）弹层打开后，二维码位置先是**抖音 logo 占位 SVG**，
    约 3 秒后才换成真二维码 PNG。旧实现拿不到图片就回落到「二维码容器」，
    而容器实际是整个登录面板（726×483）——于是给用户看的是一张**没有二维码的面板图**，
    手机怎么扫都没反应。所以这里：
    * 只截真正的二维码 ``<img>``（选择器组已按实机校准）；
    * 拿不到就返回空串，让 ``_wait_qrcode`` 继续等，而不是把烂图发给前端。
    """
    hit = selectors.find_first(cdp, "login_qrcode_image")
    if not hit or not hit.get("rect"):
        return ""
    rect = hit["rect"]
    width = float(rect.get("width") or 0)
    height = float(rect.get("height") or 0)
    # 二维码至少要有 80×80 才可能扫得出来；更小的多半是占位/图标
    if width < 80 or height < 80:
        return ""
    try:
        data = cdp.capture_screenshot(format="png", clip={
            "x": rect.get("x") or 0, "y": rect.get("y") or 0,
            "width": width, "height": height,
        })
    except cdp_mod.CDPError:
        return ""
    return f"data:image/png;base64,{data}" if data else ""


def _wait_login_loop(session_id: str, stop: threading.Event, browser: Any, account: dict,
                     settings: Any, ttl: int) -> None:
    """轮询页面状态直到登录完成 / 超时 / 被取消。"""
    deadline = time.monotonic() + max(10, ttl)
    last_state = ""

    def on_state(state: str) -> None:
        if state == "scanned":
            update_session(session_id, status="scanned", step=douyin.STEP_SCANNED,
                           message="已扫码，请在手机上确认")
        elif state == "pending":
            update_session(session_id, status="pending", step=douyin.STEP_WAITING_SCAN,
                           message="等待扫码…")

    last_refresh = 0.0
    state = ""          # 上一轮的状态（首轮为空；下面的「过期自动刷新」要用到）
    while time.monotonic() < deadline:
        if stop.is_set():
            _demo_canceled(session_id)
            return
        if _take_refresh(session_id):
            # 刷新请求：重新截一张二维码（拿不到就保留旧图，不要给前端一张空白）
            fresh = _capture_qrcode(browser.cdp)
            update_session(session_id, status="pending", step=douyin.STEP_QRCODE_READY,
                           qrcode_image=fresh or None, message="二维码已刷新，请重新扫码")
        # 二维码过期自动刷新：抖音二维码有效期约 1~2 分钟，过期后手机再扫不会有任何反应
        # （用户看到的就是「扫码后没反应」）。这里检测「二维码已失效/点击刷新」文案，
        # 点一下刷新并换新图；15 秒内最多刷一次，避免把页面刷毛。
        now = time.monotonic()
        if state == "pending" and now - last_refresh > 15.0:
            try:
                expired_hint = selectors.text_present(browser.cdp, "login_refresh_text")
            except cdp_mod.CDPError:
                expired_hint = False
            if expired_hint:
                last_refresh = now
                selectors.click_group(browser.cdp, "login_refresh_qrcode")
                selectors.click_group(browser.cdp, "login_refresh_text")
                time.sleep(1.0)
                fresh = _capture_qrcode(browser.cdp)
                if fresh:
                    update_session(session_id, status="pending",
                                   step=douyin.STEP_QRCODE_READY, qrcode_image=fresh,
                                   message="二维码已过期，已自动刷新，请重新扫码")
        state = douyin.login_page_state(browser.cdp)
        if state != last_state:
            last_state = state
            on_state(state)
        if state == "logged_in":
            update_session(session_id, status="confirmed", step=douyin.STEP_SAVING,
                           message="登录成功，正在保存登录态…")
            identity = douyin.capture_login_identity(browser.cdp, account, settings)
            browser.save()
            _finish_success(session_id, account, settings, identity, demo=False)
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    update_session(session_id, status="expired", step="expired",
                   message="登录超时，二维码已失效，请重新发起", finished_at=timeutil.utc_now_str())


def _real_sms_worker(session_id: str, stop: threading.Event, account: dict,
                     settings: Any, phone: str) -> None:
    """真实短信流程（部署机运行；本机未验证）。"""
    ttl = _ttl(settings)
    try:
        with session_store.AccountBrowser(account, settings, kind="login",
                                          restore=False) as browser:
            cdp = browser.cdp
            update_session(session_id, step=douyin.STEP_OPENING, message="正在打开登录页…")
            douyin.open_home(cdp)
            douyin.open_login_panel(cdp)
            # 切到短信登录
            if not selectors.click_group(cdp, "login_sms_entry") and \
                    not selectors.click_group(cdp, "login_sms_entry_text"):
                _fail(session_id, "页面上找不到「手机号登录」入口（可跑 diagnose 查看）")
                return
            time.sleep(1.0)
            update_session(session_id, step=douyin.STEP_SENDING_SMS, message="正在填写手机号…")
            filled = selectors.set_input_value(cdp, "login_phone_input", phone)
            if not filled.get("ok"):
                _fail(session_id, "找不到手机号输入框（可跑 diagnose 查看）")
                return
            if not selectors.click_group(cdp, "login_send_code_button") and \
                    not selectors.click_group(cdp, "login_send_code_text"):
                _fail(session_id, "找不到「发送验证码」按钮（可跑 diagnose 查看）")
                return
            update_session(session_id, step=douyin.STEP_VERIFYING_SMS,
                           message="验证码已发送，请填写后提交")
            if not _submit_code_loop(session_id, stop, browser, account, settings, ttl):
                return
    except session_store.BrowserBusy as exc:
        _fail(session_id, f"浏览器并发已满，请稍后再试（{exc}）")
    except session_store.BrowserUnavailable as exc:
        _fail(session_id, f"浏览器不可用：{exc}")
    except cdp_mod.CDPError as exc:
        _fail(session_id, f"页面操作失败：{exc}")
    except Exception as exc:      # noqa: BLE001
        _fail(session_id, f"未预期错误：{type(exc).__name__}: {exc}")


def _submit_code_loop(session_id: str, stop: threading.Event, browser: Any, account: dict,
                      settings: Any, ttl: int) -> bool:
    """等前端提交验证码 → 输入 → 提交 → 等登录成功。"""
    deadline = time.monotonic() + max(10, ttl)
    code = ""
    while time.monotonic() < deadline:
        code = _steal_code(session_id)
        if code:
            break
        if stop.is_set():
            _demo_canceled(session_id)
            return False
        time.sleep(0.2)
    if not code:
        update_session(session_id, status="expired", step="expired",
                       message="验证码提交超时，请重新发起登录",
                       finished_at=timeutil.utc_now_str())
        return False

    cdp = browser.cdp
    update_session(session_id, step=douyin.STEP_VERIFYING_SMS, message="正在校验验证码…")
    filled = selectors.set_input_value(cdp, "login_code_input", code)
    if not filled.get("ok"):
        update_session(session_id, step=douyin.STEP_VERIFYING_SMS,
                       message="正在校验验证码…（未能定位验证码输入框）")
    # 同意用户协议（没勾的话登录按钮是灰的）
    selectors.click_group(cdp, "login_agree_checkbox")
    time.sleep(0.4)
    if not selectors.click_group(cdp, "send_button_text") and \
            not selectors.click_group(cdp, "login_send_code_button"):
        # 「登录」按钮文案与「发送验证码」不同，这里用 Enter 兜底提交
        try:
            cdp.press_key("Enter")
        except cdp_mod.CDPError:
            pass
    update_session(session_id, status="confirmed", step=douyin.STEP_SAVING,
                   message="验证码已提交，正在确认登录…")
    for _ in range(60):
        if stop.is_set():
            _demo_canceled(session_id)
            return False
        if douyin.login_page_state(cdp) == "logged_in":
            identity = douyin.capture_login_identity(cdp, account, settings)
            browser.save()
            _finish_success(session_id, account, settings, identity, demo=False)
            return True
        time.sleep(1.0)
    update_session(session_id, status="failed", step="failed",
                   message="验证码校验未通过（验证码错误或已过期）",
                   finished_at=timeutil.utc_now_str())
    return False


# --------------------------------------------------------------------------
# 公共收尾
# --------------------------------------------------------------------------
def _finish_success(session_id: str, account: dict, settings: Any, identity: dict,
                    *, demo: bool) -> None:
    """登录成功收尾：落登录态 → 回填账号 → 更新会话行。"""
    account_id = int(account.get("id") or 0)
    storage_name = session_store.session_filename(account_id) if account_id else ""
    if demo and account_id:
        # 演示模式写一份**假登录态**：让 /douyin/accounts/<id>/check 的
        # 文件级校验与「账号在线」链路也能在 CI 里跑通。
        session_store.save_state(account_id, {
            "cookies": [{"name": "sessionid", "value": f"demo-{secrets.token_hex(6)}",
                         "domain": ".douyin.com", "path": "/", "expires": 0},
                        {"name": "ttwid", "value": "demo", "domain": ".douyin.com",
                         "path": "/", "expires": 0}],
            "origins": {session_store.MAIN_ORIGIN: {"demo": "1"}},
            "user_agent": "demo", "url": "https://www.douyin.com/",
        }, settings)
    now = timeutil.utc_now_str()
    session = fetch_session(session_id) or {}
    login_type = "sms" if str(session.get("method")) == "sms" else "qrcode"
    if account_id:
        try:
            db.update("douyin_accounts", {
                "nickname": str(identity.get("nickname") or account.get("nickname") or "")[:100],
                "avatar": str(identity.get("avatar") or "")[:500],
                "douyin_uid": str(identity.get("douyin_uid") or account.get("douyin_uid") or "")[:64],
                "status": "online",
                "storage_path": storage_name or None,
                "storage_updated_at": now,
                "last_check_at": now,
                "last_error": "",
                "login_type": login_type,
                "updated_at": now,
            }, "id=?", (account_id,))
        except Exception as exc:      # noqa: BLE001 - 回填失败要能看出来
            sklog.error(f"回填抖音账号失败 account={account_id}: {exc}",
                        scope="user", source="login.session")
    update_session(session_id, status="success", step=douyin.STEP_DONE,
                   message="登录成功", finished_at=now)
    sklog.info(f"登录成功：session={session_id} account={account_id} demo={demo} "
               f"nickname={identity.get('nickname')}",
               scope="user", source="login.session", user_id=account.get("user_id"))


def _fail(session_id: str, message: str) -> None:
    session = fetch_session(session_id) or {}
    if str(session.get("status")) == "canceled":
        return                    # 用户已取消，不要用失败覆盖掉取消状态
    update_session(session_id, status="failed", step="failed", message=message[:500],
                   finished_at=timeutil.utc_now_str())
    sklog.warning(f"登录失败：session={session_id} {message}",
                  scope="user", source="login.session")


# --------------------------------------------------------------------------
# 手动导入 Cookie 后的真机校验
# --------------------------------------------------------------------------
def verify_imported_session(account: dict, settings: Any) -> dict:
    """用导入的 Cookie 开一次浏览器，看抖音**是否真的认**这个登录态。

    返回 ``{"verified": True/False/None, "reason": "", "nickname": "", "message": ""}``：

    * ``True`` —— 页面已登录（有头像/昵称，且没有登录弹层）；
    * ``False`` —— 页面仍要求登录（cookie 过期 / 复制不全 / 不是同一账号）；
    * ``None`` —— **校验这件事本身没跑成**（浏览器起不来、并发满）。此时不应
      判定 Cookie 无效，只提示「已保存但未校验」。

    为什么必须用 ``use_cookie=False``：cookie 是我们自己写进浏览器的，
    拿它自证「登录成功」等于没验证；只有页面 DOM 才是抖音的真实态度。

    ⚠️ 必须**轮询等页面渲染完**再下结论。抖音首页是前端渲染的，导航刚完成时
    DOM 里既没有头像也没有登录弹层（``reason='no_signal'``）。旧实现只读一次，
    于是把好 Cookie 判成「抖音未接受」——用户白折腾一轮。现在：
    * 只有明确读到「未登录信号」（登录弹层/扫码框）才判 False；
    * 一直是 ``no_signal``（页面没渲染完/网络慢）则返回 ``None``，提示稍后再点「检测」。
    """
    try:
        with session_store.AccountBrowser(account, settings, kind="verify",
                                          restore=True) as browser:
            cdp = browser.cdp
            deadline = time.monotonic() + max(5.0, float(getattr(
                settings, "qrcode_wait_seconds", 60) or 60) / 3.0)
            state: dict = {}
            reason = "no_signal"
            while time.monotonic() < deadline:
                try:
                    state = douyin.read_login_state(cdp, use_cookie=False)
                except cdp_mod.CDPError as exc:
                    return {"verified": None, "reason": f"cdp_error:{exc}", "nickname": "",
                            "message": f"已保存，但校验时页面异常（{exc}）"}
                reason = str(state.get("reason") or "")
                if state.get("logged_in"):
                    break
                if reason == "login_required_signal":
                    # 出现登录弹层/扫码框 = 抖音明确不认这个会话，可以立刻下结论
                    break
                time.sleep(0.8)          # 页面还在渲染，等一会儿再看
            verified = bool(state.get("logged_in"))
            nickname = str(state.get("nickname") or "")
            if verified:
                message = "校验通过：抖音已接受该登录态"
                if nickname:
                    message += f"（账号：{nickname}）"
                return {"verified": True, "reason": reason, "nickname": nickname,
                        "message": message}
            if reason == "login_required_signal":
                return {"verified": False, "reason": reason, "nickname": nickname,
                        "message": ("校验未通过：抖音仍要求登录。常见原因：Cookie 已过期、"
                                    "复制不完整，或不是 sessionid。")}
            # 一直没信号：多半是页面没渲染完 / 网络慢，别武断判 Cookie 无效
            return {"verified": None, "reason": reason or "page_not_ready",
                    "nickname": nickname,
                    "message": ("已保存，但校验时抖音首页还没渲染完（可能网络慢），"
                                "无法确认。稍等一会儿点「检测」再看，或直接跑一次任务。")}
    except session_store.BrowserBusy as exc:
        return {"verified": None, "reason": "browser_busy", "nickname": "",
                "message": f"已保存，但浏览器并发已满，暂时无法校验（{exc}）"}
    except session_store.BrowserUnavailable as exc:
        return {"verified": None, "reason": "browser_unavailable", "nickname": "",
                "message": f"已保存，但本机浏览器不可用，暂时无法校验（{exc}）"}
    except Exception as exc:      # noqa: BLE001 - 校验失败不能让「导入」整体失败
        return {"verified": None, "reason": f"error:{type(exc).__name__}", "nickname": "",
                "message": f"已保存，但校验过程出错（{type(exc).__name__}: {exc}）"}


# --------------------------------------------------------------------------
# 便于测试与排障
# --------------------------------------------------------------------------
def active_sessions() -> list[dict]:
    """当前进程里还在跑的登录会话（排障用，不含二维码内容以外的敏感数据）。"""
    with _state_lock:
        items = []
        for session_id, thread in _threads.items():
            items.append({"session_id": session_id, "alive": thread.is_alive(),
                          "waiting_code": session_id in _pending_codes})
        return items


def reset_state() -> None:
    """测试用：清空线程登记与交接槽（不碰数据库）。"""
    with _state_lock:
        for flag in _stop_flags.values():
            flag.set()
        _threads.clear()
        _stop_flags.clear()
        _pending_codes.clear()
        _refresh_flags.clear()
        _visible_flags.clear()
