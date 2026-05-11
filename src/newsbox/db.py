"""SQLite 连接、迁移、查询封装。

迁移策略：
- ``apply_migrations`` 扫描 migrations 目录下所有 ``*.sql`` 文件，按文件名升序应用。
- 已应用过的迁移记录在 ``schema_migrations`` 表（``filename`` 主键 + ``applied_at`` 时间戳）。
- 迁移文件按 ``;`` 切分，所有语句都执行；不再有"可选段"概念（采集层无向量需求，
  AI 向量化在消费方仓库各自维护）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


# ---- 连接 -------------------------------------------------------------------


def get_conn(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接。

    - 自动创建父目录（若 ``db_path`` 含尚不存在的父路径）。
    - 启用 ``row_factory = sqlite3.Row``，方便按列名取值。
    - 启用 ``PRAGMA foreign_keys = ON``。
    - 调用方负责关闭连接。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---- 迁移 -------------------------------------------------------------------


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.commit()


def _applied_filenames(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT filename FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path
) -> list[str]:
    """扫描 ``migrations_dir`` 下 ``*.sql`` 按文件名升序应用未执行过的迁移。

    返回本次新应用的文件名列表（按应用顺序）。
    幂等：第二次调用时已应用的迁移会被跳过，返回空列表。
    """
    migrations_dir = Path(migrations_dir)
    _ensure_migrations_table(conn)
    applied = _applied_filenames(conn)

    sql_files = sorted(p for p in migrations_dir.glob("*.sql"))
    newly_applied: list[str] = []

    for sql_file in sql_files:
        if sql_file.name in applied:
            continue

        sql_text = sql_file.read_text(encoding="utf-8")
        if sql_text.strip():
            conn.executescript(sql_text)

        conn.execute(
            "INSERT INTO schema_migrations(filename, applied_at) VALUES (?, ?)",
            (sql_file.name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        newly_applied.append(sql_file.name)
        logger.info(f"已应用迁移：{sql_file.name}")

    return newly_applied


# ---- 高层入口 ---------------------------------------------------------------


def _package_migrations_dir() -> Path:
    """定位包内 migrations 目录（与本模块同级）。"""
    return Path(__file__).parent / "migrations"


def init_db(db_path: Path) -> None:
    """新建/打开 ``db_path``，应用包内所有迁移，完成后关闭连接。"""
    conn = get_conn(db_path)
    try:
        apply_migrations(conn, _package_migrations_dir())
    finally:
        conn.close()
