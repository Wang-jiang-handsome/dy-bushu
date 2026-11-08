"""订单接口（契约 §6 后半）：我的订单、详情、取消、主动查单。

职责边界
--------
* ``orders`` 表的读写、状态机、金额单位（分）、订单对外结构在本模块。
* **订单履约（置 paid + 加套餐天数 + 余额入账 + 写流水）不在本模块实现**，
  统一委托队友 B 的 ``app.api.cardkeys.fulfill_order()``（延迟 import）。
  为什么：支付回调、管理员确认收款、卡密兑换、余额支付四条路径都要履约，
  分散实现必然出现「某条路径忘了加天数/忘了写流水」。这里只做「单据状态」这一侧。
* 在线支付渠道（easypay/alipay/wechat）委托 pro 版 ``app.payment.gateway``；
  lite 版没有该模块，一律降级为 400 ``pay_disabled``。

坑
--
1. 金额一律用「分」存整数，展示给用户时才换算成元（``fmt_yuan``）；任何地方
   都不要引入浮点运算，否则会出现「0.1+0.2」这类对账差异。
2. ``pay_payload`` 存渠道原始返回（JSON 文本）：排障时的唯一依据，**下单失败也要留**。
3. ``_expire_if_needed``：pending 且已过 ``expire_at`` 的订单在读取时顺手落成
   expired，避免「过期订单还能点支付」和统计里 pending 永远不清零。
4. 主动查单是**补偿手段**（回调丢了的情况）：查到 paid 后必须再兜底调一次
   ``fulfill_order``，因为「通道说收到钱了」和「我们的订单已发放」是两件事。
5. ``pay_gateway`` 的调用要用 ``ok`` 字段判断成功与否，不要靠 message 关键字猜。
"""

from __future__ import annotations

import traceback
from datetime import timedelta
from typing import Any, Callable

from .. import db
from ..bootstrap import get_config
from ..common import logging as logmod
from ..common import security, textutil, timeutil
from ..config import get_settings
from ..router import HttpError, Request, Response, ok, route
from .accounts import import_optional

ORDER_CHANNELS = ("card", "easypay", "alipay", "wechat", "balance", "manual")
ONLINE_CHANNELS = ("easypay", "alipay", "wechat")
PENDING_STATUSES = ("pending",)
FINAL_STATUSES = ("paid", "failed", "canceled", "refunded", "expired")
ORDER_TTL_SECONDS = 1800                      # 未支付订单 30 分钟自动过期
CHANNEL_NAMES = {
    "card": "卡密兑换",
    "easypay": "支付宝/微信",
    "alipay": "支付宝",
    "wechat": "微信支付",
    "balance": "余额支付",
    "manual": "人工确认",
}
_SORTABLE = {"id": "o.id", "created_at": "o.created_at", "paid_at": "o.paid_at",
             "amount_cents": "o.amount_cents", "status": "o.status"}


# --------------------------------------------------------------------------
# 支付网关（pro 版可选模块）
# --------------------------------------------------------------------------
def load_pay_gateway() -> Any | None:
    """延迟 import pro 版支付网关；lite 版返回 None（调用方降级）。"""
    return import_optional("app.payment.gateway")


def online_pay_enabled() -> bool:
    """在线支付是否启用（读 site_configs.pay.enabled，与 B 的网关同一数据源）。"""
    return get_config("pay.enabled", "0") == "1"


def call_gateway(func: Callable, name: str, *args, **kwargs) -> dict:
    """调用支付网关并统一异常语义：通道报错 → 400 ``pay_channel_error``。"""
    try:
        result = func(*args, **kwargs)
    except HttpError:
        raise
    except Exception as exc:      # noqa: BLE001 - 第三方通道/网络失败是业务可预期状态
        message = f"支付通道调用失败（{name}）：{type(exc).__name__}: {exc}"
        logmod.error(message, source="api.orders",
                     context={"traceback": traceback.format_exc()[-2000:]})
        raise HttpError(400, message, code="pay_channel_error") from exc
    return result if isinstance(result, dict) else {}


def fulfill_order(order_no: str, *, channel: str = "", trade_no: str = "",
                  remark: str = "", operator: dict | None = None) -> dict:
    """订单履约入口（转调 ``app.api.cardkeys.fulfill_order``，幂等）。

    结算侧（置 paid / 加天数 / 余额入账 / 写 balance_logs / 审计）全部由 B 实现；
    本函数只负责「模块不在时给出可读错误，而不是 500」。
    """
    module = import_optional("app.api.cardkeys")
    func = getattr(module, "fulfill_order", None) if module is not None else None
    if not callable(func):
        raise HttpError(503, "订单履约模块未就绪（app.api.cardkeys.fulfill_order 不可用），请联系管理员",
                        code="server_error")
    try:
        return func(order_no, trade_no, operator,
                    channel=channel, remark=remark, apply_package=True) or {}
    except HttpError:
        raise
    except Exception as exc:      # noqa: BLE001 - 履约异常转 500 前的可读包装
        message = f"订单履约失败：{type(exc).__name__}: {exc}"
        logmod.error(message, source="api.orders",
                     context={"traceback": traceback.format_exc()[-2000:]})
        raise HttpError(500, message, code="server_error") from exc


# --------------------------------------------------------------------------
# 订单行
# --------------------------------------------------------------------------
def create_order(*, user: dict, subject: str, amount_cents: int, channel: str,
                 package: dict | None = None, remark: str = "", client_ip: str = "") -> dict:
    """建一张 pending 订单并返回整行。金额/渠道/套餐合法性由调用方先校验。"""
    now = timeutil.utc_now_str()
    order_no = security.new_order_no("SK")
    order_id = db.insert("orders", {
        "order_no": order_no,
        "user_id": user["id"],
        "package_id": package["id"] if package else None,
        "subject": subject[:200],
        "amount_cents": int(amount_cents),
        "payable_cents": int(amount_cents),
        "channel": channel,
        "status": "pending",
        "pay_payload": "{}",
        "expire_at": timeutil.to_db(timeutil.now_utc() + timedelta(seconds=ORDER_TTL_SECONDS)),
        "remark": remark[:500],
        "client_ip": client_ip,
        "created_at": now,
        "updated_at": now,
    })
    return db.query_one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}


def get_order(req: Request, order_no: str) -> dict:
    """按单号取订单并强制校验归属。"""
    user = req.require_user()
    no = (order_no or "").strip()
    if not no:
        raise HttpError(400, "缺少订单号")
    row = db.query_one(
        "SELECT o.*, p.name AS package_name, p.code AS package_code\n"
        "  FROM orders o LEFT JOIN packages p ON p.id = o.package_id\n"
        " WHERE o.order_no=? AND o.user_id=?",
        (no, user["id"]),
    )
    if not row:
        raise HttpError(404, "订单不存在或不属于当前用户", code="not_found")
    return _expire_if_needed(row)


def _expire_if_needed(row: dict) -> dict:
    """pending 且已过期 → 落成 expired（读取时顺手收尾，见模块头坑 3）。"""
    if row.get("status") == "pending" and timeutil.is_expired(row.get("expire_at")):
        now = timeutil.utc_now_str()
        db.update("orders", {"status": "expired", "updated_at": now},
                  "order_no=? AND status='pending'", (row["order_no"],))
        row = dict(row)
        row.update({"status": "expired", "updated_at": now})
    return row


def serialize_order(row: dict) -> dict:
    payload = textutil.as_dict(row.get("pay_payload"), {})
    qr_code = str(payload.get("qr_code") or payload.get("code_url") or "")
    status = row.get("status") or "pending"
    # 展示用状态：pending 但已过期时按 expired 呈现（列表接口也走这里）
    shown = "expired" if (status == "pending" and timeutil.is_expired(row.get("expire_at"))) else status
    channel = row.get("channel") or ""
    return {
        "order_no": row.get("order_no"),
        "package_id": row.get("package_id"),
        "package_name": row.get("package_name") or "",
        "subject": row.get("subject") or "",
        "amount_cents": int(row.get("amount_cents") or 0),
        "payable_cents": int(row.get("payable_cents") or 0),
        "channel": channel,
        "channel_name": CHANNEL_NAMES.get(channel, channel),
        "status": shown,
        "trade_no": row.get("trade_no") or "",
        "pay_url": row.get("pay_url") or "",
        "qr_code": qr_code,
        "paid_at": row.get("paid_at"),
        "expire_at": row.get("expire_at"),
        "notify_count": int(row.get("notify_count") or 0),
        "last_notify_at": row.get("last_notify_at"),
        "remark": row.get("remark") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "is_paid": status == "paid",
        "can_pay": shown == "pending",
        "can_cancel": shown == "pending",
    }


def fmt_yuan(cents: int) -> str:
    return f"{int(cents or 0) / 100:.2f}"


def start_online_payment(req: Request, order: dict, user: dict) -> dict:
    """调起在线支付：调网关下单，把 ``pay_url``/``qr_code``/``trade_no`` 落库。

    ``packages.py`` 的购买/充值都走这里，避免两个接口各自 import 支付模块。
    失败时订单**保持 pending**（用户可重试），渠道原始返回写进 ``pay_payload`` 便于排障。
    """
    gateway = load_pay_gateway()
    creator = getattr(gateway, "create_pay_order", None) if gateway is not None else None
    if not callable(creator):
        raise HttpError(503, "在线支付模块未就绪（app.payment.gateway.create_pay_order 不可用）",
                        code="pay_disabled")
    base_url = get_settings().public_base_url(req.headers.get("host", ""))
    result = call_gateway(creator, "create_pay_order", order, user,
                          base_url=base_url, client_ip=req.client_ip)
    now = timeutil.utc_now_str()
    payload = textutil.dumps(result)
    if not result.get("ok"):
        # 订单保持 pending：用户可换渠道重试，原始返回留档
        db.update("orders", {"pay_payload": payload, "updated_at": now},
                  "order_no=? AND user_id=? AND status='pending'",
                  (order["order_no"], user["id"]))
        message = str(result.get("message") or "支付通道下单失败，请稍后重试")
        raise HttpError(400, message, code="pay_channel_error")

    db.update("orders", {
        "pay_url": str(result.get("pay_url") or "")[:1000],
        "trade_no": str(result.get("trade_no") or "")[:200],
        "pay_payload": payload,
        "updated_at": now,
    }, "order_no=? AND user_id=? AND status='pending'", (order["order_no"], user["id"]))
    return db.query_one("SELECT * FROM orders WHERE order_no=?", (order["order_no"],)) or order


def _order_by(req: Request, default: str = "-id") -> str:
    raw = req.str_field("sort", "", max_len=40).strip() or default
    desc = raw.startswith("-")
    column = _SORTABLE.get(raw.lstrip("+-"))
    if not column:
        desc = default.startswith("-")
        column = _SORTABLE[default.lstrip("+-")]
    return f"{column} {'DESC' if desc else 'ASC'}"


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
@route("GET", "/api/v1/user/orders", auth="user", summary="我的订单（分页）", tags=("orders",))
def list_orders(req: Request) -> Response:
    user = req.require_user()
    page, page_size = req.page_args()
    where = ["o.user_id=?"]
    params: list = [user["id"]]
    status = req.str_field("status", "", max_len=20)
    if status:
        if status not in FINAL_STATUSES + PENDING_STATUSES:
            raise HttpError(400, "status 取值不正确")
        where.append("o.status=?")
        params.append(status)
    channel = req.str_field("channel", "", max_len=16)
    if channel:
        if channel not in ORDER_CHANNELS:
            raise HttpError(400, "channel 取值不正确")
        where.append("o.channel=?")
        params.append(channel)
    keyword = req.str_field("keyword", "", max_len=100)
    if keyword:
        where.append("(o.order_no LIKE ? OR o.subject LIKE ? OR o.trade_no LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    start = req.str_field("start_date", "", max_len=20)
    if start:
        where.append("o.created_at>=?")
        params.append(start)
    end = req.str_field("end_date", "", max_len=20)
    if end:
        where.append("o.created_at<=?")
        params.append(f"{end} 23:59:59" if len(end) == 10 else end)

    base = (
        "SELECT o.*, p.name AS package_name\n"
        "  FROM orders o LEFT JOIN packages p ON p.id = o.package_id\n"
        f" WHERE {' AND '.join(where)} ORDER BY {_order_by(req, '-id')}"
    )
    result = db.paginate(base, params, page, page_size)
    result["items"] = [serialize_order(row) for row in result["items"]]
    # 顶部小计：前端订单页要显示「已支付总额」
    summary = db.query_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='paid' THEN payable_cents ELSE 0 END) AS paid_cents\n"
        "  FROM orders WHERE user_id=?",
        (user["id"],),
    ) or {}
    result["summary"] = {
        "total": int(summary.get("total") or 0),
        "paid_cents": int(summary.get("paid_cents") or 0),
    }
    return ok(result)


@route("GET", "/api/v1/user/orders/<order_no>", auth="user", summary="订单详情", tags=("orders",))
def order_detail(req: Request, order_no: str) -> Response:
    return ok(serialize_order(get_order(req, order_no)))


@route("POST", "/api/v1/user/orders/<order_no>/cancel", auth="user",
       summary="取消未支付订单", tags=("orders",))
def cancel_order(req: Request, order_no: str) -> Response:
    row = get_order(req, order_no)
    if row.get("status") == "paid":
        raise HttpError(409, "订单已支付，无法取消（如需退款请联系客服）", code="conflict")
    if row.get("status") != "pending":
        raise HttpError(409, f"订单当前状态为 {row.get('status')}，无法取消", code="conflict")
    now = timeutil.utc_now_str()
    # 带 status='pending' 条件更新：并发下避免把刚支付成功的订单改成 canceled
    updated = db.update("orders", {"status": "canceled", "updated_at": now},
                        "order_no=? AND user_id=? AND status='pending'",
                        (row["order_no"], req.user_id))
    if not updated:
        raise HttpError(409, "订单状态已变化，请刷新后重试", code="conflict")
    logmod.audit("cancel_order", user=req.user, target=str(row["order_no"]), ip=req.client_ip)
    return ok(serialize_order(get_order(req, order_no)), message="订单已取消")


@route("POST", "/api/v1/user/orders/<order_no>/query", auth="user",
       summary="主动向支付通道查单（补偿回调丢失）", tags=("orders",))
def query_order(req: Request, order_no: str) -> Response:
    row = get_order(req, order_no)
    channel = row.get("channel") or ""

    if row.get("status") == "paid":
        return ok({**serialize_order(row), "paid": True, "queried": False,
                   "message": "订单已支付"}, message="订单已支付")

    # 卡密/人工渠道没有第三方通道可查：如实告知，不要假装查过
    if channel in ("card", "manual"):
        return ok({**serialize_order(row), "paid": False, "queried": False,
                   "message": "该订单使用卡密兑换/人工确认，无第三方通道可查询"},
                  message="等待支付确认")

    if channel == "balance":
        # 余额支付是同步完成的：走到这里说明当时扣款失败，让用户重新下单
        return ok({**serialize_order(row), "paid": False, "queried": False,
                   "message": "余额支付未完成，请重新下单或检查余额"},
                  message="未支付")

    if not online_pay_enabled():
        raise HttpError(400, "在线支付未启用，无法查询支付状态", code="pay_disabled")
    gateway = load_pay_gateway()
    query = getattr(gateway, "query_order", None) if gateway is not None else None
    if not callable(query):
        raise HttpError(503, "支付模块未就绪（app.payment.gateway.query_order 不可用）",
                        code="pay_disabled")

    result = call_gateway(query, "query_order", row["order_no"])
    paid = bool(result.get("paid"))
    trade_no = str(result.get("trade_no") or "")
    if paid and row.get("status") != "paid":
        # 通道说收到钱了 → 兜底履约（B 的 fulfill_order 幂等，重复调安全）
        fulfill_order(row["order_no"], channel=channel, trade_no=trade_no, remark="主动查单补偿")
        logmod.audit("order_query_paid", user=req.user, target=str(row["order_no"]), ip=req.client_ip)
    fresh = get_order(req, row["order_no"])
    return ok({
        **serialize_order(fresh),
        "paid": bool(fresh.get("status") == "paid"),
        "queried": True,
        "channel_status": str(result.get("status") or ""),
        "message": str(result.get("message") or ("支付成功" if paid else "尚未支付")),
    }, message="查询完成")
