"""时间处理：统一 UTC 存储 + 按任务时区计算「下一次执行时间」。

约定
----
* 数据库里所有 ``*_at`` 都是 **UTC** 的 ``'YYYY-MM-DD HH:MM:SS'`` 文本。
* ``run_date`` / ``time_of_day`` 按任务自己的时区解释（默认 Asia/Shanghai）。
* 只用标准库 ``zoneinfo``；Windows 上 ``tzdata`` 可能缺失，所以内置了
  常见时区的固定偏移兜底，取不到时区时不会崩，只是不带夏令时。
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timedelta, timezone  # noqa: F401  (timedelta 供其它模块转发使用)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DB_FMT = "%Y-%m-%d %H:%M:%S"
FALLBACK_OFFSETS = {          # 小时偏移，仅在中国/亚洲常用时区兜底
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Singapore": 8,
    "UTC": 0,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def utc_now_str() -> str:
    return now_utc().strftime(DB_FMT)


def to_db(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(DB_FMT)


def parse_db(text: str | None) -> datetime | None:
    """解析数据库里的时间字符串；容错常见的 ``T`` 与毫秒格式。"""
    if not text:
        return None
    raw = str(text).strip().replace("T", " ")
    if raw.endswith("Z"):
        raw = raw[:-1]
    if "." in raw:
        raw = raw.split(".")[0]
    for fmt in (DB_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def get_tz(name: str | None) -> timezone | ZoneInfo:
    name = (name or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        offset = FALLBACK_OFFSETS.get(name, 8)
        return timezone(timedelta(hours=offset))


def local_now(tz_name: str | None = None) -> datetime:
    return now_utc().astimezone(get_tz(tz_name))


def local_date_str(tz_name: str | None = None, at: datetime | None = None) -> str:
    moment = at or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(get_tz(tz_name)).strftime("%Y-%m-%d")


def parse_hhmm(text: str) -> tuple[int, int]:
    """解析 ``HH:MM``；非法输入回落到 08:30（并保证不抛异常，调度器不能因此停摆）。"""
    raw = (text or "").strip().replace("：", ":")
    try:
        hour, _, minute = raw.partition(":")
        h, m = int(hour), int(minute or 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    return 8, 30


def add_days(days: int, base: datetime | None = None) -> datetime:
    return (base or now_utc()) + timedelta(days=int(days))


def add_days_str(days: int, base: datetime | None = None) -> str:
    return to_db(add_days(days, base))


def days_between(later: datetime | None, earlier: datetime | None) -> int:
    if not later or not earlier:
        return 0
    return max(0, (later - earlier).days)


def is_expired(text: str | None) -> bool:
    dt = parse_db(text)
    return bool(dt and dt < now_utc())


def humanize_delta(text: str | None) -> str:
    """把到期时间变成「还剩 12 天」这类人话。"""
    dt = parse_db(text)
    if not dt:
        return "永久"
    seconds = (dt - now_utc()).total_seconds()
    if seconds <= 0:
        return "已过期"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    if days >= 1:
        return f"还剩 {days} 天"
    if hours >= 1:
        return f"还剩 {hours} 小时"
    return f"还剩 {max(1, int(seconds // 60))} 分钟"


# --------------------------------------------------------------------------
# 下次执行时间
# --------------------------------------------------------------------------
WEEKDAY_ALIASES = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "0": 7,
}


def parse_weekdays(raw) -> list[int]:
    """把 ``["1","5"]`` / ``"1,5"`` / ``"mon,fri"`` 统一成 ISO 星期列表（1=周一）。"""
    if raw in (None, "", []):
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).replace("，", ",").split(",")
    out: list[int] = []
    for item in items:
        key = str(item).strip().lower()
        if key in WEEKDAY_ALIASES:
            day = WEEKDAY_ALIASES[key]
            if day not in out:
                out.append(day)
    return sorted(out)


def next_run_time(
    time_of_day: str,
    tz_name: str | None = None,
    repeat_mode: str = "daily",
    weekdays=None,
    interval_minutes: int = 1440,
    after: datetime | None = None,
    last_run_at: str | None = None,
) -> datetime:
    """计算下一次执行时刻（返回 UTC datetime）。

    规则
    ----
    * ``daily``：每天 ``time_of_day``；若今天该时刻已过，则顺延到明天。
    * ``weekdays`` / ``weekends``：只在周一~周五 / 周六周日执行。
    * ``custom``：只在 ``weekdays`` 指定的星期执行。
    * ``interval``：从「上次执行时间 + interval」算起；没有上次执行时间则用
      今天/明天的 ``time_of_day`` 作为锚点。
    * 结果一定晚于 ``after``（默认当前时间），否则调度器会陷入「立刻又到期」的循环。
    """
    tz = get_tz(tz_name)
    reference = after or now_utc()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    hour, minute = parse_hhmm(time_of_day)

    if repeat_mode == "interval":
        minutes = max(1, int(interval_minutes or 1440))
        base = parse_db(last_run_at)
        if base:
            candidate = base + timedelta(minutes=minutes)
            if candidate <= reference:
                # 落后太多（服务停机过）就按间隔往前推到未来最近的窗口
                behind = (reference - candidate).total_seconds() / 60.0
                steps = int(behind // minutes) + 1
                candidate = candidate + timedelta(minutes=minutes * steps)
            return candidate
        local_today = reference.astimezone(tz)
        anchor = local_today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if anchor <= local_today:
            anchor = anchor + timedelta(minutes=minutes)
        return anchor.astimezone(UTC)

    days = parse_weekdays(weekdays)
    if repeat_mode == "weekdays":
        days = [1, 2, 3, 4, 5]
    elif repeat_mode == "weekends":
        days = [6, 7]

    local = reference.astimezone(tz)
    for offset in range(0, 400):
        candidate_local = (local + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=int(reference.microsecond // 1_000_000 * 0), microsecond=0
        )
        if offset == 0 and candidate_local <= local:
            continue
        if days and candidate_local.isoweekday() not in days:
            continue
        return candidate_local.astimezone(UTC)
    # 理论上不会到这里（400 天足够），兜底为明天同一时刻
    return (local + timedelta(days=1)).astimezone(UTC)


def in_time_window(now_local: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """判断当前本地时间是否落在 [start, end) 窗口内（跨零点也支持）。"""
    sh, sm = parse_hhmm(start_hhmm)
    eh, em = parse_hhmm(end_hhmm)
    minutes = now_local.hour * 60 + now_local.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def month_range(anchor: date | None = None) -> tuple[date, date]:
    anchor = anchor or now_utc().date()
    first = anchor.replace(day=1)
    last = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    return first, last


def seconds_until(dt: datetime | None) -> int:
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0, int((dt - now_utc()).total_seconds()))


def recent_dates(days: int = 7, tz_name: str | None = None) -> list[str]:
    today = local_now(tz_name).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def ts() -> float:
    return time.time()


def format_local(text: str | None, tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = parse_db(text)
    if not dt:
        return ""
    return dt.astimezone(get_tz(tz_name)).strftime(fmt)
