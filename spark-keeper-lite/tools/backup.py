#!/usr/bin/env python
"""SQLite 热备份：``VACUUM INTO`` 出一份一致性快照，并只保留最近 N 份。

为什么不用「拷文件」
--------------------
库开着 WAL，直接 ``copy spark.db`` 可能拿到「主库有、WAL 里没有」的半截数据。
``VACUUM INTO`` 在**读事务**里把整库导成一个新文件，天然一致，且不阻塞写入
（WAL 模式下读写不互斥），所以服务可以不停机备份。

用法
----
    python tools/backup.py                       # 备份 backend/data/spark.db → backend/data/backups/
    python tools/backup.py --keep 7
    python tools/backup.py --db backend/data/spark.db --out /backup --json
    python tools/backup.py --list

退出码：0 成功 · 1 备份失败 · 2 找不到数据库
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent


def _detect_layout(start: Path) -> tuple[Path, Path]:
    """返回 ``(项目根, 后端目录)``，同时兼容两种布局。

    * 开发树：``spark-shared/{backend/app, web, pro, tests, tools}``
    * 交付树：``spark-keeper-pro/{app, web, tests, tools}``（backend 被拍平到根）

    交付出去的工具必须能自己找到路径，否则运维在部署机上没法用。
    """
    for root in (start, start.parent, start.parent.parent):
        if (root / "app" / "config.py").is_file():
            return root, root
        if (root / "backend" / "app" / "config.py").is_file():
            return root, root / "backend"
    return start.parent, start.parent / "backend"


PROJECT_ROOT, BACKEND_DIR = _detect_layout(TOOLS_DIR)
DEFAULT_DB = BACKEND_DIR / "data" / "spark.db"
DEFAULT_OUT = BACKEND_DIR / "data" / "backups"
# 带微秒：同一秒内备份多次也能按文件名正确排序（只到秒时 "-1" 会排在 ".db" 前面，
# 一旦按名字清理就会把最新的一份删掉——这个坑踩过一次，别改回去）。
STAMP_FMT = "%Y%m%d-%H%M%S-%f"


def _connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _unique_target(out_dir: Path, prefix: str, moment: datetime | None = None) -> Path:
    """生成不冲突的文件名（同一秒备份两次也不会覆盖）。"""
    stamp = (moment or datetime.now()).strftime(STAMP_FMT)
    target = out_dir / f"{prefix}-{stamp}.db"
    seq = 1
    while target.exists():
        target = out_dir / f"{prefix}-{stamp}-{seq}.db"
        seq += 1
    return target


def _snapshot(db_path: Path, target: Path) -> str:
    """导出快照。返回使用的机制名（VACUUM INTO / Connection.backup）。"""
    conn = _connect(db_path)
    try:
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
            return "VACUUM INTO"
        except sqlite3.OperationalError as exc:
            # 老版本 SQLite（< 3.27）没有 VACUUM INTO，退回官方在线备份 API
            if "VACUUM INTO" not in str(exc).upper() and "syntax" not in str(exc).lower():
                raise
            dest = sqlite3.connect(str(target))
            try:
                conn.backup(dest)
            finally:
                dest.close()
            return "Connection.backup"
    finally:
        conn.close()


def verify(target: Path) -> dict:
    """验证快照可用：integrity_check + 表数量 + 关键表存在性。"""
    conn = _connect(target, readonly=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        tables = int(conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0])
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            "integrity": integrity,
            "tables": tables,
            "has_users": "users" in names,
            "has_schema_migrations": "schema_migrations" in names,
            "schema_version": str(conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            if "schema_migrations" in names else "",
        }
    finally:
        conn.close()


def prune(out_dir: Path, prefix: str, keep: int, protect: Path | None = None) -> list[str]:
    """只保留最近 keep 份，返回被删掉的文件名。

    排序用 ``(mtime_ns, name)``：文件名的字典序在加了 ``-N`` 后缀后不可靠，
    而 mtime 精确到纳秒。``protect`` 是本次刚生成的那份，永远不删。
    """
    if keep <= 0:
        return []
    def sort_key(path: Path) -> tuple[int, str]:
        try:
            return (path.stat().st_mtime_ns, path.name)
        except OSError:
            return (0, path.name)

    candidates = sorted((p for p in out_dir.glob(f"{prefix}-*.db") if p.is_file()), key=sort_key)
    removed: list[str] = []
    stale_list = candidates[:-keep] if len(candidates) > keep else []
    for stale in stale_list:
        if protect is not None and stale.name == protect.name:
            continue           # 绝不删掉本次刚生成的那份
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError:
            pass          # 删不掉就算了，备份本身已经成功，不要因此报失败
    return removed


def list_backups(out_dir: Path, prefix: str = "*") -> list[dict]:
    if not out_dir.exists():
        return []
    items = []
    for path in sorted(out_dir.glob(f"{prefix}-*.db")):
        stat = path.stat()
        items.append({
            "name": path.name,
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return items


def hot_backup(db_path: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT, keep: int = 14,
               prefix: str = "spark") -> dict:
    """执行一次热备份，返回结果字典（供 CLI 与 selftest 复用）。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(out_dir, prefix)
    mechanism = _snapshot(db_path, target)
    try:
        info = verify(target)
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"备份文件校验失败，已删除：{exc}") from exc
    ok = info["integrity"] == "ok" and info["tables"] > 0 and info["has_users"]
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"备份内容不完整，已删除：{info}")
    size_bytes = target.stat().st_size        # 先量体积，再清理旧备份（顺序反了会 stat 到已删文件）
    removed = prune(out_dir, prefix, keep, protect=target)
    return {
        "ok": True,
        "source": str(db_path),
        "target": str(target),
        "size_bytes": size_bytes,
        "mechanism": mechanism,
        "keep": keep,
        "pruned": removed,
        **info,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite 热备份（VACUUM INTO）+ 保留最近 N 份")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"数据库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"备份目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--keep", type=int, default=14, help="保留最近几份（默认 14，0 = 不清理）")
    parser.add_argument("--prefix", default="spark", help="备份文件名前缀（默认 spark）")
    parser.add_argument("--list", action="store_true", help="只列出已有备份")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    db_path, out_dir = Path(args.db), Path(args.out)

    if args.list:
        items = list_backups(out_dir, args.prefix)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(f"备份目录：{out_dir}（{len(items)} 份）")
            for item in items:
                print(f"  {item['name']:<28} {item['size_bytes'] / 1024:>9.1f} KB  {item['mtime']}")
        return 0

    try:
        result = hot_backup(db_path, out_dir, args.keep, args.prefix)
    except FileNotFoundError as exc:
        print(f"[备份失败] {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        print(f"[备份失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"  备份完成（{result['mechanism']}）")
        print(f"  源库：  {result['source']}")
        print(f"  快照：  {result['target']}  ({result['size_bytes'] / 1024:.1f} KB)")
        print(f"  校验：  integrity={result['integrity']} · 表 {result['tables']} 张 · "
              f"schema {result['schema_version']}")
        if result["pruned"]:
            print(f"  已清理旧备份：{', '.join(result['pruned'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
