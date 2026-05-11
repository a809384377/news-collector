"""``news-collector state`` — 采集状态查询。

只读命令：从 raw.db 的 ``source_state`` 表按 ``last_fetch_at`` 倒序展示每个信源的
最近抓取情况。NULL 的 last_fetch_at 排在末尾。

设计要点：
- 只读：不调 ``init_db``，避免在仅查询的命令里触发 schema 应用。
- raw.db 不存在时友好报错并退出 1（区别于 source_state 表为空时退出 0）。
- last_error 长度截断到 60 字符（含末尾 '…'），避免行过长打乱表格对齐。
- last_fetch_at 显示截断到 19 字符（``YYYY-MM-DD HH:MM:SS``），ISO 8601 微秒和
  时区后缀对人/agent 都没价值。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import typer

from ._helpers import home_option

# ---- 常量 -------------------------------------------------------------------

_ERROR_TRUNCATE = 60
_TIME_KEEP = 19  # len("YYYY-MM-DD HH:MM:SS")


# ---- 工具 -------------------------------------------------------------------


def _format_time(raw: str | None) -> str:
    if raw is None:
        return "never"
    # ISO 8601: 2026-05-09T23:45:12.123456+00:00 → 2026-05-09 23:45:12
    # 把 'T' 换成空格、截断到 19 字符即可。
    s = str(raw).replace("T", " ")
    return s[:_TIME_KEEP]


def _format_error(raw: str | None) -> str:
    if raw is None or raw == "":
        return "-"
    s = str(raw)
    if len(s) > _ERROR_TRUNCATE:
        return s[: _ERROR_TRUNCATE - 1] + "…"
    return s


# ---- 命令 -------------------------------------------------------------------


def state_cmd(
    home: Path = home_option(),
    source_type: str | None = typer.Option(
        None,
        "--source-type",
        help="过滤 rss / web",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="最多显示 N 行（默认全部）",
    ),
) -> None:
    """采集状态查询：列出 source_state 表，按 last_fetch_at 倒序。"""
    db_path = home / "raw.db"
    if not db_path.exists():
        typer.echo(
            f"[err] raw.db 未找到: {db_path}，请先 news-collector setup 或 fetch 一次",
            err=True,
        )
        raise typer.Exit(code=1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT source_type, source_id, last_fetch_at, "
            "       last_error, consecutive_failures "
            "FROM source_state "
        )
        params: list[object] = []
        if source_type is not None:
            sql += "WHERE source_type = ? "
            params.append(source_type)
        # NULL last_fetch_at 排末尾：SQLite 默认 ASC 时 NULL 在前、DESC 时 NULL 在后。
        # 为了「DESC 但 NULL 在末尾」，按 (last_fetch_at IS NULL) ASC 再 last_fetch_at DESC。
        sql += "ORDER BY (last_fetch_at IS NULL) ASC, last_fetch_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with closing(conn.execute(sql, params)) as cur:
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("  (no source state recorded; run news-collector fetch first)")
        raise typer.Exit(code=0)

    # 列宽（参考样例 + 内容自适应取较大值）
    w_type = max(len("source_type"), max(len(r["source_type"]) for r in rows))
    w_id = max(len("source_id"), max(len(r["source_id"]) for r in rows))
    w_time = max(len("last_fetch_at"), _TIME_KEEP, len("never"))
    w_fails = max(
        len("fails"),
        max(len(str(r["consecutive_failures"] or 0)) for r in rows),
    )

    header = (
        f"  {'source_type':<{w_type}}  "
        f"{'source_id':<{w_id}}  "
        f"{'last_fetch_at':<{w_time}}  "
        f"{'fails':<{w_fails}}  "
        f"last_error"
    )
    typer.echo(header)

    failure_count = 0
    for r in rows:
        fails = int(r["consecutive_failures"] or 0)
        if fails > 0:
            failure_count += 1
        line = (
            f"  {r['source_type']:<{w_type}}  "
            f"{r['source_id']:<{w_id}}  "
            f"{_format_time(r['last_fetch_at']):<{w_time}}  "
            f"{str(fails):<{w_fails}}  "
            f"{_format_error(r['last_error'])}"
        )
        typer.echo(line)

    typer.echo(f"  ({len(rows)} sources, {failure_count} with failures)")
