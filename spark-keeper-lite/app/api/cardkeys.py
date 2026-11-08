"""卡密（发卡/兑换）与订单发放。

为什么这个模块写得这么"重"
--------------------------
卡密在这套系统里就是钱：一张 ``time`` 卡等价于 30 天套餐，一张 ``balance`` 卡等价于现金。
所以这里的每一条约束都是必须的：

* **查卡走 ``code_hash``**（sha256），不用明文索引；明文只存在于 ``card_keys.code``
  与导出文件里，对外响应一律脱敏。
* **扣减必须原子**：``db.transaction()``（BEGIN IMMEDIATE）把「读卡 → 判状态 → 扣次数 →
  写用户权益」包成一个事务，并在事务内**重读一次卡密**再判断，保证并发的两次兑换
  只有一次能成功（另一次必然看到 ``used_count`` 已到上限 → 409 ``card_used``）。
* **全程留痕**：每次兑换写 ``card_redemptions``（业务流水）与 ``audit_logs``（审计）。
* ``fulfill_order`` 是全系统**唯一**的订单发放入口：管理端人工确认收款、余额支付、
  支付回调都调它。它在事务内重读订单状态做幂等，重复调用不会重复加天数/重复扣余额。

三种卡型的语义（Lead 定稿；``days`` / ``use_limit`` / ``amount_cents`` 三个位按卡型复用）::

    time     时长卡：days=加多少天；use_limit=这张卡可兑换几次，每次兑换消耗 1 次
    count    次数卡：days=加成有效天数；use_limit=每天额外执行次数；
             **一次性**——兑一次即 status='used'（use_limit 不表示可兑换次数，见 redeem_card 注释）
    balance  充值卡：amount_cents=充值面额；use_limit=可兑换几次，每次加一次面额

坑位提醒
--------
* ``normalize_card_code`` 会把用户粘贴内容里所有非字母数字丢掉并大写，所以连字符、
  空格、换行都不影响兑换；长度不足就直接判无效（不要拿短串去查哈希，没意义）。
* ``redeem_card`` 内部会调用 ``fulfill_order(..., apply_package=False)`` 去核销
  该用户同一套餐的待支付卡密订单——**必须**带 ``apply_package=False``，
  否则天数会被加两遍（兑换一次 + 订单发放一次）。
* 定向卡（``bound_user_id``）只校验"是不是本人"，不做"每次兑换都校验"以外的额外限制。
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any

from .. import db
from ..common import logging as logmod
from ..common import quota as quota_mod
from ..common import timeutil
from ..common.security import (
    CARD_LENGTH,
    card_code_hash,
    new_card_code,
    normalize_card_code,
    random_code,
)
from ..router import HttpError, Request, Response, ok, route

CARD_TYPES = ("time", "count", "balance")
CARD_STATUSES = ("unused", "used", "disabled", "expired")
MAX_BATCH_SIZE = 2000


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def mask_card_code(code: str) -> str:
    """对外展示用的脱敏卡密：ABCD-****-****-WXYZ。"""
    raw = normalize_card_code(code)
    if len(raw) <= 8:
        return raw[:2] + "***"
    return f"{raw[:4]}-****-****-{raw[-4:]}"


def _make_code(prefix: str = "") -> str:
    """生成一张卡密；支持自定义前缀（前缀也参与哈希，规则不变）。"""
    clean = "".join(c for c in (prefix or "").upper() if c.isalnum())[:8]
    return (clean + new_card_code())[:CARD_LENGTH]


def effective_status(card: dict) -> str:
    """列表展示用状态：unused 但激活截止时间已过 → expired（不改库，只影响展示）。"""
    status = str(card.get("status") or "unused")
    if status == "unused" and card.get("expire_at") and timeutil.is_expired(card["expire_at"]):
        return "expired"
    return status


def benefit_text(card_type: str, days: int, use_limit: int, amount_cents: int) -> str:
    """一句话说清这张卡的权益（发卡记录/导出/前端都用它，别让买家猜）。"""
    card_type = (card_type or "time").lower()
    days = max(0, int(days or 0))
    use_limit = max(1, int(use_limit or 1))
    if card_type == "balance":
        return f"余额 ¥{int(amount_cents or 0) / 100:.2f}（可兑换 {use_limit} 次）"
    if card_type == "count":
        return f"每天额外 {use_limit} 次执行额度，有效期 {days} 天"
    return f"{days} 天套餐（可兑换 {use_limit} 次）"


def _first_used_at(card: dict) -> str:
    return str(card.get("used_at") or "")


# --------------------------------------------------------------------------
# 生成卡密（CLI / 管理端共用；纯函数，不依赖 Request）
# --------------------------------------------------------------------------
def generate_cards(
    package_code: str = "monthly",
    card_type: str = "time",
    days: int = 30,
    count: int = 10,
    use_limit: int = 1,
    amount_cents: int = 0,
    batch_note: str = "",
    created_by: int | None = None,
    *,
    prefix: str = "",
    batch_no: str = "",
    bound_user_id: int | None = None,
    expire_days: int = 0,
) -> dict:
    """批量生成卡密，返回 ``{batch_no, count, codes, package_name, ...}``。

    签名与 ``main.py`` 的 ``card`` 子命令一致，改动前先看 CLI。
    """
    card_type = (card_type or "time").strip().lower()
    if card_type not in CARD_TYPES:
        raise HttpError(400, f"卡密类型只能是 {'/'.join(CARD_TYPES)}")
    count = int(count or 0)
    if count < 1 or count > MAX_BATCH_SIZE:
        raise HttpError(400, f"单批生成数量需在 1~{MAX_BATCH_SIZE} 之间")
    use_limit = max(1, int(use_limit or 1))
    days = max(0, int(days or 0))
    amount_cents = max(0, int(amount_cents or 0))

    if card_type in ("time", "count") and days <= 0:
        raise HttpError(400, "时长卡必须指定大于 0 的天数")
    if card_type == "balance" and amount_cents <= 0:
        raise HttpError(400, "充值卡必须指定大于 0 的面额（分）")

    package = None
    if (package_code or "").strip():
        package = db.query_one("SELECT * FROM packages WHERE code=?", (package_code.strip(),))
        if not package:
            raise HttpError(400, f"套餐 {package_code} 不存在")
    if package is None and card_type != "balance":
        raise HttpError(400, "生成时长卡必须指定已存在的套餐 code")

    if bound_user_id and not db.query_one("SELECT id FROM users WHERE id=?", (int(bound_user_id),)):
        raise HttpError(404, "定向用户不存在")

    now = timeutil.utc_now_str()
    stamp = time.strftime("%Y%m%d", time.gmtime())
    batch_no = (batch_no or "").strip() or f"B{stamp}{random_code(4)}"
    expire_at = timeutil.add_days_str(int(expire_days)) if int(expire_days or 0) > 0 else None

    with db.transaction():
        if db.query_one("SELECT id FROM card_batches WHERE batch_no=?", (batch_no,)):
            batch_no = f"B{time.strftime('%Y%m%d%H%M%S', time.gmtime())}{random_code(4)}"
        batch_id = db.insert(
            "card_batches",
            {
                "batch_no": batch_no,
                "package_id": package["id"] if package else None,
                "card_type": card_type,
                "days": days,
                "use_limit": use_limit,
                "amount_cents": amount_cents,
                "quantity": count,
                "note": (batch_note or "")[:200],
                "created_by": created_by,
                "created_at": now,
            },
        )
        codes: list[str] = []
        seen: set[str] = set()
        guard = 0
        while len(codes) < count and guard < count * 20 + 100:
            guard += 1
            code = _make_code(prefix)
            if code in seen:
                continue
            seen.add(code)
            # 极端情况下（前缀把随机位挤没了）可能与历史卡密撞哈希，撞了就重新生成
            if db.query_one("SELECT id FROM card_keys WHERE code_hash=?", (card_code_hash(code),)):
                continue
            codes.append(code)
        if len(codes) < count:
            raise HttpError(400, "卡密前缀过长导致可生成空间不足，请缩短前缀")

        for code in codes:
            db.insert(
                "card_keys",
                {
                    "code": code,
                    "code_hash": card_code_hash(code),
                    "batch_id": batch_id,
                    "package_id": package["id"] if package else None,
                    "card_type": card_type,
                    "days": days,
                    "use_limit": use_limit,
                    "used_count": 0,
                    "amount_cents": amount_cents,
                    "status": "unused",
                    "bound_user_id": int(bound_user_id) if bound_user_id else None,
                    "expire_at": expire_at,
                    "note": (batch_note or "")[:200],
                    "created_at": now,
                },
            )

    creator = db.query_one("SELECT * FROM users WHERE id=?", (created_by,)) if created_by else None
    logmod.audit(
        "card_generate",
        scope="admin" if creator else "system",
        user=creator,
        target=batch_no,
        detail=(
            f"生成 {count} 张 {card_type} 卡：套餐={package['code'] if package else '-'} "
            f"天数={days} 次数={use_limit} 面额={amount_cents} 备注={batch_note or '-'}"
        ),
    )
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "count": len(codes),
        "codes": codes,
        "package_code": package["code"] if package else "",
        "package_name": package["name"] if package else "",
        "card_type": card_type,
        "days": days,
        "use_limit": use_limit,
        "amount_cents": amount_cents,
        "expire_at": expire_at,
        "benefit_text": benefit_text(card_type, days, use_limit, amount_cents),
    }


# --------------------------------------------------------------------------
# 兑换
# --------------------------------------------------------------------------
def _card_invalid(message: str = "卡密不存在或已被停用") -> HttpError:
    return HttpError(400, message, code="card_invalid")


def _ensure_redeemable(card: dict, user: dict) -> None:
    """兑换前的可兑换性判断（事务外先判一次、事务内重读再判一次）。"""
    status = str(card.get("status") or "unused")
    if status == "disabled":
        raise _card_invalid("卡密不存在或已被停用")
    if status == "expired" or (card.get("expire_at") and timeutil.is_expired(card["expire_at"])):
        raise _card_invalid("卡密已过期，无法兑换")
    bound = card.get("bound_user_id")
    if bound and int(bound) != int(user["id"]):
        raise HttpError(403, "该卡密为定向卡，仅限指定账号使用", code="forbidden")
    used_count = int(card.get("used_count") or 0)
    use_limit = max(1, int(card.get("use_limit") or 1))
    if status == "used" or used_count >= use_limit:
        used_at = _first_used_at(card)
        message = f"该卡密已被使用（{used_at}）" if used_at else "该卡密已被使用"
        raise HttpError(409, message, code="card_used")


def redeem_card(user: dict, code: str, ip: str = "") -> dict:
    """兑换卡密。三种卡型共用一条原子路径，返回契约第 6 节约定的结构。"""
    normalized = normalize_card_code(code)
    if len(normalized) < CARD_LENGTH:
        raise _card_invalid()
    card = db.query_one("SELECT * FROM card_keys WHERE code_hash=?", (card_code_hash(normalized),))
    if not card:
        raise _card_invalid()
    _ensure_redeemable(card, user)

    now = timeutil.utc_now_str()
    order_no = ""
    with db.transaction():
        # 并发下两个请求都会走到这里，但事务内重读保证只有一个能通过判断
        card = db.query_one("SELECT * FROM card_keys WHERE id=?", (card["id"],))
        if not card:
            raise _card_invalid()
        _ensure_redeemable(card, user)
        current = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],))
        if not current:
            raise HttpError(404, "用户不存在")

        card_type = str(card["card_type"])
        package = (
            db.query_one("SELECT * FROM packages WHERE id=?", (card["package_id"],))
            if card["package_id"]
            else None
        )
        expire_before = str(current.get("package_expire_at") or "")
        expire_after = expire_before
        days_added = 0
        amount = 0
        kind = "activate"
        package_name = package["name"] if package else ""

        if card_type == "balance":
            amount = int(card["amount_cents"] or 0)
            if amount <= 0:
                raise _card_invalid("该卡密面额无效，请联系管理员")
            balance_after = int(current.get("balance_cents") or 0) + amount
            db.update(
                "users", {"balance_cents": balance_after, "updated_at": now}, "id=?", (current["id"],)
            )
            db.insert(
                "balance_logs",
                {
                    "user_id": current["id"],
                    "change_cents": amount,
                    "balance_after": balance_after,
                    "kind": "topup",
                    "ref_type": "card",
                    "ref_id": card["code"],
                    "remark": f"卡密充值 {mask_card_code(card['code'])}",
                    "created_at": now,
                },
            )
            kind = "topup"
            message = f"充值成功，余额增加 {amount / 100:.2f} 元"
        elif card_type == "count":
            # 次数卡：给「每天可执行次数」加成（quota.grant_bonus_tasks），不占用账号位。
            # 为什么忽略 use_limit 的"可兑换次数"语义：次数卡是**一次性**的，
            # use_limit 在它身上只表示"每天额外几次"。若按 use_limit 允许重复兑换，
            # 一张 use_limit=5 的卡就能兑 5 次、累计 +25 次/天，等于卖一张卡送五倍权益。
            # 所以下面强制 used_count=1 / status='used'，第二次兑换必定 409 card_used。
            bonus_count = max(1, int(card["use_limit"] or 1))
            bonus_days = max(1, int(card["days"] or 30))
            cached = quota_mod.Quota.for_user(current["id"])
            expire_before = str(cached.bonus_expire_at or "")
            bonus = quota_mod.grant_bonus_tasks(current["id"], count=bonus_count, days=bonus_days)
            expire_after = str(bonus.get("bonus_expire_at") or "")
            days_added = bonus_days
            kind = "activate"
            message = (
                f"已增加每天 {bonus_count} 次执行额度（有效期 {bonus_days} 天，"
                f"累计加成 {int(bonus.get('bonus_task_count') or 0)} 次/天）"
            )
        else:
            days = int(card["days"] or 0)
            if days <= 0:
                raise _card_invalid("该卡密天数无效，请联系管理员")
            # 续期语义：未过期则在原到期时间上叠加（哪怕换套餐也不吞用户已付的时间）
            base = expire_before if (expire_before and not timeutil.is_expired(expire_before)) else None
            expire_after = quota_mod.merge_expire(base, days)
            days_added = days
            update: dict[str, Any] = {"package_expire_at": expire_after, "updated_at": now}
            if package:
                update["package_id"] = package["id"]
            db.update("users", update, "id=?", (current["id"],))
            kind = "renew" if base else "activate"
            if int(card["used_count"] or 0) > 0:
                kind = "consume"          # count 卡的后续兑换
            label = package_name or "套餐"
            message = f"{label}已续期 {days} 天"

        used_count = int(card["used_count"] or 0) + 1
        use_limit = max(1, int(card["use_limit"] or 1))
        if card_type == "count":
            # 次数卡一次性：无论 use_limit（它是每日额度）是多少，兑一次就作废
            used_count, status = 1, "used"
        else:
            status = "used" if used_count >= use_limit else "unused"
        db.update(
            "card_keys",
            {
                "used_count": used_count,
                "status": status,
                "used_by": current["id"],
                "used_at": card.get("used_at") or now,
            },
            "id=?",
            (card["id"],),
        )
        db.insert(
            "card_redemptions",
            {
                "card_key_id": card["id"],
                "user_id": current["id"],
                "kind": kind,
                "days_added": days_added,
                "amount_cents": amount,
                "package_id": card["package_id"],
                "expire_before": expire_before or None,
                "expire_after": expire_after or None,
                "ip": ip,
                "created_at": now,
            },
        )
        # 卡密订单自动核销：用户先在「套餐购买」下了 channel=card 的单，再来兑卡。
        # 只处理 time 卡——count 卡不发放套餐天数，核销了会给用户"已付款但没套餐"的错觉。
        if card_type == "time" and card["package_id"]:
            pending = db.query_one(
                "SELECT order_no FROM orders WHERE user_id=? AND package_id=? AND channel='card'"
                " AND status='pending' ORDER BY id LIMIT 1",
                (current["id"], card["package_id"]),
            )
            if pending:
                fulfill_order(
                    pending["order_no"],
                    channel="card",
                    operator=None,
                    apply_package=False,      # 天数已经加过了，这里只核销订单
                    remark=f"卡密兑换自动核销 {mask_card_code(card['code'])}",
                )
                order_no = pending["order_no"]

    quota_mod.Quota.refresh(user["id"])
    logmod.audit(
        "card_redeem",
        scope="user",
        user=user,
        target=mask_card_code(card["code"]),
        detail=(
            f"类型={card['card_type']} 套餐={package_name or '-'} 天数={days_added} "
            f"面额={amount} 本次为第 {used_count}/{use_limit} 次"
        ),
        ip=ip,
    )
    logmod.notify(user["id"], "卡密兑换成功", message)

    return {
        "kind": kind,
        "card_type": card["card_type"],
        "days_added": days_added,
        "amount_cents": amount,
        "package_id": card["package_id"],
        "package_name": package_name,
        "expire_before": expire_before,
        "expire_after": expire_after,
        "remaining_uses": max(0, use_limit - used_count),
        "order_no": order_no,
        "message": message,
    }


# --------------------------------------------------------------------------
# 订单发放（管理端确认收款 / 余额支付 / 支付回调 共用）
# --------------------------------------------------------------------------
def order_public(order: dict, *, with_user: bool = False) -> dict:
    """订单对外结构（金额一律用「分」）。"""
    data = {
        "order_no": order.get("order_no"),
        "user_id": order.get("user_id"),
        "package_id": order.get("package_id"),
        "package_name": order.get("package_name") or "",
        "subject": order.get("subject") or "",
        "amount_cents": int(order.get("amount_cents") or 0),
        "payable_cents": int(order.get("payable_cents") or 0),
        "channel": order.get("channel"),
        "status": order.get("status"),
        "trade_no": order.get("trade_no") or "",
        "pay_url": order.get("pay_url") or "",
        "paid_at": order.get("paid_at") or "",
        "expire_at": order.get("expire_at") or "",
        "remark": order.get("remark") or "",
        "client_ip": order.get("client_ip") or "",
        "created_at": order.get("created_at") or "",
        "updated_at": order.get("updated_at") or "",
    }
    if with_user:
        data["username"] = order.get("username") or ""
        data["nickname"] = order.get("nickname") or ""
        data["user_email"] = order.get("user_email") or ""
    return data


def find_order(order_no: str) -> dict | None:
    return db.query_one(
        "SELECT o.*, p.name AS package_name, u.username AS username, u.nickname AS nickname,"
        " u.email AS user_email FROM orders o"
        " LEFT JOIN packages p ON p.id = o.package_id"
        " LEFT JOIN users u ON u.id = o.user_id"
        " WHERE o.order_no=?",
        (order_no,),
    )


def fulfill_order(
    order_no: str,
    trade_no: str = "",
    operator: dict | None = None,
    *,
    channel: str = "",
    remark: str = "",
    apply_package: bool = True,
) -> dict:
    """把订单置为已支付并发放权益。**幂等**：已 paid 的订单直接原样返回。

    两类订单的资金路径（由 ``orders.package_id`` 是否为 NULL 区分）::

        充值单（package_id IS NULL）→ 余额 +payable，写 balance_logs(kind='topup')
        购买单（package_id 非空）  → 按套餐加天数；仅 channel='balance' 时扣余额并写 kind='purchase'

    幂等依据：订单状态（事务内重读）+ ``balance_logs`` 里是否已有该单的对应流水，
    所以「回调 + 人工确认」并发、或渠道重复回调都不会重复发放/重复入账。

    参数
    ----
    trade_no     第三方交易号（回调/查单拿到后回填；为空则保留原值）
    operator     操作人（管理端人工确认时传管理员 dict；回调/系统传 None）
    channel      实际支付渠道，缺省取订单自身 channel（回调可能带更准确的渠道）
    apply_package 是否按套餐加天数；卡密兑换核销订单时传 False（天数已由卡密加过）
    """
    order = db.query_one("SELECT * FROM orders WHERE order_no=?", (order_no,))
    if not order:
        raise HttpError(404, "订单不存在")
    if order["status"] == "paid":
        return {
            "order": order,
            "already_paid": True,
            "days_added": 0,
            "expire_before": "",
            "expire_after": "",
            "balance_paid_cents": 0,
            "balance_credited_cents": 0,
            "is_recharge": order.get("package_id") is None,
        }
    if order["status"] in ("canceled", "refunded", "expired"):
        raise HttpError(409, f"订单当前状态为 {order['status']}，不能确认收款", code="conflict")

    now = timeutil.utc_now_str()
    with db.transaction():
        fresh = db.query_one("SELECT * FROM orders WHERE order_no=?", (order_no,))
        if not fresh:
            raise HttpError(404, "订单不存在")
        if fresh["status"] == "paid":
            # 并发（回调 + 人工确认同时到达）时后到的一方走这里，不再发放
            return {
                "order": fresh,
                "already_paid": True,
                "days_added": 0,
                "expire_before": "",
                "expire_after": "",
                "balance_paid_cents": 0,
                "balance_credited_cents": 0,
                "is_recharge": fresh.get("package_id") is None,
            }
        if fresh["status"] in ("canceled", "refunded", "expired"):
            raise HttpError(409, f"订单当前状态为 {fresh['status']}，不能确认收款", code="conflict")

        user = db.query_one("SELECT * FROM users WHERE id=?", (fresh["user_id"],))
        if not user:
            raise HttpError(404, "订单所属用户不存在")
        pay_channel = (channel or fresh["channel"] or "").strip()
        payable = int(fresh["payable_cents"] or 0)
        expire_before = str(user.get("package_expire_at") or "")
        expire_after = expire_before
        days_added = 0
        balance_paid = 0
        balance_credited = 0
        # 充值单（package_id IS NULL）与购买单走两条完全不同的资金路径：
        #   充值单 -> 余额 +payable（balance_logs kind=topup）
        #   购买单 -> 余额支付才扣款（kind=purchase），外部渠道收款不碰余额
        is_recharge = fresh.get("package_id") is None

        if is_recharge:
            credited = db.query_one(
                "SELECT id FROM balance_logs WHERE ref_type='order' AND ref_id=? AND kind='topup'",
                (order_no,),
            )
            if not credited and payable > 0:
                balance_before = int(user.get("balance_cents") or 0)
                balance_after = balance_before + payable
                db.update(
                    "users", {"balance_cents": balance_after, "updated_at": now}, "id=?", (user["id"],)
                )
                db.insert(
                    "balance_logs",
                    {
                        "user_id": user["id"],
                        "change_cents": payable,
                        "balance_after": balance_after,
                        "kind": "topup",
                        "ref_type": "order",
                        "ref_id": order_no,
                        "remark": remark or f"余额充值订单 {order_no}",
                        "created_at": now,
                    },
                )
                balance_credited = payable
        elif pay_channel == "balance" and payable > 0:
            # 余额支付：扣款 + 流水；先查是否已有该单的流水，防止「别处已扣、这里再扣」
            existed = db.query_one(
                "SELECT id FROM balance_logs WHERE ref_type='order' AND ref_id=? AND kind='purchase'",
                (order_no,),
            )
            if not existed:
                balance_before = int(user.get("balance_cents") or 0)
                if balance_before < payable:
                    raise HttpError(402, "余额不足，请先充值", code="payment_required")
                balance_after = balance_before - payable
                db.update(
                    "users", {"balance_cents": balance_after, "updated_at": now}, "id=?", (user["id"],)
                )
                db.insert(
                    "balance_logs",
                    {
                        "user_id": user["id"],
                        "change_cents": -payable,
                        "balance_after": balance_after,
                        "kind": "purchase",
                        "ref_type": "order",
                        "ref_id": order_no,
                        "remark": remark or f"余额支付订单 {order_no}",
                        "created_at": now,
                    },
                )
                balance_paid = payable

        package = None
        if not is_recharge and apply_package and fresh.get("package_id"):
            package = db.query_one("SELECT * FROM packages WHERE id=?", (fresh["package_id"],))
            if package and int(package["days"] or 0) > 0:
                days = int(package["days"])
                base = expire_before if (expire_before and not timeutil.is_expired(expire_before)) else None
                expire_after = quota_mod.merge_expire(base, days)
                days_added = days
                db.update(
                    "users",
                    {
                        "package_id": package["id"],
                        "package_expire_at": expire_after,
                        "updated_at": now,
                    },
                    "id=?",
                    (user["id"],),
                )

        merged_remark = (fresh.get("remark") or "")
        if remark:
            merged_remark = (merged_remark + " | " + remark).strip(" |")[:400]
        db.update(
            "orders",
            {
                "status": "paid",
                "trade_no": (trade_no or fresh.get("trade_no") or ""),
                "paid_at": now,
                "updated_at": now,
                "remark": merged_remark,
            },
            "order_no=?",
            (order_no,),
        )

    quota_mod.Quota.refresh(user["id"])
    paid_order = find_order(order_no) or dict(fresh)
    # 审计归属：管理员人工确认 -> admin；用户自己余额支付 -> user；支付回调 -> system
    if operator and operator.get("role") == "admin":
        scope = "admin"
    elif operator or pay_channel == "balance":
        scope = "user"
    else:
        scope = "system"
    logmod.audit(
        "order_paid",
        scope=scope,
        user=operator,
        target=order_no,
        detail=(
            f"渠道={pay_channel} 金额={payable} 天数={days_added} 交易号={trade_no or '-'} "
            f"余额扣款={balance_paid} 余额入账={balance_credited} 充值单={is_recharge}"
        ),
    )
    if days_added:
        package_name = (paid_order or {}).get("package_name") or "套餐"
        logmod.notify(
            user["id"],
            "套餐已开通",
            f"{package_name} 已生效 {days_added} 天，到期时间 {expire_after}",
        )
    elif balance_credited:
        logmod.notify(user["id"], "余额充值成功", f"订单 {order_no} 充值 {balance_credited / 100:.2f} 元")
    elif balance_paid:
        logmod.notify(user["id"], "余额支付成功", f"订单 {order_no} 已支付 {balance_paid / 100:.2f} 元")
    logmod.info(
        f"订单 {order_no} 已发放：渠道={pay_channel} 天数={days_added}"
        f" 余额扣款={balance_paid} 余额入账={balance_credited}",
        scope=scope,
        source="orders",
    )
    return {
        "order": paid_order,
        "already_paid": False,
        "days_added": days_added,
        "expire_before": expire_before,
        "expire_after": expire_after,
        "balance_paid_cents": balance_paid,
        "balance_credited_cents": balance_credited,
        "is_recharge": is_recharge,
        "channel": pay_channel,
    }


# --------------------------------------------------------------------------
# 卡密查询/管理辅助（admin.py 直接调用）
# --------------------------------------------------------------------------
CARD_SELECT = (
    "SELECT ck.id, ck.code, ck.code_hash, ck.card_type, ck.days, ck.use_limit, ck.used_count,"
    " ck.amount_cents, ck.status, ck.bound_user_id, ck.used_by, ck.used_at, ck.expire_at,"
    " ck.note, ck.created_at, ck.batch_id, ck.package_id,"
    " cb.batch_no AS batch_no, cb.note AS batch_note,"
    " p.name AS package_name, p.code AS package_code,"
    " bu.username AS bound_username, uu.username AS used_by_username"
    " FROM card_keys ck"
    " LEFT JOIN card_batches cb ON cb.id = ck.batch_id"
    " LEFT JOIN packages p ON p.id = ck.package_id"
    " LEFT JOIN users bu ON bu.id = ck.bound_user_id"
    " LEFT JOIN users uu ON uu.id = ck.used_by"
)


def card_rows(
    *,
    keyword: str = "",
    status: str = "",
    card_type: str = "",
    batch_no: str = "",
    package_id: int | None = None,
    start_date: str = "",
    end_date: str = "",
    order: str = "ck.id DESC",
    limit: int = 0,
) -> list[dict]:
    """按筛选条件取卡密（不分页；limit>0 时用于导出抽样）。"""
    sql = CARD_SELECT
    where: list[str] = []
    params: list[Any] = []
    if keyword:
        where.append("(ck.code LIKE ? OR ck.note LIKE ? OR cb.batch_no LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if status == "expired":
        where.append("(ck.status='expired' OR (ck.status='unused' AND ck.expire_at IS NOT NULL"
                     " AND ck.expire_at < ?))")
        params.append(timeutil.utc_now_str())
    elif status:
        where.append("ck.status=?")
        params.append(status)
    if card_type:
        where.append("ck.card_type=?")
        params.append(card_type)
    if batch_no:
        where.append("cb.batch_no=?")
        params.append(batch_no)
    if package_id:
        where.append("ck.package_id=?")
        params.append(int(package_id))
    if start_date:
        where.append("ck.created_at >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("ck.created_at <= ?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.query_all(sql, params)


def card_public(card: dict, *, reveal: bool = False) -> dict:
    return {
        "id": card.get("id"),
        "code": card.get("code") if reveal else mask_card_code(card.get("code") or ""),
        "batch_no": card.get("batch_no") or "",
        "card_type": card.get("card_type"),
        "days": int(card.get("days") or 0),
        "use_limit": int(card.get("use_limit") or 1),
        "used_count": int(card.get("used_count") or 0),
        "amount_cents": int(card.get("amount_cents") or 0),
        "status": effective_status(card),
        "package_id": card.get("package_id"),
        "package_name": card.get("package_name") or "",
        "bound_user_id": card.get("bound_user_id"),
        "bound_username": card.get("bound_username") or "",
        "used_by": card.get("used_by"),
        "used_by_username": card.get("used_by_username") or "",
        "used_at": card.get("used_at") or "",
        "expire_at": card.get("expire_at") or "",
        "note": card.get("note") or "",
        "created_at": card.get("created_at") or "",
        "benefit_text": benefit_text(
            str(card.get("card_type") or "time"),
            int(card.get("days") or 0),
            int(card.get("use_limit") or 1),
            int(card.get("amount_cents") or 0),
        ),
    }


def list_cards(page: int = 1, page_size: int = 20, **filters: Any) -> dict:
    """管理端卡密列表（分页 + 筛选）。"""
    sql = CARD_SELECT
    where: list[str] = []
    params: list[Any] = []
    keyword = str(filters.get("keyword") or "")
    status = str(filters.get("status") or "")
    card_type = str(filters.get("card_type") or "")
    batch_no = str(filters.get("batch_no") or "")
    package_id = filters.get("package_id")
    start_date = str(filters.get("start_date") or "")
    end_date = str(filters.get("end_date") or "")
    if keyword:
        where.append("(ck.code LIKE ? OR ck.note LIKE ? OR cb.batch_no LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if status == "expired":
        where.append(
            "(ck.status='expired' OR (ck.status='unused' AND ck.expire_at IS NOT NULL AND ck.expire_at < ?))"
        )
        params.append(timeutil.utc_now_str())
    elif status:
        where.append("ck.status=?")
        params.append(status)
    if card_type:
        where.append("ck.card_type=?")
        params.append(card_type)
    if batch_no:
        where.append("cb.batch_no=?")
        params.append(batch_no)
    if package_id:
        where.append("ck.package_id=?")
        params.append(int(package_id))
    if start_date:
        where.append("ck.created_at >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("ck.created_at <= ?")
        params.append(f"{end_date} 23:59:59")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ck.id DESC"
    result = db.paginate(sql, params, page, page_size)
    result["items"] = [card_public(row, reveal=True) for row in result["items"]]
    return result


def card_stats() -> dict:
    """卡密统计：各状态数量 + 批次汇总。"""
    rows = db.query_all(
        "SELECT status, COUNT(*) AS n, SUM(amount_cents) AS amount_cents FROM card_keys GROUP BY status"
    )
    counts = {status: 0 for status in CARD_STATUSES}
    total = 0
    balance_total = 0
    for row in rows:
        counts[str(row["status"])] = int(row["n"] or 0)
        total += int(row["n"] or 0)
        balance_total += int(row["amount_cents"] or 0)
    batches = db.query_all(
        "SELECT cb.batch_no, cb.card_type, cb.days, cb.use_limit, cb.amount_cents, cb.quantity,"
        " cb.note, cb.created_at, p.name AS package_name,"
        " SUM(CASE WHEN ck.status='used' THEN 1 ELSE 0 END) AS used_count,"
        " SUM(CASE WHEN ck.status='disabled' THEN 1 ELSE 0 END) AS disabled_count"
        " FROM card_batches cb"
        " LEFT JOIN card_keys ck ON ck.batch_id = cb.id"
        " LEFT JOIN packages p ON p.id = cb.package_id"
        " GROUP BY cb.id ORDER BY cb.id DESC LIMIT 50"
    )
    return {
        "total": total,
        "used": counts["used"],
        "unused": counts["unused"],
        "disabled": counts["disabled"],
        "expired": counts["expired"],
        "balance_amount_cents": balance_total,
        "batches": [
            {
                "batch_no": row["batch_no"],
                "card_type": row["card_type"],
                "days": int(row["days"] or 0),
                "use_limit": int(row["use_limit"] or 1),
                "amount_cents": int(row["amount_cents"] or 0),
                "quantity": int(row["quantity"] or 0),
                "used_count": int(row["used_count"] or 0),
                "disabled_count": int(row["disabled_count"] or 0),
                "package_name": row["package_name"] or "",
                "note": row["note"] or "",
                "created_at": row["created_at"],
            }
            for row in batches
        ],
    }


def change_card_status(
    card_id: int, status: str, operator: dict | None = None, ip: str = ""
) -> dict:
    """启用/停用卡密（不允许把已用完的卡"启用"回可兑换状态）。"""
    if status not in ("unused", "disabled", "expired"):
        raise HttpError(400, "卡密状态只能是 unused / disabled / expired")
    card = db.query_one("SELECT * FROM card_keys WHERE id=?", (card_id,))
    if not card:
        raise HttpError(404, "卡密不存在")
    used_count = int(card["used_count"] or 0)
    # 次数卡是一次性的：用过一次就不允许改回 unused（否则等于白送一份加成）
    exhausted = used_count > 0 if card["card_type"] == "count" else used_count >= max(
        1, int(card["use_limit"] or 1)
    )
    if status == "unused" and exhausted:
        raise HttpError(409, "该卡密已用完，不能改回未使用", code="card_used")
    before = card["status"]
    db.update("card_keys", {"status": status}, "id=?", (card_id,))
    logmod.audit(
        "card_status",
        scope="admin",
        user=operator,
        target=mask_card_code(card["code"]),
        detail=f"{before} -> {status}",
        ip=ip,
    )
    updated = db.query_one("SELECT * FROM card_keys WHERE id=?", (card_id,))
    return card_public(updated or card, reveal=True)


def delete_card(card_id: int, operator: dict | None = None, ip: str = "") -> dict:
    """删除卡密：只允许删除从未被兑换过的卡（已兑换的只能停用，否则流水会断链）。"""
    card = db.query_one("SELECT * FROM card_keys WHERE id=?", (card_id,))
    if not card:
        raise HttpError(404, "卡密不存在")
    if int(card["used_count"] or 0) > 0 or db.query_one(
        "SELECT id FROM card_redemptions WHERE card_key_id=?", (card_id,)
    ):
        raise HttpError(409, "该卡密已有兑换记录，不能删除，请改为停用", code="conflict")
    db.delete("card_keys", "id=?", (card_id,))
    logmod.audit(
        "card_delete",
        scope="admin",
        user=operator,
        target=mask_card_code(card["code"]),
        detail=f"删除未使用卡密（批次 {card.get('batch_id')}）",
        ip=ip,
    )
    return {"id": card_id, "deleted": True}


def export_cards(
    *,
    fmt: str = "csv",
    batch_no: str = "",
    status: str = "",
    card_type: str = "",
    keyword: str = "",
    limit: int = 5000,
) -> dict:
    """导出卡密内容（txt: 一行一张；csv: 带表头与状态的完整字段）。"""
    fmt = (fmt or "csv").lower()
    if fmt not in ("csv", "txt"):
        raise HttpError(400, "导出格式只能是 csv 或 txt")
    rows = card_rows(
        keyword=keyword,
        status=status,
        card_type=card_type,
        batch_no=batch_no,
        limit=max(1, min(int(limit or 5000), 50000)),
    )
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    if fmt == "txt":
        content = "\n".join(str(row["code"]) for row in rows) + ("\n" if rows else "")
        return {
            "filename": f"cards_{stamp}.txt",
            "content": content,
            "content_type": "text/plain; charset=utf-8",
            "count": len(rows),
        }
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["卡密", "批次号", "套餐", "类型", "天数", "可用次数", "已用次数", "面额(分)",
         "状态", "定向用户", "使用人", "使用时间", "激活截止", "备注", "创建时间"]
    )
    for row in rows:
        writer.writerow(
            [
                row["code"],
                row.get("batch_no") or "",
                row.get("package_name") or "",
                row.get("card_type") or "",
                row.get("days") or 0,
                row.get("use_limit") or 1,
                row.get("used_count") or 0,
                row.get("amount_cents") or 0,
                effective_status(row),
                row.get("bound_username") or "",
                row.get("used_by_username") or "",
                row.get("used_at") or "",
                row.get("expire_at") or "",
                row.get("note") or "",
                row.get("created_at") or "",
            ]
        )
    return {
        "filename": f"cards_{stamp}.csv",
        "content": buffer.getvalue(),
        "content_type": "text/csv; charset=utf-8",
        "count": len(rows),
    }


def redemption_rows(user_id: int, page: int, page_size: int) -> dict:
    """我的兑换记录。"""
    sql = (
        "SELECT r.id, r.kind, r.days_added, r.amount_cents, r.expire_before, r.expire_after,"
        " r.created_at, ck.code, ck.card_type, ck.use_limit, ck.used_count,"
        " cb.batch_no, p.name AS package_name"
        " FROM card_redemptions r"
        " LEFT JOIN card_keys ck ON ck.id = r.card_key_id"
        " LEFT JOIN card_batches cb ON cb.id = ck.batch_id"
        " LEFT JOIN packages p ON p.id = r.package_id"
        " WHERE r.user_id=? ORDER BY r.id DESC"
    )
    result = db.paginate(sql, [user_id], page, page_size)
    result["items"] = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "code": mask_card_code(row.get("code") or ""),
            "card_type": row.get("card_type") or "",
            "batch_no": row.get("batch_no") or "",
            "package_name": row.get("package_name") or "",
            "days_added": int(row.get("days_added") or 0),
            "amount_cents": int(row.get("amount_cents") or 0),
            "expire_before": row.get("expire_before") or "",
            "expire_after": row.get("expire_after") or "",
            "created_at": row.get("created_at") or "",
        }
        for row in result["items"]
    ]
    return result


# --------------------------------------------------------------------------
# 路由（用户端；管理端卡密接口在 admin.py，共用上面的辅助函数）
# --------------------------------------------------------------------------
@route(
    "POST",
    "/user/card-keys/redeem",
    auth="user",
    summary="兑换卡密（激活/续期/充值）",
    tags=("card",),
)
def redeem_route(req: Request) -> Response:
    user = req.require_user()
    code = req.str_field("code", "", max_len=64)
    if not code:
        raise _card_invalid("请输入卡密")
    data = redeem_card(user, code, req.client_ip)
    return ok(data, data.get("message") or "ok")


@route("GET", "/user/card-keys", auth="user", summary="我的卡密兑换记录", tags=("card",))
def my_redemptions(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    return ok(redemption_rows(user["id"], page, page_size))
