"""单次续火花任务的执行流程（真实浏览器 + demo_mode 假实现）。

一次执行的完整链路
------------------
1. 建/复用 ``task_runs`` 行（``running``）；
2. 选好友（``friends`` 表 + 任务级 ``friend_mode``）；
3. 打开私信页，抓会话列表，把好友与会话**匹配**起来（sec_uid 优先 → 昵称全等 →
   包含；用 ``textutil.normalize_name`` 消掉空白/大小写差异）；
4. 逐好友发送：随机文案 → 三种输入策略降级 → 点发送 → **结果校验**；
5. 每个好友写一条 ``task_run_items``，成功再写 ``send_history``（幂等台账）；
6. 汇总 ``task_runs``（running → success/partial/failed/skipped），失败截图。

幂等与「每天只发一次」
---------------------
真正的幂等依据是 ``send_history`` 的唯一索引
``(account_id, friend_key, send_date)``：

* 发送**前**查一次，已发过就跳过（省下一次浏览器交互）；
* 发送**后**再插入一次，``IntegrityError`` 说明并发下另一个执行已经记过账，
  此时**不重复计数**（消息可能真的发了两条，但台账只有一条，这在产品上可接受，
  在数据上不会出现「同一好友当天记两笔」。

⚠️ ``scheduled_tasks.daily_limit`` 目前实际只能取 1：台账唯一索引决定了
「同一账号+好友+日期」只允许一行。``daily_limit>1`` 的场景（一天多次续火花）
需要扩展索引，本版本按 1 处理并在日志里说明。

未在本机验证的部分
------------------
所有真实页面交互（选择器命中、Draft.js 注入、发送校验）都只能在部署机验证；
本机通过 demo_mode + 纯函数单测覆盖流程与数据正确性。
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db
from ..common import logging as sklog
from ..common import textutil, timeutil
from . import cdp as cdp_mod
from . import douyin, humanize, selectors, session_store

#: 每个好友最多重试几次（``scheduled_tasks.max_retry`` 的上限保护）。
#: 为什么还要单独设上限：max_retry 是「整轮重试」的参数，单好友重试太多次会把
#: 任务超时预算吃光，导致后面的好友全部被跳过。
MAX_FRIEND_ATTEMPTS = 2
#: 发送后等待「输入框清空 / 气泡出现」的时间（秒）。
SEND_VERIFY_SECONDS = 12.0
#: 单个好友发送失败时的退避（秒）。
RETRY_BACKOFF_SECONDS = 1.5
#: 文件名里不允许出现的字符（好友昵称会进文件名，必须清洗）
_UNSAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff\-]+")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Target:
    """一个待发送的好友（可能附带页面上的会话项）。"""

    friend: dict
    conversation: dict | None = None
    matched_by: str = "none"

    @property
    def nickname(self) -> str:
        return str(self.friend.get("nickname") or "").strip()

    @property
    def friend_key(self) -> str:
        """台账键：``sec_uid`` 优先，否则昵称（与迁移注释一致）。"""
        sec_uid = str(self.friend.get("sec_uid") or "").strip()
        return sec_uid or self.nickname


@dataclass
class RunOutcome:
    run_id: int
    status: str = "running"
    total: int = 0
    success: int = 0
    fail: int = 0
    skipped: int = 0
    message: str = ""
    error_code: str = ""
    items: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "total_count": self.total,
            "success_count": self.success,
            "fail_count": self.fail,
            "skipped_count": self.skipped,
            "message": self.message,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "items": self.items,
        }


# --------------------------------------------------------------------------
# 好友选择与匹配（纯函数，可单测）
# --------------------------------------------------------------------------
def select_friends(task: dict, *, account_id: int | None = None) -> list[dict]:
    """按任务的 ``friend_mode`` 取要发送的好友（只取 ``enabled=1``）。"""
    target_account = int(account_id or task.get("account_id") or 0)
    rows = db.query_all(
        "SELECT * FROM friends WHERE account_id=? AND enabled=1 ORDER BY id",
        (target_account,),
    )
    mode = str(task.get("friend_mode") or "all")
    if mode != "selected":
        return rows
    wanted = set(textutil.as_int_list(task.get("friend_ids")))
    if not wanted:
        # 「指定好友」但一个都没选：返回空列表比发全部安全得多
        return []
    return [row for row in rows if int(row.get("id") or 0) in wanted]


def _names_of(friend: dict) -> list[str]:
    """可用于匹配的名字：昵称 + 备注（备注常被用户改成页面上的显示名）。"""
    out = []
    for value in (friend.get("nickname"), friend.get("remark")):
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def match_friend(friend: dict, conversations: list[dict]) -> tuple[dict | None, str]:
    """把一条好友记录匹配到页面上的会话项。

    优先级：``sec_uid`` 全等 > 昵称全等 > 昵称互相包含。
    返回 ``(会话项, 匹配方式)``；匹配不到返回 ``(None, 'none')``。
    """
    sec_uid = str(friend.get("sec_uid") or "").strip()
    if sec_uid:
        for item in conversations:
            if str(item.get("sec_uid") or "").strip() == sec_uid:
                return item, "sec_uid"
    wanted = [textutil.normalize_name(name) for name in _names_of(friend)]
    wanted = [name for name in wanted if name]
    if not wanted:
        return None, "none"
    # 1) 全等
    for item in conversations:
        candidate = textutil.normalize_name(item.get("nickname"))
        if candidate and candidate in wanted:
            return item, "exact"
    # 2) 包含（抖音会在昵称后面加「· 已关注」之类后缀）
    for item in conversations:
        candidate = textutil.normalize_name(item.get("nickname"))
        if not candidate:
            continue
        for name in wanted:
            if name in candidate or candidate in name:
                return item, "contains"
    return None, "none"


def match_conversations(friends: list[dict],
                        conversations: list[dict]) -> tuple[list[Target], list[dict]]:
    """批量匹配。返回 ``(命中的 Target 列表, 没匹配上的好友列表)``。

    一个会话项被多条好友记录命中时，先到先得（好友表里越靠前的越优先），
    避免同一个会话被发两次。
    """
    used: set[int] = set()
    matched: list[Target] = []
    unmatched: list[dict] = []
    for friend in friends:
        available = [item for index, item in enumerate(conversations) if index not in used]
        item, how = match_friend(friend, available)
        if item is None:
            unmatched.append(friend)
            continue
        used.add(conversations.index(item))
        matched.append(Target(friend=friend, conversation=item, matched_by=how))
    return matched, unmatched


# --------------------------------------------------------------------------
# 台账（幂等）
# --------------------------------------------------------------------------
def sent_today(account_id: int, friend_key: str, send_date: str) -> bool:
    """今天是否已经给这个好友成功发过（``send_history`` 唯一索引的依据）。"""
    row = db.query_one(
        "SELECT id FROM send_history WHERE account_id=? AND friend_key=? AND send_date=?",
        (int(account_id), str(friend_key), str(send_date)),
    )
    return row is not None


def mark_sent(account_id: int, friend: dict, send_date: str, message: str,
              run_id: int | None, friend_key: str = "") -> bool:
    """写发送台账。返回 ``True`` 表示这次真的写进去了（首次发送）。

    ``False`` 表示唯一索引冲突 = 并发下已经记过账（不是错误，也不能重复计数）。
    """
    key = friend_key or str(friend.get("sec_uid") or "").strip() or \
        str(friend.get("nickname") or "").strip()
    try:
        db.insert("send_history", {
            "account_id": int(account_id),
            "friend_id": friend.get("id"),
            "friend_key": key,
            "send_date": str(send_date),
            "message": str(message)[:500],
            "run_id": run_id,
            "created_at": timeutil.utc_now_str(),
        })
        return True
    except sqlite3.IntegrityError:
        return False


def _touch_friend(friend: dict, status: str, *, sent: bool = False) -> None:
    """回填好友的最近发送状态（页面上「上次发送」列要用）。"""
    friend_id = int(friend.get("id") or 0)
    if not friend_id:
        return
    data = {"last_status": status[:20], "updated_at": timeutil.utc_now_str()}
    if sent:
        data["last_sent_at"] = timeutil.utc_now_str()
    try:
        db.update("friends", data, "id=?", (friend_id,))
    except Exception:            # noqa: BLE001 - 回填失败不能影响发送结果
        pass


# --------------------------------------------------------------------------
# 执行记录
# --------------------------------------------------------------------------
def create_run(task: dict | None, account: dict, trigger_type: str, *,
               status: str = "running", message: str = "") -> int:
    """建 ``task_runs`` 行并返回 id。"""
    now = timeutil.utc_now_str()
    tz_name = str((task or {}).get("timezone") or "Asia/Shanghai")
    return db.insert("task_runs", {
        "task_id": (task or {}).get("id"),
        "user_id": int(account.get("user_id") or (task or {}).get("user_id") or 0),
        "account_id": int(account.get("id") or 0),
        "run_date": timeutil.local_date_str(tz_name),
        "trigger_type": trigger_type,
        "status": status,
        "started_at": now,
        "finished_at": None if status == "running" else now,
        "duration_ms": 0,
        "total_count": 0, "success_count": 0, "fail_count": 0,
        "message": message[:500], "error_code": "",
        "screenshots": "[]", "created_at": now,
    })


def finish_run(run_id: int, outcome: RunOutcome) -> None:
    """把执行结果写回 ``task_runs``。"""
    if not run_id:
        return
    now = timeutil.utc_now_str()
    db.update("task_runs", {
        "status": outcome.status,
        "finished_at": now,
        "duration_ms": int(outcome.duration_ms),
        "total_count": int(outcome.total),
        "success_count": int(outcome.success),
        "fail_count": int(outcome.fail),
        "message": str(outcome.message)[:500],
        "error_code": str(outcome.error_code)[:50],
    }, "id=?", (int(run_id),))


def insert_item(run_id: int, *, friend_id: int | None, friend_name: str, status: str,
                message: str = "", error_code: str = "", error_detail: str = "",
                attempts: int = 0, duration_ms: int = 0, screenshot: str = "") -> None:
    """写一条好友级执行明细。"""
    now = timeutil.utc_now_str()
    try:
        db.insert("task_run_items", {
            "run_id": int(run_id), "friend_id": friend_id, "friend_name": friend_name[:100],
            "status": status, "message": str(message)[:500], "error_code": error_code[:50],
            "error_detail": str(error_detail)[:1000], "attempts": int(attempts),
            "duration_ms": int(duration_ms), "screenshot": screenshot or None,
            "created_at": now, "finished_at": now,
        })
    except Exception as exc:      # noqa: BLE001 - 明细写失败不能让整轮任务崩掉
        sklog.warning(f"写执行明细失败（run={run_id} friend={friend_name}）：{exc}",
                      scope="worker", source="task.runner")


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------
def safe_filename(text: str, *, limit: int = 20) -> str:
    cleaned = _UNSAFE_NAME.sub("_", str(text or "").strip())[:limit].strip("_")
    return cleaned or "item"


def save_screenshot(cdp: Any, run_id: int, name: str, settings: Any) -> str:
    """截图并返回**相对 screenshot_root 的路径**（接口层按 run_id 提供下载）。"""
    if not run_id:
        return ""
    try:
        folder = Path(getattr(settings, "screenshot_root", "") or ".") / f"run_{int(run_id)}"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_filename(name)}.png"
        data = cdp.capture_screenshot(format="png")
        if not data:
            return ""
        import base64
        (folder / filename).write_bytes(base64.b64decode(data))
        return f"run_{int(run_id)}/{filename}"
    except Exception as exc:      # noqa: BLE001 - 截图是排障辅助，失败不能影响业务
        sklog.debug(f"截图失败：{exc}", scope="worker", source="task.runner")
        return ""


# --------------------------------------------------------------------------
# 文案与输入
# --------------------------------------------------------------------------
def build_message(task: dict, target: Target, rng: random.Random | None = None) -> str:
    """按任务配置挑一条文案并渲染变量（用 Lead 已实现的 ``textutil.pick_message``）。"""
    messages = textutil.as_str_list(task.get("messages_json")) or \
        textutil.as_str_list(task.get("messages"))
    variables = {
        "nickname": target.nickname,
        "name": target.nickname,
        "date": timeutil.local_date_str(str(task.get("timezone") or "Asia/Shanghai")),
        "spark_days": target.friend.get("spark_days") or 0,
        "account": target.friend.get("account_nickname") or "",
    }
    return textutil.pick_message(messages, variables, rng)


def type_message(cdp: Any, text: str, rng: random.Random | None = None,
                 *, group: str = "message_input") -> tuple[bool, str]:
    """把文字写进抖音的 Draft.js 输入框：四种策略依次降级。

    返回 ``(是否写成功, 用了哪种策略/失败原因)``。为什么需要这么多策略：
    抖音聊天框是 Draft.js，直接改 DOM 文本它不认（下一次 onChange 就覆盖回去），
    所以必须「派发真事件」或「走真键盘」。实测顺序（前作经验）：

    1. 真键盘逐字输入 —— 最像真人，但依赖输入框已聚焦，有时点击没生效；
    2. ``ClipboardEvent('paste')`` —— 触发 Draft.js 的 onChange，最通用；
    3. ``document.execCommand('insertText')`` —— Chromium 仍支持，会派发 input；
    4. ``Input.insertText``（CDP 原生）—— 不产生 keydown，但能插入并派发 input。
    """
    rng = rng or random
    wanted = textutil.normalize_name(text)
    if not wanted:
        return False, "空文案"

    def typed() -> bool:
        current = textutil.normalize_name(editor_text(cdp, group))
        return bool(current) and wanted in current

    # 先聚焦并把残留清掉（上一轮失败可能留下半截文字）。
    # 真实抖音编辑器是 **Slate**（data-slate-editor="true"），不是 Draft.js：
    # 清空走 Ctrl+A + Backspace 最可靠（Slate 会响应真实键盘事件）。
    hit = selectors.find_first(cdp, group)
    if hit and hit.get("rect"):
        selectors.click_at(cdp, hit["rect"])
        time.sleep(humanize.random_between(0.15, 0.4, rng))
    try:
        cdp.select_all()                        # CDP 原生 selectAll（Ctrl+A 键码不可靠）
        time.sleep(0.1)
        cdp.press_key("Backspace")
        time.sleep(0.2)
    except cdp_mod.CDPError:
        pass
    if selectors.read_editor(cdp, group):
        # Ctrl+A 无效时退回 JS 清空（老版 Draft.js 用得上）
        try:
            cdp.call(selectors.clear_editor_js(), selectors.payload_for(group))
        except cdp_mod.CDPError:
            pass

    # 策略 1（首选）：CDP Input.insertText —— 实机验证对 Slate 编辑器直接生效，
    # 且不受「合成事件被 React 忽略」影响。原实现把它排在最后，导致明明能成功
    # 却先跑三个不可靠的策略。
    try:
        cdp.insert_text(text)
    except cdp_mod.CDPError as exc:
        sklog.debug(f"insertText 抛异常：{exc}", scope="worker", source="task.typing")
    if typed():
        return True, "CDP insertText"

    # 策略 2：execCommand('insertText')（Chromium 仍支持，会派发 input）
    try:
        result = cdp.call(selectors.exec_insert_js(),
                          selectors.payload_for(group, text=text))
        sklog.debug(f"execCommand 结果：{result}", scope="worker", source="task.typing")
    except cdp_mod.CDPError as exc:
        sklog.debug(f"execCommand 抛异常：{exc}", scope="worker", source="task.typing")
    if typed():
        return True, "execCommand 插入"

    # 策略 3：粘贴事件（Draft.js 版本有效）
    try:
        result = cdp.call(selectors.draft_paste_js(),
                          selectors.payload_for(group, text=text))
        sklog.debug(f"paste 注入结果：{result}", scope="worker", source="task.typing")
    except cdp_mod.CDPError as exc:
        sklog.debug(f"paste 注入抛异常：{exc}", scope="worker", source="task.typing")
    if typed():
        return True, "粘贴事件注入"

    # 策略 4：逐字真键盘（最像真人，也最慢，放最后）
    try:
        for char, delay in humanize.typing_plan(text, rng):
            cdp.type_text(char)
            time.sleep(delay)
    except cdp_mod.CDPError as exc:
        sklog.debug(f"键盘输入抛异常：{exc}", scope="worker", source="task.typing")
    if typed():
        return True, "键盘输入"

    return False, ("四种输入方式都没能把文字写进输入框"
                   "（输入框可能没聚焦，或抖音编辑器结构再次改版，请跑 diagnose）")


def scan_blockers(cdp: Any) -> str:
    """扫描页面上的拦截提示（「不是好友」「操作频繁」等）。"""
    try:
        found = cdp.call(selectors.scan_blockers_js(),
                         {"phrases": list(selectors.BLOCKER_PHRASES)})
    except cdp_mod.CDPError:
        return ""
    if isinstance(found, list) and found:
        return " / ".join(str(item) for item in found[:3])
    return ""


#: Slate 编辑器清空后常残留不可见字符（\u200b 等）。不剔掉的话
#: 「输入框已清空 ⇒ 已发送」这条判据会永远为假，把成功发送误报成失败。
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00a0"))


def editor_text(cdp: Any, group: str = "message_input") -> str:
    """读输入框当前文本，并剔除不可见字符（零宽空格/不换行空格等）。"""
    raw = selectors.read_editor(cdp, group)
    return str(raw or "").translate(_INVISIBLE_CHARS).strip()


def verify_sent(cdp: Any, text: str, *, timeout: float = SEND_VERIFY_SECONDS,
                group: str = "message_input") -> tuple[bool, str]:
    """确认消息真的发出去了。两条证据任一成立即算成功：

    1. Draft.js 发送后**输入框必然清空**（最可靠，与页面结构无关）；
    2. 消息列表里出现这条文案（第二重证据，防止「输入框清空但其实没发」）。
    """
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        try:
            if not editor_text(cdp, group):
                return True, "已发送（输入框已清空）"
        except cdp_mod.CDPError:
            pass
        try:
            info = cdp.call(selectors.last_message_js(), {
                "group": selectors.payload_for("message_list")["group"],
                "needle": text,
            })
            if isinstance(info, dict) and info.get("matched"):
                return True, "已发送（消息列表中已确认）"
        except cdp_mod.CDPError:
            pass
        time.sleep(0.3)
    return False, "提交后输入框没有清空，消息可能没发出去"


def send_message(cdp: Any, text: str, rng: random.Random | None = None,
                 *, verify_seconds: float = SEND_VERIFY_SECONDS) -> dict:
    """完整发一条：输入 → 提交 → 校验。返回 ``{ok, via, detail, error_code}``。"""
    rng = rng or random
    typed, how = type_message(cdp, text, rng)
    if not typed:
        return {"ok": False, "via": "", "detail": how, "error_code": "input_failed"}

    # 提交方式：实机（2026-10）抖音私信是 **Enter 发送**（Shift+Enter 换行），
    # 页面上并没有「发送」按钮；`.messageMsgInputinputAction` 是图片/文件上传。
    # 所以顺序是：先试真正的发送按钮（老版本有），没有就按 Enter。
    via = ""
    for _attempt in range(2):
        if selectors.click_group(cdp, "send_button"):
            via = "点击发送按钮"
            break
        time.sleep(humanize.random_between(0.3, 0.6, rng))
    if not via:
        try:
            cdp.press_key("Enter")
            via = "Enter 提交"
        except cdp_mod.CDPError as exc:
            return {"ok": False, "via": "", "detail": f"Enter 提交失败：{exc}",
                    "error_code": "no_send_button"}
    time.sleep(humanize.random_between(0.35, 0.7, rng))

    ok, detail = verify_sent(cdp, text, timeout=verify_seconds)
    if ok:
        return {"ok": True, "via": f"{how} + {via}", "detail": detail, "error_code": ""}
    blocker = scan_blockers(cdp)
    if blocker:
        code = "verify_required" if "验证" in blocker else "blocked"
        return {"ok": False, "via": f"{how} + {via}",
                "detail": f"疑似被平台拦截：{blocker}", "error_code": code}
    return {"ok": False, "via": f"{how} + {via}", "detail": detail, "error_code": "not_confirmed"}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def execute_task(task: dict, account: dict, settings: Any, dry_run: bool = False,
                 *, run_id: int | None = None, trigger_type: str = "manual",
                 friends: list[dict] | None = None) -> dict:
    """执行一次续火花任务，返回执行结果 dict（同时落库）。

    ``task`` / ``account`` 都是数据库整行 dict（队友 A 从 ``scheduled_tasks`` /
    ``douyin_accounts`` 取出来直接传）。``run_id`` 由 ``worker.runner`` 预建
    （它要先做账号级互斥），直接调用时不传即可。
    """
    started = time.monotonic()
    settings.ensure_dirs()
    account_id = int(account.get("id") or 0)
    timezone_name = str(task.get("timezone") or getattr(settings, "timezone", "Asia/Shanghai"))
    run_date = timeutil.local_date_str(timezone_name)
    timeout_seconds = int(getattr(settings, "task_timeout_seconds", 180) or 180)
    deadline = started + max(10, timeout_seconds)
    rng = humanize.make_rng()

    if not run_id:
        run_id = create_run(task, account, trigger_type)
    outcome = RunOutcome(run_id=int(run_id))
    sklog.info(
        f"开始执行任务：account={account_id} task={task.get('id')} "
        f"trigger={trigger_type} dry_run={dry_run} run={run_id}",
        scope="worker", source="task.runner", user_id=task.get("user_id"),
        context={"run_id": run_id, "account_id": account_id},
    )

    targets: list[Target] = []
    unmatched: list[dict] = []
    try:
        friends = friends if friends is not None else select_friends(task, account_id=account_id)
        if not friends:
            outcome.status = "skipped"
            outcome.message = "没有可发送的好友（请先在好友管理里添加或同步）"
            outcome.error_code = "no_friends"
            return _finish(outcome, started, run_id, task, dry_run=dry_run,
                           trigger_type=trigger_type)

        if douyin.is_demo(settings):
            targets, unmatched = _demo_match(friends, account)
            _run_targets(outcome, targets, task, account, settings, run_date, dry_run,
                         deadline, rng, cdp=None, unmatched=unmatched)
        else:
            _run_real(outcome, task, account, settings, friends, run_date, dry_run,
                      deadline, rng)
    except session_store.BrowserBusy as exc:
        outcome.status = "skipped"
        outcome.error_code = "browser_busy"
        outcome.message = f"浏览器并发已满：{exc}"
    except session_store.BrowserUnavailable as exc:
        outcome.status = "failed"
        outcome.error_code = "browser_unavailable"
        outcome.message = f"浏览器不可用：{exc}"
    except cdp_mod.CDPError as exc:
        outcome.status = "failed"
        outcome.error_code = "cdp_error"
        outcome.message = f"页面操作失败：{exc}"
    except Exception as exc:      # noqa: BLE001 - 任何异常都要落库，否则任务永远停在 running
        outcome.status = "failed"
        outcome.error_code = "unexpected"
        outcome.message = f"未预期错误：{type(exc).__name__}: {exc}"
        # 真实浏览器的失败大多没法复现，**没有堆栈基本等于没法排查**：
        # 用户可读信息留在 message，堆栈进 run_logs.context（/admin/logs 能直接看到）。
        sklog.error(f"任务执行异常：{type(exc).__name__}: {exc}",
                    scope="worker", source="task.runner", user_id=task.get("user_id"),
                    context={"run_id": run_id, "account_id": account_id,
                             "traceback": traceback.format_exc()[-2000:]})
    if dry_run and outcome.status == "skipped" and outcome.total and not outcome.fail:
        # 预演成功：好友都匹配上了、只是没发。标记 success 是为了让 CLI 退出码为 0
        # （用户会以为「预演失败」），真正的「没发送」体现在明细的 error_code=dry_run。
        outcome.status = "success"
        outcome.error_code = ""
        outcome.message = f"预演完成：匹配到 {outcome.total} 个好友，未实际发送"
    return _finish(outcome, started, run_id, task, dry_run=dry_run, trigger_type=trigger_type)


def _finish(outcome: RunOutcome, started: float, run_id: int, task: dict | None = None,
            *, dry_run: bool = False, trigger_type: str = "manual") -> dict:
    """收尾：回填任务调度缓存 + 落库执行记录 + 写日志。"""
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    finish_run(run_id, outcome)
    _backfill_task_last_run(task, outcome, dry_run=dry_run, trigger_type=trigger_type)
    log_level = "info" if outcome.status in ("success", "partial", "skipped") else "warning"
    sklog.log(
        f"任务执行结束：status={outcome.status} 成功 {outcome.success}/{outcome.total} "
        f"失败 {outcome.fail} 跳过 {outcome.skipped} 耗时 {outcome.duration_ms}ms "
        f"{outcome.message}",
        level=log_level, scope="worker", source="task.runner",
        context={"run_id": run_id, "status": outcome.status},
    )
    return outcome.as_dict()


def _backfill_task_last_run(task: dict | None, outcome: RunOutcome, *,
                            dry_run: bool = False, trigger_type: str = "manual") -> None:
    """回填 ``scheduled_tasks`` 的调度缓存（见 :func:`backfill_task_schedule`）。"""
    if dry_run or not task:
        return
    backfill_task_schedule(task, outcome.status, trigger_type=trigger_type)


def backfill_task_schedule(task: dict | None, status: str, *,
                           trigger_type: str = "manual", dry_run: bool = False) -> bool:
    """回填 ``scheduled_tasks.last_run_at`` / ``last_status``（必要时重算 ``next_run_at``）。

    为什么执行侧也要写（调度器已经在派发时写过一次 'running'）：

    * 手动执行（``POST /tasks/<id>/run`` → ``enqueue_run``）**不经过调度器**，不写的话
      接口返回的 ``last_run_at/last_status`` 永远是空/旧值；
    * ``repeat_mode='interval'`` 的 ``next_run_at`` 锚点就是 ``last_run_at``，
      锚点不更新会让间隔排期整体偏掉。

    语义裁定（避免含糊）：
    * ``last_run_at`` = **最后一次实际执行的时间**，手动执行也算 —— 它作为 interval
      的锚点正是「上次真的跑过的时间」，比「上次自动派发的时间」更准；
    * ``manual`` / ``retry`` 触发时**同时重算 next_run_at**（这两种触发方式调度器
      不会替它算）；``schedule`` 触发时不动 next_run_at —— 那是调度器在派发时就
      算好的，谁算谁写，避免两边互相覆盖；
    * ``dry_run`` **完全不写**：预演是「只探测好友、不发送」的诊断动作，
      不能影响排期（否则跑一次预演，真正的发送会被推后一个间隔）。

    ``worker.runner`` 在执行器返回后也会调用它一次（幂等）：这样即使执行器是
    注入的假实现、或 task_runner 内部回填失败，契约列的最终值也一定正确。
    """
    if dry_run or not task:
        return False
    task_id = int(task.get("id") or 0)
    if not task_id:
        return False
    now = timeutil.utc_now_str()
    data: dict[str, Any] = {
        "last_run_at": now,
        "last_status": str(status or "")[:20],
        "updated_at": now,
    }
    if str(trigger_type) in ("manual", "retry"):
        data["next_run_at"] = _next_run_after(task, now)
    try:
        db.update("scheduled_tasks", data, "id=?", (task_id,))
        return True
    except Exception as exc:      # noqa: BLE001 - 缓存列回填失败不该影响执行结果
        sklog.debug(f"回填任务 {task_id} 的 last_run_at 失败：{exc}",
                    scope="worker", source="task.runner")
        return False


def _next_run_after(task: dict, now: str) -> str:
    """按任务配置算下一次执行时间（与 ``worker.scheduler.next_run_after`` 同一套规则）。

    这里不 import 调度器：那是 worker 层，automation 反向依赖它会形成方向混乱的
    耦合；真正共享的逻辑在 ``app.common.timeutil.next_run_time`` 里。
    """
    moment = timeutil.next_run_time(
        str(task.get("time_of_day") or "08:30"),
        str(task.get("timezone") or "Asia/Shanghai"),
        str(task.get("repeat_mode") or "daily"),
        task.get("weekdays"),
        int(task.get("interval_minutes") or 1440),
        after=timeutil.now_utc(),
        last_run_at=now,
    )
    return timeutil.to_db(moment)


def _run_real(outcome: RunOutcome, task: dict, account: dict, settings: Any,
              friends: list[dict], run_date: str, dry_run: bool, deadline: float,
              rng: random.Random) -> None:
    """真实浏览器路径。"""
    with session_store.AccountBrowser(account, settings, kind="task") as browser:
        cdp = browser.cdp
        state = douyin.read_login_state(cdp)
        if not state.get("logged_in"):
            _mark_account_status(account, "need_verify" if state.get("reason") ==
                                 "login_required_signal" else "offline",
                                 str(state.get("reason") or "未登录"))
            outcome.status = "failed"
            outcome.error_code = "not_logged_in"
            outcome.message = "账号登录态已失效，请重新登录后再执行"
            return
        opened = douyin.open_chat(
            cdp, timeout=min(30.0, max(10.0, deadline - time.monotonic())), settings=settings)
        # 发送续火花只认 https://www.douyin.com/chat?isPopup=1（见 douyin.CHAT_URL）。
        # 把实际 URL 记进日志：抖音若把页面重定向走（登录态失效等），一眼能看出来。
        # 注意 log() 的签名只有 (message, level, scope, user_id, source, context, persist)，
        # 没有 account_id 关键字 —— 传了会 TypeError，把整次任务打成「未预期错误」。
        sklog.info(f"私信页：{opened.get('url') or '(空)'} "
                   f"canonical={bool(opened.get('canonical'))} via={opened.get('via') or '-'}",
                   scope="worker", source="task.runner",
                   context={"account_id": account.get("id")})
        if not opened.get("ok"):
            outcome.status = "failed"
            outcome.error_code = "page_unavailable"
            outcome.message = f"打不开私信页：{opened.get('reason')}"
            outcome.items.append({"friend_name": "", "status": "failed", "message": "",
                                  "error_code": "page_unavailable",
                                  "error_detail": str(opened.get("reason") or "")})
            return
        conversations = douyin.harvest_conversations(cdp)
        targets, unmatched = match_conversations(friends, conversations)
        if conversations and not targets:
            # 页面有会话但一个都对不上：多半是抖音改版或昵称对不上，必须报失败
            outcome.error_code = "no_friend_matched"
            sklog.warning(
                "会话列表里没有匹配到任何好友（可能是页面改版或昵称不一致）："
                f"会话 {len(conversations)} 条，好友 {len(friends)} 个，"
                f"示例会话={[c.get('nickname') for c in conversations[:5]]}",
                scope="worker", source="task.matcher",
            )
        browser.save()
        _run_targets(outcome, targets, task, account, settings, run_date, dry_run,
                     deadline, rng, cdp=cdp, unmatched=unmatched)


def _run_targets(outcome: RunOutcome, targets: list[Target], task: dict, account: dict,
                 settings: Any, run_date: str, dry_run: bool, deadline: float,
                 rng: random.Random, *, cdp: Any, unmatched: list[dict]) -> None:
    """逐好友发送（真实与 demo 共用）。"""
    account_id = int(account.get("id") or 0)
    # demo 路径不重试：演示模式的失败是按昵称约定产生的**确定性**失败，
    # 重试只会白等退避时间、吃掉任务超时预算。
    if cdp is None:
        max_attempts = 1
    else:
        max_attempts = 1 + max(0, min(int(task.get("max_retry") or 0), MAX_FRIEND_ATTEMPTS - 1))
    outcome.total = len(targets) + len(unmatched)

    # 会话列表（虚拟滚动）里没匹配到的好友**不要直接跳过**：先尝试按昵称搜索打开会话。
    # 实机教训：好友会话可能排在列表很下面，抓取只覆盖前几屏 → 误判「不在会话里」。
    targets = list(targets) + [Target(friend=friend, conversation=None, matched_by="search")
                               for friend in unmatched]
    unmatched = []

    processed = 0
    for index, target in enumerate(targets):
        blocked = _process_target(outcome, target, task, account, settings, run_date,
                                  dry_run, deadline, rng, cdp=cdp, account_id=account_id,
                                  max_attempts=max_attempts)
        processed = index + 1
        if blocked == "timeout":
            # 超时是「这一轮没做完」，不是失败：剩下的好友记 skipped，
            # 让执行记录里的 total 与明细条数对得上（否则前端会显示「0/5」）。
            outcome.skipped += 1
            outcome.error_code = "task_timeout"
            outcome.message = outcome.message or "任务超时，剩余好友本轮未执行（下次会重试）"
            insert_item(outcome.run_id, friend_id=target.friend.get("id"),
                        friend_name=target.nickname, status="skipped",
                        error_code="task_timeout",
                        error_detail=f"任务超过 {settings.task_timeout_seconds}s 未完成")
            outcome.items.append({"friend_name": target.nickname, "status": "skipped",
                                  "message": "", "error_code": "task_timeout",
                                  "error_detail": "任务超时"})
            break

    remaining = len(targets) - processed
    if remaining > 0:
        outcome.skipped += remaining
        outcome.error_code = outcome.error_code or "task_timeout"
        outcome.message = outcome.message or f"任务超时，剩余 {remaining} 个好友未执行"
    _resolve_status(outcome)


def _process_target(outcome: RunOutcome, target: Target, task: dict, account: dict,
                    settings: Any, run_date: str, dry_run: bool, deadline: float,
                    rng: random.Random, *, cdp: Any, account_id: int,
                    max_attempts: int) -> str:
    """处理一个好友，返回 ``'ok' | 'timeout' | 'gap'``。"""
    friend = target.friend
    nickname = target.nickname
    friend_key = target.friend_key

    if time.monotonic() > deadline:
        return "timeout"

    # 幂等：今天已经发过就跳过（不消耗浏览器交互，也不写台账）
    if sent_today(account_id, friend_key, run_date):
        outcome.skipped += 1
        insert_item(outcome.run_id, friend_id=friend.get("id"), friend_name=nickname,
                    status="skipped", error_code="already_sent",
                    error_detail=f"{run_date} 已给「{nickname}」发过（幂等跳过）")
        outcome.items.append({"friend_name": nickname, "status": "skipped", "message": "",
                              "error_code": "already_sent",
                              "error_detail": "今天已发过"})
        _touch_friend(friend, "skipped")
        return "gap"

    message = build_message(task, target, rng)
    if dry_run:
        outcome.skipped += 1
        insert_item(outcome.run_id, friend_id=friend.get("id"), friend_name=nickname,
                    status="skipped", message=message, error_code="dry_run",
                    error_detail="预演模式：已匹配到会话，未实际发送")
        outcome.items.append({"friend_name": nickname, "status": "skipped", "message": message,
                              "error_code": "dry_run", "error_detail": "预演模式，未发送"})
        return "gap"

    item_started = time.monotonic()
    last_detail = ""
    last_code = "send_failed"
    for attempt in range(1, max_attempts + 1):
        if time.monotonic() > deadline:
            return "timeout"
        if cdp is None:                       # demo 路径
            ok, detail, code, via = _demo_send(nickname, message, settings, rng)
        else:
            # 先**打开这个好友的会话**：抖音私信页默认不选中任何会话，右侧输入框
            # 不存在；不点开就会一直报 input_failed。已打开过则这一步是空操作。
            opened_chat = douyin.open_conversation(cdp, nickname)
            if not opened_chat.get("ok"):
                sklog.warning(
                    f"打开会话失败：{nickname} → {opened_chat.get('reason')}",
                    scope="worker", source="task.runner",
                    context={"account_id": account_id, "header": opened_chat.get("header")})
                ok, detail, code, via = (
                    False,
                    f"打不开与「{nickname}」的会话：{opened_chat.get('reason')}",
                    "conversation_not_opened",
                    "",
                )
            else:
                if opened_chat.get("via") == "clicked_conversation" and opened_chat.get("header"):
                    sklog.debug(f"已打开会话：{opened_chat.get('header')}",
                                scope="worker", source="task.runner")
                result = send_message(cdp, message, rng)
                ok = bool(result.get("ok"))
                detail = str(result.get("detail") or "")
                code = str(result.get("error_code") or "")
                via = str(result.get("via") or "")
        if ok:
            duration = int((time.monotonic() - item_started) * 1000)
            inserted = mark_sent(account_id, friend, run_date, message, outcome.run_id,
                                 friend_key=friend_key)
            outcome.success += 1
            insert_item(outcome.run_id, friend_id=friend.get("id"), friend_name=nickname,
                        status="success", message=message, attempts=attempt,
                        duration_ms=duration,
                        error_detail=(via if inserted else f"{via}（台账已存在，未重复计数）"))
            outcome.items.append({"friend_name": nickname, "status": "success",
                                  "message": message, "error_code": "",
                                  "error_detail": via, "attempts": attempt})
            _touch_friend(friend, "success", sent=True)
            _sleep_gap(task, rng, deadline)
            return "gap"
        last_detail = detail
        last_code = code or "send_failed"
        if attempt < max_attempts:
            time.sleep(max(0.0, float(getattr(settings, "task_retry_backoff_seconds",
                                             RETRY_BACKOFF_SECONDS))))

    duration = int((time.monotonic() - item_started) * 1000)
    screenshot = save_screenshot(cdp, outcome.run_id, nickname, settings) if cdp else ""
    outcome.fail += 1
    insert_item(outcome.run_id, friend_id=friend.get("id"), friend_name=nickname,
                status="failed", message=message, error_code=last_code,
                error_detail=last_detail, attempts=max_attempts, duration_ms=duration,
                screenshot=screenshot)
    outcome.items.append({"friend_name": nickname, "status": "failed", "message": message,
                          "error_code": last_code, "error_detail": last_detail,
                          "attempts": max_attempts, "screenshot": screenshot})
    _touch_friend(friend, "failed")
    return "gap"


def _sleep_gap(task: dict, rng: random.Random, deadline: float) -> float:
    """好友之间的随机间隔，且**不会睡过任务超时预算**。

    随机值只取一次（先算再裁剪）：如果先判断再重新随机，实际等待时间就和
    决定「还要不要睡」时看到的不是同一个数，超时预算会被打破。
    """
    gap = humanize.delay_seconds(task.get("send_gap_seconds"), rng, default=(3.0, 9.0))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    gap = min(gap, remaining)
    if gap > 0:
        time.sleep(gap)
    return gap


def _demo_send(nickname: str, message: str, settings: Any,
               rng: random.Random) -> tuple[bool, str, str, str]:
    """demo_mode 的假发送：不碰浏览器，按昵称约定模拟成功/失败。

    约定：昵称里包含 ``失败``/``fail`` 的会返回失败，用来在演示与单测里
    覆盖 ``partial`` 状态；其余一律成功。注释里写清这是演示行为。
    """
    step = float(getattr(settings, "demo_step_seconds", 0.01) or 0.01)
    time.sleep(max(0.0, step))
    if "失败" in nickname or "fail" in nickname.lower():
        return False, "演示模式：模拟发送失败（昵称包含「失败」）", "blocked", "演示"
    if "验证" in nickname:
        return False, "演示模式：模拟需要验证", "verify_required", "演示"
    return True, f"演示模式：已发送「{message[:12]}」", "", "演示发送"


def _demo_match(friends: list[dict], account: dict) -> tuple[list[Target], list[dict]]:
    """demo_mode 下把好友全部视作已匹配（会话列表是假的，无法真匹配）。"""
    return [Target(friend=friend, conversation=None, matched_by="demo") for friend in friends], []


def _resolve_status(outcome: RunOutcome) -> None:
    """按计数推导最终状态。

    * 有成功、无失败 → ``success``
    * 有成功也有失败 → ``partial``
    * 全失败 → ``failed``
    * 一个都没发（全是跳过/预演）→ ``skipped``
    """
    if outcome.status not in ("running", ""):
        return
    if outcome.success and not outcome.fail:
        outcome.status = "success"
        outcome.message = outcome.message or f"完成，成功 {outcome.success} 个好友"
    elif outcome.success and outcome.fail:
        outcome.status = "partial"
        outcome.message = outcome.message or \
            f"部分完成：成功 {outcome.success}，失败 {outcome.fail}"
    elif outcome.fail:
        outcome.status = "failed"
        outcome.message = outcome.message or f"全部失败：{outcome.fail} 个好友"
    else:
        outcome.status = "skipped"
        outcome.message = outcome.message or "没有需要发送的好友（可能今天已经发过）"


def _mark_account_status(account: dict, status: str, error: str) -> None:
    account_id = int(account.get("id") or 0)
    if not account_id:
        return
    now = timeutil.utc_now_str()
    try:
        db.update("douyin_accounts",
                  {"status": status, "last_error": str(error)[:200],
                   "last_check_at": now, "updated_at": now},
                  "id=?", (account_id,))
    except Exception:            # noqa: BLE001 - 状态回填失败不影响业务结果
        pass


# --------------------------------------------------------------------------
# 供接口层使用的两个便捷入口
# --------------------------------------------------------------------------
def list_conversations(account: dict, settings: Any) -> dict:
    """抓会话列表（``/friends/sync`` 用）。

    实现放在 ``douyin.py``（那里才是「怎么在抖音上操作」），这里只做转发，
    让接口层既可以从 ``task_runner`` 也可以从 ``douyin.sync_friends`` 调用。
    """
    return douyin.list_conversations(account, settings, kind="sync")


def check_account(account: dict, settings: Any) -> dict:
    """检测账号登录态（转 ``douyin.check_account``）。"""
    return douyin.check_account(account, settings)
