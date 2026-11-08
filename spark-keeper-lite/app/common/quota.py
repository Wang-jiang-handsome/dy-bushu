"""套餐权益与配额：把「这个用户现在能干什么」集中在一处判断。

为什么单独一个模块
------------------
配额判断散落在各接口里最容易出现「A 接口放行、B 接口拦截」的漏洞。
这里定义唯一的入口：

    q = Quota.for_user(user_id)
    q.ensure()                 # 未激活/已过期 -> 抛 402
    q.ensure_can_add_account()
    q.ensure_can_run_today()

``users.quota_json`` 缓存生效套餐的权益快照，套餐到期或套餐被管理员改动后
由 ``refresh()`` 重算（登录、每次任务执行前都会刷新一次）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from .. import db
from ..router import HttpError
from . import timeutil

DEFAULT_QUOTA = {
    "package_id": None,
    "package_code": "",
    "package_name": "",
    "account_limit": 0,
    "daily_task_limit": 0,
    "expire_at": None,
    "active": False,
    "source": "",          # package | card | trial | admin | none
    # 「次数卡」兑换出来的每日额外执行次数。它不属于任何套餐，所以必须能与套餐
    # 叠加并在 refresh 后保留 —— 这是 count 型卡密的落地位置。
    "bonus_task_count": 0,
    # 次数卡的到期时间（没有套餐时也能靠它判断是否还能用）
    "bonus_expire_at": None,
}


def _parse_json(text, fallback):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback


def package_quota(package: dict | None, expire_at: str | None, source: str = "package",
                  bonus_count: int = 0, bonus_expire_at: str | None = None) -> dict:
    if not package:
        return {**DEFAULT_QUOTA, "bonus_task_count": int(bonus_count or 0),
                "bonus_expire_at": bonus_expire_at}
    return {
        "package_id": package.get("id"),
        "package_code": package.get("code", ""),
        "package_name": package.get("name", ""),
        "account_limit": int(package.get("account_limit") or 0),
        "daily_task_limit": int(package.get("daily_task_limit") or 0),
        "expire_at": expire_at,
        "active": bool(expire_at) and not timeutil.is_expired(expire_at),
        "source": source,
        "bonus_task_count": int(bonus_count or 0),
        "bonus_expire_at": bonus_expire_at,
    }


def merge_expire(current: str | None, days: int, base: str | None = None) -> str:
    """续期语义：未过期则从原到期时间往后加，已过期则从现在开始算。"""
    start = timeutil.parse_db(current)
    now = timeutil.now_utc()
    if not start or start < now:
        start = now
    return timeutil.to_db(start + timedelta(days=int(days)))


@dataclass
class Quota:
    user: dict
    quota: dict = field(default_factory=lambda: dict(DEFAULT_QUOTA))

    # ---- 构造 ----
    @classmethod
    def for_user(cls, user_id: int) -> "Quota":
        user = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            raise HttpError(404, "用户不存在")
        return cls(user=user, quota=_parse_json(user.get("quota_json"), dict(DEFAULT_QUOTA)))

    @classmethod
    def refresh(cls, user_id: int) -> "Quota":
        """按 users/套餐/卡密重新计算权益并落库。"""
        user = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            raise HttpError(404, "用户不存在")
        quota = cls._compute(user)
        db.update(
            "users",
            {"quota_json": json.dumps(quota, ensure_ascii=False), "updated_at": timeutil.utc_now_str()},
            "id=?",
            (user_id,),
        )
        user["quota_json"] = json.dumps(quota, ensure_ascii=False)
        return cls(user=user, quota=quota)

    @staticmethod
    def _compute(user: dict) -> dict:
        # 上次算出来的「次数卡」加成要保留，否则每次 refresh 都会把它抹掉
        cached = _parse_json(user.get("quota_json"), {})
        bonus_count = int(cached.get("bonus_task_count") or 0)
        bonus_expire_at = cached.get("bonus_expire_at")
        if bonus_count and bonus_expire_at and timeutil.is_expired(bonus_expire_at):
            bonus_count, bonus_expire_at = 0, None      # 加成卡过期即作废

        if user.get("role") == "admin":
            return {
                **DEFAULT_QUOTA,
                "package_code": "admin",
                "package_name": "管理员",
                "account_limit": 999,
                "daily_task_limit": 999,
                "expire_at": None,
                "active": True,
                "source": "admin",
                "bonus_task_count": bonus_count,
                "bonus_expire_at": bonus_expire_at,
            }
        package_id = user.get("package_id")
        expire_at = user.get("package_expire_at")
        if package_id:
            package = db.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
            if package:
                quota = package_quota(package, expire_at, "package", bonus_count, bonus_expire_at)
                quota["active"] = bool(expire_at) and not timeutil.is_expired(expire_at)
                return quota
        return {**DEFAULT_QUOTA, "bonus_task_count": bonus_count, "bonus_expire_at": bonus_expire_at}

    # ---- 次数卡加成 ----
    @property
    def bonus_task_count(self) -> int:
        if self.user.get("role") == "admin":
            return 0
        cached = _parse_json(self.user.get("quota_json"), {})
        count = int(cached.get("bonus_task_count") or 0)
        expire_at = cached.get("bonus_expire_at")
        if count and expire_at and timeutil.is_expired(expire_at):
            return 0
        return count

    @property
    def bonus_expire_at(self) -> str | None:
        return _parse_json(self.user.get("quota_json"), {}).get("bonus_expire_at")

    # ---- 查询 ----
    @property
    def active(self) -> bool:
        """是否有「能跑任务」的有效权益：套餐未过期，或还有次数卡加成。"""
        if self.user.get("role") == "admin":
            return True
        package_ok = (
            bool(self.user.get("package_id"))
            and bool(self.user.get("package_expire_at"))
            and not timeutil.is_expired(self.user.get("package_expire_at"))
        )
        return package_ok or self.bonus_task_count > 0

    @property
    def account_limit(self) -> int:
        if self.user.get("role") == "admin":
            return 999
        return int(self.quota.get("account_limit") or 0)

    @property
    def daily_task_unlimited(self) -> bool:
        """是否**不限制**每日执行次数。

        自托管单租户场景下套餐/次数主要是商业化外壳，用户希望「随便跑多少次」。
        由 ``settings.unlimited_task_runs`` 控制（环境变量
        ``SK_UNLIMITED_TASK_RUNS=0`` 可关掉，恢复按套餐计次）。
        """
        try:
            from ..config import get_settings
            return bool(getattr(get_settings(), "unlimited_task_runs", True))
        except Exception:      # noqa: BLE001 - 配置读不到时按「不限次」处理（自托管默认）
            return True

    @property
    def daily_task_limit(self) -> int:
        """套餐每日次数 + 次数卡加成。

        不限次时返回 0 —— 调用方必须先看 :attr:`daily_task_unlimited`，
        别把 0 当成「套餐不支持」（那是 ``active`` 的事）。
        """
        if self.daily_task_unlimited:
            return 0
        if self.user.get("role") == "admin":
            return 999
        return int(self.quota.get("daily_task_limit") or 0) + self.bonus_task_count

    @property
    def expire_text(self) -> str:
        return timeutil.humanize_delta(self.user.get("package_expire_at"))

    def account_count(self) -> int:
        return int(db.query_scalar(
            "SELECT COUNT(*) FROM douyin_accounts WHERE user_id=?", (self.user["id"],), 0) or 0)

    def today_run_count(self, run_date: str | None = None) -> int:
        run_date = run_date or timeutil.local_date_str(self.user.get("timezone") if isinstance(self.user, dict) else None)
        return int(db.query_scalar(
            "SELECT COUNT(*) FROM task_runs WHERE user_id=? AND run_date=? AND status IN ('running','success','partial','failed')",
            (self.user["id"], run_date),
            0,
        ) or 0)

    # ---- 断言 ----
    def ensure(self) -> None:
        if self.user.get("status") != "active":
            raise HttpError(403, "账号已被禁用，请联系管理员")
        if not self.active:
            raise HttpError(402, "套餐未激活或已过期，请先兑换卡密或购买套餐", code="no_active_package")

    def ensure_can_add_account(self) -> None:
        self.ensure()
        if self.account_count() >= self.account_limit:
            raise HttpError(
                402,
                f"当前套餐最多绑定 {self.account_limit} 个抖音账号，请升级套餐",
                code="account_limit",
            )

    def ensure_can_run_today(self) -> None:
        self.ensure()
        # 不限次模式（自托管默认）：直接放行，不再看每日次数
        if self.daily_task_unlimited:
            return
        limit = self.daily_task_limit
        if limit <= 0:
            raise HttpError(402, "当前套餐不支持自动执行任务，请升级套餐", code="task_limit")
        used = self.today_run_count()
        if used >= limit:
            raise HttpError(
                429,
                f"今日执行次数已达上限（{used}/{limit}），请明天再试或升级套餐",
                code="task_daily_limit",
            )

    def summary(self) -> dict:
        return {
            "active": self.active,
            "package_id": self.user.get("package_id"),
            "package_code": self.quota.get("package_code", ""),
            "package_name": self.quota.get("package_name", ""),
            "expire_at": self.user.get("package_expire_at"),
            "expire_text": self.expire_text,
            "account_limit": self.account_limit,
            "account_used": self.account_count(),
            "daily_task_limit": self.daily_task_limit,
            "daily_task_used": self.today_run_count(),
            # 前端据此把「x/y 次」显示成「x/∞ 次」
            "daily_task_unlimited": self.daily_task_unlimited,
            "role": self.user.get("role"),
            "bonus_task_count": self.bonus_task_count,
            "bonus_expire_at": self.bonus_expire_at,
        }


def grant_bonus_tasks(user_id: int, count: int, days: int = 30) -> dict:
    """发放/叠加「次数卡」加成：每天额外 N 次任务执行额度。

    * 同一用户多次兑换会**叠加**（count 累加）。
    * 有效期取「更晚的那个」：在现有 bonus_expire_at 基础上续，或从今天 +
      days 天算起，避免用户续卡反而缩短有效期。
    * 调用方负责把它包在 ``db.transaction()`` 里（与卡密扣减同一事务）。
    """
    user = db.query_one("SELECT quota_json FROM users WHERE id=?", (user_id,))
    if not user:
        raise HttpError(404, "用户不存在")
    cached = _parse_json(user.get("quota_json"), {})
    bonus_count = int(cached.get("bonus_task_count") or 0) + max(0, int(count))
    current_expire = cached.get("bonus_expire_at")
    new_expire = merge_expire(current_expire, days)
    updated = {
        **DEFAULT_QUOTA,
        **cached,
        "bonus_task_count": bonus_count,
        "bonus_expire_at": new_expire,
    }
    db.update(
        "users",
        {"quota_json": json.dumps(updated, ensure_ascii=False), "updated_at": timeutil.utc_now_str()},
        "id=?",
        (user_id,),
    )
    return {"bonus_task_count": bonus_count, "bonus_expire_at": new_expire}


def limit_status(user_id: int) -> dict:
    """给前端顶部进度条用：账号数、今日执行次数。"""
    try:
        return Quota.refresh(user_id).summary()
    except HttpError:
        return dict(DEFAULT_QUOTA)
