"""文件下载：任务截图与卡密导出（契约第 8 节）。

两个接口的安全要点
------------------
1. ``/files/screenshot/<run_id>/<name>``：截图是用户隐私（抖音私信页面），必须
   按 ``task_runs.user_id`` 校验归属；管理员可以看全部。文件名只取 basename，
   解析后还要确认落在 ``screenshot_root`` 之内，防目录穿越。
2. ``/files/export/cards/<batch_no>.csv``：卡密就是钱，**必须 admin**，且导出动作
   写审计日志（谁在什么时候导出了哪个批次）。

截图目录兼容多种落盘布局（``<root>/<run_id>/x.png``、``<root>/run_<id>/x.png``、
或按日期分目录），因为自动化模块有可能按自己方便的方式组织文件。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from .. import db
from ..common import logging as logmod
from ..config import get_settings
from ..router import HttpError, Request, Response, route
from . import cardkeys


def _resolve_screenshot(run_id: int, name: str) -> Path | None:
    """在 screenshot_root 下定位截图；找不到或越界返回 None。

    只接受"目录名里的数字恰好等于 run_id"的候选路径——否则 ``*12*`` 会匹配到
    ``run_112``，把别人的截图发出去（截图是用户隐私）。
    """
    root = get_settings().screenshot_root
    try:
        root = root.resolve()
    except OSError:
        return None
    safe_name = Path(name).name
    if not safe_name or safe_name != name or safe_name.startswith("."):
        return None

    found: Path | None = None
    for candidate in (
        root / str(run_id) / safe_name,
        root / f"run_{run_id}" / safe_name,
    ):
        if candidate.is_file():
            found = candidate
            break
    if found is None:
        for pattern in (f"*{run_id}*/{safe_name}", f"*/*{run_id}*/{safe_name}"):
            for match in root.glob(pattern):
                if not match.is_file():
                    continue
                digits = "".join(ch for ch in match.parent.name if ch.isdigit())
                if digits == str(run_id):
                    found = match
                    break
            if found is not None:
                break
    if found is None:
        return None
    try:
        resolved = found.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None                     # 符号链接越界
    return resolved


@route(
    "GET",
    "/api/v1/files/screenshot/<run_id>/<name>",
    auth="user",
    summary="任务截图（校验归属）",
    tags=("files",),
)
def screenshot(req: Request, run_id: str, name: str) -> Response:
    user = req.require_user()
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise HttpError(404, "截图不存在") from None
    run = db.query_one("SELECT id, user_id FROM task_runs WHERE id=?", (rid,))
    if not run:
        raise HttpError(404, "执行记录不存在")
    if user.get("role") != "admin" and int(run["user_id"]) != req.user_id:
        # 不返回 404 而是 403：执行记录本身对用户可见，这里明确是权限问题
        raise HttpError(403, "无权访问该执行记录的截图")
    path = _resolve_screenshot(rid, name)
    if path is None:
        raise HttpError(404, "截图不存在或已被清理")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HttpError(404, f"截图读取失败：{exc}") from exc
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return Response(
        status=200,
        raw=data,
        content_type=content_type,
        headers={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"},
    )


@route(
    "GET",
    "/api/v1/files/export/cards/<batch_no>.csv",
    auth="admin",
    summary="导出卡密 CSV（admin）",
    tags=("files",),
)
def export_cards_csv(req: Request, batch_no: str) -> Response:
    admin = req.require_admin()
    batch = db.query_one("SELECT * FROM card_batches WHERE batch_no=?", (batch_no,))
    if not batch:
        raise HttpError(404, "批次不存在")
    status = req.str_field("status", "", max_len=16)
    rows = cardkeys.card_rows(batch_no=batch_no, status=status, limit=50000)
    lines = [
        "卡密,批次号,套餐,类型,天数,每日额度或可兑次数,已用次数,面额(分),状态,"
        "定向用户,使用人,使用时间,激活截止,备注,创建时间"
    ]
    for row in rows:
        cells = [
            str(row["code"]),
            str(row.get("batch_no") or ""),
            str(row.get("package_name") or ""),
            str(row.get("card_type") or ""),
            str(row.get("days") or 0),
            str(row.get("use_limit") or 1),
            str(row.get("used_count") or 0),
            str(row.get("amount_cents") or 0),
            cardkeys.effective_status(row),
            str(row.get("bound_username") or ""),
            str(row.get("used_by_username") or ""),
            str(row.get("used_at") or ""),
            str(row.get("expire_at") or ""),
            str(row.get("note") or "").replace(",", " "),
            str(row.get("created_at") or ""),
        ]
        lines.append(",".join(cells))
    # 带 BOM，Excel 直接双击打开不乱码
    content = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
    logmod.audit(
        "card_export_csv",
        scope="admin",
        user=admin,
        target=batch_no,
        detail=f"导出 {len(rows)} 张（status={status or 'all'}）",
        ip=req.client_ip,
    )
    return Response.bytes(
        content,
        content_type="text/csv; charset=utf-8",
        filename=f"cards_{batch_no}.csv",
    )
