"""登录 / 会话 / 验证码的领域逻辑（接口层只做参数校验与响应拼装）。"""

from __future__ import annotations

import json
import secrets
from datetime import timedelta

from .. import db
from ..config import get_settings
from ..router import HttpError
from . import logging as logmod
from . import mailer, timeutil
from .security import (
    TokenError,
    hash_password,
    new_id,
    password_problem,
    sha256_hex,
    verify_jwt,
    verify_password,
)

VERIFY_CODE_TTL_SECONDS = 600
VERIFY_MAX_ATTEMPTS = 5
RESET_TOKEN_TTL = 1800

# 固定的假口令哈希（轮数/盐固定），用于「用户不存在」分支的等时比较
_DUMMY_HASH = (
    "pbkdf2_sha256$240000$AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


# --------------------------------------------------------------------------
# 令牌
# --------------------------------------------------------------------------
def issue_tokens(user: dict, *, ip: str = "", user_agent: str = "",
                 refresh_ttl: int | None = None, access_ttl: int | None = None) -> dict:
    """签发 access/refresh token 并写入 sessions 白名单。

    ``refresh_ttl`` / ``access_ttl`` 用于「记住我」这类场景按需延长有效期；
    不传则走 ``SK_ACCESS_TTL`` / ``SK_REFRESH_TTL``。
    """
    from .security import sign_jwt

    cfg = get_settings()
    access_ttl = int(access_ttl or cfg.access_token_ttl)
    refresh_ttl = int(refresh_ttl or cfg.refresh_token_ttl)
    now = timeutil.utc_now_str()
    access_jti, refresh_jti = new_id("a_"), new_id("r_")
    access = sign_jwt(
        {"sub": str(user["id"]), "role": user["role"], "jti": access_jti},
        ttl=access_ttl,
        kind="access",
    )
    refresh = sign_jwt(
        {"sub": str(user["id"]), "role": user["role"], "jti": refresh_jti},
        ttl=refresh_ttl,
        kind="refresh",
    )
    db.insert(
        "sessions",
        {
            "user_id": user["id"],
            "jti": access_jti,
            "token_kind": "access",
            "issued_at": now,
            "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=access_ttl)),
            "user_agent": (user_agent or "")[:300],
            "ip": ip,
        },
    )
    db.insert(
        "sessions",
        {
            "user_id": user["id"],
            "jti": refresh_jti,
            "token_kind": "refresh",
            "issued_at": now,
            "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=refresh_ttl)),
            "user_agent": (user_agent or "")[:300],
            "ip": ip,
        },
    )
    return {
        "token": access,
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
        "token_type": "Bearer",
    }


# 「记住我」把 refresh 有效期延长到 30 天（access 仍保持短有效期，安全性不变）
REMEMBER_REFRESH_TTL = 30 * 24 * 60 * 60


def refresh_access_token(refresh_token: str, *, ip: str = "", user_agent: str = "") -> dict:
    cfg = get_settings()
    try:
        payload = verify_jwt(refresh_token, kind="refresh")
    except TokenError as exc:
        raise HttpError(401, exc.message) from exc
    session = db.query_one(
        "SELECT * FROM sessions WHERE jti=? AND revoked_at IS NULL", (payload.get("jti"),)
    )
    if not session:
        raise HttpError(401, "登录状态已失效，请重新登录")
    if timeutil.is_expired(session.get("expire_at")):
        raise HttpError(401, "登录已过期，请重新登录")
    user = db.query_one("SELECT * FROM users WHERE id=?", (payload.get("sub"),))
    if not user or user.get("status") != "active":
        raise HttpError(401, "账号不可用")
    # 轮换：旧 refresh 立即作废；长会话（记住我）保持原有的长有效期
    db.update("sessions", {"revoked_at": timeutil.utc_now_str()}, "jti=?", (payload.get("jti"),))
    old_expire = timeutil.parse_db(session.get("expire_at"))
    remaining = 0
    if old_expire:
        remaining = int((old_expire - timeutil.now_utc()).total_seconds())
    keep_long = remaining > cfg.refresh_token_ttl
    tokens = issue_tokens(
        user,
        ip=ip,
        user_agent=user_agent,
        refresh_ttl=max(remaining, cfg.refresh_token_ttl) if keep_long else None,
    )
    tokens["user"] = public_user(user)
    return tokens


def revoke_session(token: str) -> None:
    try:
        payload = verify_jwt(token)
    except TokenError:
        return
    db.update("sessions", {"revoked_at": timeutil.utc_now_str()}, "jti=?", (payload.get("jti"),))


def revoke_all_sessions(user_id: int) -> int:
    return db.update(
        "sessions",
        {"revoked_at": timeutil.utc_now_str()},
        "user_id=? AND revoked_at IS NULL",
        (user_id,),
    )


def public_user(user: dict) -> dict:
    """返回给前端的用户信息（绝不包含 hash）。"""
    quota = None
    try:
        from .quota import Quota

        quota = Quota.for_user(user["id"]).summary()
    except HttpError:
        quota = None
    return {
        "id": user["id"],
        "username": user["username"],
        "nickname": user.get("nickname") or user["username"],
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "role": user.get("role"),
        "status": user.get("status"),
        "avatar": user.get("avatar") or "",
        "balance_cents": int(user.get("balance_cents") or 0),
        "package_id": user.get("package_id"),
        "package_expire_at": user.get("package_expire_at"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "quota": quota,
    }


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
LOGIN_LOCK_MESSAGE = "失败次数过多，请稍后再试"


def authenticate(identifier: str, password: str, *, ip: str = "", user_agent: str = "") -> dict:
    """账号密码登录。identifier 支持 用户名 / 邮箱 / 手机号。

    失败信息统一为「账号或密码错误」，避免用户名枚举。
    """
    from ..http_server import LoginGuard      # 复用服务器上的锁定器（若有）

    identifier = (identifier or "").strip()
    if not identifier or not password:
        raise HttpError(400, "请输入账号与密码")

    guard: LoginGuard | None = None
    server = getattr(authenticate, "_server", None)
    if server is not None and hasattr(server, "login_guard"):
        guard = server.login_guard
    lock_key = f"{identifier.lower()}|{ip}"
    if guard is not None:
        wait = guard.locked_for(lock_key)
        if wait > 0:
            raise HttpError(429, f"{LOGIN_LOCK_MESSAGE}（约 {wait} 秒后重试）")

    user = db.query_one(
        "SELECT * FROM users WHERE username=? OR (email IS NOT NULL AND email=?)"
        " OR (phone IS NOT NULL AND phone=?)",
        (identifier, identifier.lower(), identifier),
    )
    ok = False
    need_rehash = False
    if user:
        ok, need_rehash = verify_password(password, user["password_hash"])
    else:
        # 用户不存在时也走一次 PBKDF2，抹平「用户存在与否」的响应时间差
        verify_password(password, _DUMMY_HASH)

    if not ok:
        if guard is not None:
            guard.record_fail(lock_key)
        logmod.log(
            f"登录失败：{identifier}",
            level="warning",
            scope="system",
            source="auth",
            context={"ip": ip},
        )
        raise HttpError(401, "账号或密码错误")

    if user.get("status") != "active":
        raise HttpError(403, "账号已被禁用，请联系管理员")

    if need_rehash:
        db.update(
            "users",
            {"password_hash": hash_password(password), "password_algo": "pbkdf2_sha256"},
            "id=?",
            (user["id"],),
        )
    if guard is not None:
        guard.reset(lock_key)

    db.update(
        "users",
        {"last_login_at": timeutil.utc_now_str(), "last_login_ip": ip},
        "id=?",
        (user["id"],),
    )
    logmod.audit("login", user=user, detail=f"ip={ip}", ip=ip, user_agent=user_agent)
    return user


def register(*, username: str, password: str, email: str = "", phone: str = "",
             nickname: str = "", ip: str = "", user_agent: str = "") -> dict:
    """注册。用户名唯一；邮箱/手机号可选但填了就必须唯一。"""
    problem = password_problem(password)
    if problem:
        raise HttpError(400, problem)
    now = timeutil.utc_now_str()
    with db.transaction():
        if db.query_one("SELECT id FROM users WHERE username=?", (username,)):
            raise HttpError(409, "该用户名已被注册")
        if email and db.query_one("SELECT id FROM users WHERE email=?", (email,)):
            raise HttpError(409, "该邮箱已被注册")
        if phone and db.query_one("SELECT id FROM users WHERE phone=?", (phone,)):
            raise HttpError(409, "该手机号已被注册")
        user_id = db.insert(
            "users",
            {
                "email": email or None,
                "phone": phone or None,
                "username": username,
                "password_hash": hash_password(password),
                "role": "user",
                "status": "active",
                "nickname": nickname or username,
                "balance_cents": 0,
                "commission_rate": 0,
                "quota_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )
    user = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    logmod.audit("register", user=user, detail=f"ip={ip}", ip=ip, user_agent=user_agent)
    logmod.notify(
        user_id,
        "欢迎使用抖音续火花系统",
        "第一步：在「卡密兑换」输入卡密激活套餐，或在「套餐购买」下单。\n"
        "第二步：到「抖音账号」扫码绑定账号。\n"
        "第三步：在「定时任务」里设置每天的续火花时间。",
    )
    return user


# --------------------------------------------------------------------------
# 验证码
# --------------------------------------------------------------------------
def create_verify_code(*, scene: str, channel: str, target: str, ip: str = "") -> tuple[str, int]:
    """生成验证码并入队发送。返回 (明文码, 过期秒数)。"""
    if scene not in ("register", "reset_password", "login", "bind"):
        raise HttpError(400, "验证码场景不正确")
    if channel not in ("email", "sms"):
        raise HttpError(400, "验证码通道不正确")

    recent = db.query_one(
        "SELECT created_at FROM verify_codes WHERE target=? AND scene=? ORDER BY id DESC LIMIT 1",
        (target, scene),
    )
    if recent:
        created = timeutil.parse_db(recent["created_at"])
        elapsed = (timeutil.now_utc() - created).total_seconds() if created else 999
        if elapsed < 60:
            raise HttpError(429, f"验证码发送过于频繁，请 {int(60 - elapsed)} 秒后再试")

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    now = timeutil.utc_now_str()
    db.insert(
        "verify_codes",
        {
            "scene": scene,
            "channel": channel,
            "target": target,
            "code_hash": sha256_hex(f"{scene}:{target}:{code}"),
            "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=VERIFY_CODE_TTL_SECONDS)),
            "created_at": now,
            "ip": ip,
        },
    )
    if channel == "email":
        mailer.send_verify_code_email(target, code, scene)
    else:
        mailer.send_verify_code_sms(target, code, scene)
    return code, VERIFY_CODE_TTL_SECONDS


def check_verify_code(*, scene: str, channel: str, target: str, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        raise HttpError(400, "请输入验证码")
    row = db.query_one(
        "SELECT * FROM verify_codes WHERE target=? AND scene=? AND channel=? AND used_at IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (target, scene, channel),
    )
    if not row:
        raise HttpError(400, "请先获取验证码")
    if timeutil.is_expired(row["expire_at"]):
        raise HttpError(400, "验证码已过期，请重新获取")
    if int(row["attempts"] or 0) >= VERIFY_MAX_ATTEMPTS:
        raise HttpError(429, "验证码错误次数过多，请重新获取")
    if sha256_hex(f"{scene}:{target}:{code}") != row["code_hash"]:
        db.update("verify_codes", {"attempts": int(row["attempts"] or 0) + 1}, "id=?", (row["id"],))
        raise HttpError(400, "验证码不正确")
    db.update("verify_codes", {"used_at": timeutil.utc_now_str()}, "id=?", (row["id"],))
    return True


# --------------------------------------------------------------------------
# 改密 / 重置
# --------------------------------------------------------------------------
def change_password(user: dict, old_password: str, new_password: str, *, ip: str = "") -> None:
    ok, _ = verify_password(old_password, user["password_hash"])
    if not ok:
        raise HttpError(400, "原密码不正确")
    problem = password_problem(new_password)
    if problem:
        raise HttpError(400, problem)
    if old_password == new_password:
        raise HttpError(400, "新密码不能与原密码相同")
    db.update(
        "users",
        {"password_hash": hash_password(new_password), "updated_at": timeutil.utc_now_str()},
        "id=?",
        (user["id"],),
    )
    revoked = revoke_all_sessions(user["id"])
    logmod.audit("change_password", user=user, detail=f"撤销会话 {revoked} 个", ip=ip)


def create_reset_token(user: dict) -> str:
    from .security import sign_jwt

    return sign_jwt(
        {"sub": str(user["id"]), "purpose": "reset_password", "jti": new_id("p_")},
        ttl=RESET_TOKEN_TTL,
        kind="reset",
    )


def consume_reset_token(token: str, new_password: str, *, ip: str = "") -> dict:
    try:
        payload = verify_jwt(token, kind="reset")
    except TokenError as exc:
        raise HttpError(400, exc.message) from exc
    if payload.get("purpose") != "reset_password":
        raise HttpError(400, "重置凭证无效")
    problem = password_problem(new_password)
    if problem:
        raise HttpError(400, problem)
    user = db.query_one("SELECT * FROM users WHERE id=?", (payload.get("sub"),))
    if not user:
        raise HttpError(404, "用户不存在")
    db.update(
        "users",
        {"password_hash": hash_password(new_password), "updated_at": timeutil.utc_now_str()},
        "id=?",
        (user["id"],),
    )
    revoke_all_sessions(user["id"])
    logmod.audit("reset_password", user=user, ip=ip)
    return user


def find_by_identifier(identifier: str) -> dict | None:
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    return db.query_one(
        "SELECT * FROM users WHERE username=? OR (email IS NOT NULL AND email=?)"
        " OR (phone IS NOT NULL AND phone=?)",
        (identifier, identifier.lower(), identifier),
    )


def dumps_extra(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
