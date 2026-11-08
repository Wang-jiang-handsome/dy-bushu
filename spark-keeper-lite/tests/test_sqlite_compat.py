"""SQLite 老版本兼容回归测试（CentOS 7 真实事故）。

事故现场：CentOS 7 自带 SQLite **3.7.17**（2013 年），`main.py init` 在建库阶段
直接炸掉：

    [错误] OperationalError: near "WHERE": syntax error

原因有三个，全部需要锁死：

1. `CREATE UNIQUE INDEX ... WHERE ...` 是**部分索引**，要 SQLite **3.8.0+**；
2. 心跳用的 `INSERT ... ON CONFLICT ... DO UPDATE` 要 **3.24.0+**；
3. 迁移文件里只要残留一条新语法，整个建库事务就会回滚。

现在策略是：迁移只用「无条件可用」的 SQL；部分索引由
``db.ensure_daily_unique_index()`` 按版本决定是否补建（老版本跳过、只告警），
心跳改走 ``db.upsert()``（老版本降级为 UPDATE→INSERT）。
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from app import db


class MigrationSyntaxTests(unittest.TestCase):
    """迁移里不能再出现「老 SQLite 不认」的语法。"""

    def _statements(self):
        from pathlib import Path
        root = Path(db.__file__).resolve().parent.parent / "db" / "migrations"
        self.assertTrue(root.is_dir(), f"找不到迁移目录：{root}")
        for path in sorted(root.glob("*.sql")):
            text = path.read_text(encoding="utf-8")
            for stmt in db._split_statements(text):        # noqa: SLF001
                yield path.name, stmt

    def test_no_partial_indexes(self):
        offenders = []
        for name, stmt in self._statements():
            flat = re.sub(r"\s+", " ", stmt)
            if re.search(r"CREATE\s+(UNIQUE\s+)?INDEX", flat, re.I) and \
                    re.search(r"\bWHERE\b", flat, re.I):
                offenders.append(f"{name}: {flat[:80]}")
        self.assertEqual(offenders, [], f"迁移里仍有部分索引（需 SQLite 3.8+）：{offenders}")

    def test_no_upsert_in_migrations(self):
        offenders = [f"{n}: {s[:60]}" for n, s in self._statements()
                     if "ON CONFLICT" in s.upper()]
        self.assertEqual(offenders, [], f"迁移里不要用 UPSERT（需 3.24+）：{offenders}")

    def test_users_email_phone_indexes_still_unique(self):
        # 去掉 WHERE 后必须是**普通唯一索引**：SQLite 唯一索引本来就把多个 NULL 视为不同，
        # 所以语义与原来的部分索引一致。
        sql = "\n".join(s for _, s in self._statements())
        self.assertIn("ON users(email)", sql)
        self.assertIn("ON users(phone)", sql)
        self.assertIn("ux_users_email", sql)
        self.assertIn("ux_users_phone", sql)


class SqliteVersionTests(unittest.TestCase):
    def test_parse_versions(self):
        cases = {"3.7.17": (3, 7, 17), "3.24.0": (3, 24, 0), "3.53.4": (3, 53, 4),
                 "3.8": (3, 8, 0), "": (0, 0, 0)}
        for raw, expected in cases.items():
            with mock.patch.object(db.sqlite3, "sqlite_version", raw):
                self.assertEqual(db.sqlite_version(), expected, msg=raw)

    def test_thresholds(self):
        with mock.patch.object(db.sqlite3, "sqlite_version", "3.7.17"):
            self.assertFalse(db.sqlite_at_least(3, 8, 0))
            self.assertFalse(db.sqlite_at_least(3, 24, 0))
        with mock.patch.object(db.sqlite3, "sqlite_version", "3.24.0"):
            self.assertTrue(db.sqlite_at_least(3, 8, 0))
            self.assertTrue(db.sqlite_at_least(3, 24, 0))


class DailyUniqueIndexTests(unittest.TestCase):
    def test_skipped_on_old_sqlite(self):
        with mock.patch.object(db, "sqlite_at_least", return_value=False), \
                mock.patch.object(db, "get_conn") as get_conn:
            self.assertFalse(db.ensure_daily_unique_index())
        get_conn.assert_not_called()

    def test_created_on_modern_sqlite(self):
        conn = mock.Mock()
        with mock.patch.object(db, "sqlite_at_least", return_value=True), \
                mock.patch.object(db, "get_conn", return_value=conn):
            self.assertTrue(db.ensure_daily_unique_index())
        sql = conn.execute.call_args[0][0]
        self.assertIn("ux_runs_daily_success", sql)
        self.assertIn("WHERE trigger_type", sql)


class UpsertFallbackTests(unittest.TestCase):
    def test_modern_uses_on_conflict(self):
        conn = mock.Mock()
        with mock.patch.object(db, "sqlite_at_least", return_value=True), \
                mock.patch.object(db, "get_conn", return_value=conn):
            db.upsert("worker_heartbeats", {"worker_id": "w1", "beat_at": "t"},
                      conflict_columns=("worker_id",))
        sql = conn.execute.call_args[0][0]
        self.assertIn("ON CONFLICT(worker_id)", sql)

    def test_old_does_update_then_insert(self):
        with mock.patch.object(db, "sqlite_at_least", return_value=False), \
                mock.patch.object(db, "update", return_value=1) as update, \
                mock.patch.object(db, "insert") as insert:
            db.upsert("worker_heartbeats",
                      {"worker_id": "w1", "status": "running", "beat_at": "t"},
                      conflict_columns=("worker_id",))
        update.assert_called_once()
        insert.assert_not_called()

    def test_old_inserts_when_missing(self):
        with mock.patch.object(db, "sqlite_at_least", return_value=False), \
                mock.patch.object(db, "update", return_value=0), \
                mock.patch.object(db, "insert") as insert:
            db.upsert("worker_heartbeats",
                      {"worker_id": "w1", "status": "running", "beat_at": "t"},
                      conflict_columns=("worker_id",))
        insert.assert_called_once()


class InitOnOldSqliteTests(unittest.TestCase):
    """最能说明问题的一条：把版本伪装成 3.7.17，完整建库必须成功。"""

    def test_migrate_succeeds_when_sqlite_is_old(self):
        import tempfile
        from pathlib import Path

        from app import bootstrap
        from app.config import load_settings, set_settings

        tmp = Path(tempfile.mkdtemp(prefix="spark-old-sqlite-"))
        cfg = load_settings(None)
        cfg.data_dir = tmp
        cfg.db_path = tmp / "spark.db"
        previous = None
        try:
            from app.config import get_settings
            previous = get_settings()
        except Exception:                              # noqa: BLE001
            previous = None
        try:
            set_settings(cfg)
            with mock.patch.object(db.sqlite3, "sqlite_version", "3.7.17"):
                report = bootstrap.init_app(cfg, quiet=True)
            self.assertTrue(report, "init_app 应该返回初始化报告")

            with mock.patch.object(db.sqlite3, "sqlite_version", "3.7.17"):
                applied = db.migrate(verbose=False)
            self.assertEqual(applied, [], "重复 migrate 应该是幂等的")

            tables = {row["name"] for row in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("users", tables)
            self.assertIn("task_runs", tables)
            indexes = {row["name"] for row in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertNotIn("ux_runs_daily_success", indexes,
                             "老 SQLite 上不该建部分索引（会语法错误）")
            self.assertIn("ux_users_email", indexes)
        finally:
            db.close_conn()
            if previous is not None:
                set_settings(previous)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main(verbosity=2)
