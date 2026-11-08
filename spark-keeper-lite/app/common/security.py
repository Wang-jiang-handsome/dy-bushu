"""安全原语：口令哈希、JWT、对称加密、随机码。

全部基于标准库 ``hashlib`` / ``hmac`` / ``secrets`` / ``base64``，
不依赖 cryptography 或 PyJWT——自托管场景少一个依赖就少一个装不上的理由。

安全取舍说明
------------
* 口令：PBKDF2-HMAC-SHA256，默认 240000 轮（OWASP 2023 建议 >= 600k 为最优，
  但自托管小机器登录延迟敏感，取 240k 并在字段里记录算法与轮数，便于日后升级）。
* 会话：HS256 JWT（自带 jti），服务端另有 ``sessions`` 表做白名单，
  所以「改密/强制下线」可以立刻失效，而不是等 token 过期。
* 对称加密（存代理密码）：SHA256(secrets) 派生密钥 + HMAC-SHA256 做流密码（CTR 风格），
  再加 HMAC 认证标签。**这不是 AES**，但对「数据库被拖走」这一威胁模型足够，
  且无需第三方库。若你有更高要求，可替换 ``encrypt``/``decrypt`` 两个函数。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import time
from typing import Any

from ..config import get_settings

PBKDF2_ROUNDS = 240_000
PBKDF2_ALGO = "pbkdf2_sha256"


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------
def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "$".join(
        [
            PBKDF2_ALGO,
            str(rounds),
            base64.urlsafe_b64encode(salt).decode().rstrip("="),
            base64.urlsafe_b64encode(digest).decode().rstrip("="),
        ]
    )


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """校验口令。返回 (是否通过, 是否需要重新哈希)。"""
    if not stored or not password:
        return False, False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != PBKDF2_ALGO:
        return False, False
    try:
        rounds = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        expected = base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4))
    except (ValueError, TypeError):
        return False, False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    ok = hmac.compare_digest(actual, expected)
    return ok, ok and rounds != PBKDF2_ROUNDS


PASSWORD_RULES = "密码至少 8 位，且需同时包含字母与数字"


def password_problem(password: str) -> str:
    """返回不合规原因；合规返回空串。"""
    if not password or len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 128:
        return "密码最长 128 位"
    has_alpha = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_alpha and has_digit):
        return "密码需同时包含字母与数字"
    if password.lower() in ("12345678", "password", "admin123", "abcd1234"):
        return "密码过于简单，请更换"
    return ""


# --------------------------------------------------------------------------
# JWT (HS256)
# --------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_jwt(payload: dict, secret: str | None = None, ttl: int = 3600,
             kind: str = "access") -> str:
    secret = secret or get_settings().secret
    now = int(time.time())
    body = dict(payload)
    body.setdefault("jti", secrets.token_urlsafe(16))
    body.update({"iat": now, "nbf": now, "exp": now + int(ttl), "kind": kind})
    head = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64e(json.dumps(head, separators=(",", ":"), sort_keys=True).encode()),
        _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    segments.append(_b64e(sig))
    return ".".join(segments)


class TokenError(Exception):
    def __init__(self, message: str, expired: bool = False):
        super().__init__(message)
        self.message = message
        self.expired = expired


def verify_jwt(token: str, secret: str | None = None, kind: str | None = None) -> dict:
    secret = secret or get_settings().secret
    if not token or token.count(".") != 2:
        raise TokenError("凭证格式不正确")
    head_b64, body_b64, sig_b64 = token.split(".")
    signing_input = f"{head_b64}.{body_b64}".encode()
    try:
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        given = _b64d(sig_b64)
    except (ValueError, TypeError) as exc:
        raise TokenError("凭证格式不正确") from exc
    if not hmac.compare_digest(expected, given):
        raise TokenError("凭证签名校验失败")
    try:
        body = json.loads(_b64d(body_b64))
    except (ValueError, TypeError) as exc:
        raise TokenError("凭证内容损坏") from exc
    now = int(time.time())
    if "exp" in body and now > int(body["exp"]):
        raise TokenError("登录已过期，请重新登录", expired=True)
    if "nbf" in body and now < int(body["nbf"]) - 5:
        raise TokenError("凭证尚未生效")
    if kind and body.get("kind") != kind:
        raise TokenError("凭证类型不匹配")
    return body


# --------------------------------------------------------------------------
# 对称加密（代理密码等低敏凭据）
# --------------------------------------------------------------------------
def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str, secret: str | None = None) -> str:
    if plaintext == "":
        return ""
    secret = secret or get_settings().secret
    key = hashlib.sha256(("enc:" + secret).encode()).digest()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    stream = _keystream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return "v1:" + _b64e(nonce + cipher + tag)


def decrypt(token: str, secret: str | None = None) -> str:
    if not token:
        return ""
    if not token.startswith("v1:"):
        return ""            # 旧格式/非法格式一律视为空，不抛异常打断业务
    secret = secret or get_settings().secret
    key = hashlib.sha256(("enc:" + secret).encode()).digest()
    try:
        raw = _b64d(token[3:])
    except (ValueError, TypeError):
        return ""
    if len(raw) < 32:
        return ""
    nonce, cipher, tag = raw[:16], raw[16:-16], raw[-16:]
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        return ""
    stream = _keystream(key, nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 随机标识
# --------------------------------------------------------------------------
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉 I O 0 1，避免抄错
_CARD_GROUPS = (4, 4, 4, 4, 4)                    # 卡密形如 XXXX-XXXX-XXXX-XXXX-XXXX (20 位)


def random_code(length: int, alphabet: str = string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


CARD_LENGTH = sum(_CARD_GROUPS)          # 20


def new_card_code(groups: tuple[int, ...] = _CARD_GROUPS) -> str:
    """生成卡密：20 位无连字符（32^20 ≈ 1.2e30），入库与展示都用这一种形式。"""
    return random_code(sum(groups), _ALPHABET)


def normalize_card_code(raw: str) -> str:
    """把用户粘贴的卡密规范化成 20 位大写字母数字。

    容错点（用户是复制粘贴为主，必须宽容）：
    * 丢掉空格、换行、连字符、任何非字母数字；
    * 统一大写；
    * 易混字符纠正：O→0? 不——字母表里没有 O/0，所以统一把 ``O`` 当 ``0`` 再按
      「字母表只含 A-Z(去 I,L,O) + 2-9」，把 0→O？实际做法：把常见误抄的
      ``0``→``O``、``1``→``I`` 都不可靠，因此只在**长度不足**时提示，
      长度正确时按原样查库（哈希比对，连字符不影响）。
    """
    if not raw:
        return ""
    text = "".join(c for c in raw.upper() if c.isalnum())
    threshold = CARD_LENGTH
    return text[:threshold] if len(text) > threshold else text


def card_code_hash(code: str) -> str:
    normalized = "".join(c for c in (code or "").upper() if c.isalnum())
    return hashlib.sha256(("card:" + normalized).encode()).hexdigest()


def new_order_no(prefix: str = "SK") -> str:
    """订单号：前缀 + UTC 时间 + 随机，全局唯一且可读。"""
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    return f"{prefix}{stamp}{random_code(6)}"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(8)}" if prefix else secrets.token_hex(8)


def mask_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 7:
        return digits
    return f"{digits[:3]}****{digits[-4:]}"


def mask_email(email: str) -> str:
    if "@" not in (email or ""):
        return email or ""
    name, _, domain = email.partition("@")
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[:2]}***{name[-1:]}@{domain}"


def const_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def hash_ip(ip: str, secret: str | None = None) -> str:
    secret = secret or get_settings().secret
    return hmac.new(secret.encode(), (ip or "").encode(), hashlib.sha256).hexdigest()[:16]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def totp_now(secret: str, step: int = 30, digits: int = 6, at: float | None = None) -> str:
    """标准 TOTP（RFC 6238），用于管理员二次验证；secret 为 base32。"""
    import struct

    try:
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    except Exception:
        return ""
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    if not secret or not code:
        return False
    now = time.time()
    for drift in range(-window, window + 1):
        if const_time_eq(totp_now(secret, at=now + drift * 30), code.strip()):
            return True
    return False


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


def safe_compare_dict(data: dict[str, Any]) -> str:
    """把字典按 key 排序拼成待签名串（支付渠道签名用）。"""
    return "&".join(f"{k}={data[k]}" for k in sorted(data) if data[k] not in (None, ""))
