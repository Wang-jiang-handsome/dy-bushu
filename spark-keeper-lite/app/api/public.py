"""公开接口：未登录也能读的站点数据（契约第 6 节的 ``GET /packages``）。

这里只放**确实需要免登录**的东西：套餐价目表（注册页要先看价格）。
其余 ``/user/*``、``/auth/*`` 都在各自模块里，避免"公开接口顺手多返回点东西"
导致越权——公开接口最容易出这种事故。
"""

from __future__ import annotations

from .. import db
from ..common import textutil
from ..router import Request, Response, ok, route


def package_public(row: dict) -> dict:
    """套餐对外结构（价格一律「分」；features 从 TEXT 里解析成数组）。"""
    return {
        "id": row.get("id"),
        "code": row.get("code") or "",
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "days": int(row.get("days") or 0),
        "price_cents": int(row.get("price_cents") or 0),
        "original_cents": int(row["original_cents"]) if row.get("original_cents") is not None else None,
        "account_limit": int(row.get("account_limit") or 0),
        "daily_task_limit": int(row.get("daily_task_limit") or 0),
        "features": textutil.as_str_list(row.get("features_json")),
        "sort_order": int(row.get("sort_order") or 0),
        "is_active": bool(row.get("is_active")),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def active_packages() -> list[dict]:
    rows = db.query_all("SELECT * FROM packages WHERE is_active=1 ORDER BY sort_order, id")
    return [package_public(row) for row in rows]


@route("GET", "/api/v1/packages", auth="public", summary="套餐列表（公开）", tags=("packages",))
def list_packages(req: Request) -> Response:
    return ok({"items": active_packages()})
