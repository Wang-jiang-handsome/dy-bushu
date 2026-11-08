"""输入校验：邮箱/手机号/用户名/整数范围。全部返回 (ok, 规范化值或错误信息)。"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")
PHONE_CN_RE = re.compile(r"^1[3-9]\d{9}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5\-]{3,24}$")
DOUYIN_UID_RE = re.compile(r"^[A-Za-z0-9_\-.]{2,64}$")
SEC_UID_RE = re.compile(r"^MS4wLjABAAAA[A-Za-z0-9_\-]{10,120}$")


def email(raw: str) -> tuple[bool, str]:
    value = (raw or "").strip().lower()
    if not value:
        return False, "邮箱不能为空"
    if len(value) > 120 or not EMAIL_RE.match(value):
        return False, "邮箱格式不正确"
    return True, value


def phone(raw: str) -> tuple[bool, str]:
    value = "".join(c for c in (raw or "") if c.isdigit() or c == "+")
    if value.startswith("+86"):
        value = value[3:]
    if not PHONE_CN_RE.match(value):
        return False, "手机号格式不正确"
    return True, value


def username(raw: str) -> tuple[bool, str]:
    value = (raw or "").strip()
    if not USERNAME_RE.match(value):
        return False, "用户名需 3~24 位，可使用中英文、数字、下划线和短横线"
    return True, value


def sec_uid(raw: str) -> tuple[bool, str]:
    value = (raw or "").strip()
    if not value:
        return True, ""
    if not SEC_UID_RE.match(value):
        return False, "sec_uid 格式不正确（应以 MS4wLjABAAAA 开头）"
    return True, value


def douyin_uid(raw: str) -> tuple[bool, str]:
    value = (raw or "").strip().lstrip("@")
    if not value:
        return True, ""
    if not DOUYIN_UID_RE.match(value):
        return False, "抖音号格式不正确"
    return True, value


def host_port(raw: str, default_port: int = 0) -> tuple[bool, tuple[str, int]]:
    value = (raw or "").strip()
    if not value:
        return False, ("", default_port)
    if value.startswith("[") and "]" in value:      # IPv6
        host, _, rest = value[1:].partition("]")
        port = rest.lstrip(":")
    elif ":" in value:
        host, _, port = value.rpartition(":")
    else:
        host, port = value, str(default_port)
    try:
        port_num = int(port or default_port)
    except ValueError:
        return False, (host, default_port)
    if not host or not (0 < port_num < 65536):
        return False, (host, default_port)
    return True, (host, port_num)


def one_of(value: str, allowed: tuple[str, ...], default: str = "") -> str:
    value = (value or "").strip()
    return value if value in allowed else default


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def looks_like_url(raw: str) -> bool:
    value = (raw or "").strip().lower()
    return value.startswith(("http://", "https://"))


def safe_filename(raw: str, fallback: str = "file") -> str:
    value = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fa5]", "_", (raw or "").strip())
    value = value.strip("._") or fallback
    return value[:80]


def positive_int(raw, default: int = 0) -> int:
    try:
        num = int(raw)
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default
