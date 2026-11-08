"""JSON 字段读写与文案处理。

SQLite 没有 JSON 列，所有列表/字典字段都以 TEXT 存。这里提供容错读写，
以及续火花文案的渲染（模板变量 + 随机挑选 + 一致性校验）。
"""

from __future__ import annotations

import json
import random
import re
from typing import Any, Iterable

_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

DEFAULT_MESSAGES = [
    "火花不能断 🔥",
    "打卡，续上今天的火花 ✨",
    "今天也要续火花哦 🔥",
    "来续火花啦，别断 🔥",
    "火花 +1，天天见 ✨",
]

# 影子内容/外链等容易被平台判为营销或违规的模式（仅做提示，不强制拦截）
RISKY_PATTERNS = (
    (re.compile(r"https?://", re.I), "包含外链，容易被判定为营销内容"),
    (re.compile(r"(加|扫)(我|微信|vx|VX|QQ)", re.I), "疑似引流话术"),
    (re.compile(r"\d{3,}\s*元|代刷|返利|兼职", re.I), "疑似广告内容"),
)


def loads(text: Any, fallback: Any = None) -> Any:
    if text is None or text == "":
        return fallback if fallback is not None else []
    if isinstance(text, (list, dict)):
        return text
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return fallback if fallback is not None else []
    return value if value is not None else (fallback if fallback is not None else [])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        parsed = loads(value, None)
        if isinstance(parsed, list):
            return parsed
        return [v.strip() for v in re.split(r"[\n,;，；]", value) if v.strip()]
    return [value]


def as_str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in as_list(value) if str(v).strip()]


def as_int_list(value: Any) -> list[int]:
    out: list[int] = []
    for item in as_list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def as_dict(value: Any, fallback: dict | None = None) -> dict:
    parsed = loads(value, None)
    if isinstance(parsed, dict):
        return parsed
    return dict(fallback or {})


def as_min_max(value: Any, default: tuple[int, int] = (3, 9)) -> tuple[int, int]:
    data = as_dict(value, {})
    try:
        low = int(data.get("min", default[0]))
        high = int(data.get("max", default[1]))
    except (TypeError, ValueError):
        low, high = default
    low = max(1, low)
    high = max(low, high)
    return low, high


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------
def render_template(text: str, variables: dict) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        value = variables.get(key)
        return str(value) if value is not None else match.group(0)

    return _TEMPLATE_RE.sub(repl, text or "")


def pick_message(messages: Iterable[str], variables: dict | None = None,
                 rng: random.Random | None = None) -> str:
    """从候选文案里随机挑一条并渲染变量。空列表回落到默认文案。"""
    pool = [m for m in (messages or []) if str(m).strip()]
    if not pool:
        pool = list(DEFAULT_MESSAGES)
    rng = rng or random
    chosen = rng.choice(pool)
    return render_template(str(chosen).strip(), variables or {})


def message_warnings(messages: Iterable[str]) -> list[str]:
    out: list[str] = []
    for text in messages or []:
        for pattern, hint in RISKY_PATTERNS:
            if pattern.search(str(text)):
                out.append(f"「{str(text)[:20]}…」{hint}")
                break
    return out


def preview_messages(messages: Iterable[str], variables: dict | None = None, count: int = 3) -> list[str]:
    rng = random.Random(20260816)
    pool = as_str_list(messages) or list(DEFAULT_MESSAGES)
    return [render_template(rng.choice(pool), variables or {}) for _ in range(max(1, count))]


def truncate(text: str, limit: int = 200, suffix: str = "…") -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - len(suffix))] + suffix


def normalize_name(value: str) -> str:
    """好友昵称比对用的规范化：去空白、去 emoji 影响、统一大小写。"""
    text = re.sub(r"\s+", "", str(value or ""))
    return text.casefold()


def redact(text: str) -> str:
    """日志脱敏：cookie/token/密码等关键值只留头尾。"""
    if not text:
        return ""
    def _mask(match: re.Match) -> str:
        value = match.group(2)
        if len(value) <= 8:
            return f"{match.group(1)}=***"
        return f"{match.group(1)}={value[:4]}***{value[-2:]}"

    return re.sub(r"(?i)\b(cookie|token|password|secret|sessionid|authorization)\b\s*[:=]\s*([^\s;,]+)", _mask, str(text))
