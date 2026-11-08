"""SQLite 数据访问层（标准库 sqlite3，零依赖）。

为什么不用 ORM
--------------
自托管场景要求「一个文件跑起来」，SQLite + 手写 SQL 最透明也最好排障。
这里只提供四件事：连接管理、行/字典转换、事务上下文、迁移执行。

并发说明
--------
SQLite 的 WAL 模式允许「多读一写」。Web 端是多线程的，写操作通过
``busy_timeout`` + 短事务串行化；任务执行由 scheduler 串行调度，
不依赖数据库锁做业务互斥（互斥靠 ``send_history`` 唯一索引）。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import get_settings

_MIGRATION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_\-]+)\.sql$")

_local = threading.local()
_write_lock = threading.RLock()


def _connect(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,          # 自己管事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn


def get_conn() -> sqlite3.Connection:
    """取当前线程的连接（每线程一个，避免 sqlite3 的线程限制）。"""
    cfg = get_settings()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(cfg.db_path):
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(cfg.db_path, cfg.db_busy_timeout_ms)
        _local.conn = conn
        _local.path = str(cfg.db_path)
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None
            _local.path = None


# --------------------------------------------------------------------------
# 查询辅助
# --------------------------------------------------------------------------
def query_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    cur = get_conn().execute(sql, params)
    try:
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()


def query_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    cur = get_conn().execute(sql, params)
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def query_scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if not row:
        return default
    value = next(iter(row.values()))
    return default if value is None else value


def execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    with _write_lock:
        return get_conn().execute(sql, params)


def execute_rowcount(sql: str, params: Sequence[Any] = ()) -> int:
    cur = execute(sql, params)
    try:
        return cur.rowcount
    finally:
        cur.close()


def insert(table: str, data: dict) -> int:
    """插入并返回主键 id。"""
    cols = list(data.keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    with _write_lock:
        cur = get_conn().execute(sql, [data[c] for c in cols])
        try:
            return int(cur.lastrowid or 0)
        finally:
            cur.close()


def update(table: str, data: dict, where: str, params: Sequence[Any] = ()) -> int:
    if not data:
        return 0
    sets = ", ".join(f"{k}=?" for k in data)
    sql = f"UPDATE {table} SET {sets} WHERE {where}"
    with _write_lock:
        cur = get_conn().execute(sql, [*data.values(), *params])
        try:
            return cur.rowcount
        finally:
            cur.close()


def delete(table: str, where: str, params: Sequence[Any] = ()) -> int:
    with _write_lock:
        cur = get_conn().execute(f"DELETE FROM {table} WHERE {where}", params)
        try:
            return cur.rowcount
        finally:
            cur.close()


def upsert(table: str, data: dict, conflict_columns: Sequence[str],
           update_columns: Sequence[str] | None = None) -> None:
    """INSERT ... ON CONFLICT DO UPDATE，老 SQLite 自动降级为 UPDATE→INSERT。

    UPSERT 语法需要 SQLite **3.24.0+**（2018）；CentOS 7 的 3.7.17 上会报
    ``near "ON": syntax error``。降级路径在单实例、单写的场景下与 upsert 等价
    （调度器心跳就是这种用法）。
    """
    if not data:
        return
    cols = list(data)
    if sqlite_at_least(3, 24, 0):
        updates = list(update_columns or [c for c in cols if c not in conflict_columns])
        sets = ", ".join(f"{c}=excluded.{c}" for c in updates)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)})"
            f" VALUES ({', '.join('?' for _ in cols)})"
            f" ON CONFLICT({', '.join(conflict_columns)}) DO UPDATE SET {sets}"
        )
        with _write_lock:
            get_conn().execute(sql, [data[c] for c in cols])
        return

    where = " AND ".join(f"{c}=?" for c in conflict_columns)
    params = [data.get(c) for c in conflict_columns]
    updates = [c for c in (update_columns or cols) if c not in conflict_columns]
    if updates and update(  # type: ignore[func-returns-value]
            table, {c: data[c] for c in updates if c in data}, where, params):
        return
    insert(table, data)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写事务。嵌套调用复用外层事务，避免 SQLite 不支持嵌套 BEGIN。"""
    conn = get_conn()
    with _write_lock:
        if getattr(_local, "in_tx", False):
            yield conn
            return
        _local.in_tx = True
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            _local.in_tx = False


def paginate(
    base_sql: str,
    params: Sequence[Any],
    page: int = 1,
    page_size: int = 20,
    max_page_size: int = 200,
) -> dict:
    """包装分页查询，返回 {items,total,page,page_size,pages}。"""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), max_page_size))
    total = int(query_scalar(f"SELECT COUNT(*) FROM ({base_sql}) t", params, 0) or 0)
    rows = query_all(f"{base_sql} LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size])
    pages = (total + page_size - 1) // page_size if page_size else 0
    return {"items": rows, "total": total, "page": page, "page_size": page_size, "pages": pages}


def rows_to_dict(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 迁移
# --------------------------------------------------------------------------
def migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "migrations"


def _applied_versions() -> set[str]:
    get_conn().execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    TEXT PRIMARY KEY,
               name       TEXT NOT NULL DEFAULT '',
               applied_at TEXT NOT NULL,
               checksum   TEXT NOT NULL DEFAULT ''
           )"""
    )
    return {row["version"] for row in query_all("SELECT version FROM schema_migrations")}


def discover_migrations() -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    folder = migrations_dir()
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            continue
        out.append((match.group(1), path.stem, path))
    return out


def _split_statements(sql: str) -> list[str]:
    """按分号切分 SQL，跳过字符串/标识符里的分号与注释。

    迁移文件由我们自己编写，不追求完整 SQL 解析，只需正确处理
    ``'...'`` ``"..."`` ``--`` 注释和 ``BEGIN ... END`` 触发器（本项目未使用）。
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:       # 转义的两个引号
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def sqlite_version() -> tuple[int, int, int]:
    """当前 SQLite 版本，例如 ``(3, 7, 17)``。

    为什么要它：CentOS 7 自带 SQLite 3.7.17（2013 年），而本项目用到的
    **部分索引**（3.8.0+）和 **UPSERT**（3.24.0+）在老版本上是语法错误。
    实测事故：`CREATE UNIQUE INDEX ... WHERE ...` 在 3.7.17 上直接
    ``near "WHERE": syntax error``，导致 ``main.py init`` 建库失败。
    """
    raw = str(getattr(sqlite3, "sqlite_version", "") or "0")
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def sqlite_at_least(major: int, minor: int = 0, patch: int = 0) -> bool:
    return sqlite_version() >= (major, minor, patch)


#: 「同一账号同一天同一计划任务最多一次成功」的唯一约束（部分索引，需 SQLite 3.8+）。
#: 老版本 SQLite 跳过建索引，业务上的当日幂等仍由调度器 success_today() 保证。
DAILY_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_daily_success"
    " ON task_runs(account_id, run_date, trigger_type)"
    " WHERE trigger_type = 'schedule' AND status IN ('success', 'partial')"
)


def ensure_daily_unique_index(verbose: bool = False) -> bool:
    """SQLite >= 3.8 时补建每日唯一索引；老版本跳过并记录原因。"""
    if not sqlite_at_least(3, 8, 0):
        _log_sqlite_degradation(
            f"SQLite {'.'.join(map(str, sqlite_version()))} 不支持部分索引："
            "跳过 ux_runs_daily_success（当日幂等由调度器判断保证）")
        return False
    with _write_lock:
        get_conn().execute(DAILY_UNIQUE_INDEX)
    if verbose:
        print("[migrate] 已建索引 ux_runs_daily_success（部分唯一索引）")
    return True


def _log_sqlite_degradation(message: str) -> None:
    try:
        from .common import sklog

        sklog.warning(message, scope="system", source="db.sqlite")
    except Exception:                     # noqa: BLE001 - 日志失败不能影响建库
        pass


def migrate(verbose: bool = True) -> list[str]:
    """执行未应用的迁移，返回本次应用的版本号列表。幂等。"""
    import hashlib
    from datetime import datetime, timezone

    applied = _applied_versions()
    done: list[str] = []
    for version, name, path in discover_migrations():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
        statements = _split_statements(sql)
        conn = get_conn()
        with _write_lock:
            # 迁移期间必须关掉外键：迁移里有「建新表 → 拷数据 → 换名」的重建流程，
            # 直接 DROP 会被子表的外键拦住。注意 SQLite 的 PRAGMA foreign_keys
            # 在事务内是**静默无效**的，所以必须在 BEGIN 之前设置。
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in statements:
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at, checksum)"
                        " VALUES (?,?,?,?)",
                        (
                            version,
                            name,
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            checksum,
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
        done.append(version)
        if verbose:
            print(f"[migrate] applied {version}_{name} ({len(statements)} statements)")

    # 迁移之后做一次「列补齐」：老库已经建过表，CREATE TABLE IF NOT EXISTS 不会
    # 给已有表加列。新增的列在这里自动补上，避免升级部署时手工 ALTER。
    added = ensure_columns(verbose=verbose)
    if added and verbose:
        print(f"[migrate] 补齐列：{', '.join(added)}")
    # 部分索引（3.8+）单独按版本补建，老 SQLite 跳过而不是让建库失败
    ensure_daily_unique_index(verbose=verbose)
    return done


# 期望存在但可能是后加的列：{表名: {列名: 列定义}}
EXPECTED_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "douyin_accounts": {"enabled": "INTEGER NOT NULL DEFAULT 1"},
}


def ensure_columns(verbose: bool = False) -> list[str]:
    """为已有表补上缺失的列（幂等）。返回 "表.列" 列表。"""
    added: list[str] = []
    conn = get_conn()
    for table, columns in EXPECTED_EXTRA_COLUMNS.items():
        exists = query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if not exists:
            continue
        current = {row["name"] for row in query_all(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column in current:
                continue
            with _write_lock:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(f"{table}.{column}")
            if verbose:
                print(f"[migrate] ALTER TABLE {table} ADD COLUMN {column}")
    return added


def health() -> dict:
    """数据库健康检查（安装向导与 /api/v1/install/* 使用）。"""
    cfg = get_settings()
    try:
        conn = get_conn()
        conn.execute("SELECT 1")
        tables = query_scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            (),
            0,
        )
        size = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
        version = query_scalar("SELECT MAX(version) FROM schema_migrations", (), "") or ""
        journal = query_scalar("PRAGMA journal_mode", (), "unknown")
        return {
            "ok": True,
            "path": str(cfg.db_path),
            "tables": int(tables),
            "size_bytes": size,
            "schema_version": str(version),
            "journal_mode": str(journal),
        }
    except Exception as exc:  # pragma: no cover - 只在磁盘损坏时触发
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
