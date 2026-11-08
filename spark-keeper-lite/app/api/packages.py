"""套餐与购买接口（契约 §6 前半）：``/user/packages*``。

注意：``GET /packages``（公开套餐列表）与 ``/user/card-keys*`` **不在这里**，
按与队友 B 的划分归 ``app/api/public.py`` 与 ``app/api/cardkeys.py``，
本文件只注册 ``/user/packages*`` 五条路由，避免同路径重复注册被静默覆盖。

职责边界
--------
* 套餐列表、配额详情/轻量状态、支付方式列表、下单（购买/充值）的参数校验与落库。
* 订单行的创建与支付渠道交互在 ``orders.py``（``create_order`` / ``start_online_payment``）；
  订单**履约**（置 paid、加天数、余额入账）在 B 的 ``cardkeys.fulfill_order``。
  本模块不碰 balance_logs，也不直接改 users.package_expire_at。

坑
--
1. 金额单位是「分」，字段名 ``*_cents``；展示时才换算成元。
2. 余额支付必须**先自校验余额**再调履约：履约函数在 B 那边，不能假设它一定拦负数。
   两处都拦，宁可重复也不要出现负余额。
3. 在线渠道（easypay/alipay/wechat）在 ``pay.enabled=0`` 时必须 400 ``pay_disabled``，
   不能因为没有支付模块就给出一个点不开的收银台。
4. 价格为 0 的套餐（种子数据里的 trial）不能下单：0 元订单在三方通道上是无效请求，
   也会让「已支付金额」统计出现无意义数据。此类套餐走管理员手动授权。
5. ``channel=card`` 只生成订单（用卡密支付），``channel=manual`` 等人工确认收款；
   这两种不调支付通道，但同样要落 orders 行，便于对账。
"""

from __future__ import annotations

from .. import db
from ..common import logging as logmod
from ..common import quota as quotamod
from ..common import textutil
from ..router import HttpError, Request, Response, ok, route
from . import orders as orders_api

MIN_RECHARGE_CENTS = 100          # 最低充值 1 元
MAX_RECHARGE_CENTS = 1_000_000    # 单笔上限 1 万元，防手滑


# --------------------------------------------------------------------------
# 套餐
# --------------------------------------------------------------------------
def _package_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "code": row.get("code") or "",
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "days": int(row.get("days") or 0),
        "price_cents": int(row.get("price_cents") or 0),
        "original_cents": (int(row["original_cents"]) if row.get("original_cents") is not None else None),
        "account_limit": int(row.get("account_limit") or 0),
        "daily_task_limit": int(row.get("daily_task_limit") or 0),
        "features": textutil.as_str_list(row.get("features_json")),
        "sort_order": int(row.get("sort_order") or 0),
        "is_active": bool(row.get("is_active", 1)),
    }


def active_packages() -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM packages WHERE is_active=1 ORDER BY sort_order, id"
    )
    return [_package_out(row) for row in rows]


def _limit_payload(user_id: int) -> dict:
    """配额快照 + 「现在能不能加账号/能不能跑任务」这两个前端最关心的布尔值。"""
    quota = quotamod.Quota.refresh(user_id)
    data = quota.summary()
    account_used = int(data.get("account_used") or 0)
    run_used = int(data.get("daily_task_used") or 0)
    account_limit = int(data.get("account_limit") or 0)
    run_limit = int(data.get("daily_task_limit") or 0)
    data.update({
        "account_remaining": max(0, account_limit - account_used),
        "daily_task_remaining": max(0, run_limit - run_used),
        "can_add_account": bool(data.get("active")) and account_used < account_limit,
        "can_run_today": bool(data.get("active")) and run_used < run_limit,
        "upgrade_hint": "" if data.get("active") else "套餐未激活或已过期，请先兑换卡密或购买套餐",
    })
    return data


@route("GET", "/api/v1/user/packages", auth="user", summary="套餐列表 + 我的当前套餐与限制", tags=("packages",))
def my_packages(req: Request) -> Response:
    user = req.require_user()
    limits = _limit_payload(user["id"])
    return ok({
        "items": active_packages(),
        "current": {
            "package_id": user.get("package_id"),
            "package_code": limits.get("package_code", ""),
            "package_name": limits.get("package_name", ""),
            "expire_at": limits.get("expire_at"),
            "expire_text": limits.get("expire_text", ""),
            "active": limits.get("active", False),
        },
        "limits": limits,
        "balance_cents": int(user.get("balance_cents") or 0),
    })


@route("GET", "/api/v1/user/packages/limit-info", auth="user", summary="配额详情", tags=("packages",))
def limit_info(req: Request) -> Response:
    user = req.require_user()
    return ok(_limit_payload(user["id"]))


@route("GET", "/api/v1/user/packages/limit-status", auth="user",
       summary="轻量配额状态（前端轮询）", tags=("packages",))
def limit_status(req: Request) -> Response:
    user = req.require_user()
    # 与 limit-info 同源同结构：前端轮询只要一个稳定的小 JSON，不做额外查询
    return ok(_limit_payload(user["id"]))


@route("GET", "/api/v1/user/packages/pay-methods", auth="user", summary="可用支付方式", tags=("packages",))
def pay_methods(req: Request) -> Response:
    user = req.require_user()
    online = orders_api.online_pay_enabled()

    # pro 版网关自己维护渠道清单（含图标/说明），有就用它的
    gateway = orders_api.load_pay_gateway()
    provider = getattr(gateway, "payment_methods", None) if gateway is not None else None
    if callable(provider):
        try:
            data = provider()
        except Exception as exc:      # noqa: BLE001 - 渠道清单拿不到就回落内置清单
            logmod.warning(f"支付渠道清单获取失败：{type(exc).__name__}: {exc}", source="api.packages")
            data = None
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            # 以 site_configs.pay.enabled 为准修正网关注解：契约明确「pay.enabled=0 时只返回卡密」，
            # 网关自己的判断可能只是「渠道配好了」，两回事，这里统一口径。
            data["online_pay_enabled"] = online
            for item in data["items"]:
                if isinstance(item, dict) and item.get("channel") in orders_api.ONLINE_CHANNELS:
                    item["enabled"] = bool(item.get("enabled")) and online
            data["balance_cents"] = int(user.get("balance_cents") or 0)
            return ok(data)

    # lite 版（或网关异常）：卡密 + 余额永远可用；在线渠道按 pay.enabled 决定
    items = [
        {"channel": "card", "name": "卡密兑换", "enabled": True, "icon": "key",
         "description": "用卡密激活或续期套餐"},
        {"channel": "balance", "name": "余额支付", "enabled": True, "icon": "wallet",
         "description": "用账户余额直接购买"},
        {"channel": "easypay", "name": "支付宝/微信", "enabled": online, "icon": "qr",
         "description": "扫码支付，需在后台开启在线支付"},
        {"channel": "manual", "name": "人工确认", "enabled": online, "icon": "manual",
         "description": "联系客服线下转账，管理员确认后开通"},
    ]
    return ok({
        "items": items,
        "online_pay_enabled": online,
        "balance_cents": int(user.get("balance_cents") or 0),
    })


# --------------------------------------------------------------------------
# 下单
# --------------------------------------------------------------------------
def _order_row(order_no: str) -> dict:
    return db.query_one(
        "SELECT o.*, p.name AS package_name FROM orders o"
        " LEFT JOIN packages p ON p.id = o.package_id WHERE o.order_no=?",
        (order_no,),
    ) or {}


def _validate_online_or_raise(channel: str) -> None:
    if channel in orders_api.ONLINE_CHANNELS and not orders_api.online_pay_enabled():
        raise HttpError(400, "在线支付未启用，请使用卡密兑换或余额支付", code="pay_disabled")


@route("POST", "/api/v1/user/packages/purchase", auth="user", summary="下单购买套餐", tags=("packages",))
def purchase(req: Request) -> Response:
    user = req.require_user()
    package_id = req.int_field("package_id", 0, minimum=1)
    package = db.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
    if not package or not int(package.get("is_active") or 0):
        raise HttpError(404, "套餐不存在或已下架", code="not_found")

    channel = req.str_field("channel", "card", max_len=16).lower()
    if channel not in orders_api.ORDER_CHANNELS:
        raise HttpError(400, f"channel 只能是 {'/'.join(orders_api.ORDER_CHANNELS)}")
    amount = int(package.get("price_cents") or 0)
    if amount <= 0:
        raise HttpError(400, "该套餐价格为 0，无需下单（可联系管理员直接开通）")
    _validate_online_or_raise(channel)

    if channel == "balance":
        balance = int(user.get("balance_cents") or 0)
        if balance < amount:
            # 履约在 B 那边，这里先拦一次，双保险避免负余额
            raise HttpError(
                402,
                f"余额不足：当前 {orders_api.fmt_yuan(balance)} 元，需要 {orders_api.fmt_yuan(amount)} 元",
                code="payment_required",
            )

    order = orders_api.create_order(
        user=user,
        subject=f"购买套餐：{package['name']}",
        amount_cents=amount,
        channel=channel,
        package=package,
        remark="purchase",
        client_ip=req.client_ip,
    )

    if channel == "balance":
        orders_api.fulfill_order(order["order_no"], channel="balance", operator=user,
                                 remark=f"余额支付购买 {package['name']}")
        fresh = _order_row(order["order_no"])
        data = orders_api.serialize_order(fresh)
        data["balance_cents"] = int(db.query_scalar(
            "SELECT balance_cents FROM users WHERE id=?", (user["id"],), 0) or 0)
        logmod.audit("purchase_package", user=user, target=str(order["order_no"]),
                     detail=f"balance {amount}", ip=req.client_ip)
        return ok(data, message="支付成功，套餐已开通")

    if channel in orders_api.ONLINE_CHANNELS:
        orders_api.start_online_payment(req, order, user)

    data = orders_api.serialize_order(_order_row(order["order_no"]))
    if channel == "card":
        data["pay_hint"] = "订单已生成，请到「卡密兑换」输入卡密完成支付"
    elif channel == "manual":
        data["pay_hint"] = "请联系客服完成转账，管理员确认收款后自动开通"
    logmod.audit("create_purchase_order", user=user, target=str(order["order_no"]),
                 detail=f"{channel} {amount}", ip=req.client_ip)
    return ok(data, message="下单成功")


@route("POST", "/api/v1/user/packages/recharge", auth="user", summary="余额充值下单", tags=("packages",))
def recharge(req: Request) -> Response:
    user = req.require_user()
    amount = req.int_field("amount_cents", 0, minimum=MIN_RECHARGE_CENTS, maximum=MAX_RECHARGE_CENTS)
    channel = req.str_field("channel", "card", max_len=16).lower()
    if channel not in orders_api.ORDER_CHANNELS:
        raise HttpError(400, f"channel 只能是 {'/'.join(orders_api.ORDER_CHANNELS)}")
    if channel == "balance":
        raise HttpError(400, "余额充值不能用余额支付")
    _validate_online_or_raise(channel)

    # package_id 为空 = 充值单：履约时按「入账」而不是「加套餐天数」处理
    order = orders_api.create_order(
        user=user,
        subject=f"余额充值 {orders_api.fmt_yuan(amount)} 元",
        amount_cents=amount,
        channel=channel,
        package=None,
        remark="recharge",
        client_ip=req.client_ip,
    )
    if channel in orders_api.ONLINE_CHANNELS:
        orders_api.start_online_payment(req, order, user)

    data = orders_api.serialize_order(_order_row(order["order_no"]))
    if channel == "card":
        data["pay_hint"] = "订单已生成，请到「卡密兑换」输入卡密完成充值"
    elif channel == "manual":
        data["pay_hint"] = "请联系客服完成转账，管理员确认收款后自动到账"
    logmod.audit("create_recharge_order", user=user, target=str(order["order_no"]),
                 detail=f"{channel} {amount}", ip=req.client_ip)
    return ok(data, message="充值订单已创建")
