"""``news-collector status`` — 系统综合健康总览。

输出三段：
- Containers   ← docker compose ps（rsshub + redis）
- Database     ← raw.db 路径 / 大小 / 总行数 / 上次 fetch 时间
- Recent failures ← source_state 中 consecutive_failures>0 的 top 5

设计取舍：
- 只读命令：不调 ``init_db``，避免在仅查询场景里触发 schema 应用。
- 各段独立降级：
    * docker daemon / CLI 不可用 → Containers 段打印 ``(docker unavailable: ...)``
    * raw.db 不存在 → Database 段打印提示，但**不**退出 1，仍把容器/路径展示给用户
    * source_state 表没有失败记录 → Recent failures 段打印 ``(no failures recorded)``
- 总是 exit 0：status 是健康总览，调用方靠看输出判断，而不是靠退出码。
- 与 ``state`` 命令共用「last_error 截断到 60 字符 + …、时间截断到 19 字符」约定。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import typer

from .docker_helpers import DockerError, container_status
from ._helpers import home_option

# ---- 常量 -------------------------------------------------------------------

_ERROR_TRUNCATE = 60
_TIME_KEEP = 19  # len("YYYY-MM-DD HH:MM:SS")
_FAILURES_TOP_N = 5


# ---- 工具 -------------------------------------------------------------------


def _format_time(raw: str | None) -> str:
    if raw is None:
        return "never"
    s = str(raw).replace("T", " ")
    return s[:_TIME_KEEP]


def _format_error(raw: str | None) -> str:
    if raw is None or raw == "":
        return "-"
    s = str(raw)
    if len(s) > _ERROR_TRUNCATE:
        return s[: _ERROR_TRUNCATE - 1] + "…"
    return s


def _format_size_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


# ---- 各段渲染 ---------------------------------------------------------------


def _print_containers(compose_file: Path) -> None:
    typer.echo("== Containers ==")
    try:
        status = container_status(compose_file)
    except DockerError as exc:
        # DockerError message 可能多行（如 yml 缺失会附 setup 引导提示）。
        # 旧版 `f"  (docker unavailable: {exc})"` 把多行包进括号，第二行
        # 顶到行首没缩进，括号闭合错位——v0.5.2 实测视觉残缺。
        # 改为标题行 + 内容按行缩进 4 空格，第二行起天然对齐。
        typer.echo("  docker unavailable:")
        for line in str(exc).split("\n"):
            typer.echo(f"    {line}")
        return

    # 固定顺序 rsshub / redis 而非 dict 序，输出更稳定
    for svc in ("rsshub", "redis"):
        state = status.get(svc, "Missing")
        typer.echo(f"  {svc:<7}: {state}")


def _print_database(db_path: Path) -> None:
    typer.echo("== Database ==")
    typer.echo(f"  path        : {db_path}")

    if not db_path.exists():
        typer.echo(
            f"  (raw.db not found: {db_path}; run news-collector setup)"
        )
        return

    size = db_path.stat().st_size
    typer.echo(f"  size        : {_format_size_mb(size)}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        with closing(conn.execute("SELECT COUNT(*) AS c FROM articles_raw")) as cur:
            total_rows = cur.fetchone()["c"]
        with closing(
            conn.execute("SELECT MAX(last_fetch_at) AS t FROM source_state")
        ) as cur:
            last_fetch = cur.fetchone()["t"]
    finally:
        conn.close()

    typer.echo(f"  total rows  : {total_rows}")
    typer.echo(f"  last fetch  : {_format_time(last_fetch)}")


def _print_failures(db_path: Path) -> None:
    typer.echo(f"== Recent failures (top {_FAILURES_TOP_N}) ==")
    if not db_path.exists():
        # 数据库不存在时也跳过 — Database 段已说明，这里不重复噪音
        typer.echo("  (no failures recorded)")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        with closing(
            conn.execute(
                "SELECT source_type, source_id, consecutive_failures, last_error "
                "FROM source_state "
                "WHERE consecutive_failures > 0 "
                "ORDER BY consecutive_failures DESC "
                "LIMIT ?",
                (_FAILURES_TOP_N,),
            )
        ) as cur:
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("  (no failures recorded)")
        return

    # 列宽自适应（source_type 固定窄；source_id 取实际最长）
    w_type = max(3, max(len(r["source_type"]) for r in rows))
    w_id = max(len("source_id"), max(len(r["source_id"]) for r in rows))

    for r in rows:
        line = (
            f"  {r['source_type']:<{w_type}}  "
            f"{r['source_id']:<{w_id}}  "
            f"fails={int(r['consecutive_failures'])}  "
            f"err={_format_error(r['last_error'])}"
        )
        typer.echo(line)


# ---- 命令 -------------------------------------------------------------------


def status_cmd(
    home: Path = home_option(),
) -> None:
    """系统综合健康总览：容器 + 数据库 + 最近失败信源。"""
    db_path = home / "raw.db"
    compose_file = home / "docker-compose.yml"

    _print_containers(compose_file)
    typer.echo("")
    _print_database(db_path)
    typer.echo("")
    _print_failures(db_path)
