"""调度数学与幂等约束测试。

覆盖两件事：
1. ``app/common/timeutil.next_run_time`` 的下一次执行时间计算（跨天 / 星期筛选 /
   interval 模式 / 「永不返回过去时间」这条防死循环不变量）。
2. 数据库层的「每天只跑一次」与「发送台账幂等」唯一约束——这是自动续火花
   不能重复发消息的**最后一道防线**，比调度器代码更可靠，必须有测试钉住。

运行：
    cd spark-shared
    python -m unittest tests.test_scheduler_math -v
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_harness import AppHarness  # noqa: E402

UTC = timezone.utc


class TestNextRunMath(unittest.TestCase):
    """纯时间计算，不碰数据库。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = AppHarness("schedulemath").start()
        from app.common import timeutil as tu          # noqa: PLC0415

        cls.tu = tu
        cls.tz = tu.get_tz("Asia/Shanghai")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def local(self, moment: datetime) -> str:
        return moment.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    # ---- daily ----
    def test_daily_same_local_day_when_time_not_passed(self) -> None:
        after = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)        # = 08:00 上海
        nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", "daily", after=after)
        self.assertEqual(self.local(nxt), "2026-08-16 08:30")
        self.assertEqual(nxt.astimezone(UTC).strftime("%Y-%m-%d %H:%M"), "2026-08-16 00:30")

    def test_daily_rolls_to_tomorrow_when_time_passed(self) -> None:
        after = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)        # = 10:00 上海，已过 08:30
        nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", "daily", after=after)
        self.assertEqual(self.local(nxt), "2026-08-17 08:30")

    def test_daily_crosses_utc_date_boundary(self) -> None:
        """「跨天」的正确性：本地 00:10 属于 UTC 的前一天，run_date 要按本地算。"""
        after = datetime(2026, 8, 16, 16, 30, tzinfo=UTC)      # = 08-17 00:30 上海
        nxt = self.tu.next_run_time("00:10", "Asia/Shanghai", "daily", after=after)
        self.assertEqual(self.local(nxt), "2026-08-18 00:10")
        self.assertEqual(nxt.astimezone(UTC).strftime("%Y-%m-%d %H:%M"), "2026-08-17 16:10",
                         msg="本地 08-18 00:10 应该是 UTC 08-17 16:10")
        self.assertEqual(self.tu.local_date_str("Asia/Shanghai", at=nxt), "2026-08-18")
        self.assertEqual(self.tu.local_date_str("Asia/Shanghai", at=after), "2026-08-17")

    def test_result_is_always_in_the_future(self) -> None:
        """调度器靠这条不变量避免「立刻又到期」的死循环。"""
        after = datetime(2026, 8, 16, 3, 17, 42, tzinfo=UTC)
        for mode, weekdays in (("daily", []), ("weekdays", []), ("weekends", []),
                              ("custom", [1, 3, 5]), ("custom", []), ("interval", [])):
            for time_of_day in ("00:00", "08:30", "23:59"):
                with self.subTest(mode=mode, time_of_day=time_of_day):
                    nxt = self.tu.next_run_time(time_of_day, "Asia/Shanghai", mode,
                                                weekdays=weekdays, interval_minutes=60,
                                                after=after, last_run_at="2026-08-16 03:00:00")
                    self.assertGreater(nxt, after,
                                       msg=f"{mode}/{time_of_day} 返回了不晚于现在的时间：{nxt}")

    def test_weekday_modes(self) -> None:
        after = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)        # = 12:00 上海
        for mode, allowed in (("weekdays", {1, 2, 3, 4, 5}), ("weekends", {6, 7})):
            with self.subTest(mode=mode):
                nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", mode, after=after)
                self.assertIn(nxt.astimezone(self.tz).isoweekday(), allowed)
                gap = (nxt.astimezone(self.tz).date() - after.astimezone(self.tz).date()).days
                self.assertGreaterEqual(gap, 1)
                self.assertLessEqual(gap, 7, msg="星期筛选最多等 7 天")
                self.assertEqual(self.local(nxt).endswith("08:30"), True)

    def test_custom_weekday_filter(self) -> None:
        after = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
        nxt = self.tu.next_run_time("09:15", "Asia/Shanghai", "custom", weekdays=["mon", "fri"],
                                    after=after)
        self.assertIn(nxt.astimezone(self.tz).isoweekday(), (1, 5))
        self.assertTrue(self.local(nxt).endswith("09:15"))

    def test_interval_anchors_on_last_run(self) -> None:
        after = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
        nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", "interval", interval_minutes=90,
                                    after=after, last_run_at="2026-08-16 00:00:00")
        self.assertEqual(nxt.astimezone(UTC).strftime("%Y-%m-%d %H:%M"), "2026-08-16 01:30")

    def test_interval_recovers_from_long_downtime(self) -> None:
        """服务停机 6 天后，下一次执行必须落在未来，而不是补跑 144 次。"""
        after = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
        nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", "interval", interval_minutes=60,
                                    after=after, last_run_at="2026-08-10 00:00:00")
        self.assertGreater(nxt, after)
        self.assertLessEqual((nxt - after).total_seconds(), 3600,
                             msg="补齐到未来后的落点不该超过一个间隔")

    def test_interval_without_last_run(self) -> None:
        after = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)        # 08:00 上海
        nxt = self.tu.next_run_time("08:30", "Asia/Shanghai", "interval", interval_minutes=120,
                                    after=after, last_run_at=None)
        self.assertEqual(self.local(nxt), "2026-08-16 08:30")

    def test_parse_hhmm_is_forgiving(self) -> None:
        """调度器不能因为一条脏数据停摆：非法时间回落到 08:30。"""
        self.assertEqual(self.tu.parse_hhmm("07:05"), (7, 5))
        self.assertEqual(self.tu.parse_hhmm("7:5"), (7, 5))
        self.assertEqual(self.tu.parse_hhmm("25:99"), (8, 30))
        self.assertEqual(self.tu.parse_hhmm(""), (8, 30))
        self.assertEqual(self.tu.parse_hhmm(None), (8, 30))

    def test_parse_weekdays_aliases(self) -> None:
        self.assertEqual(self.tu.parse_weekdays(["1", "5"]), [1, 5])
        self.assertEqual(self.tu.parse_weekdays("mon,fri"), [1, 5])
        self.assertEqual(self.tu.parse_weekdays("周一"), [])      # 中文不认，但不抛异常
        self.assertEqual(self.tu.parse_weekdays("1,1,2"), [1, 2])
        self.assertEqual(self.tu.parse_weekdays("sun"), [7])
        self.assertEqual(self.tu.parse_weekdays("0"), [7])

    def test_expiry_and_humanize(self) -> None:
        self.assertTrue(self.tu.is_expired("2020-01-01 00:00:00"))
        self.assertFalse(self.tu.is_expired(self.tu.add_days_str(3)))
        self.assertIn("还剩", self.tu.humanize_delta(self.tu.add_days_str(10)))
        self.assertEqual(self.tu.humanize_delta(None), "永久")


class TestIdempotencyConstraints(unittest.TestCase):
    """数据库唯一约束：每天只跑一次 + 发送台账幂等。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = AppHarness("idempotency").start()
        cls.user = cls.h.create_user("scheduler")
        cls.account_id = cls._make_account(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.stop()

    def _make_account(self) -> int:
        from app import db                              # noqa: PLC0415
        from app.common import timeutil                 # noqa: PLC0415

        now = timeutil.utc_now_str()
        return db.insert("douyin_accounts", {
            "user_id": self.user["id"], "nickname": "调度测试号", "status": "online",
            "created_at": now, "updated_at": now,
        })

    def _insert_run(self, run_date: str, trigger_type: str, status: str = "success") -> int:
        from app import db                              # noqa: PLC0415
        from app.common import timeutil                 # noqa: PLC0415

        now = timeutil.utc_now_str()
        return db.insert("task_runs", {
            "task_id": None, "user_id": self.user["id"], "account_id": self.account_id,
            "run_date": run_date, "trigger_type": trigger_type, "status": status,
            "started_at": now, "finished_at": now, "created_at": now,
        })

    def test_pragma_settings(self) -> None:
        from app import db                              # noqa: PLC0415

        self.assertEqual(str(db.query_scalar("PRAGMA journal_mode", (), "")).lower(), "wal",
                         msg="必须开 WAL：Web 多线程 + 调度器并发写")
        self.assertEqual(int(db.query_scalar("PRAGMA foreign_keys", (), 0)), 1,
                         msg="必须开外键：删用户要连带清理数据")

    def test_scheduled_run_once_per_account_per_day(self) -> None:
        """同一账号 + 同一天 + schedule 触发，只允许一条（防重复发消息）。"""
        self._insert_run("2026-08-16", "schedule")
        with self.assertRaises(sqlite3.IntegrityError,
                               msg="重复的每日定时执行必须被唯一索引拦住"):
            self._insert_run("2026-08-16", "schedule")

    def test_manual_run_is_not_blocked_by_daily_index(self) -> None:
        """手动「立即执行」可以有多次，不受每日唯一索引限制。"""
        self._insert_run("2026-08-17", "manual")
        self._insert_run("2026-08-17", "manual")
        from app import db                              # noqa: PLC0415

        count = int(db.query_scalar(
            "SELECT COUNT(*) FROM task_runs WHERE account_id=? AND run_date=? AND trigger_type='manual'",
            (self.account_id, "2026-08-17"), 0))
        self.assertEqual(count, 2)

    def test_different_days_are_independent(self) -> None:
        self._insert_run("2026-08-18", "schedule")
        self._insert_run("2026-08-19", "schedule")
        from app import db                              # noqa: PLC0415

        count = int(db.query_scalar(
            "SELECT COUNT(*) FROM task_runs WHERE account_id=? AND trigger_type='schedule'"
            " AND run_date IN ('2026-08-18','2026-08-19')", (self.account_id,), 0))
        self.assertEqual(count, 2)

    def test_send_history_is_idempotent(self) -> None:
        """同一账号 + 同一好友 + 同一天只能成功发一次（台账是幂等依据）。"""
        from app import db                              # noqa: PLC0415
        from app.common import timeutil                 # noqa: PLC0415

        now = timeutil.utc_now_str()

        def add(friend_key: str, send_date: str) -> int:
            return db.insert("send_history", {
                "account_id": self.account_id, "friend_id": None, "friend_key": friend_key,
                "send_date": send_date, "message": "火花不能断 🔥", "run_id": None,
                "created_at": now,
            })

        add("小明", "2026-08-16")
        with self.assertRaises(sqlite3.IntegrityError, msg="同一天给同一好友发第二次必须被拦住"):
            add("小明", "2026-08-16")
        add("小明", "2026-08-17")        # 换一天可以
        add("小红", "2026-08-16")        # 换好友可以

    def test_friend_uniqueness_per_account(self) -> None:
        """同一账号下好友昵称唯一，避免导入重复。"""
        from app import db                              # noqa: PLC0415
        from app.common import timeutil                 # noqa: PLC0415

        now = timeutil.utc_now_str()

        def add(nickname: str) -> int:
            return db.insert("friends", {
                "user_id": self.user["id"], "account_id": self.account_id,
                "nickname": nickname, "created_at": now, "updated_at": now,
            })

        add("好友A")
        with self.assertRaises(sqlite3.IntegrityError):
            add("好友A")

    def test_deleting_user_cascades(self) -> None:
        """删用户必须连带删账号与任务记录（外键级联），不留孤儿数据。"""
        from app import db                              # noqa: PLC0415
        from app.common import timeutil                 # noqa: PLC0415

        victim = self.h.create_user("victim")
        now = timeutil.utc_now_str()
        account_id = db.insert("douyin_accounts", {
            "user_id": victim["id"], "nickname": "victim 的号", "status": "online",
            "created_at": now, "updated_at": now,
        })
        db.insert("scheduled_tasks", {
            "user_id": victim["id"], "account_id": account_id, "name": "victim 任务",
            "time_of_day": "08:30", "created_at": now, "updated_at": now,
        })
        db.delete("users", "id=?", (victim["id"],))
        self.assertEqual(int(db.query_scalar("SELECT COUNT(*) FROM douyin_accounts WHERE user_id=?",
                                            (victim["id"],), 0)), 0)
        self.assertEqual(int(db.query_scalar("SELECT COUNT(*) FROM scheduled_tasks WHERE user_id=?",
                                            (victim["id"],), 0)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
