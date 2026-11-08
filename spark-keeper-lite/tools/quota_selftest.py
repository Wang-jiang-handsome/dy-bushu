"""配额与次数卡加成的快速自检（Lead 维护）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _detect_backend(root: Path) -> Path:
    """开发树后端在 ``root/backend``；交付树后端就在 ``root``。"""
    for candidate in (root / "backend", root):
        if (candidate / "main.py").is_file() and (candidate / "app").is_dir():
            return candidate
    return root / "backend"


BACKEND = _detect_backend(ROOT)
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("SK_DATA_DIR", str(BACKEND / "data"))

from app import bootstrap as boot              # noqa: E402
from app import db                              # noqa: E402
from app.common import quota as q               # noqa: E402
from app.common import timeutil                 # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    boot.init_app(quiet=True)
    db.migrate(verbose=False)

    # 本自检验证的是**套餐计次数学**（套餐 + 次数卡加成），所以先关掉「不限次」，
    # 否则 daily_task_limit 一律为 0，这些断言全部失去意义。
    from app.config import get_settings
    settings = get_settings()
    settings.unlimited_task_runs = False

    name = "__quota_selftest__"
    db.delete("users", "username=?", (name,))
    now = timeutil.utc_now_str()
    user_id = db.insert(
        "users",
        {
            "username": name,
            "password_hash": "x",
            "role": "user",
            "status": "active",
            "quota_json": "{}",
            "created_at": now,
            "updated_at": now,
        },
    )
    package = db.query_one("SELECT * FROM packages WHERE code='monthly'")

    # 1) 无套餐、无加成 → 未激活
    quo = q.Quota.refresh(user_id)
    check("无套餐时 active=False", quo.active is False)
    check("无套餐时 daily_limit=0", quo.daily_task_limit == 0)
    try:
        quo.ensure()
        check("无套餐时 ensure() 抛 402", False)
    except Exception as exc:      # noqa: BLE001
        check("无套餐时 ensure() 抛 402", getattr(exc, "status", 0) == 402, str(exc))

    # 2) 授权套餐 → active
    db.update(
        "users",
        {"package_id": package["id"], "package_expire_at": timeutil.add_days_str(30)},
        "id=?",
        (user_id,),
    )
    quo = q.Quota.refresh(user_id)
    check("有套餐时 active=True", quo.active is True)
    check("有套餐时 daily_limit=套餐值", quo.daily_task_limit == package["daily_task_limit"],
          f"{quo.daily_task_limit} vs {package['daily_task_limit']}")

    # 3) 次数卡加成：+5 次/天，且 refresh 后不被抹掉
    with db.transaction():
        q.grant_bonus_tasks(user_id, 5, days=30)
    quo = q.Quota.refresh(user_id)
    expect = package["daily_task_limit"] + 5
    check("加成后 daily_limit = 套餐 + 5", quo.daily_task_limit == expect,
          f"{quo.daily_task_limit} vs {expect}")
    check("加成计数可读", quo.bonus_task_count == 5, str(quo.bonus_task_count))
    quo2 = q.Quota.refresh(user_id)   # 再刷一次，确认不会丢
    check("多次 refresh 不丢加成", quo2.daily_task_limit == expect, str(quo2.daily_task_limit))

    # 4) 叠加两次
    with db.transaction():
        q.grant_bonus_tasks(user_id, 3, days=30)
    quo = q.Quota.refresh(user_id)
    check("两次兑换叠加为 8", quo.bonus_task_count == 8, str(quo.bonus_task_count))

    # 5) 加成过期 → 自动作废
    cached = json.loads(db.query_one("SELECT quota_json FROM users WHERE id=?", (user_id,))["quota_json"])
    cached["bonus_expire_at"] = timeutil.to_db(timeutil.add_days(-1))
    db.update("users", {"quota_json": json.dumps(cached)}, "id=?", (user_id,))
    quo = q.Quota.refresh(user_id)
    check("加成过期后归零", quo.bonus_task_count == 0 and quo.daily_task_limit == package["daily_task_limit"],
          f"bonus={quo.bonus_task_count} limit={quo.daily_task_limit}")

    # 6) 套餐过期但次数卡有效 → 仍可用（active=True 靠加成）
    #    注意：必须从数据库重新读，不能复用上面的 cached —— refresh() 已经改过它了。
    fresh = json.loads(db.query_one("SELECT quota_json FROM users WHERE id=?", (user_id,))["quota_json"])
    fresh["bonus_task_count"] = 2
    db.update(
        "users",
        {"quota_json": json.dumps(fresh), "package_expire_at": timeutil.add_days_str(-1)},
        "id=?",
        (user_id,),
    )
    quo = q.Quota.refresh(user_id)
    check("套餐过期 + 有效次数卡 → active，额度 = 套餐 + 2",
          quo.active is True and quo.daily_task_limit == package["daily_task_limit"] + 2,
          f"active={quo.active} limit={quo.daily_task_limit}")

    # 7) 完全没有套餐，只靠次数卡 → 仍可用，额度就是加成数
    db.update(
        "users",
        {"package_id": None, "package_expire_at": None},
        "id=?",
        (user_id,),
    )
    quo = q.Quota.refresh(user_id)
    check("无套餐 + 次数卡 → active 且额度 = 2",
          quo.active is True and quo.daily_task_limit == 2,
          f"active={quo.active} limit={quo.daily_task_limit}")
    check("account_limit 仍为 0（次数卡不送账号位）", quo.account_limit == 0, str(quo.account_limit))

    # 8) 管理员不受限
    admin = db.query_one("SELECT * FROM users WHERE role='admin' LIMIT 1")
    if admin:
        qa_ = q.Quota.for_user(admin["id"])
        check("管理员 daily_limit=999", qa_.daily_task_limit == 999, str(qa_.daily_task_limit))

    # 9) 不限次模式（自托管默认）：次数不拦，界面显示 ∞
    try:
        settings.unlimited_task_runs = True
        quo = q.Quota.refresh(user_id)
        check("不限次模式：daily_task_unlimited=True", quo.daily_task_unlimited is True)
        check("不限次模式：daily_task_limit=0（前端据此显示 ∞）",
              quo.daily_task_limit == 0, str(quo.daily_task_limit))
        raised = ""
        try:
            quo.ensure_can_run_today()
        except Exception as exc:      # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        check("不限次模式：ensure_can_run_today() 不拦截", raised == "", raised)
    finally:
        settings.unlimited_task_runs = False

    db.delete("users", "id=?", (user_id,))
    print(f"\n  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
