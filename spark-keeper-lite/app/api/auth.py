"""认证接口（契约 §2）：公开配置、验证码、注册、登录、刷新、退出、找回密码。

职责边界
--------
本模块只做「参数校验 → 调 ``app/common/auth.py`` 的领域逻辑 → 拼响应」。
口令哈希、JWT 签发、会话白名单、验证码存储全部在 ``common/auth.py`` 与
``common/security.py``，这里**不重复实现**，避免出现两套安全判断。

坑（实现时踩到/绕开的，改代码前先看这段）
----------------------------------------
1. 登录失败锁定：``LoginGuard`` 实例挂在 ``HttpServer.login_guard`` 上，而
   ``common/auth.py::authenticate()`` 通过 ``getattr(authenticate, "_server", None)``
   取它 —— 全仓库没有任何地方给 ``authenticate._server`` 赋值，所以地基里的锁定
   逻辑实际不会触发。契约要求 429 且 ``code=locked``，因此这里由接口层显式做
   「锁检查 / 记失败 / 成功后清零」，不依赖那个空挂载点。
2. ``/auth/verify-code`` 是「注册/重置前预校验」，**不能消费验证码**：地基的
   ``check_verify_code()`` 会把 ``used_at`` 置上，预校验之后紧接着提交注册就会被
   判「请先获取验证码」。所以这里单独实现只读校验；但**校验失败照样累加
   attempts**，否则这个接口会变成 6 位验证码的免费爆破入口。
3. ``/auth/forgot-password`` 返回 ``reset_token``，真正的身份证明是随后
   ``/auth/reset-password`` 里对验证码的消费式校验 —— reset_token 单独不足以重置
   密码（否则只要知道用户名就能改别人密码）。验证码的 target 只可能是用户自己的
   邮箱/手机号，所以重置时按 ``user_id`` 反查最近一条未使用的 reset_password 码。
4. ``remember`` 只回显：地基 ``issue_tokens()`` 不接受 ttl。前端据此选
   localStorage / sessionStorage，真正的 token 寿命由 ``SK_ACCESS_TTL`` /
   ``SK_REFRESH_TTL`` 控制（改这里不影响）。
5. 公开配置里同时给出 ``site_name``/``allow_register``/``online_pay_enabled`` 等
   扁平字段：前端 ``window.__SPARK__`` 与登录页都直接读它，不再二次请求。
"""

from __future__ import annotations

from .. import db
from ..bootstrap import get_config
from ..common import auth as authlib
from ..common import logging as logmod
from ..common import mailer, security, timeutil, validate
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route
from ..version import VERSION

VERIFY_SCENES = ("register", "reset_password", "login", "bind")
VERIFY_CHANNELS = ("email", "sms")
LOGIN_LOCK_CODE = "locked"


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _channel_configured(channel: str) -> bool:
    """邮件/短信通道是否真的配好了：决定 ``sent`` 与是否返回 ``dev_code``。"""
    health = mailer.health()
    if channel == "email":
        return bool(health.get("smtp_configured"))
    return bool(health.get("sms_provider"))


def _mask_target(channel: str, target: str) -> str:
    return security.mask_email(target) if channel == "email" else security.mask_phone(target)


def _normalize_target(channel: str, raw: str) -> str:
    """按通道规范化并校验目标地址；不合法直接 400。"""
    if channel == "email":
        good, value = validate.email(raw)
    else:
        good, value = validate.phone(raw)
    if not good:
        raise HttpError(400, value)
    return value


def _lock_key(req: Request, identifier: str) -> str:
    """锁定维度：账号 + IP。只按账号锁会被恶意者用来锁死别人；只按 IP 锁会误伤 NAT。"""
    return f"{(identifier or '').strip().lower()}|{req.client_ip}"


def _authenticate_with_guard(req: Request) -> dict:
    """带锁定的登录：等价于 ``authlib.authenticate()`` + 显式 LoginGuard（见模块头坑 1）。"""
    identifier = req.str_field("identifier", max_len=160)
    password = str(req.field("password") or "")
    if not identifier or not password:
        raise HttpError(400, "请输入账号与密码")

    guard = getattr(req.server, "login_guard", None)
    key = _lock_key(req, identifier)
    if guard is not None:
        wait = guard.locked_for(key)
        if wait > 0:
            raise HttpError(429, f"{authlib.LOGIN_LOCK_MESSAGE}（约 {wait} 秒后重试）", code=LOGIN_LOCK_CODE)
    try:
        user = authlib.authenticate(
            identifier,
            password,
            ip=req.client_ip,
            user_agent=req.headers.get("user-agent", ""),
        )
    except HttpError:
        if guard is not None:
            guard.record_fail(key)
        raise
    if guard is not None:
        guard.reset(key)
    return user


def _issue_login_payload(req: Request, user: dict, remember: bool) -> dict:
    """登录/注册成功后的统一响应体：token 组 + 用户对象（含配额快照）。

    ``remember=True`` 只延长 refresh 的有效期（access 仍是短有效期），
    具体秒数由地基 ``REMEMBER_REFRESH_TTL`` 决定，并随 ``refresh_expires_in`` 返回，
    前端据此决定存 localStorage 还是 sessionStorage。
    """
    _refresh_quota(user["id"])
    fresh = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],)) or user
    payload = authlib.issue_tokens(
        fresh,
        ip=req.client_ip,
        user_agent=req.headers.get("user-agent", ""),
        refresh_ttl=authlib.REMEMBER_REFRESH_TTL if remember else None,
    )
    payload["remember"] = bool(remember)
    payload["user"] = authlib.public_user(fresh)
    return payload


def _refresh_quota(user_id: int) -> None:
    """重算套餐权益快照。登录时刷新一次，避免套餐过期后 ``quota_json`` 仍是旧值。"""
    from ..common.quota import Quota

    try:
        Quota.refresh(user_id)
    except HttpError:
        # 用户刚被删除/禁用等边界：配额快照不刷新也能登录，public_user 会回退默认值
        pass


# --------------------------------------------------------------------------
# 公开配置
# --------------------------------------------------------------------------
@route("GET", "/api/v1/auth/public-configs", auth="public",
       summary="站点公开配置（站名/logo/备案/开放注册/在线支付）", tags=("auth",))
def public_configs(req: Request) -> Response:
    cfg = get_settings()
    return ok({
        "site_name": get_config("site.name", "抖音续火花系统"),
        "site_logo": get_config("site.logo", "/static/logo.svg"),
        "site_icp": get_config("site.icp", ""),
        "site_customer_service": get_config("site.customer_service", ""),
        "site_notice": get_config("site.notice", "") or get_config("site.announcement", ""),
        "allow_register": get_config("auth.allow_register", "1") == "1",
        "require_email_verify": get_config("auth.require_email_verify", "0") == "1",
        "require_invite": get_config("auth.require_invite", "0") == "1",
        "online_pay_enabled": get_config("pay.enabled", "0") == "1",
        "pay_channel": get_config("pay.channel", "easypay"),
        "default_time": get_config("task.default_time", "08:30"),
        "default_timezone": get_config("task.default_timezone", cfg.timezone),
        "version": VERSION,
        "api_base": "/api/v1",
    })


# --------------------------------------------------------------------------
# 验证码
# --------------------------------------------------------------------------
@route("POST", "/api/v1/auth/send-verify-code", auth="public",
       summary="发送邮箱/短信验证码", tags=("auth",))
def send_verify_code(req: Request) -> Response:
    scene = req.str_field("scene", "register", max_len=32)
    channel = req.str_field("channel", "email", max_len=16).lower()
    if scene not in VERIFY_SCENES:
        raise HttpError(400, "验证码场景不正确")
    if channel not in VERIFY_CHANNELS:
        raise HttpError(400, "验证码通道不正确")

    target = _normalize_target(channel, req.str_field("target", max_len=200))
    # captcha（图形验证码）当前版本未启用：字段接受但忽略，未来接入时在此校验
    configured = _channel_configured(channel)
    code, ttl = authlib.create_verify_code(
        scene=scene, channel=channel, target=target, ip=req.client_ip
    )
    data = {
        "sent": configured,
        "expire_in": ttl,
        "scene": scene,
        "channel": channel,
        "target": _mask_target(channel, target),
    }
    if not configured:
        # 契约：未配置通道时 sent=false 且回 dev_code，方便本地把注册流程跑通
        data["dev_code"] = code
        data["hint"] = "邮件/短信通道未配置，验证码仅在本响应中返回（生产环境请配置 SMTP/短信）"
    return ok(data)


@route("POST", "/api/v1/auth/verify-code", auth="public",
       summary="校验验证码（预校验，不消费）", tags=("auth",))
def verify_code(req: Request) -> Response:
    scene = req.str_field("scene", "register", max_len=32)
    channel = req.str_field("channel", "email", max_len=16).lower()
    if scene not in VERIFY_SCENES or channel not in VERIFY_CHANNELS:
        raise HttpError(400, "验证码场景或通道不正确")
    target = _normalize_target(channel, req.str_field("target", max_len=200))
    _peek_verify_code(scene=scene, channel=channel, target=target, code=req.str_field("code", max_len=16))
    return ok({"verified": True, "scene": scene, "channel": channel, "target": _mask_target(channel, target)})


def _peek_verify_code(*, scene: str, channel: str, target: str, code: str) -> None:
    """只读校验验证码：命中不置 ``used_at``，未命中累加 ``attempts``。

    为什么不用 ``authlib.check_verify_code()``：那是消费式校验，预校验通过后再提交
    注册/重置会被判「请先获取验证码」（见模块头坑 2）。安全上两者等价的部分是
    「过期 / 次数上限 / 哈希比对」，差别只在最后是否写入 ``used_at``。
    """
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
    if int(row["attempts"] or 0) >= authlib.VERIFY_MAX_ATTEMPTS:
        raise HttpError(429, "验证码错误次数过多，请重新获取")
    if security.sha256_hex(f"{scene}:{target}:{code}") != row["code_hash"]:
        db.update("verify_codes", {"attempts": int(row["attempts"] or 0) + 1}, "id=?", (row["id"],))
        raise HttpError(400, "验证码不正确")


# --------------------------------------------------------------------------
# 注册 / 登录
# --------------------------------------------------------------------------
@route("POST", "/api/v1/auth/user/register", auth="public", summary="用户注册（注册即登录）", tags=("auth",))
def register(req: Request) -> Response:
    if get_config("auth.allow_register", "1") != "1":
        raise HttpError(403, "站点已关闭注册，请联系管理员开通", code="forbidden")

    good, username = validate.username(req.str_field("username", max_len=64))
    if not good:
        raise HttpError(400, username)
    password = str(req.field("password") or "")
    confirm = str(req.field("confirm_password") or "")
    if confirm and confirm != password:
        raise HttpError(400, "两次输入的密码不一致")

    raw_email = req.str_field("email", "", max_len=160)
    raw_phone = req.str_field("phone", "", max_len=32)
    email = _normalize_target("email", raw_email) if raw_email else ""
    phone = _normalize_target("sms", raw_phone) if raw_phone else ""
    verify_code = req.str_field("verify_code", "", max_len=16)
    require_email_verify = get_config("auth.require_email_verify", "0") == "1"

    if require_email_verify and not email:
        raise HttpError(400, "本站注册需要邮箱验证，请填写邮箱")
    if require_email_verify and not verify_code:
        raise HttpError(400, "请先获取并填写邮箱验证码")

    if verify_code:
        if not (email or phone):
            raise HttpError(400, "填写验证码时请同时填写邮箱或手机号")
        channel = "email" if email else "sms"
        # 消费式校验：一码只能用一次，防止同一个验证码批量注册
        authlib.check_verify_code(
            scene="register", channel=channel, target=email or phone, code=verify_code
        )

    referrer_id = _resolve_inviter(req.str_field("invite_code", "", max_len=64))

    user = authlib.register(
        username=username,
        password=password,
        email=email,
        phone=phone,
        nickname=req.str_field("nickname", "", max_len=64),
        ip=req.client_ip,
        user_agent=req.headers.get("user-agent", ""),
    )
    if referrer_id:
        db.update("users", {"parent_agent_id": referrer_id}, "id=?", (user["id"],))
        user["parent_agent_id"] = referrer_id
    # 注册即登录：默认给长会话（用户刚注册完就跳走的情况更常见），可传 remember=false 关闭
    return ok(_issue_login_payload(req, user, remember=req.bool_field("remember", True)), message="注册成功")


def _resolve_inviter(invite_code: str) -> int | None:
    """邀请码 = 邀请人的用户名。返回邀请人 id；不需要邀请码时找不到就忽略。"""
    require_invite = get_config("auth.require_invite", "0") == "1"
    if not invite_code:
        if require_invite:
            raise HttpError(403, "本站注册需要邀请码", code="forbidden")
        return None
    row = db.query_one("SELECT id FROM users WHERE username=?", (invite_code,))
    if not row:
        if require_invite:
            raise HttpError(400, "邀请码不存在")
        return None
    return int(row["id"])


@route("POST", "/api/v1/auth/user/login", auth="public", summary="用户登录", tags=("auth",))
def user_login(req: Request) -> Response:
    user = _authenticate_with_guard(req)
    return ok(_issue_login_payload(req, user, remember=req.bool_field("remember", False)), message="登录成功")


@route("POST", "/api/v1/auth/admin/login", auth="public", summary="管理端登录（非管理员拒绝）", tags=("auth",))
def admin_login(req: Request) -> Response:
    user = _authenticate_with_guard(req)
    if user.get("role") != "admin":
        # 口令正确但不是管理员：不签发 token，并把这次尝试写进日志便于排查
        logmod.log(
            f"非管理员尝试登录管理端：{user.get('username')}",
            level="warning",
            scope="system",
            source="auth.admin_login",
            context={"ip": req.client_ip},
        )
        raise HttpError(403, "该账号没有管理端权限", code="forbidden")
    return ok(_issue_login_payload(req, user, remember=req.bool_field("remember", False)), message="登录成功")


@route("POST", "/api/v1/auth/refresh", auth="public", summary="刷新令牌（旧 refresh 立即作废）", tags=("auth",))
def refresh(req: Request) -> Response:
    token = req.str_field("refresh_token", "", max_len=4096) or req.bearer_token()
    if not token:
        raise HttpError(400, "缺少 refresh_token")
    payload = authlib.refresh_access_token(
        token, ip=req.client_ip, user_agent=req.headers.get("user-agent", "")
    )
    payload.setdefault("remember", True)
    return ok(payload)


@route("POST", "/api/v1/auth/logout", auth="user", summary="退出登录（吊销当前会话）", tags=("auth",))
def logout(req: Request) -> Response:
    user = req.require_user()
    authlib.revoke_session(req.bearer_token())
    logmod.audit("logout", user=user, ip=req.client_ip)
    return ok({"logged_out": True}, message="已退出登录")


# --------------------------------------------------------------------------
# 找回密码
# --------------------------------------------------------------------------
@route("POST", "/api/v1/auth/forgot-password", auth="public",
       summary="发送重置验证码并返回 reset_token", tags=("auth",))
def forgot_password(req: Request) -> Response:
    identifier = req.str_field("identifier", max_len=160)
    if not identifier:
        raise HttpError(400, "请输入用户名/邮箱/手机号")

    user = authlib.find_by_identifier(identifier)
    # 账号不存在/被禁用时返回同样的「已发送」文案，避免账号枚举
    if not user or user.get("status") != "active":
        return ok({
            "sent": False, "expire_in": 0, "channel": "", "target": "",
            "reset_token": "", "dev_code": "",
            "message": "如果该账号存在，重置验证码已发送",
        })

    channel = "email" if user.get("email") else ("sms" if user.get("phone") else "")
    if not channel:
        # 没有绑定渠道就无法证明身份，这里必须明确拒绝而不是「放行但要验证码」
        raise HttpError(400, "该账号未绑定邮箱或手机号，无法自助重置，请联系管理员")
    target = user["email"] if channel == "email" else user["phone"]
    configured = _channel_configured(channel)

    dev_code = ""
    expire_in = authlib.VERIFY_CODE_TTL_SECONDS
    try:
        dev_code, expire_in = authlib.create_verify_code(
            scene="reset_password", channel=channel, target=target, ip=req.client_ip
        )
    except HttpError as exc:
        # 60 秒内重复请求：沿用上一条还没过期的验证码，仍然发 reset_token，
        # 否则用户被限流卡住后拿不到凭证（重置仍要验证码，安全性不变）
        if exc.status != 429:
            raise

    data = {
        "sent": configured,
        "expire_in": expire_in,
        "channel": channel,
        "target": _mask_target(channel, target),
        "reset_token": authlib.create_reset_token(user),
        "message": "重置验证码已发送" if configured else "未配置发送通道，验证码仅在本响应中返回",
    }
    if not configured and dev_code:
        data["dev_code"] = dev_code
    logmod.audit("forgot_password", user=user, detail=f"channel={channel}", ip=req.client_ip)
    return ok(data)


@route("POST", "/api/v1/auth/reset-password", auth="public", summary="用 reset_token 重置密码", tags=("auth",))
def reset_password(req: Request) -> Response:
    token = req.str_field("reset_token", "", max_len=4096)
    if not token:
        raise HttpError(400, "缺少 reset_token，请重新走找回密码流程")
    password = str(req.field("password") or "")
    confirm = str(req.field("confirm_password") or "")
    if confirm and confirm != password:
        raise HttpError(400, "两次输入的密码不一致")
    problem = security.password_problem(password)
    if problem:
        # 先校验口令规则再消费验证码：否则用户填了弱口令要重新收验证码
        raise HttpError(400, problem)

    try:
        payload = security.verify_jwt(token, kind="reset")
    except security.TokenError as exc:
        raise HttpError(400, exc.message) from exc
    if payload.get("purpose") != "reset_password":
        raise HttpError(400, "重置凭证无效")
    user = db.query_one("SELECT * FROM users WHERE id=?", (payload.get("sub"),))
    if not user:
        raise HttpError(404, "用户不存在")

    # reset_token 只证明「有人为该账号发起过重置」，真正证明身份的是验证码：
    # 两者都校验，缺一不可（见模块头坑 3）
    code = req.str_field("verify_code", "", max_len=16)
    if not code:
        raise HttpError(400, "请填写收到的验证码")
    row = db.query_one(
        "SELECT * FROM verify_codes WHERE scene='reset_password' AND used_at IS NULL"
        " AND target IN (?, ?) ORDER BY id DESC LIMIT 1",
        (user.get("email") or "\x00", user.get("phone") or "\x00"),
    )
    if not row:
        raise HttpError(400, "请先获取验证码")
    authlib.check_verify_code(
        scene="reset_password", channel=row["channel"], target=row["target"], code=code
    )

    authlib.consume_reset_token(token, password, ip=req.client_ip)
    return ok({"reset": True, "username": user["username"]}, message="密码已重置，请用新密码登录")
