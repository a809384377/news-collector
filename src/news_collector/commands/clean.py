"""``news-collector clean`` — 清理 raw.db 中过老的 articles_raw 行。

破坏性命令，防呆设计是核心：

- ``--before`` 必填；走 ``utils.time.parse_since`` 解析。``cutoff`` 后凡
  ``fetched_at < cutoff`` 的 articles_raw 行被删掉。
- 默认 dry-run：只输出"将删多少 / 保留多少 / 总共多少"。dry-run 无副作用，
  在交互终端 / 非交互环境（脚本 / agent）下都能跑，方便调用方先查"会删多少"。
- 真删需显式 ``--yes``——这就是唯一的真删确认开关。无 ``--yes`` 则一律走
  dry-run，不另设 stdin tty 检测（s5 D5 修正：dry-run 是无害的，强制 tty 检
  测属于过度防呆）。
- 默认 ``--vacuum`` 在真删后跑 VACUUM，回收磁盘。``--no-vacuum`` 跳过。

注意：
- VACUUM 必须在事务外。Python ``sqlite3`` 默认 ``isolation_level=""`` 会自动
  ``BEGIN``，调用 VACUUM 前需要先 ``commit()``，再把 ``isolation_level`` 设为
  ``None``，跑完恢复 ``""``。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import typer

from ..utils.time import parse_since
from ._helpers import home_option

# ---- 工具 -------------------------------------------------------------------


def _format_time(dt) -> str:
    """统一显示成 ``YYYY-MM-DD HH:MM:SS``（UTC，不含 tz 后缀）。"""
    # parse_since 返回的 datetime 都带 tzinfo（UTC）；统一去秒后小数 + tz 显示。
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _format_size(num_bytes: int) -> str:
    """``187.2 MB`` 风格。"""
    return f"{num_bytes / 1024 / 1024:.1f} MB"


# ---- 命令 -------------------------------------------------------------------


def clean_cmd(
    home: Path = home_option(),
    before: str = typer.Option(
        ...,
        "--before",
        help="删除 fetched_at < <cutoff> 的行；支持 30d / 7d / 24h / 2026-04-10",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="真删开关；不传则只 dry-run（dry-run 在任意环境都能跑）。",
    ),
    vacuum: bool = typer.Option(
        True,
        "--vacuum/--no-vacuum",
        help="真删后跑 VACUUM 回收磁盘（默认 on）",
    ),
) -> None:
    """清理 raw.db 中过老的 articles_raw 行（带 dry-run / 防呆 / VACUUM）。"""
    db_path = home / "raw.db"
    if not db_path.exists():
        typer.echo(f"[err] raw.db 未找到: {db_path}", err=True)
        raise typer.Exit(code=1)

    # 解析 --before
    try:
        cutoff = parse_since(before)
    except ValueError as exc:
        typer.echo(f"[err] --before 解析失败: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if cutoff is None:
        # parse_since 对空串返回 None；但这里 typer.Option(...) 必填会拦掉空值。
        # 兜底：仍按 exit 2 报错。
        typer.echo("[err] --before 必须提供（如 30d / 7d）", err=True)
        raise typer.Exit(code=2)

    conn = sqlite3.connect(str(db_path))
    try:
        with closing(
            conn.execute(
                "SELECT COUNT(*) FROM articles_raw WHERE fetched_at < ?",
                (cutoff.isoformat(),),
            )
        ) as cur:
            cnt_to_delete = int(cur.fetchone()[0])
        with closing(conn.execute("SELECT COUNT(*) FROM articles_raw")) as cur:
            cnt_total = int(cur.fetchone()[0])
        cnt_keep = cnt_total - cnt_to_delete

        cutoff_str = _format_time(cutoff)

        if not yes:
            # ---- dry-run 路径 -------------------------------------------------
            typer.echo("[dry-run] news-collector clean")
            typer.echo("")
            typer.echo(
                f"  cutoff (fetched_at <):  {cutoff_str}  (--before={before})"
            )
            typer.echo("")
            typer.echo(f"  Articles to delete:    {cnt_to_delete:>6,}")
            typer.echo(f"  Articles to keep:      {cnt_keep:>6,}")
            typer.echo(f"  Total in raw.db:       {cnt_total:>6,}")
            typer.echo("")
            typer.echo("  This is a DRY RUN. To actually delete, re-run with --yes.")
            return

        # ---- --yes 真删路径 -------------------------------------------------
        db_size_before = db_path.stat().st_size

        typer.echo("[execute] news-collector clean --yes")
        typer.echo("")
        typer.echo(
            f"  cutoff (fetched_at <):  {cutoff_str}  (--before={before})"
        )
        typer.echo("")
        typer.echo(f"  Deleting {cnt_to_delete:,} articles ...  done.")

        conn.execute(
            "DELETE FROM articles_raw WHERE fetched_at < ?",
            (cutoff.isoformat(),),
        )
        conn.commit()

        if vacuum:
            # VACUUM 要求没有活跃事务：先 commit（已 commit 过），再把
            # isolation_level 设为 None 防止 sqlite3 自动 BEGIN，跑完恢复。
            prev_iso = conn.isolation_level
            conn.isolation_level = None
            try:
                conn.execute("VACUUM")
            finally:
                conn.isolation_level = prev_iso

            db_size_after = db_path.stat().st_size
            typer.echo(
                f"  Running VACUUM ...           done "
                f"(db.size: {_format_size(db_size_before)} → "
                f"{_format_size(db_size_after)})"
            )
            typer.echo("")
            typer.echo("  ⚠ VACUUM 期间数据库加锁，请勿同时运行 fetch。")
    finally:
        conn.close()
